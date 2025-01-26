import pandas as pd
from datetime import datetime, timezone
import logging

class PriceDropStrategy:
    def __init__(self, config):
        """Initialize strategy with config"""
        self.logger = logging.getLogger('Strategy')
        self.config = config
        self.trading_settings = config.get('TRADING_SETTINGS', {})
        self.mode = self.trading_settings.get('MODE', 'spot')
        self.leverage = config.get('FUTURES_SETTINGS', {}).get('LEVERAGE', 1) if self.mode == 'futures' else 1
        self.timeframe_config = config.get('TIMEFRAMES', {})
        self.timeframe_priority = ['daily', 'weekly', 'monthly']
        
        # Initialize order history
        self.order_history = {
            'daily': {},    # {symbol: {threshold: last_order_time}}
            'weekly': {},
            'monthly': {}
        }
        
        self.logger.info(f"Strategy initialized in {self.mode.upper()} mode")
        if self.mode == 'futures':
            self.logger.info(f"Leverage: {self.leverage}x")

    def can_place_order(self, timeframe, symbol, threshold, current_time):
        """Check if order can be placed based on timeframe restrictions"""
        if symbol not in self.order_history[timeframe]:
            self.order_history[timeframe][symbol] = {}
            return True

        if threshold not in self.order_history[timeframe][symbol]:
            return True

        last_order = self.order_history[timeframe][symbol][threshold]
        wait_times = {
            'daily': 1,
            'weekly': 7,
            'monthly': 30
        }
        
        days_diff = (current_time - last_order).days
        return days_diff >= wait_times[timeframe]

    def calculate_position_size(self, available_balance, price, risk_percentage=1.0):
        """Calculate position size based on mode and leverage"""
        if self.mode == 'futures':
            # Account for leverage in futures mode
            position_size = (available_balance * risk_percentage / 100) * self.leverage / price
            return round(position_size, 8)
        else:
            # Regular spot calculation
            position_size = (available_balance * risk_percentage / 100) / price
            return round(position_size, 8)

    def generate_signals(self, data, reference_prices, current_time):
        """Generate trading signals based on price drops"""
        signals = []
        try:
            symbol = data['symbol'].iloc[0]
            current_price = float(data['close'].iloc[-1])

            # Process each timeframe in priority order
            for timeframe in self.timeframe_priority:
                config = self.timeframe_config.get(timeframe, {})
                if not config.get('enabled', False):
                    continue

                ref_data = reference_prices.get(timeframe, {})
                if not ref_data or not ref_data.get('open'):
                    continue

                ref_price = float(ref_data['open'])
                drop_percentage = ((ref_price - current_price) / ref_price) * 100 if ref_price > 0 else 0

                # Initialize order history for symbol
                if symbol not in self.order_history[timeframe]:
                    self.order_history[timeframe][symbol] = {}

                # Check thresholds in ascending order
                for threshold in sorted(config.get('thresholds', [])):
                    # Skip if already ordered at this threshold
                    if threshold in self.order_history[timeframe][symbol]:
                        continue

                    # Check if threshold is triggered
                    if drop_percentage >= threshold:
                        if self.can_place_order(timeframe, symbol, threshold, current_time):
                            signals.append((timeframe, threshold, current_price))
                            self.order_history[timeframe][symbol][threshold] = current_time
                            
                            self.logger.info(
                                f"Signal generated: {symbol} {timeframe} "
                                f"Drop: {drop_percentage:.2f}% "
                                f"Threshold: {threshold:.2f}%"
                            )
                            break  # Stop checking higher thresholds

        except Exception as e:
            self.logger.error(f"Error generating signals: {e}")

        return signals