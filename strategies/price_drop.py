import pandas as pd

class PriceDropStrategy:
    def __init__(self, drop_thresholds=[0.01, 0.02, 0.03]):  # Default drop thresholds
        self.drop_thresholds = sorted(drop_thresholds, reverse=True)  # Sort thresholds in descending order

    def generate_signals(self, data, daily_open_price):
        prices = pd.Series(data)
        last_daily_close = prices.iloc[-1]  # Closing price of the last daily candle
        drop_percentage = (daily_open_price - last_daily_close) / daily_open_price

        signals = []
        for threshold in self.drop_thresholds:
            if drop_percentage >= threshold:
                signals.append((threshold, last_daily_close))
        return signals