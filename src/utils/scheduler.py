import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Dict
from ..types.models import TimeFrame

logger = logging.getLogger(__name__)

class WeeklySummaryScheduler:
    def __init__(self, telegram_bot, mongo_client):
        self.telegram_bot = telegram_bot
        self.mongo_client = mongo_client
        self.target_day = 6  # Sunday
        self.target_hour = 17  # 17:00 UTC
        self.running = True

    async def run(self):
        """Run the weekly summary scheduler"""
        logger.info("Starting weekly summary scheduler")
        
        while self.running:
            try:
                now = datetime.now(timezone.utc)
                target = self._get_next_summary_time(now)
                delay = (target - now).total_seconds()
                
                logger.info(f"Next weekly summary scheduled for: {target}")
                await asyncio.sleep(delay)
                
                if self.running:  # Check if still running after sleep
                    await self.telegram_bot.automation_manager.generate_weekly_summary()
                    
            except Exception as e:
                logger.error(f"Error in weekly summary scheduler: {e}")
                await asyncio.sleep(300)  # Sleep 5 minutes on error

    def _get_next_summary_time(self, current: datetime) -> datetime:
        """Calculate next summary time"""
        days_ahead = self.target_day - current.weekday()
        if days_ahead <= 0:  # Target time has passed this week
            days_ahead += 7
            
        next_time = current.replace(
            hour=self.target_hour, 
            minute=0, 
            second=0, 
            microsecond=0
        ) + timedelta(days=days_ahead)
            
        return next_time

    async def stop(self):
        """Stop the scheduler"""
        self.running = False

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
