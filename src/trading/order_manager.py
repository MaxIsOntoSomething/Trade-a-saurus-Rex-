import asyncio
import logging
import time  # Add this import
import os
import platform
from datetime import datetime, timedelta
from typing import Dict, List
from ..types.models import Order, OrderStatus, TimeFrame
from ..trading.binance_client import BinanceClient
from ..database.mongo_client import MongoClient
from ..telegram.bot import TelegramBot

logger = logging.getLogger(__name__)

class OrderManager:
    def __init__(self, binance_client: BinanceClient, mongo_client: MongoClient, 
                 telegram_bot: TelegramBot, config: dict):
        self.binance_client = binance_client
        self.mongo_client = mongo_client
        self.telegram_bot = telegram_bot
        self.config = config
        self.running = False
        self.monitor_task = None
        self.check_interval = 60  # 60 seconds between checks
        logger.setLevel(logging.DEBUG)  # Add this line
        self.clear_command = 'cls' if platform.system() == 'Windows' else 'clear'
        
    async def start(self):
        """Start the order manager"""
        self.running = True
        logger.info("Starting order monitoring...")
        # Don't await the task, just create and return it
        self.monitor_task = asyncio.create_task(self.monitor_thresholds())
        return self.monitor_task  # Return the task instead of awaiting it
        
    async def stop(self):
        """Stop the order manager"""
        logger.info("Stopping order monitoring...")
        self.running = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
            
    async def check_connection_health(self):
        """Check if all connections are healthy with improved error handling"""
        try:
            # Check Binance connection with timeout
            await asyncio.wait_for(self.binance_client.client.ping(), timeout=5.0)
            
            # Check MongoDB connection with timeout
            await asyncio.wait_for(self.mongo_client.db.command('ping'), timeout=5.0)
            
            return True
        except asyncio.TimeoutError as e:
            logger.error(f"Connection health check timeout: {e}")
            await self._notify_connection_issue("Connection timeout")
            return False
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            await self._notify_connection_issue(str(e))
            return False

    async def _notify_connection_issue(self, error_message: str):
        """Send connection issue notification"""
        if not hasattr(self, '_last_notification_time'):
            self._last_notification_time = 0

        current_time = time.time()
        # Only send notification every 5 minutes to avoid spam
        if current_time - self._last_notification_time >= 300:
            self._last_notification_time = current_time
            message = (
                "🚨 Connection Issue Detected!\n\n"
                f"Error: {error_message}\n"
                "Bot will automatically attempt to reconnect.\n"
                "Check server status if this persists."
            )
            try:
                for user_id in self.telegram_bot.allowed_users:
                    await self.telegram_bot.app.bot.send_message(
                        chat_id=user_id,
                        text=message
                    )
            except Exception as e:
                logger.error(f"Failed to send connection notification: {e}")

    async def process_symbol(self, symbol: str):
        """Check daily/weekly/monthly timeframes for a single symbol."""
        try:
            logger.info(f"\n=== Processing {symbol} ===")
            ticker = await self.binance_client.client.get_symbol_ticker(symbol=symbol)
            current_price = float(ticker['price'])
            logger.info(f"Current {symbol} price: ${current_price:,.2f}")

            # Fetch all reference prices first and wait for completion
            await self.binance_client.update_reference_prices([symbol])
            await asyncio.sleep(0.1)  # Small delay after fetching references

            # Check each timeframe in order
            for timeframe in TimeFrame:
                await self.binance_client.check_timeframe_reset(timeframe)
                logger.info(f"Checking {timeframe.value} timeframe...")
                
                # Check thresholds with proper timeframe values
                thresholds = self.config['trading']['thresholds'][timeframe.value]
                triggered = await self.binance_client.check_thresholds(
                    symbol, {timeframe.value: thresholds}
                )
                
                if triggered:
                    tf, threshold = triggered
                    logger.info(f"🎯 Trigger: {symbol} {threshold}% on {timeframe.value}")
                    # Create buy order
                    await self.create_order(symbol, timeframe, threshold)
                    logger.info(f"Created buy order for {symbol} at threshold {threshold}%")
                
                await asyncio.sleep(0.1)  # Small delay between timeframes

        except Exception as e:
            logger.error(f"Error processing {symbol}: {e}", exc_info=True)

    async def monitor_thresholds(self):
        """Monitor thresholds with improved error handling and recovery"""
        logger.info("Starting price monitoring loop")
        self.check_interval = 5  # 5 seconds between checks
        last_check = time.time()
        consecutive_failures = 0
        max_failures = 10  # Maximum consecutive failures before forced restart

        while self.running:
            try:
                # Clear terminal at start of new cycle
                os.system(self.clear_command)
                
                current_time = time.time()
                time_since_last = current_time - last_check
                logger.info(f"Time since last check: {time_since_last:.2f}s")
                last_check = current_time

                if not await self.check_connection_health():
                    consecutive_failures += 1
                    logger.error(f"Connection health check failed ({consecutive_failures}/{max_failures})")
                    
                    if consecutive_failures >= max_failures:
                        # Send critical error notification
                        message = (
                            "🚨 CRITICAL: Maximum connection failures reached!\n"
                            "Bot will attempt to restart.\n"
                            "Please check server status."
                        )
                        try:
                            for user_id in self.telegram_bot.allowed_users:
                                await self.telegram_bot.app.bot.send_message(
                                    chat_id=user_id,
                                    text=message
                                )
                        except Exception as e:
                            logger.error(f"Failed to send critical error notification: {e}")
                            
                        # Force restart by raising exception
                        raise RuntimeError("Maximum connection failures reached")
                        
                    await asyncio.sleep(30)
                    continue
                
                # Reset failure counter on successful health check
                consecutive_failures = 0

                if not self.telegram_bot.is_paused:
                    logger.info("\n" + "="*50)
                    logger.info("Starting price check cycle")
                    symbols = self.config['trading']['pairs']

                    # Process symbols sequentially to maintain log order
                    for symbol in symbols:
                        await self.process_symbol(symbol)
                        await asyncio.sleep(0.5)  # Small delay between symbols

                    logger.info("\nCompleted price check cycle")
                    logger.info("="*50)

                    # Only check orders if there are pending ones
                    pending_count = await self.mongo_client.orders.count_documents(
                        {"status": OrderStatus.PENDING.value}
                    )
                    
                    if pending_count > 0:
                        logger.info(f"\nFound {pending_count} pending orders...")
                        await self.monitor_orders()
                        # 3 second delay and clear are handled in monitor_orders
                else:
                    logger.info("Trading is paused")

                # Countdown with terminal clearing
                remaining = self.check_interval
                while remaining > 0 and self.running:
                    logger.info(f"Next check in {remaining} seconds...")
                    await asyncio.sleep(min(5, remaining))
                    remaining -= 5
                    if remaining > 0:  # Don't clear on last iteration
                        os.system(self.clear_command)

            except Exception as e:
                logger.error(f"Error in monitoring: {e}", exc_info=True)
                await asyncio.sleep(10)
            
    async def create_order(self, symbol: str, timeframe: TimeFrame, threshold: float):
        """Create and store a new order"""
        try:
            order = await self.binance_client.place_limit_buy_order(
                symbol=symbol,
                amount=self.config['trading']['order_amount'],
                threshold=threshold,
                timeframe=timeframe
            )
            
            await self.mongo_client.insert_order(order)
            await self.telegram_bot.send_order_notification(order)
            
        except Exception as e:
            logger.error(f"Failed to create order: {e}")
            
    async def monitor_orders(self):
        """Monitor and update status of pending orders"""
        try:
            pending_orders = await self.mongo_client.get_pending_orders()
            cancel_after = timedelta(hours=self.config['trading']['cancel_after_hours'])
            
            for order in pending_orders:
                # Check if order should be cancelled due to time
                if datetime.utcnow() - order.created_at > cancel_after:
                    if await self.binance_client.cancel_order(order.symbol, order.order_id):
                        order.status = OrderStatus.CANCELLED
                        order.cancelled_at = datetime.utcnow()
                        await self.mongo_client.update_order_status(
                            order.order_id, order.status, cancelled_at=order.cancelled_at
                        )
                        await self.telegram_bot.send_order_notification(order)
                        continue
                
                # Check current order status
                status = await self.binance_client.check_order_status(
                    order.symbol, order.order_id
                )
                
                if status and status != order.status:
                    order.status = status
                    if status == OrderStatus.FILLED:
                        order.filled_at = datetime.utcnow()
                        await self.mongo_client.update_order_status(
                            order.order_id, status, filled_at=order.filled_at
                        )
                        
                        # Check balance changes
                        balance_change = await self.binance_client.get_balance_changes()
                        if balance_change:
                            order.balance_change = balance_change
                            await self.telegram_bot.send_balance_update(
                                order.symbol, balance_change
                            )
                        # Send only ROAR notification for filled orders
                        await self.telegram_bot.send_roar(order)
                        
                    elif status == OrderStatus.CANCELLED:
                        order.cancelled_at = datetime.utcnow()
                        await self.mongo_client.update_order_status(
                            order.order_id, status, cancelled_at=order.cancelled_at
                        )
                        # Send notification only for cancelled orders
                        await self.telegram_bot.send_order_notification(order)

            # Add delay after order checks if there were orders
            await asyncio.sleep(3)
            os.system(self.clear_command)

        except Exception as e:
            logger.error(f"Error monitoring orders: {e}", exc_info=True)
