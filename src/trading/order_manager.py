import asyncio
import logging
import time  # Add this import
import os
import platform
from datetime import datetime, timedelta
from typing import Dict, List
from decimal import Decimal  # Add the missing Decimal import
from ..types.models import Order, OrderStatus, TimeFrame, TPSLStatus
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
        
        # Check if running in Docker environment
        self.running_in_docker = os.environ.get('RUNNING_IN_DOCKER', 'false').lower() == 'true'
        self.clear_command = 'cls' if platform.system() == 'Windows' else 'clear'
        
    async def start(self):
        """Start the order manager"""
        self.running = True
        logger.info("Starting order monitoring...")
        # Don't await the task, just create and return it
        self.monitor_task = asyncio.create_task(self.monitor_thresholds())
        
        # Add TP/SL monitoring task
        self.tp_sl_task = asyncio.create_task(self.monitor_tp_sl())
        
        return self.monitor_task  # Return the main monitoring task
        
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
                
        if hasattr(self, 'tp_sl_task') and self.tp_sl_task:
            self.tp_sl_task.cancel()
            try:
                await self.tp_sl_task
            except asyncio.CancelledError:
                pass
            
    async def check_connection_health(self):
        """Check if all connections are healthy"""
        try:
            # Check Binance connection
            await self.binance_client.client.ping()
            
            # Check MongoDB connection
            await self.mongo_client.db.command('ping')
            
            return True
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False
            
    async def process_symbol(self, symbol: str):
        """Process a single symbol for threshold checking"""
        try:
            # Skip if the symbol was removed during this cycle
            if hasattr(self.binance_client, 'removed_symbols_this_cycle') and symbol in self.binance_client.removed_symbols_this_cycle:
                logger.info(f"Skipping symbol {symbol} that was removed this cycle")
                return
                
            # Skip invalid symbols
            if symbol in self.binance_client.invalid_symbols:
                logger.debug(f"Skipping known invalid symbol: {symbol}")
                return
                
            # Skip if trading is paused
            if hasattr(self.binance_client, 'telegram_bot') and self.binance_client.telegram_bot.is_paused:
                logger.info(f"Trading is paused, skipping {symbol}")
                return
            
            # Also, double-check against current trading_symbols list in database
            if self.mongo_client:
                trading_symbols = await self.mongo_client.get_trading_symbols()
                if trading_symbols is not None and symbol not in trading_symbols:
                    logger.info(f"Skipping {symbol} - not in active trading symbols list")
                    return
                
            # Get current price
            current_price = await self.binance_client.get_current_price(symbol)
            
            # Check if price is None (could happen with invalid symbols)
            if current_price is None:
                logger.warning(f"Unable to get current price for {symbol}, skipping")
                return
                
            logger.info(f"Current {symbol} price: ${current_price:,.2f}")
            
            # Check if we have enough balance for at least one order
            order_amount = self.config['trading']['order_amount']
            has_enough_balance = await self.binance_client.check_reserve_balance(order_amount)
            
            if not has_enough_balance:
                logger.warning(f"Insufficient balance for orders. Current balance below required amount.")
                
            # Check thresholds for each timeframe
            for timeframe in TimeFrame:
                logger.info(f"Checking {timeframe.value} timeframe...")
                
                # Get triggered thresholds
                triggered_thresholds = await self.binance_client.check_thresholds(symbol, timeframe)
                
                # Process each triggered threshold
                for threshold in triggered_thresholds:
                    logger.info(f"🎯 Processing trigger: {symbol} {threshold}% on {timeframe.value}")
                    
                    # Skip order placement if balance is insufficient
                    if not has_enough_balance:
                        logger.warning(f"Skipping order for {symbol} at threshold {threshold}% due to insufficient balance")
                        continue
                    
                    # Attempt to place an order for this threshold
                    try:
                        # Place buy order
                        order = await self.binance_client.place_limit_buy_order(
                            symbol=symbol,
                            amount=order_amount,
                            threshold=threshold,
                            timeframe=timeframe
                        )
                        
                        if order:
                            # Save order to database
                            await self.mongo_client.insert_order(order)
                            
                            # Send notification
                            if self.telegram_bot:
                                await self.telegram_bot.send_order_notification(order)
                                
                            logger.info(f"Created buy order for {symbol} at threshold {threshold}%")
                        else:
                            logger.error(f"Failed to create buy order for {symbol} at threshold {threshold}%")
                    except Exception as e:
                        logger.error(f"Error placing order for {symbol} at threshold {threshold}%: {e}")
                        
        except Exception as e:
            logger.error(f"Error processing {symbol}: {e}")

    async def monitor_thresholds(self):
        logger.info("Starting price monitoring loop")
        self.check_interval = 5  # Now 5 seconds
        last_check = time.time()
        last_balance_record = datetime.now() - timedelta(hours=1)  # Force initial balance record

        while self.running:
            try:
                # Only clear terminal if not running in Docker
                if not self.running_in_docker:
                    os.system(self.clear_command)
                
                current_time = time.time()
                time_since_last = current_time - last_check
                logger.info(f"Time since last check: {time_since_last:.2f}s")
                last_check = current_time

                # Reset the removed_symbols_this_cycle at the start of each new cycle
                if hasattr(self.binance_client, 'removed_symbols_this_cycle'):
                    self.binance_client.removed_symbols_this_cycle = set()

                if not await self.check_connection_health():
                    logger.error("Connection health check failed, waiting 30s...")
                    await asyncio.sleep(30)
                    continue

                # Record balance once per hour
                now = datetime.now()
                if (now - last_balance_record).total_seconds() > 3600:  # 1 hour in seconds
                    try:
                        # Get current balance - explicitly use the configured base currency
                        base_currency = self.binance_client.base_currency
                        balance = await self.binance_client.get_balance(base_currency)
                        
                        # Get total invested amount - Fix: use the correct method name
                        invested = await self.calculate_invested_amount()
                        
                        # Record balance to MongoDB
                        if self.mongo_client:
                            await self.mongo_client.record_balance(
                                now, balance, invested
                            )
                            
                        # Log with the proper currency
                        logger.info(f"Recorded balance: ${float(balance):.2f} {base_currency}, Invested: ${float(invested):.2f}")
                        
                        last_balance_record = now
                    except Exception as e:
                        logger.error(f"Failed to record balance: {e}")

                if not self.telegram_bot.is_paused:
                    logger.info("\n" + "="*50)
                    logger.info("Starting price check cycle")
                    
                    # Get configured symbols, filtering out invalid ones
                    configured_symbols = self.config['trading']['pairs']
                    valid_symbols = [s for s in configured_symbols if s not in self.binance_client.invalid_symbols]
                    
                    if len(valid_symbols) < len(configured_symbols):
                        logger.info(f"Processing {len(valid_symbols)} valid symbols (skipping {len(configured_symbols) - len(valid_symbols)} invalid)")

                    # Process symbols sequentially to maintain log order
                    for symbol in valid_symbols:
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
                        
                        # Create TP/SL orders if configured
                        await self.binance_client.create_tp_sl_orders(order)
                        # Update order in database with TP/SL information
                        await self.mongo_client.insert_order(order)
                        
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

    async def calculate_invested_amount(self) -> Decimal:
        """Calculate the total amount currently invested"""
        try:
            # Get sum of all filled orders that haven't been sold yet
            pipeline = [
                {"$match": {"status": OrderStatus.FILLED.value}},
                {"$group": {
                    "_id": "$symbol",
                    "total_invested": {
                        "$sum": {
                            "$multiply": [
                                {"$toDecimal": "$price"},
                                {"$toDecimal": "$quantity"}
                            ]
                        }
                    }
                }},
                {"$group": {
                    "_id": None,
                    "grand_total": {"$sum": "$total_invested"}
                }}
            ]
            
            result = await self.mongo_client.orders.aggregate(pipeline).to_list(1)
            
            if result and len(result) > 0:
                return Decimal(str(result[0]["grand_total"]))
            return Decimal('0')
            
        except Exception as e:
            logger.error(f"Error calculating invested amount: {e}")
            return Decimal('0')

    async def _check_timeframe_resets(self):
        """Check if any timeframes need to be reset"""
        try:
            for timeframe in TimeFrame:
                reset_occurred = await self.binance_client.check_timeframe_reset(timeframe)
                if reset_occurred:
                    logger.info(f"Timeframe {timeframe.value} was reset")
                    # Additional reset-related tasks can be added here
        except Exception as e:
            logger.error(f"Error checking timeframe resets: {e}")

    async def run_trading_cycle(self):
        """Run one trading cycle"""
        try:
            # Skip if trading is paused
            if (
                hasattr(self.telegram_bot, "is_paused") and 
                self.telegram_bot.is_paused
            ):
                logger.debug("Trading is paused, skipping cycle")
                return

            # Check timeframe resets first - ensure this runs before other trading operations
            await self._check_timeframe_resets()
            
            # Process each trading pair
            for symbol in self.config['trading']['pairs']:
                if not self.running:
                    break
                    
                # Process the symbol
                await self.process_symbol(symbol)
                
            # Check pending orders
            pending_count = await self.mongo_client.orders.count_documents(
                {"status": OrderStatus.PENDING.value}
            )
            
            if pending_count > 0:
                logger.info(f"Found {pending_count} pending orders, checking status...")
                await self.monitor_orders()
                
        except Exception as e:
            logger.error(f"Error in trading cycle: {e}", exc_info=True)
            
    async def monitor_tp_sl(self):
        """Monitor take profit and stop loss levels for all active orders"""
        logger.info("Starting TP/SL monitoring")
        check_interval = 20  # Check every 20 seconds
        
        while self.running:
            try:
                # Skip if trading is paused
                if self.telegram_bot.is_paused:
                    await asyncio.sleep(check_interval)
                    continue
                
                # Get all orders with active (pending) TP/SL
                orders = await self.mongo_client.get_orders_with_active_tp_sl()
                
                if orders:
                    logger.info(f"Checking TP/SL for {len(orders)} orders")
                    
                    for order in orders:
                        # Skip if connection issues
                        if not await self.check_connection_health():
                            logger.error("Connection health check failed, waiting...")
                            await asyncio.sleep(30)
                            break
                            
                        # Check TP/SL triggers
                        triggers = await self.binance_client.check_tp_sl_triggers(order)
                        
                        # If anything triggered, update in database and notify
                        if triggers['tp_triggered'] or triggers['sl_triggered']:
                            # Update database with new status
                            await self.mongo_client.update_tp_sl_status(
                                order_id=order.order_id,
                                tp_status=order.take_profit.status if triggers['tp_triggered'] else None,
                                sl_status=order.stop_loss.status if triggers['sl_triggered'] else None,
                                tp_triggered_at=order.take_profit.triggered_at if triggers['tp_triggered'] else None,
                                sl_triggered_at=order.stop_loss.triggered_at if triggers['sl_triggered'] else None
                            )
                            
                            # Send notification
                            if self.telegram_bot:
                                if triggers['tp_triggered']:
                                    await self.telegram_bot.send_tp_notification(order)
                                if triggers['sl_triggered']:
                                    await self.telegram_bot.send_sl_notification(order)
                                    
                        # Add small delay between orders
                        await asyncio.sleep(0.5)
                
            except Exception as e:
                logger.error(f"Error in TP/SL monitoring: {e}", exc_info=True)
            
            # Sleep until next check
            await asyncio.sleep(check_interval)
