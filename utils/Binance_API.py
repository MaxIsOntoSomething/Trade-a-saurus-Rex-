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
        """Create order based on API mode"""
        try:
            if self.api_mode == 'futures_testnet' or self.api_mode == 'futures':
                return await self._create_futures_order(symbol, side, quantity, price)
            else:
                return await self._create_spot_order(symbol, side, quantity, price)
        except Exception as e:
            self.logger.error(f"Error creating order: {e}")
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

    # Add other necessary methods like get_balance, cancel_order, etc.
    # ...existing code...
