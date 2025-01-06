import pandas as pd

class PriceDropStrategy:
    def __init__(self, drop_threshold=0.06):
        self.drop_threshold = drop_threshold

    def generate_signal(self, data):
        prices = pd.Series(data)
        last_8h = prices[-2:]  # Letzten 2 4-Stunden-Kerzen damit der Preisverfall berechnet werden kann
        drop_percentage = (last_8h.iloc[0] - last_8h.iloc[1]) / last_8h.iloc[0]

        if drop_percentage >= self.drop_threshold:
            return "BUY", last_8h.iloc[1]  # Buy Signal und Preis der letzten Kerze zurückgeben
        return None, None