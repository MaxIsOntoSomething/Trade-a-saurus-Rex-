import asyncio
import json
import logging
import os
from pathlib import Path
import sys

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

# Setup logging first and get config logger
config_logger = setup_logging()
logger = logging.getLogger(__name__)

def validate_config(config: dict) -> bool:
    """Validate configuration parameters"""
    required_fields = {
        'binance': ['api_key', 'api_secret', 'testnet'],
        'telegram': ['bot_token', 'allowed_users'],
        'mongodb': ['uri', 'database'],
        'trading': ['base_currency', 'order_amount', 'cancel_after_hours', 
                   'pairs', 'thresholds']
    }
    
    try:
        for section, fields in required_fields.items():
            if section not in config:
                raise ValueError(f"Missing section: {section}")
            for field in fields:
                if field not in config[section]:
                    raise ValueError(f"Missing field: {section}.{field}")
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
        'binance': {
            'api_key': os.getenv('BINANCE_API_KEY'),
            'api_secret': os.getenv('BINANCE_API_SECRET'),
            'testnet': os.getenv('BINANCE_TESTNET', 'true').lower() == 'true'
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

async def main():
    """Main function with improved config loading"""
    print(DINO_ASCII)
    logger.info("Starting Trade-a-saurus Rex...")
    
    try:
        # Load configuration from appropriate source
        config = load_and_merge_config()
        
        # Debug log the configuration
        logger.info("=" * 50)
        logger.info("[CONFIG] Active Configuration:")
        logger.info(f"[CONFIG] Base Currency: {config['trading']['base_currency']}")
        logger.info(f"[CONFIG] Reserve Balance: ${config['trading']['reserve_balance']:,.2f}")
        logger.info(f"[CONFIG] Trading Pairs: {', '.join(config['trading']['pairs'])}")
        logger.info(f"[CONFIG] Order Amount: ${config['trading']['order_amount']:,.2f}")
        logger.info("=" * 50)

        if not validate_config(config):
            logger.error("Invalid configuration, exiting...")
            return

        # Initialize components
        binance_client = BinanceClient(
            api_key=config['binance']['api_key'],
            api_secret=config['binance']['api_secret'],
            testnet=config['binance']['testnet']
        )
        await binance_client.initialize()
        
        # Check initial connection with all configured pairs
        if not await check_initial_connection(binance_client, config):
            logger.error("Initial connection check failed, exiting...")
            return
        
        mongo_client = MongoClient(
            uri=config['mongodb']['uri'],
            database=config['mongodb']['database']
        )
        await mongo_client.init_indexes()
        
        telegram_bot = TelegramBot(
            token=config['telegram']['bot_token'],
            allowed_users=config['telegram']['allowed_users'],
            binance_client=binance_client,
            mongo_client=mongo_client,
            config=config  # Add config here
        )
        await telegram_bot.initialize()
        
        order_manager = OrderManager(
            binance_client=binance_client,
            mongo_client=mongo_client,
            telegram_bot=telegram_bot,
            config=config
        )

        # Run both components concurrently
        try:
            # Start both services
            tasks = [
                asyncio.create_task(order_manager.start()),
                asyncio.create_task(telegram_bot.start())
            ]
            
            # Keep the main loop running
            while True:
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    logger.info("Shutdown signal received...")
                    break
                    
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt...")
        finally:
            # Cancel all tasks
            for task in tasks:
                task.cancel()
            # Wait for tasks to complete
            await asyncio.gather(*tasks, return_exceptions=True)
            # Cleanup
            logger.info("Shutting down components...")
            await asyncio.gather(
                order_manager.stop(),
                telegram_bot.stop(),
                binance_client.close(),
                return_exceptions=True
            )
            
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    asyncio.run(main())
