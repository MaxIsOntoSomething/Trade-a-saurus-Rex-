import pandas as pd
from datetime import datetime, timezone, timedelta
import logging
from colorama import Fore, Style  # Add this import

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
        """Check if order can be placed with proper execution tracking"""
        try:
            executions = self.threshold_executions.get(timeframe, {}).get(symbol, {}).get(threshold, [])
            
            # Get timeframe duration
            durations = {
                'daily': timedelta(days=1),
                'weekly': timedelta(days=7),
                'monthly': timedelta(days=30)
            }
            duration = durations.get(timeframe)
            
            if not duration:
                return False
                
            # Clean old executions
            valid_executions = [
                t for t in executions 
                if (current_time - t) < duration
            ]
            
            # Update cleaned executions
            if symbol not in self.threshold_executions.get(timeframe, {}):
                self.threshold_executions[timeframe][symbol] = {}
            self.threshold_executions[timeframe][symbol][threshold] = valid_executions
            
            # Check if we can execute
            return len(valid_executions) == 0

        except Exception as e:
            self.logger.error(f"Error checking order placement: {e}")
            return False

    def record_execution(self, timeframe, symbol, threshold, execution_time):
        """Record threshold execution with proper initialization"""
        try:
            if timeframe not in self.threshold_executions:
                self.threshold_executions[timeframe] = {}
            
            if symbol not in self.threshold_executions[timeframe]:
                self.threshold_executions[timeframe][symbol] = {}
                
            if threshold not in self.threshold_executions[timeframe][symbol]:
                self.threshold_executions[timeframe][symbol][threshold] = []
                
            self.threshold_executions[timeframe][symbol][threshold].append(execution_time)
            
        except Exception as e:
            self.logger.error(f"Error recording execution: {e}")

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
        """Generate signals with proper tracking and display"""
        signals = []
        try:
            symbol = df['symbol'].iloc[0]
            current_price = float(df['close'].iloc[-1])

            # Format output line
            status_line = []

            for timeframe in self.timeframe_priority:
                if not self.timeframe_config.get(timeframe, {}).get('enabled', False):
                    continue

                ref_data = reference_prices.get(timeframe, {})
                if not ref_data or not ref_data.get('open'):
                    continue

                ref_price = float(ref_data['open'])
                drop_percentage = ((ref_price - current_price) / ref_price) * 100

                # Add status with color
                color = Fore.RED if drop_percentage > 0 else Fore.GREEN
                arrow = "↓" if drop_percentage > 0 else "↑"
                status_line.append(f"{timeframe}: {color}{abs(drop_percentage):+.2f}%{arrow}{Style.RESET_ALL}")

                # Check thresholds if price dropped
                if drop_percentage > 0:
                    thresholds = sorted(self.timeframe_config[timeframe].get('thresholds', []))
                    for threshold in thresholds:
                        threshold_pct = threshold * 100
                        
                        # Check if threshold was already executed
                        if self.can_place_order(timeframe, symbol, threshold, current_time):
                            if drop_percentage >= threshold_pct:
                                self.record_execution(timeframe, symbol, threshold, current_time)
                                signals.append((timeframe, threshold, current_price))
                                return signals  # Return on first valid signal

            # Print status if no signals
            if not signals and status_line:
                print(f"\r{symbol}: {current_price:.8f} | {' | '.join(status_line)}", end='', flush=True)

        except Exception as e:
            self.logger.error(f"Error generating signals: {e}")

        return signals

    async def handle_price_update(self, symbol, price):
        """Handle price updates with improved display"""
        try:
            df = pd.DataFrame({'symbol': [symbol], 'close': [price]})
            reference_prices = self.get_reference_prices(symbol)
            
            if not reference_prices:
                return
                
            signals = self.strategy.generate_signals(df, reference_prices, datetime.now(timezone.utc))
            
            if signals:
                print(f"\n{Fore.CYAN}Checking {symbol} Thresholds:")
                
                # Show all thresholds for each timeframe
                for timeframe in ['daily', 'weekly', 'monthly']:
                    config = self.timeframe_config.get(timeframe, {})
                    if not config.get('enabled', False):
                        continue
                        
                    print(f"\n{timeframe.capitalize()}:")
                    for threshold in config.get('thresholds', []):
                        threshold_pct = threshold * 100
                        drop = ((reference_prices[timeframe]['open'] - price) / 
                               reference_prices[timeframe]['open']) * 100
                        
                        # Check if threshold was executed
                        executed = False
                        if (symbol in self.strategy.threshold_executions.get(timeframe, {}) and
                            threshold in self.strategy.threshold_executions[timeframe].get(symbol, {})):
                            executed = True
                        
                        # Format status with colors
                        if executed:
                            status = f"{Fore.GREEN}✓"
                        elif drop >= threshold_pct:
                            status = f"{Fore.YELLOW}⚡"
                        else:
                            status = f"{Fore.RED}✗"
                        
                        print(f"  {status} {threshold_pct:>5.1f}% ({drop:>6.2f}%){Style.RESET_ALL}")
                
                # Show execution signals
                for timeframe, threshold, signal_price in signals:
                    print(f"\n🎯 Valid Signal: {symbol} ({timeframe} -{threshold*100}%)")
                    await self.execute_trade(symbol, price)
                    
        except Exception as e:
            self.logger.error(f"Error handling price update: {e}")