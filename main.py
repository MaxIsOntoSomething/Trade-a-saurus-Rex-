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
from config.config_handler import ConfigHandler
from telegram.ext import Application  # Update import

from strategies.price_drop import PriceDropStrategy
from utils.logger import setup_logger
from utils.websocket_manager import WebSocketManager
from utils.rate_limiter import RateLimiter
from utils.telegram_handler import TelegramHandler
from utils.file_handler import AsyncFileHandler  # Add import

# Initialize colorama
init(autoreset=True)

# Check if running in Docker
IN_DOCKER = os.environ.get('DOCKER', '').lower() == 'true'

# Load configuration based on environment
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
    # Class-level variables
    valid_trading_symbols = []
    insufficient_balance_timestamp = None
    balance_check_cooldown = timedelta(hours=24)
    balance_pause_reason = None  # New variable to track why trading is paused

    def __init__(self, use_testnet, use_telegram, timeframe_config, order_type, use_percentage, trade_amount, reserve_balance_usdt):
        # Load configuration only once
        self.config = ConfigHandler.load_config(use_env=IN_DOCKER)
        
        # Use config values if not running from CLI
        if IN_DOCKER or not any([timeframe_config, order_type, use_percentage, trade_amount, reserve_balance_usdt]):
            use_testnet = self.config.get('USE_TESTNET', True)
            use_telegram = self.config.get('USE_TELEGRAM', False)
            timeframe_config = self.config.get('timeframe_config', {})
            order_type = self.config.get('ORDER_TYPE', 'limit')
            use_percentage = self.config.get('USE_PERCENTAGE', False)
            trade_amount = self.config.get('TRADE_AMOUNT', 10)
            reserve_balance_usdt = self.config.get('RESERVE_BALANCE', 2000)

        # Initialize Telegram handler first if enabled
        self.telegram_handler = None
        if use_telegram and self.config['USE_TELEGRAM']:
            self.telegram_handler = TelegramHandler(
                self.config['TELEGRAM_TOKEN'],
                self.config['TELEGRAM_CHAT_ID'],
                self
            )

        # Add timestamp sync
        self.recv_window = 60000
        self.time_offset = 0
        self.start_time = datetime.now()
        self.valid_symbols = []  # Add this to track valid symbols
        self.invalid_symbols = []  # Add this to track invalid symbols
        self.invalid_symbols_file = str(ConfigHandler.get_data_dir() / 'invalid_symbols.txt')  # Update directory handling
        self.order_counter = 0  # Add counter for unique IDs
        self.tax_rate = 0.28  # Add 28% tax rate
        self.symbol_stats = {}  # Track per-symbol statistics
        
        if use_testnet:
            self.client = Client(TESTNET_API_KEY, TESTNET_API_SECRET, testnet=True)
            self.client.API_URL = 'https://testnet.binance.vision/api'
        else:
            self.client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
        
        # Sync time with Binance servers
        server_time = self.client.get_server_time()
        self.time_offset = server_time['serverTime'] - int(time.time() * 1000)
        
        # Add timeframe_config as instance variable
        self.timeframe_config = timeframe_config
        
        # Fix the strategy initialization
        self.strategy = PriceDropStrategy(timeframe_config)
        self.logger = setup_logger()
        self.use_telegram = use_telegram
        self.order_type = order_type
        self.use_percentage = use_percentage
        self.trade_amount = trade_amount
        self.reserve_balance_usdt = reserve_balance_usdt

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
                timeframe: {} for timeframe in timeframe_config.keys()
            } for symbol in TRADING_SYMBOLS
        }

        # Add WebSocket manager
        self.ws_manager = None
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

    async def _make_api_call(self, func, *args, **kwargs):
        """Wrapper for API calls with rate limiting"""
        await self.rate_limiter.acquire()
        return func(*args, **kwargs)

    async def get_cached_price(self, symbol):
        """Get cached price or fetch new one"""
        current_time = time.time()
        
        if (symbol in self.price_cache and
            current_time - self.price_cache[symbol]['timestamp'] < self.cache_duration):
            return self.price_cache[symbol]['price']
        
        # If no cache or expired, fetch new price
        ticker = await self._make_api_call(self.client.get_symbol_ticker, symbol=symbol)
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
        """Save trades atomically"""
        try:
            await self.file_handler.save_json_atomic(self.trades_file, self.trades)
        except Exception as e:
            self.logger.error(f"Error saving trades: {e}")
            raise

    async def execute_trade(self, symbol, price):
        """Execute trade with new structure"""
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
            if price_filter:
                tick_size = float(price_filter['tickSize'])
                price_precision = len(str(tick_size).rstrip('0').split('.')[-1])
                formatted_price = f"{current_price:.{price_precision}f}"

            # Calculate and format quantity with proper precision
            available_usdt = await self.get_available_usdt()
            if available_usdt < self.trade_amount:
                print(f"{Fore.RED}Balance issue detected:")
                print(f"Available: {available_usdt} USDT")
                print(f"Required: {self.trade_amount} USDT")
                print(f"Reserve: {self.reserve_balance_usdt} USDT")
                
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
                
                if self.use_telegram:
                    await self.send_telegram_message(pause_message)
                return

            trade_amount = available_usdt * self.trade_amount if self.use_percentage else min(self.trade_amount, available_usdt)
            
            # Get lot size filter for quantity precision
            lot_size_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
            if lot_size_filter:
                step_size = float(lot_size_filter['stepSize'])
                quantity_precision = len(str(step_size).rstrip('0').split('.')[-1])
                quantity = (trade_amount / current_price)
                quantity = round(quantity - (quantity % float(step_size)), quantity_precision)
                formatted_quantity = f"{quantity:.{quantity_precision}f}"

            # Create order with proper formatting
            timestamp = int(time.time() * 1000) + self.time_offset
            order_params = {
                'symbol': symbol,
                'side': SIDE_BUY,
                'quantity': f"%.{quantity_precision}f" % quantity,
                'recvWindow': self.recv_window,
                'timestamp': timestamp
            }

            if self.order_type == "limit":
                order_params.update({
                    'type': ORDER_TYPE_LIMIT,
                    'timeInForce': TIME_IN_FORCE_GTC,
                    'price': f"%.8f" % price
                })
            else:
                order_params['type'] = ORDER_TYPE_MARKET

            # Generate unique bot order ID
            bot_order_id = self.generate_order_id(symbol)
            
            # Place order with rate limiting
            order = await self._make_api_call(self.client.create_order, **order_params)
            
            # Use UTC for all datetime operations
            order_time = datetime.now(timezone.utc)
            cancel_time = order_time + self.limit_order_timeout
            
            # Create trade entry with new structure
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
                    'order_id': order['orderId'],
                    'placed_time': order_time.isoformat(),
                    'cancel_time': cancel_time.isoformat(),
                    'last_check': order_time.isoformat()
                }
            }
            
            # Save trades immediately
            await self._save_trades_atomic()
            
            # Start monitoring task
            asyncio.create_task(self.monitor_order(bot_order_id))
            
        except BinanceAPIException as e:
            error_msg = f"Binance API error in execute_trade: {str(e)}"
            self.logger.error(error_msg)
            print(f"{Fore.RED}{error_msg}")
        except Exception as e:
            error_msg = f"Error executing trade for {symbol}: {str(e)}"
            self.logger.error(error_msg)
            print(f"{Fore.RED}{error_msg}")
            # Clean up if something went wrong
            if bot_order_id in self.pending_orders:
                del self.pending_orders[bot_order_id]
                self.save_pending_orders()
            if bot_order_id in self.trades:
                del self.trades[bot_order_id]
                self.save_trades()

    async def monitor_order(self, trade_id):
        """Monitor order with new structure"""
        try:
            trade = self.trades[trade_id]
            symbol = trade['trade_info']['symbol']
            order_id = trade['order_metadata']['order_id']
            placed_time = datetime.fromisoformat(trade['order_metadata']['placed_time'])
            
            while True:
                now = datetime.now(timezone.utc)
                
                # Check for timeout
                if now - placed_time > self.limit_order_timeout:
                    await self._cancel_order(symbol, order_id)
                    trade['trade_info']['status'] = 'CANCELLED'
                    await self._save_trades_atomic()
                    break
                
                # Get order status
                order_status = await self._get_order_status_with_retry(symbol, order_id)
                
                if order_status['status'] == 'FILLED':
                    # Update trade info
                    trade['trade_info'].update({
                        'status': 'FILLED',
                        'filled_time': now.isoformat(),
                        'actual_price': float(order_status['price']),
                        'actual_quantity': float(order_status['executedQty'])
                    })
                    await self._save_trades_atomic()
                    break
                    
                await asyncio.sleep(10)
                
        except Exception as e:
            self.logger.error(f"Error monitoring order: {e}")

    def get_historical_data(self, symbol, interval, start_str):
        klines = self.client.get_historical_klines(
            symbol,
            interval,
            start_str
        )
        df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_av', 'trades', 'tb_base_av', 'tb_quote_av', 'ignore'])
        return df

    def get_daily_open_price(self, symbol):
        df = self.get_historical_data(symbol, Client.KLINE_INTERVAL_1DAY, "1 day ago UTC")
        return float(df['open'].iloc[-1])

    def print_daily_open_price(self):
        for symbol in self.valid_symbols:  # Changed from TRADING_SYMBOLS
            daily_open_price = self.get_daily_open_price(symbol)
            print(f"Daily open price for {symbol} at 00:00 UTC: {daily_open_price}")
            self.logger.info(f"Daily open price for {symbol} at 00:00 UTC: {daily_open_price}")

    def get_balance(self, asset=None):
        """Get balance for specific asset or all assets"""
        try:
            timestamp = int(time.time() * 1000) + self.time_offset
            balances = self.client.get_account(
                recvWindow=self.recv_window,
                timestamp=timestamp
            )['balances']
            
            # Convert to dictionary for easier access
            balance_report = {}
            for balance in balances:
                free = float(balance['free'])
                locked = float(balance['locked'])
                total = free + locked
                if total > 0:  # Only include non-zero balances
                    balance_report[balance['asset']] = {
                        'free': free,
                        'locked': locked,
                        'total': total
                    }
            
            # Return specific asset balance if requested
            if asset:
                return balance_report.get(asset, None)
            return balance_report
            
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
        """Save trades atomically"""
        try:
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
            timestamp = int(time.time() * 1000) + self.time_offset
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
            if price_filter:
                tick_size = float(price_filter['tickSize'])
                price_precision = len(str(tick_size).rstrip('0').split('.')[-1])
                formatted_price = f"{current_price:.{price_precision}f}"

            # Calculate and format quantity with proper precision
            available_usdt = await self.get_available_usdt()
            if available_usdt < self.trade_amount:
                print(f"{Fore.RED}Balance issue detected:")
                print(f"Available: {available_usdt} USDT")
                print(f"Required: {self.trade_amount} USDT")
                print(f"Reserve: {self.reserve_balance_usdt} USDT")
                
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
                
                if self.use_telegram:
                    await self.send_telegram_message(pause_message)
                return

            trade_amount = available_usdt * self.trade_amount if self.use_percentage else min(self.trade_amount, available_usdt)
            
            # Get lot size filter for quantity precision
            lot_size_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
            if lot_size_filter:
                step_size = float(lot_size_filter['stepSize'])
                quantity_precision = len(str(step_size).rstrip('0').split('.')[-1])
                quantity = (trade_amount / current_price)
                quantity = round(quantity - (quantity % float(step_size)), quantity_precision)
                formatted_quantity = f"{quantity:.{quantity_precision}f}"

            # Create order with proper formatting
            timestamp = int(time.time() * 1000) + self.time_offset
            order_params = {
                'symbol': symbol,
                'side': SIDE_BUY,
                'quantity': f"%.{quantity_precision}f" % quantity,
                'recvWindow': self.recv_window,
                'timestamp': timestamp
            }

            if self.order_type == "limit":
                order_params.update({
                    'type': ORDER_TYPE_LIMIT,
                    'timeInForce': TIME_IN_FORCE_GTC,
                    'price': f"%.8f" % price
                })
            else:
                order_params['type'] = ORDER_TYPE_MARKET

            # Generate unique bot order ID
            bot_order_id = self.generate_order_id(symbol)
            
            # Place order with rate limiting
            order = await self._make_api_call(self.client.create_order, **order_params)
            
            # Use UTC for all datetime operations
            order_time = datetime.now(timezone.utc)
            cancel_time = order_time + self.limit_order_timeout
            
            # Create trade entry with new structure
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
                    'order_id': order['orderId'],
                    'placed_time': order_time.isoformat(),
                    'cancel_time': cancel_time.isoformat(),
                    'last_check': order_time.isoformat()
                }
            }
            
            # Save trades immediately
            await self._save_trades_atomic()
            
            # Start monitoring task
            asyncio.create_task(self.monitor_order(bot_order_id))
            
        except BinanceAPIException as e:
            error_msg = f"Binance API error in execute_trade: {str(e)}"
            self.logger.error(error_msg)
            print(f"{Fore.RED}{error_msg}")
        except Exception as e:
            error_msg = f"Error executing trade for {symbol}: {str(e)}"
            self.logger.error(error_msg)
            print(f"{Fore.RED}{error_msg}")
            # Clean up if something went wrong
            if bot_order_id in self.pending_orders:
                del self.pending_orders[bot_order_id]
                self.save_pending_orders()
            if bot_order_id in self.trades:
                del self.trades[bot_order_id]
                self.save_trades()

    async def cancel_all_orders(self):
        """Cancel all pending orders"""
        try:
            for symbol in self.valid_symbols:
                try:
                    # Get all open orders for the symbol
                    open_orders = self.client.get_open_orders(symbol=symbol)
                    for order in open_orders:
                        self.client.cancel_order(
                            symbol=symbol,
                            orderId=order['orderId']
                        )
                        print(f"{Fore.YELLOW}Cancelled order {order['orderId']} for {symbol}")
                except Exception as e:
                    print(f"{Fore.RED}Error cancelling orders for {symbol}: {e}")
                    continue
        except Exception as e:
            print(f"{Fore.RED}Error in cancel_all_orders: {e}")
            self.logger.error(f"Error in cancel_all_orders: {e}")

    def fetch_current_price(self, symbol):
        try:
            # Show clean loading animation
            print(f"\r{Fore.CYAN}Loading {symbol} price... ⟳", end="")
            
            # Get current price and 24h stats
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            stats_24h = self.client.get_ticker(symbol=symbol)
            current_price = float(ticker['price'])
            price_change = float(stats_24h['priceChangePercent'])
            
            # Determine trend direction and color based on price change
            trend_arrow = "↑" if price_change >= 0 else "↓"
            trend_color = Fore.GREEN if price_change >= 0 else Fore.RED
            
            # Print clean price info
            print(f"\r{Fore.CYAN}[{datetime.now().strftime('%H:%M:%S')}] {symbol}:")
            print(f"  Price: {trend_color}{current_price:.2f} USDT {trend_arrow}")
            print(f"  24h Change: {trend_color}{price_change:+.2f}% {trend_arrow}")
            
            # Get and format reference prices
            reference_prices = self.get_reference_prices(symbol)
            print(f"{Fore.CYAN}Reference Prices for {symbol}:")
            for timeframe, prices in reference_prices.items():
                # Calculate percentage change from open
                change_from_open = ((current_price - prices['open']) / prices['open']) * 100
                
                # If price is higher than open: GREEN, ↑, positive percentage
                # If price is lower than open: RED, ↓, negative percentage
                price_color = Fore.GREEN if change_from_open >= 0 else Fore.RED
                price_arrow = "↑" if change_from_open >= 0 else "↓"
                
                print(f"  {timeframe.capitalize()}:")
                print(f"    Open: {prices['open']:.2f} USDT")
                print(f"    High: {prices['high']:.2f} USDT")
                print(f"    Low: {prices['low']:.2f} USDT")
                print(f"    Change: {price_color}{change_from_open:+.2f}% {price_arrow}")
            
            return current_price
        except Exception as e:
            print(f"\r{Fore.RED}Error fetching price for {symbol}: {str(e)}")
            self.logger.error(f"Error fetching current price of {symbol}: {str(e)}")
            return None

    async def safe_telegram_send(self, chat_id, text, parse_mode=None, reply_markup=None):
        """Safely send Telegram messages with retry logic"""
        max_retries = 3
        retry_delay = 2
        
        for attempt in range(max_retries):
            try:
                # Split message if too long
                if len(text) > 4000:
                    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
                    responses = []
                    for chunk in chunks:
                        response = await self.telegram_app.bot.send_message(
                            chat_id=chat_id,
                            text=chunk,
                            parse_mode=parse_mode,
                            reply_markup=reply_markup,
                            read_timeout=30,
                            connect_timeout=30,
                            write_timeout=30,
                            pool_timeout=30
                        )
                        responses.append(response)
                    return responses[-1]  # Return last message
                else:
                    return await self.telegram_app.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                        read_timeout=30,
                        connect_timeout=30,
                        write_timeout=30,
                        pool_timeout=30
                    )
            except Exception as e:
                if attempt == max_retries - 1:  # Last attempt
                    self.logger.error(f"Failed to send Telegram message after {max_retries} attempts: {e}")
                    raise
                await asyncio.sleep(retry_delay * (attempt + 1))  # Exponential backoff

    async def send_telegram_message(self, message):
        """Updated send_telegram_message method"""
        if self.telegram_handler:
            try:
                await self.telegram_handler.send_message(message, parse_mode='HTML')
            except Exception as e:
                print(f"Error sending Telegram message: {e}")
                self.logger.error(f"Error sending Telegram message: {e}")

    def get_reference_prices(self, symbol):
        references = {}
        
        try:
            if self.timeframe_config['daily']['enabled']:
                daily_data = self.get_historical_data(symbol, Client.KLINE_INTERVAL_1DAY, "2 days ago UTC")
                references['daily'] = {
                    'open': float(daily_data['open'].iloc[-1]),
                    'high': float(daily_data['high'].iloc[-1]),
                    'low': float(daily_data['low'].iloc[-1])
                }
            
            if self.timeframe_config['weekly']['enabled']:
                weekly_data = self.get_historical_data(symbol, Client.KLINE_INTERVAL_1WEEK, "2 weeks ago UTC")
                references['weekly'] = {
                    'open': float(weekly_data['open'].iloc[-1]),
                    'high': float(weekly_data['high'].iloc[-1]),
                    'low': float(weekly_data['low'].iloc[-1])
                }
                monthly_data = self.get_historical_data(symbol, Client.KLINE_INTERVAL_1MONTH, "2 months ago UTC")
                references['monthly'] = {
                    'open': float(monthly_data['open'].iloc[-1]),
                    'high': float(monthly_data['high'].iloc[-1]),
                    'low': float(monthly_data['low'].iloc[-1])
                }
        except Exception as e:
            self.logger.error(f"Error getting reference prices for {symbol}: {e}")
            
        return references

    async def main_loop(self):
        """Main bot loop with improved shutdown handling"""
        try:
            # Initialize Telegram first if enabled
            if self.telegram_handler and not self.telegram_handler.initialized:
                print(f"{Fore.CYAN}Initializing Telegram...")
                telegram_success = await self.telegram_handler.initialize()
                if not telegram_success:
                    print(f"{Fore.YELLOW}Failed to initialize Telegram, continuing without it...")
                    self.telegram_handler = None

            # Perform startup checks
            if not await self.startup_checks():
                raise Exception("Startup checks failed")

            # Initialize WebSocket manager
            self.ws_manager = WebSocketManager(self.client, self.valid_symbols, self.logger)
            self.ws_manager.add_callback(self.handle_price_update)

            print(f"{Fore.GREEN}Starting WebSocket connection...")
            await self.ws_manager.start()

            while True:
                try:
                    # Check for resets
                    await self.check_and_handle_resets()
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    self.logger.info("Main loop cancelled, shutting down...")
                    break
                except Exception as e:
                    self.logger.error(f"Error in main loop: {e}")
                    await asyncio.sleep(5)

        except Exception as e:
            self.logger.error(f"Fatal error in main loop: {e}")
            raise
        finally:
            # Proper cleanup sequence
            if self.ws_manager:
                await self.ws_manager.stop()
            if self.telegram_handler:
                await self.telegram_handler.shutdown()

    def run(self):
        """Run the bot with improved error handling"""
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            try:
                loop.run_until_complete(self.main_loop())
            except KeyboardInterrupt:
                print(f"{Fore.YELLOW}\nShutdown requested by user...")
            except Exception as e:
                print(f"{Fore.RED}\nError in main loop: {str(e)}")
                self.logger.error(f"Error in main loop: {str(e)}")
            finally:
                # Ensure proper cleanup
                if self.telegram_handler:
                    loop.run_until_complete(self.telegram_handler.shutdown())
                loop.close()
                
        except Exception as e:
            print(f"{Fore.RED}Fatal error: {str(e)}")
            self.logger.error(f"Fatal error: {str(e)}")

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
            print(f"{Fore.RED}Error handling price update for {symbol}: {e}")

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

if __name__ == "__main__":
    try:
        # 1. First ask about network
        while True:
            testnet_input = input("Do you want to use the testnet? (yes/no): ").strip().lower()
            if testnet_input in ['yes', 'no']:
                use_testnet = testnet_input == 'yes'
                break
            print("Invalid input. Please enter 'yes' or 'no'.")

        # 2. Then about Telegram - Add validation
        while True:
            telegram_input = input("Do you want to use Telegram notifications? (yes/no): ").strip().lower()
            if telegram_input in ['yes', 'no']:
                use_telegram = telegram_input == 'yes'
                if use_telegram:
                    if not USE_TELEGRAM:
                        print(f"{Fore.YELLOW}Telegram will be disabled due to invalid configuration.")
                        print(f"{Fore.YELLOW}Please check your config.json contains:")
                        print('  "TELEGRAM_TOKEN": "YOUR_BOT_TOKEN",')
                        print('  "TELEGRAM_CHAT_ID": "YOUR_CHAT_ID"')
                        print("\nTo get these values:")
                        print("1. Token: Talk to @BotFather on Telegram")
                        print("2. Chat ID: Talk to @userinfobot on Telegram")
                        use_telegram = False
                    else:
                        print(f"{Fore.GREEN}Telegram is properly configured and will be enabled.")
                break
            print("Invalid input. Please enter 'yes' or 'no'.")

        # 3. Then order type
        while True:
            order_type = input("Do you want to use limit orders or market orders? (limit/market): ").strip().lower()
            if order_type in ['limit', 'market']:
                break
            print("Invalid input. Please enter 'limit' or 'market'.")

        # 4. Trade amount type
        while True:
            percentage_input = input("Do you want to use a percentage of USDT per trade? (yes/no): ").strip().lower()
            if percentage_input in ['yes', 'no']:
                use_percentage = percentage_input == 'yes'
                break
            print("Invalid input. Please enter 'yes' or 'no'.")

        # 5. Trade amount value
        while True:
            try:
                if use_percentage:
                    trade_amount = float(input("Enter the percentage of USDT to use per trade (e.g., 10 for 10%): ").strip()) / 100
                    if 0 < trade_amount <= 1:
                        break
                    print("Percentage must be between 0 and 100.")
                else:
                    trade_amount = float(input("Enter the amount of USDT to use per trade: ").strip())
                    if trade_amount > 0:
                        break
                    print("Amount must be greater than 0.")
            except ValueError:
                print("Please enter a valid number.")

        # 6. USDT reserve
        while True:
            try:
                reserve_balance_usdt = float(input("Enter the USDT reserve balance (minimum USDT to keep): ").strip())
                if reserve_balance_usdt >= 0:
                    print(f"USDT Reserve set to: {reserve_balance_usdt} USDT")
                    break
                print("Reserve balance must be non-negative.")
            except ValueError:
                print("Please enter a valid number.")

        # 7. Finally timeframe configuration
        timeframe_config = {}
        timeframes = ['daily', 'weekly', 'monthly']
        
        for timeframe in timeframes:
            print(f"\n{Fore.CYAN}Configure {timeframe.capitalize()} Settings:")
            print(f"{Fore.YELLOW}Note: Thresholds must be entered in ascending order (e.g., 1%, 2%, 3%)")
            while True:
                enabled_input = input(f"Enable {timeframe} trading? (yes/no): ").strip().lower()
                if enabled_input in ['yes', 'no']:
                    enabled = enabled_input == 'yes'
                    break
                print("Invalid input. Please enter 'yes' or 'no'.")
            
            if enabled:
                while True:
                    try:
                        num_thresholds = int(input(f"Enter the number of {timeframe} drop thresholds: ").strip())
                        if 0 < num_thresholds <= 10:
                            break
                        print("Please enter a number between 1 and 10.")
                    except ValueError:
                        print("Please enter a valid number.")

                thresholds = []
                last_threshold = 0  # Keep track of last threshold
                for i in range(num_thresholds):
                    while True:
                        try:
                            threshold_input = float(input(f"Enter {timeframe} drop threshold {i+1} percentage (must be > {last_threshold:.1f}%): ").strip())
                            threshold = threshold_input / 100
                            
                            # Compare the input values directly, not the converted ones
                            if threshold_input <= last_threshold:
                                print(f"Threshold must be higher than {last_threshold:.1f}%")
                                continue
                                
                            if 0 < threshold_input <= 100:
                                thresholds.append(threshold)
                                last_threshold = threshold_input  # Store the input percentage, not the converted value
                                break
                                
                            print("Threshold must be between 0 and 100 percent.")
                        except ValueError:
                            print("Please enter a valid number.")
                
                timeframe_config[timeframe] = {
                    'enabled': enabled,
                    'thresholds': thresholds,
                    'orders_placed': {}
                }
            else:
                timeframe_config[timeframe] = {
                    'enabled': False,
                    'thresholds': [],
                    'orders_placed': {}
                }

        # Initialize and run bot with error handling
        try:
            bot = BinanceBot(use_testnet, use_telegram, timeframe_config, order_type, use_percentage, trade_amount, reserve_balance_usdt)
            bot.test_connection()
            bot.run()
        except Exception as e:
            print(f"Error initializing bot: {str(e)}")
            logging.error(f"Error initializing bot: {str(e)}")

    except KeyboardInterrupt:
        print("\nBot shutdown requested by user.")
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        logging.error(f"Unexpected error: {str(e)}")






