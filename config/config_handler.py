import os
import json
from pathlib import Path
from typing import Dict, Any
import re

class ConfigHandler:
    @staticmethod
    def is_valid_token(token: str) -> bool:
        """Validate Telegram bot token format"""
        if not isinstance(token, str) or not token:
            return False
        # Token format: <bot_id>:<hex_string>
        pattern = r'^\d+:[A-Za-z0-9_-]{35}$'
        return bool(re.match(pattern, token.strip()))

    @staticmethod
    def validate_config(config: Dict[str, Any]) -> None:
        """Validate critical configuration settings"""
        # First check if Telegram settings exist
        telegram_token = config.get('TELEGRAM_TOKEN', '')
        telegram_chat_id = config.get('TELEGRAM_CHAT_ID', '')

        # Default to False if settings are invalid
        telegram_enabled = False
        
        # Only validate if both token and chat_id are present and not placeholder values
        if (telegram_token and telegram_chat_id and 
            telegram_token not in ['YOUR_TELEGRAM_BOT_TOKEN', 'your_telegram_token'] and
            telegram_chat_id not in ['YOUR_TELEGRAM_CHAT_ID', 'your_chat_id']):
            
            if ConfigHandler.is_valid_token(telegram_token) and str(telegram_chat_id).lstrip('-').isdigit():
                telegram_enabled = True
                print(f"Telegram configuration validated successfully")
            else:
                print(f"Warning: Invalid Telegram configuration")

        # Set the final Telegram state
        config['USE_TELEGRAM'] = telegram_enabled

    @staticmethod
    def load_config(use_env: bool = False) -> Dict[str, Any]:
        """Load configuration based on environment"""
        config = {}
        
        if use_env and os.path.exists('.env'):
            # Docker environment - use .env
            config = ConfigHandler._load_from_env()
            print("Using Docker configuration from .env")
        else:
            # Manual run - use config.json
            config_path = Path('config/config.json')
            if config_path.exists():
                with open(config_path) as f:
                    config = json.load(f)
                print("Using configuration from config.json")
                
                # Convert TRADING_SYMBOLS from list to required format if needed
                if isinstance(config.get('TRADING_SYMBOLS'), list):
                    config['TRADING_SYMBOLS'] = [str(symbol) for symbol in config['TRADING_SYMBOLS']]
                
                # Ensure timeframe configuration exists
                if 'TIMEFRAMES' in config:
                    config['timeframe_config'] = config['TIMEFRAMES']
            else:
                raise FileNotFoundError("config.json not found")
        
        # Validate the configuration
        ConfigHandler.validate_config(config)
        return config

    @staticmethod
    def _load_from_env() -> Dict[str, Any]:
        """Load configuration from environment variables"""
        config = {
            'BINANCE_API_KEY': os.getenv('BINANCE_API_KEY'),
            'BINANCE_API_SECRET': os.getenv('BINANCE_API_SECRET'),
            'TESTNET_API_KEY': os.getenv('TESTNET_API_KEY'),
            'TESTNET_API_SECRET': os.getenv('TESTNET_API_SECRET'),
            'TELEGRAM_TOKEN': os.getenv('TELEGRAM_TOKEN'),
            'TELEGRAM_CHAT_ID': os.getenv('TELEGRAM_CHAT_ID'),
            'TRADING_SYMBOLS': os.getenv('TRADING_SYMBOLS', '').split(','),
            'USE_TESTNET': os.getenv('USE_TESTNET', 'true').lower() == 'true',
            'USE_TELEGRAM': os.getenv('USE_TELEGRAM', 'true').lower() == 'true',
            'ORDER_TYPE': os.getenv('ORDER_TYPE', 'limit'),
            'USE_PERCENTAGE': os.getenv('USE_PERCENTAGE', 'false').lower() == 'true',
            'TRADE_AMOUNT': float(os.getenv('TRADE_AMOUNT', '10')),
            'RESERVE_BALANCE': float(os.getenv('RESERVE_BALANCE', '2000')),
            'TIME_INTERVAL': os.getenv('TIME_INTERVAL', '1d'),  # Added default time interval
        }

        # Parse timeframe configurations
        config['TIMEFRAMES'] = ConfigHandler._parse_timeframe_config()
        
        return config

    @staticmethod
    def _parse_timeframe_config() -> Dict[str, Dict]:
        """Parse timeframe configuration from environment variables"""
        timeframes = {}
        
        # Parse daily config
        daily = os.getenv('DAILY_CONFIG', 'true:1,2,3').split(':')
        timeframes['daily'] = {
            'enabled': daily[0].lower() == 'true',
            'thresholds': [float(x)/100 for x in daily[1].split(',')]
        }
        
        # Parse weekly config
        weekly = os.getenv('WEEKLY_CONFIG', 'true:3,6,10').split(':')
        timeframes['weekly'] = {
            'enabled': weekly[0].lower() == 'true',
            'thresholds': [float(x)/100 for x in weekly[1].split(',')]
        }
        
        # Parse monthly config
        monthly = os.getenv('MONTHLY_CONFIG', 'true:5,10').split(':')
        timeframes['monthly'] = {
            'enabled': monthly[0].lower() == 'true',
            'thresholds': [float(x)/100 for x in monthly[1].split(',')]
        }
        
        return timeframes

    @staticmethod
    def _load_from_json() -> Dict[str, Any]:
        """Load configuration from JSON file"""
        config_path = Path('config/config.json')
        with open(config_path) as f:
            return json.load(f)

    @staticmethod
    def get_data_dir() -> Path:
        """Get platform-specific data directory"""
        return Path('data')

    @staticmethod
    def get_logs_dir() -> Path:
        """Get platform-specific logs directory"""
        return Path('logs')
