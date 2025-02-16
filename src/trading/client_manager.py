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

    async def validate_mode_switch(self, new_mode: str) -> tuple[bool, str]:
        """Validate if mode switch is possible"""
        try:
            if new_mode == 'futures':
                # Check if futures trading is enabled
                if not self.config['trading'].get('enable_futures', False):
                    return False, "Futures trading is not enabled in configuration"

                # Check for required futures settings
                if 'futures_settings' not in self.config['trading']:
                    return False, "Missing futures configuration settings"

                # Verify futures API keys are set
                if self.testnet:
                    if not all([self.config['binance']['testnet_futures'].get(k) 
                              for k in ['api_key', 'api_secret']]):
                        return False, "Missing futures testnet API credentials"
                else:
                    if not all([self.config['binance']['mainnet'].get(k) 
                              for k in ['futures_api_key', 'futures_api_secret']]):
                        return False, "Missing futures mainnet API credentials"

            # Check balance requirements
            if not await self.validate_balance_requirements(new_mode):
                return False, "Insufficient balance for mode switch"

            # Check for open orders
            open_orders = await self.get_open_orders()
            if open_orders:
                return False, f"Found {len(open_orders)} open orders. Cancel them first."

            return True, "Mode switch validated"

        except Exception as e:
            logger.error(f"Error validating mode switch: {e}")
            return False, f"Validation error: {str(e)}"

    async def validate_balance_requirements(self, new_mode: str) -> bool:
        """Check if balance meets requirements for mode switch"""
        try:
            current_balance = await self.active_client.get_balance()
            
            if new_mode == 'futures':
                # Check minimum futures balance requirement
                min_futures_balance = self.config['trading'].get('min_futures_balance', 10)
                if float(current_balance) < min_futures_balance:
                    return False
            
            # Check reserve balance requirement
            if float(current_balance) < self.reserve_balance:
                return False
                
            return True
            
        except Exception as e:
            logger.error(f"Error checking balance requirements: {e}")
            return False

    async def prepare_mode_switch(self, new_mode: str) -> bool:
        """Prepare for mode switch by cleaning up current mode"""
        try:
            # Cancel all open orders
            await self.active_client.cancel_all_orders()
            
            # Close all positions if switching from futures
            if self.trading_mode == 'futures':
                await self.active_client.close_all_positions()
            
            # Clear cached data
            self.active_client.clear_cache()
            
            return True
            
        except Exception as e:
            logger.error(f"Error preparing for mode switch: {e}")
            return False

    async def switch_mode(self, new_mode: str) -> bool:
        """Switch trading mode with validation"""
        try:
            # Validate mode switch
            valid, message = await self.validate_mode_switch(new_mode)
            if not valid:
                logger.error(f"Mode switch validation failed: {message}")
                return False

            # Prepare for switch
            if not await self.prepare_mode_switch(new_mode):
                return False

            # Update mode
            self.trading_mode = new_mode
            
            # Initialize new client
            await self.initialize()
            
            logger.info(f"Successfully switched to {new_mode} mode")
            return True
            
        except Exception as e:
            logger.error(f"Error switching modes: {e}")
            return False
