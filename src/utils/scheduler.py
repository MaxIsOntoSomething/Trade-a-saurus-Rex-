import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Dict
from ..telegram.Telegram import TelegramBot  # Use new Telegram.py
from ..database.mongo_client import MongoClient
from ..types.models import TimeFrame

logger = logging.getLogger(__name__)

class WeeklySummaryScheduler:
    def __init__(self, telegram_bot: TelegramBot, mongo_client: MongoClient):
        self.bot = telegram_bot
        self.mongo_client = mongo_client
        self.next_summary = self._get_next_summary_time()
        self.running = True

    def _get_next_summary_time(self) -> datetime:
        """Get next summary time (Sunday at 00:00 UTC)"""
        now = datetime.utcnow()
        days_ahead = 6 - now.weekday()  # 6 = Sunday
        if days_ahead <= 0:
            days_ahead += 7
        next_sunday = now + timedelta(days=days_ahead)
        return next_sunday.replace(hour=0, minute=0, second=0, microsecond=0)
        
    async def run(self):
        """Run the scheduler"""
        while self.running:
            try:
                now = datetime.utcnow()
                
                if now >= self.next_summary:
                    await self.bot.automation_manager.generate_weekly_summary()
                    self.next_summary = self._get_next_summary_time()
                    
                # Sleep until next check
                await asyncio.sleep(60)  # Check every minute
                
            except Exception as e:
                logger.error(f"Error in scheduler: {e}")
                await asyncio.sleep(300)  # Sleep 5 minutes on error

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
