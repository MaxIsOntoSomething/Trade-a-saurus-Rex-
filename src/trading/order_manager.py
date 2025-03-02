import asyncio
import logging
import time  # Add this import
import os
import platform
from datetime import datetime, timedelta
from typing import Dict, List
from ..types.models import Order, OrderStatus, TimeFrame, OrderType, TradeDirection
from ..trading.binance_client import BinanceClient
from ..database.mongo_client import MongoClient
from ..telegram.bot import TelegramBot
from .futures_client import FuturesClient
from binance.error import ClientError  # Add this import

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
        self.check_interval = 60
        logger.setLevel(logging.DEBUG)
        self.clear_command = 'cls' if platform.system() == 'Windows' else 'clear'
        
        # Add client type tracking
        self.client_type = config['environment']['trading_mode']
        
        # Initialize futures client if enabled
        self.futures_client = None
        if (config['environment']['trading_mode'] == 'futures' or 
            config['trading'].get('futures', {}).get('enabled', False)):
            futures_config = {
                **config['binance']['testnet_futures' if config['environment']['testnet'] else 'mainnet'],
                'testnet': config['environment']['testnet'],
                'config': config,  # Pass the full config
                'trading': config['trading'],  # Pass trading settings directly
                'reserve_balance': config['trading']['reserve_balance'],
                **config['trading'].get('futures_settings', {})
            }
            self.futures_client = FuturesClient(futures_config)
            self.active_client = self.futures_client

        # Track active client
        self.active_client = binance_client

    async def start(self):
        """Start the order manager with futures support"""
        self.running = True
        logger.info("Starting order monitoring...")
        
        # Initialize futures client if enabled
        if self.futures_client:
            await self.futures_client.initialize()
            logger.info("Futures trading enabled")
            
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
            # Check MongoDB connection first
            mongo_status = await asyncio.wait_for(
                self.mongo_client.db.command("ping"),
                timeout=5.0
            )
            
            if not mongo_status.get('ok', 0):
                raise ConnectionError("MongoDB ping failed")

            # Check exchange connection based on client type
            if isinstance(self.active_client, FuturesClient):
                await asyncio.wait_for(
                    self.active_client.ping(),
                    timeout=5.0
                )
            else:
                await asyncio.wait_for(
                    self.binance_client.client.ping(),
                    timeout=5.0
                )

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

    async def get_price(self, symbol: str) -> float:
        """Get price with fallback between spot and futures"""
        try:
            # Try primary client first
            try:
                ticker = await self.active_client.get_symbol_ticker(symbol=symbol)
                return float(ticker['price'])
            except AttributeError:
                # Fallback to alternate client
                if self.futures_client and isinstance(self.active_client, BinanceClient):
                    ticker = await self.futures_client.get_symbol_ticker(symbol=symbol)
                    return float(ticker['price'])
                elif isinstance(self.active_client, FuturesClient):
                    ticker = await self.binance_client.get_symbol_ticker(symbol=symbol)
                    return float(ticker['price'])
                raise
        except ClientError as e:
            # ClientError doesn't have a code attribute, parse from error string
            error_str = str(e)
            if '-1121' in error_str and 'Invalid symbol' in error_str:
                logger.error(f"Invalid symbol: {symbol}")
            else:
                logger.error(f"Failed to get price for {symbol}: {e}")
            raise
        except KeyError:
            logger.error(f"Price key missing in ticker response for {symbol}")
            raise
        except Exception as e:
            logger.error(f"Failed to get price for {symbol}: {e}")
            raise

    async def process_symbol(self, symbol: str):
        """Process symbol with improved error handling and fallback"""
        try:
            # Use new price getter with fallback
            current_price = await self.get_price(symbol)
            logger.info(f"Current {symbol} price: ${current_price:,.2f}")

            # Check if symbol is futures-enabled
            is_futures = (
                self.futures_client and 
                symbol in self.config['trading'].get('futures_settings', {}).get('allowed_pairs', [])
            )
            
            # Get current price from correct client
            client = self.futures_client if is_futures else self.binance_client
            # Use client's get_symbol_ticker method which handles the differences
            ticker = await client.get_symbol_ticker(symbol=symbol)
            current_price = float(ticker.get('price', 0))
            logger.info(f"Current {symbol} price: ${current_price:,.2f}")

            # Update reference prices using correct client
            await client.update_reference_prices([symbol])
            await asyncio.sleep(0.1)

            for timeframe in TimeFrame:
                await client.check_timeframe_reset(timeframe)
                logger.info(f"Checking {timeframe.value} timeframe...")
                
                thresholds = self.config['trading']['thresholds'][timeframe.value]
                triggered = await client.check_thresholds(
                    symbol, {timeframe.value: thresholds}
                )
                
                if triggered:
                    tf, threshold = triggered
                    logger.info(f"🎯 Trigger: {symbol} {threshold}% on {timeframe.value}")
                    # Create buy order
                    await self.create_order(symbol, timeframe, threshold)
                    logger.info(f"Created buy order for {symbol} at threshold {threshold}%")
                
                await asyncio.sleep(0.1)  # Small delay between timeframes

            # Add futures position check if applicable
            if is_futures:
                await self.monitor_futures_positions()

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
        """Create and store an order with improved futures support"""
        try:
            # Get order amount using the new calculation method
            amount = await self.binance_client.calculate_trade_amount()
            
            # Get current price for the signal
            ticker = await self.active_client.get_symbol_ticker(symbol=symbol)
            signal_price = float(ticker['price'])

            # Check if this should be a futures order
            is_futures = (
                self.futures_client and 
                symbol in self.config['trading'].get('futures_settings', {}).get('allowed_pairs', [])
            )
            
            order = None
            if is_futures:
                # Use futures client for futures orders
                order = await self.futures_client.place_futures_order(
                    symbol=symbol,
                    amount=amount,  # Use calculated amount
                    direction=TradeDirection.LONG,
                    leverage=self.config['trading']['futures_settings']['default_leverage'],
                    margin_type=self.config['trading']['futures_settings']['margin_type'],
                    signal_price=signal_price,
                    threshold=threshold,
                    timeframe=timeframe
                )
            else:
                # Use spot client for spot orders
                order = await self.binance_client.place_limit_buy_order(
                    symbol=symbol,
                    amount=amount,  # Use calculated amount
                    threshold=threshold,
                    timeframe=timeframe
                )
            
            if order:
                logger.info(f"Order created: {order.order_id} ({order.order_type.value})")
                await self.mongo_client.insert_order(order)
                await self.telegram_bot.send_order_notification(order)
            else:
                logger.error(f"Failed to create order for {symbol}")
                return  # Return without raising error
            
        except Exception as e:
            logger.error(f"Error creating order: {e}")
            return  # Return without raising error

    async def monitor_orders(self):
        """Monitor both spot and futures orders"""
        try:
            pending_orders = await self.mongo_client.get_pending_orders()
            cancel_after = timedelta(hours=self.config['trading']['cancel_after_hours'])
            
            for order in pending_orders:
                # Handle futures orders
                if order.order_type == OrderType.FUTURES:
                    if self.futures_client:
                        # Get position info
                        positions = await self.futures_client.get_open_positions()
                        if order.symbol in positions:
                            position_data = positions[order.symbol]
                            # Check if position should be closed
                            if datetime.utcnow() - order.created_at > cancel_after:
                                closing_order = await self.futures_client.close_position(
                                    order.symbol,
                                    position_data
                                )
                                if closing_order:
                                    await self.mongo_client.update_order_status(
                                        order.order_id,
                                        OrderStatus.FILLED,
                                        filled_at=datetime.utcnow()
                                    )
                                    await self.telegram_bot.send_roar(closing_order)
                    continue
                
                # Handle spot orders
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

    async def check_futures_positions(self):
        """Monitor open futures positions"""
        if not self.futures_client:
            return
            
        try:
            positions = await self.futures_client.get_open_positions()
            account_info = await self.futures_client.get_account_info()
            
            # Log positions and account info
            logger.info(f"Open Futures Positions: {len(positions)}")
            logger.info(f"Account Balance: ${float(account_info['totalWalletBalance']):,.2f}")
            logger.info(f"Unrealized PNL: ${float(account_info['totalUnrealizedProfit']):,.2f}")
            
            # Check for position updates
            for symbol, position in positions.items():
                # Process position updates
                # Add your position management logic here
                pass
                
        except Exception as e:
            logger.error(f"Error checking futures positions: {e}")

    async def switch_client(self, client_type: str):
        """Switch between spot and futures clients with improved error handling"""
        try:
            if client_type == self.client_type:
                return

            if (client_type == "futures"):
                if not self.futures_client:
                    logger.error("Futures client not initialized")
                    return
                self.active_client = self.futures_client
                self.client_type = "futures"
                logger.debug("Switched to futures client")
            elif client_type == "spot":
                self.active_client = self.binance_client
                self.client_type = "spot"
                logger.debug("Switched to spot client")
            else:
                logger.error(f"Invalid client type: {client_type}")
                raise ValueError(f"Invalid client type: {client_type}")

        except Exception as e:
            logger.error(f"Error switching client: {e}")
            raise

    async def monitor_futures_positions(self):
        """Monitor and manage futures positions"""
        if not self.futures_client:
            return
            
        try:
            positions = await self.futures_client.get_open_positions()
            for symbol, position in positions.items():
                # Update position in database
                await self.mongo_client.update_futures_position(position)
                
                # Check position age
                created_at = datetime.fromtimestamp(float(position['updateTime']) / 1000)
                age = datetime.utcnow() - created_at
                
                # Close old positions
                if age > timedelta(hours=self.config['trading']['cancel_after_hours']):
                    closing_order = await self.futures_client.close_position(symbol, position)
                    if closing_order:
                        await self.mongo_client.insert_order(closing_order)
                        await self.telegram_bot.send_roar(closing_order)
                        
        except Exception as e:
            logger.error(f"Error monitoring futures positions: {e}")

    async def check_order_status(self, order: Order):
        """Check and update order status with TP/SL tracking"""
        try:
            # Check main order status first
            current_status = await self.active_client.check_order_status(
                order.symbol, 
                order.order_id
            )
            
            if current_status == OrderStatus.FILLED and order.status != OrderStatus.FILLED:
                # Main order just filled, start monitoring TP/SL
                await self._handle_order_fill(order)
            
            # If main order is filled, check TP/SL status
            elif order.status == OrderStatus.FILLED:
                await self._check_tp_sl_status(order)

        except Exception as e:
            logger.error(f"Error checking order status: {e}")

    async def _check_tp_sl_status(self, order: Order):
        """Monitor TP/SL orders"""
        try:
            # Check TP order if exists
            if order.tp_order_id:
                tp_status = await self.active_client.check_order_status(
                    order.symbol, order.tp_order_id
                )
                if tp_status == OrderStatus.FILLED:
                    # TP hit - update order and cancel SL
                    await self._handle_tp_sl_trigger(order, "TP")
                    return

            # Check SL order if exists
            if order.sl_order_id:
                sl_status = await self.active_client.check_order_status(
                    order.symbol, order.sl_order_id
                )
                if sl_status == OrderStatus.FILLED:
                    # SL hit - update order and cancel remaining orders
                    await self._handle_tp_sl_trigger(order, "SL")
                    return

        except Exception as e:
            logger.error(f"Error checking TP/SL status: {e}")

    async def _handle_tp_sl_trigger(self, order: Order, trigger_type: str):
        """Handle TP or SL trigger"""
        try:
            # Get position info for PnL calculation
            position_info = await self.active_client.get_position_info(order.symbol)
            realized_pnl = float(position_info.get('realizedPnl', 0))
            exit_price = float(order.tp_price if trigger_type == "TP" else order.sl_price)

            # Update database
            await self.mongo_client.update_tp_sl_status(
                order_id=order.order_id,
                tp_status="FILLED" if trigger_type == "TP" else "CANCELLED",
                sl_status="FILLED" if trigger_type == "SL" else "CANCELLED",
                exit_type=trigger_type,
                exit_price=exit_price,
                realized_pnl=realized_pnl
            )

            # Cancel other orders
            if trigger_type == "TP" and order.sl_order_id:
                await self.active_client.cancel_order(order.symbol, order.sl_order_id)
            elif trigger_type == "SL" and order.tp_order_id:
                await self.active_client.cancel_order(order.symbol, order.tp_order_id)

            # Send notification
            if self.telegram_bot:
                await self.telegram_bot.send_tp_sl_notification(
                    order=order,
                    trigger_type=trigger_type,
                    exit_price=exit_price,
                    realized_pnl=realized_pnl
                )

        except Exception as e:
            logger.error(f"Error handling {trigger_type} trigger: {e}")

    async def sync_pending_orders(self, client, db_client):
        """
        Check all pending orders in the database; if any are filled on the exchange, update the status to FILLED.
        """
        # 1) Retrieve pending orders
        pending_orders = await db_client.get_pending_orders()
        # 2) Check each one against the exchange
        for order in pending_orders:
            exchange_status = await client.get_order_status(order.symbol, order.exchange_id)
            # 3) If order is filled on exchange, update to FILLED in DB
            if exchange_status == 'FILLED':
                await db_client.update_order_status(order.id, OrderStatus.FILLED)
