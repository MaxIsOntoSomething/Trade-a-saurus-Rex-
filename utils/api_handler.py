import logging
import asyncio
from datetime import datetime, timezone
import time
from binance.client import Client
from utils.rate_limiter import RateLimiter

class APIHandler:
    def __init__(self, client, symbols, logger=None):
        self.client = client
        self.symbols = symbols
        self.logger = logger or logging.getLogger(__name__)
        self.last_prices = {}
        self.callbacks = []
        self.is_running = False
        self.update_interval = 2  # Update every 2 seconds
        self.update_task = None
        self.rate_limiter = RateLimiter(max_requests=1200)  # Binance limit
        self.price_cache = {}
        self.cache_duration = 1  # Cache duration in seconds
        self.recv_window = 5000
        self.time_offset = 0
        self.last_sync = 0

        # Update no_timestamp_methods to include all read-only endpoints
        self.no_timestamp_methods = {
            'get_symbol_ticker',
            'get_exchange_info',  # This needs to always be no timestamp
            'get_symbol_info',
            'get_server_time',
            'get_historical_klines',
            'get_ticker',
            'get_klines',
            'get_depth',
            'get_aggregate_trades'
        }

    def add_callback(self, callback):
        """Add callback function"""
        self.callbacks.append(callback)

    async def start(self):
        """Start price updates"""
        self.is_running = True
        self.update_task = asyncio.create_task(self._price_update_loop())
        self.logger.info("Price update loop started")

    async def stop(self):
        """Stop price updates"""
        self.is_running = False
        if self.update_task:
            self.update_task.cancel()
            try:
                await self.update_task
            except asyncio.CancelledError:
                pass
        self.logger.info("Price update loop stopped")

    async def _make_api_call(self, func, *args, _no_timestamp=False, **kwargs):
        """Make API call with rate limiting and visual feedback"""
        await self.rate_limiter.acquire()
        
        # Get endpoint name from function
        endpoint = func.__name__.replace('get_', '').replace('_', ' ').title()
        
        try:
            # Special handling for Testnet endpoints
            is_testnet = hasattr(self.client, 'API_URL') and 'testnet' in self.client.API_URL
            
            # Remove timestamp for certain endpoints
            if func.__name__ in self.no_timestamp_methods or _no_timestamp:
                kwargs.pop('timestamp', None)
                
            # Special handling for exchange info in Testnet
            if is_testnet and func.__name__ in ['get_exchange_info', 'get_symbol_info']:
                try:
                    if func.__name__ == 'get_exchange_info':
                        return self.client.get_exchange_info()
                    elif func.__name__ == 'get_symbol_info':
                        exchange_info = self.client.get_exchange_info()
                        symbol = kwargs.get('symbol')
                        return next((s for s in exchange_info['symbols'] if s['symbol'] == symbol), None)
                except Exception as e:
                    print(f"\r❌ {endpoint} failed in Testnet: {str(e)}", end='', flush=True)
                    raise
            
            print(f"\r📡 {endpoint}: Calling API...", end='', flush=True)
            response = func(*args, **kwargs)
            print(f"\r✅ {endpoint}: Success", end='', flush=True)
            
            return response
            
        except Exception as e:
            print(f"\r❌ {endpoint}: Failed - {str(e)}", end='', flush=True)
            raise

    async def _price_update_loop(self):
        """Main price update loop with visual feedback"""
        cycle = 0
        spinner = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
        
        while self.is_running:
            try:
                cycle += 1
                spin = spinner[cycle % len(spinner)]
                print(f"\r{spin} Monitoring {len(self.symbols)} pairs... ", end='', flush=True)
                
                for symbol in self.symbols:
                    try:
                        # Get current price with visual feedback
                        print(f"\r{spin} Fetching {symbol}... ", end='', flush=True)
                        
                        # Get current price
                        ticker = await self._make_api_call(
                            self.client.get_symbol_ticker,
                            symbol=symbol,
                            _no_timestamp=True
                        )
                        stats_24h = await self._make_api_call(
                            self.client.get_ticker,
                            symbol=symbol,
                            _no_timestamp=True
                        )
                        
                        current_price = float(ticker['price'])
                        price_change = float(stats_24h['priceChangePercent'])

                        # Update last prices
                        self.last_prices[symbol] = {
                            'price': current_price,
                            'change': price_change,
                            'timestamp': datetime.now(timezone.utc)
                        }

                        # Call callbacks
                        for callback in self.callbacks:
                            try:
                                await callback(symbol, current_price)
                            except Exception as e:
                                self.logger.error(f"Callback error for {symbol}: {e}")

                        print(f"\r{spin} {symbol}: {current_price:.8f} USDT ({price_change:+.2f}%) ", end='', flush=True)
                        await asyncio.sleep(0.1)  # Small delay for visual effect

                    except Exception as e:
                        self.logger.error(f"Error updating price for {symbol}: {e}")
                        continue

                print(f"\r{spin} Cycle complete. Waiting {self.update_interval}s...", end='', flush=True)
                await asyncio.sleep(self.update_interval)

            except Exception as e:
                self.logger.error(f"Error in price update loop: {e}")
                print(f"\r❌ Update loop error: {e}")
                await asyncio.sleep(5)  # Wait before retrying

    def get_last_price(self, symbol):
        """Get the last known price for a symbol"""
        return self.last_prices.get(symbol)

    async def get_cached_price(self, symbol):
        """Get cached price or fetch new one"""
        current_time = time.time()
        
        if (symbol in self.price_cache and 
            current_time - self.price_cache[symbol]['timestamp'] < self.cache_duration):
            return self.price_cache[symbol]['price']
        
        try:
            ticker = await self._make_api_call(
                self.client.get_symbol_ticker,
                symbol=symbol,
                _no_timestamp=True
            )
            price = float(ticker['price'])
            
            self.price_cache[symbol] = {
                'price': price,
                'timestamp': current_time
            }
            
            return price
            
        except Exception as e:
            self.logger.error(f"Error fetching price for {symbol}: {e}")
            return None
