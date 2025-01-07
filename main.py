from binance.client import Client
from binance.enums import *
from datetime import datetime, timedelta, timezone
import pandas as pd
import time
import json
import telegram
from telegram.ext import Updater, CommandHandler
from colorama import Fore, Style, init

from strategies.price_drop import PriceDropStrategy
from utils.logger import setup_logger

# Initialize colorama
init(autoreset=True)

# Load configuration from JSON file
with open('config/config.json') as config_file:
    config = json.load(config_file)

BINANCE_API_KEY = config['BINANCE_API_KEY']
BINANCE_API_SECRET = config['BINANCE_API_SECRET']
TESTNET_API_KEY = config['TESTNET_API_KEY']
TESTNET_API_SECRET = config['TESTNET_API_SECRET']
TRADING_SYMBOLS = config['TRADING_SYMBOLS']
TIME_INTERVAL = config['TIME_INTERVAL']
TELEGRAM_TOKEN = config['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = config['TELEGRAM_CHAT_ID']

class BinanceBot:
    def __init__(self, use_testnet, use_telegram, drop_thresholds, order_type, use_percentage, trade_amount):
        if use_testnet:
            self.client = Client(TESTNET_API_KEY, TESTNET_API_SECRET, testnet=True)
            self.client.API_URL = 'https://testnet.binance.vision/api'
        else:
            self.client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
        
        self.strategy = PriceDropStrategy(drop_thresholds=drop_thresholds)
        self.logger = setup_logger()
        self.use_telegram = use_telegram
        self.order_type = order_type
        self.use_percentage = use_percentage
        self.trade_amount = trade_amount
        if self.use_telegram:
            self.telegram_bot = telegram.Bot(token=TELEGRAM_TOKEN)
        
        self.last_order_time = {symbol: None for symbol in TRADING_SYMBOLS}
        self.orders_placed_today = {symbol: {threshold: False for threshold in drop_thresholds} for symbol in TRADING_SYMBOLS}  # Track the orders placed today per symbol per threshold
        self.lot_size_info = self.get_lot_size_info()
        self.total_bought = {symbol: 0 for symbol in TRADING_SYMBOLS}
        self.total_spent = {symbol: 0 for symbol in TRADING_SYMBOLS}
        self.total_trades = 0  # Track the total number of trades
        self.max_trades_executed = False

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

    def test_connection(self):
        try:
            for symbol in TRADING_SYMBOLS:
                ticker = self.client.get_symbol_ticker(symbol=symbol)
                price = ticker['price']
                print(f"Connection successful. Current price of {symbol}: {price}")
        except Exception as e:
            print(f"Error testing connection: {str(e)}")
            self.logger.error(f"Error testing connection: {str(e)}")
            raise

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
        for symbol in TRADING_SYMBOLS:
            daily_open_price = self.get_daily_open_price(symbol)
            print(f"Daily open price for {symbol} at 00:00 UTC: {daily_open_price}")
            self.logger.info(f"Daily open price for {symbol} at 00:00 UTC: {daily_open_price}")

    def get_balance(self):
        try:
            balances = self.client.get_account(recvWindow=5000)['balances']
            balance_report = {}
            for balance in balances:
                asset = balance['asset']
                if asset == 'USDT' or asset in [symbol.replace('USDT', '') for symbol in TRADING_SYMBOLS]:
                    free = float(balance['free'])
                    locked = float(balance['locked'])
                    total = free + locked
                    if total > 0:
                        balance_report[asset] = total
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

    def execute_trade(self, symbol, price):
        try:
            # Fetch the current price right before placing the order
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            current_price = float(ticker['price'])
            
            balance = self.client.get_asset_balance(asset='USDT', recvWindow=5000)
            available_balance = float(balance['free'])
            if available_balance < 100:
                print(f"Insufficient balance to place order for {symbol}. Available balance: {available_balance} USDT")
                return
            
            if self.use_percentage:
                trade_amount = available_balance * self.trade_amount
            else:
                trade_amount = self.trade_amount
            
            quantity = trade_amount / current_price  # Calculate quantity based on trade amount
            quantity = self.adjust_quantity(symbol, quantity)
            
            if self.order_type == "limit":
                order = self.client.create_order(
                    symbol=symbol,
                    side=SIDE_BUY,
                    type=ORDER_TYPE_LIMIT,
                    timeInForce=TIME_IN_FORCE_GTC,
                    quantity=quantity,
                    price=str(current_price),
                    recvWindow=5000
                )
                print(f"Limit order set at price {current_price}")
                self.logger.info(f"Limit order set at price {current_price}")
                # Wait for the order to be filled
                while True:
                    order_status = self.client.get_order(symbol=symbol, orderId=order['orderId'])
                    if order_status['status'] == 'FILLED':
                        self.total_bought[symbol] += quantity
                        self.total_spent[symbol] += quantity * current_price
                        self.total_trades += 1  # Increment total trades
                        self.logger.info(f"BUY ORDER for {symbol}: {order}")
                        print(Fore.GREEN + f"BUY ORDER for {symbol}: {order}")
                        print(Fore.YELLOW + f"Bought {quantity} {symbol.replace('USDT', '')}")
                        if self.use_telegram:
                            self.telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"BUY ORDER for {symbol}: {order}")
                        self.print_balance_report()  # Print balance report after each buy
                        self.last_order_time[symbol] = datetime.now(timezone.utc)  # Update last order time
                        break
                    time.sleep(1)  # Check order status every second
            elif self.order_type == "market":
                order = self.client.create_order(
                    symbol=symbol,
                    side=SIDE_BUY,
                    type=ORDER_TYPE_MARKET,
                    quantity=quantity,
                    recvWindow=5000
                )
                self.total_bought[symbol] += quantity
                self.total_spent[symbol] += quantity * current_price
                self.total_trades += 1  # Increment total trades
                self.logger.info(f"BUY ORDER for {symbol}: {order}")
                print(Fore.GREEN + f"BUY ORDER for {symbol}: {order}")
                print(Fore.YELLOW + f"Bought {quantity} {symbol.replace('USDT', '')}")
                if self.use_telegram:
                    self.telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"BUY ORDER for {symbol}: {order}")
                self.print_balance_report()  # Print balance report after each buy
                self.last_order_time[symbol] = datetime.now(timezone.utc)  # Update last order time
        except Exception as e:
            self.logger.error(f"Error executing trade for {symbol}: {str(e)}")
            print(f"Error executing trade for {symbol}: {str(e)}")

    def fetch_current_price(self, symbol):
        try:
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            current_price = ticker['price']
            print(f"Current price of {symbol}: {current_price}")
        except Exception as e:
            print(f"Error fetching current price of {symbol}: {str(e)}")
            self.logger.error(f"Error fetching current price of {symbol}: {str(e)}")

    def handle_balance(self, update, context):
        balance_report = self.get_balance()
        if balance_report:
            balance_message = "\n".join([f"{asset}: {total}" for asset, total in balance_report.items()])
            update.message.reply_text(f"Current balance:\n{balance_message}")
        else:
            update.message.reply_text("Error fetching balance.")

    def handle_trades(self, update, context):
        update.message.reply_text(f"Total number of trades done: {self.total_trades}")

    def handle_profits(self, update, context):
        profits = self.get_profits()
        if profits is not None:
            profit_message = "\n".join([f"{symbol}: {profit} USDT" for symbol, profit in profits.items()])
            update.message.reply_text(f"Current profits:\n{profit_message}")
        else:
            update.message.reply_text("Error calculating profits.")

    def run(self):
        fetch_price_interval = 60  # Check every minute to catch flash trades
        last_price_fetch_time = time.time() - fetch_price_interval  # Ensure the price is fetched immediately on start
        next_daily_open_check = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

        if self.use_telegram:
            updater = Updater(TELEGRAM_TOKEN)  # Corrected initialization
            dispatcher = updater.dispatcher
            dispatcher.add_handler(CommandHandler("balance", self.handle_balance))
            dispatcher.add_handler(CommandHandler("trades", self.handle_trades))
            dispatcher.add_handler(CommandHandler("profits", self.handle_profits))
            updater.start_polling()

        self.print_daily_open_price()  # Print daily open price at startup
        self.print_balance_report()  # Print balance report at startup

        while True:
            try:
                current_time = time.time()
                if current_time - last_price_fetch_time >= fetch_price_interval:
                    for symbol in TRADING_SYMBOLS:
                        self.fetch_current_price(symbol)
                    last_price_fetch_time = current_time

                if datetime.now(timezone.utc) >= next_daily_open_check:
                    self.print_daily_open_price()
                    next_daily_open_check += timedelta(days=1)
                    self.last_order_time = {symbol: None for symbol in TRADING_SYMBOLS}  # Reset last order time at 00:00 UTC
                    self.orders_placed_today = {symbol: {threshold: False for threshold in self.strategy.drop_thresholds} for symbol in TRADING_SYMBOLS}  # Reset orders placed today count
                    self.max_trades_executed = False  # Reset max trades executed flag

                if not self.max_trades_executed:
                    for symbol in TRADING_SYMBOLS:
                        if any(not placed for placed in self.orders_placed_today[symbol].values()):
                            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            print(f"[{timestamp}] Fetching historical data for {symbol}...")
                            historical_data = self.get_historical_data(symbol, TIME_INTERVAL, "1 day ago UTC")
                            daily_open_price = self.get_daily_open_price(symbol)
                            signals = self.strategy.generate_signals(historical_data['close'].astype(float).values, daily_open_price)
                            
                            for threshold, price in signals:
                                if not self.orders_placed_today[symbol][threshold]:
                                    print(f"Signal generated for {symbol} at threshold {threshold}: BUY at price {price}")
                                    self.execute_trade(symbol, price)
                                    self.orders_placed_today[symbol][threshold] = True
                                    self.logger.info(f"Signal generated for {symbol} at threshold {threshold}: BUY at price {price}")
                    self.max_trades_executed = all(all(placed for placed in self.orders_placed_today[symbol].values()) for symbol in TRADING_SYMBOLS)
                else:
                    next_reset_time = next_daily_open_check - datetime.now(timezone.utc)
                    print(f"Max trades executed for the day. Next reset in {next_reset_time}.")
                    self.logger.info(f"Max trades executed for the day. Next reset in {next_reset_time}.")
                    if self.use_telegram:
                        self.telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"Max trades executed for the day. Next reset in {next_reset_time}.")
                    time.sleep(3600)  # Sleep for an hour before checking again
                
                time.sleep(60)  # Check every minute
                
            except Exception as e:
                self.logger.error(f"Error in main loop: {str(e)}")
                print(f"Error in main loop: {str(e)}")
                time.sleep(60)

if __name__ == "__main__":
    use_testnet = input("Do you want to use the testnet? (yes/no): ").strip().lower() == 'yes'
    use_telegram = input("Do you want to use Telegram notifications? (yes/no): ").strip().lower() == 'yes'
    num_thresholds = int(input("Enter the number of drop thresholds: ").strip())
    drop_thresholds = []
    for i in range(num_thresholds):
        while True:
            threshold = float(input(f"Enter drop threshold {i+1} percentage (e.g., 1 for 1%): ").strip()) / 100
            if i == 0 or threshold > drop_thresholds[-1]:
                drop_thresholds.append(threshold)
                break
            else:
                print(f"Threshold {i+1} must be higher than threshold {i}. Please enter a valid value.")
    order_type = input("Do you want to use limit orders or market orders? (limit/market): ").strip().lower()
    use_percentage = input("Do you want to use a percentage of USDT per trade? (yes/no): ").strip().lower() == 'yes'
    if use_percentage:
        trade_amount = float(input("Enter the percentage of USDT to use per trade (e.g., 10 for 10%): ").strip()) / 100
    else:
        trade_amount = float(input("Enter the amount of USDT to use per trade: ").strip())
    bot = BinanceBot(use_testnet, use_telegram, drop_thresholds, order_type, use_percentage, trade_amount)
    bot.test_connection()
    bot.run()