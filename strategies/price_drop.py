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
        
        # Fix timeframe config initialization
        self.timeframe_config = config.get('TIMEFRAMES', {})
        if not self.timeframe_config:
            self.logger.error("No timeframe configuration found in config")
            self.timeframe_config = {
                'daily': {'enabled': True, 'thresholds': [0.01, 0.02, 0.03]},
                'weekly': {'enabled': True, 'thresholds': [0.03, 0.06, 0.10]},
                'monthly': {'enabled': True, 'thresholds': [0.05, 0.10]}
            }
        
        # Log the loaded configuration
        self.logger.info("Loaded timeframe configuration:")
        for timeframe, config in self.timeframe_config.items():
            self.logger.info(f"{timeframe}: enabled={config.get('enabled', False)}, thresholds={config.get('thresholds', [])}")
            
        self.timeframe_priority = ['daily', 'weekly', 'monthly']  # Enforce priority order
        
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

    def generate_signals(self, df, reference_prices, current_time):
        """Generate signals with priority order"""
        signals = []
        signal_found = False  # Track if higher priority signal found
        try:
            symbol = df['symbol'].iloc[0]
            current_price = float(df['close'].iloc[-1])
            
            print(f"\nAnalyzing {symbol}:")
            print(f"Current Price: {current_price}")
            
            # Process timeframes in strict priority order
            for timeframe in self.timeframe_priority:
                if signal_found:
                    break  # Skip lower priority timeframes if signal found
                    
                timeframe_cfg = self.timeframe_config.get(timeframe, {})
                enabled = timeframe_cfg.get('enabled', False)
                thresholds = timeframe_cfg.get('thresholds', [])
                
                print(f"\nTimeframe {timeframe}:")
                print(f"Enabled: {enabled}")
                print(f"Thresholds: {[t*100 for t in thresholds]}%")

                if not enabled:
                    continue

                ref_data = reference_prices.get(timeframe, {})
                if not ref_data or not ref_data.get('open'):
                    continue

                ref_price = float(ref_data['open'])
                drop_percentage = ((ref_price - current_price) / ref_price) * 100

                print(f"Reference Price: {ref_price}")
                print(f"Current Drop: {drop_percentage:.2f}%")

                # Check thresholds in ascending order
                for threshold in sorted(thresholds):
                    threshold_pct = threshold * 100
                    print(f"Checking {threshold_pct}% threshold...")
                    
                    if drop_percentage >= threshold_pct:
                        if self.can_place_order(timeframe, symbol, threshold, current_time):
                            print(f"✅ Signal generated: {threshold_pct}% threshold met!")
                            signals.append((timeframe, threshold, current_price))
                            signal_found = True  # Mark signal found to skip lower priorities
                            break
                        else:
                            print(f"⏳ Signal blocked by cooldown")
                    else:
                        print(f"❌ Drop {drop_percentage:.2f}% not enough for {threshold_pct}% threshold")

        except Exception as e:
            self.logger.error(f"Error generating signals: {e}")

        return signals[:1]  # Only return the highest priority signal