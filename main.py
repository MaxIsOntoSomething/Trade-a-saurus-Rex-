from binance.client import Client
from binance.enums import *
from datetime import datetime
import pandas as pd
import time
import json
import telegram
from telegram.ext import Updater, CommandHandler

from strategies.price_drop import PriceDropStrategy
from utils.logger import setup_logger

# Load configuration from JSON file
with open('config/config.json') as config_file:
    config = json.load(config_file)

BINANCE_API_KEY = config['BINANCE_API_KEY']
BINANCE_API_SECRET = config['BINANCE_API_SECRET']
TESTNET_API_KEY = config['TESTNET_API_KEY']
TESTNET_API_SECRET = config['TESTNET_API_SECRET']
TRADING_SYMBOLS = config['TRADING_SYMBOLS']
QUANTITY_PERCENTAGE = config['QUANTITY_PERCENTAGE']
TIME_INTERVAL = config['TIME_INTERVAL']
TELEGRAM_TOKEN = config['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = config['TELEGRAM_CHAT_ID']

class BinanceBot:
    def __init__(self, use_testnet):
        if use_testnet:
            self.client = Client(TESTNET_API_KEY, TESTNET_API_SECRET, testnet=True)
            self.client.API_URL = 'https://testnet.binance.vision/api'
        else:
            self.client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
        
        self.strategy = PriceDropStrategy()
        self.logger = setup_logger()
        self.telegram_bot = telegram.Bot(token=TELEGRAM_TOKEN)
        self.total_bought = {symbol: 0 for symbol in TRADING_SYMBOLS}
        self.total_spent = {symbol: 0 for symbol in TRADING_SYMBOLS}

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

    def get_historical_data(self, symbol):
        klines = self.client.get_historical_klines(
            symbol,
            Client.KLINE_INTERVAL_4HOUR,
            "8 hours ago UTC"
        )
        df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_av', 'trades', 'tb_base_av', 'tb_quote_av', 'ignore'])
        return df['close'].astype(float).values

    def execute_trade(self, symbol, signal, price):
        try:
            if signal == "BUY":
                balance = self.client.get_asset_balance(asset='USDT')
                available_balance = float(balance['free'])
                quantity = (available_balance * QUANTITY_PERCENTAGE) / float(price)
                order = self.client.create_order(
                    symbol=symbol,
                    side=SIDE_BUY,
                    type=ORDER_TYPE_LIMIT,
                    timeInForce=TIME_IN_FORCE_GTC,
                    quantity=quantity,
                    price=str(price)
                )
                self.total_bought[symbol] += quantity
                self.total_spent[symbol] += quantity * float(price)
                self.logger.info(f"BUY ORDER for {symbol}: {order}")
                print(f"BUY ORDER for {symbol}: {order}")
                self.telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"BUY ORDER for {symbol}: {order}")
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

    def get_balance(self):
        try:
            balance = self.client.get_asset_balance(asset='USDT')
            return balance['free']
        except Exception as e:
            print(f"Error fetching balance: {str(e)}")
            self.logger.error(f"Error fetching balance: {str(e)}")
            return None

    def get_profits(self):
        try:
            profits = {}
            for symbol in TRADING_SYMBOLS:
                current_price = float(self.client.get_symbol_ticker(symbol=symbol)['price'])
                average_price = self.total_spent[symbol] / self.total_bought[symbol] if self.total_bought[symbol] > 0 else 0
                profit = (current_price - average_price) * self.total_bought[symbol]
                profits[symbol] = profit
            return profits
        except Exception as e:
            print(f"Error calculating profits: {str(e)}")
            self.logger.error(f"Error calculating profits: {str(e)}")
            return None

    def handle_balance(self, update, context):
        balance = self.get_balance()
        if balance is not None:
            update.message.reply_text(f"Current balance: {balance} USDT")
        else:
            update.message.reply_text("Error fetching balance.")

    def handle_profits(self, update, context):
        profits = self.get_profits()
        if profits is not None:
            profit_message = "\n".join([f"{symbol}: {profit} USDT" for symbol, profit in profits.items()])
            update.message.reply_text(f"Current profits:\n{profit_message}")
        else:
            update.message.reply_text("Error calculating profits.")

    def run(self):
        fetch_price_interval = 20 * 60  # 20 minutes in seconds
        last_price_fetch_time = time.time() - fetch_price_interval  # Ensure the price is fetched immediately on start

        updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
        dispatcher = updater.dispatcher
        dispatcher.add_handler(CommandHandler("balance", self.handle_balance))
        dispatcher.add_handler(CommandHandler("profits", self.handle_profits))
        updater.start_polling()

        while True:
            try:
                current_time = time.time()
                if current_time - last_price_fetch_time >= fetch_price_interval:
                    for symbol in TRADING_SYMBOLS:
                        self.fetch_current_price(symbol)
                    last_price_fetch_time = current_time

                for symbol in TRADING_SYMBOLS:
                    print(f"Fetching historical data for {symbol}...")
                    historical_data = self.get_historical_data(symbol)
                    signal, price = self.strategy.generate_signal(historical_data)
                    
                    if signal:
                        print(f"Signal generated for {symbol}: {signal} at price {price}")
                        self.execute_trade(symbol, signal, price)
                        self.logger.info(f"Signal generated for {symbol}: {signal} at price {price}")
                
                time.sleep(60)  # Check every minute
                
            except Exception as e:
                self.logger.error(f"Error in main loop: {str(e)}")
                print(f"Error in main loop: {str(e)}")
                time.sleep(60)

if __name__ == "__main__":
    use_testnet = input("Do you want to use the testnet? (yes/no): ").strip().lower() == 'yes'
    bot = BinanceBot(use_testnet)
    bot.test_connection()
    bot.run()