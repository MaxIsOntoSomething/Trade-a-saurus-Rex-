import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as ticker
from datetime import datetime, timedelta  # Added timedelta import
from decimal import Decimal
import io
import logging
import pandas as pd  # Add missing pandas import
from typing import List, Dict, Optional
from ..types.models import Order, OrderType, TradeDirection, TimeFrame  # Added TimeFrame import
import mplfinance as mpf

logger = logging.getLogger(__name__)

class ChartGenerator:
    def __init__(self):
        plt.style.use('dark_background')
        self.colors = {
            'up': '#26a69a',    # Green for up candles
            'down': '#ef5350',   # Red for down candles
            'line': '#e0e0e0',   # White for lines
            'entry': '#ffeb3b',  # Yellow for entry line
            'liq_long': '#ef5350',  # Red for long liquidation
            'liq_short': '#26a69a',  # Green for short liquidation
            'futures_long': '#26a69a',  # Green for long trades
            'futures_short': '#ef5350',  # Red for short trades
            'spot': '#ffeb3b'    # Yellow for spot trades
        }
        
        # Timeframe formats for x-axis
        self.timeframe_formats = {
            TimeFrame.DAILY: '%H:%M',    # Show hours and minutes for daily
            TimeFrame.WEEKLY: '%Y-%m-%d', # Show full date for weekly
            TimeFrame.MONTHLY: '%Y-%m-%d' # Show full date for monthly
        }
        
        # Add default widths for timeframes (in seconds)
        self.default_widths = {
            TimeFrame.DAILY: 3600,     # 1 hour
            TimeFrame.WEEKLY: 86400,   # 1 day
            TimeFrame.MONTHLY: 259200  # 3 days
        }
        
        # Add minimum periods requirement
        self.min_periods = 1  # Changed from 2 to 1
        self.required_periods = 8

        # Add requirements for proper chart generation
        self.required_candles = 8
        self.price_padding = 0.1  # 10% padding for price range

        # Chart requirements
        self.requirements = {
            TimeFrame.MONTHLY: {
                'required': 8,
                'minimum': 1,
                'format': '%Y-%m',
                'label': 'Month',
                'locator': mdates.MonthLocator()
            },
            TimeFrame.WEEKLY: {
                'required': 8,
                'minimum': 1,
                'format': '%Y-%m-%d',
                'label': 'Week',
                'locator': mdates.WeekdayLocator(byweekday=mdates.MO)
            },
            TimeFrame.DAILY: {
                'required': 8,
                'minimum': 1,
                'format': '%m-%d',
                'label': 'Day',
                'locator': mdates.DayLocator()
            }
        }

        # Y-axis scaling parameters
        self.y_axis_params = {
            'price_padding': 0.1,  # 10% padding
            'min_price_range': 1.0,  # Minimum price range to show
            'outlier_threshold': 3.0  # Standard deviations for outlier detection
        }

        self.style = mpf.make_mpf_style(
            base_mpf_style='yahoo',
            gridstyle='',
            y_on_right=True,
            marketcolors=mpf.make_marketcolors(
                up='#26a69a',
                down='#ef5350',
                edge='inherit',
                wick='inherit',
                volume='in',
                ohlc='inherit'
            ),
            rc={
                'axes.labelsize': 12,
                'axes.titlesize': 14,
                'font.size': 12
            }
        )

    def validate_candles(self, candles: List[Dict], timeframe: TimeFrame) -> tuple[bool, str]:
        """Validate candle data with detailed logging"""
        logger.info(f"Validating {len(candles) if candles else 0} candles for {timeframe.value}")
        
        if not candles:
            logger.error("No candle data provided")
            return False, "No candle data available"

        # Log raw candle data for debugging
        logger.debug(f"Raw candle data: {candles[:2]}")  # Log first 2 candles

        valid_candles = []
        for i, candle in enumerate(candles):
            try:
                # Log each candle's format
                logger.debug(f"Candle {i + 1} format: {list(candle.keys())}")
                
                # Check timestamp format
                timestamp = candle.get('timestamp')
                logger.debug(f"Timestamp for candle {i + 1}: {timestamp}")
                
                # Check price data
                price_fields = {
                    'open': candle.get('open'),
                    'high': candle.get('high'),
                    'low': candle.get('low'),
                    'close': candle.get('close')
                }
                logger.debug(f"Price data for candle {i + 1}: {price_fields}")
                
                # Validate price data
                if all(isinstance(price, (int, float, str, Decimal)) for price in price_fields.values()):
                    valid_candles.append(candle)
                else:
                    logger.warning(f"Invalid price data in candle {i + 1}: {price_fields}")
                    
            except Exception as e:
                logger.error(f"Error validating candle {i + 1}: {e}")
                continue

        # Log validation results
        logger.info(f"Found {len(valid_candles)} valid candles out of {len(candles)}")
        
        if len(valid_candles) < self.min_periods:
            msg = f"Insufficient valid candles: got {len(valid_candles)}, need {self.min_periods}"
            logger.error(msg)
            return False, msg

        return True, ""

    def calculate_liquidation_price(self, order: Order) -> Optional[float]:
        """Calculate liquidation price for futures orders"""
        if order.order_type != OrderType.FUTURES or not order.leverage:
            return None

        try:
            entry_price = float(order.price)
            leverage = float(order.leverage)
            
            # Simplified liquidation calculation (adjust maintenance margin as needed)
            maintenance_margin = 0.01  # 1% maintenance margin
            
            if order.direction == TradeDirection.LONG:
                liq_price = entry_price * (1 - (1 / leverage) + maintenance_margin)
            else:
                liq_price = entry_price * (1 + (1 / leverage) - maintenance_margin)
            
            return liq_price
        except Exception as e:
            logger.error(f"Error calculating liquidation price: {e}")
            return None

    def get_default_width(self, timeframe: TimeFrame) -> float:
        """Get default candle width for timeframe"""
        return self.default_widths.get(timeframe, 3600)  # Default to 1 hour if unknown

    def calculate_axis_limits(self, prices: List[float], reference_price: Optional[float] = None,
                            entry_price: Optional[float] = None) -> tuple[float, float]:
        """Calculate optimal Y-axis limits"""
        if not prices:
            return 0, 0

        # Include reference and entry prices in range calculation
        all_prices = prices.copy()
        if reference_price:
            all_prices.append(reference_price)
        if entry_price:
            all_prices.append(entry_price)

        # Calculate statistics for outlier detection
        mean_price = sum(all_prices) / len(all_prices)
        std_dev = (sum((x - mean_price) ** 2 for x in all_prices) / len(all_prices)) ** 0.5
        
        # Filter outliers
        filtered_prices = [p for p in all_prices if 
                         abs(p - mean_price) <= self.y_axis_params['outlier_threshold'] * std_dev]
        
        if not filtered_prices:
            filtered_prices = all_prices  # Use all prices if filtering removed everything

        min_price = min(filtered_prices)
        max_price = max(filtered_prices)
        price_range = max_price - min_price

        # Ensure minimum range
        if price_range < self.y_axis_params['min_price_range']:
            mid_price = (min_price + max_price) / 2
            min_price = mid_price - self.y_axis_params['min_price_range'] / 2
            max_price = mid_price + self.y_axis_params['min_price_range'] / 2

        # Add padding
        padding = price_range * self.y_axis_params['price_padding']
        return min_price - padding, max_price + padding

    def prepare_candle_data(self, candles: List[Dict]) -> pd.DataFrame:
        """Convert candle data to pandas DataFrame with proper timestamp handling"""
        data = []
        for candle in candles:
            # Handle Unix timestamp in milliseconds
            if isinstance(candle['timestamp'], (int, float)):
                timestamp = datetime.fromtimestamp(int(candle['timestamp']) / 1000)
            else:
                # Try parsing string timestamp
                timestamp = datetime.strptime(candle['timestamp'], "%Y-%m-%d")

            data.append({
                'Date': timestamp,
                'Open': float(candle['open']),
                'High': float(candle['high']),
                'Low': float(candle['low']),
                'Close': float(candle['close']),
                'Volume': float(candle.get('volume', 0))
            })

        df = pd.DataFrame(data)
        df.set_index('Date', inplace=True)
        return df

    async def generate_trade_chart(self, candles: List[Dict], order: Order, 
                                 reference_price: Optional[Decimal] = None) -> Optional[bytes]:
        """Generate a candlestick chart"""
        try:
            logger.info(f"Generating chart for {order.symbol} ({order.timeframe.value})")
            logger.info(f"Order type: {order.order_type.value}")
            
            # Validate data
            is_valid, message = self.validate_candles(candles, order.timeframe)
            if not is_valid:
                logger.warning(f"Chart validation failed: {message}")
                return self._generate_error_chart(message)

            if not candles or len(candles) < 15:  # Require minimum 15 candles
                logger.error("Not enough candles for chart generation")
                return None

            df = self.prepare_candle_data(candles)
            
            # Validate reference price
            ref_value = float(reference_price) if reference_price else None
            opening_price = float(df.iloc[0]['open'])

            # Create plots
            addplots = []

            # Add entry point marker
            entry_time = order.filled_at or order.created_at
            if entry_time:
                entry_series = pd.Series(index=df.index, dtype=float)
                entry_series.loc[:] = float('nan')
                closest_time = min(df.index, key=lambda x: abs(x - entry_time))
                entry_series.loc[closest_time] = float(order.price)
                addplots.append(mpf.make_addplot(
                    entry_series,
                    type='scatter',
                    marker='^',
                    markersize=100,
                    color='lime'
                ))

            # Add reference price line
            if ref_value and not pd.isna(ref_value):
                ref_series = pd.Series([ref_value] * len(df), index=df.index)
                addplots.append(mpf.make_addplot(
                    ref_series,
                    type='line',
                    color='blue',
                    linestyle='--',
                    width=1
                ))

            # Create plot
            buf = io.BytesIO()
            entry_change = ((float(order.price) - opening_price) / opening_price) * 100
            current_change = ((float(df.iloc[-1]['close']) - opening_price) / opening_price) * 100

            title = (
                f"{order.symbol} Trade Analysis ({order.timeframe.value})\n"
                f"Open: ${opening_price:.2f} | Entry: ${float(order.price):.2f} ({entry_change:+.2f}%)\n"
                f"Current: ${float(df.iloc[-1]['close']):.2f} ({current_change:+.2f}%)"
            )

            mpf.plot(
                df,
                type='candle',
                style=self.style,
                title=title,
                ylabel='Price (USDT)',
                ylabel_lower='Volume',
                volume=True,
                figsize=(12, 8),
                addplot=addplots,
                savefig=dict(fname=buf, dpi=150, bbox_inches='tight')
            )

            buf.seek(0)
            return buf.getvalue()

        except Exception as e:
            logger.error(f"Error generating chart: {e}")
            return None

    def _generate_error_chart(self, message: str) -> Optional[bytes]:
        """Generate an error message chart"""
        try:
            fig, ax = plt.subplots(figsize=(12, 6))
            ax.text(0.5, 0.5, message, 
                   ha='center', va='center',
                   wrap=True,
                   color='red')
            ax.set_axis_off()
            
            buf = io.BytesIO()
            plt.savefig(buf, format='png', bbox_inches='tight', dpi=100)
            plt.close(fig)
            buf.seek(0)
            return buf.getvalue()
        except Exception as e:
            logger.error(f"Failed to generate error chart: {e}")
            return None

    def format_chart_axes(self, ax1, ax2, timeframe: TimeFrame, times):
        """Format chart axes with proper time formatting and grid"""
        # Format price axis
        ax1.grid(True, alpha=0.2)
        ax2.grid(True, alpha=0.2)
        ax1.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, p: f'${x:,.2f}'))
        
        # Format dates based on timeframe
        date_format = self.timeframe_formats.get(timeframe, '%Y-%m-%d %H:%M')
        ax1.xaxis.set_major_formatter(mdates.DateFormatter(date_format))
        ax2.xaxis.set_major_formatter(mdates.DateFormatter(date_format))
        
        # Auto-rotate and align the tick labels for better readability
        plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45, ha='right')
        plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45, ha='right')

        # Set proper time axis limits
        if len(times) > 1:
            margin = 0.05  # 5% margin on each side
            time_range = (times[-1] - times[0]).total_seconds()
            margin_seconds = time_range * margin
            ax1.set_xlim(
                times[0] - timedelta(seconds=margin_seconds),
                times[-1] + timedelta(seconds=margin_seconds)
            )
            ax2.set_xlim(
                times[0] - timedelta(seconds=margin_seconds),
                times[-1] + timedelta(seconds=margin_seconds)
            )

    def add_chart_footer(self, fig, order: Order):
        """Add footer with trade information"""
        footer_text = (
            f"{order.symbol} • {order.timeframe.value.title()} • "
            f"{order.order_type.value.upper()} • "
            f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC"
        )
        if order.order_type == OrderType.FUTURES:
            footer_text = f"{footer_text} • {order.leverage}x • {order.direction.value.upper()}"

        plt.figtext(0.99, 0.01, footer_text,
                   ha='right', va='bottom',
                   color='gray', alpha=0.7,
                   fontsize=8)

    def format_info_text(self, order: Order, reference_price: Optional[Decimal] = None) -> str:
        """Format trade information text"""
        try:
            info = [
                f"Trade Details for {order.symbol}:",
                f"Entry Price: ${float(order.price):.2f}"
            ]
            
            if reference_price is not None:
                order_price = Decimal(str(order.price))
                change = ((order_price - reference_price) / reference_price) * Decimal('100')
                info.append(f"Reference Price: ${float(reference_price):.2f} ({float(change):+.2f}%)")
                
            info.extend([
                f"Amount: {float(order.quantity):.8f}",
                f"Total Value: ${float(order.price * order.quantity)::.2f}",
                f"Type: {order.order_type.value.upper()}"
            ])
            
            if order.leverage:
                info.append(f"Leverage: {order.leverage}x")
            if order.direction:
                info.append(f"Direction: {order.direction.value.upper()}")
                
            return "\n".join(info)
            
        except Exception as e:
            logger.error(f"Error formatting info text: {e}", exc_info=True)
            return "Error generating trade information"
