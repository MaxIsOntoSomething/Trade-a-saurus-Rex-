from binance.client import Client
import asyncio
import websockets
import json
from datetime import datetime
from colorama import Fore
import logging
import os

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

    def add_callback(self, callback):
        """Add callback function to be called when price updates are received"""
        self.callbacks.append(callback)

    async def start(self):
        """Start WebSocket connection"""
        try:
            # Get initial prices before starting WebSocket
            await self._send_initial_prices()
            
            # Start refresh timer
            asyncio.create_task(self._refresh_timer())
            
            # Determine WebSocket URL based on testnet or mainnet
            ws_url = "wss://testnet.binance.vision/ws" if self.client.API_URL == "https://testnet.binance.vision/api" else "wss://stream.binance.com:9443/ws"
            
            # Create subscription message for all symbols
            streams = [f"{symbol.lower()}@ticker" for symbol in self.symbols]
            subscribe_msg = {
                "method": "SUBSCRIBE",
                "params": streams,
                "id": 1
            }

            async with websockets.connect(ws_url) as websocket:
                self.ws = websocket
                self.is_connected = True
                print(f"{Fore.GREEN}WebSocket connection established")
                self.logger.info("WebSocket connection established")

                # Send subscription message
                await websocket.send(json.dumps(subscribe_msg))

                # Start listening for messages
                while True:
                    try:
                        message = await websocket.recv()
                        await self._handle_socket_message(json.loads(message))
                    except websockets.ConnectionClosed:
                        print(f"{Fore.YELLOW}WebSocket connection closed")
                        self.is_connected = False
                        await self._handle_reconnection()
                        break

        except Exception as e:
            self.logger.error(f"Error starting WebSocket: {e}")
            await self._handle_reconnection()

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
        """Stop WebSocket connection"""
        try:
            if self.ws:
                await self.ws.close()
                self.is_connected = False
                print(f"{Fore.YELLOW}WebSocket connection closed")
                self.logger.info("WebSocket connection closed")
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

