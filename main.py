from binance.client import Client
from binance.enums import *
import time
from datetime import datetime
import pandas as pd

from config.config import *
from strategies.price_drop import PriceDropStrategy
from utils.logger import setup_logger

class BinanceBot:
    def __init__(self, use_testnet):
        if use_testnet:
            self.client = Client(TESTNET_API_KEY, TESTNET_API_SECRET, testnet=True)
            self.client.API_URL = 'https://testnet.binance.vision/api'
        else:
            self.client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
        
        self.strategy = PriceDropStrategy()
        self.logger = setup_logger()

    def test_connection(self):
        try:
            btc_ticker = self.client.get_symbol_ticker(symbol="BTCUSDT")
            eth_ticker = self.client.get_symbol_ticker(symbol="ETHUSDT")
            btc_price = btc_ticker['price']
            eth_price = eth_ticker['price']
            print(f"Connection successful. Current price of BTCUSDT: {btc_price}, ETHUSDT: {eth_price}")
        except Exception as e:
            print(f"Error testing connection: {str(e)}")
            self.logger.error(f"Error testing connection: {str(e)}")
            raise

    def get_historical_data(self):
        klines = self.client.get_historical_klines(
            TRADING_SYMBOL,
            Client.KLINE_INTERVAL_4HOUR,
            "8 hours ago UTC"
        )
        df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_av', 'trades', 'tb_base_av', 'tb_quote_av', 'ignore'])
        return df['close'].astype(float).values

    def execute_trade(self, signal, price):
        try:
            if signal == "BUY":
                order = self.client.create_order(
                    symbol=TRADING_SYMBOL,
                    side=SIDE_BUY,
                    type=ORDER_TYPE_LIMIT,
                    timeInForce=TIME_IN_FORCE_GTC,
                    quantity=QUANTITY,
                    price=str(price)
                )
                self.logger.info(f"BUY ORDER: {order}")
                print(f"BUY ORDER: {order}")
        except Exception as e:
            self.logger.error(f"Error executing trade: {str(e)}")
            print(f"Error executing trade: {str(e)}")

    def fetch_current_price(self):
        try:
            ticker = self.client.get_symbol_ticker(symbol=TRADING_SYMBOL)
            current_price = ticker['price']
            print(f"Current price of {TRADING_SYMBOL}: {current_price}")
        except Exception as e:
            print(f"Error fetching current price: {str(e)}")
            self.logger.error(f"Error fetching current price: {str(e)}")

    def run(self):
        fetch_price_interval = 20 * 60  
        last_price_fetch_time = time.time() - fetch_price_interval 

        while True:
            try:
                current_time = time.time()
                if current_time - last_price_fetch_time >= fetch_price_interval:
                    self.fetch_current_price()
                    last_price_fetch_time = current_time

                print("Fetching historical data...")
                historical_data = self.get_historical_data()
                signal, price = self.strategy.generate_signal(historical_data)
                
                if signal:
                    print(f"Signal generated: {signal} at price {price}")
                    self.execute_trade(signal, price)
                    self.logger.info(f"Signal generated: {signal} at price {price}")
                
                time.sleep(60)  # Jede Minute check
                
            except Exception as e:
                self.logger.error(f"Error in main loop: {str(e)}")
                print(f"Error in main loop: {str(e)}")
                time.sleep(60)

if __name__ == "__main__":
    use_testnet = input("Do you want to use the testnet? (yes/no): ").strip().lower() == 'yes'
    bot = BinanceBot(use_testnet)
    bot.test_connection()
    bot.run()