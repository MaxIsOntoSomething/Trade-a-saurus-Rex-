import pandas as pd

class PriceDropStrategy:
    def __init__(self, drop_threshold=0.06):
        self.drop_threshold = drop_threshold

    def generate_signal(self, data):
        prices = pd.Series(data)
        last_8h = prices[-2:]  # Last 2 4-hour candles to calculate the price drop
        drop_percentage = (last_8h.iloc[0] - last_8h.iloc[1]) / last_8h.iloc[0]

        if drop_percentage >= self.drop_threshold:
            return "BUY", last_8h.iloc[1]  # Return buy signal and price of the last candle
        return None, None