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
        
        # Properly read reserve balance from config
        self._reserve_balance = None  # Initialize private attribute first
        
        # Read reserve balance in priority order
        if 'reserve_balance' in network_config:
            self.reserve_balance = float(network_config['reserve_balance'])
        elif 'trading' in network_config and 'reserve_balance' in network_config['trading']:
            self.reserve_balance = float(network_config['trading']['reserve_balance'])
        elif 'env' in network_config and network_config['env'].get('TRADING_RESERVE_BALANCE'):
            self.reserve_balance = float(network_config['env']['TRADING_RESERVE_BALANCE'])
        else:
            self.reserve_balance = 500  # Default to 500 instead of 0
            logger.info("[INIT] No reserve balance configured, using default: $500.00")

        logger.info(f"[INIT] Reserve balance set to: ${self.reserve_balance:,.2f}")
        
        # Add missing attributes for threshold tracking
        self.triggered_thresholds = {}
        self.last_reset = {tf: datetime.utcnow() for tf in TimeFrame}
        self.intervals = FUTURES_INTERVALS

        # Add timestamp offset tracking
        self.time_offset = 0
        self.last_timestamp = 0
        self.recv_window = 60000  # Increased from 5000 to 60000
        self.sync_interval = 30   # Decreased from 60 to 30 for more frequent syncs
        self.max_retries = 5
        self.retry_delay = 1
        self.max_delay = 30  # Maximum delay between retries

        self.tp_enabled = network_config.get('tp_enabled', False)
        self.sl_enabled = network_config.get('sl_enabled', False)
        self.default_tp_percent = float(network_config.get('default_tp_percent', 50))
        self.default_sl_percent = float(network_config.get('default_sl_percent', 10))

    def set_telegram_bot(self, bot):
        """Set telegram bot for notifications"""
        self.telegram_bot = bot

    def _get_timestamp(self) -> int:
        """Get current timestamp with server offset"""
        return int(time.time() * 1000 + self.time_offset)

    async def _sync_time(self, force: bool = False) -> bool:
        """Synchronize time with Binance server with improved retries"""
        now = time.time()
        
        # Sync more frequently and always sync if last sync was more than 30 seconds ago
        if not force and (now - self.last_sync) < 30:
            return True
            
        retry_delay = self.retry_delay
        for attempt in range(self.max_retries):
            try:
                server_time = await asyncio.get_event_loop().run_in_executor(
                    None, 
                    lambda: self.client.time()['serverTime']
                )
                
                self.time_offset = server_time - int(now * 1000)
                self.last_sync = now
                logger.info(f"Time sync successful - Offset: {self.time_offset}ms")
                return True
                
            except Exception as e:
                logger.warning(f"Time sync attempt {attempt + 1} failed: {e}")
            
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, self.max_delay)
            
        logger.error("Failed to synchronize time after max retries")
        return False

    async def _make_request(self, method: str, **kwargs) -> Any:
        """Make API request with improved error handling"""
        retry_delay = self.retry_delay
        
        for attempt in range(self.max_retries):
            try:
                # Only sync time before critical operations that need timestamp
                if method not in ['exchange_info', 'ping', 'time']:  # Add exceptions for methods that don't need timestamp
                    if not await self._sync_time():
                        raise Exception("Time synchronization failed")
                    # Add timestamp and increased recvWindow only for methods that need it
                    kwargs.update({
                        'timestamp': self._get_timestamp(),
                        'recvWindow': self.recv_window
                    })
                
                # Convert synchronous client methods to async
                client_method = getattr(self.client, method)
                response = await asyncio.get_event_loop().run_in_executor(
                    None, 
                    lambda: client_method(**kwargs)
                )
                return response
                
            except BinanceAPIException as e:
                if e.code == -1021:  # Timestamp error
                    # Force time sync on timestamp error
                    await self._sync_time(force=True)
                    continue
                raise
                
            except (asyncio.TimeoutError, Exception) as e:
                logger.error(f"Request failed (attempt {attempt + 1}): {e}")
                if attempt == self.max_retries - 1:
                    raise
                    
                # Add jitter to retry delay
                jittered_delay = retry_delay * (1 + random.random())
                await asyncio.sleep(jittered_delay)
                retry_delay = min(retry_delay * 2, self.max_delay)

    async def initialize(self):
        """Initialize futures client with improved error handling"""
        try:
            self.client = UMFutures(
                key=self.api_key,
                secret=self.api_secret,
                base_url="https://testnet.binancefuture.com" if self.testnet else "https://fapi.binance.com"
            )

            # Initial time sync with retries
            if not await self._sync_time(force=True):
                logger.error("Failed to synchronize time during initialization")
                return False
                
            # Get exchange info with retries - wrap sync call in async
            try:
                exchange_info = await asyncio.get_event_loop().run_in_executor(
                    None, self.client.exchange_info
                )
                
                # Process and store symbol info
                for symbol in exchange_info['symbols']:
                    self.symbol_info[symbol['symbol']] = {
                        'quantityPrecision': symbol['quantityPrecision'],
                        'pricePrecision': symbol['pricePrecision'],
                        'filters': {f['filterType']: f for f in symbol['filters']}
                    }
                
                # Set reserve balance from constructor
                logger.info(f"[INIT] Reserve balance set to: ${self.reserve_balance:,.2f}")
                
                logger.info(
                    f"Initialized Futures {'Testnet' if self.testnet else 'Mainnet'}\n"
                    f"Position Mode: {self.position_mode}"
                )
                return True
                
            except Exception as e:
                logger.error(f"Failed to get exchange info: {e}")
                return False

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

    async def check_reserve_balance(self, order_amount: float) -> bool:
        """Check if placing a futures order would violate reserve balance"""
        try:
            logger.info("[RESERVE CHECK] Starting futures reserve balance check...")
            
            # Get current futures account info
            account = await self._make_request('account')
            
            # Get current available balance (this is the real usable margin)
            available_balance = Decimal(str(account['availableBalance']))
            total_margin = Decimal(str(account.get('totalMarginBalance', '0')))
            unrealized_pnl = Decimal(str(account.get('totalUnrealizedProfit', '0')))
            
            # Validate reserve balance is set
            if self.reserve_balance <= 0:
                logger.warning("[RESERVE CHECK] Reserve balance is not set or invalid, using 0")
                self.reserve_balance = 0
            
            # Log all balance components
            logger.info(f"[RESERVE CHECK] Available Balance: ${float(available_balance):,.2f}")
            logger.info(f"[RESERVE CHECK] Total Margin Balance: ${float(total_margin):,.2f}")
            logger.info(f"[RESERVE CHECK] Unrealized P/L: ${float(unrealized_pnl):,.2f}")
            logger.info(f"[RESERVE CHECK] Reserve Required: ${float(self.reserve_balance):,.2f}")
            
            # Calculate margin required for new position with leverage consideration
            leverage = self.default_leverage
            margin_required = Decimal(str(order_amount)) / Decimal(str(leverage))
            
            # Calculate remaining balance after new position
            remaining_after_order = float(available_balance - margin_required)
            
            logger.info(f"[RESERVE CHECK] New Position Margin Required: ${float(margin_required):,.2f}")
            logger.info(f"[RESERVE CHECK] Remaining After Order: ${remaining_after_order:,.2f}")

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
                        current_balance=available_balance,
                        reserve_balance=self.reserve_balance,
                        pending_value=margin_required
                    )

            return is_valid

        except Exception as e:
            logger.error(f"[RESERVE CHECK] Error checking futures reserve balance: {e}")
            return False

    async def place_futures_order(self, symbol: str, amount: float, direction: TradeDirection,
                                leverage: Optional[int] = None,
                                margin_type: Optional[str] = None,
                                signal_price: Optional[float] = None,
                                threshold: Optional[float] = None,
                                timeframe: Optional[TimeFrame] = None) -> Optional[Order]:
        """Place a futures order with TP/SL support"""
        try:
            # Check reserve balance first
            if not await self.check_reserve_balance(amount):
                logger.error("Order would violate reserve balance")
                return None

            # Only allow LONG orders
            if direction == TradeDirection.SHORT:
                logger.warning("SHORT orders are currently disabled")
                return None

            # Setup symbol configurations
            setup_success = await self.setup_symbol(symbol, leverage, margin_type)
            if not setup_success:
                logger.warning(f"Symbol setup had issues for {symbol}, attempting order anyway")

            # Get current market price and convert to Decimal
            ticker = self.client.ticker_price(symbol=symbol)
            current_price = Decimal(str(ticker['price']))

            # Use signal price if provided, otherwise use current price
            limit_price = Decimal(str(signal_price)) if signal_price else current_price
            
            # Convert amount and leverage to Decimal for calculations
            dec_amount = Decimal(str(amount))
            dec_leverage = Decimal(str(leverage or self.default_leverage))
            
            # Calculate quantity based on USDT amount and leverage
            quantity = (dec_amount * dec_leverage) / limit_price
            
            # Adjust quantity precision
            quantity = self._adjust_quantity_precision(symbol, quantity)
            
            # Adjust price precision and tick size
            precision = self.symbol_info[symbol]['pricePrecision']
            limit_price = self._adjust_price_to_tick_size(
                symbol,
                Decimal(str(round(float(limit_price), precision)))
            )

            # Convert values to float for Binance API
            float_quantity = float(quantity)
            float_price = float(limit_price)

            # Prepare order parameters
            order_params = {
                "symbol": symbol,
                "side": "BUY" if direction == TradeDirection.LONG else "SELL",
                "type": "LIMIT",
                "timeInForce": "GTC",
                "quantity": float_quantity,
                "price": float_price,
                'timestamp': self._get_timestamp(),
                'recvWindow': self.recv_window
            }

            # Determine position side based on position mode
            position_side = None
            if self.position_mode == "HEDGE":
                position_side = "LONG" if direction == TradeDirection.LONG else "SHORT"
                order_params["positionSide"] = position_side

            # Place the main order
            order_response = self.client.new_order(**order_params)
            main_order_id = str(order_response['orderId'])

            # Initialize TP/SL variables
            tp_order_id = None
            sl_order_id = None
            tp_price = None
            sl_price = None

            # Place TP order if enabled
            if self.tp_enabled:
                tp_percent = Decimal(str(self.default_tp_percent)) / Decimal('100')
                tp_price = limit_price * (Decimal('1') + tp_percent)
                tp_price = self._adjust_price_to_tick_size(symbol, tp_price)
                
                tp_params = {
                    "symbol": symbol,
                    "side": "SELL" if direction == TradeDirection.LONG else "BUY",
                    "type": "TAKE_PROFIT_MARKET",
                    "stopPrice": float(tp_price),
                    "quantity": float_quantity,
                    "timestamp": self._get_timestamp(),
                    "recvWindow": self.recv_window,
                    "workingType": "MARK_PRICE"
                }

                # Only add reduceOnly if not in HEDGE mode
                if self.position_mode != "HEDGE":
                    tp_params["reduceOnly"] = True

                # Add position side for HEDGE mode
                if position_side:
                    tp_params["positionSide"] = position_side

                try:
                    tp_response = self.client.new_order(**tp_params)
                    tp_order_id = str(tp_response['orderId'])
                    logger.info(f"Take Profit order placed at ${float(tp_price):,.2f} ({self.default_tp_percent}%)")
                except Exception as e:
                    logger.error(f"Failed to place TP order: {e}")

            # Place SL order if enabled
            if self.sl_enabled:
                sl_percent = Decimal(str(self.default_sl_percent)) / Decimal('100')
                sl_price = limit_price * (Decimal('1') - sl_percent)
                sl_price = self._adjust_price_to_tick_size(symbol, sl_price)
                
                sl_params = {
                    "symbol": symbol,
                    "side": "SELL" if direction == TradeDirection.LONG else "BUY",
                    "type": "STOP_MARKET",
                    "stopPrice": float(sl_price),
                    "quantity": float_quantity,
                    "timestamp": self._get_timestamp(),
                    "recvWindow": self.recv_window,
                    "workingType": "MARK_PRICE"
                }

                # Only add reduceOnly if not in HEDGE mode
                if self.position_mode != "HEDGE":
                    sl_params["reduceOnly"] = True

                # Add position side for HEDGE mode
                if position_side:
                    sl_params["positionSide"] = position_side

                try:
                    sl_response = self.client.new_order(**sl_params)
                    sl_order_id = str(sl_response['orderId'])
                    logger.info(f"Stop Loss order placed at ${float(sl_price):,.2f} ({self.default_sl_percent}%)")
                except Exception as e:
                    logger.error(f"Failed to place SL order: {e}")

            # Create order object with position side
            order = Order(
                symbol=symbol,
                status=OrderStatus.PENDING,
                order_type=OrderType.FUTURES,
                price=limit_price,
                quantity=quantity,
                timeframe=timeframe or TimeFrame.DAILY,
                threshold=threshold,
                order_id=main_order_id,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                filled_at=None,
                leverage=int(dec_leverage),
                direction=direction,
                fee_asset='USDT',
                margin_type=margin_type or self.default_margin_type,
                tp_order_id=tp_order_id,
                sl_order_id=sl_order_id,
                tp_price=float(tp_price) if tp_price else None,
                sl_price=float(sl_price) if sl_price else None,
                position_side=position_side
            )

            logger.info(
                f"Placed {direction.value} futures order for {symbol}\n"
                f"Position Mode: {self.position_mode}\n"
                f"Position Side: {order_params.get('positionSide', 'ONE_WAY')}\n"
                f"Signal Price: ${float(limit_price):.2f}\n"
                f"Quantity: {float(quantity):.8f}\n"
                f"Leverage: {float(dec_leverage)}x"
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
        """Close an open futures position and cancel any associated TP/SL orders"""
        try:
            # Cancel any existing TP/SL orders first
            try:
                open_orders = self.client.get_open_orders(symbol=symbol)
                for order in open_orders:
                    if order['reduceOnly']:  # This identifies TP/SL orders
                        await self.cancel_order(symbol, order['orderId'])
            except Exception as e:
                logger.warning(f"Error cancelling TP/SL orders: {e}")

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
            # Use mark price instead of ticker_price for futures
            ticker = self.client.mark_price(symbol=symbol)
            return {
                'symbol': symbol,
                'price': ticker['markPrice']
            }
        except Exception as e:
            logger.error(f"Failed to get futures ticker: {e}")
            raise

    async def get_account_info(self) -> Dict:
        """Get futures account information with proper error handling"""
        try:
            account = self.client.account()
            # Add proper field checks and defaults
            return {
                'totalWalletBalance': float(account.get('totalWalletBalance', 0)),
                'totalUnrealizedProfit': float(account.get('totalUnrealizedProfit', 0)),
                'availableBalance': float(account.get('availableBalance', 0)),
                'positions': [
                    {
                        'symbol': pos['symbol'],
                        'positionAmt': float(pos.get('positionAmt', 0)),
                        'entryPrice': float(pos.get('entryPrice', 0)),
                        'unrealizedProfit': float(pos.get('unrealizedProfit', 0)),
                        'leverage': int(pos.get('leverage', 1)),
                        'marginType': pos.get('marginType', 'ISOLATED')
                    }
                    for pos in account.get('positions', [])
                    if abs(float(pos.get('positionAmt', 0))) > 0
                ]
            }
        except Exception as e:
            logger.error(f"Failed to get account info: {e}")
            return {
                'totalWalletBalance': 0,
                'totalUnrealizedProfit': 0,
                'availableBalance': 0,
                'positions': []
            }

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
        """Check if timeframe needs reset for futures with proper UTC handling"""
        now = datetime.utcnow()
        last_reset = self.last_reset.get(timeframe)
        
        if not last_reset:
            self.last_reset[timeframe] = now
            return True
            
        # Get current time components for precise reset checks
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
            logger.info(f"Resetting futures {timeframe.value} thresholds at {now}")
            self.last_reset[timeframe] = now
            
            # Clear triggered thresholds for all symbols for this timeframe
            for symbol in list(self.triggered_thresholds.keys()):
                if timeframe in self.triggered_thresholds[symbol]:
                    self.triggered_thresholds[symbol][timeframe] = []
                
            # Send reset notification with price info
            if self.telegram_bot:
                prices_info = []
                for symbol in self.reference_prices:
                    current = await self.get_current_price(symbol)
                    ref = self.reference_prices[symbol].get(timeframe)
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

    async def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current futures price with error handling"""
        try:
            ticker = self.client.ticker_price(symbol=symbol)
            return float(ticker['price'])
        except Exception as e:
            logger.error(f"Error getting futures price for {symbol}: {e}")
            return None

    async def check_thresholds(self, symbol: str, thresholds: Dict[str, List[float]]) -> Optional[tuple]:
        """Check futures price against thresholds with cooldown"""
        try:
            current_price = await self.get_current_price(symbol)
            if not current_price:
                return None
                
            for timeframe in TimeFrame:
                if timeframe.value not in thresholds:
                    continue

                ref_price = self.reference_prices.get(symbol, {}).get(timeframe)
                if not ref_price:
                    continue
                    
                price_change = ((ref_price - current_price) / ref_price) * 100
                logger.debug(f"Futures {symbol} {timeframe.value} change: {price_change:.2f}%")
                
                # Check thresholds from lowest to highest
                for threshold in sorted(thresholds[timeframe.value]):
                    # Only trigger if not already triggered
                    if (price_change >= threshold and 
                        threshold not in self.triggered_thresholds[symbol][timeframe]):
                        
                        logger.info(f"Futures threshold triggered for {symbol}: {threshold}% on {timeframe.value}")
                        self.triggered_thresholds[symbol][timeframe].append(threshold)
                        
                        # Send notification if bot is set
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
            logger.error(f"Error checking futures thresholds for {symbol}: {e}")
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
        """Get reserve balance with proper default"""
        return self._reserve_balance if self._reserve_balance is not None else 500

    @reserve_balance.setter
    def reserve_balance(self, value: Optional[float]) -> None:
        """Set reserve balance with validation"""
        try:
            if value is None:
                self._reserve_balance = 500  # Default to 500 if None
                logger.info("[INIT] Reserve balance defaulting to $500.00")
            else:
                parsed_value = float(value)
                if parsed_value < 0:
                    self._reserve_balance = 500
                    logger.warning("[INIT] Negative reserve balance not allowed, using default $500.00")
                else:
                    self._reserve_balance = parsed_value
        except (TypeError, ValueError) as e:
            self._reserve_balance = 500
            logger.warning(f"[INIT] Invalid reserve balance value: {e}, using default $500.00")

    async def get_candles_for_chart(self, symbol: str, timeframe: TimeFrame, count: int = 15) -> List[Dict]:
        """Get candles for chart generation with improved logging"""
        try:
            interval_map = {
                TimeFrame.DAILY: '1d',
                TimeFrame.WEEKLY: '1w',
                TimeFrame.MONTHLY: '1M'
            }
            
            interval = interval_map[timeframe]
            logger.info(f"Fetching {count} {interval} candles for {symbol}")
            
            # Get klines with proper parameters
            klines = self.client.klines(
                symbol=symbol,
                interval=interval,
                limit=count
            )
            
            logger.info(f"Received {len(klines)} klines from Binance")
            logger.debug(f"First kline sample: {klines[0] if klines else None}")
            
            if not klines:
                logger.error(f"No candles returned for {symbol}")
                return []
                
            # Convert klines to candle format
            candles = []
            for k in klines:
                try:
                    timestamp = int(k[0])  # Binance timestamp is in milliseconds
                    candle = {
                        'timestamp': timestamp,
                        'open': float(k[1]),
                        'high': float(k[2]),
                        'low': float(k[3]),
                        'close': float(k[4]),
                        'volume': float(k[5])
                    }
                    candles.append(candle)
                except (ValueError, IndexError) as e:
                    logger.error(f"Error processing kline: {e}, Data: {k}")
                    continue
            
            logger.info(f"Successfully processed {len(candles)} candles")
            logger.debug(f"First processed candle: {candles[0] if candles else None}")
            
            return candles
            
        except Exception as e:
            logger.error(f"Failed to get futures candles: {e}", exc_info=True)
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

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage with improved error handling"""
        try:
            await self._make_request('change_leverage', 
                symbol=symbol,
                leverage=leverage
            )
            logger.info(f"Successfully set leverage for {symbol} to {leverage}x")
            return True
        except BinanceAPIException as e:
            if e.code == -4046:  # "No need to change leverage"
                logger.info(f"Leverage already set to {leverage}x for {symbol}")
                return True
            logger.error(f"Failed to set leverage: {e}")
            return False

    async def set_margin_type(self, symbol: str, margin_type: str) -> bool:
        """Set margin type with improved error handling"""
        try:
            await self._make_request('change_margin_type',
                symbol=symbol,
                marginType=margin_type
            )
            logger.info(f"Successfully set margin type for {symbol} to {margin_type}")
            return True
        except BinanceAPIException as e:
            if e.code == -4046:  # "No need to change margin type"
                logger.info(f"Margin type already set to {margin_type}")
                return True
            logger.error(f"Failed to set margin type: {e}")
            return False

    async def get_position_mode(self) -> str:
        """Get current position mode with error handling"""
        try:
            result = await self._make_request('get_position_mode')
            mode = 'HEDGE' if result.get('dualSidePosition') else 'ONE_WAY'
            logger.info(f"Current position mode: {mode}")
            return mode
        except Exception as e:
            logger.error(f"Failed to get position mode: {e}")
            return 'ONE_WAY'  # Default to ONE_WAY mode

    async def set_position_mode(self, mode: str) -> bool:
        """Set position mode with improved error handling"""
        try:
            dual_side = mode == 'HEDGE'
            await self._make_request('change_position_mode',
                dualSidePosition=dual_side
            )
            logger.info(f"Successfully set position mode to {mode}")
            return True
        except BinanceAPIException as e:
            if e.code == -4059:  # "No need to change position side"
                logger.info(f"Position mode already set to {mode}")
                return True
            logger.error(f"Failed to set position mode: {e}")
            return False

    async def close_all_positions(self) -> bool:
        """Close all open positions when switching modes"""
        try:
            positions = await self.get_open_positions()
            success = True
            
            for symbol, pos in positions.items():
                if abs(float(pos['positionAmt'])) > 0:
                    result = await self.close_position(symbol, pos)
                    if not result:
                        success = False
                        logger.error(f"Failed to close position for {symbol}")
                        
            return success
            
        except Exception as e:
            logger.error(f"Error closing all positions: {e}")
            return False
