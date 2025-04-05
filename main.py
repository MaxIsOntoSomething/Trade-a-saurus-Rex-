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
    """Validate configuration parameters with updated structure"""
    required_fields = {
        'binance': ['spot_testnet', 'mainnet', 'use_testnet'],
        'telegram': ['bot_token', 'allowed_users'],
        'mongodb': ['uri', 'database'],
        'trading': ['base_currency', 'order_amount', 'cancel_after_hours', 
                   'pairs', 'thresholds']
    }
    
    try:
        for section, fields in required_fields.items():
            if section not in config:
                logger.error(f"Missing section: {section}")
                return False
            
            for field in fields:
                if field not in config[section]:
                    logger.error(f"Missing field: {section}.{field}")
                    return False
                    
        # Additional validation for nested API keys
        if 'api_key' not in config['binance']['spot_testnet'] or 'api_secret' not in config['binance']['spot_testnet']:
            logger.error("Missing Binance spot testnet API credentials")
            return False
            
        if 'api_key' not in config['binance']['mainnet'] or 'api_secret' not in config['binance']['mainnet']:
            logger.error("Missing Binance mainnet API credentials")
            return False
            
        return True
    except Exception as e:
        logger.error(f"Configuration validation failed: {e}")
        return False

def load_config_from_env() -> dict:
    """Load configuration from environment variables with support for both API sets"""
    # Load reserve balance with proper parsing
    try:
        reserve_balance = os.getenv('TRADING_RESERVE_BALANCE')
        if reserve_balance:
            reserve_balance = float(reserve_balance.replace(',', ''))
            logger.info(f"[CONFIG] Loaded reserve balance from ENV: ${reserve_balance:,.2f}")
        else:
            reserve_balance = 500  # Default value
            logger.warning(f"[CONFIG] Using default reserve balance: ${reserve_balance:,.2f}")
    except (TypeError, ValueError) as e:
        logger.error(f"[CONFIG] Error parsing reserve balance: {e}")
        reserve_balance = 500  # Fallback to default

    # Parse TP/SL settings - add proper error handling and logging
    try:
        take_profit_setting = os.getenv('TRADING_TAKE_PROFIT', '5%')
        stop_loss_setting = os.getenv('TRADING_STOP_LOSS', '3%')
        logger.info(f"[CONFIG] Raw TP/SL settings from ENV - TP: {take_profit_setting}, SL: {stop_loss_setting}")
    except Exception as e:
        logger.error(f"[CONFIG] Error accessing TP/SL settings: {e}")
        take_profit_setting = '5%'
        stop_loss_setting = '3%'
    
    # Parse partial take profit settings
    partial_tp_enabled = os.getenv('TRADING_PARTIAL_TP_ENABLED', 'false').lower() == 'true'
    partial_tp_levels = []
    
    try:
        # Load the partial take profit levels if enabled
        if partial_tp_enabled:
            logger.info("[CONFIG] Partial take profits are enabled, loading levels...")
            
            # Process each level (up to 3)
            for level in range(1, 4):
                level_env = os.getenv(f'TRADING_PARTIAL_TP_LEVEL{level}')
                if not level_env:
                    continue
                
                # Parse the profit percentage and position percentage
                parts = level_env.split(',')
                if len(parts) != 2:
                    logger.warning(f"[CONFIG] Invalid format for TRADING_PARTIAL_TP_LEVEL{level}: {level_env}")
                    continue
                
                try:
                    profit_percentage = float(parts[0])
                    position_percentage = float(parts[1])
                    
                    partial_tp_levels.append({
                        "level": level,
                        "profit_percentage": profit_percentage,
                        "position_percentage": position_percentage
                    })
                    
                    logger.info(f"[CONFIG] Loaded partial TP level {level}: {profit_percentage}% profit, {position_percentage}% of position")
                    
                except ValueError as e:
                    logger.warning(f"[CONFIG] Error parsing TRADING_PARTIAL_TP_LEVEL{level}: {e}")
        else:
            logger.info("[CONFIG] Partial take profits are disabled")
    except Exception as e:
        logger.error(f"[CONFIG] Error loading partial take profit settings: {e}")
        partial_tp_enabled = False
        partial_tp_levels = []

    # Parse trailing stop loss settings
    try:
        trailing_sl_enabled = os.getenv('TRADING_TRAILING_SL_ENABLED', 'false').lower() == 'true'
        
        # Get activation percentage and callback rate
        trailing_sl_activation = 0.0
        trailing_sl_callback = 0.0
        
        if trailing_sl_enabled:
            try:
                trailing_sl_activation = float(os.getenv('TRADING_TRAILING_SL_ACTIVATION', '1.0'))
                trailing_sl_callback = float(os.getenv('TRADING_TRAILING_SL_CALLBACK', '0.5'))
                logger.info(f"[CONFIG] Trailing stop loss enabled with activation: {trailing_sl_activation}%, callback: {trailing_sl_callback}%")
            except ValueError as e:
                logger.warning(f"[CONFIG] Error parsing trailing stop loss parameters: {e}")
                trailing_sl_enabled = False
        else:
            logger.info("[CONFIG] Trailing stop loss is disabled")
    except Exception as e:
        logger.error(f"[CONFIG] Error loading trailing stop loss settings: {e}")
        trailing_sl_enabled = False
        trailing_sl_activation = 0.0
        trailing_sl_callback = 0.0

    # Add MongoDB driver configuration with proper validation
    mongodb_driver = os.getenv('MONGODB_DRIVER', 'motor').lower()
    if mongodb_driver not in ['motor', 'pymongo_async', 'pymongo']:
        logger.warning(f"[CONFIG] Invalid MongoDB driver '{mongodb_driver}'. Using 'motor' as default.")
        mongodb_driver = 'motor'
    logger.info(f"[CONFIG] Using MongoDB driver: {mongodb_driver}")

    # Add MongoDB load config setting
    mongodb_load_config = os.getenv('MONGODB_LOAD_CONFIG', 'true').lower() == 'true'
    logger.info(f"[CONFIG] Load config from MongoDB: {mongodb_load_config}")

    # Rest of the config loading with spot_testnet/mainnet API keys
    config = {
        'binance': {
            'spot_testnet': {
                'api_key': os.getenv('BINANCE_SPOT_TESTNET_API_KEY'),
                'api_secret': os.getenv('BINANCE_SPOT_TESTNET_API_SECRET')
            },
            'mainnet': {
                'api_key': os.getenv('BINANCE_MAINNET_API_KEY'),
                'api_secret': os.getenv('BINANCE_MAINNET_API_SECRET')
            },
            'use_testnet': os.getenv('BINANCE_USE_TESTNET', 'true').lower() == 'true'
        },
        'telegram': {
            'bot_token': os.getenv('TELEGRAM_BOT_TOKEN'),
            'allowed_users': [int(id) for id in os.getenv('TELEGRAM_ALLOWED_USERS', '').split(',') if id]
        },
        'mongodb': {
            'uri': os.getenv('MONGODB_URI', 'mongodb://localhost:27017'),
            'database': os.getenv('MONGODB_DATABASE', 'tradeasaurus'),
            'driver': mongodb_driver,
            'load_db_config': mongodb_load_config
        },
        'trading': {
            'base_currency': os.getenv('TRADING_BASE_CURRENCY', 'USDT'),
            'order_amount': float(os.getenv('TRADING_ORDER_AMOUNT', '100')),
            'cancel_after_hours': int(os.getenv('TRADING_CANCEL_HOURS', '8')),
            'pairs': os.getenv('TRADING_PAIRS', 'BTCUSDT,ETHUSDT').split(','),
            'reserve_balance': reserve_balance,
            'take_profit': take_profit_setting,
            'stop_loss': stop_loss_setting,
            'only_lower_entries': os.getenv('TRADING_ONLY_LOWER_ENTRIES', 'true').lower() == 'true',
            'partial_take_profits': {
                'enabled': partial_tp_enabled,
                'levels': partial_tp_levels
            },
            'trailing_stop_loss': {
                'enabled': trailing_sl_enabled,
                'activation_percentage': trailing_sl_activation,
                'callback_rate': trailing_sl_callback
            },
            'thresholds': {
                'daily': [float(x) for x in os.getenv('TRADING_THRESHOLDS_DAILY', '1,2,5').split(',') if x],
                'weekly': [float(x) for x in os.getenv('TRADING_THRESHOLDS_WEEKLY', '5,10,15').split(',') if x],
                'monthly': [float(x) for x in os.getenv('TRADING_THRESHOLDS_MONTHLY', '10,20,30').split(',') if x]
            }
        }
    }
    
    # Log the TP/SL settings specifically for debugging
    logger.info(f"[CONFIG] Take Profit setting: {config['trading']['take_profit']}")
    logger.info(f"[CONFIG] Stop Loss setting: {config['trading']['stop_loss']}")
    
    return config

async def load_and_merge_config() -> dict:
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

        # Initialize MongoDB client early to check for stored config
        mongo_client = MongoClient(
            uri=config['mongodb']['uri'],
            database_name=config['mongodb']['database'],
            driver=config['mongodb'].get('driver', 'motor')
        )
        
        # Load config from database if enabled
        if config['mongodb'].get('load_db_config', True):
            logger.info("Attempting to load trading configuration from database...")
            db_config = await mongo_client.load_trading_config()
            
            if db_config:
                logger.info("Found stored configuration in database, merging...")
                config['trading'].update(db_config)
            else:
                logger.info("No stored configuration found, saving current config to database...")
                await mongo_client.save_trading_config(config)
        
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
        
        # Check validity of all configured pairs
        valid_pairs = []
        invalid_pairs = []
        
        for symbol in config['trading']['pairs']:
            try:
                ticker = await binance_client.client.get_symbol_ticker(symbol=symbol)
                price = float(ticker['price'])
                logger.info(f"🔸 Current {symbol} Price: ${price:,.2f}")
                valid_pairs.append(symbol)
            except Exception as e:
                error_message = str(e)
                if "APIError(code=-1121): Invalid symbol" in error_message:
                    logger.warning(f"❌ Invalid symbol: {symbol} - Removing from trading pairs.")
                    invalid_pairs.append(symbol)
                    # Track invalid symbol
                    binance_client.invalid_symbols.add(symbol)
                    if binance_client.mongo_client:
                        await binance_client.mongo_client.save_invalid_symbol(symbol, error_message)
                else:
                    logger.error(f"❌ Error checking {symbol}: {e}")
        
        # Update config with only valid pairs
        if invalid_pairs:
            logger.warning(f"Removed {len(invalid_pairs)} invalid pairs: {', '.join(invalid_pairs)}")
            config['trading']['pairs'] = valid_pairs
            
        if not valid_pairs:
            logger.error("❌ No valid trading pairs found. Check your configuration.")
            return False
            
        # Save valid pairs to database for persistence
        if binance_client.mongo_client:
            # Check if any trading symbols exist in database
            existing_symbols = await binance_client.mongo_client.get_trading_symbols()
            if not existing_symbols:
                logger.info(f"No trading symbols in database, saving {len(valid_pairs)} validated pairs")
                for symbol in valid_pairs:
                    await binance_client.mongo_client.save_trading_symbol(symbol)
            
        logger.info(f"✅ Found {len(valid_pairs)} valid trading pairs: {', '.join(valid_pairs)}")
        logger.info("=" * 50)
        return True
    except Exception as e:
        logger.error("=" * 50)
        logger.error(f"❌ Failed to connect to Binance: {e}")
        logger.error("=" * 50)
        return False

async def initialize_services(config):
    """Initialize all services with updated Binance API structure"""
    try:
        logger.info("Initializing services...")
        
        # Create MongoDB client with driver choice
        mongo_client = MongoClient(
            uri=config['mongodb']['uri'],
            database_name=config['mongodb']['database'],
            driver=config['mongodb'].get('driver', 'motor')  # Pass the driver
        )
        
        # Initialize indexes - this works with both drivers
        await mongo_client.init_indexes()
        
        # Ensure we use a consistent base currency throughout
        base_currency = config['trading'].get('base_currency', 'USDT')
        
        # Log important configuration information
        logger.info(f"Base Currency: {base_currency}")
        logger.info(f"Reserve Balance: ${config['trading'].get('reserve_balance', 0):,.2f}")
        logger.info(f"Trading Pairs: {', '.join(config['trading']['pairs'])}")
        
        # Check for USDC vs USDT consistency
        for pair in config['trading']['pairs']:
            if not pair.endswith(base_currency):
                logger.warning(f"Pair {pair} doesn't use {base_currency} as base currency. This might cause issues.")
        
        # Determine which API keys to use based on use_testnet setting
        use_testnet = config['binance']['use_testnet']
        api_env = 'spot_testnet' if use_testnet else 'mainnet'
        
        # Initialize Binance client with the appropriate API keys
        binance_client = BinanceClient(
            api_key=config['binance'][api_env]['api_key'],
            api_secret=config['binance'][api_env]['api_secret'],
            testnet=use_testnet,
            mongo_client=mongo_client,
            config=config
        )
        
        # Initialize client connection
        await binance_client.initialize()
        
        # Log reserve balance after initialization to verify
        logger.info(f"[VERIFY] Reserve balance after BinanceClient init: ${binance_client.reserve_balance:,.2f}")
        logger.info(f"[VERIFY] Take Profit setting: {binance_client.default_tp_percentage}%")
        logger.info(f"[VERIFY] Stop Loss setting: {binance_client.default_sl_percentage}%")
        
        # Initialize Telegram bot
        telegram_bot = TelegramBot(
            token=config['telegram']['bot_token'],
            allowed_users=config['telegram']['allowed_users'],
            binance_client=binance_client,
            mongo_client=mongo_client,
            config=config
        )
        
        # Link components
        binance_client.telegram_bot = telegram_bot
        
        # Initialize telegram bot
        await telegram_bot.initialize()
        
        # Verify reserve balance one more time after all initialization (NEW CODE)
        logger.info(f"[VERIFY] Final reserve balance: ${binance_client.reserve_balance:,.2f}")
        
        # Initialize OrderManager
        order_manager = OrderManager(
            binance_client=binance_client,
            mongo_client=mongo_client,
            telegram_bot=telegram_bot,
            config=config
        )
        
        return {
            'mongo_client': mongo_client,
            'binance_client': binance_client,
            'telegram_bot': telegram_bot,
            'order_manager': order_manager
        }
    except Exception as e:
        logger.error(f"Error initializing services: {e}", exc_info=True)
        raise

async def main():
    """Main function with improved config loading"""
    print(DINO_ASCII)
    logger.info("Starting Trade-a-saurus Rex...")
    
    try:
        # Load configuration from appropriate source
        config = await load_and_merge_config()
        
        # Debug log the configuration
        logger.info("=" * 50)
        logger.info("[CONFIG] Active Configuration:")
        logger.info(f"[CONFIG] Environment: {'TESTNET' if config['binance']['use_testnet'] else 'MAINNET'}")
        logger.info(f"[CONFIG] Base Currency: {config['trading']['base_currency']}")
        logger.info(f"[CONFIG] Reserve Balance: ${config['trading']['reserve_balance']:,.2f}")
        logger.info(f"[CONFIG] Trading Pairs: {', '.join(config['trading']['pairs'])}")
        logger.info(f"[CONFIG] Order Amount: ${config['trading']['order_amount']:,.2f}")
        logger.info(f"[CONFIG] Take Profit: {config['trading']['take_profit']}")
        logger.info(f"[CONFIG] Stop Loss: {config['trading']['stop_loss']}")
        logger.info("=" * 50)
        
        if not validate_config(config):
            logger.error("Invalid configuration, exiting...")
            return

        services = await initialize_services(config)
        binance_client = services['binance_client']
        telegram_bot = services['telegram_bot']
        order_manager = services['order_manager']
        
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
