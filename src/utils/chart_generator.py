import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as ticker
from datetime import datetime
from decimal import Decimal
import io
import logging
import pandas as pd  # Add missing pandas import
from typing import List, Dict, Optional
from ..types.models import Order, OrderType, TradeDirection, TimeFrame  # Added TimeFrame import

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

    def validate_candle_data(self, candles: List[Dict]) -> bool:
        """Validate candle data for completeness and correctness"""
        try:
            if not candles or len(candles) < 2:
                logger.warning("Not enough candles for chart generation (minimum 2 required)")
                return False

            required_fields = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
            
            for candle in candles:
                if not all(field in candle for field in required_fields):
                    logger.error(f"Missing required fields in candle: {candle}")
                    return False
                    
                if not (float(candle['low']) <= float(candle['high']) and 
                       float(candle['open']) <= float(candle['high']) and 
                       float(candle['close']) <= float(candle['high']) and
                       float(candle['low']) <= float(candle['open']) and
                       float(candle['low']) <= float(candle['close'])):
                    logger.error(f"Invalid price relationships in candle: {candle}")
                    return False

            return True
            
        except Exception as e:
            logger.error(f"Error validating candle data: {e}")
            return False

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

    async def generate_trade_chart(self, candles: List[Dict], order: Order, 
                                 ref_price: Optional[Decimal] = None) -> Optional[bytes]:
        """Generate clean chart with thicker candles and proper date formatting"""
        try:
            # Validate input data
            if not self.validate_candle_data(candles):
                logger.error("Invalid candle data")
                return None

            # Limit to last 8 candles for cleaner look
            candles = candles[-8:]

            # Create figure with black background
            fig, ax = plt.subplots(figsize=(12, 6), facecolor='black')
            ax.set_facecolor('black')

            # Calculate candle width based on data
            times = [datetime.fromtimestamp(c['timestamp'] / 1000) for c in candles]
            if len(times) > 1:
                time_diff = (times[-1] - times[0]).total_seconds()
                width = (time_diff / len(candles)) * 0.6  # 60% of average time delta
            else:
                width = 43200  # 12 hours in seconds

            # Plot candlesticks with thicker style
            for candle in candles:
                t = datetime.fromtimestamp(candle['timestamp'] / 1000)
                o = float(candle['open'])
                h = float(candle['high'])
                l = float(candle['low'])
                c = float(candle['close'])

                color = self.colors['up'] if c >= o else self.colors['down']
                
                # Plot thicker wicks with shadow effect
                ax.vlines(t, l, h, color=color, linewidth=2, zorder=1)
                
                # Plot thicker body with 3D effect
                body_height = c - o if c >= o else o - c
                body_bottom = min(o, c)
                ax.bar(t, body_height, bottom=body_bottom, 
                      width=width/86400, color=color,  # Divide by seconds in day
                      alpha=1.0, zorder=2)

            # Add entry price line
            entry_price = float(order.price)
            ax.axhline(y=entry_price, color=self.colors['entry'], 
                      linestyle='--', linewidth=1.5,
                      label=f'Entry ${entry_price:,.2f}')

            # Add liquidation price for futures
            if order.order_type == OrderType.FUTURES:
                liq_price = self.calculate_liquidation_price(order)
                if liq_price:
                    liq_color = (self.colors['liq_long'] if order.direction == TradeDirection.LONG 
                               else self.colors['liq_short'])
                    ax.axhline(y=liq_price, color=liq_color, 
                             linestyle=':', linewidth=1.5,
                             label=f'Liq ${liq_price:,.2f}')

            # Format axes
            ax.grid(True, alpha=0.1, linestyle='--')
            ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, p: f'${x:,.2f}'))
            
            # Set date format based on timeframe
            date_format = self.timeframe_formats.get(order.timeframe, '%Y-%m-%d %H:%M')
            ax.xaxis.set_major_formatter(mdates.DateFormatter(date_format))
            plt.xticks(rotation=45)

            # Add footer with timeframe info
            footer_text = (
                f"{order.symbol} • {order.timeframe.value.title()} Chart • "
                f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC"
            )
            plt.figtext(0.99, 0.01, footer_text, 
                       ha='right', va='bottom', 
                       color='gray', alpha=0.7, 
                       fontsize=8)

            # Add title
            title = f"{order.symbol}"
            if order.order_type == OrderType.FUTURES:
                title += f" {order.direction.value.upper()} {order.leverage}x"
            plt.title(title, pad=10)

            # Add legend with better positioning
            ax.legend(loc='upper left', bbox_to_anchor=(0.02, 0.98))

            # Adjust layout
            plt.tight_layout()

            # Save with high quality
            buffer = io.BytesIO()
            plt.savefig(buffer, format='png', dpi=150, bbox_inches='tight')
            buffer.seek(0)
            plt.close()

            return buffer.getvalue()

        except Exception as e:
            logger.error(f"Error generating chart: {e}", exc_info=True)
            plt.close()
            return None

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
