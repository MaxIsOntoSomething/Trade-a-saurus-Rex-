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

        # Process each timeframe
        for timeframe in self.timeframe_priority:
            config = self.timeframe_config[timeframe]
            if not config['enabled']:
                continue

            ref_price = reference_prices[timeframe]
            last_price = float(data['close'].iloc[-1])
            drop_percentage = (ref_price - last_price) / ref_price
            
            # Sort thresholds from lowest to highest
            sorted_thresholds = sorted(config['thresholds'])
            
            # Initialize order history for this symbol if not exists
            if symbol not in self.order_history[timeframe]:
                self.order_history[timeframe][symbol] = {}

            # Check each threshold from lowest to highest
            for threshold in sorted_thresholds:
                # Skip if this threshold was already executed for this symbol and timeframe
                if threshold in self.order_history[timeframe][symbol]:
                    print(f"Skipping {timeframe} {threshold*100}% threshold for {symbol} - already executed")
                    continue
                
                # Check if threshold is triggered
                if drop_percentage >= threshold:
                    if self.can_place_order(timeframe, symbol, threshold, current_time):
                        print(f"Signal: {timeframe} {threshold*100}% threshold triggered for {symbol}")
                        signals.append((timeframe, threshold, last_price))
                        self.order_history[timeframe][symbol][threshold] = current_time
                        break  # Stop checking higher thresholds for this timeframe
                    
        return signals