import mplfinance as mpf
import pandas as pd
import numpy as np  # Add the missing numpy import
from datetime import datetime
from decimal import Decimal
from typing import List, Dict, Optional
import logging
from ..types.models import TimeFrame, Order
import io
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
from matplotlib.ticker import FuncFormatter

logger = logging.getLogger(__name__)

class ChartGenerator:
    def __init__(self):
        self.style = mpf.make_mpf_style(
            base_mpf_style='yahoo',  # Changed to yahoo style for better readability
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

    def validate_candle_data(self, candles: List[Dict]) -> bool:
        """Validate candle data for completeness and correctness"""
        try:
            if not candles or len(candles) < 2:
                logger.error("Not enough candles for chart generation")
                return False

            required_fields = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
            
            for candle in candles:
                # Check all required fields exist
                if not all(field in candle for field in required_fields):
                    logger.error(f"Missing required fields in candle: {candle}")
                    return False
                    
                # Validate price relationships
                if not (float(candle['low']) <= float(candle['high']) and 
                       float(candle['open']) <= float(candle['high']) and 
                       float(candle['close']) <= float(candle['high']) and
                       float(candle['low']) <= float(candle['open']) and
                       float(candle['low']) <= float(candle['close'])):
                    logger.error(f"Invalid price relationships in candle: {candle}")
                    return False
                    
                # Validate numeric values
                if any(not isinstance(candle[field], (int, float)) 
                      for field in ['open', 'high', 'low', 'close', 'volume']):
                    logger.error(f"Non-numeric values in candle: {candle}")
                    return False

            return True
            
        except Exception as e:
            logger.error(f"Error validating candle data: {e}")
            return False

    def validate_reference_price(self, ref_price: float, candles: List[Dict]) -> bool:
        """Validate reference price against candle data"""
        if not candles:
            return False
            
        # Get price range from candles
        all_prices = []
        for candle in candles:
            all_prices.extend([
                float(candle['open']),
                float(candle['high']),
                float(candle['low']),
                float(candle['close'])
            ])
            
        min_price = min(all_prices)
        max_price = max(all_prices)
        price_range = max_price - min_price
        
        # Calculate acceptable range (50% of price range)
        margin = price_range * 0.5
        acceptable_min = min_price - margin
        acceptable_max = max_price + margin
        
        # Check if reference price is within acceptable range
        if not acceptable_min <= ref_price <= acceptable_max:
            logger.warning(
                f"Reference price ${ref_price:.3f} outside acceptable range "
                f"${acceptable_min:.3f} - ${acceptable_max:.3f}"
            )
            return False
            
        return True

    def prepare_candle_data(self, candles: List[Dict]) -> pd.DataFrame:
        """Convert raw candle data to pandas DataFrame"""
        df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df

    async def generate_trade_chart(self, 
                                 candles: List[Dict], 
                                 order: Order,
                                 reference_price: Optional[Decimal] = None) -> Optional[bytes]:
        """Generate candlestick chart with trade markers"""
        try:
            # Validate input data
            if not self.validate_candle_data(candles):
                logger.error("Failed candle data validation")
                return None

            df = self.prepare_candle_data(candles)
            
            # Validate reference price if provided
            ref_value = float(reference_price) if reference_price else None
            if ref_value and not self.validate_reference_price(ref_value, candles):
                logger.warning("Using first candle's open price as reference")
                ref_value = float(df.iloc[0]['open'])

            # Verify opening price
            opening_price = float(df.iloc[0]['open'])
            if abs(opening_price - float(reference_price if reference_price else 0)) > (opening_price * 0.1):
                logger.warning(f"Large discrepancy between reference price and candle open price: "
                             f"Open={opening_price}, Ref={reference_price}")
            
            # Create empty list for addplots
            addplots = []

            # Add entry point marker
            entry_time = order.filled_at or order.created_at
            if entry_time:
                # Create entry marker with NaN values
                entry_series = pd.Series(index=df.index, dtype=float)
                entry_series.loc[:] = float('nan')
                
                # Find closest candle time
                closest_time = min(df.index, key=lambda x: abs(x - entry_time))
                entry_series.loc[closest_time] = float(order.price)
                
                ap_entry = mpf.make_addplot(
                    entry_series,
                    type='scatter',
                    marker='^',
                    markersize=100,
                    color='lime'
                )
                addplots.append(ap_entry)

            # Add reference price line if provided and valid
            if reference_price is not None:
                ref_value = float(reference_price)
                if not pd.isna(ref_value):
                    ref_series = pd.Series([ref_value] * len(df), index=df.index)
                    ap_ref = mpf.make_addplot(
                        ref_series,
                        type='line',
                        color='blue',
                        linestyle='--',
                        width=1
                    )
                    addplots.append(ap_ref)

            # Add opening price line
            open_series = pd.Series([opening_price] * len(df), index=df.index)
            ap_open = mpf.make_addplot(
                open_series,
                type='line',
                color='gray',
                linestyle=':',
                width=1,
                alpha=0.5
            )
            addplots.append(ap_open)

            # Create plot
            buf = io.BytesIO()
            
            # Plot configuration with percentages
            entry_change = ((float(order.price) - opening_price) / opening_price) * 100
            current_change = ((float(df.iloc[-1]['close']) - opening_price) / opening_price) * 100
            
            title = (
                f"{order.symbol} Trade Analysis ({order.timeframe.value})\n"
                f"Open: ${opening_price:.2f} | Entry: ${float(order.price):.2f} ({entry_change:+.2f}%)\n"
                f"Current: ${float(df.iloc[-1]['close']):.2f} ({current_change:+.2f}%)"
            )

            # Generate plot with error handling
            try:
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
            except Exception as plot_error:
                logger.error(f"Plot generation error: {plot_error}")
                return None

            buf.seek(0)
            return buf.getvalue()
            
        except Exception as e:
            logger.error(f"Error generating chart: {e}")
            return None

    async def generate_balance_chart(self, 
                                  balance_data: List[Dict],
                                  btc_prices: List[Dict],
                                  buy_orders: List[Dict]) -> Optional[bytes]:
        """Generate chart showing balance, investments, and BTC price with buy markers"""
        try:
            if not balance_data or len(balance_data) < 2:
                logger.error("Not enough balance data for chart generation")
                return None
                
            # Create DataFrames
            balance_df = pd.DataFrame([
                {
                    'timestamp': entry['timestamp'],
                    'balance': float(entry['balance']),
                    'invested': float(entry['invested']) if entry.get('invested') is not None else 0
                }
                for entry in balance_data
            ])
            balance_df.set_index('timestamp', inplace=True)
            
            # Create BTC price DataFrame
            if btc_prices:
                btc_df = pd.DataFrame([
                    {
                        'timestamp': price['timestamp'],
                        'price': float(price['price'])
                    }
                    for price in btc_prices
                ])
                btc_df.set_index('timestamp', inplace=True)
                # Resample to match balance_df index if needed
                btc_df = btc_df.reindex(balance_df.index, method='ffill')
            else:
                btc_df = pd.DataFrame(index=balance_df.index)
                btc_df['price'] = np.nan
            
            # Create figure with subplots
            fig = plt.figure(figsize=(12, 10))
            gs = GridSpec(3, 1, height_ratios=[2, 1, 1])
            
            # Format dates on x-axis
            date_formatter = plt.matplotlib.dates.DateFormatter('%Y-%m-%d')
            
            # Plot 1: Balance
            ax1 = fig.add_subplot(gs[0])
            balance_df['balance'].plot(ax=ax1, color='green', linewidth=2, legend=True)
            ax1.set_ylabel('USDT Balance')
            ax1.xaxis.set_major_formatter(date_formatter)
            ax1.grid(True, alpha=0.3)
            ax1.set_title('Account Balance History')
            
            # Format y-axis with commas
            ax1.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f'${x:,.2f}'))
            
            # Plot buy markers on balance chart
            for order in buy_orders:
                timestamp = order['timestamp']
                if timestamp in balance_df.index:
                    balance_value = balance_df.loc[timestamp, 'balance']
                    ax1.scatter(timestamp, balance_value, marker='^', s=100, 
                               color='lime', edgecolors='darkgreen', zorder=5)
            
            # Plot 2: Invested amount
            ax2 = fig.add_subplot(gs[1], sharex=ax1)
            balance_df['invested'].plot(ax=ax2, color='blue', linewidth=2, legend=True)
            ax2.set_ylabel('USDT Invested')
            ax2.xaxis.set_major_formatter(date_formatter)
            ax2.grid(True, alpha=0.3)
            
            # Format y-axis with commas
            ax2.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f'${x:,.2f}'))
            
            # Plot 3: BTC price reference
            ax3 = fig.add_subplot(gs[2], sharex=ax1)
            btc_df['price'].plot(ax=ax3, color='orange', linewidth=2)
            ax3.set_ylabel('BTC Price (USDT)')
            ax3.set_xlabel('Date')
            ax3.xaxis.set_major_formatter(date_formatter)
            ax3.grid(True, alpha=0.3)
            
            # Format y-axis with commas
            ax3.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f'${x:,.2f}'))
            
            # Highlight buy orders on BTC chart
            for order in buy_orders:
                if order['symbol'] == 'BTCUSDT':
                    timestamp = order['timestamp']
                    if timestamp in btc_df.index:
                        price = float(order['price'])
                        ax3.scatter(timestamp, price, marker='^', s=100,
                                  color='lime', edgecolors='darkgreen', zorder=5)
            
            # Layout adjustments
            plt.tight_layout()
            fig.subplots_adjust(hspace=0.15)
            
            # Save to buffer
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
            plt.close(fig)
            buf.seek(0)
            
            return buf.getvalue()
            
        except Exception as e:
            logger.error(f"Error generating balance chart: {e}", exc_info=True)
            return None

    def format_info_text(self, order: Order, reference_price: Optional[Decimal] = None) -> str:
        """Format trade information text"""
        try:
            info = [
                f"Trade Details for {order.symbol}:",
                f"Entry Price: ${float(order.price):.2f}"
            ]
            
            # Safe decimal calculations
            if reference_price is not None:
                order_price = Decimal(str(order.price))
                change = ((order_price - reference_price) / reference_price) * Decimal('100')
                info.append(f"Reference Price: ${float(reference_price):.2f} ({float(change):+.2f}%)")
                
            info.extend([
                f"Amount: {float(order.quantity):.8f}",
                f"Total Value: ${float(order.price * order.quantity):.2f}",  # Fixed format string here
                f"Type: {order.order_type.value.upper()}"
            ])
            
            if order.leverage:
                info.append(f"Leverage: {order.leverage}x")
            if order.direction:
                info.append(f"Direction: {order.direction.value.upper()}")
                
            return "\n".join(info)
            
        except Exception as e:
            logger.error(f"Error formatting info text: {e}", exc_info=True)  # Added stack trace
            return "Error generating trade information"
