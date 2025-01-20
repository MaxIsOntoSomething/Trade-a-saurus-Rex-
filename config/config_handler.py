import os
import json
from pathlib import Path
from typing import Dict, Any

class ConfigHandler:
    @staticmethod
    def load_config() -> Dict[str, Any]:
        """Load configuration from .env or config.json based on environment"""
        if os.path.exists('.env'):
            return ConfigHandler._load_from_env()
        return ConfigHandler._load_from_json()

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
