from binance.client import Client
from binance.enums import *
from datetime import datetime
import pandas as pd
import time
import json
import telegram
from telegram.ext import Updater, CommandHandler

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
        self.telegram_bot = telegram.Bot(token=TELEGRAM_TOKEN)
        self.total_bought = {symbol: 0 for symbol in TRADING_SYMBOLS}
        self.total_spent = {symbol: 0 for symbol in TRADING_SYMBOLS}

    def test_connection(self):
        try:
            for symbol in TRADING_SYMBOLS:
                ticker = self.client.get_symbol_ticker(symbol=symbol)
                price = ticker['price']
                print(f"Verbindung erfolgreich. Aktueller Preis von {symbol}: {price}")
        except Exception as e:
            print(f"Fehler beim Testen der Verbindung: {str(e)}")
            self.logger.error(f"Fehler beim Testen der Verbindung: {str(e)}")
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
                self.logger.info(f"KAUF-ORDER für {symbol}: {order}")
                print(f"KAUF-ORDER für {symbol}: {order}")
                self.telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"KAUF-ORDER für {symbol}: {order}")
        except Exception as e:
            self.logger.error(f"Fehler beim Ausführen des Handels für {symbol}: {str(e)}")
            print(f"Fehler beim Ausführen des Handels für {symbol}: {str(e)}")

    def fetch_current_price(self, symbol):
        try:
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            current_price = ticker['price']
            print(f"Aktueller Preis von {symbol}: {current_price}")
        except Exception as e:
            print(f"Fehler beim Abrufen des aktuellen Preises von {symbol}: {str(e)}")
            self.logger.error(f"Fehler beim Abrufen des aktuellen Preises von {symbol}: {str(e)}")

    def get_balance(self):
        try:
            balance = self.client.get_asset_balance(asset='USDT')
            return balance['free']
        except Exception as e:
            print(f"Fehler beim Abrufen des Kontostands: {str(e)}")
            self.logger.error(f"Fehler beim Abrufen des Kontostands: {str(e)}")
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
            print(f"Fehler beim Berechnen des Gewinns: {str(e)}")
            self.logger.error(f"Fehler beim Berechnen des Gewinns: {str(e)}")
            return None

    def handle_balance(self, update, context):
        balance = self.get_balance()
        if balance is not None:
            update.message.reply_text(f"Aktueller Kontostand: {balance} USDT")
        else:
            update.message.reply_text("Fehler beim Abrufen des Kontostands.")

    def handle_profits(self, update, context):
        profits = self.get_profits()
        if profits is not None:
            profit_message = "\n".join([f"{symbol}: {profit} USDT" for symbol, profit in profits.items()])
            update.message.reply_text(f"Aktueller Gewinn:\n{profit_message}")
        else:
            update.message.reply_text("Fehler beim Berechnen des Gewinns.")

    def run(self):
        fetch_price_interval = 20 * 60  # 20 Minuten in Sekunden
        last_price_fetch_time = time.time() - fetch_price_interval  # Sicherstellen, dass der Preis sofort beim Start abgerufen wird

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
                    print(f"Abrufen historischer Daten für {symbol}...")
                    historical_data = self.get_historical_data(symbol)
                    signal, price = self.strategy.generate_signal(historical_data)
                    
                    if signal:
                        print(f"Signal generiert für {symbol}: {signal} zum Preis {price}")
                        self.execute_trade(symbol, signal, price)
                        self.logger.info(f"Signal generiert für {symbol}: {signal} zum Preis {price}")
                
                time.sleep(60)  # Jede Minute prüfen
                
            except Exception as e:
                self.logger.error(f"Fehler in der Hauptschleife: {str(e)}")
                print(f"Fehler in der Hauptschleife: {str(e)}")
                time.sleep(60)

if __name__ == "__main__":
    use_testnet = input("Möchten Sie das Testnet verwenden? (ja/nein): ").strip().lower() == 'ja'
    bot = BinanceBot(use_testnet)
    bot.test_connection()
    bot.run()