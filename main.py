from binance.client import Client
from binance.enums import *
from datetime import datetime, timedelta, timezone
import pandas as pd
import time
import json
import telegram
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram import BotCommand
from colorama import Fore, Style, init
from binance.exceptions import BinanceAPIException
import asyncio
from concurrent.futures import ThreadPoolExecutor
import psutil  # Add missing import
import logging  # Add missing import
import os  # Add missing import
from pathlib import Path  # Add this import
from config.config_handler import ConfigHandler
from telegram.ext import Application  # Update import

from strategies.price_drop import PriceDropStrategy
from utils.logger import setup_logger
from utils.api_handler import APIHandler  # Replace WebSocketManager import
from utils.rate_limiter import RateLimiter
from utils.telegram_handler import TelegramHandler
from utils.file_handler import AsyncFileHandler  # Add import
import sys  # Add this at the top with other imports

# Initialize colorama
init(autoreset=True)

# Check if running in Docker
IN_DOCKER = os.environ.get('DOCKER', '').lower() == 'true'

# Initialize configuration handling
ConfigHandler.reset_cache()  # Reset any previous cache
config = ConfigHandler.load_config(use_env=IN_DOCKER)

# Initialize mandatory settings
BINANCE_API_KEY = config['BINANCE_API_KEY']
BINANCE_API_SECRET = config['BINANCE_API_SECRET']
TESTNET_API_KEY = config['TESTNET_API_KEY']
TESTNET_API_SECRET = config['TESTNET_API_SECRET']
TRADING_SYMBOLS = config['TRADING_SYMBOLS']

# Initialize optional settings with defaults
TELEGRAM_TOKEN = config.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = config.get('TELEGRAM_CHAT_ID', '')
USE_TELEGRAM = config.get('USE_TELEGRAM', False)

# Remove or comment out this line since TIME_INTERVAL is not used
# TIME_INTERVAL = config['TIME_INTERVAL']

class BinanceBot:
    def __init__(self, config):
        # Update logger initialization to include telegram logger
        self.logger, self.api_logger, self.ws_logger, self.telegram_logger = setup_logger()
        self.logger.info("Initializing BinanceBot...")

        # Store config and required settings
        self.config = config
        self.use_testnet = config.get('USE_TESTNET', True)
        
        # Update Telegram settings initialization
        telegram_settings = config.get('TELEGRAM_SETTINGS', {})
        self.use_telegram = telegram_settings.get('USE_TELEGRAM', False)
        self.telegram_token = telegram_settings.get('TELEGRAM_TOKEN', '')
        self.telegram_chat_id = telegram_settings.get('TELEGRAM_CHAT_ID', '')
        
        self.order_type = config.get('ORDER_TYPE', 'limit')
        self.use_percentage = config.get('USE_PERCENTAGE', False)
        self.trade_amount = config.get('TRADE_AMOUNT', 10)
        self.reserve_balance_usdt = config.get('RESERVE_BALANCE', 2000)
        self.timeframe_config = config.get('TIMEFRAMES', {})
        self.valid_symbols = []
        self.invalid_symbols = []

        # Initialize Telegram handler if enabled and properly configured
        self.telegram_handler = None
        if (self.use_telegram and 
            self.telegram_token and 
            self.telegram_chat_id and 
            self.telegram_token != '' and 
            self.telegram_chat_id != ''):
            
            self.logger.info("Initializing Telegram handler...")
        self.use_percentage = config.get('USE_PERCENTAGE', False)
        self.trade_amount = config.get('TRADE_AMOUNT', 10)
        self.reserve_balance_usdt = config.get('RESERVE_BALANCE', 2000)
        self.timeframe_config = config.get('TIMEFRAMES', {})
        self.valid_symbols = []
        self.invalid_symbols = []

        # Initialize Telegram handler if enabled and properly configured
        self.telegram_handler = None
        if (self.use_telegram and 
            self.telegram_token and 
            self.telegram_chat_id and 
            self.telegram_token != '' and 
            self.telegram_chat_id != ''):
            
            self.logger.info("Initializing Telegram handler...")
            self.telegram_handler = TelegramHandler(
                self.telegram_token,
                self.telegram_chat_id,
                self
            )
            # Pass telegram logger to handler
            self.telegram_handler.logger = self.telegram_logger
            self.logger.info("Telegram handler initialized")

        # Initialize client based on testnet setting
        if self.use_testnet:
            self.client = Client(
                config.get('TESTNET_API_KEY', ''),
                config.get('TESTNET_API_SECRET', ''),
                testnet=True
            )
            self.client.API_URL = 'https://testnet.binance.vision/api'
        else:
            self.client = Client(
                config.get('BINANCE_API_KEY', ''),
                config.get('BINANCE_API_SECRET', '')
            )

        # Add timestamp sync
        self.recv_window = 60000  # Increase from 5000 to 60000
        self.time_offset = 0
        self.last_time_sync = 0
        self.sync_interval = 3600  # Sync every hour
        self.max_timestamp_attempts = 3
        self.start_time = datetime.now()
        self.valid_symbols = []  # Add this to track valid symbols
        self.invalid_symbols = []  # Add this to track invalid symbols
        self.invalid_symbols_file = str(ConfigHandler.get_data_dir() / 'invalid_symbols.txt')  # Update directory handling
        self.order_counter = 0  # Add counter for unique IDs
        self.tax_rate = 0.28  # Add 28% tax rate
        self.symbol_stats = {}  # Track per-symbol statistics
        
        # Add balance tracking attributes
        self.insufficient_balance_timestamp = None
        self.balance_pause_reason = None
        self.balance_check_cooldown = timedelta(hours=24)  # 24-hour cooldown

        # Sync time with Binance servers
        self._sync_server_time()
        
        # Fix the strategy initialization
        self.strategy = PriceDropStrategy(self.timeframe_config)

        # Add GraphGenerator
        from utils.graph_generator import GraphGenerator
        self.graph_generator = GraphGenerator()

        self.last_order_time = {}
        self.orders_placed_today = {}
        self.total_bought = {}
        self.total_spent = {}
        self.orders_placed = {}
        self.total_trades = 0  # Track the total number of trades
        self.max_trades_executed = False
        self.trades_file = 'data/trades.json'
        self.trades = self.load_trades()
        self.executor = ThreadPoolExecutor(max_workers=10)  # For running async tasks
        self.limit_order_timeout = timedelta(hours=8)
        self.next_reset_times = {
            'daily': datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1),
            'weekly': self.get_next_weekly_reset(),
            'monthly': self.get_next_monthly_reset()
        }

        # Initialize orders_placed with correct structure
        self.orders_placed = {
            symbol: {
                timeframe: {} for timeframe in self.timeframe_config.keys()
            } for symbol in TRADING_SYMBOLS
        }

        # Replace WebSocket manager with API handler
        self.api_handler = None  # Initialize as None
        self.last_price_updates = {}

        # Add rate limiting
        self.rate_limiter = RateLimiter(max_requests=1200)  # Set to 1200 to be safe (well under 3000 limit)
        self.price_cache = {}
        self.cache_duration = 1  # Cache duration in seconds

        self.order_check_interval = 60  # Check orders every 60 seconds

        self.trades_file = 'data/trades.json'
        self.trades = self.load_trades()
        self.file_handler = AsyncFileHandler()  # Add this line

        # Initialize pending orders from trades
        self.pending_orders = {
            trade_id: trade['order_metadata'] 
            for trade_id, trade in self.trades.items() 
            if trade['trade_info']['status'] == 'PENDING'
        }
        self.is_shutting_down = False

        # Create data directory and trades file
        self.trades_dir = Path('data')
        self.trades_dir.mkdir(exist_ok=True)
        self.trades_file = self.trades_dir / 'trades.json'
        
        # Initialize trades file if it doesn't exist
        if not self.trades_file.exists():
            with open(self.trades_file, 'w') as f:
                json.dump({}, f)
        
        self.trades = self.load_trades()

    async def shutdown(self):
        """Enhanced graceful shutdown sequence with error recovery"""
        if self.is_shutting_down:
            return
            
        self.is_shutting_down = True
        self.logger.info("Initiating clean shutdown sequence...")
        
        cleanup_errors = []
        
        try:
            cleanup_tasks = []

            # Stop API handler first
            if self.api_handler:
                cleanup_tasks.append(self.api_handler.stop())

            # Try to save current state
            try:
                await self._save_trades_atomic()
            except Exception as e:
                cleanup_errors.append(f"Failed to save trades: {e}")
                # Try alternate save location
                try:
                    alt_path = os.path.join(os.path.expanduser('~'), 'binance_bot_backup.json')
                    await self.file_handler.save_json_atomic(alt_path, self.trades)
                    self.logger.info(f"Trades saved to alternate location: {alt_path}")
                except Exception as alt_e:
                    cleanup_errors.append(f"Failed to save backup: {alt_e}")

            # Stop Telegram bot
            if self.telegram_handler:
                cleanup_tasks.append(self.telegram_handler.shutdown())

            # Wait for cleanup tasks with timeout
            if cleanup_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*cleanup_tasks, return_exceptions=True),
                        timeout=15
                    )
                except asyncio.TimeoutError:
                    cleanup_errors.append("Cleanup tasks timed out")
                    
        except Exception as e:
            cleanup_errors.append(f"Error during shutdown: {e}")
        finally:
            if cleanup_errors:
                self.logger.error("Shutdown completed with errors:\n" + "\n".join(cleanup_errors))
            else:
                self.logger.info("Shutdown sequence completed successfully. Orders preserved.")

    def _sync_server_time(self):
        """Synchronize local time with Binance server time"""
        try:
            for _ in range(self.max_timestamp_attempts):
                # Remove timestamp from get_server_time call
                server_time = self.client.get_server_time()
                local_time = int(time.time() * 1000)
                self.time_offset = server_time['serverTime'] - local_time
                
                # Additional verification step for testnet
                if hasattr(self.client, 'API_URL') and 'testnet' in self.client.API_URL:
                    # Add a small buffer for testnet latency
                    self.time_offset += 1000  # Add 1 second buffer
                
                self.last_time_sync = time.time()
                self.logger.info(f"Time synchronized. Offset: {self.time_offset}ms")
                return True
                    
            raise Exception("Failed to synchronize time after multiple attempts")
            
        except Exception as e:
            self.logger.error(f"Error synchronizing time: {e}")
            return False

    def _get_timestamp(self):
        """Get current timestamp with server offset"""
        return int(time.time() * 1000) + self.time_offset

    def _check_time_sync(self):
        """Check if time needs to be resynced"""
        if time.time() - self.last_time_sync > self.sync_interval:
            self._sync_server_time()

    async def _make_api_call(self, func, *args, _no_timestamp=False, **kwargs):
        """Enhanced API call wrapper with timestamp control"""
        await self.rate_limiter.acquire()
        
        start_time = time.time()
        log_data = {
            'request_data': {
                'function': func.__name__,
                'args': args,
                'kwargs': {k: v for k, v in kwargs.items() if not k.lower() in ['apikey', 'secret', 'token']}
            },
            'response_data': None,
            'duration': 0
        }
        
        try:
            # Only add timestamp if _no_timestamp is False
            if not _no_timestamp:
                self._check_time_sync()
                kwargs['timestamp'] = self._get_timestamp()
            
            response = func(*args, **kwargs)
            duration = (time.time() - start_time) * 1000
            
            log_data.update({
                'response_data': self._sanitize_response(response),
                'duration': duration
            })
            
            self.api_logger.debug(
                "API Call completed successfully",
                extra=log_data
            )
            
            return response
            
        except Exception as e:
            duration = (time.time() - start_time) * 1000
            log_data.update({
                'response_data': {
                    'error_type': type(e).__name__,
                    'error_message': str(e)
                },
                'duration': duration
            })
            self.api_logger.error(
                "API Call failed with unexpected error",
                extra=log_data
            )
            raise

    def _sanitize_response(self, response):
        """Sanitize response data for logging"""
        if isinstance(response, dict):
            return {
                k: v for k, v in response.items()
                if not any(sensitive in k.lower() for sensitive in ['key', 'secret', 'token', 'password'])
            }
        return response

    async def get_cached_price(self, symbol):
        """Get cached price or fetch new one"""
        current_time = time.time()
        
        if (symbol in self.price_cache and
            current_time - self.price_cache[symbol]['timestamp'] < self.cache_duration):
            return self.price_cache[symbol]['price']
        
        # Get price without timestamp
        ticker = await self._make_api_call(
            self.client.get_symbol_ticker,
            symbol=symbol,
            _no_timestamp=True  # Add flag to skip timestamp
        )
        price = float(ticker['price'])
        
        self.price_cache[symbol] = {
            'price': price,
            'timestamp': current_time
        }
        
        return price

    def load_trades(self):
        """Load trades with new structure"""
        try:
            if os.path.exists(self.trades_file):
                with open(self.trades_file, 'r') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            self.logger.error(f"Error loading trades: {e}")
            return {}

    async def verify_pending_orders(self):
        """Verify all pending orders using single source of truth"""
        try:
            # Get all pending orders
            pending_trades = {
                trade_id: trade for trade_id, trade in self.trades.items()
                if trade['trade_info']['status'] == 'PENDING'
            }
            
            for trade_id, trade in pending_trades.items():
                try:
                    symbol = trade['trade_info']['symbol']
                    order_id = trade['order_metadata']['order_id']
                    
                    # Get order status
                    order_status = await self._get_order_status_with_retry(symbol, order_id)
                    
                    if not order_status:
                        continue
                        
                    # Update trade based on status
                    if order_status['status'] == 'FILLED':
                        self.trades[trade_id]['trade_info'].update({
                            'status': 'FILLED',
                            'filled_time': datetime.now(timezone.utc).isoformat(),
                            'actual_price': float(order_status['price']),
                            'actual_quantity': float(order_status['executedQty'])
                        })
                    elif order_status['status'] == 'CANCELED':
                        self.trades[trade_id]['trade_info']['status'] = 'CANCELLED'
                    
                    # Update last check time
                    self.trades[trade_id]['order_metadata']['last_check'] = datetime.now(timezone.utc).isoformat()
                    
                except Exception as e:
                    self.logger.error(f"Error processing order {trade_id}: {e}")
            
            # Save updated trades
            await self._save_trades_atomic()
            
        except Exception as e:
            self.logger.error(f"Error in verify_pending_orders: {e}")

    async def _get_order_status_with_retry(self, symbol, order_id, max_retries=3):
        """Get order status with retries"""
        for attempt in range(max_retries):
            try:
                return await self._make_api_call(
                    self.client.get_order,
                    symbol=symbol,
                    orderId=order_id,
                    recvWindow=self.recv_window
                )
            except BinanceAPIException as e:
                if e.code == -2013:  # Order does not exist
                    return None
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(1)

    async def _remove_processed_orders(self, order_ids):
        """Remove processed orders atomically"""
        try:
            # Create new dict without processed orders
            updated_orders = {
                k: v for k, v in self.pending_orders.items()
                if k not in order_ids
            }
            
            # Save atomically using file handler
            await self.file_handler.save_json_atomic(self.orders_file, updated_orders)
            
            # Update memory state only after successful save
            self.pending_orders = updated_orders
            
        except Exception as e:
            self.logger.error(f"Error removing processed orders: {e}")
            raise

    async def _handle_filled_order(self, symbol, order_status):
        """Handle filled order with improved verification"""
        try:
            quantity = float(order_status['executedQty'])
            price = float(order_status['price'])
            order_id = order_status['orderId']
            
            # Verify the order exists in our tracking
            bot_order_id = None
            for id, info in self.pending_orders.items():
                if info['orderId'] == order_id:
                    bot_order_id = id
                    break
                    
            if not bot_order_id:
                self.logger.warning(f"Order {order_id} filled but not found in pending orders")
                return
                
            # Update trades first
            if bot_order_id in self.trades:
                self.trades[bot_order_id].update({
                    'status': 'FILLED',
                    'filled_time': datetime.now(timezone.utc).isoformat(),
                    'actual_price': price,
                    'actual_quantity': quantity
                })
                await self._save_trades_atomic()
            
            # Log the successful fill
            fill_msg = (
                f"✅ Order filled and verified:\n"
                f"ID: {bot_order_id}\n"
                f"Symbol: {symbol}\n"
                f"Quantity: {quantity}\n"
                f"Price: {price}"
            )
            self.logger.info(fill_msg)
            
            if self.telegram_handler:
                await self.telegram_handler.send_message(fill_msg)
                
        except Exception as e:
            self.logger.error(f"Error handling filled order: {e}")
            raise

    async def _save_trades_atomic(self):
        """Save trades atomically with initialization"""
        try:
            # Ensure trades file exists
            if not self.trades_file.exists():
                self.trades_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.trades_file, 'w') as f:
                    json.dump({}, f)
                    
            await self.file_handler.save_json_atomic(self.trades_file, self.trades)
            
        except Exception as e:
            self.logger.error(f"Error saving trades: {e}")
            raise

    async def execute_trade(self, symbol, price):
        """Execute trade with improved feedback"""
        try:
            if not await self.check_balance_status():
                return False

            print(f"\n🎯 Trade opportunity detected for {symbol}")
            
            # Get available balance
            available_usdt = await self.get_available_usdt()
            if available_usdt < self.trade_amount:
                print(f"\n⚠️ Insufficient balance: {available_usdt:.2f} USDT")
                return False

            # Calculate trade amount
            trade_amount = (
                available_usdt * self.trade_amount 
                if self.use_percentage 
                else min(self.trade_amount, available_usdt)
            )

            print(f"💰 Trade amount: {trade_amount:.2f} USDT")

            # Get symbol info from API handler
            symbol_info = await self.api_handler.get_symbol_info(symbol)
            if not symbol_info:
                raise ValueError(f"Symbol info not found for {symbol}")

            # Create and execute order
            order = await self._create_order(symbol, price, trade_amount, symbol_info)
            if not order:
                return False

            # Log successful order
            print(f"\n✅ Order placed successfully for {symbol}")
            print(f"   Price: {order['price']}")
            print(f"   Quantity: {order['origQty']}")
            print(f"   Total: {float(order['price']) * float(order['origQty']):.2f} USDT")

            return True

        except Exception as e:
            self.logger.error(f"Trade execution failed for {symbol}: {e}")
            print(f"\r❌ Trade failed for {symbol}: {e}")
            return False

    async def _create_order(self, symbol, price, amount, symbol_info):
        """Create order with proper formatting"""
        try:
            # Format price and quantity
            formatted_price, formatted_quantity = await self.api_handler._format_order_amounts(
                symbol_info, price, amount
            )

            # Create order parameters
            order_params = {
                'symbol': symbol,
                'side': SIDE_BUY,
                'recvWindow': self.recv_window
            }

            if self.order_type == "limit":
                order_params.update({
                    'type': ORDER_TYPE_LIMIT,
                    'timeInForce': TIME_IN_FORCE_GTC,
                    'price': formatted_price,
                    'quantity': formatted_quantity
                })
            else:
                order_params.update({
                    'type': ORDER_TYPE_MARKET,
                    'quoteOrderQty': f"{amount:.2f}"
                })

            # Add timestamp and execute order
            order_params['timestamp'] = self._get_timestamp()
            return await self._make_api_call(
                self.client.create_order,
                **order_params
            )

        except Exception as e:
            self.logger.error(f"Order creation failed: {e}")
            raise

    async def monitor_order(self, trade_id):
        """Monitor order with new structure"""
        try:
            trade = self.trades[trade_id]
            symbol = trade['trade_info']['symbol']
            order_id = trade['order_metadata']['order_id']
            cancel_time = datetime.fromisoformat(trade['order_metadata']['cancel_time'])
            
            while True:
                now = datetime.now(timezone.utc)
                
                # Check for timeout
                if now >= cancel_time:
                    await self._cancel_order(symbol, order_id)
                    trade['trade_info']['status'] = 'CANCELLED'
                    await self._save_trades_atomic()
                    break
                
                # Get order status
                order_status = await self._get_order_status_with_retry(symbol, order_id)
                
                if not order_status:
                    self.logger.warning(f"Order {order_id} not found, assuming cancelled")
                    trade['trade_info']['status'] = 'CANCELLED'
                    await self._save_trades_atomic()
                    break
                
                if order_status['status'] == 'FILLED':
                    # Update trade info
                    trade['trade_info'].update({
                        'status': 'FILLED',
                        'filled_time': now.isoformat(),
                        'actual_price': float(order_status['price']),
                        'actual_quantity': float(order_status['executedQty'])
                    })
                    await self._save_trades_atomic()
                    
                    # Log successful fill
                    fill_msg = (
                        f"✅ Order filled:\n"
                        f"Symbol: {symbol}\n"
                        f"Price: {float(order_status['price'])}\n"
                        f"Quantity: {float(order_status['executedQty'])}"
                    )
                    self.logger.info(fill_msg)
                    if self.telegram_handler:
                        await self.telegram_handler.send_message(fill_msg)
                    break
                    
                await asyncio.sleep(10)
                
        except Exception as e:
            self.logger.error(f"Error monitoring order {trade_id}: {e}")
            if trade_id in self.trades:
                self.trades[trade_id]['trade_info']['status'] = 'ERROR'
                await self._save_trades_atomic()

    def get_historical_data(self, symbol, interval, start_str):
        """Get historical data with error handling"""
        try:
            klines = self.client.get_historical_klines(
                symbol,
                interval,
                start_str
            )
            
            if not klines:
                return None
                
            df = pd.DataFrame(
                klines,
                columns=[
                    'timestamp', 'open', 'high', 'low', 'close',
                    'volume', 'close_time', 'quote_av', 'trades',
                    'tb_base_av', 'tb_quote_av', 'ignore'
                ]
            )
            
            # Convert string values to float
            for col in ['open', 'high', 'low', 'close']:
                df[col] = df[col].astype(float)
                
            return df
            
        except Exception as e:
            self.logger.error(f"Error fetching historical data for {symbol}: {str(e)}")
            return None

    def get_daily_open_price(self, symbol):
        df = self.get_historical_data(symbol, Client.KLINE_INTERVAL_1DAY, "1 day ago UTC")
        return float(df['open'].iloc[-1])

    def print_daily_open_price(self):
        for symbol in self.valid_symbols:  # Changed from TRADING_SYMBOLS
            daily_open_price = self.get_daily_open_price(symbol)
            print(f"Daily open price for {symbol} at 00:00 UTC: {daily_open_price}")
            self.logger.info(f"Daily open price for {symbol} at 00:00 UTC: {daily_open_price}")

    def get_balance(self, asset=None):
        """Get balance for specific asset or all assets with improved timestamp handling"""
        try:
            # Ensure time is synced
            self._sync_server_time()
            
            # Use a larger recvWindow for testnet
            recv_window = 60000 if hasattr(self.client, 'API_URL') and 'testnet' in self.client.API_URL else self.recv_window
            
            timestamp = self._get_timestamp()
            
            # Try up to 3 times with increasing recvWindow
            for attempt in range(3):
                try:
                    balances = self.client.get_account(
                        recvWindow=recv_window * (attempt + 1),
                        timestamp=timestamp
                    )['balances']
                    
                    # Convert to dictionary for easier access
                    balance_report = {}
                    for balance in balances:
                        free = float(balance['free'])
                        locked = float(balance['locked'])
                        total = free + locked
                        if total > 0:  # Add missing colon
                            balance_report[balance['asset']] = {
                                'free': free,
                                'locked': locked,
                                'total': total
                            }
                    
                    # Return specific asset balance if requested
                    if asset:
                        return balance_report.get(asset, None)
                    return balance_report
                    
                except BinanceAPIException as e:
                    if e.code == -1021 and attempt < 2:  # Add missing colon
                        self._sync_server_time()
                        timestamp = self._get_timestamp()
                        continue
                    raise
                    
        except Exception as e:
            self.logger.error(f"Error fetching balance: {str(e)}")
            return None

    def print_balance_report(self):
        balance_report = self.get_balance()
        if balance_report:
            print(Fore.BLUE + "Balance Report:")
            for asset, total in balance_report.items():
                print(Fore.BLUE + f"{asset}: {total}")
            self.logger.info("Balance Report:")
            for asset, total in balance_report.items():
                self.logger.info(f"{asset}: {total}")

    def ensure_utc(self, dt):
        """Ensure datetime is UTC aware"""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    async def verify_pending_orders(self):
        """Verify all pending orders with improved safety"""
        try:
            # Create a snapshot of orders to process
            orders_to_process = list(self.pending_orders.items())
            processed_orders = set()
            
            for bot_order_id, order_info in orders_to_process:
                try:
                    symbol = order_info['symbol']
                    order_id = order_info['orderId']
                    
                    # Get order status with retries
                    order_status = await self._get_order_status_with_retry(symbol, order_id)
                    
                    if not order_status:
                        continue
                        
                    # Process order based on status
                    if order_status['status'] == 'FILLED':
                        await self._handle_filled_order(symbol, order_status)
                        processed_orders.add(bot_order_id)
                    elif order_status['status'] == 'CANCELED':
                        processed_orders.add(bot_order_id)
                    elif order_status['status'] == 'NEW':
                        # Check for timeout
                        placed_time = datetime.fromisoformat(order_info['placed_time'])
                        if datetime.now(timezone.utc) - placed_time > self.limit_order_timeout:
                            if await self._cancel_order(symbol, order_id):
                                processed_orders.add(bot_order_id)
                                
                except Exception as e:
                    self.logger.error(f"Error processing order {bot_order_id}: {e}")
            
            # Remove processed orders atomically
            if processed_orders:
                await self._remove_processed_orders(processed_orders)
            
        except Exception as e:
            self.logger.error(f"Error in verify_pending_orders: {e}")

    async def _get_order_status_with_retry(self, symbol, order_id, max_retries=3):
        """Get order status with retries"""
        for attempt in range(max_retries):
            try:
                return await self._make_api_call(
                    self.client.get_order,
                    symbol=symbol,
                    orderId=order_id,
                    recvWindow=self.recv_window
                )
            except BinanceAPIException as e:
                if e.code == -2013:  # Order does not exist
                    return None
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(1)

    async def _remove_processed_orders(self, order_ids):
        """Remove processed orders atomically"""
        try:
            # Create new dict without processed orders
            updated_orders = {
                k: v for k, v in self.pending_orders.items()
                if k not in order_ids
            }
            
            # Save atomically using file handler
            await self.file_handler.save_json_atomic(self.orders_file, updated_orders)
            
            # Update memory state only after successful save
            self.pending_orders = updated_orders
            
        except Exception as e:
            self.logger.error(f"Error removing processed orders: {e}")
            raise

    async def _handle_filled_order(self, symbol, order_status):
        """Handle filled order with improved verification"""
        try:
            quantity = float(order_status['executedQty'])
            price = float(order_status['price'])
            order_id = order_status['orderId']
            
            # Verify the order exists in our tracking
            bot_order_id = None
            for id, info in self.pending_orders.items():
                if info['orderId'] == order_id:
                    bot_order_id = id
                    break
                    
            if not bot_order_id:
                self.logger.warning(f"Order {order_id} filled but not found in pending orders")
                return
                
            # Update trades first
            if bot_order_id in self.trades:
                self.trades[bot_order_id].update({
                    'status': 'FILLED',
                    'filled_time': datetime.now(timezone.utc).isoformat(),
                    'actual_price': price,
                    'actual_quantity': quantity
                })
                await self._save_trades_atomic()
            
            # Log the successful fill
            fill_msg = (
                f"✅ Order filled and verified:\n"
                f"ID: {bot_order_id}\n"
                f"Symbol: {symbol}\n"
                f"Quantity: {quantity}\n"
                f"Price: {price}"
            )
            self.logger.info(fill_msg)
            
            if self.telegram_handler:
                await self.telegram_handler.send_message(fill_msg)
                
        except Exception as e:
            self.logger.error(f"Error handling filled order: {e}")
            raise

    async def _save_trades_atomic(self):
        """Save trades atomically with initialization"""
        try:
            # Ensure trades file exists
            if not self.trades_file.exists():
                self.trades_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.trades_file, 'w') as f:
                    json.dump({}, f)
                    
            await self.file_handler.save_json_atomic(self.trades_file, self.trades)
            
        except Exception as e:
            self.logger.error(f"Error saving trades: {e}")
            raise

    async def monitor_order(self, bot_order_id, symbol, order_id, price, placed_time):
        """Asynchronously monitor an order until it's filled"""
        try:
            placed_time = self.ensure_utc(placed_time)
            last_balance_check = None
            
            while True:
                now = datetime.now(timezone.utc)
                
                # Check for timeout
                if now - placed_time > self.limit_order_timeout:
                    await self._cancel_order(symbol, order_id)
                    break

                # Get order status
                order_status = self.client.get_order(
                    symbol=symbol,
                    orderId=order_id,
                    recvWindow=self.recv_window
                )
                
                if order_status['status'] == 'FILLED':
                    # Get balances before processing
                    base_asset = symbol.replace('USDT', '')
                    
                    # Verify balance changes
                    new_balance = await self._get_verified_balance(base_asset)
                    usdt_balance = await self._get_verified_balance('USDT')
                    
                    if new_balance and usdt_balance:
                        quantity = float(order_status['executedQty'])
                        executed_price = float(order_status['price'])
                        total_cost = quantity * executed_price
                        
                        # Update tracking
                        self.total_bought[symbol] = self.total_bought.get(symbol, 0) + quantity
                        self.total_spent[symbol] = self.total_spent.get(symbol, 0) + total_cost
                        self.total_trades += 1
                        
                        # Create fill message
                        fill_msg = (
                            f"✅ Order filled for {symbol} [ID: {bot_order_id}]:\n"
                            f"Quantity: {quantity:.8f}\n"
                            f"Price: {executed_price:.8f} USDT\n"
                            f"Total Cost: {total_cost:.2f} USDT\n\n"
                            f"Updated Balances:\n"
                            f"• {base_asset}: {new_balance['total']:.8f}\n"
                            f"• USDT: {usdt_balance['free']:.2f}"
                        )
                        
                        # Log and notify
                        print(f"{Fore.GREEN}Order filled for {symbol}")
                        self.logger.info(f"Order filled: {order_status}")
                        
                        # Send Telegram message as separate task
                        if self.telegram_handler:
                            asyncio.create_task(self.telegram_handler.send_message(fill_msg))
                    
                    break
                    
                elif order_status['status'] == 'CANCELED':
                    self.logger.info(f"Order {order_id} for {symbol} was canceled")
                    break
                
                await asyncio.sleep(10)
                
        except Exception as e:
            self.logger.error(f"Error monitoring order: {e}")
            print(f"{Fore.RED}Error monitoring order: {e}")
        finally:
            # Clean up pending order
            if bot_order_id in self.pending_orders:
                del self.pending_orders[bot_order_id]
                self.save_pending_orders()

    async def get_available_usdt(self):
        """Enhanced balance check with reserve protection"""
        try:
            self._check_time_sync()  # Check time sync before request
            
            timestamp = self._get_timestamp()
            
            for attempt in range(self.max_timestamp_attempts):
                try:
                    balance = self.client.get_asset_balance(
                        asset='USDT',
                        recvWindow=self.recv_window,
                        timestamp=timestamp
                    )
                    
                    total_usdt = float(balance['free'])
                    available_usdt = total_usdt - self.reserve_balance_usdt
                    
                    if total_usdt < self.reserve_balance_usdt:
                        self.balance_pause_reason = "reserve"
                        return 0
                    return max(available_usdt, 0)
                    
                except BinanceAPIException as e:
                    if e.code == -1021:  # Timestamp error
                        if attempt < self.max_timestamp_attempts - 1:
                            self._sync_server_time()
                            timestamp = self._get_timestamp()
                            continue
                    raise
                    
        except Exception as e:
            self.logger.error(f"Error getting USDT balance: {e}")
            return 0

    async def check_balance_status(self):
        """Check if trading should be paused due to balance"""
        if self.insufficient_balance_timestamp:
            time_since_insufficient = datetime.now(timezone.utc) - self.insufficient_balance_timestamp
            if time_since_insufficient < self.balance_check_cooldown:
                remaining_time = self.balance_check_cooldown - time_since_insufficient
                hours, remainder = divmod(remaining_time.seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                pause_message = (
                    f"{Fore.YELLOW}Trading paused: "
                    f"{'Below reserve' if self.balance_pause_reason == 'reserve' else 'Insufficient balance'}. "
                    f"Resuming in {hours}h {minutes}m {seconds}s"
                )
                print(pause_message)
                return False
            else:
                self.insufficient_balance_timestamp = None
                self.balance_pause_reason = None
        return True

    def generate_order_id(self, symbol):
        """Generate a unique order ID"""
        self.order_counter += 1
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
        return f"BOT_{timestamp}_{symbol}_{self.order_counter}"

    async def execute_trade(self, symbol, price):
        """Execute trade with proper order tracking"""
        try:
            # Check balance status first
            if not await self.check_balance_status():
                return

            # Use cached price instead of fetching new one
            current_price = await self.get_cached_price(symbol)
            
            # Format price with exact decimal places based on symbol info
            if not hasattr(self, 'symbol_info_cache'):
                self.symbol_info_cache = {}
                exchange_info = await self._make_api_call(self.client.get_exchange_info)
                for info in exchange_info['symbols']:
                    self.symbol_info_cache[info['symbol']] = info

            symbol_info = self.symbol_info_cache.get(symbol)
            if not symbol_info:
                raise ValueError(f"Symbol info not found for {symbol}")

            # Get price filter for precision
            price_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'PRICE_FILTER'), None)
            if not price_filter:
                raise ValueError(f"Price filter not found for {symbol}")

            tick_size = float(price_filter['tickSize'])
            price_precision = len(str(tick_size).rstrip('0').split('.')[-1])
            formatted_price = f"{current_price:.{price_precision}f}"

            # Check and handle insufficient balance
            available_usdt = await self.get_available_usdt()
            if available_usdt < self.trade_amount:
                # Set cooldown timestamp and reason
                self.insufficient_balance_timestamp = datetime.now(timezone.utc)
                self.balance_pause_reason = "insufficient" if available_usdt > 0 else "reserve"
                
                # Cancel all pending orders
                await self.cancel_all_orders()
                
                pause_message = (
                    "🚨 Trading paused for 24 hours\n"
                    f"Reason: {'Balance below reserve' if self.balance_pause_reason == 'reserve' else 'Insufficient balance'}\n"
                    f"Available: {available_usdt} USDT\n"
                    f"Required: {self.trade_amount} USDT\n"
                    f"Reserve: {self.reserve_balance_usdt} USDT\n"
                    "All pending orders have been cancelled."
                )
                
                print(f"{Fore.YELLOW}{pause_message}")
                if self.telegram_handler:
                    await self.telegram_handler.send_message(pause_message)
                return

            # Calculate trade amount
            trade_amount = available_usdt * self.trade_amount if self.use_percentage else min(self.trade_amount, available_usdt)
            
            # Get lot size filter for quantity precision
            lot_size_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
            if not lot_size_filter:
                raise ValueError(f"Lot size filter not found for {symbol}")

            step_size = float(lot_size_filter['stepSize'])
            quantity_precision = len(str(step_size).rstrip('0').split('.')[-1])
            quantity = (trade_amount / current_price)
            quantity = round(quantity - (quantity % float(step_size)), quantity_precision)
            formatted_quantity = f"{quantity:.{quantity_precision}f}"

            # Create order parameters with proper validation
            order_params = {
                'symbol': symbol,
                'side': SIDE_BUY,
                'recvWindow': self.recv_window
            }

            if self.order_type == "limit":
                order_params.update({
                    'type': ORDER_TYPE_LIMIT,
                    'timeInForce': TIME_IN_FORCE_GTC,
                    'price': formatted_price,
                    'quantity': formatted_quantity
                })
            else:
                # Market order: remove 'price'/'quantity', only use quoteOrderQty
                order_params.update({
                    'type': ORDER_TYPE_MARKET,
                    'quoteOrderQty': f"{trade_amount:.{quantity_precision}f}"
                })

            # Add timestamp last
            order_params['timestamp'] = self._get_timestamp()

            # Log the order parameters before sending
            self.api_logger.debug(
                f"Preparing order - Symbol: {symbol}\n"
                f"Order Parameters: {order_params}"
            )

            # Place order with rate limiting
            order = await self._make_api_call(self.client.create_order, **order_params)
            
            # Log the successful order
            self.api_logger.info(
                f"Order placed successfully:\n"
                f"Order ID: {order['orderId']}\n"
                f"Symbol: {symbol}\n"
                f"Type: {order_params['type']}\n"
                f"Side: {order_params['side']}\n"
                f"Quantity: {formatted_quantity if 'quantity' in order_params else 'N/A'}\n"
                f"Price: {formatted_price if 'price' in order_params else 'MARKET'}\n"
                f"Quote Quantity: {order_params.get('quoteOrderQty', 'N/A')}"
            )

            # Create trade entry with new structure
            order_time = datetime.now(timezone.utc)
            cancel_time = order_time + self.limit_order_timeout
            
            bot_order_id = self.generate_order_id(symbol)  # Add this line to generate order ID
            
            self.trades[bot_order_id] = {
                'trade_info': {
                    'symbol': symbol,
                    'entry_price': float(formatted_price),
                    'quantity': float(formatted_quantity),
                    'total_cost': float(formatted_price) * float(formatted_quantity),
                    'current_value': None,
                    'profit_usdt': None,
                    'profit_percentage': None,
                    'status': 'PENDING',
                    'type': 'bot'
                },
                'order_metadata': {
                    'order_id': order['orderId'],  # Fix: orderId instead of OrderId
                    'placed_time': order_time.isoformat(),
                    'cancel_time': cancel_time.isoformat(),
                    'last_check': order_time.isoformat()
                }
            }
            
            # Save trades immediately
            await self._save_trades_atomic()
            
            # Start monitoring task
            asyncio.create_task(self.monitor_order(bot_order_id))
            
            return True
            
        except BinanceAPIException as e:
            self.logger.error(f"Binance API error in execute_trade: {str(e)}")
            print(f"{Fore.RED}Binance API error in execute_trade: {str(e)}")
            return False
            
        except Exception as e:
            self.api_logger.error(f"Trade execution failed:\nSymbol: {symbol}\nError: {str(e)}")
            print(f"{Fore.RED}Error executing trade for {symbol}: {str(e)}")
            # Clean up if something went wrong
            if 'bot_order_id' in locals() and bot_order_id in self.trades:
                del self.trades[bot_order_id]
                await self._save_trades_atomic()
            return False

    async def handle_price_update(self, symbol, price):
        """Handle price updates with direct API calls"""
        try:
            # Get reference prices
            ref_prices = self.get_reference_prices(symbol)
            
            # Create DataFrame for strategy
            df = pd.DataFrame({
                'symbol': [symbol],
                'close': [price]
            })
            
            # Generate signals
            signals = self.strategy.generate_signals(
                df,
                ref_prices,
                datetime.now(timezone.utc)
            )
            
            # Execute trades for valid signals
            for timeframe, threshold, signal_price in signals:
                if await self.check_balance_status():
                    await self.execute_trade(symbol, price)
                    
        except Exception as e:
            self.logger.error(f"Error handling price update for {symbol}: {e}")

    async def check_prices(self):
        """Check prices for all symbols"""
        try:
            for symbol in self.valid_symbols:
                ticker = await self._make_api_call(
                    self.client.get_symbol_ticker,
                    symbol=symbol,
                    _no_timestamp=True
                )
                price = float(ticker['price'])
                await self.handle_price_update(symbol, price)
                await asyncio.sleep(0.5)  # Add small delay between symbols
                
        except Exception as e:
            self.logger.error(f"Error checking prices: {e}")

    async def main_loop(self):
        """Main bot loop with regular API calls"""
        try:
            # Perform startup checks
            if not await self.startup_checks():
                raise Exception("Startup checks failed")

            # Initialize price checking
            self.api_handler = APIHandler(self.client, self.valid_symbols, self.logger)
            await self.api_handler.start()

            while True:
                try:
                    # Check for resets
                    await self.check_and_handle_resets()
                    
                    # Check prices every 2 seconds
                    await self.check_prices()
                    
                    await asyncio.sleep(2)
                    
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self.logger.error(f"Error in main loop: {e}")
                    await asyncio.sleep(5)
                    
        except Exception as e:
            self.logger.error(f"Fatal error in main loop: {e}")
            raise
        finally:
            if self.api_handler:
                await self.api_handler.stop()

    async def handle_price_update(self, symbol, price):
        """Handle real-time price updates"""
        try:
            # Price is already a float, no need to convert
            df = pd.DataFrame({
                'symbol': [symbol],
                'close': [price]  # Use price directly
            })
            
            # Get reference prices
            reference_prices = self.get_reference_prices(symbol)
            if not reference_prices:
                return  # Skip if no reference prices available
            
            # Generate trading signals
            signals = self.strategy.generate_signals(
                df,
                reference_prices,
                datetime.now(timezone.utc)
            )
            
            # Execute trades for valid signals
            for timeframe, threshold, signal_price in signals:
                await self.execute_trade(symbol, price)  # Use current price

        except Exception as e:
            self.logger.error(f"Error handling price update for {symbol}: {e}")

    async def startup_checks(self):
        """Perform startup checks and verifications"""
        try:
            print(f"{Fore.CYAN}Performing startup checks...")
            
            # Verify trades and pending orders are in sync
            pending_count = len(self.pending_orders)
            pending_in_trades = len([
                t for t in self.trades.values() 
                if t['trade_info']['status'] == 'PENDING'
            ])
            
            if pending_count != pending_in_trades:
                print(f"{Fore.YELLOW}Syncing pending orders with trades...")
                # Re-sync pending orders from trades
                self.pending_orders = {
                    trade_id: trade['order_metadata']
                    for trade_id, trade in self.trades.items()
                    if trade['trade_info']['status'] == 'PENDING'
                }
            
            if pending_count > 0:
                print(f"{Fore.YELLOW}Found {pending_count} pending orders. Verifying status...")
                await self.verify_pending_orders()
                print(f"{Fore.GREEN}Order verification complete!")
            
            # Update next reset times
            self.next_reset_times = {
                'daily': datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1),
                'weekly': self.get_next_weekly_reset(),
                'monthly': self.get_next_monthly_reset()
            }
            
            print(f"{Fore.GREEN}Startup checks completed successfully!")
            return True
            
        except Exception as e:
            print(f"{Fore.RED}Error during startup checks: {e}")
            self.logger.error(f"Startup checks failed: {e}")
            return False

    async def check_and_handle_resets(self):
        """Check for timeframe resets and notify"""
        now = datetime.now(timezone.utc)
        reset_messages = []

        for timeframe, reset_time in self.next_reset_times.items():
            if now >= reset_time:
                # Generate reset message
                message = await self._generate_reset_overview(timeframe)
                reset_messages.append(message)
                
                # Update next reset time
                if timeframe == 'daily':
                    self.next_reset_times[timeframe] = reset_time + timedelta(days=1)
                elif timeframe == 'weekly':
                    self.next_reset_times[timeframe] = reset_time + timedelta(days=7)
                else:  # monthly
                    # Calculate first day of next month
                    if reset_time.month == 12:
                        next_month = datetime(reset_time.year + 1, 1, 1, tzinfo=timezone.utc)
                    else:
                        next_month = datetime(reset_time.year, reset_time.month + 1, 1, tzinfo=timezone.utc)
                    self.next_reset_times[timeframe] = next_month

        # Send all reset messages
        if reset_messages:
            combined_message = "\n\n".join(reset_messages)
            print(f"\n{Fore.CYAN}{combined_message}")
            if self.telegram_handler:
                await self.telegram_handler.send_message(combined_message)

    async def _generate_reset_overview(self, timeframe):
        """Generate reset overview with opens for all symbols"""
        try:
            header = f"🔄 {timeframe.capitalize()} Reset Overview\n"
            header += f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            
            # Get opens for all symbols
            opens = []
            for symbol in self.valid_symbols:
                ticker = await self._make_api_call(self.client.get_symbol_ticker, symbol=symbol)
                current_price = float(ticker['price'])
                
                # Get previous timeframe data
                if timeframe == 'daily':
                    interval = Client.KLINE_INTERVAL_1DAY
                    lookback = "2 days ago UTC"
                elif timeframe == 'weekly':
                    interval = Client.KLINE_INTERVAL_1WEEK
                    lookback = "2 weeks ago UTC"
                else:
                    interval = Client.KLINE_INTERVAL_1MONTH
                    lookback = "2 months ago UTC"
                
                historical = self.get_historical_data(symbol, interval, lookback)
                previous_open = float(historical['open'].iloc[-1])
                
                # Calculate change
                change = ((current_price - previous_open) / previous_open) * 100
                arrow = "↑" if change >= 0 else "↓"
                
                opens.append({
                    'symbol': symbol,
                    'price': current_price,
                    'previous_open': previous_open,
                    'change': change,
                    'arrow': arrow
                })
            
            # Sort by change percentage
            opens.sort(key=lambda x: x['change'], reverse=True)
            
            # Format message
            details = []
            for data in opens:
                details.append(
                    f"{data['symbol']}:\n"
                    f"  Current: {data['price']:.8f}\n"
                    f"  Previous Open: {data['previous_open']:.8f}\n"
                    f"  Change: {data['change']:+.2f}% {data['arrow']}"
                )
            
            return header + "\n".join(details)
            
        except Exception as e:
            self.logger.error(f"Error generating reset overview: {e}")
            return f"Error generating {timeframe} reset overview: {str(e)}"

    async def _get_verified_balance(self, asset, max_retries=3, retry_delay=1):
        """Get balance with verification and retries"""
        for attempt in range(max_retries):
            try:
                balance = self.get_balance(asset)
                if balance is not None:
                    return balance
                await asyncio.sleep(retry_delay)
            except Exception as e:
                self.logger.error(f"Balance check attempt {attempt + 1} failed: {e}")
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(retry_delay)
        return None

    def get_next_weekly_reset(self):
        """Get the next weekly reset time"""
        now = datetime.now(timezone.utc)
        # Get next Monday at 00:00 UTC
        days_ahead = 7 - now.weekday()  # 0 = Monday, so this gets days until next Monday
        if days_ahead <= 0:  # If today is Monday, jump to next week
            days_ahead += 7
        next_monday = now + timedelta(days_ahead)
        return next_monday.replace(hour=0, minute=0, second=0, microsecond=0)

    def get_next_monthly_reset(self):
        """Get the next monthly reset time"""
        now = datetime.now(timezone.utc)
        if now.month == 12:
            next_month = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            next_month = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
        return next_month

    async def save_trades(self):
        """Save trades atomically"""
        await self.file_handler.save_json_atomic(self.trades_file, self.trades)

    def test_connection(self):
        """Test connection to Binance API and verify trading symbols"""
        retries = 12
        while retries > 0:
            try:
                self.valid_symbols = []
                self.invalid_symbols = []
                
                for symbol in list(TRADING_SYMBOLS):
                    try:
                        ticker = self.client.get_symbol_ticker(symbol=symbol)
                        print(f"Testing {symbol}: {ticker['price']} USDT")
                        self.valid_symbols.append(symbol)
                    except BinanceAPIException as symbol_error:
                        if symbol_error.code == -1121:  # Invalid symbol error code
                            print(f"{Fore.RED}Invalid symbol detected: {symbol}")
                            self.logger.warning(f"Invalid symbol detected: {symbol}")
                            self.invalid_symbols.append(symbol)
                            continue
                        else:
                            raise symbol_error

                if self.valid_symbols:  # If we have at least one valid symbol
                    print(f"\n{Fore.GREEN}Successfully connected to {'Testnet' if self.client.API_URL == 'https://testnet.binance.vision/api' else 'Live'} API")
                    print(f"{Fore.GREEN}Valid symbols: {', '.join(self.valid_symbols)}")
                    
                    if self.invalid_symbols:
                        print(f"{Fore.YELLOW}Invalid symbols removed: {', '.join(self.invalid_symbols)}")
                        # Update tracking files
                        self._update_config_file()
                        self._update_invalid_symbols_file()
                    
                    return True
                else:
                    raise Exception("No valid trading symbols found")
                    
            except BinanceAPIException as e:
                if "502 Bad Gateway" in str(e):
                    print(f"{Fore.RED}Binance servers are under maintenance.")
                    print(f"{Fore.YELLOW}Retrying in 5 minutes... ({retries} attempts remaining)")
                    time.sleep(300)  # Wait for 5 minutes
                    retries -= 1
                else:
                    print(f"Error testing connection: {str(e)}")
                    self.logger.error(f"Error testing connection: {str(e)}")
                    raise
            except Exception as e:
                print(f"Unexpected error: {str(e)}")
                self.logger.error(f"Unexpected error: {str(e)}")
                raise

        print(f"{Fore.RED}Could not connect after {12-retries} attempts. Please check your connection and API keys.")
        self.logger.error("Connection attempts exhausted")
        return False

    def _update_config_file(self):
        """Update config.json with valid symbols only"""
        try:
            config_path = 'config/config.json'
            with open(config_path, 'r') as f:
                config_data = json.load(f)
            
            config_data['TRADING_SYMBOLS'] = self.valid_symbols
            
            with open(config_path, 'w') as f:
                json.dump(config_data, f, indent=4)
            
            print(f"{Fore.GREEN}Updated config.json with valid symbols")
        except Exception as e:
            print(f"{Fore.RED}Error updating config file: {e}")
            self.logger.error(f"Error updating config file: {e}")

    def _update_invalid_symbols_file(self):
        """Update invalid_symbols.txt with removed symbols"""
        try:
            os.makedirs('data', exist_ok=True)
            with open(self.invalid_symbols_file, 'w') as f:
                f.write(f"# Invalid symbols removed on {datetime.now()}\n")
                for symbol in self.invalid_symbols:
                    f.write(f"{symbol}\n")
            
            print(f"{Fore.YELLOW}Invalid symbols saved to {self.invalid_symbols_file}")
        except Exception as e:
            print(f"{Fore.RED}Error updating invalid symbols file: {e}")
            self.logger.error(f"Error updating invalid symbols file: {e}")

    async def cancel_all_orders(self):
        """Cancel all pending orders and cleanup trades file"""
        try:
            self.logger.info("Cancelling all pending orders...")
            cancelled = 0
            
            # Track which trades need status update
            cancelled_trade_ids = set()
            
            for symbol in self.valid_symbols:
                try:
                    # Get open orders for symbol
                    open_orders = await self._make_api_call(
                        self.client.get_open_orders,
                        symbol=symbol
                    )
                    
                    for order in open_orders:
                        try:
                            await self._make_api_call(
                                self.client.cancel_order,
                                symbol=symbol,
                                orderId=order['orderId']
                            )
                            
                            # Find corresponding trade ID
                            for trade_id, trade in self.trades.items():
                                if (trade['trade_info']['status'] == 'PENDING' and 
                                    trade['order_metadata']['order_id'] == order['orderId']):
                                    cancelled_trade_ids.add(trade_id)
                                    
                            cancelled += 1
                            
                        except BinanceAPIException as e:
                            if e.code == -2011:  # Order not found or already cancelled
                                continue
                            raise
                            
                except Exception as e:
                    self.logger.error(f"Error cancelling orders for {symbol}: {e}")
                    continue
            
            # Update trades file
            if cancelled_trade_ids:
                for trade_id in cancelled_trade_ids:
                    if trade_id in self.trades:
                        self.trades[trade_id]['trade_info']['status'] = 'CANCELLED'
                        self.trades[trade_id]['trade_info']['cancel_time'] = datetime.now(timezone.utc).isoformat()
                
                # Save updates to trades file
                await self._save_trades_atomic()
            
            self.logger.info(f"Cancelled {cancelled} pending orders and updated trades file")
            return True
            
        except Exception as e:
            self.logger.error(f"Error in cancel_all_orders: {e}")
            return False

    async def run_async(self):
        """Run the bot asynchronously"""
        try:
            # Initialize Telegram with visible feedback
            if self.telegram_handler:
                print(f"{Fore.CYAN}Initializing Telegram integration...")
                telegram_success = False
                
                for attempt in range(3):
                    try:
                        print(f"{Fore.CYAN}Telegram attempt {attempt + 1}/3...")
                        telegram_success = await self.telegram_handler.initialize()
                        if telegram_success:
                            print(f"{Fore.GREEN}✓ Telegram connected successfully!")
                            await self.telegram_handler.send_message(
                                "🤖 Bot connected and ready!"
                            )
                            break
                    except Exception as e:
                        print(f"{Fore.RED}Telegram attempt {attempt + 1} failed: {e}")
                        if attempt < 2:
                            print(f"{Fore.YELLOW}Retrying in 5s...")
                            await asyncio.sleep(5)
                
                if not telegram_success:
                    print(f"{Fore.RED}Failed to initialize Telegram - continuing without it")
                    self.telegram_handler = None

            # Start main loop
            await self.main_loop()
            
        except Exception as e:
            self.logger.error(f"Error in run_async: {e}")
            raise

    def run(self):
        """Run the bot with proper event loop handling"""
        try:
            # Handle different Python versions and platforms
            if sys.version_info >= (3, 10):
                if sys.platform.startswith('win'):
                    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
                else:
                    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
            
            # Create and set event loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            try:
                # Run main loop with proper error handling
                loop.run_until_complete(self.run_async())
            except KeyboardInterrupt:
                print(f"{Fore.YELLOW}\nClean shutdown requested. Preserving open orders...")
                loop.run_until_complete(self.shutdown())
            except Exception as e:
                print(f"{Fore.RED}\nError in main loop: {str(e)}")
                self.logger.error(f"Error in main loop: {str(e)}")
            finally:
                # Ensure proper cleanup
                try:
                    cleanup_tasks = []
                    if self.api_handler:
                        cleanup_tasks.append(self.api_handler.stop())
                    if self.telegram_handler:
                        cleanup_tasks.append(self.telegram_handler.shutdown())
                    
                    if cleanup_tasks:
                        # Run cleanup tasks with timeout
                        loop.run_until_complete(
                            asyncio.wait_for(
                                asyncio.gather(*cleanup_tasks),
                                timeout=30
                            )
                        )
                    
                    # Cancel all remaining tasks
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                        try:
                            loop.run_until_complete(task)
                        except (asyncio.CancelledError, Exception):
                            pass
                    
                    # Shutdown asyncgens and close loop
                    loop.run_until_complete(loop.shutdown_asyncgens())
                    loop.close()
                    
                except Exception as e:
                    self.logger.error(f"Error during cleanup: {e}")
                    
        except Exception as e:
            print(f"{Fore.RED}Fatal error: {str(e)}")
            self.logger.error(f"Fatal error: {str(e)}")

    def get_reference_prices(self, symbol):
        """Get reference prices with improved error handling"""
        references = {}
        
        try:
            # For each timeframe, safely get historical data
            for timeframe in ['daily', 'weekly', 'monthly']:
                if not self.timeframe_config.get(timeframe, {}).get('enabled', False):
                    continue
                    
                try:
                    if timeframe == 'daily':
                        interval = Client.KLINE_INTERVAL_1DAY
                        lookback = "2 days ago UTC"
                    elif timeframe == 'weekly':
                        interval = Client.KLINE_INTERVAL_1WEEK
                        lookback = "2 weeks ago UTC"
                    else:  # monthly
                        interval = Client.KLINE_INTERVAL_1MONTH
                        lookback = "2 months ago UTC"

                    # Get historical data
                    df = self.get_historical_data(symbol, interval, lookback)
                    
                    # Verify we have data
                    if df is not None and not df.empty:
                        references[timeframe] = {
                            'open': float(df['open'].iloc[-1]),
                            'high': float(df['high'].iloc[-1]),
                            'low': float(df['low'].iloc[-1])
                        }
                    else:
                        self.logger.warning(f"No historical data found for {symbol} {timeframe}")
                        references[timeframe] = {
                            'open': None,
                            'high': None,
                            'low': None
                        }
                        
                except Exception as e:
                    self.logger.error(f"Error getting {timeframe} data for {symbol}: {str(e)}")
                    references[timeframe] = {
                        'open': None,
                        'high': None,
                        'low': None
                    }
                    
        except Exception as e:
            self.logger.error(f"Error getting reference prices for {symbol}: {str(e)}")
            
        return references

# Update main entry point
if __name__ == "__main__":
    try:
        # Initialize color support for Windows
        if sys.platform.startswith('win'):
            os.system('color')
        
        # Load config and create bot instance
        config = ConfigHandler.load_config(use_env=os.environ.get('DOCKER', '').lower() == 'true')
        bot = BinanceBot(config)
        
        # Test connection before starting
        if bot.test_connection():
            bot.run()  # This will now work with the added run method
        else:
            print(f"{Fore.RED}Connection test failed. Bot will not start.")
            
    except KeyboardInterrupt:
        print("\nBot shutdown requested by user.")
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        logging.error(f"Unexpected error: {str(e)}")












