import pandas as pd

class PriceDropStrategy:
    def __init__(self, drop_threshold=0.05):  # Updated drop threshold to 5%
        self.drop_threshold = drop_threshold

    def generate_signal(self, data, daily_open_price):
        prices = pd.Series(data)
        last_4h_close = prices.iloc[-1]  # Closing price of the last 4-hour candle
        drop_percentage = (daily_open_price - last_4h_close) / daily_open_price

        if drop_percentage >= self.drop_threshold:
            return "BUY", last_4h_close  # Return buy signal and price of the last candle
        return None, None