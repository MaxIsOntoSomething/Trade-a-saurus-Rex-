import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import signal
import traceback
from aiohttp import web
import threading
from datetime import datetime

# Configure Windows console for UTF-8
if sys.platform == 'win32':
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    sys.stdout.reconfigure(encoding='utf-8')

from dotenv import load_dotenv
from src.trading.binance_client import BinanceClient
from src.database.mongo_client import MongoClient
from src.telegram.bot import TelegramBot, DINO_ASCII
from src.trading.order_manager import OrderManager
from src.utils.logger import setup_logging
from src.trading.futures_client import FuturesClient
from src.utils.scheduler import WeeklySummaryScheduler

# Setup logging first and get config logger
config_logger = setup_logging()
logger = logging.getLogger(__name__)

class ClientManager:
    def __init__(self, config: dict):
        self.config = config
        self.spot_client = None
        self.futures_client = None
        self.active_client = None
        self.trading_mode = config['environment']['trading_mode']
        self.testnet = config['environment']['testnet']

    async def initialize(self) -> BinanceClient:
        """Initialize appropriate client based on configuration"""
        try:
            # Get API credentials based on environment
            if self.testnet:
                if self.trading_mode == 'futures':
                    api_config = {
                        **self.config['binance']['testnet_futures'],
                        'testnet': True,  # Add testnet flag
                        'reserve_balance': self.config['trading']['reserve_balance'],  # Add reserve balance
                        **self.config['trading'].get('futures_settings', {}),  # Add futures settings
                        'env': {  # Add environment variables
                            'TRADING_RESERVE_BALANCE': os.getenv('TRADING_RESERVE_BALANCE')
                        }
                    }
                else:
                    api_config = {
                        **self.config['binance']['testnet_spot'],
                        'testnet': True
                    }
            else:
                api_config = {
                    **self.config['binance']['mainnet'],
                    'testnet': False
                }

            # Initialize appropriate client with full config
            if self.trading_mode == 'futures':
                self.futures_client = FuturesClient(api_config)
                await self.futures_client.initialize()
                self.active_client = self.futures_client
            else:
                self.spot_client = BinanceClient(
                    api_key=api_config['api_key'],
                    api_secret=api_config['api_secret'],
                    testnet=self.testnet,
                    base_currency=self.config['trading']['base_currency'],
                    reserve_balance=self.config['trading']['reserve_balance'],
                    config=self.config  # Pass the full config object
                )
                await self.spot_client.initialize()
                self.active_client = self.spot_client

            logger.info(
                f"Initialized {self.trading_mode.upper()} client "
                f"on {'Testnet' if self.testnet else 'Mainnet'} "
                f"with base currency: {self.config['trading']['base_currency']} "
                f"and reserve: ${self.config['trading']['reserve_balance']:,.2f}"
            )
            return self.active_client

        except Exception as e:
            logger.error(f"Failed to initialize client manager: {e}")
            raise

def validate_config(config: dict) -> bool:
    """Validate configuration parameters"""
    required_fields = {
        'environment': ['testnet', 'trading_mode'],
        'binance': {
            'mainnet': ['api_key', 'api_secret'],
            'testnet_spot': ['api_key', 'api_secret'],
            'testnet_futures': ['api_key', 'api_secret']
        },
        'telegram': ['bot_token', 'allowed_users'],
        'mongodb': ['uri', 'database'],
        'trading': ['base_currency', 'cancel_after_hours', 
                   'pairs', 'thresholds', 'reserve_balance']  # Removed order_amount from required fields
    }

    try:
        # Validate trading amount settings
        trading = config.get('trading', {})
        if not any(key in trading for key in ['order_amount', 'amount_type']):
            raise ValueError("Either 'order_amount' or 'amount_type' must be specified in trading section")

        # If amount_type is specified, validate its settings
        if 'amount_type' in trading:
            if trading['amount_type'] not in ['fixed', 'percentage']:
                raise ValueError("amount_type must be 'fixed' or 'percentage'")
            
            if trading['amount_type'] == 'fixed' and 'fixed_amount' not in trading:
                raise ValueError("fixed_amount must be specified when amount_type is 'fixed'")
            
            if trading['amount_type'] == 'percentage' and 'percentage_amount' not in trading:
                raise ValueError("percentage_amount must be specified when amount_type is 'percentage'")

        # Validate environment settings
        if config['environment']['trading_mode'] not in ['spot', 'futures']:
            raise ValueError("trading_mode must be 'spot' or 'futures'")

        # Validate nested structure
        for section, fields in required_fields.items():
            if section not in config:
                raise ValueError(f"Missing section: {section}")
                
            if isinstance(fields, dict):
                for subsection, subfields in fields.items():
                    if subsection not in config[section]:
                        raise ValueError(f"Missing subsection: {section}.{subsection}")
                    for field in subfields:
                        if field not in config[section][subsection]:
                            raise ValueError(f"Missing field: {section}.{subsection}.{field}")
            else:
                for field in fields:
                    if field not in config[section]:
                        raise ValueError(f"Missing field: {section}.{field}")

        # Validate futures settings if in futures mode
        if config['environment']['trading_mode'] == 'futures':
            futures_settings = config['trading'].get('futures_settings')
            if not futures_settings:
                raise ValueError("Missing futures_settings in trading section")
            
            required_futures_fields = [
                'default_leverage',
                'margin_type',
                'position_mode'
            ]
            
            for field in required_futures_fields:
                if field not in futures_settings:
                    raise ValueError(f"Missing required futures field: {field}")
                    
            # Validate leverage range
            if not (1 <= futures_settings['default_leverage'] <= 125):
                raise ValueError("default_leverage must be between 1 and 125")
                
            # Validate margin type
            if futures_settings['margin_type'] not in ['ISOLATED', 'CROSSED']:
                raise ValueError("margin_type must be 'ISOLATED' or 'CROSSED'")
                
            # Validate position mode
            if futures_settings['position_mode'] not in ['ONE_WAY', 'HEDGE']:
                raise ValueError("position_mode must be 'ONE_WAY' or 'HEDGE'")

        # Validate thresholds structure
        threshold_timeframes = ['daily', 'weekly', 'monthly']
        for timeframe in threshold_timeframes:
            if timeframe not in config['trading']['thresholds']:
                raise ValueError(f"Missing threshold timeframe: {timeframe}")
            if not isinstance(config['trading']['thresholds'][timeframe], list):
                raise ValueError(f"Thresholds for {timeframe} must be a list")

        # Validate futures TP/SL settings if in futures mode
        if config['environment']['trading_mode'] == 'futures':
            futures_settings = config['trading'].get('futures_settings', {})
            
            # Validate TP/SL percentages if enabled
            if futures_settings.get('tp_enabled', False):
                tp_percent = futures_settings.get('default_tp_percent', 0)
                if not (0 < tp_percent <= 500):  # Maximum 500% profit target
                    raise ValueError("default_tp_percent must be between 0 and 500")
                    
            if futures_settings.get('sl_enabled', False):
                sl_percent = futures_settings.get('default_sl_percent', 0)
                if not (0 < sl_percent <= 100):  # Maximum 100% loss
                    raise ValueError("default_sl_percent must be between 0 and 100")

        return True
        
    except Exception as e:
        logger.error(f"Configuration validation failed: {e}")
        return False

def load_config_from_env() -> dict:
    """Load configuration from environment variables"""
    # Load reserve balance with proper parsing
    try:
        reserve_balance = os.getenv('TRADING_RESERVE_BALANCE')
        if reserve_balance:
            # Handle scientific notation and large numbers
            reserve_balance = float(reserve_balance.replace(',', ''))
            logger.info(f"[CONFIG] Loaded reserve balance from ENV: ${reserve_balance:,.2f}")
        else:
            reserve_balance = 500  # Default value
            logger.warning(f"[CONFIG] Using default reserve balance: ${reserve_balance:,.2f}")
    except (TypeError, ValueError) as e:
        logger.error(f"[CONFIG] Error parsing reserve balance: {e}")
        reserve_balance = 500  # Fallback to default

    # Rest of the config loading
    config = {
        'environment': {
            'testnet': os.getenv('TRADING_TESTNET', 'true').lower() == 'true',
            'trading_mode': os.getenv('TRADING_MODE', 'spot').lower()
        },
        'binance': {
            'mainnet': {
                'api_key': os.getenv('BINANCE_MAINNET_API_KEY'),
                'api_secret': os.getenv('BINANCE_MAINNET_API_SECRET')
            },
            'testnet_spot': {
                'api_key': os.getenv('BINANCE_TESTNET_SPOT_API_KEY'),
                'api_secret': os.getenv('BINANCE_TESTNET_SPOT_API_SECRET')
            },
            'testnet_futures': {
                'api_key': os.getenv('BINANCE_TESTNET_FUTURES_API_KEY'),
                'api_secret': os.getenv('BINANCE_TESTNET_FUTURES_API_SECRET')
            }
        },
        'telegram': {
            'bot_token': os.getenv('TELEGRAM_BOT_TOKEN'),
            'allowed_users': [int(id) for id in os.getenv('TELEGRAM_ALLOWED_USERS', '').split(',') if id]
        },
        'mongodb': {
            'uri': os.getenv('MONGODB_URI', 'mongodb://localhost:27017'),
            'database': os.getenv('MONGODB_DATABASE', 'tradeasaurus')
        },
        'trading': {
            'base_currency': os.getenv('TRADING_BASE_CURRENCY', 'USDT'),
            'order_amount': float(os.getenv('TRADING_ORDER_AMOUNT', '100')),
            'cancel_after_hours': int(os.getenv('TRADING_CANCEL_HOURS', '8')),
            'pairs': os.getenv('TRADING_PAIRS', 'BTCUSDT,ETHUSDT').split(','),
            'reserve_balance': reserve_balance,  # Use parsed reserve balance
            'thresholds': {
                'daily': [float(x) for x in os.getenv('TRADING_THRESHOLDS_DAILY', '1,2,5').split(',') if x],
                'weekly': [float(x) for x in os.getenv('TRADING_THRESHOLDS_WEEKLY', '5,10,15').split(',') if x],
                'monthly': [float(x) for x in os.getenv('TRADING_THRESHOLDS_MONTHLY', '10,20,30').split(',') if x]
            }
        }
    }
    
    # Add futures configuration if needed
    if config['environment']['trading_mode'] == 'futures':
        config['trading']['futures_settings'] = {
            'enabled': True,
            'default_leverage': int(os.getenv('FUTURES_DEFAULT_LEVERAGE', '5')),
            'default_margin_type': os.getenv('FUTURES_MARGIN_TYPE', 'ISOLATED'),
            'position_mode': os.getenv('FUTURES_POSITION_MODE', 'ONE_WAY'),
            'allowed_pairs': config['trading']['pairs'],  # Fetch from trading pairs
            'tp_enabled': os.getenv('FUTURES_TP_ENABLED', 'true').lower() == 'true',
            'sl_enabled': os.getenv('FUTURES_SL_ENABLED', 'true').lower() == 'true',
            'default_tp_percent': float(os.getenv('FUTURES_DEFAULT_TP_PERCENT', '50')),
            'default_sl_percent': float(os.getenv('FUTURES_DEFAULT_SL_PERCENT', '10'))
        }

    return config

def load_and_merge_config() -> dict:
    """Load and merge configuration from appropriate sources"""
    try:
        in_docker = os.getenv('RUNNING_IN_DOCKER', '').lower() == 'true'
        config_logger.log_config(f"Running in Docker: {in_docker}")
        
        config = {}
        config_path = Path('config/config.json')
        if not in_docker and config_path.exists():
            config_logger.log_config("Loading configuration from config.json")
            with open(config_path, 'r') as f:
                config = json.load(f)
        else:
            config_logger.log_config("Loading configuration from environment variables")
            load_dotenv()
            config = load_config_from_env()
        
        if not validate_config(config):
            raise ValueError("Invalid configuration")
            
        # Ensure allowed pairs are fetched from trading pairs
        if config['environment']['trading_mode'] == 'futures':
            config['trading']['futures_settings']['allowed_pairs'] = config['trading']['pairs']
        
        # Log final configuration
        config_logger.log_config(
            f"Active Configuration:\n"
            f"Base Currency: {config['trading']['base_currency']}\n"
            f"Reserve Balance: ${config['trading']['reserve_balance']:,.2f}\n"
            f"Trading Pairs: {', '.join(config['trading']['pairs'])}"
        )
        
        return config
        
    except Exception as e:
        logger.error(f"Error loading configuration: {e}")
        raise

async def check_initial_connection(binance_client: BinanceClient, config: dict) -> bool:
    """Check initial connection and get prices for all configured pairs"""
    try:
        logger.info("Testing connection to Binance...")
        await binance_client.client.ping()
        
        logger.info("=" * 50)
        logger.info("✅ Successfully connected to Binance")
        
        # Get prices for all configured pairs
        for symbol in config['trading']['pairs']:
            ticker = await binance_client.client.get_symbol_ticker(symbol=symbol)
            price = float(ticker['price'])
            logger.info(f"🔸 Current {symbol} Price: ${price:,.2f}")
        
        logger.info("=" * 50)
        return True
    except Exception as e:
        logger.error("=" * 50)
        logger.error(f"❌ Failed to connect to Binance: {e}")
        logger.error("=" * 50)
        return False

# Add health check endpoint
async def health_check(request):
    return web.Response(text="OK")

# Add crash handler
def handle_crash(exc_type, exc_value, exc_traceback):
    crash_time = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    crash_file = f"crash_reports/crash_{crash_time}.txt"
    
    try:
        os.makedirs("crash_reports", exist_ok=True)
        with open(crash_file, "w") as f:
            f.write(f"Crash Report - {crash_time}\n\n")
            traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)
            
        logger.critical(f"Bot crashed! Report saved to {crash_file}")
        
        # Force restart container
        if os.getenv('RUNNING_IN_DOCKER') == 'true':
            os.kill(1, signal.SIGTERM)
    except Exception as e:
        logger.critical(f"Failed to save crash report: {e}")

# Add monitoring server
async def start_monitoring_server():
    app = web.Application()
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, 'localhost', 8080)
    await site.start()
    return runner

async def main():
    """Main function with crash recovery"""
    print(DINO_ASCII)
    logger.info("Starting Trade-a-saurus Rex...")

    # Set up crash handler
    sys.excepthook = handle_crash

    # Start monitoring server
    monitor = await start_monitoring_server()
    
    try:
        restart_count = 0
        max_restarts = 5
        
        while restart_count < max_restarts:
            try:
                # Load configuration from appropriate source
                config = load_and_merge_config()
                
                # Initialize client manager
                client_manager = ClientManager(config)
                active_client = await client_manager.initialize()

                # Initialize other components with active client
                mongo_client = MongoClient(
                    uri=config['mongodb']['uri'],
                    database=config['mongodb']['database']
                )
                await mongo_client.init_indexes()

                telegram_bot = TelegramBot(
                    token=config['telegram']['bot_token'],
                    allowed_users=config['telegram']['allowed_users'],
                    binance_client=active_client,
                    mongo_client=mongo_client,
                    config=config
                )
                await telegram_bot.initialize()
                
                order_manager = OrderManager(
                    binance_client=active_client,
                    mongo_client=mongo_client,
                    telegram_bot=telegram_bot,
                    config=config
                )

                # Initialize scheduler
                scheduler = WeeklySummaryScheduler(telegram_bot, mongo_client)
                
                # Run components with automatic recovery
                while True:
                    try:
                        # Start both services
                        tasks = [
                            asyncio.create_task(order_manager.start()),
                            asyncio.create_task(telegram_bot.start()),
                            asyncio.create_task(scheduler.run())  # Add scheduler task
                        ]
                        
                        # Monitor tasks
                        while True:
                            for task in tasks:
                                if task.done() and task.exception():
                                    raise task.exception()
                                await asyncio.sleep(1)
                                
                    except Exception as e:
                        logger.error(f"Service error, restarting: {e}")
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        await asyncio.sleep(5)
                        continue

            except Exception as e:
                logger.error(f"Critical error: {e}", exc_info=True)
                restart_count += 1
                if restart_count < max_restarts:
                    logger.info(f"Restarting bot (attempt {restart_count}/{max_restarts})...")
                    await asyncio.sleep(30)
                else:
                    logger.critical("Max restart attempts reached!")
                    raise

    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        raise
    finally:
        await monitor.cleanup()
        logger.info("Bot shutdown complete")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt, shutting down...")
    except Exception as e:
        logger.critical(f"Fatal error in main: {e}", exc_info=True)
        sys.exit(1)
