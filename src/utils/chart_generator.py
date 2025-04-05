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
                                  btc_prices: List[Dict],  # Keep param for backward compatibility
                                  buy_orders: List[Dict]) -> Optional[bytes]:
        """Generate chart showing balance and investment data with improved visualization"""
        try:
            if not balance_data or len(balance_data) < 2:
                logger.error("Not enough balance data for chart generation")
                return None
                
            # Create DataFrame with balance data
            balance_df = pd.DataFrame([
                {
                    'timestamp': entry['timestamp'],
                    'balance': float(entry['balance']),
                    'invested': float(entry['invested']) if entry.get('invested') is not None else 0,
                    'net_deposits': float(entry.get('net_deposits', 0))
                }
                for entry in balance_data
            ])
            balance_df.set_index('timestamp', inplace=True)
            
            # Calculate net worth (total balance)
            balance_df['net_worth'] = balance_df['balance'] + balance_df['invested']
            
            # Calculate portfolio performance excluding deposits/withdrawals effect
            # First, get cumulative net deposits at each point
            balance_df['cumulative_deposits'] = balance_df['net_deposits'].cumsum()
            
            # Create an adjusted net worth that removes deposit/withdrawal effects
            first_net_worth = balance_df['net_worth'].iloc[0] - balance_df['cumulative_deposits'].iloc[0]
            balance_df['adjusted_net_worth'] = balance_df['net_worth'] - balance_df['cumulative_deposits']
            
            # Calculate percentage change from initial adjusted worth for true performance
            if first_net_worth > 0:
                balance_df['true_performance'] = (balance_df['adjusted_net_worth'] / first_net_worth - 1) * 100
            else:
                balance_df['true_performance'] = 0
            
            # Create figure with subplots: balance chart and performance chart
            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 10), gridspec_kw={'height_ratios': [3, 1]})
            
            # Format dates on x-axis
            date_formatter = mdates.DateFormatter('%Y-%m-%d')
            
            # Plot lines on main chart (ax1)
            balance_df['invested'].plot(ax=ax1, color='blue', linewidth=2, 
                                      label='Invested Amount', alpha=0.7)
            balance_df['balance'].plot(ax=ax1, color='green', linewidth=2, 
                                     label='Available Balance', alpha=0.7)
            balance_df['net_worth'].plot(ax=ax1, color='purple', linewidth=2.5, 
                                       label='Net Worth', linestyle='-')
            
            # Plot adjusted net worth as a dashed line
            balance_df['adjusted_net_worth'].plot(ax=ax1, color='red', linewidth=2, 
                                               label='Adj. Net Worth (excl. deposits)', 
                                               linestyle='--', alpha=0.7)
            
            # Add deposit markers
            if 'net_deposits' in balance_df.columns and any(balance_df['net_deposits'] != 0):
                # Find points with deposits or withdrawals
                deposit_points = balance_df[balance_df['net_deposits'] > 0]
                withdrawal_points = balance_df[balance_df['net_deposits'] < 0]
                
                # Plot deposit markers
                if not deposit_points.empty:
                    ax1.scatter(deposit_points.index, deposit_points['net_worth'], 
                               marker='^', color='green', s=80, label='Deposits',
                               zorder=5, alpha=0.8)
                    
                    # Add deposit annotations
                    for idx, row in deposit_points.iterrows():
                        ax1.annotate(f"+${row['net_deposits']:.2f}", 
                                    (idx, row['net_worth']),
                                    xytext=(0, 15), textcoords='offset points',
                                    ha='center', fontsize=8,
                                    bbox=dict(boxstyle='round,pad=0.3', fc='green', alpha=0.3))
                
                # Plot withdrawal markers
                if not withdrawal_points.empty:
                    ax1.scatter(withdrawal_points.index, withdrawal_points['net_worth'], 
                               marker='v', color='red', s=80, label='Withdrawals',
                               zorder=5, alpha=0.8)
                    
                    # Add withdrawal annotations
                    for idx, row in withdrawal_points.iterrows():
                        ax1.annotate(f"${row['net_deposits']:.2f}", 
                                    (idx, row['net_worth']),
                                    xytext=(0, -15), textcoords='offset points',
                                    ha='center', fontsize=8,
                                    bbox=dict(boxstyle='round,pad=0.3', fc='red', alpha=0.3))
            
            # Initialize buy order variables
            buy_timestamps = []
            buy_values = []
            annotations = []
            
            # Add buy markers if we have orders
            if buy_orders:
                for order in buy_orders:
                    timestamp = order['timestamp']
                    if timestamp in balance_df.index:
                        buy_timestamps.append(timestamp)
                        # Plot markers at net worth level
                        buy_values.append(balance_df.loc[timestamp, 'net_worth'])
                        
                        # Create annotation with symbol and amount
                        symbol = order.get('symbol', '').replace('USDT', '')
                        amount = float(order.get('quantity', 0))
                        annotations.append(f"{symbol}: {amount:.4f}")
            
            # Add small green dots for buy points if we have any
            if buy_timestamps:
                ax1.scatter(buy_timestamps, buy_values, color='lime', 
                          s=50, zorder=5, alpha=0.6, marker='o',
                          label='Buy Orders')
                
                # Add subtle annotations
                for i, (x, y, label) in enumerate(zip(buy_timestamps, buy_values, annotations)):
                    if i % 2 == 0:  # Annotate every other point to reduce clutter
                        ax1.annotate(label, (x, y), xytext=(5, 5),
                                  textcoords='offset points', fontsize=8,
                                  bbox=dict(facecolor='white', edgecolor='none', alpha=0.7),
                                  ha='left', va='bottom')
            
            # Customize first plot
            ax1.set_title('Portfolio Balance History', fontsize=14, pad=10)
            ax1.set_ylabel('Value ($)', fontsize=12)
            ax1.grid(True, alpha=0.3)
            ax1.legend(loc='upper left', frameon=True, framealpha=0.8)
            ax1.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f'${x:,.2f}'))
            
            # Format x-axis dates for first plot
            ax1.xaxis.set_major_formatter(date_formatter)
            plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45, ha='right')
            
            # Plot performance percentage on second chart (ax2)
            if 'true_performance' in balance_df.columns:
                balance_df['true_performance'].plot(ax=ax2, color='blue', linewidth=2, 
                                                 label='Portfolio Performance (%)')
                
                # Add zero line for reference
                ax2.axhline(y=0, color='black', linestyle='-', alpha=0.2)
                
                # Shade positive area green and negative area red
                ax2.fill_between(balance_df.index, balance_df['true_performance'], 0, 
                                where=balance_df['true_performance'] >= 0, 
                                facecolor='green', alpha=0.3)
                ax2.fill_between(balance_df.index, balance_df['true_performance'], 0, 
                                where=balance_df['true_performance'] < 0, 
                                facecolor='red', alpha=0.3)
                
                # Customize second plot
                ax2.set_ylabel('Performance (%)', fontsize=12)
                ax2.set_xlabel('Date', fontsize=12)
                ax2.grid(True, alpha=0.3)
                ax2.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f'{x:.1f}%'))
                
                # Format x-axis dates for second plot
                ax2.xaxis.set_major_formatter(date_formatter)
                plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45, ha='right')
            
            # Add summary box
            latest = balance_df.iloc[-1]
            
            # Calculate total deposits and withdrawals
            total_deposits = balance_df[balance_df['net_deposits'] > 0]['net_deposits'].sum()
            total_withdrawals = abs(balance_df[balance_df['net_deposits'] < 0]['net_deposits'].sum())
            net_deposits_total = balance_df['cumulative_deposits'].iloc[-1]
            
            # Create summary text with deposit information
            summary_text = (
                f"Current Status:\n"
                f"Net Worth: ${latest['net_worth']:,.2f}\n"
                f"Available: ${latest['balance']:,.2f}\n"
                f"Invested: ${latest['invested']:,.2f}\n"
                f"Total Deposits: ${total_deposits:,.2f}\n"
                f"Total Withdrawals: ${total_withdrawals:,.2f}\n"
                f"Net Deposits: ${net_deposits_total:,.2f}\n"
                f"Actual Return: {latest['true_performance']:.2f}%"
            )
            
            # Add text box with summary
            props = dict(boxstyle='round', facecolor='white', alpha=0.8)
            ax1.text(0.02, 0.98, summary_text, transform=ax1.transAxes,
                    fontsize=9, verticalalignment='top', bbox=props)
            
            # Adjust layout
            plt.tight_layout()
            
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
            # Create a common date range for all data series
            all_dates = set()
            all_dates.update(portfolio_data.keys())
            all_dates.update(btc_performance.keys())
            if sp500_performance:
                all_dates.update(sp500_performance.keys())
            
            date_range = sorted(list(all_dates))
            
            # Create DataFrames with the common date range
            df = pd.DataFrame(index=date_range)
            df.index = pd.to_datetime(df.index)
            
            # Fill in the data
            df['Portfolio'] = pd.Series(portfolio_data)
            df['Bitcoin'] = pd.Series(btc_performance)
            if sp500_performance:
                df['S&P 500'] = pd.Series(sp500_performance)
            
            # Forward fill missing values
            df = df.fillna(method='ffill')
            # Backward fill any remaining NaN at the start
            df = df.fillna(method='bfill')
            
            # Create figure
            fig, ax = plt.subplots(figsize=(12, 8))
            
            # Plot all series
            df['Portfolio'].plot(ax=ax, color='green', linewidth=2, label='Portfolio')
            df['Bitcoin'].plot(ax=ax, color='orange', linewidth=2, label='Bitcoin')
            if 'S&P 500' in df.columns:
                df['S&P 500'].plot(ax=ax, color='blue', linewidth=2, label='S&P 500')
            
            # Add zero line
            ax.axhline(y=0, color='gray', linestyle='--', alpha=0.7)
            
            # Format chart
            ax.set_title('Return on Investment (ROI) Comparison')
            ax.set_ylabel('ROI (%)')
            ax.grid(True, alpha=0.3)
            
            # Format y-axis as percentage
            ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f'{x:.1f}%'))
            
            # Format x-axis to show dates nicely
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
            plt.xticks(rotation=45)
            
            # Add legend
            ax.legend(loc='best')
            
            # Calculate final values for annotation
            final_values = df.iloc[-1]
            
            # Add performance summary text
            summary_text = (
                f"Final ROI:\n"
                f"Portfolio: {final_values['Portfolio']:.2f}%\n"
                f"Bitcoin: {final_values['Bitcoin']:.2f}%"
            )
            if 'S&P 500' in final_values:
                summary_text += f"\nS&P 500: {final_values['S&P 500']:.2f}%"
            
            # Add text box with performance summary
            props = dict(boxstyle='round', facecolor='wheat', alpha=0.5)
            ax.text(0.02, 0.98, summary_text, transform=ax.transAxes, fontsize=10,
                   verticalalignment='top', bbox=props)
            
            # Adjust layout
            plt.tight_layout()
            
            # Save to buffer
            buf = io.BytesIO()
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
            
            # Add partial take profit information if configured
            if hasattr(order, 'partial_take_profits') and order.partial_take_profits and len(order.partial_take_profits) > 0:
                info.append("\nPartial Take Profits:")
                
                for ptp in order.partial_take_profits:
                    if hasattr(ptp, 'price') and ptp.price:
                        ptp_price = float(ptp.price)
                        profit_pct = ptp.profit_percentage if hasattr(ptp, 'profit_percentage') else ((ptp_price / float(order.price) - 1) * 100)
                        position_pct = ptp.position_percentage if hasattr(ptp, 'position_percentage') else 0
                        
                        # Calculate exact quantity to be sold at this level
                        quantity_sold = float(order.quantity) * (position_pct / 100)
                        value_sold = quantity_sold * ptp_price
                        
                        info.append(f"• Level {ptp.level}: ${ptp_price:.4f} (+{profit_pct:.2f}%)")
                        info.append(f"  Sell {position_pct}% of position ({quantity_sold:.8f} = ${value_sold:.2f})")
                
            # Add stop loss information if configured
            if order.stop_loss and hasattr(order.stop_loss, 'price') and order.stop_loss.price:
                sl_price = float(order.stop_loss.price)
                sl_percentage = order.stop_loss.percentage if hasattr(order.stop_loss, 'percentage') else ((1 - sl_price / float(order.price)) * 100)
                info.append(f"Stop Loss: ${sl_price:.2f} (-{sl_percentage:.2f}%)")
            
            # Add trailing stop loss information if configured
            if hasattr(order, 'trailing_stop_loss') and order.trailing_stop_loss:
                tsl = order.trailing_stop_loss
                if hasattr(tsl, 'activation_price') and tsl.activation_price:
                    info.append("\nTrailing Stop Loss:")
                    info.append(f"• Activation: +{tsl.activation_percentage:.2f}% (${float(tsl.activation_price):.4f})")
                    info.append(f"• Callback Rate: {tsl.callback_rate:.2f}%")
                    info.append(f"• Current Stop: ${float(tsl.current_stop_price):.4f}")
                    
                    if hasattr(tsl, 'activated_at') and tsl.activated_at:
                        info.append(f"• Activated: {tsl.activated_at.strftime('%Y-%m-%d %H:%M:%S')}")
            
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
            ax.axhline(y=float(order.price), color='b', linestyle='-', alpha=0.8, label=f"Entry: ${float(order.price):,.2f}")
            
            # Add reference price if available
            if reference_price is not None:
                ax.axhline(y=float(reference_price), color='g', linestyle='--', alpha=0.8, label=f"Reference: ${float(reference_price):,.2f}")
            
            # Add Take Profit line if configured - Remove dollar signs
            if order.take_profit and hasattr(order.take_profit, 'price') and order.take_profit.price:
                tp_price = float(order.take_profit.price)
                tp_percentage = float(order.take_profit.percentage) if hasattr(order.take_profit, 'percentage') else ((tp_price / float(order.price) - 1) * 100)
                ax.axhline(y=tp_price, color='green', linestyle='--', alpha=0.8, 
                          label=f"TP: {tp_price:,.2f} (+{tp_percentage:.2f}%)")
            
            # Add Partial Take Profit lines if configured
            if hasattr(order, 'partial_take_profits') and order.partial_take_profits:
                # Define colors for partial TPs
                colors = ['#00FF00', '#33CC33', '#009900', '#006600']  # Green gradient
                
                for i, ptp in enumerate(order.partial_take_profits):
                    if hasattr(ptp, 'price') and ptp.price:
                        ptp_price = float(ptp.price)
                        ptp_pct = ptp.profit_percentage
                        position_pct = ptp.position_percentage
                        
                        # Use different color for each level
                        color_idx = min(i, len(colors) - 1)
                        
                        ax.axhline(y=ptp_price, color=colors[color_idx], linestyle='--', alpha=0.8,
                                  label=f"PTP {ptp.level}: {ptp_price:,.2f} (+{ptp_pct:.2f}%, {position_pct}%)")
            
            # Add Stop Loss line if configured - Remove dollar signs
            if order.stop_loss and hasattr(order.stop_loss, 'price') and order.stop_loss.price:
                sl_price = float(order.stop_loss.price)
                sl_percentage = float(order.stop_loss.percentage) if hasattr(order.stop_loss, 'percentage') else ((1 - sl_price / float(order.price)) * 100)
                ax.axhline(y=sl_price, color='red', linestyle='--', alpha=0.8, 
                          label=f"SL: {sl_price:,.2f} (-{sl_percentage:.2f}%)")
            
            # Add Trailing Stop Loss lines if configured
            if hasattr(order, 'trailing_stop_loss') and order.trailing_stop_loss:
                tsl = order.trailing_stop_loss
                if hasattr(tsl, 'activation_price') and tsl.activation_price:
                    # Activation line
                    tsl_activation = float(tsl.activation_price)
                    ax.axhline(y=tsl_activation, color='orange', linestyle=':', alpha=0.8,
                              label=f"TSL Act: {tsl_activation:,.2f} (+{tsl.activation_percentage:.2f}%)")
                    
                    # Current stop line
                    tsl_current = float(tsl.current_stop_price)
                    ax.axhline(y=tsl_current, color='orange', linestyle='-', alpha=0.8,
                              label=f"TSL: {tsl_current:,.2f}")
            
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
            
            # Add partial take profit lines if configured
            if hasattr(order, 'partial_take_profits') and order.partial_take_profits:
                # Define gradient colors for partial TPs
                colors = ['#00FF00', '#33CC33', '#009900', '#006600']  # Green gradient
                
                for i, ptp in enumerate(order.partial_take_profits):
                    if hasattr(ptp, 'price') and ptp.price:
                        ptp_price = float(ptp.price)
                        ptp_pct = ptp.profit_percentage
                        position_pct = ptp.position_percentage
                        
                        # Use different color for each level with consistent pattern
                        color_idx = min(i, len(colors) - 1)
                        
                        # Add dashed line with custom pattern for each level
                        ax.axhline(y=ptp_price, color=colors[color_idx], linestyle='--', 
                                   linewidth=1, alpha=0.7, dashes=(2, 1 + i))
                        
                        # Add text label for each partial TP level
                        ax.text(0.02, ptp_price, 
                               f"PTP {ptp.level}: {ptp_price:.2f} (+{ptp_pct:.2f}%, {position_pct}%)", 
                               transform=ax.get_yaxis_transform(),
                               va='center', ha='left', fontsize=9, 
                               backgroundcolor='white', alpha=0.7, color=colors[color_idx])
            
            # Add stop loss line if configured
            if order.stop_loss:
                sl_price = float(order.stop_loss.price)
                sl_pct = order.stop_loss.percentage
                ax.axhline(y=sl_price, color='red', linestyle='--', linewidth=1, alpha=0.7)
                # Format the text without $ symbol inside the text method
                ax.text(0.02, sl_price, f"SL: {sl_price:.2f} (-{sl_pct:.2f}%)", transform=ax.get_yaxis_transform(),
                       va='center', ha='left', fontsize=9, backgroundcolor='white', alpha=0.7)
            
            # Add trailing stop loss activation line if configured
            if hasattr(order, 'trailing_stop_loss') and order.trailing_stop_loss:
                tsl = order.trailing_stop_loss
                if hasattr(tsl, 'activation_price') and tsl.activation_price:
                    tsl_activation = float(tsl.activation_price)
                    tsl_current = float(tsl.current_stop_price)
                    
                    # Add activation line (dotted orange)
                    ax.axhline(y=tsl_activation, color='orange', linestyle=':', linewidth=1, alpha=0.7)
                    ax.text(0.02, tsl_activation, 
                           f"TSL Act: {tsl_activation:.2f} (+{tsl.activation_percentage:.2f}%)", 
                           transform=ax.get_yaxis_transform(),
                           va='center', ha='left', fontsize=9, 
                           backgroundcolor='white', alpha=0.7)
                    
                    # Add current stop price (solid orange)
                    ax.axhline(y=tsl_current, color='orange', linestyle='-', linewidth=1, alpha=0.7)
                    ax.text(0.02, tsl_current, 
                           f"TSL: {tsl_current:.2f}", 
                           transform=ax.get_yaxis_transform(),
                           va='center', ha='left', fontsize=9, 
                           backgroundcolor='white', alpha=0.7)
            
            return True
        except Exception as e:
            logger.error(f"Failed to add TP/SL lines: {e}")
            return False
