from binance.client import AsyncClient
from binance.exceptions import BinanceAPIException
from decimal import Decimal
from datetime import datetime, timedelta
import asyncio
import logging
from typing import Dict, List, Optional, Tuple
from ..types.models import Order, OrderStatus, TimeFrame, OrderType  # Add OrderType
from ..utils.rate_limiter import RateLimiter
from ..types.constants import PRECISION, MIN_NOTIONAL, TIMEFRAME_INTERVALS, TRADING_FEES
from ..utils.chart_generator import ChartGenerator

logger = logging.getLogger(__name__)

class BinanceClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True,
                 base_currency: str = None, reserve_balance: float = None, config: dict = None):  # Add config parameter
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.client: Optional[AsyncClient] = None
        self.reference_prices: Dict[str, Dict[TimeFrame, float]] = {}
        self.triggered_thresholds: Dict[str, Dict[TimeFrame, List[float]]] = {}
        self.rate_limiter = RateLimiter()
        self.symbol_info = {}
        self.last_reset: Dict[TimeFrame, datetime] = {
            tf: datetime.utcnow() for tf in TimeFrame
        }
        self.balance_cache = {}
        self.reference_timestamps = {
            TimeFrame.DAILY: None,
            TimeFrame.WEEKLY: None,
            TimeFrame.MONTHLY: None
        }
        logger.setLevel(logging.DEBUG)
        self.telegram_bot = None
        self.chart_generator = ChartGenerator()
        
        # Initialize with provided values - improved reserve balance handling
        self.base_currency = base_currency
        self.reserve_balance = None
        
        # Add MongoDB client reference for threshold tracking
        self.mongo_client = None
        self.config = config  # Store config directly
        
        # Set configuration values from passed config with better reserve balance handling
        if config:
            self.base_currency = config['trading'].get('base_currency', 'USDT')
            self.reserve_balance = float(config['trading'].get('reserve_balance', 0))
            logger.info(f"[INIT] Loaded from config:")
            logger.info(f"[INIT] Base Currency: {self.base_currency}")
            logger.info(f"[INIT] Reserve Balance: ${self.reserve_balance:,.2f}")
        else:
            # Use passed values or defaults
            self.base_currency = base_currency or 'USDT'
            self.reserve_balance = float(reserve_balance) if reserve_balance is not None else 0
            logger.info(f"[INIT] Using direct parameters:")
            logger.info(f"[INIT] Base Currency: {self.base_currency}")
            logger.info(f"[INIT] Reserve Balance: ${self.reserve_balance:,.2f}")

    def set_telegram_bot(self, bot):
        """Set telegram bot for notifications"""
        self.telegram_bot = bot

    def set_mongo_client(self, mongo_client):
        """Set MongoDB client for threshold tracking"""
        self.mongo_client = mongo_client
        
    async def check_initial_balance(self) -> bool:
        """Check if current balance is above reserve requirement"""
        try:
            if self.reserve_balance is None:
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
                f"Current Balance: ${float(current_balance):,.2f}\n"
                f"Reserve Balance: ${self.reserve_balance:.2f}"
            )
            return True

        except Exception as e:
            logger.error(f"Error checking initial balance: {e}")
            return False

    async def initialize(self):
        """Initialize client with proper error handling for configuration"""
        self.client = await AsyncClient.create(
            api_key=self.api_key,
            api_secret=self.api_secret,
            testnet=self.testnet
        )

        # Get exchange info for precision
        exchange_info = await self.client.get_exchange_info()
        for symbol in exchange_info['symbols']:
            self.symbol_info[symbol['symbol']] = {
                'baseAssetPrecision': symbol['baseAssetPrecision'],
                'quotePrecision': symbol['quotePrecision'],
                'filters': {f['filterType']: f for f in symbol['filters']}
            }
            
        # Check initial balance after initialization
        await self.check_initial_balance()
        
    async def close(self):
        if self.client:
            await self.client.close_connection()
            
    async def check_timeframe_reset(self, timeframe: TimeFrame) -> bool:
        """Check if timeframe needs reset with proper UTC handling"""
        now = datetime.utcnow()
        last_reset = self.last_reset.get(timeframe)
        
        if not last_reset:
            self.last_reset[timeframe] = now
            return True
            
        # Get current time components
        current_hour = now.hour
        current_minute = now.minute
        current_weekday = now.weekday()  # Monday is 0
        current_day = now.day
        
        # Check reset conditions based on timeframe
        reset_needed = False
        
        if timeframe == TimeFrame.DAILY:
            # Reset at UTC 00:00
            if current_hour == 0 and current_minute == 0:
                if (now - last_reset).total_seconds() >= 60:
                    reset_needed = True
                    
        elif timeframe == TimeFrame.WEEKLY:
            # Reset Monday at UTC 00:00
            if current_weekday == 0 and current_hour == 0 and current_minute == 0:
                if (now - last_reset).total_seconds() >= 60:
                    reset_needed = True
                    
        elif timeframe == TimeFrame.MONTHLY:
            # Reset 1st of month at UTC 00:00
            if current_day == 1 and current_hour == 0 and current_minute == 0:
                if (now - last_reset).total_seconds() >= 60:
                    reset_needed = True
        
        if reset_needed:
            logger.info(f"Resetting {timeframe.value} thresholds at {now}")
            self.last_reset[timeframe] = now
            
            # Clear triggered thresholds for all symbols for this timeframe
            for symbol in list(self.triggered_thresholds.keys()):
                if timeframe in self.triggered_thresholds[symbol]:
                    self.triggered_thresholds[symbol][timeframe] = []
                
            # Send reset notification with price info
            if self.telegram_bot:
                prices_info = []
                async for symbol in self.reference_prices:
                    current = await self.get_current_price(symbol)
                    ref = self.reference_prices.get(symbol, {}).get(timeframe)
                    if current and ref:
                        change = ((current - ref) / ref) * 100
                        prices_info.append({
                            "symbol": symbol,
                            "current_price": current,
                            "reference_price": ref,
                            "price_change": change
                        })
                
                await self.telegram_bot.send_timeframe_reset_notification({
                    "timeframe": timeframe,
                    "prices": prices_info
                })
            
            return True
            
        return False

    async def get_current_price(self, symbol: str) -> float:
        """Get current price with error handling"""
        try:
            ticker = await self.client.get_symbol_ticker(symbol=symbol)
            return float(ticker['price'])
        except Exception as e:
            logger.error(f"Error getting current price for {symbol}: {e}")
            return None

    async def get_reference_timestamp(self, timeframe: TimeFrame) -> int:
        """Get the reference timestamp for a timeframe"""
        now = datetime.utcnow()
        
        if timeframe == TimeFrame.DAILY:
            # Get previous day's midnight UTC
            reference = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if now.hour == 0 and now.minute < 1:  # Within first minute of new day
                reference -= timedelta(days=1)
        
        elif timeframe == TimeFrame.WEEKLY:
            # Get previous week's Monday at midnight UTC
            days_since_monday = now.weekday()
            reference = now.replace(hour=0, minute=0, second=0, microsecond=0)
            # Subtract days to get to Monday, then subtract an additional week
            reference -= timedelta(days=days_since_monday + 7)
        
        elif timeframe == TimeFrame.MONTHLY:
            # Fix: Get previous month's 1st day midnight UTC
            if now.month == 1:
                # If January, go to December of previous year
                reference = now.replace(year=now.year-1, month=12, day=1,
                                     hour=0, minute=0, second=0, microsecond=0)
            else:
                # Go to 1st of previous month
                reference = now.replace(month=now.month-1, day=1,
                                     hour=0, minute=0, second=0, microsecond=0)
        
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
            
            if (klines and len(klines) > 0):
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
                    
                    if (ref_price is not None):
                        self.reference_prices[symbol][timeframe] = ref_price
                    else:
                        logger.warning(f"    Using current price as {timeframe.value} reference")
                        self.reference_prices[symbol][timeframe] = current_price

                # Add small delay between symbols
                await asyncio.sleep(0.1)

        except Exception as e:
            logger.error(f"Failed to update prices: {e}", exc_info=True)
            raise
            
    async def check_thresholds(self, symbol: str, thresholds: Dict[str, List[float]]) -> Optional[tuple]:
        """Check price against thresholds"""
        try:
            ticker = await self.client.get_symbol_ticker(symbol=symbol)
            current_price = float(ticker['price'])
            
            for timeframe in TimeFrame:
                if timeframe.value not in thresholds:
                    continue
                    
                ref_price = self.reference_prices.get(symbol, {}).get(timeframe)
                if not ref_price:
                    continue
                    
                price_change = ((ref_price - current_price) / ref_price) * 100
                
                # Check against thresholds
                for threshold in sorted(thresholds[timeframe.value]):
                    if (price_change >= threshold and 
                        threshold not in self.triggered_thresholds[symbol][timeframe]):
                        
                        logger.info(f"Threshold triggered for {symbol}: {threshold}% on {timeframe.value}")
                        self.triggered_thresholds[symbol][timeframe].append(threshold)
                        
                        # Send notification
                        if self.telegram_bot:
                            await self.telegram_bot.send_threshold_notification(
                                symbol=symbol,
                                timeframe=timeframe,
                                threshold=threshold,
                                current_price=current_price,
                                reference_price=ref_price,
                                price_change=price_change
                            )
                        
                        return timeframe, threshold
                        
            return None
            
        except Exception as e:
            logger.error(f"Error checking thresholds for {symbol}: {e}")
            return None
            
    def _get_quantity_precision(self, symbol: str) -> int:
        """Get the required decimal precision for quantity"""
        if symbol in self.symbol_info:
            return self.symbol_info[symbol]['baseAssetPrecision']
        return PRECISION['DEFAULT']
        
    def _get_price_precision(self, symbol: str) -> int:
        """Get the required decimal precision for price"""
        if symbol in self.symbol_info:
            return self.symbol_info[symbol]['quotePrecision']
        return PRECISION['DEFAULT']
        
    async def calculate_fees(self, symbol: str, price: Decimal, quantity: Decimal) -> Tuple[Decimal, str]:
        """Calculate fees for an order"""
        if self.testnet:
            # Testnet simulation
            fee_rate = TRADING_FEES['DEFAULT']
            fee_amount = price * quantity * Decimal(str(fee_rate))
            fee_asset = 'USDT'
        else:
            try:
                trade_fee = await self.client.get_trade_fee(symbol=symbol)
                if trade_fee and trade_fee[0]:
                    fee_rate = Decimal(str(trade_fee[0].get('makerCommission', TRADING_FEES['MAKER'])))
                    fee_amount = price * quantity * fee_rate
                    fee_asset = trade_fee[0].get('feeCoin', 'USDT')
                else:
                    fee_rate = Decimal(str(TRADING_FEES['DEFAULT']))
                    fee_amount = price * quantity * fee_rate
                    fee_asset = 'USDT'
            except Exception as e:
                logger.warning(f"Failed to get trading fees for {symbol}, using default: {e}")
                fee_rate = Decimal(str(TRADING_FEES['DEFAULT']))
                fee_amount = price * quantity * fee_rate
                fee_asset = 'USDT'

        return fee_amount.quantize(Decimal('0.00000001')), fee_asset

    def _adjust_quantity_to_lot_size(self, symbol: str, quantity: Decimal) -> Decimal:
        """Adjust quantity to comply with lot size filter"""
        if symbol not in self.symbol_info:
            return quantity

        lot_size_filter = self.symbol_info[symbol]['filters'].get('LOT_SIZE', {})
        if not lot_size_filter:
            return quantity

        min_qty = Decimal(str(lot_size_filter.get('minQty', '0')))
        max_qty = Decimal(str(lot_size_filter.get('maxQty', '999999')))
        step_size = Decimal(str(lot_size_filter.get('stepSize', '0')))

        if step_size == 0:
            return quantity

        # Calculate precision from step size
        step_precision = abs(Decimal(str(step_size)).as_tuple().exponent)
        
        # Round to step size
        adjusted_qty = Decimal(str(float(quantity) - (float(quantity) % float(step_size))))
        adjusted_qty = adjusted_qty.quantize(Decimal('0.' + '0' * step_precision))

        # Ensure quantity is within bounds
        adjusted_qty = max(min_qty, min(adjusted_qty, max_qty))
        
        logger.debug(f"Adjusted quantity from {quantity} to {adjusted_qty} (step size: {step_size})")
        return adjusted_qty

    async def check_reserve_balance(self, order_amount: float) -> bool:
        """Check if placing an order would violate reserve balance"""
        try:
            logger.info("[RESERVE CHECK] Starting reserve balance check...")
            
            # Get current balance in base currency (USDT)
            current_balance = await self.get_balance(self.base_currency)
            logger.info(f"[RESERVE CHECK] Current balance: ${float(current_balance):,.2f}")
            logger.info(f"[RESERVE CHECK] Reserve balance: ${float(self.reserve_balance):,.2f}")
            
            # Get sum of pending orders - only if mongo client is available
            pending_orders_value = Decimal('0')
            if hasattr(self, 'mongo_client') and self.mongo_client:
                try:
                    cursor = self.mongo_client.orders.find({"status": "pending"})
                    async for order in cursor:
                        pending_orders_value += (Decimal(str(order['price'])) * Decimal(str(order['quantity'])))
                except Exception as e:
                    logger.warning(f"[RESERVE CHECK] Could not get pending orders: {e}")

            # Calculate remaining balance after pending orders and new order
            available_balance = float(current_balance - pending_orders_value)
            remaining_after_order = available_balance - order_amount

            logger.info(f"[RESERVE CHECK] Available after pending: ${available_balance:,.2f}")
            logger.info(f"[RESERVE CHECK] Order amount: ${order_amount:,.2f}")
            logger.info(f"[RESERVE CHECK] Remaining after order: ${remaining_after_order:,.2f}")

            # Check if remaining balance would be above reserve
            is_valid = remaining_after_order >= self.reserve_balance

            if not is_valid:
                logger.warning(
                    f"[RESERVE CHECK] Order would violate reserve balance:\n"
                    f"Required Balance: ${self.reserve_balance:,.2f}\n"
                    f"Remaining Balance: ${remaining_after_order:,.2f}"
                )
                
                # Send alert through telegram if available
                if self.telegram_bot:
                    await self.telegram_bot.send_reserve_alert(
                        current_balance=Decimal(str(current_balance)),
                        reserve_balance=Decimal(str(self.reserve_balance)),
                        pending_value=Decimal(str(order_amount))
                    )

            return is_valid

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
                if threshold not in self.triggered_thresholds[symbol][timeframe]:
                    self.triggered_thresholds[symbol][timeframe].append(threshold)
            
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

    async def get_candles_for_chart(self, symbol: str, timeframe: TimeFrame, count: int = 8) -> List[Dict]:
        """Get historical candles with proper timeframe alignment"""
        try:
            # Define interval and period mapping for each timeframe
            interval_map = {
                TimeFrame.DAILY: ('1d', timedelta(days=1)),
                TimeFrame.WEEKLY: ('1w', timedelta(weeks=1)),
                TimeFrame.MONTHLY: ('1M', timedelta(days=30))
            }
            
            interval, period = interval_map[timeframe]
            
            # Calculate proper start and end times based on timeframe
            now = datetime.utcnow()
            end_time = now

            if timeframe == TimeFrame.DAILY:
                # Get exactly 8 days of data
                start_time = end_time - timedelta(days=8)
            
            elif timeframe == TimeFrame.WEEKLY:
                # Get exactly 8 weeks of data, aligned to Monday
                days_since_monday = end_time.weekday()
                end_time = end_time - timedelta(days=days_since_monday)
                start_time = end_time - timedelta(weeks=8)
            
            elif timeframe == TimeFrame.MONTHLY:
                # Get exactly 8 months of data, aligned to 1st of month
                if now.month > 8:
                    start_time = now.replace(month=now.month - 8, day=1)
                else:
                    # Handle year boundary
                    months_in_prev_year = 8 - now.month
                    start_time = now.replace(year=now.year - 1,
                                          month=12 - months_in_prev_year + 1,
                                          day=1)
                end_time = now.replace(day=1)

            # Convert to milliseconds for Binance API
            start_ms = int(start_time.timestamp() * 1000)
            end_ms = int(end_time.timestamp() * 1000)
            
            # Get candles from Binance
            await self.rate_limiter.acquire()
            klines = await self.client.get_klines(
                symbol=symbol,
                interval=interval,
                startTime=start_ms,
                endTime=end_ms,
                limit=8
            )
            
            if not klines:
                logger.error(f"No candles returned for {symbol}")
                return []
            
            # Format candles with proper timestamps
            candles = []
            for k in klines:
                dt = datetime.fromtimestamp(k[0] / 1000)
                
                # Format timestamp based on timeframe
                if timeframe == TimeFrame.MONTHLY:
                    formatted_time = dt.strftime("%Y-%m")  # YYYY-MM
                elif timeframe == TimeFrame.WEEKLY:
                    formatted_time = dt.strftime("%Y-%m-%d")  # Show Monday date
                else:
                    formatted_time = dt.strftime("%Y-%m-%d")  # Full date for daily
                
                candles.append({
                    'timestamp': formatted_time,
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4]),
                    'volume': float(k[5])
                })
            
            return candles[-8:]  # Ensure exactly 8 candles
            
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
                
            # Get reference price from stored prices
            ref_price = None
            if order.symbol in self.reference_prices:
                ref_price = self.reference_prices[order.symbol].get(order.timeframe)
            
            return await self.chart_generator.generate_trade_chart(
                candles,
                order,
                Decimal(str(ref_price)) if ref_price else None
            )
            
        except Exception as e:
            logger.error(f"Failed to generate trade chart: {e}")
            return None

    async def get_symbol_ticker(self, symbol: str) -> Dict:
        """Get current price for symbol"""
        try:
            await self.rate_limiter.acquire()
            ticker = await self.client.get_symbol_ticker(symbol=symbol)
            return {
                'symbol': symbol,
                'price': ticker['price']
            }
        except Exception as e:
            logger.error(f"Failed to get ticker for {symbol}: {e}")
            raise

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for futures trading"""
        try:
            await self.client.futures_change_leverage(
                symbol=symbol, 
                leverage=leverage
            )
            return True
        except Exception as e:
            logger.error(f"Failed to set leverage: {e}")
            return False

    async def set_margin_type(self, symbol: str, margin_type: str) -> bool:
        """Set margin type for futures trading"""
        try:
            await self.client.futures_change_margin_type(
                symbol=symbol,
                marginType=margin_type
            )
            return True
        except Exception as e:
            logger.error(f"Failed to set margin type: {e}")
            return False

    async def get_position_mode(self) -> str:
        """Get current position mode"""
        try:
            result = await self.client.futures_get_position_mode()
            return 'HEDGE' if result['dualSidePosition'] else 'ONE_WAY'
        except Exception as e:
            logger.error(f"Failed to get position mode: {e}")
            return 'ONE_WAY'

    async def set_position_mode(self, mode: str) -> bool:
        """Set position mode for futures trading"""
        try:
            dual_side = mode == 'HEDGE'
            await self.client.futures_change_position_mode(
                dualSidePosition=dual_side
            )
            return True
        except Exception as e:
            logger.error(f"Failed to set position mode: {e}")
            return False
