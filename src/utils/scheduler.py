import asyncio
from datetime import datetime, timedelta
import logging
from typing import Dict
from ..types.models import TimeFrame

logger = logging.getLogger(__name__)

class WeeklySummaryScheduler:
    def __init__(self, telegram_bot, mongo_client):
        self.telegram_bot = telegram_bot
        self.mongo_client = mongo_client
        self.next_run = self._calculate_next_run()
        
    def _calculate_next_run(self) -> datetime:
        """Calculate next Sunday 17:00 UTC"""
        now = datetime.utcnow()
        days_ahead = 6 - now.weekday()  # 6 = Sunday
        if days_ahead <= 0:
            days_ahead += 7
        next_sunday = now + timedelta(days=days_ahead)
        return next_sunday.replace(hour=17, minute=0, second=0, microsecond=0)
        
    async def generate_weekly_summary(self) -> str:
        """Generate weekly trading summary"""
        try:
            # Get trading summary data
            summary = await self.mongo_client.get_trading_summary(include_futures=True)
            
            # Format message
            msg = [
                "📊 Weekly Trading Summary\n",
                f"Period: {(self.next_run - timedelta(days=7)).strftime('%Y-%m-%d')} to {self.next_run.strftime('%Y-%m-%d')}\n"
            ]
            
            # Add buy overview
            orders = await self.mongo_client.get_weekly_orders()
            if orders:
                msg.extend([
                    "\n🔵 Buy Orders Overview:",
                    f"Total Orders: {len(orders)}",
                    f"Total Volume: ${sum(float(o.price * o.quantity) for o in orders):,.2f}"
                ])
            
            # Add threshold updates
            triggered = await self.mongo_client.get_weekly_triggered_thresholds()
            if triggered:
                msg.extend([
                    "\n🎯 Threshold Updates:",
                    *[f"{t['symbol']}: {t['threshold']}% at ${float(t['price']):,.2f}" 
                      for t in triggered]
                ])
            
            # Add P/L analysis
            if summary.get('futures_orders'):
                msg.extend([
                    "\n💰 Futures P/L:",
                    f"Total PnL: ${float(summary['futures_orders'].get('total_pnl', 0)):+,.2f}",
                    f"Active Positions: {summary.get('active_positions', 0)}",
                    f"Unrealized PnL: ${float(summary.get('unrealized_pnl', 0)):+,.2f}"
                ])
            
            # Add equity allocation
            account = await self.telegram_bot.binance_client.get_account_info()
            msg.extend([
                "\n📈 Equity Allocation:",
                f"Total Balance: ${float(account.get('totalWalletBalance', 0)):,.2f}",
                f"Available: ${float(account.get('availableBalance', 0)):,.2f}",
                f"In Position: ${float(account.get('totalWalletBalance', 0)) - float(account.get('availableBalance', 0)):,.2f}"
            ])
            
            return "\n".join(msg)
            
        except Exception as e:
            logger.error(f"Error generating weekly summary: {e}")
            return "❌ Error generating weekly summary"

    async def run(self):
        """Run the scheduler"""
        while True:
            try:
                now = datetime.utcnow()
                
                if now >= self.next_run:
                    # Generate and send summary
                    summary = await self.generate_weekly_summary()
                    for user_id in self.telegram_bot.allowed_users:
                        try:
                            await self.telegram_bot.app.bot.send_message(
                                chat_id=user_id,
                                text=summary
                            )
                        except Exception as e:
                            logger.error(f"Failed to send summary to {user_id}: {e}")
                    
                    # Calculate next run
                    self.next_run = self._calculate_next_run()
                    
                # Sleep for 5 minutes
                await asyncio.sleep(300)
                
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
                await asyncio.sleep(60)
