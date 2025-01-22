import os
import json
from pathlib import Path
from typing import Dict, Any
import re

class ConfigHandler:
    _config_cache = None  # Add cache for config
    _config_validated = False

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
        telegram_token = config.get('TELEGRAM_TOKEN', '')
        telegram_chat_id = config.get('TELEGRAM_CHAT_ID', '')
        
        telegram_enabled = False
        
        if telegram_token and telegram_chat_id and \
           telegram_token not in ['YOUR_TELEGRAM_BOT_TOKEN', 'your_telegram_token'] and \
           telegram_chat_id not in ['YOUR_TELEGRAM_CHAT_ID', 'your_chat_id']:
            
            if ConfigHandler.is_valid_token(telegram_token) and str(telegram_chat_id).lstrip('-').isdigit():
                telegram_enabled = True
                print("Telegram configuration validated successfully")
            else:
                print("Warning: Invalid Telegram configuration")
        
        config['USE_TELEGRAM'] = telegram_enabled

    @staticmethod
    def load_config(use_env: bool = False) -> Dict[str, Any]:
        """Load configuration based on environment"""
        try:
            # Return cached config if already validated
            if ConfigHandler._config_validated and ConfigHandler._config_cache:
                return ConfigHandler._config_cache

            # Always check for DOCKER env variable first
            is_docker = os.environ.get('DOCKER', '').lower() == 'true'
            
            # Force use_env if running in Docker
            if is_docker:
                use_env = True

            if use_env:
                config = ConfigHandler._load_from_env()
                print("Using configuration from environment variables")
            else:
                config_path = Path('config/config.json')
                if config_path.exists():
                    with open(config_path) as f:
                        config = json.load(f)
                    print("Using configuration from config.json")
                    
                    if isinstance(config.get('TRADING_SYMBOLS'), list):
                        config['TRADING_SYMBOLS'] = [str(symbol) for symbol in config['TRADING_SYMBOLS']]
                    
                    if 'TIMEFRAMES' in config:
                        config['timeframe_config'] = config['TIMEFRAMES']
                else:
                    raise FileNotFoundError("config.json not found")

            # Validate and cache config
            if not ConfigHandler._config_validated:
                ConfigHandler.validate_config(config)
                ConfigHandler._config_validated = True
                ConfigHandler._config_cache = config

            return config
            
        except Exception as e:
            print(f"Error loading configuration: {e}")
            raise

    @staticmethod
    def _load_from_env() -> Dict[str, Any]:
        """Load configuration from environment variables"""
        try:
            # Convert string to list for trading symbols
            trading_symbols = [s.strip() for s in os.getenv('TRADING_SYMBOLS', '').split(',') if s.strip()]
            
            config = {
                'BINANCE_API_KEY': os.getenv('BINANCE_API_KEY'),
                'BINANCE_API_SECRET': os.getenv('BINANCE_API_SECRET'),
                'TESTNET_API_KEY': os.getenv('TESTNET_API_KEY'),
                'TESTNET_API_SECRET': os.getenv('TESTNET_API_SECRET'),
                'TELEGRAM_TOKEN': os.getenv('TELEGRAM_TOKEN'),
                'TELEGRAM_CHAT_ID': os.getenv('TELEGRAM_CHAT_ID'),
                'TRADING_SYMBOLS': trading_symbols,
                'USE_TESTNET': os.getenv('USE_TESTNET', 'true').lower() == 'true',
                'USE_TELEGRAM': os.getenv('USE_TELEGRAM', 'true').lower() == 'true',
                'ORDER_TYPE': os.getenv('ORDER_TYPE', 'limit'),
                'USE_PERCENTAGE': os.getenv('USE_PERCENTAGE', 'false').lower() == 'true',
                'TRADE_AMOUNT': float(os.getenv('TRADE_AMOUNT', '10')),
                'RESERVE_BALANCE': float(os.getenv('RESERVE_BALANCE', '2000')),
            }

            # Parse timeframe configurations from environment
            timeframes = ConfigHandler._parse_timeframe_config()
            config['TIMEFRAMES'] = timeframes
            config['timeframe_config'] = timeframes  # Add both keys for compatibility
            
            return config
            
        except Exception as e:
            print(f"Error loading environment configuration: {e}")
            print("Environment variables available:")
            for key, value in os.environ.items():
                if not any(secret in key.lower() for secret in ['key', 'secret', 'token']):
                    print(f"{key}: {value}")
            raise

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

    @staticmethod
    def reset_cache():
        """Reset the configuration cache"""
        ConfigHandler._config_cache = None
        ConfigHandler._config_validated = False

    @staticmethod
    def get_config():
        """Get the current configuration"""
        if not ConfigHandler._config_cache:
            raise RuntimeError("Configuration not loaded. Call load_config() first.")
        return ConfigHandler._config_cache
