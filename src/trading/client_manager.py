from typing import Dict, Optional
from .binance_client import BinanceClient
from .futures_client import FuturesClient
import logging

logger = logging.getLogger(__name__)

class ClientManager:
    def __init__(self, config: dict):
        self.config = config
        self.trading_mode = config['environment']['trading_mode']
        self.testnet = config['environment']['testnet']
        self.active_client = None
        self.base_currency = config['trading']['base_currency']
        self.reserve_balance = config['trading'].get('reserve_balance', 0)
        self.client_type = self.trading_mode
        
        # Get correct API config
        if self.testnet:
            self.api_config = (
                config['binance']['testnet_futures']
                if self.trading_mode == 'futures'
                else config['binance']['testnet_spot']
            )
        else:
            self.api_config = config['binance']['mainnet']

    async def initialize(self) -> BinanceClient:
        """Initialize appropriate client based on configuration"""
        try:
            if self.trading_mode == 'futures':
                self.active_client = FuturesClient({
                    'api_key': self.api_config['api_key'],
                    'api_secret': self.api_config['api_secret'],
                    'testnet': self.testnet,
                    **self.config['trading'].get('futures_settings', {})
                })
            else:
                self.active_client = BinanceClient(
                    api_key=self.api_config['api_key'],
                    api_secret=self.api_config['api_secret'],
                    testnet=self.testnet,
                    base_currency=self.base_currency,  # Add these parameters
                    reserve_balance=self.reserve_balance
                )

            await self.active_client.initialize()
            logger.info(
                f"Initialized {self.trading_mode.upper()} client "
                f"on {'Testnet' if self.testnet else 'Mainnet'} "
                f"with reserve balance: ${self.reserve_balance:,.2f}"
            )
            return self.active_client

        except Exception as e:
            logger.error(f"Failed to initialize client manager: {e}")
            raise

    async def switch_client(self, client_type: str):
        """Switch to a different client type"""
        if client_type == self.client_type:
            return self.active_client

        try:
            # Close existing client if any
            if self.active_client:
                await self.active_client.cleanup()

            # Initialize and switch to new client
            return await self.initialize(client_type)

        except Exception as e:
            logger.error(f"Failed to switch client to {client_type}: {e}")
            raise

    async def cleanup(self):
        """Cleanup all clients"""
        clients = [
            self.spot_client,
            self.spot_testnet_client,
            self.futures_testnet_client
        ]
        
        for client in clients:
            if client:
                try:
                    await client.cleanup()
                except Exception as e:
                    logger.error(f"Error cleaning up client: {e}")

    def get_active_client(self):
        """Get currently active client"""
        return self.active_client

    def get_client_type(self):
        """Get current client type"""
        return self.client_type
