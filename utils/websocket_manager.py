from binance.client import Client
import asyncio
import websockets
import json
from datetime import datetime
from colorama import Fore
import logging

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

    def add_callback(self, callback):
        """Add callback function to be called when price updates are received"""
        self.callbacks.append(callback)

    async def start(self):
        """Start WebSocket connection"""
        try:
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

    async def _handle_socket_message(self, msg):
        """Handle incoming WebSocket messages"""
        try:
            if 'e' not in msg or msg['e'] != '24hrTicker':
                return

            symbol = msg['s']  # Symbol
            price = float(msg['c'])  # Current price
            price_change = float(msg['P'])  # 24h price change percent
            high = float(msg['h'])  # 24h high
            low = float(msg['l'])  # 24h low

            # Store the last price
            self.last_prices[symbol] = {
                'price': price,
                'change': price_change,
                'high': high,
                'low': low,
                'timestamp': datetime.now()
            }

            # Call all registered callbacks with the update
            for callback in self.callbacks:
                asyncio.create_task(callback(symbol, self.last_prices[symbol]))

        except Exception as e:
            self.logger.error(f"Error processing WebSocket message: {e}")

    async def _handle_reconnection(self):
        """Handle WebSocket reconnection with exponential backoff"""
        while not self.is_connected:
            try:
                print(f"{Fore.YELLOW}Attempting to reconnect in {self.reconnect_delay} seconds...")
                await asyncio.sleep(self.reconnect_delay)
                await self.start()
                
                if self.is_connected:
                    print(f"{Fore.GREEN}Successfully reconnected to WebSocket")
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
