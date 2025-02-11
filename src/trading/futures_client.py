from binance.um_futures import UMFutures  # Updated import
from binance.exceptions import BinanceAPIException
from decimal import Decimal
from datetime import datetime, timedelta
import logging
from typing import Dict, Optional, Tuple, List, Union, Any
from ..types.models import Order, OrderStatus, TimeFrame, OrderType, TradeDirection
from ..types.constants import TIMEFRAME_INTERVALS
from ..utils.chart_generator import ChartGenerator  # Add this import
import asyncio
import time
import math
import random

logger = logging.getLogger(__name__)

FUTURES_INTERVALS = {
    'DAILY': timedelta(days=1),
    'WEEKLY': timedelta(weeks=1),
    'MONTHLY': timedelta(days=30)  # Approximation for a month
}

class FuturesClient:
    def __init__(self, network_config: Dict):
        """
        Initialize FuturesClient with network configuration
        network_config should contain:
        {
            'api_key': str,
            'api_secret': str,
            'testnet': bool,
            'default_leverage': int,
            'default_margin_type': str,  # 'ISOLATED' or 'CROSSED'
            'position_mode': str  # 'ONE_WAY' or 'HEDGE'
        }
        """
        self.api_key = network_config['api_key']
        self.api_secret = network_config['api_secret']
        self.testnet = network_config['testnet']
        self.default_leverage = network_config.get('default_leverage', 5)
        self.default_margin_type = network_config.get('default_margin_type', 'ISOLATED')
        self.position_mode = network_config.get('position_mode', 'ONE_WAY')
        self.client: Optional[UMFutures] = None
        self.symbol_info = {}
        self.telegram_bot = None  # Add this line
        self.reference_prices = {}  # Add this line
        self.timeframe_reset = {}  # Add this line
        self.chart_generator = ChartGenerator()  # Initialize chart generator
        
        # Add missing attributes for threshold tracking
        self.triggered_thresholds = {}
        self.last_reset = {tf: datetime.utcnow() for tf in TimeFrame}
        self.intervals = FUTURES_INTERVALS

        # Add timestamp offset tracking
        self.time_offset = 0
        self.last_timestamp = 0
        self.recv_window = 5000  # Default to 5000ms recvWindow

        # Update timestamp settings
        self.time_offset = 0
        self.last_sync = 0
        self.sync_interval = 30  # Sync every 30 seconds
        self.recv_window = 10000  # Increase from 5000 to 10000ms
        self.max_retries = 3
        self.retry_delay = 1

    def set_telegram_bot(self, bot):
        """Set telegram bot for notifications"""
        self.telegram_bot = bot

    def _get_timestamp(self) -> int:
        """Get current timestamp with server offset"""
        return int(time.time() * 1000 + self.time_offset)

    async def _sync_time(self, force: bool = False) -> bool:
        """Synchronize time with Binance server with retries"""
        now = time.time()
        
        # Only sync if forced or interval elapsed
        if not force and (now - self.last_sync) < self.sync_interval:
            return True
            
        for attempt in range(self.max_retries):
            try:
                # Get server time
                start_time = time.time() * 1000
                server_time = self.client.time()['serverTime']
                end_time = time.time() * 1000
                
                # Calculate network latency
                latency = (end_time - start_time) / 2
                
                # Only use response if latency is acceptable
                if latency < 100:  # Less than 100ms latency
                    self.time_offset = int(server_time - ((start_time + end_time) / 2))
                    self.last_sync = now
                    logger.debug(f"Time sync successful - Offset: {self.time_offset}ms, Latency: {latency:.2f}ms")
                    return True
                    
                # Add jitter to retry delay
                await asyncio.sleep(self.retry_delay * (1 + random.random()))
                self.retry_delay *= 2  # Exponential backoff
                
            except Exception as e:
                logger.warning(f"Time sync attempt {attempt + 1} failed: {e}")
                await asyncio.sleep(self.retry_delay)
                self.retry_delay *= 2
                
        logger.error("Failed to synchronize time after max retries")
        return False

    async def _make_request(self, method: str, **kwargs) -> Any:
        """Make API request with automatic time sync and retries"""
        for attempt in range(self.max_retries):
            try:
                # Ensure time is synced
                if not await self._sync_time():
                    raise Exception("Time synchronization failed")
                    
                # Add timestamp and recvWindow to all requests
                kwargs.update({
                    'timestamp': self._get_timestamp(),
                    'recvWindow': self.recv_window
                })
                
                # Make request
                return getattr(self.client, method)(**kwargs)
                
            except BinanceAPIException as e:
                if e.code == -1021:  # Timestamp error
                    logger.warning(f"Timestamp error on attempt {attempt + 1}, resyncing time")
                    await self._sync_time(force=True)
                    continue
                raise
                
            except Exception as e:
                logger.error(f"Request failed: {e}")
                if attempt == self.max_retries - 1:
                    raise
                    
            # Add jitter to retry delay
            await asyncio.sleep(self.retry_delay * (1 + random.random()))
            self.retry_delay *= 2

    async def initialize(self):
        """Initialize futures client with improved time sync"""
        try:
            self.client = UMFutures(
                key=self.api_key,
                secret=self.api_secret,
                base_url="https://testnet.binancefuture.com" if self.testnet else "https://fapi.binance.com"
            )

            # Initial time sync
            if not await self._sync_time(force=True):
                logger.error("Failed to synchronize time during initialization")
                return False

            # Rest of initialization using _make_request
            exchange_info = await self._make_request('exchange_info')
            for symbol in exchange_info['symbols']:
                self.symbol_info[symbol['symbol']] = {
                    'quantityPrecision': symbol['quantityPrecision'],
                    'pricePrecision': symbol['pricePrecision'],
                    'filters': {f['filterType']: f for f in symbol['filters']}
                }

            logger.info(
                f"Initialized Futures {'Testnet' if self.testnet else 'Mainnet'}\n"
                f"Position Mode: {self.position_mode}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to initialize futures client: {e}")
            return False

    async def setup_symbol(self, symbol: str, leverage: Optional[int] = None, 
                         margin_type: Optional[str] = None) -> bool:
        """Setup symbol-specific configurations with improved error handling"""
        try:
            target_leverage = leverage or self.default_leverage
            target_margin = (margin_type or self.default_margin_type).upper()
            
            logger.info(f"Configuring {symbol} with leverage: {target_leverage}x, margin: {target_margin}")

            # Set initial leverage
            try:
                self.client.change_leverage(
                    symbol=symbol,
                    leverage=target_leverage,
                    timestamp=self._get_timestamp(),
                    recvWindow=self.recv_window
                )
                logger.info(f"Set leverage for {symbol} to {target_leverage}x")
            except BinanceAPIException as e:
                if e.code == -4046:  # "No need to change leverage"
                    logger.info(f"Leverage already set correctly for {symbol}")
                else:
                    logger.error(f"Failed to set leverage: {e}")
                    return False

            # Set margin type - only attempt if different from current
            try:
                position_info = self.client.get_position_risk(
                    symbol=symbol,
                    timestamp=self._get_timestamp(),
                    recvWindow=self.recv_window
                )
                if position_info and len(position_info) > 0:
                    current_margin = position_info[0].get('marginType', '').upper()
                    
                    if current_margin == target_margin:
                        logger.info(f"Margin type already set to {target_margin} for {symbol}")
                    else:
                        try:
                            self.client.change_margin_type(
                                symbol=symbol,
                                marginType=target_margin,
                                timestamp=self._get_timestamp(),
                                recvWindow=self.recv_window
                            )
                            logger.info(f"Set margin type for {symbol} to {target_margin}")
                        except BinanceAPIException as e:
                            if e.code == -4046:  # "No need to change margin type"
                                logger.info(f"Margin type already set to {target_margin}")
                            else:
                                logger.warning(f"Failed to change margin type: {e}")
                else:
                    logger.warning(f"Could not get position info for {symbol}, using default margin type")
                    
            except Exception as e:
                # Don't treat as error if we can't check margin type
                logger.info(f"Could not verify margin type for {symbol}: {e}")
                
            return True

        except Exception as e:
            logger.error(f"Error in setup_symbol for {symbol}: {e}")
            return False

    def _adjust_quantity_precision(self, symbol: str, quantity: Decimal) -> Decimal:
        """Adjust quantity to the correct precision for futures"""
        try:
            precision = self.symbol_info[symbol]['quantityPrecision']
            adjusted = float(quantity)
            # Convert to string with correct precision, then back to Decimal
            return Decimal(str(round(adjusted, precision)))
        except Exception as e:
            logger.error(f"Error adjusting quantity precision: {e}")
            # Fallback to basic precision
            return Decimal(str(round(float(quantity), 4)))

    def _adjust_price_to_tick_size(self, symbol: str, price: Decimal) -> Decimal:
        """Adjust price to comply with symbol's tick size"""
        try:
            symbol_info = self.symbol_info.get(symbol)
            if not symbol_info:
                logger.warning(f"No symbol info found for {symbol}, using raw price")
                return price

            price_filter = symbol_info['filters'].get('PRICE_FILTER', {})
            if not price_filter:
                logger.warning(f"No price filter found for {symbol}, using raw price")
                return price

            tick_size = Decimal(str(price_filter.get('tickSize', '0.1')))
            
            # Round price to nearest tick size
            rounded_price = (price / tick_size).quantize(Decimal('1')) * tick_size
            logger.debug(f"Adjusted price from {price} to {rounded_price} (tick size: {tick_size})")
            return rounded_price

        except Exception as e:
            logger.error(f"Error adjusting price to tick size: {e}")
            return price

    async def place_futures_order(self, symbol: str, amount: float, direction: TradeDirection,
                                leverage: Optional[int] = None,
                                margin_type: Optional[str] = None,
                                signal_price: Optional[float] = None,
                                threshold: Optional[float] = None,
                                timeframe: Optional[TimeFrame] = None) -> Optional[Order]:
        """Place a futures order with proper position side handling"""
        try:
            # Only allow LONG orders
            if direction == TradeDirection.SHORT:
                logger.warning("SHORT orders are currently disabled")
                return None

            # Setup symbol configurations
            setup_success = await self.setup_symbol(symbol, leverage, margin_type)
            if not setup_success:
                logger.warning(f"Symbol setup had issues for {symbol}, attempting order anyway")

            # Get current market price
            ticker = self.client.ticker_price(symbol=symbol)
            current_price = Decimal(ticker['price'])

            # Use signal price if provided, otherwise use current price
            limit_price = Decimal(str(signal_price)) if signal_price else current_price
            
            # Calculate quantity based on USDT amount and leverage
            leverage = leverage or self.default_leverage
            quantity = (Decimal(str(amount)) * Decimal(str(leverage))) / limit_price
            
            # Adjust quantity precision
            quantity = self._adjust_quantity_precision(symbol, quantity)
            
            # Adjust price precision and tick size
            precision = self.symbol_info[symbol]['pricePrecision']
            limit_price = self._adjust_price_to_tick_size(
                symbol,
                Decimal(str(round(float(limit_price), precision)))
            )

            # Prepare order parameters
            order_params = {
                "symbol": symbol,
                "side": "BUY" if direction == TradeDirection.LONG else "SELL",
                "type": "LIMIT",
                "timeInForce": "GTC",
                "quantity": float(quantity),
                "price": float(limit_price),
                'timestamp': self._get_timestamp(),
                'recvWindow': self.recv_window
            }

            # Add position side based on position mode
            if self.position_mode == "HEDGE":
                order_params["positionSide"] = "LONG" if direction == TradeDirection.LONG else "SHORT"
            
            # Place the order with position side
            try:
                order_response = self.client.new_order(**order_params)
            except BinanceAPIException as e:
                logger.error(f"Failed to place order: {e}")
                return None

            # Create Order object
            order = Order(
                symbol=symbol,
                status=OrderStatus.PENDING,
                order_type=OrderType.FUTURES,
                price=limit_price,
                quantity=quantity,
                timeframe=timeframe or TimeFrame.DAILY,
                threshold=threshold,
                order_id=str(order_response['orderId']),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                filled_at=None,
                leverage=leverage,
                direction=direction,
                fee_asset='USDT',
                margin_type=margin_type or self.default_margin_type
            )

            logger.info(
                f"Placed {direction.value} futures order for {symbol}\n"
                f"Position Mode: {self.position_mode}\n"
                f"Position Side: {order_params.get('positionSide', 'ONE_WAY')}\n"
                f"Signal Price: ${float(limit_price):.2f}\n"
                f"Quantity: {float(quantity):.8f}\n"
                f"Leverage: {leverage}x"
            )
            return order

        except Exception as e:
            logger.error(f"Failed to place futures order: {e}")
            return None

    async def check_order_status(self, symbol: str, order_id: str) -> Optional[OrderStatus]:
        """Check status of a futures order"""
        try:
            order = self.client.query_order(
                symbol=symbol,
                orderId=order_id,
                timestamp=self._get_timestamp(),
                recvWindow=self.recv_window
            )
            
            if order['status'] == 'FILLED':
                return OrderStatus.FILLED
            elif order['status'] in ['CANCELED', 'CANCELLED', 'EXPIRED', 'REJECTED']:
                return OrderStatus.CANCELLED
            return OrderStatus.PENDING
            
        except Exception as e:
            logger.error(f"Failed to check order status: {e}")
            return None

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel a futures order"""
        try:
            self.client.cancel_order(
                symbol=symbol,
                orderId=order_id,
                timestamp=self._get_timestamp(),
                recvWindow=self.recv_window
            )
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order: {e}")
            return False

    async def get_trade_fee(self, symbol: str, order_response: Dict) -> Tuple[Decimal, str]:
        """Get the trading fee for a futures order"""
        try:
            # For testnet, simulate a fee
            if self.testnet:
                trade_value = Decimal(order_response['cumQuote'])
                fee = trade_value * Decimal('0.0004')  # 0.04% fee
                return fee, 'USDT'

            # For mainnet, get actual fee
            trades = await self.client.futures_account_trades(
                symbol=symbol,
                orderId=order_response['orderId'],
                timestamp=self._get_timestamp(),
                recvWindow=self.recv_window
            )
            
            total_fee = Decimal('0')
            fee_asset = 'USDT'
            
            for trade in trades:
                total_fee += Decimal(str(trade['commission']))
                fee_asset = trade['commissionAsset']

            return total_fee, fee_asset

        except Exception as e:
            logger.error(f"Failed to get trade fee: {e}")
            return Decimal('0'), 'USDT'

    async def close_position(self, symbol: str, position_data: Dict) -> Optional[Order]:
        """Close an open futures position"""
        try:
            quantity = abs(float(position_data['positionAmt']))
            direction = TradeDirection.SHORT if float(position_data['positionAmt']) > 0 else TradeDirection.LONG
            
            close_response = await self.client.new_order(
                symbol=symbol,
                side="SELL" if direction == TradeDirection.LONG else "BUY",
                type='MARKET',
                quantity=quantity,
                reduceOnly=True,
                timestamp=self._get_timestamp(),
                recvWindow=self.recv_window
            )

            # Create closing order object
            price = Decimal(str(close_response['avgPrice']))
            order = Order(
                symbol=symbol,
                status=OrderStatus.FILLED,
                order_type=OrderType.FUTURES,
                price=price,
                quantity=Decimal(str(quantity)),
                timeframe=TimeFrame.DAILY,
                order_id=str(close_response['orderId']),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                filled_at=datetime.utcnow(),
                leverage=int(position_data['leverage']),
                direction=direction,
                fee_asset='USDT'
            )

            # Get closing fee
            fee_info = await self.get_trade_fee(symbol, close_response)
            order.fees = fee_info[0]
            order.fee_asset = fee_info[1]

            return order

        except Exception as e:
            logger.error(f"Failed to close position: {e}")
            return None

    async def get_open_positions(self) -> Dict:
        """Get all open futures positions"""
        try:
            positions = await self._make_request('account')
            return {
                pos['symbol']: {
                    'symbol': pos['symbol'],
                    'positionAmt': pos.get('positionAmt', '0'),
                    'entryPrice': pos.get('entryPrice', '0'),  # Use get() with default
                    'unrealizedProfit': pos.get('unrealizedProfit', '0'),
                    'leverage': pos.get('leverage', '1'),
                    'marginType': pos.get('marginType', self.default_margin_type),
                    'updateTime': datetime.utcnow().timestamp() * 1000
                }
                for pos in positions['positions']
                if abs(float(pos.get('positionAmt', '0'))) > 0
            }
        except Exception as e:
            logger.error(f"Failed to get open positions: {e}")
            return {}

    async def get_balance_changes(self, symbol: str = 'USDT') -> Optional[Decimal]:
        """Get balance changes since last check"""
        # Maintain a cache of previous balances
        if not hasattr(self, 'balance_cache'):
            self.balance_cache = {}

        try:
            # Get current wallet balance from futures account
            account = self.client.account()
            current_balance = Decimal(account['availableBalance'])

            # Get previous balance from cache
            previous_balance = self.balance_cache.get(symbol)
            
            # Update cache
            self.balance_cache[symbol] = current_balance

            # Calculate change if we have a previous balance
            if previous_balance is not None:
                return current_balance - previous_balance
            return None

        except Exception as e:
            logger.error(f"Failed to get balance changes: {e}")
            return None

    async def get_symbol_ticker(self, symbol: str) -> Dict:
        """Get futures market price for symbol"""
        try:
            # Use ticker_price instead of mark_price for consistency
            ticker = self.client.ticker_price(symbol=symbol)
            return {
                'symbol': symbol,
                'price': ticker['price']
            }
        except Exception as e:
            logger.error(f"Failed to get futures ticker: {e}")
            raise

    async def get_account_info(self) -> Dict:
        """Get futures account information"""
        try:
            account = self.client.account()
            return {
                'totalWalletBalance': Decimal(account['totalWalletBalance']),
                'totalUnrealizedProfit': Decimal(account['totalUnrealizedProfit']),
                'availableBalance': Decimal(account['availableBalance']),
                'positions': {
                    pos['symbol']: {
                        'amount': Decimal(pos['positionAmt']),
                        'entryPrice': Decimal(pos['entryPrice']),
                        'unrealizedProfit': Decimal(pos['unrealizedProfit']),
                        'leverage': int(pos['leverage']),
                        'marginType': pos['marginType']
                    }
                    for pos in account['positions']
                    if float(pos['positionAmt']) != 0
                }
            }
        except Exception as e:
            logger.error(f"Failed to get account info: {e}")
            return {}

    async def cleanup(self):
        """Cleanup futures client"""
        if self.client:
            await self.client.close_connection()

    async def update_reference_prices(self, symbols: List[str]):
        """Update reference prices for futures market"""
        try:
            for symbol in symbols:
                if (symbol not in self.reference_prices):
                    self.reference_prices[symbol] = {}
                    self.triggered_thresholds[symbol] = {tf: [] for tf in TimeFrame}

                # Use ticker_price instead of futures_symbol_ticker
                ticker = self.client.ticker_price(symbol=symbol)
                current_price = float(ticker['price'])

                logger.info(f"\n=== Checking {symbol} ===")
                logger.info(f"Current futures price for {symbol}: ${current_price:,.2f}")

                for timeframe in TimeFrame:
                    logger.info(f"  ▶ Getting {timeframe.value} futures reference price")
                    ref_price = await self.get_reference_price(symbol, timeframe)
                    
                    if (ref_price is not None):
                        self.reference_prices[symbol][timeframe] = ref_price
                    else:
                        logger.warning(f"    Using current futures price as {timeframe.value} reference")
                        self.reference_prices[symbol][timeframe] = current_price

                await asyncio.sleep(0.1)

        except Exception as e:
            logger.error(f"Failed to update futures prices: {e}", exc_info=True)
            raise

    async def get_reference_price(self, symbol: str, timeframe: TimeFrame) -> float:
        """Get reference price from futures market"""
        try:
            interval_map = {
                TimeFrame.DAILY: '1d',
                TimeFrame.WEEKLY: '1w',
                TimeFrame.MONTHLY: '1M'
            }
            
            interval = interval_map[timeframe]
            
            # Use klines instead of futures_klines
            klines = self.client.klines(
                symbol=symbol,
                interval=interval,
                limit=1
            )
            
            if (klines and len(klines) > 0):
                ref_price = float(klines[0][1])  # Opening price
                logger.info(f"    {timeframe.value} reference: ${ref_price:,.2f}")
                return ref_price
            else:
                logger.warning(f"No futures kline data for {symbol} {timeframe.value}")
                return None
                
        except Exception as e:
            logger.error(f"Failed to get futures reference price for {symbol} {timeframe.value}: {e}", exc_info=True)
            return None

    async def check_timeframe_reset(self, timeframe: TimeFrame) -> bool:
        """Check if timeframe needs reset for futures"""
        now = datetime.utcnow()
        
        # Get interval duration from FUTURES_INTERVALS
        interval = self.intervals.get(timeframe.value.upper())
        if not interval:
            logger.error(f"Invalid timeframe: {timeframe}")
            return False
            
        try:
            if now - self.last_reset[timeframe] >= interval:
                logger.info(f"Resetting {timeframe.value} thresholds")
                self.last_reset[timeframe] = now
                
                # Clear triggered thresholds for this timeframe
                for symbol in self.triggered_thresholds:
                    self.triggered_thresholds[symbol][timeframe] = []
                    
                return True
                
            return False
            
        except Exception as e:
            logger.error(f"Error checking timeframe reset: {e}")
            return False

    async def check_thresholds(self, symbol: str, thresholds: Dict[str, List[float]]) -> Optional[tuple]:
        """Check price against thresholds for futures market"""
        try:
            # Use ticker_price instead of futures_symbol_ticker
            ticker = self.client.ticker_price(symbol=symbol)
            current_price = float(ticker['price'])
            
            for timeframe in TimeFrame:
                # Skip if timeframe not in thresholds
                if timeframe.value not in thresholds:
                    continue

                if symbol not in self.reference_prices:
                    continue
                    
                ref_price = self.reference_prices[symbol].get(timeframe)
                if not ref_price:
                    continue
                    
                # We only want to trigger orders when price drops (for LONG)
                price_change = ((ref_price - current_price) / ref_price) * 100
                logger.debug(f"{symbol} {timeframe.value} price change: {price_change:.2f}%")
                
                # Check thresholds from lowest to highest
                for threshold in sorted(thresholds[timeframe.value]):
                    if (price_change >= threshold and 
                        threshold not in self.triggered_thresholds[symbol][timeframe]):
                        logger.info(f"Threshold triggered for {symbol}: {threshold}% on {timeframe.value}")
                        
                        # Send threshold notification before updating triggered list
                        if self.telegram_bot:
                            await self.telegram_bot.send_threshold_notification(
                                symbol=symbol,
                                timeframe=timeframe,
                                threshold=threshold,
                                current_price=current_price,
                                reference_price=ref_price,
                                price_change=price_change
                            )
                        
                        self.triggered_thresholds[symbol][timeframe].append(threshold)
                        return timeframe, threshold
                        
            return None
            
        except Exception as e:
            logger.error(f"Error checking thresholds for {symbol}: {e}", exc_info=True)
            return None

    async def ping(self):
        """Ping method for health checks"""
        try:
            return self.client.ping()  # UMFutures ping is synchronous
        except Exception as e:
            logger.error(f"Ping failed: {e}")
            raise

    async def process_symbol(self, symbol: str):
        """Process symbol with futures pricing"""
        try:
            # Use synchronous mark_price method
            ticker = self.client.ticker_price(symbol=symbol)
            current_price = float(ticker['price'])
            
            # Update reference prices
            await self.update_reference_prices([symbol])
            
            # Check timeframes
            for timeframe in TimeFrame:
                await self.check_timeframe_reset(timeframe)
                thresholds = await self.check_thresholds(symbol, timeframe)
                if thresholds:
                    logger.info(f"Thresholds triggered for {symbol}: {thresholds}")
                    
            return current_price
            
        except Exception as e:
            logger.error(f"Error processing symbol {symbol}: {e}")
            raise

    async def get_balance(self, symbol: str = 'USDT') -> Decimal:
        """Get futures wallet balance"""
        try:
            account = self.client.account()  # Use account() instead of balance()
            return Decimal(str(account['availableBalance']))
        except Exception as e:
            logger.error(f"Failed to get futures balance: {e}")
            return Decimal('0')

    async def get_account(self) -> Dict:
        """Get futures account information in spot-compatible format"""
        try:
            account = self.client.account()
            return {
                'balances': [{
                    'asset': 'USDT',
                    'free': account['availableBalance'],
                    'locked': account.get('initialMargin', '0'),
                    'total': account['totalWalletBalance']
                }]
            }
        except Exception as e:
            logger.error(f"Failed to get futures account: {e}")
            return {'balances': []}

    # Add reserve balance property
    @property
    def reserve_balance(self) -> float:
        return getattr(self, '_reserve_balance', 0)

    @reserve_balance.setter
    def reserve_balance(self, value: float):
        self._reserve_balance = float(value)

    async def get_candles_for_chart(self, symbol: str, timeframe: TimeFrame, count: int = 15) -> List[Dict]:
        """Get candles for chart generation"""
        try:
            interval_map = {
                TimeFrame.DAILY: '1d',
                TimeFrame.WEEKLY: '1w',
                TimeFrame.MONTHLY: '1M'
            }
            
            interval = interval_map[timeframe]
            
            klines = self.client.klines(
                symbol=symbol,
                interval=interval,
                limit=count
            )
            
            if not klines:
                logger.error(f"No candles returned for {symbol} {timeframe.value}")
                return []
                
            # Convert klines to candle format
            candles = []
            for k in klines:
                candles.append({
                    'timestamp': k[0],
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4]),
                    'volume': float(k[5])
                })
                
            return candles
            
        except Exception as e:
            logger.error(f"Failed to get candles for chart: {e}")
            return []

    async def generate_trade_chart(self, order: Order) -> Optional[bytes]:
        """Generate chart for a futures trade"""
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
            logger.error(f"Failed to generate futures trade chart: {e}")
            return None

    # ...rest of existing code...
