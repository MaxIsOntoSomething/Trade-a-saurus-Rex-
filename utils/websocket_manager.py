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

        # Add resync settings
        self.last_sync_time = 0
        self.sync_interval = 1800  # Sync every 30 minutes
        self.client = client
        self._ensure_time_sync()

        # Add connection state tracking
        self.connection_state = {
            'ws': None,
            'is_connected': False,
            'last_pong': time.time(),
            'last_sync': 0
        }
        self.ws_logger = logging.getLogger('WebSocket')

    def _ensure_time_sync(self):
        """Ensure time is synced with Binance server"""
        current_time = time.time()
        if current_time - self.last_sync_time > self.sync_interval:
            try:
                server_time = self.client.get_server_time()
                self.time_offset = server_time['serverTime'] - int(current_time * 1000)
                self.last_sync_time = current_time
                self.logger.debug(f"Time synced with server. Offset: {self.time_offset}ms")
            except Exception as e:
                self.logger.error(f"Failed to sync time: {e}")

    def add_callback(self, callback):
        """Add callback function to be called when price updates are received"""
        self.callbacks.append(callback)

    async def _ensure_connection(self):
        """Verify connection is alive and websocket is open"""
        if (not self.connection_state['is_connected'] or 
            not self.connection_state['ws'] or 
            not self.connection_state['ws'].open):
            await self._reconnect()
            return False
        return True

    async def _reconnect(self):
        """Handle reconnection with proper cleanup"""
        self.connection_state['is_connected'] = False
        if self.connection_state['ws']:
            try:
                await self.connection_state['ws'].close()
            except Exception:
                pass
        self.connection_state['ws'] = None
        
        # Reset timestamp sync
        await self._sync_time()
        
        # Trigger reconnection
        self.connection_attempts += 1
        await self.start()

    async def _sync_time(self):
        """Synchronize time with Binance server"""
        try:
            # Call server time directly without _make_api_call to avoid recursion
            server_time = self.client.get_server_time()
            self.time_offset = server_time['serverTime'] - int(time.time() * 1000)
            self.connection_state['last_sync'] = time.time()
            self.logger.debug(f"Time synchronized. Offset: {self.time_offset}ms")
        except Exception as e:
            self.logger.error(f"Time sync failed: {e}")

    async def _make_api_call(self, func, *args, **kwargs):
        """Make API call with timestamp"""
        # Don't sync time here to avoid recursion with _sync_time
        if 'timestamp' in kwargs or func.__name__ not in ['get_server_time']:
            kwargs['timestamp'] = int(time.time() * 1000) + self.time_offset
        
        return func(*args, **kwargs)

    async def start(self):
        """Start WebSocket connection with improved state management and logging"""
        while True:
            try:
                # Ensure time sync before connection
                await self._sync_time()
                
                endpoint = self.backup_endpoints[self.connection_attempts % len(self.backup_endpoints)]
                delay = min(300, (2 ** self.connection_attempts)) * (0.8 + 0.4 * random.random())
                
                if self.connection_attempts > 0:
                    self.logger.info(f"Reconnecting in {delay:.1f}s using {endpoint}")
                    await asyncio.sleep(delay)

                # Log connection attempt
                self.logger.info(f"Connecting to WebSocket endpoint: {endpoint}")

                # Create WebSocket connection with shorter timeouts
                websocket = await websockets.connect(
                    endpoint,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=10,
                    compression=None,
                    ssl=True  # Explicitly enable SSL
                )

                # Update connection state
                self.connection_state.update({
                    'ws': websocket,
                    'is_connected': True,
                    'last_pong': time.time()
                })
                
                self.logger.info("WebSocket connection established successfully")
                
                # Update class-level ws reference
                self.ws = websocket
                self.connection_attempts = 0
                
                # Start keepalive task
                if self.keepalive_task and not self.keepalive_task.done():
                    self.keepalive_task.cancel()
                self.keepalive_task = asyncio.create_task(self._keepalive_loop())
                
                # Subscribe to streams
                await self._subscribe_to_streams()
                
                # Initial prices update
                await self._send_initial_prices()
                
                # Main message loop
                try:
                    while True:
                        try:
                            message = await asyncio.wait_for(
                                websocket.recv(),
                                timeout=30
                            )
                            self.connection_state['last_pong'] = time.time()
                            
                            # Log successful message receipt
                            self.ws_logger.debug(
                                "WebSocket message received",
                                extra={'duration': 0, 'message_type': 'recv'}
                            )
                            
                            await self._handle_socket_message(json.loads(message))
                            
                        except asyncio.TimeoutError:
                            self.logger.debug("WebSocket message timeout, sending ping")
                            try:
                                await websocket.ping()
                                continue
                            except:
                                break
                        except websockets.ConnectionClosed as e:
                            self.logger.warning(f"WebSocket closed: {e.code} {e.reason}")
                            break
                            
                finally:
                    if self.keepalive_task and not self.keepalive_task.done():
                        self.keepalive_task.cancel()
                    
            except Exception as e:
                self.connection_state['is_connected'] = False
                self.logger.error(f"WebSocket error: {e}")
                
                self.ws = None
                self.connection_state['ws'] = None
                
                self.connection_attempts += 1
                
                if self.connection_attempts >= self.max_reconnect_attempts:
                    self.logger.error("Max reconnection attempts reached")
                    await asyncio.sleep(300)
                    self.connection_attempts = 0

    async def _heartbeat_loop(self):
        """Send periodic heartbeats to keep connection alive"""
        while self.connection_state['is_connected']:
            try:
                if self.connection_state['ws'] and not self.connection_state['ws'].closed:
                    await self.connection_state['ws'].ping()
                    self.logger.debug("Heartbeat ping sent")
                await asyncio.sleep(15)  # Send heartbeat every 15 seconds
            except Exception as e:
                self.logger.error(f"Heartbeat error: {e}")
                break

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
        """Handle incoming WebSocket messages with enhanced logging"""
        try:
            start_time = time.time()
            
            # Log incoming message with simplified format
            self.ws_logger.debug(
                "WebSocket message received",
                extra={
                    'details': f"Message: {self._sanitize_ws_message(msg)}"
                }
            )
            
            if 'e' not in msg:
                return
                
            # Process message and log duration
            try:
                await self._process_socket_message(msg)
                duration = (time.time() - start_time) * 1000
                self.ws_logger.debug(
                    "WebSocket message processed",
                    extra={
                        'details': (
                            f"Event: {msg.get('e')}\n"
                            f"Symbol: {msg.get('s')}\n"
                            f"Duration: {duration:.2f}ms"
                        )
                    }
                )
                
            except Exception as e:
                duration = (time.time() - start_time) * 1000
                self.ws_logger.error(
                    "WebSocket message processing failed",
                    extra={
                        'details': (
                            f"Error: {type(e).__name__}: {str(e)}\n"
                            f"Event: {msg.get('e')}\n"
                            f"Symbol: {msg.get('s')}\n"
                            f"Duration: {duration:.2f}ms"
                        )
                    }
                )
                raise
                
        except Exception as e:
            self.logger.error(f"Error in message handler: {e}")

    def _sanitize_ws_message(self, msg):
        """Sanitize WebSocket message for logging"""
        if isinstance(msg, dict):
            return {
                k: v for k, v in msg.items()
                if not any(sensitive in k.lower() for sensitive in ['key', 'secret', 'token', 'password'])
            }
        return msg

    async def _process_socket_message(self, msg):
        """Process WebSocket message"""
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
            # Set connection state first to stop loops
            self.connection_state['is_connected'] = False
            self.is_connected = False

            # Cancel keepalive task
            if self.keepalive_task and not self.keepalive_task.done():
                self.keepalive_task.cancel()
                try:
                    await self.keepalive_task
                except asyncio.CancelledError:
                    pass

            # Close WebSocket connection
            if self.connection_state['ws']:
                ws = self.connection_state['ws']
                try:
                    if not ws.closed:
                        await ws.close()
                except Exception as e:
                    self.logger.warning(f"Error closing WebSocket: {e}")
                finally:
                    self.connection_state['ws'] = None
                    self.ws = None

            # Clear state
            self.callbacks = []
            self.last_prices = {}
            self.initial_prices_sent = False
            
            self.logger.info("WebSocket connection closed and cleaned up")

        except Exception as e:
            self.logger.error(f"Error stopping WebSocket: {e}")

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
        """Improved keepalive with proper state checking"""
        while self.connection_state['is_connected']:
            try:
                current_time = time.time()
                
                # Check connection health
                if current_time - self.connection_state['last_pong'] > self.websocket_timeout:
                    self.logger.warning("Keepalive timeout detected")
                    await self._reconnect()
                    break
                
                # Check websocket state properly
                ws = self.connection_state['ws']
                if ws:
                    try:
                        # Use ping to check connection
                        pong_waiter = await ws.ping()
                        await asyncio.wait_for(pong_waiter, timeout=self.ping_timeout)
                        self.connection_state['last_pong'] = time.time()
                    except Exception:
                        self.logger.warning("WebSocket ping failed")
                        await self._reconnect()
                        break
                        
                await asyncio.sleep(self.ping_interval)
                
            except Exception as e:
                self.logger.error(f"Keepalive error: {e}")
                await self._reconnect()
                break

    async def _subscribe_to_streams(self):
        """Subscribe to price streams"""
        try:
            # Use connection_state['ws'] instead of self.ws
            if not self.connection_state['ws']:
                self.logger.error("WebSocket not initialized")
                return
                
            streams = [f"{symbol.lower()}@ticker" for symbol in self.symbols]
            subscribe_msg = {
                "method": "SUBSCRIBE",
                "params": streams,
                "id": 1
            }
            
            await self.connection_state['ws'].send(json.dumps(subscribe_msg))
            self.logger.info(f"Subscribed to {len(streams)} streams")
            
        except Exception as e:
            self.logger.error(f"Error subscribing to streams: {e}")
            # Trigger reconnection on subscription error
            await self._reconnect()

    async def _handle_connection_error(self):
        """Handle connection errors with exponential backoff"""
        self.connection_attempts += 1
        if self.connection_attempts >= self.max_reconnect_attempts:
            self.logger.error("Max reconnection attempts reached")
            await asyncio.sleep(300)  # 5 minute cooldown
            self.connection_attempts = 0
        await self._sync_time()  # Ensure time is synced after error

