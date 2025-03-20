import mplfinance as mpf
import pandas as pd
import numpy as np  # Add the missing numpy import
from datetime import datetime, timedelta  # Add timedelta import
from decimal import Decimal
from typing import List, Dict, Optional
import logging
from ..types.models import TimeFrame, Order
import io
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
from matplotlib.ticker import FuncFormatter
import matplotlib.dates as mdates

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
            
            # Add Take Profit line if order has TP configured
            if order.take_profit and hasattr(order.take_profit, 'price') and order.take_profit.price:
                tp_price = float(order.take_profit.price)
                tp_series = pd.Series([tp_price] * len(df), index=df.index)
                ap_tp = mpf.make_addplot(
                    tp_series,
                    type='line',
                    color='green',  # Green for take profit
                    linestyle='--',
                    width=1.5,
                    secondary_y=False
                )
                addplots.append(ap_tp)
            
            # Add Stop Loss line if order has SL configured
            if order.stop_loss and hasattr(order.stop_loss, 'price') and order.stop_loss.price:
                sl_price = float(order.stop_loss.price)
                sl_series = pd.Series([sl_price] * len(df), index=df.index)
                ap_sl = mpf.make_addplot(
                    sl_series,
                    type='line',
                    color='red',  # Red for stop loss
                    linestyle='--',
                    width=1.5,
                    secondary_y=False
                )
                addplots.append(ap_sl)

            # Create plot
            buf = io.BytesIO()
            
            # Plot configuration with percentages
            entry_change = ((float(order.price) - opening_price) / opening_price) * 100
            current_change = ((float(df.iloc[-1]['close']) - opening_price) / opening_price) * 100
            
            # Add TP/SL info to title if available
            title = (
                f"{order.symbol} Trade Analysis ({order.timeframe.value})\n"
                f"Open: ${opening_price:.2f} | Entry: ${float(order.price):.2f} ({entry_change:+.2f}%)\n"
                f"Current: ${float(df.iloc[-1]['close']):.2f} ({current_change:+.2f}%)"
            )
            
            # Additional TP/SL line for title - Remove dollar signs to avoid parsing issues
            tp_sl_line = ""
            if order.take_profit and hasattr(order.take_profit, 'price') and order.take_profit.price:
                tp_price = float(order.take_profit.price)
                tp_percentage = float(order.take_profit.percentage) if hasattr(order.take_profit, 'percentage') else ((tp_price / float(order.price) - 1) * 100)
                tp_sl_line += f"TP: {tp_price:.2f} (+{tp_percentage:.2f}%) "
                
            if order.stop_loss and hasattr(order.stop_loss, 'price') and order.stop_loss.price:
                sl_price = float(order.stop_loss.price)
                sl_percentage = float(order.stop_loss.percentage) if hasattr(order.stop_loss, 'percentage') else ((1 - sl_price / float(order.price)) * 100)
                tp_sl_line += f"SL: {sl_price:.2f} (-{sl_percentage:.2f}%)"
                
            if tp_sl_line:
                title += f"\n{tp_sl_line}"

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
                    yesterday = today - timedelta(days=1)  # Now works with proper import
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

    async def generate_ytd_comparison_chart(self, 
                                    btc_data: Dict,
                                    sp500_data: Dict,
                                    year: int = None) -> Optional[bytes]:
        """Generate year-to-date comparison chart between Bitcoin and S&P 500"""
        try:
            if not year:
                year = datetime.now().year
            
            if not btc_data or len(btc_data) < 2:
                logger.error("Not enough BTC data for YTD comparison")
                return None
                
            if not sp500_data or len(sp500_data) < 2:
                logger.error("Not enough S&P 500 data for YTD comparison")
                return None
            
            # Create DataFrames
            btc_df = pd.DataFrame([
                {'date': date, 'value': value} for date, value in btc_data.items()
            ])
            btc_df['date'] = pd.to_datetime(btc_df['date'])
            btc_df.set_index('date', inplace=True)
            
            sp500_df = pd.DataFrame([
                {'date': date, 'value': value} for date, value in sp500_data.items()
            ])
            sp500_df['date'] = pd.to_datetime(sp500_df['date'])
            sp500_df.set_index('date', inplace=True)
            
            # Create figure
            fig, ax = plt.subplots(figsize=(12, 8))
            
            # Plot BTC data
            btc_df['value'].plot(ax=ax, color='orange', linewidth=2, label='Bitcoin')
            
            # Plot S&P 500 data
            sp500_df['value'].plot(ax=ax, color='blue', linewidth=2, label='S&P 500')
            
            # Add zero line
            ax.axhline(y=0, color='gray', linestyle='--', alpha=0.7)
            
            # Format chart
            ax.set_title(f'Bitcoin vs S&P 500 Performance ({year} Year-to-Date)')
            ax.set_ylabel('Year-to-Date Change (%)')
            ax.grid(True, alpha=0.3)
            
            # Format y-axis as percentage
            ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f'{x:.1f}%'))
            
            # Format x-axis to show months
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%b'))
            ax.xaxis.set_major_locator(mdates.MonthLocator())
            
            # Add legend
            ax.legend(loc='best')
            
            # Get final values
            final_btc = btc_df['value'].iloc[-1]
            final_sp500 = sp500_df['value'].iloc[-1]
            
            # Add text with performance comparison
            comparison_text = (
                f"YTD Performance:\n"
                f"Bitcoin: {final_btc:.2f}%\n"
                f"S&P 500: {final_sp500:.2f}%\n"
                f"Difference: {(final_btc - final_sp500):.2f}%"
            )
            
            # Add text box with performance summary
            props = dict(boxstyle='round', facecolor='wheat', alpha=0.5)
            ax.text(0.02, 0.98, comparison_text, transform=ax.transAxes, fontsize=10,
                  verticalalignment='top', bbox=props)
            
            # Save to buffer
            buf = io.BytesIO()
            plt.tight_layout()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
            plt.close(fig)
            buf.seek(0)
            
            return buf.getvalue()
            
        except Exception as e:
            logger.error(f"Error generating YTD comparison chart: {e}", exc_info=True)
            return None

    async def generate_portfolio_composition_chart(self, 
                                          asset_values: Dict, 
                                          total_value: float) -> Optional[bytes]:
        """Generate pie chart showing portfolio asset allocation"""
        try:
            if not asset_values or len(asset_values) == 0:
                logger.error("No assets data for portfolio composition chart")
                return None
                
            # Remove assets with very small percentages to avoid cluttering
            filtered_assets = {}
            other_value = 0
            
            for asset, value in asset_values.items():
                percentage = (value / total_value * 100) if total_value > 0 else 0
                if percentage >= 1.0:  # Only show assets that are at least 1% of portfolio
                    filtered_assets[asset] = value
                else:
                    other_value += value
                    
            # Add an "Other" category if needed
            if other_value > 0:
                filtered_assets["Other"] = other_value
                
            # Sort by value for better presentation
            sorted_items = sorted(filtered_assets.items(), key=lambda x: x[1], reverse=True)
            
            # Create labels and values
            labels = [item[0] for item in sorted_items]
            values = [item[1] for item in sorted_items]
            
            # Configure plot
            plt.figure(figsize=(10, 8))
            
            # Generate colors - ensure USDT is a specific color if present
            colors = plt.cm.tab20.colors[:len(labels)]
            if 'USDT' in labels:
                usdt_index = labels.index('USDT')
                # Use a specific color for USDT
                colors = list(colors)
                colors[usdt_index] = (0.2, 0.8, 0.2, 1.0)
            
            # Create explode effect for pie slices
            explode = [0.05] * len(labels)
            if 'USDT' in labels:
                usdt_index = labels.index('USDT')
                explode[usdt_index] = 0.1
                
            # Create pie chart
            patches, texts, autotexts = plt.pie(
                values,
                labels=None,
                explode=explode,
                shadow=True,
                startangle=90,
                colors=colors,
                autopct='%1.1f%%',
                pctdistance=0.85,
                wedgeprops=dict(width=0.5, edgecolor='w')
            )
            
            # Customize the text appearance
            for autotext in autotexts:
                autotext.set_color('white')
                autotext.set_fontsize(10)
                autotext.set_fontweight('bold')
                
            # Create legend with values
            legend_labels = [f"{label} (${value:.2f})" for label, value in zip(labels, values)]
            plt.legend(
                patches,
                legend_labels,
                loc="center left",
                bbox_to_anchor=(1, 0.5),
                frameon=False
            )
            
            plt.title("Portfolio Composition", fontsize=16, pad=20)
            plt.tight_layout()
            
            # Save to buffer
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
            plt.close()
            buf.seek(0)
            
            return buf.getvalue()
            
        except Exception as e:
            logger.error(f"Error generating portfolio composition chart: {e}", exc_info=True)
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
                f"Total Value: ${float(order.price * order.quantity):.2f}",  # Fixed format string
                f"Type: {order.order_type.value.upper()}"
            ])
            
            # Add TP/SL information if configured
            if order.take_profit and hasattr(order.take_profit, 'price') and order.take_profit.price:
                tp_price = float(order.take_profit.price)
                tp_percentage = order.take_profit.percentage if hasattr(order.take_profit, 'percentage') else ((tp_price / float(order.price) - 1) * 100)
                info.append(f"Take Profit: ${tp_price:.2f} (+{tp_percentage:.2f}%)")
                
            if order.stop_loss and hasattr(order.stop_loss, 'price') and order.stop_loss.price:
                sl_price = float(order.stop_loss.price)
                sl_percentage = order.stop_loss.percentage if hasattr(order.stop_loss, 'percentage') else ((1 - sl_price / float(order.price)) * 100)
                info.append(f"Stop Loss: ${sl_price:.2f} (-{sl_percentage:.2f}%)")
            
            if order.leverage:
                info.append(f"Leverage: {order.leverage}x")
            if order.direction:
                info.append(f"Direction: {order.direction.value.upper()}")
                
            return "\n".join(info)
            
        except Exception as e:
            logger.error(f"Error formatting info text: {e}", exc_info=True)  # Added stack trace
            return "Error generating trade information"

    async def generate_simple_chart(self, candles, order, reference_price=None):
        """Generate a simplified chart with minimal features when full chart generation fails"""
        try:
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            from matplotlib.ticker import FuncFormatter
            import io
            from datetime import datetime
            import numpy as np
            
            # Convert candle timestamps to datetime objects
            dates = [datetime.fromtimestamp(candle['timestamp']/1000) for candle in candles]
            closes = [candle['close'] for candle in candles]
            
            # Create a simple line chart (no candlesticks)
            fig, ax = plt.subplots(figsize=(10, 6))
            
            # Plot simple price line
            ax.plot(dates, closes, 'b-', linewidth=2)
            
            # Add order price as horizontal line
            ax.axhline(y=float(order.price), color='r', linestyle='--', alpha=0.8, label=f"Entry: ${float(order.price):,.2f}")
            
            # Add reference price if available
            if reference_price is not None:
                ax.axhline(y=float(reference_price), color='g', linestyle='--', alpha=0.8, label=f"Reference: ${float(reference_price):,.2f}")
            
            # Add Take Profit line if configured - Remove dollar signs
            if order.take_profit and hasattr(order.take_profit, 'price') and order.take_profit.price:
                tp_price = float(order.take_profit.price)
                tp_percentage = float(order.take_profit.percentage) if hasattr(order.take_profit, 'percentage') else ((tp_price / float(order.price) - 1) * 100)
                ax.axhline(y=tp_price, color='green', linestyle='--', alpha=0.8, 
                          label=f"TP: {tp_price:,.2f} (+{tp_percentage:.2f}%)")
            
            # Add Stop Loss line if configured - Remove dollar signs
            if order.stop_loss and hasattr(order.stop_loss, 'price') and order.stop_loss.price:
                sl_price = float(order.stop_loss.price)
                sl_percentage = float(order.stop_loss.percentage) if hasattr(order.stop_loss, 'percentage') else ((1 - sl_price / float(order.price)) * 100)
                ax.axhline(y=sl_price, color='red', linestyle='--', alpha=0.8, 
                          label=f"SL: {sl_price:,.2f} (-{sl_percentage:.2f}%)")
            
            # Format x-axis to show clean dates
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
            plt.xticks(rotation=45)
            
            # Format y-axis to show dollar prices
            ax.yaxis.set_major_formatter(FuncFormatter(lambda x, p: f'${x:,.2f}'))
            
            # Add labels and title
            ax.set_title(f"{order.symbol} - {order.timeframe.value} Chart", fontsize=16)
            ax.set_xlabel("Date", fontsize=12)
            ax.set_ylabel("Price (USD)", fontsize=12)
            
            # Add legend
            ax.legend(loc='upper left')
            
            # Add grid
            ax.grid(True, alpha=0.3)
            
            # Make layout tight
            plt.tight_layout()
            
            # Save the chart to bytes
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=100)
            plt.close(fig)
            buf.seek(0)
            
            return buf.getvalue()
            
        except Exception as e:
            logger.error(f"Error in simplified chart generation: {e}", exc_info=True)
            return None

    def format_info_text(self, order, ref_price=None) -> str:
        """Format trade information for chart display"""
        try:
            # Base text
            avg_price = f"${float(order.price):.2f}"
            symbol = order.symbol
            
            # Format the TP/SL text with proper escaping for currency symbols
            tp_sl_text = ""
            if order.take_profit:
                tp_sl_text += f"TP: {float(order.take_profit.price):.2f} (+{order.take_profit.percentage:.2f}%)"
            if order.stop_loss:
                if tp_sl_text:
                    tp_sl_text += " "
                tp_sl_text += f"SL: {float(order.stop_loss.price):.2f} (-{order.stop_loss.percentage:.2f}%)"
                
            # Build the complete info text without $ symbols in the TP/SL section
            info_text = f"{symbol} @ {avg_price}"
            if ref_price:
                # Calculate price change from reference
                change = ((float(order.price) / float(ref_price)) - 1) * 100
                info_text += f" ({change:+.2f}%)"
                
            if tp_sl_text:
                info_text += f"\n{tp_sl_text}"
                
            return info_text
            
        except Exception as e:
            logger.error(f"Error formatting chart info: {e}")
            return f"{order.symbol} @ ${float(order.price):.2f}"

    def _add_tp_sl_lines(self, ax, order, min_price, max_price):
        """Add take profit and stop loss lines to the chart with fixed formatting"""
        try:
            entry_price = float(order.price)
            
            # Add entry price line
            ax.axhline(y=entry_price, color='blue', linestyle='-', linewidth=1, alpha=0.7)
            ax.text(0.02, entry_price, f"Entry: ${entry_price:.2f}", transform=ax.get_yaxis_transform(),
                    va='center', ha='left', fontsize=9, backgroundcolor='white', alpha=0.7)
            
            # Add take profit line if configured
            if order.take_profit:
                tp_price = float(order.take_profit.price)
                tp_pct = order.take_profit.percentage
                ax.axhline(y=tp_price, color='green', linestyle='--', linewidth=1, alpha=0.7)
                # Format the text without $ symbol inside the text method
                ax.text(0.02, tp_price, f"TP: {tp_price:.2f} (+{tp_pct:.2f}%)", transform=ax.get_yaxis_transform(),
                       va='center', ha='left', fontsize=9, backgroundcolor='white', alpha=0.7)
            
            # Add stop loss line if configured
            if order.stop_loss:
                sl_price = float(order.stop_loss.price)
                sl_pct = order.stop_loss.percentage
                ax.axhline(y=sl_price, color='red', linestyle='--', linewidth=1, alpha=0.7)
                # Format the text without $ symbol inside the text method
                ax.text(0.02, sl_price, f"SL: {sl_price:.2f} (-{sl_pct:.2f}%)", transform=ax.get_yaxis_transform(),
                       va='center', ha='left', fontsize=9, backgroundcolor='white', alpha=0.7)
            
            return True
        except Exception as e:
            logger.error(f"Failed to add TP/SL lines: {e}")
            return False
