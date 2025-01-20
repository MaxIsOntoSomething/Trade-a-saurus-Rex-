import pandas as pd

class PriceDropStrategy:
    def __init__(self, timeframe_config):
        self.timeframe_config = timeframe_config
        # Sort timeframes by priority (daily, weekly, monthly)
        self.timeframe_priority = ['daily', 'weekly', 'monthly']
        self.order_history = {
            'daily': {},    # Format: {symbol: {threshold: last_order_time}}
            'weekly': {},
            'monthly': {}
        }

    def can_place_order(self, timeframe, symbol, threshold, current_time):
        # Check if threshold was already executed
        if symbol in self.order_history[timeframe]:
            if threshold in self.order_history[timeframe][symbol]:
                last_order = self.order_history[timeframe][symbol][threshold]
                
                # Check timeframe-specific restrictions
                if timeframe == 'daily':
                    return (current_time - last_order).days >= 1
                elif timeframe == 'weekly':
                    return (current_time - last_order).days >= 7
                elif timeframe == 'monthly':
                    return (current_time - last_order).days >= 30
                
        return True  # Allow order if no previous execution

    def generate_signals(self, data, reference_prices, current_time):
        signals = []
        symbol = data['symbol'].iloc[0] if 'symbol' in data else None
        if not symbol:
            return signals

        current_price = float(data['close'].iloc[-1])

        # Process each timeframe
        for timeframe in self.timeframe_priority:
            config = self.timeframe_config[timeframe]
            if not config['enabled'] or timeframe not in reference_prices:
                continue

            ref_price = float(reference_prices[timeframe]['open'])  # Ensure we get the 'open' price
            drop_percentage = (ref_price - current_price) / ref_price if ref_price > 0 else 0
            
            # Sort thresholds from lowest to highest
            sorted_thresholds = sorted(config['thresholds'])
            
            # Initialize order history for this symbol if not exists
            if symbol not in self.order_history[timeframe]:
                self.order_history[timeframe][symbol] = {}

            # Check each threshold from lowest to highest
            for threshold in sorted_thresholds:
                if threshold in self.order_history[timeframe][symbol]:
                    continue
                
                # Check if threshold is triggered
                if drop_percentage >= threshold:
                    if self.can_place_order(timeframe, symbol, threshold, current_time):
                        signals.append((timeframe, threshold, current_price))
                        self.order_history[timeframe][symbol][threshold] = current_time
                        break  # Stop checking higher thresholds for this timeframe
                    
        return signals