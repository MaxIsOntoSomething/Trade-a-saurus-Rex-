from binance.client import Client
import asyncio
import websockets
import json
from datetime import datetime
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

    def add_callback(self, callback):
        """Add callback function to be called when price updates are received"""
        self.callbacks.append(callback)

    async def start(self):
        """Start WebSocket connection with improved reconnection logic"""
        while True:
            try:
                # Rotate through backup endpoints
                endpoint = self.backup_endpoints[self.connection_attempts % len(self.backup_endpoints)]
                
                # Calculate backoff with jitter
                delay = min(300, (2 ** self.connection_attempts)) * (0.8 + 0.4 * random.random())
                
                if self.connection_attempts > 0:
                    print(f"Reconnecting in {delay:.1f} seconds using {endpoint}")
                    await asyncio.sleep(delay)

                async with websockets.connect(endpoint) as websocket:
                    self.ws = websocket
                    self.is_connected = True
                    self.connection_attempts = 0  # Reset on successful connection
                    self.last_pong = time.time()
                    
                    # Start ping/pong task
                    self.ping_task = asyncio.create_task(self._ping_loop())
                    
                    # Subscribe to streams
                    await self._subscribe_to_streams()
                    
                    while True:
                        try:
                            message = await websocket.recv()
                            # Reset pong timer on any message
                            self.last_pong = time.time()
                            await self._handle_socket_message(json.loads(message))
                        except websockets.ConnectionClosed:
                            raise
                        
            except Exception as e:
                self.is_connected = False
                self.logger.error(f"WebSocket error: {e}")
                if self.ping_task:
                    self.ping_task.cancel()
                self.connection_attempts += 1
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

                # Store the last price with reduced information
                self.last_prices[symbol] = {
                    'price': price,
                    'change': price_change,
                    'timestamp': datetime.now()
                }

                # Clear console for Windows
                os.system('cls' if os.name == 'nt' else 'clear')
                
                # Print header
                print(f"{Fore.CYAN}{'Symbol':<12} {'Price':<15} {'24h Change':<15}")
                print("-" * 42)  # Reduced line length

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

                # Call all registered callbacks with just the price value
                for callback in self.callbacks:
                    await callback(symbol, float(price))  # Make sure we send just the price float

        except Exception as e:
            self.logger.error(f"Error processing WebSocket message: {e}")
            print(f"{Fore.RED}Error processing WebSocket message: {e}")

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
            # Cancel ping task first
            if self.ping_task and not self.ping_task.done():
                self.ping_task.cancel()
                try:
                    await self.ping_task
                except asyncio.CancelledError:
                    pass

            # Close WebSocket connection
            if self.ws:
                await self.ws.close()
                self.ws = None
            
            self.is_connected = False
            print(f"{Fore.YELLOW}WebSocket connection closed")
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

    async def _subscribe_to_streams(self):
        """Subscribe to price streams"""
        streams = [f"{symbol.lower()}@ticker" for symbol in self.symbols]
        subscribe_msg = {
            "method": "SUBSCRIBE",
            "params": streams,
            "id": 1
        }
        await self.ws.send(json.dumps(subscribe_msg))

