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
        telegram_settings = config.get('TELEGRAM_SETTINGS', {})
        telegram_token = telegram_settings.get('TELEGRAM_TOKEN', '')
        telegram_chat_id = telegram_settings.get('TELEGRAM_CHAT_ID', '')
        
        telegram_enabled = False
        
        if telegram_token and telegram_chat_id:
            if ConfigHandler.is_valid_token(telegram_token) and str(telegram_chat_id).lstrip('-').isdigit():
                telegram_enabled = True
                print("Telegram configuration validated successfully")
            else:
                print("Warning: Invalid Telegram configuration")
        
        # Update telegram settings in config
        telegram_settings['USE_TELEGRAM'] = telegram_enabled
        config['TELEGRAM_SETTINGS'] = telegram_settings

        # For backward compatibility
        config['USE_TELEGRAM'] = telegram_enabled
        config['TELEGRAM_TOKEN'] = telegram_token
        config['TELEGRAM_CHAT_ID'] = telegram_chat_id

        # Validate trading settings
        trading_settings = config.get('TRADING_SETTINGS', {})
        if trading_settings.get('MODE') not in ['spot', 'futures']:
            raise ValueError("Trading mode must be either 'spot' or 'futures'")
            
        # Validate futures settings if needed
        if trading_settings.get('MODE') == 'futures':
            futures_settings = config.get('FUTURES_SETTINGS', {})
            leverage = futures_settings.get('LEVERAGE', 1)
            
            if not isinstance(leverage, int) or leverage < 1 or leverage > 125:
                raise ValueError("Leverage must be between 1 and 125")
                
            if futures_settings.get('MARGIN_TYPE') not in ['isolated', 'cross']:
                raise ValueError("Margin type must be either 'isolated' or 'cross'")
                
            if futures_settings.get('POSITION_MODE') not in ['one-way', 'hedge']:
                raise ValueError("Position mode must be either 'one-way' or 'hedge'")

    @staticmethod
    def _validate_trading_settings(config: Dict[str, Any]) -> None:
        """Validate trading settings with detailed feedback"""
        settings = config.get('TRADING_SETTINGS', {})
        if not settings:
            raise ValueError("Missing TRADING_SETTINGS section")
            
        required_settings = {
            'MODE': ['spot', 'futures'],
            'USE_TESTNET': bool,
            'ORDER_TYPE': ['limit', 'market'],
            'TRADE_AMOUNT': (float, int),
            'USE_PERCENTAGE': bool,
            'RESERVE_BALANCE': (float, int)
        }
        
        for key, expected in required_settings.items():
            value = settings.get(key)
            if value is None:
                raise ValueError(f"Missing required setting: TRADING_SETTINGS.{key}")
                
            if isinstance(expected, list):
                if value not in expected:
                    raise ValueError(f"Invalid {key}: {value}. Must be one of {expected}")
            elif isinstance(expected, tuple):
                if not isinstance(value, expected):
                    raise ValueError(f"Invalid {key} type: {type(value)}. Must be {expected}")
            elif not isinstance(value, expected):
                raise ValueError(f"Invalid {key} type: {type(value)}. Must be {expected}")

    @staticmethod
    def _validate_timeframes(config: Dict[str, Any]) -> None:
        """Validate timeframe configuration"""
        timeframes = config.get('TIMEFRAMES', {})
        if not timeframes:
            raise ValueError("Missing TIMEFRAMES section")
            
        required_timeframes = ['daily', 'weekly', 'monthly']
        for timeframe in required_timeframes:
            if timeframe not in timeframes:
                raise ValueError(f"Missing timeframe configuration: {timeframe}")
                
            settings = timeframes[timeframe]
            if not isinstance(settings, dict):
                raise ValueError(f"Invalid timeframe settings for {timeframe}")
                
            if 'enabled' not in settings:
                raise ValueError(f"Missing 'enabled' setting for {timeframe}")
                
            if 'thresholds' not in settings:
                raise ValueError(f"Missing 'thresholds' setting for {timeframe}")
                
            thresholds = settings['thresholds']
            if not isinstance(thresholds, list):
                raise ValueError(f"Thresholds must be a list for {timeframe}")
                
            if not all(isinstance(t, (int, float)) for t in thresholds):
                raise ValueError(f"Invalid threshold values for {timeframe}")

    @staticmethod
    def load_config(use_env: bool = False) -> Dict[str, Any]:
        """Enhanced configuration loading with validation"""
        try:
            # Load from appropriate source
            if use_env:
                config = ConfigHandler._load_from_env()
                print("Using environment variables for configuration")
            else:
                config = ConfigHandler._load_from_json()
                print("Using config.json for configuration")

            # Validate configuration sections
            ConfigHandler._validate_trading_settings(config)
            ConfigHandler._validate_timeframes(config)
            ConfigHandler.validate_config(config)  # Original validation

            # Print loaded configuration
            print("\nValidated Configuration:")
            print(f"Mode: {config['TRADING_SETTINGS']['MODE']}")
            print(f"Test Mode: {config['TRADING_SETTINGS']['USE_TESTNET']}")
            print(f"Symbols: {', '.join(config['TRADING_SYMBOLS'])}")
            print(f"Reserve Balance: {config['TRADING_SETTINGS']['RESERVE_BALANCE']} USDT")
            print("\nActive Timeframes:")
            for timeframe, settings in config['TIMEFRAMES'].items():
                if settings['enabled']:
                    thresholds = [f"{t*100:.1f}%" for t in settings['thresholds']]
                    print(f"• {timeframe.capitalize()}: {', '.join(thresholds)}")

            # Cache validated config
            ConfigHandler._config_cache = config
            ConfigHandler._config_validated = True
            
            return config

        except FileNotFoundError:
            print("Config file not found, creating default...")
            ConfigHandler._create_default_config()
            raise ValueError("Please fill in the newly created config.json")
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON in config file")
        except Exception as e:
            raise ValueError(f"Configuration error: {str(e)}")

    @staticmethod
    def _create_default_config():
        """Create default config template"""
        default_config = {
            "TRADING_SETTINGS": {
                "MODE": "spot",
                "USE_TESTNET": True,
                "ORDER_TYPE": "limit",
                "TRADE_AMOUNT": 10,
                "USE_PERCENTAGE": False,
                "RESERVE_BALANCE": 2000
            },
            "BINANCE_API_KEY": "your_api_key_here",
            "BINANCE_API_SECRET": "your_api_secret_here",
            "TESTNET_API_KEY": "your_testnet_key_here",
            "TESTNET_API_SECRET": "your_testnet_secret_here",
            "TRADING_SYMBOLS": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
            "TIMEFRAMES": {
                "daily": {
                    "enabled": True,
                    "thresholds": [0.01, 0.02, 0.03]
                },
                "weekly": {
                    "enabled": True,
                    "thresholds": [0.03, 0.06, 0.10]
                },
                "monthly": {
                    "enabled": True,
                    "thresholds": [0.01, 0.10]
                }
            },
            "TELEGRAM_SETTINGS": {
                "USE_TELEGRAM": False,
                "TELEGRAM_TOKEN": "",
                "TELEGRAM_CHAT_ID": ""
            }
        }

        # Create config directory if needed
        os.makedirs('config', exist_ok=True)
        
        # Write default config
        with open('config/config.json', 'w') as f:
            json.dump(default_config, f, indent=4)
            
        print(f"Default configuration created at {os.path.abspath('config/config.json')}")

    @staticmethod
    def _validate_critical_settings(config: Dict[str, Any]) -> None:
        """Validate all critical configuration settings"""
        required_settings = {
            'TRADING_SETTINGS': {
                'MODE': ['spot', 'futures'],
                'USE_TESTNET': bool,
                'ORDER_TYPE': ['limit', 'market'],
                'TRADE_AMOUNT': float,
                'USE_PERCENTAGE': bool,
                'RESERVE_BALANCE': float
            },
            'TIMEFRAMES': {
                'daily': {'enabled': bool, 'thresholds': list},
                'weekly': {'enabled': bool, 'thresholds': list},
                'monthly': {'enabled': bool, 'thresholds': list}
            }
        }

        # Validate each required setting
        for section, settings in required_settings.items():
            if section not in config:
                raise ValueError(f"Missing required section: {section}")
                
            for key, expected_type in settings.items():
                if isinstance(expected_type, list):
                    if key not in config[section] or config[section][key] not in expected_type:
                        raise ValueError(f"Invalid {key} in {section}. Must be one of: {expected_type}")
                elif key not in config[section] or not isinstance(config[section][key], expected_type):
                    raise ValueError(f"Missing or invalid {key} in {section}. Expected type: {expected_type}")

        # Print validated configuration
        print("\nValidated Configuration:")
        print(f"Mode: {config['TRADING_SETTINGS']['MODE']}")
        print(f"Test Mode: {config['TRADING_SETTINGS']['USE_TESTNET']}")
        print(f"Order Type: {config['TRADING_SETTINGS']['ORDER_TYPE']}")
        print(f"Trade Amount: {config['TRADING_SETTINGS']['TRADE_AMOUNT']} USDT")
        print(f"Reserve Balance: {config['TRADING_SETTINGS']['RESERVE_BALANCE']} USDT")
        print("\nEnabled Timeframes:")
        for timeframe, settings in config['TIMEFRAMES'].items():
            if settings['enabled']:
                print(f"{timeframe.capitalize()}: {[f'{t*100}%' for t in settings['thresholds']]}")

    @staticmethod
    def _parse_timeframe_config() -> Dict[str, Dict]:
        """Parse timeframe configuration from environment variables"""
        try:
            timeframes = {}
            
            # Get default configurations from environment
            daily_cfg = os.getenv('DAILY_CONFIG', 'true:1,2,3').split(':')
            weekly_cfg = os.getenv('WEEKLY_CONFIG', 'true:3,6,10').split(':')
            monthly_cfg = os.getenv('MONTHLY_CONFIG', 'true:5,10').split(':')
            
            # Parse each timeframe
            configs = {
                'daily': daily_cfg,
                'weekly': weekly_cfg,
                'monthly': monthly_cfg
            }
            
            for timeframe, cfg in configs.items():
                if len(cfg) != 2:
                    raise ValueError(f"Invalid {timeframe} config format: {cfg}")
                    
                enabled = cfg[0].lower() == 'true'
                thresholds = [float(x)/100 for x in cfg[1].split(',')]
                
                timeframes[timeframe] = {
                    'enabled': enabled,
                    'thresholds': thresholds
                }
                
            return timeframes
            
        except Exception as e:
            raise ValueError(f"Error parsing timeframe configuration: {e}")

    @staticmethod
    def _load_from_env() -> Dict[str, Any]:
        """Load configuration from environment variables"""
        try:
            # First load required variables
            required_vars = {
                'BINANCE_API_KEY': 'Binance API key',
                'BINANCE_API_SECRET': 'Binance API secret',
                'TRADING_MODE': 'Trading mode (spot/futures)',
                'TRADING_SYMBOLS': 'Trading symbols list'
            }

            # Check required variables
            missing = [var for var, desc in required_vars.items() if not os.getenv(var)]
            if missing:
                raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

            # ...rest of the existing _load_from_env code...

            required_env_vars = [
                'BINANCE_API_KEY',
                'BINANCE_API_SECRET',
                'TRADING_MODE',
                'USE_TESTNET',
                'ORDER_TYPE',
                'TRADE_AMOUNT',
                'RESERVE_BALANCE',
                'TRADING_SYMBOLS'
            ]

            # Verify required variables exist
            missing_vars = [var for var in required_env_vars if not os.getenv(var)]
            if missing_vars:
                raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

            config = {
                'TRADING_SETTINGS': {
                    'MODE': os.getenv('TRADING_MODE', 'spot').lower(),
                    'USE_TESTNET': os.getenv('USE_TESTNET', 'true').lower() == 'true',
                    'ORDER_TYPE': os.getenv('ORDER_TYPE', 'limit').lower(),
                    'TRADE_AMOUNT': float(os.getenv('TRADE_AMOUNT', '10')),
                    'USE_PERCENTAGE': os.getenv('USE_PERCENTAGE', 'false').lower() == 'true',
                    'RESERVE_BALANCE': float(os.getenv('RESERVE_BALANCE', '2000'))
                },
                'BINANCE_API_KEY': os.getenv('BINANCE_API_KEY'),
                'BINANCE_API_SECRET': os.getenv('BINANCE_API_SECRET'),
                'TESTNET_API_KEY': os.getenv('TESTNET_API_KEY'),
                'TESTNET_API_SECRET': os.getenv('TESTNET_API_SECRET'),
                'TRADING_SYMBOLS': [s.strip() for s in os.getenv('TRADING_SYMBOLS', '').split(',') if s.strip()],
                'TELEGRAM_SETTINGS': {
                    'USE_TELEGRAM': os.getenv('USE_TELEGRAM', 'false').lower() == 'true',
                    'TELEGRAM_TOKEN': os.getenv('TELEGRAM_TOKEN', ''),
                    'TELEGRAM_CHAT_ID': os.getenv('TELEGRAM_CHAT_ID', '')
                }
            }

            # Parse timeframe configurations
            config['TIMEFRAMES'] = ConfigHandler._parse_timeframe_config()
            
            return config

        except Exception as e:
            raise ValueError(f"Error loading configuration from environment: {e}")

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
