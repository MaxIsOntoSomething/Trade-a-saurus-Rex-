from binance.client import Client
from binance.enums import *
from datetime import datetime, timezone
import logging
from utils.rate_limiter import RateLimiter
import asyncio
import time

class BinanceAPI:
    def __init__(self, config, logger=None):
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.trading_mode = config['TRADING_SETTINGS']['MODE']
        self.use_testnet = config['TRADING_SETTINGS']['USE_TESTNET']
        
        # Initialize appropriate client
        if self.trading_mode == 'futures' and self.use_testnet:
            self.client = Client(
                config['FUTURES_TESTNET_API_KEY'],
                config['FUTURES_TESTNET_API_SECRET'],
                testnet=True
            )
            self.client.API_URL = 'https://testnet.binancefuture.com/fapi'
        elif self.trading_mode == 'futures':
            self.client = Client(
                config['BINANCE_API_KEY'],
                config['BINANCE_API_SECRET']
            )
            self.client.API_URL = 'https://fapi.binance.com/fapi'
        elif self.use_testnet:
            self.client = Client(
                config['TESTNET_API_KEY'],
                config['TESTNET_API_SECRET'],
                testnet=True
            )
            self.client.API_URL = 'https://testnet.binance.vision/api'
        else:
            self.client = Client(
                config['BINANCE_API_KEY'],
                config['BINANCE_API_SECRET']
            )

        # Initialize other settings
        self.rate_limiter = RateLimiter(max_requests=1200)
        self.symbol_info_cache = {}
        self.last_info_update = 0
        self.info_update_interval = 3600
        self.recv_window = 60000
        self.last_server_time = 0
        self.server_time_update_interval = 30  # Update every 30 seconds

        # Futures specific settings
        if self.trading_mode == 'futures':
            self.leverage = config['FUTURES_SETTINGS']['LEVERAGE']
            self.margin_type = config['FUTURES_SETTINGS']['MARGIN_TYPE']
            self.position_mode = config['FUTURES_SETTINGS']['POSITION_MODE']

        # Add API mode tracking
        self.api_mode = self._determine_api_mode()
        self.logger.info(f"Initializing Binance API in {self.api_mode.upper()} mode")

    def _determine_api_mode(self) -> str:
        """Determine which API mode we're using"""
        if self.trading_mode == 'futures' and self.use_testnet:
            return 'futures_testnet'
        elif self.trading_mode == 'futures':
            return 'futures'
        elif self.use_testnet:
            return 'spot_testnet'
        return 'spot'

    async def initialize_exchange_info(self):
        """Initialize exchange info with futures support"""
        try:
            if self.trading_mode == 'futures':
                exchange_info = self.client.futures_exchange_info()
            else:
                exchange_info = self.client.get_exchange_info()
                
            self.symbol_info_cache = {
                s['symbol']: s for s in exchange_info['symbols']
            }
            self.last_info_update = time.time()
            
            # Set up futures trading if needed
            if self.trading_mode == 'futures':
                await self._setup_futures_trading()
                
            return True
            
        except Exception as e:
            self.logger.error(f"Error initializing exchange info: {e}")
            return False

    async def _setup_futures_trading(self):
        """Configure futures trading settings"""
        try:
            for symbol in self.symbol_info_cache:
                # Set leverage
                self.client.futures_change_leverage(
                    symbol=symbol,
                    leverage=self.leverage
                )
                
                # Set margin type
                self.client.futures_change_margin_type(
                    symbol=symbol,
                    marginType=self.margin_type.upper()
                )
                
            # Set position mode
            self.client.futures_change_position_mode(
                dualSidePosition=self.position_mode == 'hedge'
            )
            
        except Exception as e:
            self.logger.error(f"Error setting up futures trading: {e}")
            raise

    async def create_order(self, symbol, side, quantity, price=None):
        """Enhanced order creation with validation"""
        try:
            # Get symbol info
            symbol_info = await self.get_symbol_info(symbol)
            if not symbol_info:
                raise ValueError(f"Symbol info not found for {symbol}")
                
            # Validate and format quantity
            quantity = await self._validate_order_quantity(symbol_info, quantity, price)
            
            if self.trading_mode == 'futures':
                quantity, price = await self._format_futures_order(symbol_info, quantity, price)
                return await self._create_futures_order(symbol, side, quantity, price)
            else:
                return await self._create_spot_order(symbol, side, quantity, price)
                
        except Exception as e:
            self.logger.error(f"Order creation failed: {e}")
            raise

    async def _create_futures_order(self, symbol, side, quantity, price=None):
        """Create futures order with proper formatting"""
        order_params = {
            'symbol': symbol,
            'side': side,
            'quantity': quantity
        }

        if price:
            order_params.update({
                'type': 'LIMIT',
                'price': price,
                'timeInForce': 'GTC'
            })
        else:
            order_params.update({
                'type': 'MARKET'
            })

        return await self._make_api_call(
            self.client.futures_create_order,
            **order_params
        )

    async def _create_spot_order(self, symbol, side, quantity, price=None):
        """Create spot order with proper formatting"""
        order_params = {
            'symbol': symbol,
            'side': side,
            'recvWindow': self.recv_window
        }

        if price:
            order_params.update({
                'type': ORDER_TYPE_LIMIT,
                'timeInForce': TIME_IN_FORCE_GTC,
                'price': price,
                'quantity': quantity
            })
        else:
            order_params.update({
                'type': ORDER_TYPE_MARKET,
                'quantity': quantity
            })
        response = self.client.create_order(**order_params)
                
        return response

    async def get_account_info(self):
        """Get account info based on API mode"""
        try:
            if self.api_mode == 'futures_testnet' or self.api_mode == 'futures':
                return await self._make_api_call(
                    self.client.futures_account,
                    _no_timestamp=True
                )
            else:
                return await self._make_api_call(
                    self.client.get_account,
                    _no_timestamp=True
                )
        except Exception as e:
            self.logger.error(f"Error getting account info: {e}")
            return None

    async def get_position_info(self, symbol):
        """Get position info based on API mode"""
        if 'futures' in self.api_mode:
            try:
                positions = await self._make_api_call(
                    self.client.futures_position_information,
                    symbol=symbol
                )
                return positions[0] if positions else None
            except Exception as e:
                self.logger.error(f"Error getting position info: {e}")
                return None
        return None

    async def get_symbol_ticker(self, symbol):
        """Get current price for a symbol"""
        try:
            # Remove the _make_api_call wrapper since ticker endpoints don't need timestamps
            if self.trading_mode == 'futures':
                # Direct call for futures
                await self.rate_limiter.acquire()
                return self.client.futures_symbol_ticker(symbol=symbol)
            else:
                # Direct call for spot
                await self.rate_limiter.acquire()
                return self.client.get_symbol_ticker(symbol=symbol)
        except Exception as e:
            self.logger.error(f"Error getting ticker for {symbol}: {e}")
            return None

    async def get_24h_stats(self, symbol):
        """Get 24-hour stats for a symbol"""
        try:
            await self.rate_limiter.acquire()
            if self.trading_mode == 'futures':
                return self.client.futures_ticker(symbol=symbol)
            else:
                return self.client.get_ticker(symbol=symbol)
        except Exception as e:
            self.logger.error(f"Error getting 24h stats for {symbol}: {e}")
            return None

    async def get_symbol_info(self, symbol):
        """Get symbol information"""
        try:
            # Update cache if needed
            current_time = time.time()
            if current_time - self.last_info_update > self.info_update_interval:
                await self.initialize_exchange_info()

            return self.symbol_info_cache.get(symbol)
        except Exception as e:
            self.logger.error(f"Error getting symbol info for {symbol}: {e}")
            return None

    async def _update_server_time(self):
        """Update server time"""
        try:
            server_time = await self._make_api_call(
                self.client.get_server_time,
                _no_timestamp=True
            )
            self.last_server_time = server_time['serverTime']
            return True
        except Exception as e:
            self.logger.error(f"Error updating server time: {e}")
            return False

    async def _make_api_call(self, func, *args, _no_timestamp=False, **kwargs):
        """Make API call with improved timestamp handling"""
        try:
            await self.rate_limiter.acquire()

            if not _no_timestamp:
                # Always get fresh server time for timestamp-sensitive calls
                await self._sync_time()
                
                # Use server time plus a small buffer
                current_time = self.last_server_time + 500  # Add 500ms buffer
                kwargs['timestamp'] = current_time
                
                # Use larger recvWindow for testnet
                kwargs['recvWindow'] = 60000 if self.use_testnet else 5000

            # Make the API call
            return func(*args, **kwargs)

        except Exception as e:
            if 'recvWindow' in str(e):
                # Force time resync on timestamp errors
                await self._sync_time()
                self.logger.error(f"Timestamp error in API call: {e}")
            else:
                self.logger.error(f"API call error: {e}")
            raise

    async def _format_order_amounts(self, symbol_info, price, quantity):
        """Format price and quantity according to symbol rules"""
        try:
            # Get filters
            price_filter = next(f for f in symbol_info['filters'] if f['filterType'] == 'PRICE_FILTER')
            lot_filter = next(f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE')
            
            # Get precision from tick size
            tick_size = float(price_filter['tickSize'])
            step_size = float(lot_filter['stepSize'])
            
            price_precision = len(str(tick_size).rstrip('0').split('.')[-1])
            quantity_precision = len(str(step_size).rstrip('0').split('.')[-1])
            
            # Format according to precision
            formatted_price = f"{price:.{price_precision}f}"
            formatted_quantity = f"{quantity:.{quantity_precision}f}"
            
            return formatted_price, formatted_quantity
            
        except Exception as e:
            self.logger.error(f"Error formatting order amounts: {e}")
            raise

    async def cancel_order(self, symbol, order_id):
        """Cancel order with proper timestamp"""
        try:
            # Get fresh server time
            server_time = self.client.get_server_time()
            timestamp = server_time['serverTime']
            
            if self.api_mode == 'futures_testnet' or self.api_mode == 'futures':
                return await self._make_api_call(
                    self.client.futures_cancel_order,
                    symbol=symbol,
                    orderId=order_id,
                    timestamp=timestamp,
                    recvWindow=60000
                )
            else:
                return await self._make_api_call(
                    self.client.cancel_order,
                    symbol=symbol,
                    orderId=order_id,
                    timestamp=timestamp,
                    recvWindow=60000
                )
        except Exception as e:
            self.logger.error(f"Error canceling order: {e}")
            return False

    async def get_open_orders(self, symbol=None):
        """Get open orders with optional symbol filter"""
        try:
            if self.api_mode == 'futures_testnet' or self.api_mode == 'futures':
                return await self._make_api_call(
                    self.client.futures_get_open_orders,
                    symbol=symbol
                )
            else:
                return await self._make_api_call(
                    self.client.get_open_orders,
                    symbol=symbol
                )
        except Exception as e:
            self.logger.error(f"Error getting open orders: {e}")
            return []

    async def change_leverage(self, symbol, leverage):
        """Change leverage for futures trading"""
        if not self.trading_mode == 'futures':
            return False
        try:
            return await self._make_api_call(
                self.client.futures_change_leverage,
                symbol=symbol,
                leverage=leverage
            )
        except Exception as e:
            self.logger.error(f"Error changing leverage: {e}")
            return False

    async def change_margin_type(self, symbol, margin_type):
        """Change margin type for futures trading"""
        if not self.trading_mode == 'futures':
            return False
        try:
            return await self._make_api_call(
                self.client.futures_change_margin_type,
                symbol=symbol,
                marginType=margin_type.upper()
            )
        except Exception as e:
            self.logger.error(f"Error changing margin type: {e}")
            return False

    async def get_balance(self, asset=None):
        """Get balance with proper timestamp handling"""
        for attempt in range(3):
            try:
                # Force time sync before balance check
                await self._sync_time()
                
                # Add small buffer to timestamp
                timestamp = self.last_server_time + 500
                
                params = {
                    'timestamp': timestamp,
                    'recvWindow': 60000  # Use maximum recvWindow for balance checks
                }

                if self.api_mode == 'futures_testnet' or self.api_mode == 'futures':
                    account = await self._make_api_call(
                        self.client.futures_account,
                        **params
                    )
                    # ...rest of the balance handling code...
                else:
                    account = await self._make_api_call(
                        self.client.get_account,
                        **params
                    )
                    # ...rest of the balance handling code...

            except Exception as e:
                self.logger.error(f"Balance check attempt {attempt + 1} failed: {e}")
                if attempt == 2:  # Last attempt
                    raise
                await asyncio.sleep(1)
                continue

    async def get_order_status(self, symbol, order_id):
        """Get order status with retry logic"""
        for attempt in range(3):
            try:
                if self.api_mode == 'futures_testnet' or self.api_mode == 'futures':
                    return await self._make_api_call(
                        self.client.futures_get_order,
                        symbol=symbol,
                        orderId=order_id
                    )
                else:
                    return await self._make_api_call(
                        self.client.get_order,
                        symbol=symbol,
                        orderId=order_id
                    )
            except Exception as e:
                if attempt == 2:  # Last attempt
                    self.logger.error(f"Error getting order status: {e}")
                    raise
                await asyncio.sleep(1)

    async def get_server_time(self):
        """Get server time directly"""
        try:
            return await self._make_api_call(
                self.client.get_server_time,
                _no_timestamp=True
            )
        except Exception as e:
            self.logger.error(f"Error getting server time: {e}")
            return None

    async def get_exchange_info(self):
        """Get exchange information"""
        try:
            if self.trading_mode == 'futures':
                return await self._make_api_call(
                    self.client.futures_exchange_info,
                    _no_timestamp=True
                )
            else:
                return await self._make_api_call(
                    self.client.get_exchange_info,
                    _no_timestamp=True
                )
        except Exception as e:
            self.logger.error(f"Error getting exchange info: {e}")
            return None

    async def get_orderbook(self, symbol, limit=100):
        """Get order book for a symbol"""
        try:
            if self.trading_mode == 'futures':
                return await self._make_api_call(
                    self.client.futures_order_book,
                    symbol=symbol,
                    limit=limit
                )
            else:
                return await self._make_api_call(
                    self.client.get_order_book,
                    symbol=symbol,
                    limit=limit
                )
        except Exception as e:
            self.logger.error(f"Error getting orderbook for {symbol}: {e}")
            return None

    async def get_recent_trades(self, symbol, limit=100):
        """Get recent trades for a symbol"""
        try:
            if self.trading_mode == 'futures':
                return await self._make_api_call(
                    self.client.futures_recent_trades,
                    symbol=symbol,
                    limit=limit
                )
            else:
                return await self._make_api_call(
                    self.client.get_recent_trades,
                    symbol=symbol,
                    limit=limit
                )
        except Exception as e:
            self.logger.error(f"Error getting recent trades for {symbol}: {e}")
            return None

    async def create_batch_orders(self, orders):
        """Create multiple orders in a single request"""
        try:
            if self.trading_mode == 'futures':
                return await self._make_api_call(
                    self.client.futures_create_batch_orders,
                    orders=orders
                )
            else:
                # Process spot orders sequentially as batch not supported
                responses = []
                for order in orders:
                    response = await self.create_order(**order)
                    responses.append(response)
                return responses
        except Exception as e:
            self.logger.error(f"Error creating batch orders: {e}")
            raise

    async def create_oco_order(self, symbol, side, quantity, price, stop_price, stop_limit_price):
        """Create OCO (One-Cancels-Other) order"""
        try:
            if self.trading_mode == 'futures':
                raise NotImplementedError("OCO orders not supported in futures mode")
                
            order_params = {
                'symbol': symbol,
                'side': side,
                'quantity': quantity,
                'price': price,
                'stopPrice': stop_price,
                'stopLimitPrice': stop_limit_price,
                'stopLimitTimeInForce': 'GTC',
                'recvWindow': self.recv_window
            }
            
            return await self._make_api_call(
                self.client.create_oco_order,
                **order_params
            )
        except Exception as e:
            self.logger.error(f"Error creating OCO order: {e}")
            raise

    async def create_conditional_order(self, symbol, side, quantity, stop_price, price=None):
        """Create conditional order (stop/stop-limit)"""
        try:
            order_params = {
                'symbol': symbol,
                'side': side,
                'quantity': quantity,
                'stopPrice': stop_price,
                'recvWindow': self.recv_window
            }

            if self.trading_mode == 'futures':
                order_params['type'] = 'STOP_MARKET'
                if price:
                    order_params['type'] = 'STOP'
                    order_params['price'] = price
                    order_params['timeInForce'] = 'GTC'
                return await self._make_api_call(
                    self.client.futures_create_order,
                    **order_params
                )
            else:
                if price:
                    order_params['type'] = 'STOP_LOSS_LIMIT'
                    order_params['price'] = price
                    order_params['timeInForce'] = 'GTC'
                else:
                    order_params['type'] = 'STOP_LOSS'
                return await self._make_api_call(
                    self.client.create_order,
                    **order_params
                )
        except Exception as e:
            self.logger.error(f"Error creating conditional order: {e}")
            raise

    async def get_trade_history(self, symbol=None, limit=500):
        """Get trade history with improved filtering"""
        try:
            params = {'limit': limit, 'recvWindow': self.recv_window}
            if symbol:
                params['symbol'] = symbol

            if self.trading_mode == 'futures':
                trades = await self._make_api_call(
                    self.client.futures_account_trades,
                    **params
                )
            else:
                trades = await self._make_api_call(
                    self.client.get_my_trades,
                    **params
                )

            # Format trade data consistently
            formatted_trades = []
            for trade in trades:
                formatted_trade = {
                    'symbol': trade['symbol'],
                    'id': trade['id'],
                    'orderId': trade['orderId'],
                    'price': float(trade['price']),
                    'quantity': float(trade['qty']),
                    'commission': float(trade['commission']),
                    'commissionAsset': trade['commissionAsset'],
                    'time': datetime.fromtimestamp(trade['time']/1000, tz=timezone.utc),
                    'isBuyer': trade['isBuyer'],
                    'isMaker': trade['isMaker']
                }
                formatted_trades.append(formatted_trade)

            return formatted_trades

        except Exception as e:
            self.logger.error(f"Error getting trade history: {e}")
            return None

    async def get_all_orders(self, symbol, limit=500):
        """Get all orders with retry logic"""
        try:
            params = {
                'symbol': symbol,
                'limit': limit,
                'recvWindow': self.recv_window
            }

            if self.trading_mode == 'futures':
                orders = await self._make_api_call(
                    self.client.futures_get_all_orders,
                    **params
                )
            else:
                orders = await self._make_api_call(
                    self.client.get_all_orders,
                    **params
                )

            return orders

        except Exception as e:
            self.logger.error(f"Error getting all orders: {e}")
            return None

    async def get_klines(self, symbol, interval, limit=500, start_time=None, end_time=None):
        """Get klines/candlestick data"""
        try:
            params = {
                'symbol': symbol,
                'interval': interval,
                'limit': limit
            }

            if start_time:
                params['startTime'] = int(start_time.timestamp() * 1000)
            if end_time:
                params['endTime'] = int(end_time.timestamp() * 1000)

            if self.trading_mode == 'futures':
                klines = await self._make_api_call(
                    self.client.futures_klines,
                    **params
                )
            else:
                klines = await self._make_api_call(
                    self.client.get_klines,
                    **params
                )

            return klines

        except Exception as e:
            self.logger.error(f"Error getting klines: {e}")
            return None

    async def _validate_order_quantity(self, symbol_info, quantity, price=None):
        """Validate order quantity against all filters"""
        try:
            filters = symbol_info['filters']
            
            # LOT_SIZE filter
            lot_filter = next(f for f in filters if f['filterType'] == 'LOT_SIZE')
            step_size = float(lot_filter['stepSize'])
            min_qty = float(lot_filter['minQty'])
            max_qty = float(lot_filter['maxQty'])
            
            # MARKET_LOT_SIZE filter (if exists)
            market_lot_filter = next((f for f in filters if f['filterType'] == 'MARKET_LOT_SIZE'), None)
            if market_lot_filter:
                min_qty = max(min_qty, float(market_lot_filter['minQty']))
                max_qty = min(max_qty, float(market_lot_filter['maxQty']))
            
            # Check quantity bounds
            if quantity < min_qty or quantity > max_qty:
                raise ValueError(f"Quantity {quantity} outside bounds [{min_qty}, {max_qty}]")
            
            # Round to step size
            quantity = float(round(quantity - (quantity % float(step_size)), 8))
            
            # MIN_NOTIONAL check
            min_notional_filter = next(f for f in filters if f['filterType'] == 'MIN_NOTIONAL')
            min_notional = float(min_notional_filter['minNotional'])
            
            if price:
                notional = quantity * price
            else:
                # Get current price for market orders
                ticker = await self.get_symbol_ticker(symbol_info['symbol'])
                notional = quantity * float(ticker['price'])
                
            if notional < min_notional:
                raise ValueError(f"Order value {notional} below min notional {min_notional}")
                
            return quantity
            
        except Exception as e:
            self.logger.error(f"Order validation error: {e}")
            raise

    async def _format_futures_order(self, symbol_info, quantity, price=None):
        """Format futures order with proper precision"""
        try:
            filters = symbol_info['filters']
            
            # Get price precision
            price_filter = next(f for f in filters if f['filterType'] == 'PRICE_FILTER')
            tick_size = float(price_filter['tickSize'])
            price_precision = len(str(tick_size).rstrip('0').split('.')[-1])
            
            # Get quantity precision
            qty_filter = next(f for f in filters if f['filterType'] == 'LOT_SIZE')
            step_size = float(qty_filter['stepSize'])
            qty_precision = len(str(step_size).rstrip('0').split('.')[-1])
            
            # Round quantity
            quantity = float(round(quantity, qty_precision))
            
            # Round price if provided
            if price:
                price = float(round(price, price_precision))
                
            return quantity, price
            
        except Exception as e:
            self.logger.error(f"Error formatting futures order: {e}")
            raise

    async def _recover_order_state(self, order_id, symbol):
        """Recover order state with retries and verification"""
        max_retries = 3
        base_delay = 1
        
        for attempt in range(max_retries):
            try:
                # Get order status
                order = await self.get_order_status(symbol, order_id)
                if not order:
                    await asyncio.sleep(base_delay * (attempt + 1))
                    continue
                    
                # Verify order state
                if order['status'] in ['FILLED', 'PARTIALLY_FILLED']:
                    fills = []
                    avg_price = 0
                    total_qty = 0
                    
                    # Get trade details
                    trades = await self._make_api_call(
                        self.client.get_my_trades,
                        symbol=symbol,
                        orderId=order_id
                    )
                    
                    for trade in trades:
                        qty = float(trade['qty'])
                        price = float(trade['price'])
                        total_qty += qty
                        avg_price += price * qty
                        fills.append({
                            'price': price,
                            'quantity': qty,
                            'commission': float(trade['commission']),
                            'commissionAsset': trade['commissionAsset']
                        })
                    
                    if total_qty > 0:
                        avg_price /= total_qty
                        
                    return {
                        'status': order['status'],
                        'fills': fills,
                        'avgPrice': avg_price,
                        'executedQty': total_qty
                    }
                    
                return {'status': order['status']}
                
            except Exception as e:
                self.logger.error(f"Recovery attempt {attempt + 1} failed: {e}")
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(base_delay * (attempt + 1))

    async def cleanup_stale_orders(self, max_age_hours=24):
        """Clean up stale orders"""
        try:
            cutoff_time = int((time.time() - (max_age_hours * 3600)) * 1000)
            
            for symbol in self.symbol_info_cache:
                try:
                    orders = await self.get_open_orders(symbol)
                    for order in orders:
                        if int(order['time']) < cutoff_time:
                            self.logger.warning(f"Found stale order: {order['orderId']}")
                            await self.cancel_order(symbol, order['orderId'])
                            
                except Exception as e:
                    self.logger.error(f"Error cleaning orders for {symbol}: {e}")
                    
        except Exception as e:
            self.logger.error(f"Error in cleanup_stale_orders: {e}")

    async def _sync_time(self):
        """Enhanced time synchronization with multiple attempts"""
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                # Get server time directly without going through _make_api_call
                await self.rate_limiter.acquire()
                server_time = self.client.get_server_time()
                local_time = int(time.time() * 1000)
                
                # Calculate offset and verify it's reasonable
                new_offset = server_time['serverTime'] - local_time
                if abs(new_offset) > 1000:  # If offset is more than 1 second
                    self.logger.warning(f"Large time offset detected: {new_offset}ms")
                
                # Update stored values
                self.last_server_time = server_time['serverTime']
                self.time_offset = new_offset
                
                self.logger.info(f"Time synchronized. Offset: {self.time_offset}ms")
                return True

            except Exception as e:
                self.logger.error(f"Time sync attempt {attempt + 1} failed: {e}")
                if attempt == max_attempts - 1:
                    raise
                await asyncio.sleep(1)
        return False
