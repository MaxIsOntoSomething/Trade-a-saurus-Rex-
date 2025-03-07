from binance.client import AsyncClient
from binance.exceptions import BinanceAPIException
from decimal import Decimal
from datetime import datetime, timedelta
import asyncio
import logging
from typing import Dict, List, Optional, Tuple
import aiohttp
import json
from ..types.models import Order, OrderStatus, TimeFrame, OrderType  # Add OrderType
from ..utils.rate_limiter import RateLimiter
from ..types.constants import PRECISION, MIN_NOTIONAL, TIMEFRAME_INTERVALS, TRADING_FEES
from ..utils.chart_generator import ChartGenerator
from ..utils.yahoo_scrapooooor_sp500 import YahooSP500Scraper  # Import the new Yahoo scraper

logger = logging.getLogger(__name__)

class BinanceClient:
    def __init__(self, api_key: str, api_secret: str, telegram_bot=None, mongo_client=None, config=None, testnet: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.client = None
        self.reference_prices = {}
        self.triggered_thresholds = {}
        self.rate_limiter = RateLimiter()
        self.symbol_info = {}
        self.last_reset = {
            tf: datetime.utcnow() for tf in TimeFrame
        }
        self.balance_cache = {}
        self.reference_timestamps = {
            TimeFrame.DAILY: None,
            TimeFrame.WEEKLY: None,
            TimeFrame.MONTHLY: None
        }
        logger.setLevel(logging.DEBUG)
        self.telegram_bot = telegram_bot
        self.chart_generator = ChartGenerator()
        self.reserve_balance = None
        self.base_currency = None
        self.mongo_client = mongo_client
        self.config = config
        
        # Initialize Yahoo SP500 scraper
        self.yahoo_scraper = YahooSP500Scraper()
        
        # Initialize thresholds dictionary with nested structure to track triggered thresholds
        self.triggered_thresholds = {}
        
        # Initialize pairs with nested dictionaries for each timeframe if config is provided
        if config and 'trading' in config and 'pairs' in config['trading']:
            pairs = config['trading']['pairs']
            for pair in pairs:
                self.triggered_thresholds[pair] = {
                    'daily': set(),
                    'weekly': set(),
                    'monthly': set()
                }
        
    def set_telegram_bot(self, bot):
        """Set telegram bot for notifications"""
        self.telegram_bot = bot
        
    async def check_initial_balance(self) -> bool:
        """Check if current balance is above reserve requirement"""
        try:
            if self.reserve_balance is None or self.reserve_balance <= 0:
                logger.info(f"Initial balance check skipped - no reserve requirement (value: {self.reserve_balance})")
                return True

            if not self.base_currency:
                logger.warning("Initial balance check skipped - no base currency specified")
                return True

            current_balance = await self.get_balance(self.base_currency)
            if float(current_balance) < self.reserve_balance:
                logger.error(
                    f"Initial balance check failed:\n"
                    f"Current Balance: ${float(current_balance):.2f}\n"
                    f"Reserve Balance: ${self.reserve_balance:.2f}\n"
                    f"Trading will be paused until balance is above reserve."
                )
                if self.telegram_bot:
                    self.telegram_bot.is_paused = True
                    await self.telegram_bot.send_initial_balance_alert(
                        current_balance=current_balance,
                        reserve_balance=self.reserve_balance
                    )
                return False

            logger.info(
                f"Initial balance check passed:\n"
                f"Current Balance: ${float(current_balance):.2f}\n"
                f"Reserve Balance: ${self.reserve_balance:.2f}"
            )
            return True

        except Exception as e:
            logger.error(f"Error checking initial balance: {e}")
            return False

    async def initialize(self):
        """Initialize client with reserve balance and base currency"""
        self.client = await AsyncClient.create(
            api_key=self.api_key,
            api_secret=self.api_secret,
            testnet=self.testnet
        )

        # Set reserve balance directly from config with debug logging
        if self.telegram_bot and self.telegram_bot.config:
            self.base_currency = self.telegram_bot.config['trading']['base_currency']
            self.reserve_balance = float(self.telegram_bot.config['trading'].get('reserve_balance', 0))
            logger.info(f"[INIT] Base Currency: {self.base_currency}")
            logger.info(f"[INIT] Reserve Balance: ${self.reserve_balance:,.2f}")
        else:
            logger.warning("[INIT] No config found for reserve balance!")
            self.reserve_balance = 0  # Set default value instead of None

        # Initialize restored threshold info
        self.restored_threshold_info = []

        # Get exchange info for precision
        exchange_info = await self.client.get_exchange_info()
        for symbol in exchange_info['symbols']:
            self.symbol_info[symbol['symbol']] = {
                'baseAssetPrecision': symbol['baseAssetPrecision'],
                'quotePrecision': symbol['quotePrecision'],
                'filters': {f['filterType']: f for f in symbol['filters']}
            }
            
        # Restore triggered thresholds from database
        if self.mongo_client:
            self.restored_threshold_info = await self.restore_threshold_state()
            
        # Add initial balance check
        await self.check_initial_balance()

    async def restore_threshold_state(self):
        """Restore triggered thresholds from database on startup"""
        try:
            logger.info("Restoring threshold state from database...")
            threshold_states = await self.mongo_client.get_triggered_thresholds()
            
            restored_count = 0
            restored_info = {}  # Change to dictionary to properly track thresholds by symbol/timeframe
            
            # Process each threshold state and update the internal dictionary
            for state in threshold_states:
                symbol = state.get("symbol")
                timeframe_str = state.get("timeframe")
                thresholds = state.get("thresholds", [])
                
                # Skip if any required data is missing
                if not symbol or not timeframe_str:
                    continue
                    
                # Ensure pair is in our tracking dictionary
                if symbol not in self.triggered_thresholds:
                    self.triggered_thresholds[symbol] = {
                        'daily': set(),
                        'weekly': set(),
                        'monthly': set()
                    }
                
                # Convert thresholds to a set of floats and update internal state
                threshold_set = set(float(t) for t in thresholds)
                self.triggered_thresholds[symbol][timeframe_str] = threshold_set
                
                # Track restored thresholds in a structured way for notifications
                if symbol not in restored_info:
                    restored_info[symbol] = {}
                    
                if threshold_set:  # Only include timeframes with thresholds
                    timeframe_enum = TimeFrame(timeframe_str)
                    restored_info[symbol][timeframe_enum] = threshold_set
                    restored_count += len(threshold_set)
                    logger.info(f"Restored {len(threshold_set)} thresholds for {symbol} {timeframe_str}")
            
            logger.info(f"Restored {restored_count} thresholds across {len(restored_info)} symbols")
            self.restored_threshold_info = restored_info
            return restored_info
            
        except Exception as e:
            logger.error(f"Failed to restore threshold state: {e}")
            self.restored_threshold_info = {}
            return {}

    async def close(self):
        if self.client:
            await self.client.close_connection()
            
    async def check_timeframe_reset(self, timeframe: TimeFrame) -> bool:
        """Check if timeframe needs to be reset and return True if reset occurred"""
        now = datetime.utcnow()
        interval = TIMEFRAME_INTERVALS[timeframe.value.upper()]
        
        if now - self.last_reset[timeframe] >= interval:
            logger.info(f"Resetting {timeframe.value} thresholds")
            
            # Prepare reset data
            reset_data = {
                "timeframe": timeframe,
                "prices": []
            }
            
            # Get opening prices for all symbols
            for symbol in self.reference_prices.keys():
                ticker = await self.client.get_symbol_ticker(symbol=symbol)
                current_price = float(ticker['price'])
                ref_price = await self.get_reference_price(symbol, timeframe)
                
                price_data = {
                    "symbol": symbol,
                    "current_price": current_price,
                    "reference_price": ref_price,
                    "price_change": ((current_price - ref_price) / ref_price * 100) if ref_price else 0
                }
                reset_data["prices"].append(price_data)
                
                self.reference_prices[symbol][timeframe] = ref_price or current_price
                
                # Clear triggered thresholds
                if symbol in self.triggered_thresholds:
                    self.triggered_thresholds[symbol][timeframe] = set()
                    
                    # Clear in database as well
                    if self.telegram_bot and self.telegram_bot.mongo_client:
                        await self.telegram_bot.mongo_client.save_triggered_threshold(
                            symbol, timeframe.value, []
                        )
                    
            self.last_reset[timeframe] = now
            
            # Send notification via telegram bot if available
            if self.telegram_bot:
                await self.telegram_bot.send_timeframe_reset_notification(reset_data)
            
            return True
            
        return False

    async def get_reference_timestamp(self, timeframe: TimeFrame) -> int:
        """Get the reference timestamp for a timeframe"""
        now = datetime.utcnow()
        
        if timeframe == TimeFrame.DAILY:
            # Get previous day's midnight UTC
            reference = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if now.hour == 0 and now.minute < 1:  # Within first minute of new day
                reference -= timedelta(days=1)
        
        elif timeframe == TimeFrame.WEEKLY:
            # Get last Monday midnight UTC
            days_since_monday = now.weekday()
            reference = now.replace(hour=0, minute=0, second=0, microsecond=0)
            reference -= timedelta(days=days_since_monday)
            if now.weekday() == 0 and now.hour == 0 and now.minute < 1:
                reference -= timedelta(days=7)
        
        elif timeframe == TimeFrame.MONTHLY:
            # Get 1st of current month midnight UTC
            reference = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if now.day == 1 and now.hour == 0 and now.minute < 1:
                # If within first minute of new month, use last month
                if now.month == 1:
                    reference = reference.replace(year=now.year-1, month=12)
                else:
                    reference = reference.replace(month=now.month-1)
        
        return int(reference.timestamp() * 1000)  # Convert to milliseconds

    async def get_reference_price(self, symbol: str, timeframe: TimeFrame) -> float:
        """Get reference price for symbol at timeframe"""
        try:
            # Use the current candle's open price instead of historical data
            interval_map = {
                TimeFrame.DAILY: '1d',    # Daily candle
                TimeFrame.WEEKLY: '1w',   # Weekly candle
                TimeFrame.MONTHLY: '1M'   # Monthly candle
            }
            
            interval = interval_map[timeframe]
            
            await self.rate_limiter.acquire()
            
            # Get current candle
            klines = await self.client.get_klines(
                symbol=symbol,
                interval=interval,
                limit=1  # Just get the current candle
            )
            
            if klines and len(klines) > 0:
                ref_price = float(klines[0][1])  # Current candle's open price
                logger.info(f"    {timeframe.value} reference: ${ref_price:,.2f}")
                return ref_price
            else:
                logger.warning(f"No kline data for {symbol} {timeframe.value}")
                return None
                
        except Exception as e:
            logger.error(f"Failed to get reference price for {symbol} {timeframe.value}: {e}", exc_info=True)
            return None

    async def update_reference_prices(self, symbols: List[str]):
        """Update reference prices for all timeframes"""
        try:
            for symbol in symbols:
                if (symbol not in self.reference_prices):
                    self.reference_prices[symbol] = {}
                    self.triggered_thresholds[symbol] = {tf: [] for tf in TimeFrame}

                # Get current price first
                await self.rate_limiter.acquire()
                ticker = await self.client.get_symbol_ticker(symbol=symbol)
                current_price = float(ticker['price'])

                # Print symbol header and current price together
                logger.info(f"\n=== Checking {symbol} ===")
                logger.info(f"Current price for {symbol}: ${current_price:,.2f}")

                # Process each timeframe
                for timeframe in TimeFrame:
                    logger.info(f"  ▶ Getting {timeframe.value} reference price")
                    ref_price = await self.get_reference_price(symbol, timeframe)
                    
                    if ref_price is not None:
                        self.reference_prices[symbol][timeframe] = ref_price
                    else:
                        logger.warning(f"    Using current price as {timeframe.value} reference")
                        self.reference_prices[symbol][timeframe] = current_price

                # Add small delay between symbols
                await asyncio.sleep(0.1)

        except Exception as e:
            logger.error(f"Failed to update prices: {e}", exc_info=True)
            raise
            
    async def check_thresholds(self, symbol: str, timeframe: TimeFrame) -> List[float]:
        """Check if any thresholds are triggered for the given symbol and timeframe"""
        try:
            # Skip if trading is paused
            if hasattr(self, 'telegram_bot') and self.telegram_bot.is_paused:
                return []

            # Get current price and reference price
            current_price = await self.get_current_price(symbol)
            reference_price = await self.get_reference_price(symbol, timeframe)
            
            if not reference_price:
                logger.warning(f"No reference price for {symbol} on {timeframe.value}")
                return []
                
            # Calculate absolute price change percentage
            price_change = abs(((current_price - reference_price) / reference_price) * 100)
            
            # Debug log for price change
            logger.debug(f"{symbol} {timeframe.value} price change: {price_change:.2f}%")
            
            # Get thresholds for the timeframe from config
            timeframe_thresholds = self.config['trading']['thresholds'][timeframe.value]
            
            # Get already triggered thresholds for this symbol and timeframe
            # Ensure proper access to the triggered thresholds data structure
            triggered = set()
            if symbol in self.triggered_thresholds and timeframe.value in self.triggered_thresholds[symbol]:
                triggered = self.triggered_thresholds[symbol][timeframe.value]
            
            # Check which thresholds are triggered but not yet processed
            newly_triggered = []
            for threshold in timeframe_thresholds:
                if price_change >= threshold and threshold not in triggered:
                    logger.info(f"Threshold triggered for {symbol}: {threshold}% on {timeframe.value}")
                    newly_triggered.append(threshold)
                    
                    # Mark threshold as triggered
                    self.mark_threshold_triggered(symbol, timeframe, threshold)
                    
                    # Send notification if telegram bot is available
                    if hasattr(self, 'telegram_bot') and self.telegram_bot:
                        await self.telegram_bot.send_threshold_notification(
                            symbol, timeframe, threshold, 
                            current_price, reference_price, price_change
                        )
                    
            return newly_triggered
            
        except Exception as e:
            logger.error(f"Error checking thresholds for {symbol} on {timeframe.value}: {e}")
            return []

    def mark_threshold_triggered(self, symbol: str, timeframe: TimeFrame, threshold: float):
        """Mark a threshold as triggered in memory and persist to database"""
        try:
            # Initialize if needed
            if symbol not in self.triggered_thresholds:
                self.triggered_thresholds[symbol] = {}
            if timeframe not in self.triggered_thresholds[symbol]:
                self.triggered_thresholds[symbol][timeframe] = []
                
            # Add threshold if not already in list
            if threshold not in self.triggered_thresholds[symbol][timeframe]:
                self.triggered_thresholds[symbol][timeframe].append(threshold)
                logger.info(f"Marking threshold {threshold}% as triggered for {symbol} {timeframe.value}")
                
            # Persist to database in background
            asyncio.create_task(self.mongo_client.save_triggered_threshold(
                symbol, timeframe, threshold
            ))
        except Exception as e:
            logger.error(f"Error marking threshold triggered: {e}")
            
    async def reset_timeframe_thresholds(self, timeframe_str: str):
        """Reset all thresholds for a specific timeframe"""
        try:
            # Convert string to TimeFrame enum if it's not already
            if isinstance(timeframe_str, str):
                timeframe = TimeFrame(timeframe_str)
            else:
                timeframe = timeframe_str
            
            logger.info(f"Resetting all thresholds for {timeframe.value} timeframe...")
            
            # Get reference prices for all symbols
            reset_data = {
                "timeframe": timeframe,
                "prices": []
            }
            
            # Track which symbols were reset for logging
            reset_symbols = []
            
            for symbol in self.config['trading']['pairs']:
                # Get current price
                current_price = await self.get_current_price(symbol)
                
                # Store current price as new reference
                if symbol not in self.reference_prices:
                    self.reference_prices[symbol] = {}
                
                # Update reference price to current price
                old_reference = self.reference_prices.get(symbol, {}).get(timeframe)
                self.reference_prices[symbol][timeframe] = current_price
                
                # Calculate price change from previous reference (if exists)
                price_change = 0
                if old_reference:
                    price_change = ((current_price - old_reference) / old_reference) * 100
                
                # Add to reset data for notification
                reset_data["prices"].append({
                    "symbol": symbol,
                    "current_price": current_price,
                    "reference_price": current_price,  # New reference is current price
                    "previous_reference": old_reference,
                    "price_change": price_change
                })
                
                # Check if symbol had triggered thresholds to reset
                if symbol in self.triggered_thresholds and timeframe.value in self.triggered_thresholds[symbol]:
                    # Only log if there were thresholds to clear
                    if self.triggered_thresholds[symbol][timeframe.value]:
                        reset_symbols.append(symbol)
                        logger.info(f"Cleared thresholds for {symbol} {timeframe.value}: {list(self.triggered_thresholds[symbol][timeframe.value])}")
                    
                    # Clear triggered thresholds for this symbol and timeframe
                    self.triggered_thresholds[symbol][timeframe.value] = set()
            
            # Persist changes to database
            await self.mongo_client.save_reference_prices(self.reference_prices)
            await self.mongo_client.clear_triggered_thresholds(timeframe)
            
            # Send notification if telegram bot is available
            if reset_data["prices"] and hasattr(self, 'telegram_bot') and self.telegram_bot:
                await self.telegram_bot.send_timeframe_reset_notification(reset_data)
                
            if reset_symbols:
                logger.info(f"Reset thresholds for {len(reset_symbols)} symbols: {', '.join(reset_symbols)}")
            else:
                logger.info(f"No triggered thresholds were found to reset for {timeframe.value} timeframe")
                
            return True
            
        except Exception as e:
            logger.error(f"Error resetting {timeframe_str} thresholds: {e}", exc_info=True)
            return False

    async def restore_triggered_thresholds(self):
        """Restore triggered thresholds from database on startup"""
        try:
            # Get thresholds from database
            stored_thresholds = await self.mongo_client.get_all_triggered_thresholds()
            
            # Initialize dictionary for notification data
            restored_info = {}
            
            # Process each stored threshold
            for threshold_data in stored_thresholds:
                symbol = threshold_data['symbol']
                timeframe = TimeFrame(threshold_data['timeframe'])
                threshold = threshold_data['threshold']
                
                # Initialize nested dictionaries if needed
                if symbol not in self.triggered_thresholds:
                    self.triggered_thresholds[symbol] = {}
                if timeframe not in self.triggered_thresholds[symbol]:
                    self.triggered_thresholds[symbol][timeframe] = []
                    
                # Add threshold to in-memory storage
                if threshold not in self.triggered_thresholds[symbol][timeframe]:
                    self.triggered_thresholds[symbol][timeframe].append(threshold)
                    
                # Add to notification data
                if symbol not in restored_info:
                    restored_info[symbol] = {}
                if timeframe not in restored_info[symbol]:
                    restored_info[symbol][timeframe] = []
                restored_info[symbol][timeframe].append(threshold)
                
            # Store restored threshold info for notification
            self.restored_threshold_info = restored_info
            
            # Log restoration results
            total_count = sum(len(thresholds) for symbol_data in self.triggered_thresholds.values() 
                            for thresholds in symbol_data.values())
            logger.info(f"Restored {total_count} triggered thresholds from database")
            
            return self.triggered_thresholds
            
        except Exception as e:
            logger.error(f"Error restoring triggered thresholds: {e}")
            return {}

    async def check_reserve_balance(self, order_amount: float) -> bool:
        """Check if placing an order would violate reserve balance"""
        try:
            logger.info("[RESERVE CHECK] Starting reserve balance check...")
            
            if self.reserve_balance is None or self.reserve_balance <= 0:
                logger.info(f"[RESERVE CHECK] No valid reserve balance set (value: {self.reserve_balance})")
                return True

            # Get current balance in base currency (USDT)
            current_balance = await self.get_balance(self.base_currency)
            logger.info(f"[RESERVE CHECK] Current balance: ${float(current_balance):,.2f}")
            logger.info(f"[RESERVE CHECK] Reserve balance: ${float(self.reserve_balance):,.2f}")
            
            # Get sum of pending orders
            pending_orders_value = Decimal('0')
            if self.telegram_bot and self.telegram_bot.mongo_client:
                cursor = self.telegram_bot.mongo_client.orders.find({"status": "pending"})
                async for order in cursor:
                    pending_orders_value += (Decimal(str(order['price'])) * Decimal(str(order['quantity'])))

            # Calculate remaining balance after pending orders
            available_balance = float(current_balance - pending_orders_value)
            remaining_after_order = available_balance - order_amount

            logger.info(f"[RESERVE CHECK] Available after pending: ${available_balance:,.2f}")
            logger.info(f"[RESERVE CHECK] Remaining after order: ${remaining_after_order:,.2f}")
            logger.info(f"[RESERVE CHECK] Required reserve: ${float(self.reserve_balance):,.2f}")

            # Check if remaining balance would be above reserve
            return remaining_after_order >= self.reserve_balance

        except Exception as e:
            logger.error(f"[RESERVE CHECK] Error checking reserve balance: {e}")
            return False

    async def place_limit_buy_order(self, symbol: str, amount: float, 
                                  threshold: Optional[float] = None,
                                  timeframe: Optional[TimeFrame] = None,
                                  is_manual: bool = False) -> Order:
        """Place a limit buy order with reserve balance check"""
        # Check reserve balance first
        if not is_manual and not await self.check_reserve_balance(amount):
            raise ValueError("Order would violate reserve balance")

        await self.rate_limiter.acquire()
        
        try:
            # Get current price
            ticker = await self.client.get_symbol_ticker(symbol=symbol)
            price = Decimal(ticker['price'])
            
            # Calculate quantity based on USDT amount
            quantity = Decimal(str(amount)) / price
            
            # Get and apply precision
            quantity_precision = self._get_quantity_precision(symbol)
            price_precision = self._get_price_precision(symbol)
            
            # Round quantity to precision and adjust for lot size
            quantity = Decimal(str(round(quantity, quantity_precision)))
            quantity = self._adjust_quantity_to_lot_size(symbol, quantity)
            price = Decimal(str(round(price, price_precision)))
            
            # Log order details before placement
            logger.info(f"Placing order: {symbol} quantity={quantity} price=${price}")
            
            # Calculate fees
            fees, fee_asset = await self.calculate_fees(symbol, price, quantity)
            
            # Check minimum notional
            min_notional = MIN_NOTIONAL.get(symbol, MIN_NOTIONAL['DEFAULT'])
            if price * quantity < Decimal(str(min_notional)):
                raise ValueError(f"Order value below minimum notional: {min_notional} USDT")
            
            # Only update triggered thresholds if it's not a manual trade
            if not is_manual and threshold and symbol in self.triggered_thresholds and timeframe:
                self.mark_threshold_triggered(symbol, timeframe.value, threshold)
            
            # Generate unique order ID
            order_id = str(int(datetime.utcnow().timestamp() * 1000))
            
            if not is_manual:
                # Place order on Binance
                order_response = await self.client.create_order(
                    symbol=symbol,
                    side='BUY',
                    type='LIMIT',
                    timeInForce='GTC',
                    quantity=float(quantity),
                    price=float(price)
                )
                order_id = str(order_response['orderId'])
            
            # Create order object with all required fields
            order = Order(
                symbol=symbol,
                status=OrderStatus.PENDING if not is_manual else OrderStatus.FILLED,
                order_type=OrderType.SPOT,
                price=price,
                quantity=quantity,
                timeframe=timeframe or TimeFrame.DAILY,
                order_id=order_id,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                filled_at=datetime.utcnow() if is_manual else None,
                fees=fees,
                fee_asset=fee_asset,
                threshold=threshold  # This is now handled by the Order class
            )
            
            return order
            
        except BinanceAPIException as e:
            logger.error(f"Failed to place order: {e}")
            raise
        except Exception as e:
            logger.error(f"Error placing order: {str(e)}")
            raise

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an order"""
        try:
            await self.client.cancel_order(symbol=symbol, orderId=order_id)
            return True
        except BinanceAPIException as e:
            logger.error(f"Failed to cancel order: {e}")
            return False
            
    async def check_order_status(self, symbol: str, order_id: str) -> Optional[OrderStatus]:
        """Check the status of an order"""
        try:
            order = await self.client.get_order(symbol=symbol, orderId=order_id)
            if order['status'] == 'FILLED':
                return OrderStatus.FILLED
            elif order['status'] == 'CANCELED':
                return OrderStatus.CANCELLED
            return OrderStatus.PENDING
        except BinanceAPIException as e:
            logger.error(f"Failed to check order status: {e}")
            return None
            
    async def get_balance(self, symbol: str = 'USDT') -> Decimal:
        """Get balance for a specific asset"""
        await self.rate_limiter.acquire()
        try:
            account = await self.client.get_account()
            for balance in account['balances']:
                if (balance['asset'] == symbol):
                    return Decimal(balance['free'])
            return Decimal('0')
        except BinanceAPIException as e:
            logger.error(f"Failed to get balance: {e}")
            raise
            
    async def get_balance_changes(self, symbol: str = 'USDT') -> Optional[Decimal]:
        """Get balance changes since last check"""
        current_balance = await self.get_balance(symbol)
        previous_balance = self.balance_cache.get(symbol)
        self.balance_cache[symbol] = current_balance
        
        if previous_balance is not None:
            return current_balance - previous_balance
        return None

    async def get_candles_for_chart(self, symbol: str, timeframe: TimeFrame, count: int = 15) -> List[Dict]:
        """Get historical candles for chart generation with proper alignment"""
        try:
            # Get reference timestamp first
            ref_timestamp = await self.get_reference_timestamp(timeframe)
            
            # Map timeframes to intervals and milliseconds
            interval_map = {
                TimeFrame.DAILY: ('1d', 24 * 60 * 60 * 1000),
                TimeFrame.WEEKLY: ('1w', 7 * 24 * 60 * 60 * 1000),
                TimeFrame.MONTHLY: ('1M', 30 * 24 * 60 * 60 * 1000)
            }
            
            interval, ms_per_candle = interval_map[timeframe]
            
            # Calculate start and end times
            end_time = ref_timestamp + ms_per_candle  # Include the reference candle
            start_time = end_time - (count * ms_per_candle)
            
            # Get candles with specific time range
            await self.rate_limiter.acquire()
            klines = await self.client.get_klines(
                symbol=symbol,
                interval=interval,
                startTime=start_time,
                endTime=end_time,
                limit=count + 2  # Get extra candles to ensure coverage
            )
            
            if not klines:
                logger.error(f"No candles returned for {symbol} {timeframe.value}")
                return []
            
            # Process and validate candles
            candles = []
            for k in klines:
                candle_time = k[0]
                if start_time <= candle_time <= end_time:
                    candles.append({
                        'timestamp': candle_time,
                        'open': float(k[1]),
                        'high': float(k[2]),
                        'low': float(k[3]),
                        'close': float(k[4]),
                        'volume': float(k[5])
                    })
            
            # Ensure we have the right number of candles
            candles = candles[-count:] if len(candles) > count else candles
            
            # Log candle alignment info
            logger.info(f"Got {len(candles)} candles for {symbol} {timeframe.value}")
            logger.info(f"Time range: {datetime.fromtimestamp(start_time/1000)} to {datetime.fromtimestamp(end_time/1000)}")
            
            return candles
            
        except Exception as e:
            logger.error(f"Failed to get candles for chart: {e}")
            return []

    async def generate_trade_chart(self, order: Order) -> Optional[bytes]:
        """Generate chart for a trade"""
        try:
            candles = await self.get_candles_for_chart(
                order.symbol,
                order.timeframe
            )
            
            if not candles:
                return None
                
            ref_price = self.reference_prices.get(order.symbol, {}).get(order.timeframe)
            
            return await self.chart_generator.generate_trade_chart(
                candles,
                order,
                Decimal(str(ref_price)) if ref_price else None
            )
            
        except Exception as e:
            logger.error(f"Failed to generate trade chart: {e}")
            return None

    async def get_historical_prices(self, symbol: str, days: int = 30) -> List[Dict]:
        """Get historical daily prices for a symbol"""
        try:
            # Calculate start time
            end_time = int(datetime.utcnow().timestamp() * 1000)
            start_time = int((datetime.utcnow() - timedelta(days=days)).timestamp() * 1000)
            
            await self.rate_limiter.acquire(weight=10)  # Higher weight for klines request
            
            # Get klines (daily candles)
            klines = await self.client.get_klines(
                symbol=symbol,
                interval='1d',
                startTime=start_time,
                endTime=end_time,
                limit=1000  # Maximum allowed
            )
            
            # Process the klines data
            results = []
            for k in klines:
                timestamp = datetime.fromtimestamp(k[0] / 1000)  # Convert ms to datetime
                results.append({
                    'timestamp': timestamp,
                    'price': Decimal(str(k[4])),  # Use closing price
                    'open': Decimal(str(k[1])),
                    'high': Decimal(str(k[2])),
                    'low': Decimal(str(k[3])),
                    'volume': Decimal(str(k[5]))
                })
                
            logger.info(f"Retrieved {len(results)} historical prices for {symbol}")
            return results
            
        except Exception as e:
            logger.error(f"Failed to get historical prices for {symbol}: {e}")
            return []

    def mark_threshold_triggered(self, pair: str, timeframe: str, threshold: float):
        """Mark a threshold as triggered and persist to database"""
        # Ensure symbol exists in dictionary
        if pair not in self.triggered_thresholds:
            self.triggered_thresholds[pair] = {
                'daily': set(),
                'weekly': set(),
                'monthly': set()
            }
            
        # Ensure timeframe exists in the symbol's dictionary
        if timeframe not in self.triggered_thresholds[pair]:
            self.triggered_thresholds[pair][timeframe] = set()
        
        # Skip if already triggered
        if threshold in self.triggered_thresholds[pair][timeframe]:
            logger.debug(f"Threshold {threshold}% already triggered for {pair} {timeframe}")
            return False
        
        # Mark as triggered
        self.triggered_thresholds[pair][timeframe].add(threshold)
        
        # Save to database - create task but wait for completion to ensure it's saved
        try:
            logger.info(f"Marking threshold {threshold}% as triggered for {pair} {timeframe}")
            if self.mongo_client:
                asyncio.create_task(
                    self.mongo_client.save_triggered_threshold(
                        pair, 
                        timeframe, 
                        list(self.triggered_thresholds[pair][timeframe])
                    )
                )
            return True
        except Exception as e:
            logger.error(f"Failed to persist threshold {threshold}% for {pair} {timeframe}: {e}")
            return False

    async def reset_timeframe_thresholds(self, timeframe: str):
        """Reset thresholds for a specific timeframe and persist to database"""
        try:
            # Reset in memory
            for symbol in self.triggered_thresholds:
                # Ensure timeframe exists in the symbol's dictionary
                if timeframe not in self.triggered_thresholds[symbol]:
                    self.triggered_thresholds[symbol][timeframe] = set()
                else:
                    self.triggered_thresholds[symbol][timeframe] = set()
            
            # Reset in database
            if self.mongo_client:
                await self.mongo_client.reset_timeframe_thresholds(timeframe)
            
            logger.info(f"Reset all thresholds for timeframe: {timeframe}")
            return True
        except Exception as e:
            logger.error(f"Failed to reset thresholds for {timeframe}: {e}")
            return False

    async def get_current_price(self, symbol: str) -> float:
        """Get the current price for a symbol"""
        try:
            await self.rate_limiter.acquire()
            ticker = await self.client.get_symbol_ticker(symbol=symbol)
            return float(ticker['price'])
        except Exception as e:
            logger.error(f"Failed to get current price for {symbol}: {e}")
            return None

    def _get_quantity_precision(self, symbol: str) -> int:
        """Get the quantity precision for a symbol"""
        try:
            if symbol in self.symbol_info:
                return self.symbol_info[symbol]['baseAssetPrecision']
            return 8  # Default precision if symbol info not available
        except Exception as e:
            logger.error(f"Error getting quantity precision for {symbol}: {e}")
            return 8  # Default safe value

    def _get_price_precision(self, symbol: str) -> int:
        """Get the price precision for a symbol"""
        try:
            if symbol in self.symbol_info:
                return self.symbol_info[symbol]['quotePrecision']
            return 8  # Default precision if symbol info not available
        except Exception as e:
            logger.error(f"Error getting price precision for {symbol}: {e}")
            return 8  # Default safe value

    def _adjust_quantity_to_lot_size(self, symbol: str, quantity: Decimal) -> Decimal:
        """Adjust quantity to valid lot size"""
        try:
            if symbol not in self.symbol_info:
                return quantity  # Return unchanged if symbol info not available
                
            filters = self.symbol_info[symbol]['filters']
            if 'LOT_SIZE' in filters:
                lot_filter = filters['LOT_SIZE']
                min_qty = Decimal(str(lot_filter['minQty']))
                max_qty = Decimal(str(lot_filter['maxQty']))
                step_size = Decimal(str(lot_filter['stepSize']))
                
                # Adjust to step size
                if step_size != Decimal('0'):
                    decimal_places = abs(step_size.as_tuple().exponent)
                    quantity = (quantity // step_size) * step_size
                    quantity = quantity.quantize(Decimal('0.' + '0' * decimal_places))
                    
                # Ensure within limits
                quantity = max(min_qty, min(max_qty, quantity))
                
            return quantity
        except Exception as e:
            logger.error(f"Error adjusting quantity for {symbol}: {e}")
            return quantity

    async def calculate_fees(self, symbol: str, price: Decimal, quantity: Decimal) -> Tuple[Decimal, str]:
        """Calculate trading fees for an order"""
        try:
            # Get fee rate for the symbol or use default
            fee_rate = Decimal(str(TRADING_FEES.get(symbol, TRADING_FEES['DEFAULT'])))
            
            # Calculate fee amount
            fee_amount = price * quantity * fee_rate
            
            # Default fee asset is USDT for spot trades
            fee_asset = "USDT"
            
            return fee_amount, fee_asset
        except Exception as e:
            logger.error(f"Error calculating fees: {e}")
            return Decimal('0'), "USDT"  # Default safe values

    async def get_historical_benchmark(self, symbol: str, days: int = 90) -> Dict:
        """Get historical performance data for a benchmark asset"""
        try:
            # For cryptocurrency benchmarks like BTC
            if symbol in ["BTCUSDT", "ETHUSDT"]:
                # Get historical prices
                prices = await self.get_historical_prices(symbol, days)
                if not prices or len(prices) < 2:
                    logger.warning(f"Not enough historical data for {symbol}")
                    return {}
                    
                # Calculate daily ROI percentages relative to first day
                base_price = float(prices[0]['price'])
                result = {}
                
                for price_data in prices:
                    date = price_data['timestamp'].strftime('%Y-%m-%d')
                    current_price = float(price_data['price'])
                    roi = ((current_price - base_price) / base_price) * 100
                    result[date] = roi
                    
                return result
                
            # For S&P 500 data using Yahoo scraper
            elif symbol == "SP500":
                try:
                    # Use Yahoo Finance scraper to get S&P 500 historical data
                    return await self.yahoo_scraper.get_sp500_data(days)
                except Exception as e:
                    logger.error(f"Error getting Yahoo S&P 500 data: {e}", exc_info=True)
                    logger.info("Falling back to simulated S&P 500 data")
                    return await self._get_simulated_sp500_data(days)
            else:
                logger.warning(f"Unsupported benchmark symbol: {symbol}")
                return {}
                
        except Exception as e:
            logger.error(f"Error getting historical benchmark data for {symbol}: {e}", exc_info=True)
            return {}
            
    async def _get_simulated_sp500_data(self, days: int = 90) -> Dict:
        """Generate simulated S&P 500 data when API is unavailable"""
        import numpy as np
        
        logger.info("Generating simulated S&P 500 data")
        today = datetime.utcnow()
        base_value = 4000.0  # Starting value
        daily_change = 0.05  # Average daily change percentage
        result = {}
        
        # Generate simulated S&P 500 performance data
        for day in range(days, -1, -1):
            date = (today - timedelta(days=day)).strftime('%Y-%m-%d')
            # Simulate some realistic movement with noise and slight upward trend
            random_factor = np.random.normal(0, 1) * daily_change
            base_value *= (1 + random_factor / 100)
            result[date] = ((base_value - 4000.0) / 4000.0) * 100
                
        logger.info(f"Generated {len(result)} days of simulated S&P 500 data")
        return result

    # ...rest of existing code...
