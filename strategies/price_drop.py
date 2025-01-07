import pandas as pd

class PriceDropStrategy:
    def __init__(self, drop_threshold=0.05):  # Default drop threshold to 5%
        self.drop_threshold = drop_threshold

    def generate_signal(self, data, daily_open_price):
        prices = pd.Series(data)
        last_daily_close = prices.iloc[-1]  # Closing price of the last daily candle
        drop_percentage = (daily_open_price - last_daily_close) / daily_open_price

        if drop_percentage >= self.drop_threshold:
            return "BUY", last_daily_close  # Return buy signal and price of the last candle
        return None, None