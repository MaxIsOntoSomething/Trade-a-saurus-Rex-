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
        # Add timestamp sync
        self.recv_window = 60000
        self.time_offset = 0
        self.start_time = datetime.now()
        self.valid_symbols = []  # Add this to track valid symbols
        self.invalid_symbols = []  # Add this to track invalid symbols
        self.invalid_symbols_file = str(ConfigHandler.get_data_dir() / 'invalid_symbols.txt')  # Update directory handling
        
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

        if self.use_telegram:
            # Update Telegram initialization
            self.telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
            self.commands_setup = False
            # Remove the asyncio.create_task call here
        
        self.last_order_time = {}
        self.orders_placed_today = {}
        self.total_bought = {}
        self.total_spent = {}
        self.orders_placed = {}
        self.total_trades = 0  # Track the total number of trades
        self.max_trades_executed = False
        self.pending_orders = self.load_pending_orders()  # Add this to track pending orders
        self.orders_file = 'data/pending_orders.json'
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

    def load_pending_orders(self):
        """Load pending orders from file"""
        try:
            if os.path.exists('data/pending_orders.json'):
                with open('data/pending_orders.json', 'r') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            self.logger.error(f"Error loading pending orders: {e}")
            return {}

    def save_pending_orders(self):
        """Save pending orders to file"""
        try:
            os.makedirs('data', exist_ok=True)
            with open('data/pending_orders.json', 'w') as f:
                json.dump(self.pending_orders, f, indent=4)
        except Exception as e:
            self.logger.error(f"Error saving pending orders: {e}")

    async def setup_telegram_commands(self):
        """Setup the Telegram bot commands menu"""
        if self.commands_setup:
            return
            
        try:
            commands = [
                BotCommand("start", "Show available commands and bot status"),
                BotCommand("positions", "Show available trading opportunities"),
                BotCommand("balance", "Show current balance"),
                BotCommand("trades", "Show total number of trades"),
                BotCommand("profits", "Show current profits"),
                BotCommand("stats", "Show system stats and bot information"),
                BotCommand("distribution", "Show entry price distribution"),
                BotCommand("stacking", "Show position building over time"),
                BotCommand("buytimes", "Show time between buys"),
                BotCommand("portfolio", "Show portfolio value evolution"),
                BotCommand("allocation", "Show asset allocation"),
                BotCommand("orders", "Show open limit orders")  # Add new command
            ]
            
            await self.telegram_app.bot.set_my_commands(commands)
            print(f"{Fore.GREEN}Telegram command menu setup successfully!")
            self.logger.info("Telegram command menu setup successfully!")
            self.commands_setup = True
        except Exception as e:
            print(f"{Fore.RED}Error setting up Telegram commands: {e}")
            self.logger.error(f"Error setting up Telegram commands: {e}")

    def get_next_weekly_reset(self):
        now = datetime.now(timezone.utc)
        return (now + timedelta(days=(7 - now.weekday()))).replace(hour=0, minute=0, second=0, microsecond=0)

    def get_next_monthly_reset(self):
        now = datetime.now(timezone.utc)
        if now.month == 12:
            next_month = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            next_month = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
        return next_month

    def get_lot_size_info(self):
        lot_size_info = {}
        exchange_info = self.client.get_exchange_info()
        for symbol_info in exchange_info['symbols']:
            symbol = symbol_info['symbol']
            if symbol in TRADING_SYMBOLS:
                for filter in symbol_info['filters']:
                    if filter['filterType'] == 'LOT_SIZE':
                        lot_size_info[symbol] = {
                            'minQty': float(filter['minQty']),
                            'maxQty': float(filter['maxQty']),
                            'stepSize': float(filter['stepSize'])
                        }
        return lot_size_info

    def adjust_quantity(self, symbol, quantity):
        step_size = self.lot_size_info[symbol]['stepSize']
        return round(quantity // step_size * step_size, 8)

    def update_config_file(self):
        """Update config.json with valid symbols only"""
        try:
            with open('config/config.json', 'r') as f:
                config_data = json.load(f)
            
            config_data['TRADING_SYMBOLS'] = self.valid_symbols
            
            with open('config/config.json', 'w') as f:
                json.dump(config_data, f, indent=4)
            
            print(f"{Fore.GREEN}Updated config.json with valid symbols only")
        except Exception as e:
            print(f"{Fore.RED}Error updating config file: {e}")
            self.logger.error(f"Error updating config file: {e}")

    def update_invalid_symbols_file(self):
        """Update invalid_symbols.txt with invalid symbols"""
        try:
            # Create data directory if it doesn't exist
            os.makedirs('data', exist_ok=True)
            
            # Write invalid symbols to file
            with open(self.invalid_symbols_file, 'w') as f:
                f.write(f"# List of invalid symbols (one per line)\n")
                f.write(f"# Last updated: {datetime.now()}\n")
                for symbol in self.invalid_symbols:
                    f.write(f"{symbol}\n")
            
            print(f"{Fore.YELLOW}Invalid symbols saved to {self.invalid_symbols_file}")
        except Exception as e:
            print(f"{Fore.RED}Error updating invalid symbols file: {e}")
            self.logger.error(f"Error updating invalid symbols file: {e}")

    def test_connection(self):
        retries = 12
        while retries > 0:
            try:
                self.valid_symbols = []
                self.invalid_symbols = []
                
                for symbol in list(TRADING_SYMBOLS):
                    try:
                        ticker = self.client.get_symbol_ticker(symbol=symbol)
                        price = ticker['price']
                        print(f"Connection successful. Current price of {symbol}: {price}")
                        self.valid_symbols.append(symbol)
                    except BinanceAPIException as symbol_error:
                        if (symbol_error.code == -1121):  # Invalid symbol error code
                            print(f"{Fore.RED}Invalid symbol detected: {symbol}")
                            self.logger.warning(f"Invalid symbol detected: {symbol}")
                            self.invalid_symbols.append(symbol)
                            continue
                        else:
                            raise symbol_error

                if self.valid_symbols:  # If we have at least one valid symbol
                    print(f"\n{Fore.GREEN}Successfully connected with valid symbols: {', '.join(self.valid_symbols)}")
                    if self.invalid_symbols:
                        print(f"{Fore.YELLOW}Skipping invalid symbols: {', '.join(self.invalid_symbols)}")
                        
                        # Update config and invalid symbols files
                        self.update_config_file()
                        self.update_invalid_symbols_file()
                    
                    # Update class variable
                    BinanceBot.valid_trading_symbols = self.valid_symbols
                    self._initialize_data_structures()
                    return True
                else:
                    raise Exception("No valid trading symbols found")
                    
            except BinanceAPIException as e:
                if "502 Bad Gateway" in str(e):
                    print(Fore.RED + "Binance testnet spot servers are under maintenance.")
                    print(Fore.RED + "Waiting 5 minutes to try again...")
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

        print(Fore.RED + "Server maintenance might take longer. Shutting down the bot.")
        self.logger.error("Server maintenance might take longer. Shutting down the bot.")
        exit(1)

    def _initialize_data_structures(self):
        """Initialize data structures with validated symbols"""
        self.last_order_time = {symbol: None for symbol in self.valid_symbols}
        self.orders_placed_today = {
            symbol: {
                timeframe: {
                    threshold: False 
                    for threshold in self.timeframe_config[timeframe]['thresholds']
                } for timeframe in self.timeframe_config if self.timeframe_config[timeframe]['enabled']
            } for symbol in self.valid_symbols
        }
        self.total_bought = {symbol: 0 for symbol in self.valid_symbols}
        self.total_spent = {symbol: 0 for symbol in self.valid_symbols}
        self.orders_placed = {
            symbol: {
                timeframe: {} for timeframe in self.timeframe_config.keys()
            } for symbol in self.valid_symbols
        }
        self.lot_size_info = self.get_lot_size_info()

    async def _shutdown_telegram(self):
        """Improved Telegram shutdown"""
        if self.use_telegram and hasattr(self, 'telegram_app'):
            try:
                if hasattr(self.telegram_app, 'updater'):
                    if getattr(self.telegram_app.updater, '_running', False):
                        await self.telegram_app.updater.stop()
                if getattr(self.telegram_app, 'running', False):
                    await self.telegram_app.stop()
                print(f"{Fore.GREEN}Telegram bot stopped successfully")
            except Exception as e:
                print(f"{Fore.YELLOW}Note: Telegram was already stopped or not running")
                self.logger.info("Telegram was already stopped or not running")

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

    def get_balance(self):
        """Get complete balance report for all assets"""
        try:
            timestamp = int(time.time() * 1000) + self.time_offset
            balances = self.client.get_account(
                recvWindow=self.recv_window,
                timestamp=timestamp
            )['balances']
            
            # Include all non-zero balances
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
            return balance_report
        except Exception as e:
            print(f"Error fetching balance: {str(e)}")
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

    async def monitor_order(self, symbol, order_id, price, placed_time):
        """Asynchronously monitor an order until it's filled"""
        try:
            # Ensure timezone awareness
            placed_time = placed_time if placed_time.tzinfo else placed_time.replace(tzinfo=timezone.utc)
            
            while True:
                now = datetime.now(timezone.utc)  # Always use UTC
                if now - placed_time > self.limit_order_timeout:
                    # Cancel order if timeout reached
                    self.client.cancel_order(symbol=symbol, orderId=order_id)
                    msg = f"Order {order_id} for {symbol} canceled due to timeout"
                    print(msg)
                    self.logger.info(msg)
                    break

                order_status = self.client.get_order(symbol=symbol, orderId=order_id, recvWindow=self.recv_window)
                
                if order_status['status'] == 'FILLED':
                    quantity = float(order_status['executedQty'])
                    executed_price = float(order_status['price'])
                    total_cost = quantity * executed_price
                    self.total_bought[symbol] += quantity
                    self.total_spent[symbol] += quantity * price
                    self.total_trades += 1
                    
                    fill_msg = (
                        f"✅ Order filled for {symbol}:\n"
                        f"Quantity: {quantity}\n"
                        f"Price: {executed_price:.8f} USDT\n"
                        f"Total: {total_cost:.2f} USDT"
                    )
                    
                    print(Fore.GREEN + fill_msg)
                    self.logger.info(f"Order filled: {order_status}")
                    
                    if self.use_telegram:
                        await self.send_telegram_message(fill_msg)
                    
                    self.print_balance_report()
                    break
                
                elif order_status['status'] == 'CANCELED':
                    msg = f"Order {order_id} for {symbol} was canceled"
                    print(msg)
                    self.logger.info(msg)
                    break
                
                await asyncio.sleep(10)
                
        except Exception as e:
            print(f"Error monitoring order: {e}")
            self.logger.error(f"Error monitoring order: {e}")
        finally:
            if symbol in self.pending_orders:
                del self.pending_orders[symbol]
                self.save_pending_orders()  # Save after order complete/canceled

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

    async def execute_trade(self, symbol, price):
        try:
            # Check balance status first
            if not await self.check_balance_status():
                return

            ticker = self.client.get_symbol_ticker(symbol=symbol)
            current_price = float(ticker['price'])
            
            # Format price to proper decimal string (no scientific notation)
            formatted_price = '{:.8f}'.format(current_price).rstrip('0').rstrip('.')
            
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
            
            if self.use_percentage:
                trade_amount = available_usdt * self.trade_amount
            else:
                trade_amount = min(self.trade_amount, available_usdt)
            
            # Calculate quantity with proper precision
            quantity = trade_amount / current_price
            quantity = self.adjust_quantity(symbol, quantity)

            # Get symbol info for precision
            symbol_info = None
            exchange_info = self.client.get_exchange_info()
            for info in exchange_info['symbols']:
                if info['symbol'] == symbol:
                    symbol_info = info
                    break
            
            if not symbol_info:
                raise ValueError(f"Symbol info not found for {symbol}")
            
            # Get the quantity precision from lot size filter
            lot_size_filter = None
            for filter in symbol_info['filters']:
                if filter['filterType'] == 'LOT_SIZE':
                    lot_size_filter = filter
                    break
            
            if not lot_size_filter:
                raise ValueError(f"Lot size filter not found for {symbol}")
            
            # Calculate step size decimal places
            step_size = float(lot_size_filter['stepSize'])
            precision = len(str(step_size).rstrip('0').split('.')[-1])
            
            # Round quantity to the correct precision
            quantity = round(quantity, precision)
            quantity = float(f"%.{precision}f" % quantity)  # Ensure proper string formatting
            
            # Validate against min and max quantity
            min_qty = float(lot_size_filter['minQty'])
            max_qty = float(lot_size_filter['maxQty'])
            
            if quantity < min_qty:
                print(f"{Fore.YELLOW}Quantity {quantity} below minimum {min_qty} for {symbol}, adjusting...")
                quantity = min_qty
            elif quantity > max_qty:
                print(f"{Fore.YELLOW}Quantity {quantity} above maximum {max_qty} for {symbol}, adjusting...")
                quantity = max_qty
            
            # Create order with validated quantity
            timestamp = int(time.time() * 1000) + self.time_offset
            
            if self.order_type == "limit":
                order = self.client.create_order(
                    symbol=symbol,
                    side=SIDE_BUY,
                    type=ORDER_TYPE_LIMIT,
                    timeInForce=TIME_IN_FORCE_GTC,
                    quantity=quantity,
                    price=formatted_price,  # Use formatted price instead of str(current_price)
                    recvWindow=self.recv_window,
                    timestamp=timestamp
                )
                
                # Use UTC for all datetime operations
                order_time = datetime.now(timezone.utc)
                cancel_time = order_time + self.limit_order_timeout
                
                order_msg = (
                    f"Limit order set for {symbol}:\n"
                    f"Price: {formatted_price} USDT\n"
                    f"Quantity: {quantity}\n"
                    f"Will cancel at: {cancel_time.strftime('%Y-%m-%d %H:%M:%S')} UTC"
                )
                
                print(order_msg)
                self.logger.info(order_msg)
                
                if self.use_telegram:
                    await self.send_telegram_message(order_msg)
                
                self.pending_orders[symbol] = {
                    'orderId': order['orderId'],
                    'price': formatted_price,
                    'quantity': quantity,
                    'placed_time': datetime.now(timezone.utc).isoformat(),
                    'cancel_time': (datetime.now(timezone.utc) + self.limit_order_timeout).isoformat()
                }
                self.save_pending_orders()  # Save after placing order
                
                asyncio.create_task(self.monitor_order(symbol, order['orderId'], current_price, order_time))
                
            elif self.order_type == "market":
                order = self.client.create_order(
                    symbol=symbol,
                    side=SIDE_BUY,
                    type=ORDER_TYPE_MARKET,
                    quantity=quantity,
                    recvWindow=self.recv_window
                )
                self.total_bought[symbol] += quantity
                self.total_spent[symbol] += quantity * current_price
                self.total_trades += 1  # Increment total trades
                self.logger.info(f"BUY ORDER for {symbol}: {order}")
                print(Fore.GREEN + f"BUY ORDER for {symbol}: {order}")
                print(Fore.YELLOW + f"Bought {quantity} {symbol.replace('USDT', '')}")
                if self.use_telegram:
                    asyncio.run(self.send_telegram_message(f"BUY ORDER for {symbol}: {order}"))
                self.print_balance_report()  # Print balance report after each buy
                self.last_order_time[symbol] = datetime.now(timezone.utc)  # Update last order time
        except BinanceAPIException as e:
            error_msg = f"Binance API error in execute_trade: {str(e)}"
            self.logger.error(error_msg)
            print(f"{Fore.RED}{error_msg}")
        except Exception as e:
            error_msg = f"Error executing trade for {symbol}: {str(e)}"
            self.logger.error(error_msg)
            print(f"{Fore.RED}{error_msg}")

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

    async def handle_balance(self, update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /balance command with detailed balance info"""
        balance_report = self.get_balance()
        if balance_report:
            message = "Current Balance:\n\n"
            for asset, details in balance_report.items():
                message += (f"{asset}:\n"
                          f"  Free: {details['free']}\n"
                          f"  Locked: {details['locked']}\n"
                          f"  Total: {details['total']}\n"
                          f"------------------------\n")
            await update.effective_message.reply_text(message)
        else:
            await update.effective_message.reply_text("Error fetching balance.")

    async def handle_trades(self, update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text(f"Total number of trades done: {self.total_trades}")

    async def handle_profits(self, update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
        profits = self.get_profits()
        if profits is not None:
            profit_message = "\n".join([f"{symbol}: {profit} USDT" for symbol, profit in profits.items()])
            await update.effective_message.reply_text(f"Current profits:\n{profit_message}")
        else:
            await update.effective_message.reply_text("Error calculating profits.")

    async def handle_start(self, update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
        welcome_msg = (
            "🤖 Binance Trading Bot\n\n"
            "Available Commands:\n"
            "📊 Market Analysis:\n"
            "/positions - Show available trade opportunities\n"
            "/orders - Show open limit orders with cancel times\n\n"
            "💰 Portfolio & Trading:\n"
            "/balance - Show current balance\n"
            "/trades - Show total number of trades\n"
            "/profits - Show current profits\n"
            "/portfolio - Show portfolio value evolution\n"
            "/allocation - Show asset allocation\n\n"
            "📈 Analytics:\n"
            "/distribution - Show entry price distribution\n"
            "/stacking - Show position building over time\n"
            "/buytimes - Show time between buys\n\n"
            "ℹ️ System:\n"
            "/stats - Show system stats and bot information\n\n"
            "🔄 Trading Status:\n"
            f"Mode: {'Testnet' if self.client.API_URL == 'https://testnet.binance.vision/api' else 'Live'}\n"
            f"Order Type: {self.order_type.capitalize()}\n"
            f"USDT Reserve: {self.reserve_balance_usdt}\n"
            "Bot is actively monitoring markets! 🚀"
        )
        await update.effective_message.reply_text(welcome_msg)

    async def handle_positions(self, update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /positions command with proper context"""
        if not update or not update.effective_chat:
            return
        try:
            message = "🎯 Available Trading Positions:\n\n"
            
            for symbol in self.valid_symbols:  # Changed from TRADING_SYMBOLS
                message += f"📊 {symbol}:\n"
                
                for timeframe in ['daily', 'weekly', 'monthly']:
                    if self.timeframe_config[timeframe]['enabled']:
                        message += f"\n{timeframe.capitalize()}:\n"
                        
                        # Get available thresholds
                        available_thresholds = []
                        for threshold in self.timeframe_config[timeframe]['thresholds']:
                            if not self.strategy.order_history[timeframe].get(symbol, {}).get(threshold):
                                available_thresholds.append(f"{threshold*100}%")
                        
                        if available_thresholds:
                            message += f"  Available drops: {', '.join(available_thresholds)}\n"
                        else:
                            message += "  ❌ All positions filled\n"
                
                message += "\n" + "-"*20 + "\n"
            
            # Add current prices
            message += "\n📈 Current Prices:\n"
            for symbol in self.valid_symbols:  # Changed from TRADING_SYMBOLS
                price = self.client.get_symbol_ticker(symbol=symbol)['price']
                message += f"{symbol}: {price}\n"
            
            await update.effective_message.reply_text(message)
            
        except Exception as e:
            error_msg = f"Error fetching positions: {str(e)}"
            self.logger.error(error_msg)
            await update.effective_message.reply_text(error_msg)

    async def send_telegram_message(self, message):
        """Updated send_telegram_message method"""
        if self.use_telegram:
            try:
                await self.telegram_app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=message,
                    parse_mode='HTML'
                )
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

    async def handle_stats(self, update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stats command - Show system stats and bot runtime information"""
        try:
            # Get system information
            cpu_percent = psutil.cpu_percent(interval=1)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            
            # Calculate bot runtime
            runtime = datetime.now() - self.start_time
            days = runtime.days
            hours = runtime.seconds // 3600
            minutes = (runtime.seconds % 3600) // 60
            seconds = runtime.seconds % 60
            
            # Format message
            stats_message = (
                "🤖 Bot Statistics\n\n"
                f"⏱️ Runtime: {days}d {hours}h {minutes}m {seconds}s\n"
                f"🔄 Total Trades: {self.total_trades}\n\n"
                "💻 System Information:\n"
                f"CPU Usage: {cpu_percent}%\n"
                f"RAM Usage: {memory.percent}%\n"
                f"Free RAM: {memory.available / 1024 / 1024:.1f}MB\n"
                f"Disk Usage: {disk.percent}%\n"
                f"Free Disk: {disk.free / 1024 / 1024 / 1024:.1f}GB\n\n"
                "⚙️ Bot Configuration:\n"
                f"Order Type: {self.order_type}\n"
                f"Using Testnet: {self.client.API_URL == 'https://testnet.binance.vision/api'}\n"
                f"Symbols: {', '.join(self.valid_symbols)}\n"  # Changed from TRADING_SYMBOLS
                "Timeframes Enabled:\n"
            )
            
            # Add timeframe information
            for timeframe in self.timeframe_config:
                if self.timeframe_config[timeframe]['enabled']:
                    thresholds = [f"{t*100}%" for t in self.timeframe_config[timeframe]['thresholds']]
                    stats_message += f"- {timeframe.capitalize()}: {', '.join(thresholds)}\n"
            
            # Add trading amounts
            stats_message += f"\n💰 Trading Configuration:\n"
            stats_message += f"USDT Reserve: {self.reserve_balance_usdt}\n"
            if self.use_percentage:
                stats_message += f"Trade Amount: {self.trade_amount * 100}% of available USDT\n"
            else:
                stats_message += f"Trade Amount: {self.trade_amount} USDT\n"
            
            await update.effective_message.reply_text(stats_message)
            
        except Exception as e:
            error_msg = f"Error fetching stats: {str(e)}"
            self.logger.error(error_msg)
            await update.effective_message.reply_text(error_msg)

    async def main_loop(self):
        try:
            # Initialize WebSocket manager
            self.ws_manager = WebSocketManager(self.client, self.valid_symbols, self.logger)
            self.ws_manager.add_callback(self.handle_price_update)
            await self.ws_manager.start()

            # Initialize Telegram if enabled
            if self.use_telegram:
                try:
                    await self.telegram_app.initialize()
                    await self.setup_telegram_commands()
                    await self.telegram_app.start()
                    await self.telegram_app.updater.start_polling()
                    print(f"{Fore.GREEN}Telegram bot started successfully!")
                except Exception as e:
                    print(f"{Fore.RED}Error starting Telegram bot: {e}")
                    self.use_telegram = False  # Disable Telegram if it fails to start

            self.print_daily_open_price()
            self.print_balance_report()

            print(Fore.GREEN + "Bot started successfully!")
            print(Fore.YELLOW + "Monitoring price movements via WebSocket...")
            self.logger.info("Bot started successfully!")

            # Main loop for other periodic tasks
            while True:
                try:
                    now = datetime.now(timezone.utc)
                    
                    # Check for daily open price at 00:00 UTC
                    if now >= self.next_reset_times['daily']:
                        self.print_daily_open_price()
                        self.next_reset_times['daily'] += timedelta(days=1)

                    # Reset orders placed at the end of each timeframe
                    for timeframe, reset_time in self.next_reset_times.items():
                        if now >= reset_time:
                            # ...existing reset logic...
                            pass

                    await asyncio.sleep(1)

                except Exception as e:
                    self.logger.error(f"Error in main loop: {e}")
                    await asyncio.sleep(60)

        except Exception as e:
            self.logger.error(f"Fatal error in main loop: {e}")
            raise
        finally:
            # Cleanup
            if self.ws_manager:
                await self.ws_manager.stop()
            await self._shutdown_telegram()

    async def handle_price_update(self, symbol, price_data):
        """Handle real-time price updates from WebSocket"""
        try:
            current_price = price_data['price']
            price_change = price_data['change']
            
            # Store the latest data
            self.last_price_updates[symbol] = {
                'price': current_price,
                'change': price_change,
                'timestamp': datetime.now()
            }
            
            # Clear screen and print header
            print("\033[2J\033[H")  # Clear screen and move cursor to top
            print(f"{Fore.CYAN}{'Symbol':<12} {'Price':<16} {'24h Change':<12} {'Time'}")
            print("-" * 50)
            
            # Print all symbols' latest data
            for sym in sorted(self.last_price_updates.keys()):
                data = self.last_price_updates[sym]
                trend_arrow = "↑" if float(data['change']) >= 0 else "↓"
                trend_color = Fore.GREEN if float(data['change']) >= 0 else Fore.RED
                
                print(f"{Fore.CYAN}{sym:<12} "
                      f"{trend_color}{float(data['price']):,.8f} "
                      f"{data['change']:+.2f}% {trend_arrow}{Fore.RESET}")

            # Check for trading signals
            df = pd.DataFrame({
                'symbol': [symbol],
                'close': [current_price]
            })
            
            reference_prices = self.get_reference_prices(symbol)
            signals = self.strategy.generate_signals(
                df,
                reference_prices,
                datetime.now(timezone.utc)
            )
            
            # Execute trades for any signals
            for timeframe, threshold, price in signals:
                await self.execute_trade(symbol, price)

        except Exception as e:
            self.logger.error(f"Error handling price update: {e}")

    def get_profits(self):
        """Calculate current profits"""
        try:
            profits = {}
            current_prices = {}
            
            # Get current prices
            for symbol in self.valid_symbols:  # Changed from TRADING_SYMBOLS
                ticker = self.client.get_symbol_ticker(symbol=symbol)
                current_prices[symbol] = float(ticker['price'])
            
            # Calculate profits for each symbol
            for symbol in self.valid_symbols:  # Changed from TRADING_SYMBOLS
                if self.total_bought[symbol] > 0:
                    current_value = self.total_bought[symbol] * current_prices[symbol]
                    profit = current_value - self.total_spent[symbol]
                    profits[symbol] = round(profit, 2)
                else:
                    profits[symbol] = 0
                    
            return profits
        except Exception as e:
            self.logger.error(f"Error calculating profits: {e}")
            return None

    async def handle_distribution(self, update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /distribution command"""
        try:
            no_data = True
            for symbol in self.valid_symbols:
                try:
                    if self.total_bought[symbol] > 0:
                        # Get entry prices from orders_placed
                        entry_prices = []
                        for timeframe in self.orders_placed[symbol].values():
                            for order in timeframe.values():
                                try:
                                    entry_prices.append(float(order['price']))
                                except (KeyError, ValueError):
                                    continue
                        
                        if entry_prices:
                            no_data = False
                            graph = self.graph_generator.generate_entry_price_histogram(symbol, entry_prices)
                            if graph:
                                await update.effective_message.reply_photo(
                                    photo=graph,
                                    caption=f"Entry price distribution for {symbol}"
                                )
                            else:
                                await update.effective_message.reply_text(
                                    f"⚠️ Could not generate graph for {symbol}. Please try again later."
                                )
                except Exception as e:
                    await update.effective_message.reply_text(
                        f"⚠️ Error processing {symbol}: {str(e)}"
                    )
                    continue
            
            if no_data:
                await update.effective_message.reply_text(
                    "📊 No trading data available yet. Make some trades first!"
                )
                
        except Exception as e:
            await update.effective_message.reply_text(
                "❌ Error generating distribution graphs. Please try again later.\n"
                f"Error: {str(e)}"
            )
            self.logger.error(f"Error in handle_distribution: {str(e)}")

    # Similar error handling for other graph commands...
    async def handle_stacking(self, update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stacking command"""
        try:
            no_data = True
            for symbol in self.valid_symbols:
                try:
                    if (self.total_bought[symbol] > 0):
                        timestamps = []
                        quantities = []
                        prices = []
                        
                        for timeframe in self.orders_placed[symbol].values():
                            for order in timeframe.values():
                                try:
                                    timestamps.append(order['timestamp'])
                                    quantities.append(float(order['qty']))
                                    prices.append(float(order['price']))
                                except (KeyError, ValueError):
                                    continue
                        
                        if timestamps:
                            no_data = False
                            graph = self.graph_generator.generate_position_stacking(
                                symbol, timestamps, quantities, prices
                            )
                            if graph:
                                await update.effective_message.reply_photo(
                                    photo=graph,
                                    caption=f"Position building for {symbol}"
                                )
                            else:
                                await update.effective_message.reply_text(
                                    f"⚠️ Could not generate stacking graph for {symbol}"
                                )
                except Exception as e:
                    await update.effective_message.reply_text(
                        f"⚠️ Error processing {symbol}: {str(e)}"
                    )
                    continue
            
            if no_data:
                await update.effective_message.reply_text(
                    "📊 No position data available yet. Make some trades first!"
                )
                
        except Exception as e:
            await update.effective_message.reply_text(
                "❌ Error generating stacking visualization. Please try again later.\n"
                f"Error: {str(e)}"
            )
            self.logger.error(f"Error in handle_stacking: {str(e)}")

    # Add similar error handling for other graph handlers...
    async def handle_buy_times(self, update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /buytimes command"""
        try:
            buy_timestamps = []
            for symbol in self.valid_symbols:
                for timeframe in self.orders_placed[symbol].values():
                    for order in timeframe.values():
                        buy_timestamps.append(order['timestamp'])
            
            if buy_timestamps:
                graph = self.graph_generator.generate_time_between_buys(sorted(buy_timestamps))
                if graph:
                    await update.effective_message.reply_photo(photo=graph)
                else:
                    await update.effective_message.reply_text("Need at least 2 trades to generate time analysis")
            else:
                await update.effective_message.reply_text("No trades found")
        except Exception as e:
            await update.effective_message.reply_text(f"Error generating buy times analysis: {str(e)}")

    async def handle_portfolio(self, update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /portfolio command"""
        try:
            # Get historical portfolio values
            timestamps = []
            total_values = []
            
            # Calculate portfolio value at each trade
            current_prices = {symbol: float(self.client.get_symbol_ticker(symbol=symbol)['price']) 
                            for symbol in self.valid_symbols}
            
            for timestamp in sorted(set([order['timestamp'] 
                                      for symbol in self.valid_symbols 
                                      for timeframe in self.orders_placed[symbol].values() 
                                      for order in timeframe.values()])):
                total_value = sum(self.total_bought[symbol] * current_prices[symbol] 
                                for symbol in self.valid_symbols)
                timestamps.append(timestamp)
                total_values.append(total_value)
            
            if timestamps:
                graph = self.graph_generator.generate_portfolio_evolution(timestamps, total_values)
                await update.effective_message.reply_photo(photo=graph)
            else:
                await update.effective_message.reply_text("No portfolio data available")
        except Exception as e:
            await update.effective_message.reply_text(f"Error generating portfolio evolution: {str(e)}")

    async def handle_allocation(self, update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /allocation command with complete portfolio and fast response"""
        try:
            # Send immediate response
            processing_message = await update.effective_message.reply_text("📊 Generating portfolio allocation...")

            try:
                balances = self.get_balance()
                if not balances:
                    await processing_message.edit_text("❌ Error fetching balance information")
                    return

                # Process prices in parallel using asyncio.gather
                async def get_asset_value(asset, balance):
                    try:
                        if asset == 'USDT':
                            return asset, balance['total']
                        else:
                            try:
                                ticker = await asyncio.to_thread(
                                    self.client.get_symbol_ticker,
                                    symbol=f"{asset}USDT"
                                )
                                price = float(ticker['price'])
                                return asset, balance['total'] * price
                            except:
                                try:
                                    ticker = await asyncio.to_thread(
                                        self.client.get_symbol_ticker,
                                        symbol=f"USDT{asset}"
                                    )
                                    price = 1 / float(ticker['price'])
                                    return asset, balance['total'] * price
                                except:
                                    return None, None

                    except Exception as e:
                        self.logger.warning(f"Could not get price for {asset}: {e}")
                        return None, None

                # Process all assets in parallel
                tasks = [get_asset_value(asset, balance) for asset, balance in balances.items()]
                results = await asyncio.gather(*tasks)

                # Filter valid results
                asset_values = [(asset, value) for asset, value in results if asset is not None]
                
                if not asset_values:
                    await processing_message.edit_text("No assets found in portfolio")
                    return

                # Sort by value
                asset_values.sort(key=lambda x: x[1], reverse=True)
                assets, values = zip(*asset_values)

                # Generate report while graph is being created
                total_value = sum(values)
                report = "💰 Portfolio Allocation:\n\n"
                for asset, value in asset_values:
                    percentage = (value / total_value) * 100
                    report += f"{asset}: ${value:.2f} ({percentage:.2f}%)\n"
                report += f"\nTotal Portfolio Value: ${total_value:.2f}"

                # Generate graph in thread pool
                graph = await asyncio.to_thread(
                    self.graph_generator.generate_asset_allocation,
                    list(assets),
                    list(values)
                )

                # Delete processing message and send final report
                await processing_message.delete()
                
                if graph:
                    await update.effective_message.reply_photo(
                        photo=graph,
                        caption=report
                    )
                else:
                    await update.effective_message.reply_text(report)

            except Exception as e:
                await processing_message.edit_text(
                    f"❌ Error generating allocation: {str(e)}"
                )
                self.logger.error(f"Error in allocation: {e}")

        except Exception as e:
            await update.effective_message.reply_text(
                "❌ Error processing command. Please try again."
            )
            self.logger.error(f"Error in handle_allocation: {e}")

    # Similar updates for other handlers...

    async def handle_orders(self, update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /orders command - Show open limit orders"""
        try:
            open_orders = []
            for symbol in self.valid_symbols:
                try:
                    symbol_orders = self.client.get_open_orders(symbol=symbol)
                    open_orders.extend(symbol_orders)
                except Exception as e:
                    await update.effective_message.reply_text(f"Error fetching orders for {symbol}: {str(e)}")
                    continue
            
            if not open_orders:
                await update.effective_message.reply_text("No open limit orders")
                return
            
            message = "📋 Open Limit Orders:\n\n"
            for order in open_orders:
                symbol = order['symbol']
                side = order['side']
                quantity = float(order['origQty'])
                price = float(order['price'])
                total = quantity * price
                
                # Calculate time info
                order_time = datetime.fromtimestamp(order['time']/1000)
                cancel_time = order_time + self.limit_order_timeout
                now = datetime.now()
                time_until_cancel = cancel_time - now
                hours_left = time_until_cancel.total_seconds() / 3600
                
                # Format the cancel time info
                if hours_left < 0:
                    cancel_info = "Order will be cancelled soon"
                else:
                    cancel_info = f"Cancels in: {hours_left:.1f} hours"
                
                message += (
                    f"Symbol: {symbol}\n"
                    f"Side: {side}\n"
                    f"Quantity: {quantity}\n"
                    f"Price: {price:.8f} USDT\n"
                    f"Total: {total:.2f} USDT\n"
                    f"Placed: {order_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Order ID: {order['orderId']}\n"
                    f"❗ {cancel_info}\n"
                    f"─────────────\n"
                )
            
            await update.effective_message.reply_text(message)
            
        except Exception as e:
            error_msg = f"Error fetching open orders: {str(e)}"
            self.logger.error(error_msg)
            await update.effective_message.reply_text(error_msg)

    async def setup_telegram(self):
        """Setup Telegram handlers with proper context types"""
        try:
            # Add command handlers with proper context types
            self.telegram_app.add_handler(CommandHandler(
                "positions", 
                lambda update, context: self.handle_positions(update, context)
            ))
            self.telegram_app.add_handler(CommandHandler(
                "balance", 
                lambda update, context: self.handle_balance(update, context)
            ))
            self.telegram_app.add_handler(CommandHandler(
                "trades", 
                lambda update, context: self.handle_trades(update, context)
            ))
            self.telegram_app.add_handler(CommandHandler(
                "profits", 
                lambda update, context: self.handle_profits(update, context)
            ))
            self.telegram_app.add_handler(CommandHandler(
                "start", 
                lambda update, context: self.handle_start(update, context)
            ))
            self.telegram_app.add_handler(CommandHandler(
                "stats", 
                lambda update, context: self.handle_stats(update, context)
            ))
            self.telegram_app.add_handler(CommandHandler(
                "distribution", 
                lambda update, context: self.handle_distribution(update, context)
            ))
            self.telegram_app.add_handler(CommandHandler(
                "stacking", 
                lambda update, context: self.handle_stacking(update, context)
            ))
            self.telegram_app.add_handler(CommandHandler(
                "buytimes", 
                lambda update, context: self.handle_buy_times(update, context)
            ))
            self.telegram_app.add_handler(CommandHandler(
                "portfolio", 
                lambda update, context: self.handle_portfolio(update, context)
            ))
            self.telegram_app.add_handler(CommandHandler(
                "allocation", 
                lambda update, context: self.handle_allocation(update, context)
            ))
            self.telegram_app.add_handler(CommandHandler(
                "orders", 
                lambda update, context: self.handle_orders(update, context)
            ))

            # Start the bot
            await self.telegram_app.initialize()
            await self.setup_telegram_commands()
            await self.telegram_app.start()
            await self.telegram_app.updater.start_polling(
                allowed_updates=["message"],
                drop_pending_updates=True
            )
            
            print(f"{Fore.GREEN}Telegram bot started successfully!")
            self.logger.info("Telegram bot started successfully!")
            
        except Exception as e:
            print(f"{Fore.RED}Error setting up Telegram: {e}")
            self.logger.error(f"Error setting up Telegram: {e}")
            self.use_telegram = False

    def run(self):
        """Run the bot's main loop"""
        try:
            # Create and run the event loop
            loop = asyncio.new_event_loop()  # Create new event loop
            asyncio.set_event_loop(loop)     # Set it as the current event loop
            
            # Initialize Telegram before starting main loop
            if self.use_telegram:
                loop.run_until_complete(self.setup_telegram())
            
            # Run the main loop
            loop.run_until_complete(self.main_loop())
            
        except KeyboardInterrupt:
            print("\nShutdown requested... closing connections")
            self.logger.info("Shutdown requested by user")
        except Exception as e:
            print(f"\nError in main loop: {str(e)}")
            self.logger.error(f"Error in main loop: {str(e)}")
        finally:
            # Ensure proper cleanup
            if self.use_telegram:
                loop.run_until_complete(self._shutdown_telegram())
            loop.close()

    def __del__(self):
        """Save pending orders when bot shuts down"""
        self.save_pending_orders()

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
            bot.run()  # Now this will work
        except Exception as e:
            print(f"Error initializing bot: {str(e)}")
            logging.error(f"Error initializing bot: {str(e)}")

    except KeyboardInterrupt:
        print("\nBot shutdown requested by user.")
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        logging.error(f"Unexpected error: {str(e)}")





