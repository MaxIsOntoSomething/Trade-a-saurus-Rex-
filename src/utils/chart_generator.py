import matplotlib
# Force matplotlib to use Agg backend to avoid Tkinter thread issues
matplotlib.use('Agg')
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
import numpy as np

logger = logging.getLogger(__name__)

class ChartGenerator:
    def __init__(self):
        # Remove plt.style.use('dark_background') as it can cause thread issues
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

        # Modify style to use dark theme without Tkinter dependencies
        self.style = mpf.make_mpf_style(
            base_mpf_style='charles',
            gridstyle='',
            y_on_right=False,
            marketcolors=mpf.make_marketcolors(
                up='#26a69a',      # Green for bullish
                down='#ef5350',    # Red for bearish
                edge='inherit',
                wick='inherit',
                volume={
                    'up': '#26a69a55',   # Transparent green
                    'down': '#ef535055'   # Transparent red
                }
            ),
            rc={
                'font.size': 8,
                'axes.titlesize': 10,
                'axes.labelsize': 8,
                'figure.facecolor': '#1e1e1e',  # Dark background
                'axes.facecolor': '#1e1e1e',    # Dark background for axes
                'axes.edgecolor': '#666666',    # Light gray edges
                'axes.labelcolor': '#ffffff',   # White labels
                'xtick.color': '#ffffff',       # White ticks
                'ytick.color': '#ffffff'        # White ticks
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

    def prepare_candle_data(self, candles: List[Dict], timeframe: TimeFrame) -> pd.DataFrame:
        """Convert candle data to pandas DataFrame with proper aggregation"""
        data = []
        for candle in candles:
            timestamp = datetime.fromtimestamp(int(candle['timestamp']) / 1000)
            
            # For monthly view, aggregate to monthly OHLC
            if timeframe == TimeFrame.MONTHLY:
                # Set timestamp to first day of month
                timestamp = timestamp.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            
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

        # Aggregate data for monthly view
        if timeframe == TimeFrame.MONTHLY:
            df = df.groupby(pd.Grouper(freq='M')).agg({
                'Open': 'first',
                'High': 'max',
                'Low': 'min',
                'Close': 'last',
                'Volume': 'sum'
            })
        elif timeframe == TimeFrame.WEEKLY:
            df = df.groupby(pd.Grouper(freq='W-MON')).agg({
                'Open': 'first',
                'High': 'max',
                'Low': 'min',
                'Close': 'last',
                'Volume': 'sum'
            })
        elif timeframe == TimeFrame.DAILY:
            df = df.groupby(pd.Grouper(freq='D')).agg({
                'Open': 'first',
                'High': 'max',
                'Low': 'min',
                'Close': 'last',
                'Volume': 'sum'
            })
            
        return df.dropna()

    async def generate_trade_chart(self, candles: List[Dict], order: Order, 
                                 reference_price: Optional[Decimal] = None) -> Optional[bytes]:
        """Generate trade chart with proper timeframe handling"""
        try:
            if not candles:
                logger.error("No candles provided for chart generation")
                return self._generate_error_chart("No candle data available")

            # Sort and process candles
            candles.sort(key=lambda x: x['timestamp'])
            df = self.prepare_candle_data(candles, order.timeframe)

            # Adjust buffer based on timeframe
            buffer_sizes = {
                TimeFrame.MONTHLY: timedelta(days=5),
                TimeFrame.WEEKLY: timedelta(days=1),
                TimeFrame.DAILY: timedelta(hours=1)
            }
            buffer_time = buffer_sizes.get(order.timeframe, timedelta(hours=1))

            # Check if order time is within range (with buffer)
            if order.created_at < df.index[0] - buffer_time or order.created_at > df.index[-1] + buffer_time:
                error_msg = (
                    f"Order time ({order.created_at}) outside candle data range\n"
                    f"Data range: {df.index[0]} to {df.index[-1]}"
                )
                logger.error(error_msg)
                return self._generate_error_chart(error_msg)

            # Calculate percentage changes accurately using Decimal
            current_price = Decimal(str(df['Close'].iloc[-1]))  # Changed from 'close' to 'Close'
            entry_price = Decimal(str(order.price))
            
            logger.debug(f"Available columns: {df.columns.tolist()}")  # Add debug logging
            
            if reference_price:
                ref_change = ((current_price - reference_price) / reference_price) * 100
            else:
                ref_change = Decimal('0')
                
            entry_change = ((current_price - entry_price) / entry_price) * 100

            # Calculate volume statistics for highlighting
            volume_mean = df['Volume'].mean()  # Changed from 'volume' to 'Volume'
            volume_std = df['Volume'].std()
            high_volume_threshold = volume_mean + volume_std
            
            # Create volume colors array
            colors = np.where(df['Volume'] > high_volume_threshold, '#FF9800', '#78909C')
            
            # Enhanced plot configuration
            fig, axes = mpf.plot(
                df,
                type='candle',
                style=self.style,
                volume=True,
                returnfig=True,
                title=f'\n{order.symbol} - {order.timeframe.value} ({order.order_type.value.upper()})',
                figsize=(14, 9),  # Larger figure size
                panel_ratios=(7, 2),  # Better ratio between price and volume
                volume_panel=1,
                addplot=[
                    mpf.make_addplot(df['Volume'], type='bar', panel=1, 
                                   color=colors, alpha=0.8)
                ]
            )

            # Enhanced title with better spacing and formatting
            fig.suptitle(f'{order.symbol} - {order.timeframe.value}\n' +
                        f'Entry: ${float(entry_price):,.2f} | Current: ${float(current_price):,.2f}\n' +
                        f'Change: {float(entry_change):+.2f}%',
                        y=0.95, fontsize=12, color='white')

            # Add enhanced legend with background
            legend_elements = [
                plt.Line2D([0], [0], color='#00C853', label='Bullish', linewidth=2),
                plt.Line2D([0], [0], color='#FF1744', label='Bearish', linewidth=2),
                plt.Line2D([0], [0], color='#FF9800', label='High Volume', linewidth=2)
            ]
            legend = axes[0].legend(handles=legend_elements, 
                                  loc='upper left',
                                  fontsize=9,
                                  facecolor='#2a2a2a',
                                  edgecolor='#787878',
                                  framealpha=0.8)
            for text in legend.get_texts():
                text.set_color('white')

            # Enhanced price line annotations
            self._add_price_lines(axes[0], order, entry_price, reference_price)

            # Improve axes scaling and formatting
            self._format_axes(axes[0], axes[1], df)

            # Add enhanced volume analysis
            self._add_volume_analysis(axes[1], df)

            # Add trade information footer
            self._add_enhanced_footer(fig, order)

            # Fine-tune layout
            plt.tight_layout()
            
            # Save with higher DPI for better quality
            buf = io.BytesIO()
            fig.savefig(buf, format='png', bbox_inches='tight', dpi=150,
                       facecolor='#1a1a1a', edgecolor='none')
            buf.seek(0)
            return buf.getvalue()

        except Exception as e:
            logger.error(f"Chart generation failed: {e}", exc_info=True)
            return self._generate_error_chart(f"Chart generation error: {str(e)}")

    def _add_price_lines(self, ax, order: Order, entry_price: Decimal, reference_price: Optional[Decimal]):
        """Add enhanced price lines and annotations including TP/SL for futures"""
        # Entry line with gradient effect
        ax.axhline(y=float(entry_price), color='#4CAF50', linestyle='-', 
                  alpha=0.3, linewidth=1.5)
        
        # Add entry price marker on the right side
        ax.annotate(f'Entry ${float(entry_price):,.2f}',
                   xy=(ax.get_xlim()[1], float(entry_price)),
                   xytext=(5, 0),
                   textcoords='offset points',
                   color='#4CAF50',
                   fontsize=9,
                   ha='left',
                   bbox=dict(facecolor='#2a2a2a', edgecolor='#4CAF50', alpha=0.7))

        # Add current price marker at the top right corner of the chart
        current_price = float(order.price)
        ax.annotate(f'Current ${current_price:,.2f}',
                   xy=(1, 1.05),  # Position at the top right
                   xycoords='axes fraction',
                   textcoords='offset points',
                   color='#FFEB3B',
                   fontsize=10,
                   ha='right',
                   bbox=dict(facecolor='#2a2a2a', edgecolor='#FFEB3B', alpha=0.7))

        # Add reference price if available
        if reference_price:
            ax.axhline(y=float(reference_price), color='#2196F3', 
                      linestyle='--', alpha=0.3, linewidth=1.5)
            ax.annotate(f'Ref ${float(reference_price):,.2f}',
                       xy=(ax.get_xlim()[0], float(reference_price)),
                       xytext=(-5, 0),
                       textcoords='offset points',
                       color='#2196F3',
                       fontsize=9,
                       ha='right',
                       bbox=dict(facecolor='#2a2a2a', edgecolor='#2196F3', alpha=0.7))

        # Add TP line for futures orders
        if order.order_type == OrderType.FUTURES and hasattr(order, 'tp_price') and order.tp_price:
            ax.axhline(y=float(order.tp_price), color='#00C853', 
                      linestyle=':', alpha=0.5, linewidth=1.5)
            ax.annotate(f'TP ${float(order.tp_price):,.2f}',
                       xy=(ax.get_xlim()[0], float(order.tp_price)),
                       xytext=(-5, 0),
                       textcoords='offset points',
                       color='#00C853',
                       fontsize=9,
                       ha='right',
                       bbox=dict(facecolor='#2a2a2a', edgecolor='#00C853', alpha=0.7))

        # Add SL line for futures orders
        if order.order_type == OrderType.FUTURES and hasattr(order, 'sl_price') and order.sl_price:
            ax.axhline(y=float(order.sl_price), color='#FF1744', 
                      linestyle=':', alpha=0.5, linewidth=1.5)
            ax.annotate(f'SL ${float(order.sl_price):,.2f}',
                       xy=(ax.get_xlim()[0], float(order.sl_price)),
                       xytext=(-5, 0),
                       textcoords='offset points',
                       color='#FF1744',
                       fontsize=9,
                       ha='right',
                       bbox=dict(facecolor='#2a2a2a', edgecolor='#FF1744', alpha=0.7))

    def _generate_error_chart(self, message: str) -> Optional[bytes]:
        """Generate an error message chart with thread-safe configuration"""
        try:
            # Create figure with dark theme manually
            plt.rcParams['figure.facecolor'] = '#1e1e1e'
            plt.rcParams['axes.facecolor'] = '#1e1e1e'
            plt.rcParams['text.color'] = '#ffffff'
            
            fig, ax = plt.subplots(figsize=(12, 6))
            ax.set_facecolor('#1e1e1e')
            
            ax.text(0.5, 0.5, f"⚠️ {message}",
                   ha='center', va='center',
                   wrap=True,
                   color='red',
                   fontsize=12)
            ax.text(0.5, 0.4, 
                   "Please check the following:\n" +
                   "• Data availability\n" +
                   "• Timeframe configuration\n" +
                   "• API connection status",
                   ha='center', va='center',
                   wrap=True,
                   color='gray',
                   fontsize=10)
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

    def _format_axes(self, price_ax, volume_ax, df: pd.DataFrame):
        """Enhanced axes formatting"""
        # Calculate optimal price range
        price_range = df['High'].max() - df['Low'].min()
        margin = price_range * 0.1
        price_ax.set_ylim(df['Low'].min() - margin, df['High'].max() + margin)

        # Format price axis
        price_ax.yaxis.set_major_formatter(ticker.FuncFormatter(
            lambda x, p: f'${x:,.2f}'))
        price_ax.grid(True, alpha=0.15, linestyle=':')

        # Format volume axis
        max_volume = df['Volume'].max()
        volume_ax.set_ylim(0, max_volume * 1.1)
        volume_ax.yaxis.set_major_formatter(ticker.FuncFormatter(
            lambda x, p: f'{x:,.0f}'))

    def _add_volume_analysis(self, ax, df: pd.DataFrame):
        """Add enhanced volume analysis"""
        # Calculate and plot volume moving average
        volume_ma = df['Volume'].rolling(window=20).mean()
        ax.plot(df.index, volume_ma, color='#FF9800', 
                alpha=0.7, linewidth=1, label='Volume MA(20)')

        # Highlight significant volume bars
        volume_std = df['Volume'].std()
        volume_mean = df['Volume'].mean()
        significant_volume = df['Volume'] > (volume_mean + 2 * volume_std)
        
        for idx in df.index[significant_volume]:
            ax.annotate('⚡',
                       xy=(idx, df.loc[idx, 'Volume']),
                       xytext=(0, 5),
                       textcoords='offset points',
                       ha='center',
                       fontsize=8,
                       alpha=0.8)

    def _add_enhanced_footer(self, fig, order: Order):
        """Add enhanced footer with trade information"""
        # Get current mode
        mode = "FUTURES" if order.order_type == OrderType.FUTURES else "SPOT"
        
        footer_text = (
            f"{order.symbol} • {order.timeframe.value.title()} • "
            f"{mode}"
        )

        # Add leverage and direction for futures
        if order.order_type == OrderType.FUTURES:
            footer_text += f" • {order.leverage}x • {order.direction.value.upper()}"
            # Add TP/SL if present
            if hasattr(order, 'tp_price') and order.tp_price:
                footer_text += f" • TP: ${float(order.tp_price):.2f}"
            if hasattr(order, 'sl_price') and order.sl_price:
                footer_text += f" • SL: ${float(order.sl_price):.2f}"

        # Add timestamp
        footer_text += f" • Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC"

        fig.text(0.99, 0.01, footer_text,
                ha='right', va='bottom',
                color='#787878',
                fontsize=8,
                bbox=dict(facecolor='#2a2a2a',
                         edgecolor='#787878',
                         alpha=0.7,
                         pad=5))
