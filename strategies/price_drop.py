import pandas as pd
from datetime import datetime, timezone
import logging
from colorama import Fore  # Add this import

class PriceDropStrategy:
    def __init__(self, config):
        self.logger = logging.getLogger('Strategy')
        
        # Get timeframe config from TRADING_SETTINGS
        self.timeframe_config = config.get('TIMEFRAMES', {})
        if not self.timeframe_config:
            self.logger.warning("No timeframe config provided, using defaults")
            self.timeframe_config = {
                'daily': {
                    'enabled': True,
                    'thresholds': [0.01, 0.02, 0.03]
                },
                'weekly': {
                    'enabled': True,
                    'thresholds': [0.03, 0.06, 0.10]
                },
                'monthly': {
                    'enabled': True,
                    'thresholds': [0.05, 0.10]
                }
            }
        
        self.trading_settings = config.get('TRADING_SETTINGS', {})
        self.mode = self.trading_settings.get('MODE', 'spot')
        self.leverage = config.get('FUTURES_SETTINGS', {}).get('LEVERAGE', 1) if self.mode == 'futures' else 1
        
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

        # Add threshold execution tracking
        self.threshold_executions = {
            timeframe: {} for timeframe in ['daily', 'weekly', 'monthly']
        }

    def can_place_order(self, timeframe, symbol, threshold, current_time):
        """Enhanced order placement check with proper threshold limits"""
        try:
            # Each threshold has its own independent execution limit
            if symbol not in self.threshold_executions[timeframe]:
                self.threshold_executions[timeframe][symbol] = {}
            if threshold not in self.threshold_executions[timeframe][symbol]:
                self.threshold_executions[timeframe][symbol][threshold] = []
            
            executions = self.threshold_executions[timeframe][symbol][threshold]
            
            # Clean old executions based on timeframe
            max_age_days = {
                'daily': 1,
                'weekly': 7,
                'monthly': 30
            }[timeframe]
            
            cleaned_executions = [
                t for t in executions 
                if (current_time - t).days < max_age_days
            ]
            
            # Update cleaned executions
            self.threshold_executions[timeframe][symbol][threshold] = cleaned_executions
            
            # Each threshold allows 1 execution within its timeframe
            return len(cleaned_executions) < 1

        except Exception as e:
            self.logger.error(f"Error checking order placement: {e}")
            return False

    def record_execution(self, timeframe, symbol, threshold, execution_time):
        """Record when a threshold was executed"""
        if symbol not in self.threshold_executions[timeframe]:
            self.threshold_executions[timeframe][symbol] = {}
        if threshold not in self.threshold_executions[timeframe][symbol]:
            self.threshold_executions[timeframe][symbol][threshold] = []
            
        self.threshold_executions[timeframe][symbol][threshold].append(execution_time)

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
        """Generate signals with proper threshold tracking"""
        signals = []
        try:
            symbol = df['symbol'].iloc[0]
            current_price = float(df['close'].iloc[-1])
            
            # Log full details
            self.logger.info(f"Analyzing {symbol} @ {current_price} USDT")
            
            for timeframe in self.timeframe_priority:
                if not self.timeframe_config.get(timeframe, {}).get('enabled', False):
                    continue
                    
                ref_data = reference_prices.get(timeframe, {})
                if not ref_data or not ref_data.get('open'):
                    continue

                ref_price = float(ref_data['open'])
                drop_percentage = ((ref_price - current_price) / ref_price) * 100

                # Only log drops that meet a threshold
                if drop_percentage > 0:
                    self.logger.info(
                        f"{timeframe}: {drop_percentage:.2f}% drop from {ref_price:.8f}"
                    )

                thresholds = sorted(self.timeframe_config[timeframe].get('thresholds', []))
                
                for threshold in thresholds:
                    threshold_pct = threshold * 100
                    
                    # Skip if already executed max times
                    if not self.can_place_order(timeframe, symbol, threshold, current_time):
                        continue

                    if drop_percentage >= threshold_pct:
                        # Record execution
                        self.record_execution(timeframe, symbol, threshold, current_time)
                        signals.append((timeframe, threshold, current_price))
                        return signals

        except Exception as e:
            self.logger.error(f"Error generating signals: {e}")

        return signals