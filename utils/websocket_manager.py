import sys  # Add this import at the top with other imports
from binance.client import Client
import asyncio
import websockets
import json
from datetime import datetime, timezone, timedelta
from colorama import Fore
import logging
import os
import random
import time

class WebSocketManager:
    def __init__(self, client, symbols, logger=None):
        self.client = client
        self.symbols = symbols
        self.logger = logger or logging.getLogger(__name__)
        self.ws = None
        self.last_prices = {}
        self.callbacks = []
        self.is_connected = False
        self.reconnect_delay = 5
        self.max_reconnect_delay = 300
        self.initial_prices_sent = False  # Add this flag
        self.last_refresh = datetime.now()
        self.refresh_interval = 60  # Refresh every 60 seconds
        self.connection_attempts = 0
        self.backup_endpoints = [
            "wss://stream.binance.com:9443/ws",
            "wss://stream.binance.com:443/ws",
            "wss://stream1.binance.com:9443/ws"
        ]
        self.ping_interval = 30
        self.last_pong = None
        self.ping_task = None

        # Add timeout settings
        self.websocket_timeout = int(os.getenv('WEBSOCKET_TIMEOUT', 60))
        self.retry_delay = int(os.getenv('WEBSOCKET_RETRY_DELAY', 5))
        self.max_reconnect_attempts = int(os.getenv('MAX_RECONNECT_ATTEMPTS', 10))
        self.last_pong = time.time()
        self.ping_interval = 30
        self.ping_timeout = 10
        self.ws = None
        self.keepalive_task = None
        self.display_lock = asyncio.Lock()
        self.last_display_update = 0
        self.display_update_interval = 1  # Update display every second
        self.next_reset_times = {
            'daily': None,
            'weekly': None,
            'monthly': None
        }  # Add this for countdown timers

    def add_callback(self, callback):
        """Add callback function to be called when price updates are received"""
        self.callbacks.append(callback)

    async def start(self):
        """Start WebSocket connection with improved error handling"""
        while True:
            try:
                # Select endpoint based on connection attempts
                endpoint = self.backup_endpoints[self.connection_attempts % len(self.backup_endpoints)]
                
                # Calculate backoff with jitter
                delay = min(300, (2 ** self.connection_attempts)) * (0.8 + 0.4 * random.random())
                
                if self.connection_attempts > 0:
                    print(f"Reconnecting in {delay:.1f} seconds using {endpoint}")
                    await asyncio.sleep(delay)

                async with websockets.connect(
                    endpoint,
                    ping_interval=self.ping_interval,
                    ping_timeout=self.ping_timeout,
                    close_timeout=self.websocket_timeout
                ) as websocket:
                    self.ws = websocket
                    self.is_connected = True
                    self.connection_attempts = 0
                    self.last_pong = time.time()
                    
                    # Start keepalive task
                    self.keepalive_task = asyncio.create_task(self._keepalive_loop())
                    
                    # Subscribe to streams
                    await self._subscribe_to_streams()
                    
                    while True:
                        try:
                            message = await asyncio.wait_for(
                                websocket.recv(),
                                timeout=self.websocket_timeout
                            )
                            self.last_pong = time.time()
                            await self._handle_socket_message(json.loads(message))
                        except asyncio.TimeoutError:
                            print(f"{Fore.YELLOW}WebSocket timeout, reconnecting...")
                            break
                        except websockets.ConnectionClosed as e:
                            print(f"{Fore.YELLOW}WebSocket closed: {e.code} {e.reason}")
                            break
                
            except Exception as e:
                self.is_connected = False
                self.logger.error(f"WebSocket error: {e}")
                
                if self.keepalive_task:
                    self.keepalive_task.cancel()
                    
                self.connection_attempts += 1
                if self.connection_attempts >= self.max_reconnect_attempts:
                    print(f"{Fore.RED}Max reconnection attempts reached. Waiting for manual intervention.")
                    await asyncio.sleep(300)  # Wait 5 minutes before trying again
                    self.connection_attempts = 0
                continue

    async def _send_initial_prices(self):
        """Send initial prices for all symbols"""
        try:
            for symbol in self.symbols:
                ticker = self.client.get_symbol_ticker(symbol=symbol)
                stats_24h = self.client.get_ticker(symbol=symbol)
                
                price = float(ticker['price'])
                
                # Call callbacks with just the price value
                for callback in self.callbacks:
                    await callback(symbol, price)  # Send only the price float

        except Exception as e:
            self.logger.error(f"Error getting initial prices: {e}")

    async def _handle_socket_message(self, msg):
        """Handle incoming WebSocket messages"""
        try:
            if 'e' not in msg:  # Handle initial connection message
                return
            
            if msg['e'] == '24hrTicker':
                symbol = msg['s']  # Symbol
                price = float(msg['c'])  # Current price as float
                price_change = float(msg['P'])  # 24h price change percent as float

                # Update last prices atomically
                self.last_prices[symbol] = {
                    'price': price,
                    'change': price_change,
                    'timestamp': datetime.now()
                }

                # Call callbacks first
                for callback in self.callbacks:
                    asyncio.create_task(callback(symbol, float(price)))

                # Update display with rate limiting
                current_time = time.time()
                if current_time - self.last_display_update >= self.display_update_interval:
                    async with self.display_lock:
                        self.last_display_update = current_time
                        await self._update_price_display()

        except Exception as e:
            self.logger.error(f"Error processing WebSocket message: {e}")
            print(f"{Fore.RED}Error processing WebSocket message: {e}")

    async def _update_price_display(self):
        """Update price display without blocking"""
        try:
            # Only clear console if we have new prices to show
            if not self.last_prices:
                return

            # Clear console and handle stdout flush properly
            os.system('cls' if os.name == 'nt' else 'clear')
            
            # Print header with current time
            current_time = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            print(f"\n{Fore.CYAN}Live Price Updates - {current_time} UTC")
            print(f"{Fore.CYAN}{'Symbol':<12} {'Price':<15} {'24h Change':<15}")
            print("-" * 42)

            # Print all prices with proper formatting
            for sym, data in sorted(self.last_prices.items()):
                try:
                    color = Fore.GREEN if data['change'] >= 0 else Fore.RED
                    arrow = "↑" if data['change'] >= 0 else "↓"
                    
                    # Format price based on value
                    price = data['price']
                    if price < 1:
                        price_str = f"{price:.8f}"
                    elif price < 100:
                        price_str = f"{price:.6f}"
                    else:
                        price_str = f"{price:.2f}"

                    print(f"{Fore.CYAN}{sym:<12} "
                          f"{color}{price_str:<15} "
                          f"{data['change']:+.2f}% {arrow}{Fore.RESET}")
                except (KeyError, ValueError):
                    continue

            print("-" * 42)
            
            # Add countdown timers
            await self._print_countdown_timers()
            
            # Force stdout flush
            sys.stdout.flush()

        except Exception as e:
            self.logger.error(f"Error updating price display: {e}")
            print(f"{Fore.RED}Error updating price display: {e}")

    async def _print_countdown_timers(self):
        """Print countdown timers asynchronously"""
        try:
            print(f"{Fore.CYAN}Next Resets:")
            now = datetime.now(timezone.utc)
            
            resets = {
                'daily': now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1),
                'weekly': (now + timedelta(days=(7 - now.weekday()))).replace(hour=0, minute=0, second=0, microsecond=0),
                'monthly': (now.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            }

            for timeframe, reset_time in sorted(resets.items()):
                if reset_time > now:
                    time_left = reset_time - now
                    hours = int(time_left.total_seconds() / 3600)
                    minutes = int((time_left.total_seconds() % 3600) / 60)
                    seconds = int(time_left.total_seconds() % 60)
                    print(f"{Fore.YELLOW}{timeframe.capitalize()}: {hours:02d}h {minutes:02d}m {seconds:02d}s")

        except Exception as e:
            self.logger.error(f"Error printing countdown timers: {e}")

    async def _handle_reconnection(self):
        """Handle WebSocket reconnection with exponential backoff"""
        while not self.is_connected:
            try:
                print(f"{Fore.YELLOW}Attempting to reconnect in {self.reconnect_delay} seconds...")
                await asyncio.sleep(self.reconnect_delay)
                
                # Verify all pending orders before reconnecting
                if hasattr(self, 'bot') and self.bot:
                    await self.bot.verify_pending_orders()
                
                await self.start()
                
                if self.is_connected:
                    print(f"{Fore.GREEN}Successfully reconnected to WebSocket")
                    # Force refresh all prices after reconnection
                    await self._force_refresh()
                    break
                    
                # Exponential backoff with maximum delay
                self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)
                
            except Exception as e:
                self.logger.error(f"Reconnection attempt failed: {e}")

    def get_last_price(self, symbol):
        """Get the last known price for a symbol"""
        return self.last_prices.get(symbol)

    async def stop(self):
        """Stop WebSocket connection with improved cleanup"""
        try:
            self.is_connected = False

            # Cancel keepalive task first
            if self.keepalive_task and not self.keepalive_task.done():
                self.keepalive_task.cancel()
                try:
                    await self.keepalive_task
                except asyncio.CancelledError:
                    pass

            # Close WebSocket connection
            if self.ws:
                try:
                    await self.ws.close()
                except Exception as e:
                    self.logger.warning(f"Error closing WebSocket: {e}")
                finally:
                    self.ws = None

            self.logger.info("WebSocket connection closed")

        except Exception as e:
            self.logger.error(f"Error stopping WebSocket: {e}")
        finally:
            # Ensure we clear all state
            self.callbacks = []
            self.last_prices = {}
            self.initial_prices_sent = False

    async def _refresh_timer(self):
        """Force refresh prices every 60 seconds"""
        while True:
            try:
                await asyncio.sleep(self.refresh_interval)
                await self._force_refresh()
            except Exception as e:
                self.logger.error(f"Error in refresh timer: {e}")
                await asyncio.sleep(5)  # Wait before retrying

    async def _force_refresh(self):
        """Force refresh all prices"""
        try:
            for symbol in self.symbols:
                ticker = self.client.get_symbol_ticker(symbol=symbol)
                stats_24h = self.client.get_ticker(symbol=symbol)
                
                price = float(ticker['price'])
                price_change = float(stats_24h['priceChangePercent'])

                self.last_prices[symbol] = {
                    'price': price,
                    'change': price_change,
                    'timestamp': datetime.now()
                }

            # Clear and redraw the display
            os.system('cls' if os.name == 'nt' else 'clear')
            
            # Print header
            print(f"{Fore.CYAN}{'Symbol':<12} {'Price':<15} {'24h Change':<15}")
            print("-" * 42)

            # Print all prices
            for sym, data in sorted(self.last_prices.items()):
                color = Fore.GREEN if data['change'] >= 0 else Fore.RED
                arrow = "↑" if data['change'] >= 0 else "↓"
                print(f"{Fore.CYAN}{sym:<12} "
                      f"{color}{data['price']:<15.8f} "
                      f"{data['change']:+.2f}% {arrow}{Fore.RESET}")

            # Add bottom separator and UTC clock
            print("-" * 42)
            print(f"{Fore.YELLOW}UTC: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")

            # Call callbacks with updates
            for symbol, data in self.last_prices.items():
                for callback in self.callbacks:
                    asyncio.create_task(callback(symbol, data['price']))

        except Exception as e:
            self.logger.error(f"Error in force refresh: {e}")

    async def _ping_loop(self):
        """Send periodic pings to keep connection alive"""
        while self.is_connected:
            try:
                if time.time() - self.last_pong > self.ping_interval * 2:
                    # Connection seems dead, force reconnect
                    self.ws.close()
                    break
                    
                await self.ws.ping()
                await asyncio.sleep(self.ping_interval)
            except Exception as e:
                self.logger.error(f"Ping error: {e}")
                break

    async def _keepalive_loop(self):
        """Maintain WebSocket connection with keepalive pings"""
        while self.is_connected:
            try:
                if time.time() - self.last_pong > self.websocket_timeout:
                    print(f"{Fore.YELLOW}Keepalive timeout detected, forcing reconnection...")
                    if self.ws:  # Only check if ws exists
                        try:
                            await self.ws.close(code=1012, reason="Keepalive timeout")
                        except:
                            pass  # Ignore errors during close
                    break
                
                if self.ws:  # Only attempt ping if ws exists
                    try:
                        await self.ws.ping()
                    except:
                        # If ping fails, force reconnect
                        self.is_connected = False
                        break
                
                await asyncio.sleep(self.ping_interval)
                
            except Exception as e:
                self.logger.error(f"Keepalive error: {e}")
                self.is_connected = False  # Force reconnect on error
                break

    async def _subscribe_to_streams(self):
        """Subscribe to price streams"""
        streams = [f"{symbol.lower()}@ticker" for symbol in self.symbols]
        subscribe_msg = {
            "method": "SUBSCRIBE",
            "params": streams,
            "id": 1
        }
        await self.ws.send(json.dumps(subscribe_msg))

