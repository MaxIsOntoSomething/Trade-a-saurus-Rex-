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
                                  btc_prices: List[Dict],  # Keep param for backward compatibility but don't use it
                                  buy_orders: List[Dict]) -> Optional[bytes]:
        """Generate chart showing balance and investment data on the same chart"""
        try:
            if not balance_data or len(balance_data) < 2:
                logger.error("Not enough balance data for chart generation")
                return None
                
            # Create DataFrame
            balance_df = pd.DataFrame([
                {
                    'timestamp': entry['timestamp'],
                    'balance': float(entry['balance']),
                    'invested': float(entry['invested']) if entry.get('invested') is not None else 0,
                    'fees': float(entry.get('fees', 0))  # Add fees column with default value of 0
                }
                for entry in balance_data
            ])
            balance_df.set_index('timestamp', inplace=True)
            
            # Calculate profit (balance - invested)
            balance_df['profit'] = balance_df['balance'] - balance_df['invested']
            
            # Create figure with two subplots (balance+invested on top, fees on bottom)
            fig = plt.figure(figsize=(12, 8))
            gs = GridSpec(2, 1, height_ratios=[3, 1])
            
            # Format dates on x-axis
            date_formatter = plt.matplotlib.dates.DateFormatter('%Y-%m-%d')
            
            # Plot 1: Balance and Invested on same chart
            ax1 = fig.add_subplot(gs[0])
            balance_df['balance'].plot(ax=ax1, color='green', linewidth=2, label='Total Balance')
            balance_df['invested'].plot(ax=ax1, color='blue', linewidth=2, label='Invested Amount')
            balance_df['profit'].plot(ax=ax1, color='purple', linewidth=1.5, linestyle='--', label='Profit')
            
            ax1.set_ylabel('USDT Value')
            ax1.xaxis.set_major_formatter(date_formatter)
            ax1.grid(True, alpha=0.3)
            ax1.set_title('Account Balance History')
            
            # Format y-axis with commas and dollar sign
            ax1.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f'${x:,.2f}'))
            
            # Add legend with better positioning
            ax1.legend(loc='upper left', frameon=True, framealpha=0.8)
            
            # Plot buy markers on balance chart with clearer appearance
            buy_marker_timestamps = []
            buy_marker_values = []
            buy_marker_sizes = []
            buy_marker_labels = []
            
            for order in buy_orders:
                timestamp = order['timestamp']
                if timestamp in balance_df.index:
                    balance_value = balance_df.loc[timestamp, 'balance']
                    buy_marker_timestamps.append(timestamp)
                    buy_marker_values.append(balance_value)
                    
                    # Scale marker size based on order value (min 100, max 400)
                    order_value = float(order.get('value', 0))
                    marker_size = min(max(100, order_value * 2), 400)
                    buy_marker_sizes.append(marker_size)
                    
                    # Create label with symbol and amount
                    symbol = order.get('symbol', '').replace('USDT', '')
                    amount = float(order.get('quantity', 0))
                    buy_marker_labels.append(f"{symbol}: {amount:.4f}")
            
            # Add markers if we have any
            if buy_marker_timestamps:
                scatter = ax1.scatter(
                    buy_marker_timestamps, 
                    buy_marker_values, 
                    marker='^', 
                    s=buy_marker_sizes,
                    color='lime', 
                    edgecolors='darkgreen', 
                    zorder=5,
                    alpha=0.8
                )
                
                # Add annotation for each buy point
                for i, (x, y, label) in enumerate(zip(buy_marker_timestamps, buy_marker_values, buy_marker_labels)):
                    # Only annotate every other point if many to avoid clutter
                    if len(buy_marker_timestamps) <= 10 or i % 2 == 0:
                        ax1.annotate(
                            label,
                            xy=(x, y),
                            xytext=(0, 10),  # 10 points vertical offset
                            textcoords='offset points',
                            ha='center',
                            va='bottom',
                            fontsize=8,
                            bbox=dict(boxstyle='round,pad=0.3', fc='yellow', alpha=0.7)
                        )
            
            # Plot 2: Fees
            ax2 = fig.add_subplot(gs[1], sharex=ax1)
            if 'fees' in balance_df.columns:
                balance_df['fees'].plot(ax=ax2, color='red', linewidth=2, label='Fees')
                ax2.set_ylabel('USDT Fees')
                ax2.xaxis.set_major_formatter(date_formatter)
                ax2.grid(True, alpha=0.3)
                
                # Format y-axis with commas
                ax2.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f'${x:,.2f}'))
                
                # Add legend
                ax2.legend(loc='upper left')
            else:
                ax2.text(0.5, 0.5, 'No fee data available', 
                      horizontalalignment='center', verticalalignment='center',
                      transform=ax2.transAxes)
            
            ax2.set_xlabel('Date')
            
            # Add vertical grid lines for better date alignment
            ax1.grid(True, which='major', axis='x', linestyle='-', alpha=0.2)
            ax2.grid(True, which='major', axis='x', linestyle='-', alpha=0.2)
            
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

    async def generate_roi_comparison_chart(self, 
                                    portfolio_data: Dict,
                                    btc_performance: Dict,
                                    sp500_performance: Dict = None) -> Optional[bytes]:
        """Generate chart comparing portfolio ROI with BTC and S&P 500"""
        try:
            if not portfolio_data or len(portfolio_data) < 2:
                logger.error("Not enough portfolio data for ROI comparison")
                
                # Generate minimal sample data if needed
                if not portfolio_data or len(portfolio_data) < 2:
                    logger.info("Creating minimal sample data for ROI chart")
                    # Create sample data with two points (to avoid errors but still show in logs)
                    today = datetime.now()
                    yesterday = today - timedelta(days=1)
                    portfolio_data = {
                        yesterday.strftime('%Y-%m-%d'): 0.0,
                        today.strftime('%Y-%m-%d'): 1.0
                    }
            
            # Create DataFrame for portfolio data
            portfolio_df = pd.DataFrame([
                {'date': date, 'roi': value} for date, value in portfolio_data.items()
            ])
            portfolio_df['date'] = pd.to_datetime(portfolio_df['date'])
            portfolio_df.set_index('date', inplace=True)
            
            # Check if BTC data is available, create minimal if needed
            if not btc_performance or len(btc_performance) < 2:
                logger.info("Creating minimal sample BTC data")
                today = datetime.now()
                yesterday = today - timedelta(days=1)
                btc_performance = {
                    yesterday.strftime('%Y-%m-%d'): 0.0,
                    today.strftime('%Y-%m-%d'): 1.5
                }
                
            # Create DataFrame for BTC performance
            btc_df = pd.DataFrame([
                {'date': date, 'roi': value} for date, value in btc_performance.items()
            ])
            btc_df['date'] = pd.to_datetime(btc_df['date'])
            btc_df.set_index('date', inplace=True)
            
            # Create DataFrame for S&P 500 if available
            sp500_df = None
            if sp500_performance and len(sp500_performance) > 0:
                sp500_df = pd.DataFrame([
                    {'date': date, 'roi': value} for date, value in sp500_performance.items()
                ])
                sp500_df['date'] = pd.to_datetime(sp500_df['date'])
                sp500_df.set_index('date', inplace=True)
            else:
                # Create minimal sample S&P data
                logger.info("Creating minimal sample S&P 500 data")
                today = datetime.now()
                yesterday = today - timedelta(days=1)
                sp500_data = {
                    yesterday.strftime('%Y-%m-%d'): 0.0,
                    today.strftime('%Y-%m-%d'): 0.8
                }
                sp500_df = pd.DataFrame([
                    {'date': date, 'roi': value} for date, value in sp500_data.items()
                ])
                sp500_df['date'] = pd.to_datetime(sp500_df['date'])
                sp500_df.set_index('date', inplace=True)
            
            # Align all data on the same dates
            # Get the common date range
            start_date = max(
                portfolio_df.index.min(),
                btc_df.index.min(),
                sp500_df.index.min() if sp500_df is not None else portfolio_df.index.min()
            )
            end_date = min(
                portfolio_df.index.max(),
                btc_df.index.max(),
                sp500_df.index.max() if sp500_df is not None else portfolio_df.index.max()
            )
            
            # Filter all dataframes to common date range
            portfolio_df = portfolio_df.loc[start_date:end_date]
            btc_df = btc_df.loc[start_date:end_date]
            if sp500_df is not None:
                sp500_df = sp500_df.loc[start_date:end_date]
            
            # Create figure for ROI comparison
            fig, ax = plt.subplots(figsize=(12, 8))
            
            # Plot portfolio ROI
            portfolio_df['roi'].plot(ax=ax, color='green', linewidth=2, label='Portfolio')
            
            # Plot BTC ROI
            btc_df['roi'].plot(ax=ax, color='orange', linewidth=2, label='Bitcoin')
            
            # Plot S&P 500 ROI if available
            if sp500_df is not None:
                sp500_df['roi'].plot(ax=ax, color='blue', linewidth=2, label='S&P 500')
            
            # Add zero line for reference
            ax.axhline(y=0, color='gray', linestyle='--', alpha=0.7)
            
            # Format chart
            ax.set_title('Return on Investment (ROI) Comparison')
            ax.set_ylabel('ROI (%)')
            ax.grid(True, alpha=0.3)
            
            # Format y-axis as percentage
            ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f'{x:.1f}%'))
            
            # Add legend
            ax.legend(loc='best')
            
            # Calculate final ROI values for annotation
            final_portfolio_roi = portfolio_df['roi'].iloc[-1]
            final_btc_roi = btc_df['roi'].iloc[-1]
            final_sp500_roi = sp500_df['roi'].iloc[-1] if sp500_df is not None else None
            
            # Add annotations for final values
            text_y_pos = max(final_portfolio_roi, final_btc_roi)
            if final_sp500_roi is not None:
                text_y_pos = max(text_y_pos, final_sp500_roi)
            text_y_pos += 5  # Add some padding
            
            # Add performance summary text
            summary_text = (
                f"Portfolio: {final_portfolio_roi:.2f}%\n"
                f"Bitcoin: {final_btc_roi:.2f}%"
            )
            if final_sp500_roi is not None:
                summary_text += f"\nS&P 500: {final_sp500_roi:.2f}%"
                
            # Add text box with performance summary
            props = dict(boxstyle='round', facecolor='wheat', alpha=0.5)
            ax.text(0.02, 0.98, summary_text, transform=ax.transAxes, fontsize=10,
                  verticalalignment='top', bbox=props)
            
            # Save to buffer
            buf = io.BytesIO()
            plt.tight_layout()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
            plt.close(fig)
            buf.seek(0)
            
            return buf.getvalue()
            
        except Exception as e:
            logger.error(f"Error generating ROI comparison chart: {e}", exc_info=True)
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
                f"Total Value: ${float(order.price * order.quantity)::.2f}",  # Fixed format string here
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
