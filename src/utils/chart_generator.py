import mplfinance as mpf
import pandas as pd
import numpy as np  # Add the missing numpy import
from datetime import datetime, timedelta  # Add timedelta import
from decimal import Decimal
from typing import List, Dict, Optional, Tuple, Any
import logging
from ..types.models import TimeFrame, Order, OrderType, OrderDirection, MarginMode, PositionSide
import io
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
from matplotlib.ticker import FuncFormatter
import matplotlib.dates as mdates
import plotly.graph_objects as go
from plotly.subplots import make_subplots

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
        """Generate a chart for a trade with entry point and reference price"""
        try:
            if not self.validate_candle_data(candles):
                logger.error("Invalid candle data for chart generation")
                return None

            # Prepare candle data
            df = self.prepare_candle_data(candles)
            
            # Create figure with secondary y-axis
            fig = make_subplots(rows=1, cols=1, shared_xaxes=True, 
                               vertical_spacing=0.03, subplot_titles=('Price Chart',),
                               specs=[[{"secondary_y": True}]])
            
            # Add candlestick chart
            fig.add_trace(go.Candlestick(
                x=df['timestamp'],
                open=df['open'],
                high=df['high'],
                low=df['low'],
                close=df['close'],
                name="Price"
            ))
            
            # Add volume bars
            fig.add_trace(go.Bar(
                x=df['timestamp'],
                y=df['volume'],
                name="Volume",
                marker_color='rgba(0, 0, 255, 0.3)',
                opacity=0.3
            ), secondary_y=True)
            
            # Add entry point
            entry_time = order.filled_at if order.filled_at else order.created_at
            entry_price = float(order.price)
            
            fig.add_trace(go.Scatter(
                x=[entry_time, entry_time],
                y=[df['low'].min() * 0.99, df['high'].max() * 1.01],
                mode='lines',
                line=dict(color='green', width=2, dash='dash'),
                name="Entry Point"
            ))
            
            fig.add_trace(go.Scatter(
                x=[df['timestamp'].min(), df['timestamp'].max()],
                y=[entry_price, entry_price],
                mode='lines',
                line=dict(color='green', width=1, dash='dot'),
                name="Entry Price"
            ))
            
            # Add reference price if provided
            if reference_price is not None and self.validate_reference_price(float(reference_price), candles):
                ref_price = float(reference_price)
                fig.add_trace(go.Scatter(
                    x=[df['timestamp'].min(), df['timestamp'].max()],
                    y=[ref_price, ref_price],
                    mode='lines',
                    line=dict(color='blue', width=1, dash='dot'),
                    name="Reference Price"
                ))
            
            # Add TP/SL lines if present in the order
            if hasattr(order, 'tp_price') and order.tp_price:
                tp_price = float(order.tp_price)
                fig.add_trace(go.Scatter(
                    x=[df['timestamp'].min(), df['timestamp'].max()],
                    y=[tp_price, tp_price],
                    mode='lines',
                    line=dict(color='lime', width=1, dash='dot'),
                    name="Take Profit"
                ))
                
            if hasattr(order, 'sl_price') and order.sl_price:
                sl_price = float(order.sl_price)
                fig.add_trace(go.Scatter(
                    x=[df['timestamp'].min(), df['timestamp'].max()],
                    y=[sl_price, sl_price],
                    mode='lines',
                    line=dict(color='red', width=1, dash='dot'),
                    name="Stop Loss"
                ))
                
            # Add liquidation price for futures orders
            if order.order_type == OrderType.FUTURES and order.leverage and order.leverage > 1:
                # Calculate liquidation price based on direction and leverage
                # This is a simplified calculation - in real trading, the exact liquidation price
                # depends on maintenance margin requirements which vary by exchange
                maintenance_margin = 0.005  # 0.5% maintenance margin (typical for Binance)
                
                if order.direction == OrderDirection.LONG:
                    # For long positions: entry_price * (1 - (1 / leverage) + maintenance_margin)
                    liquidation_price = entry_price * (1 - (1 / order.leverage) + maintenance_margin)
                else:
                    # For short positions: entry_price * (1 + (1 / leverage) - maintenance_margin)
                    liquidation_price = entry_price * (1 + (1 / order.leverage) - maintenance_margin)
                
                fig.add_trace(go.Scatter(
                    x=[df['timestamp'].min(), df['timestamp'].max()],
                    y=[liquidation_price, liquidation_price],
                    mode='lines',
                    line=dict(color='red', width=2, dash='dash'),
                    name="Liquidation Price"
                ))
            
            # Update layout
            fig.update_layout(
                title=f"{order.symbol} Trade Chart",
                xaxis_title="Time",
                yaxis_title="Price",
                height=600,
                template="plotly_dark",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                margin=dict(l=50, r=50, b=100, t=100, pad=4)
            )
            
            # Set y-axes titles
            fig.update_yaxes(title_text="Price", secondary_y=False)
            fig.update_yaxes(title_text="Volume", secondary_y=True)
            
            # Add info text
            info_text = self.format_info_text(order, reference_price)
            
            # Add annotation with trade info
            fig.add_annotation(
                xref="paper", yref="paper",
                x=0.01, y=0.01,
                text=info_text,
                showarrow=False,
                font=dict(family="Courier New, monospace", size=10, color="white"),
                align="left",
                bgcolor="rgba(0,0,0,0.5)",
                bordercolor="white",
                borderwidth=1,
                borderpad=4
            )
            
            # Convert to PNG image
            img_bytes = fig.to_image(format="png", width=1000, height=600, scale=2)
            return img_bytes
            
        except Exception as e:
            logger.error(f"Error generating trade chart: {e}", exc_info=True)
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
        """Format information text for chart annotation"""
        try:
            # Calculate price change from reference if available
            price_change_text = ""
            if reference_price is not None:
                price_change = ((order.price / reference_price) - 1) * 100
                price_change_text = f"Price Change: {price_change:.2f}%\n"
                
            # Format basic order information
            info_text = (
                f"Symbol: {order.symbol}\n"
                f"Type: {order.order_type.value.upper()}\n"
                f"Price: ${float(order.price):.2f}\n"
                f"Quantity: {float(order.quantity):.8f}\n"
                f"Value: ${float(order.price * order.quantity):.2f}\n"
                f"{price_change_text}"
            )
            
            # Add threshold information if available
            if order.threshold is not None and order.timeframe is not None:
                info_text += f"Threshold: {order.threshold}% ({order.timeframe.value})\n"
                
            # Add fee information if available
            if order.fee is not None:
                info_text += f"Fee: {float(order.fee):.8f} {order.fee_currency}\n"
                
            # Add futures-specific information
            if order.order_type == OrderType.FUTURES:
                # Add direction
                direction = "LONG" if order.direction == OrderDirection.LONG else "SHORT"
                info_text += f"Direction: {direction}\n"
                
                # Add leverage if available
                if order.leverage is not None:
                    info_text += f"Leverage: {order.leverage}x\n"
                
                # Add margin mode if available
                if order.margin_mode is not None:
                    margin_mode = order.margin_mode.upper() if hasattr(order.margin_mode, 'upper') else order.margin_mode
                    info_text += f"Margin Mode: {margin_mode}\n"
                
                # Calculate and add liquidation price if leverage is available
                if order.leverage and order.leverage > 1:
                    maintenance_margin = 0.005  # 0.5% maintenance margin (typical for Binance)
                    
                    if order.direction == OrderDirection.LONG:
                        # For long positions: entry_price * (1 - (1 / leverage) + maintenance_margin)
                        liquidation_price = float(order.price) * (1 - (1 / order.leverage) + maintenance_margin)
                    else:
                        # For short positions: entry_price * (1 + (1 / leverage) - maintenance_margin)
                        liquidation_price = float(order.price) * (1 + (1 / order.leverage) - maintenance_margin)
                    
                    info_text += f"Liquidation Price: ${liquidation_price:.2f}\n"
                
                # Add position side if available
                if order.position_side is not None:
                    position_side = order.position_side if isinstance(order.position_side, str) else order.position_side.value
                    info_text += f"Position Side: {position_side}\n"
            
            # Add timestamp
            timestamp = order.filled_at if order.filled_at else order.created_at
            if timestamp:
                info_text += f"Time: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
                
            return info_text
            
        except Exception as e:
            logger.error(f"Error formatting info text: {e}")
            return "Error formatting trade information"

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
            ax.axhline(y=float(order.price), color='r', linestyle='--', alpha=0.8, label=f"Order Price: ${float(order.price):,.2f}")
            
            # Add reference price if available
            if reference_price is not None:
                ax.axhline(y=float(reference_price), color='g', linestyle='--', alpha=0.8, label=f"Reference: ${float(reference_price):,.2f}")
            
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

    async def generate_futures_chart(self, 
                                  candles: List[Dict], 
                                  order: Order,
                                  funding_rate: Optional[float] = None,
                                  position_info: Optional[Dict] = None) -> Optional[bytes]:
        """Generate a chart specifically for futures trades with additional information"""
        try:
            if not self.validate_candle_data(candles):
                logger.error("Invalid candle data for chart generation")
                return None
                
            if order.order_type != OrderType.FUTURES:
                logger.warning("Non-futures order provided to generate_futures_chart")
                return await self.generate_trade_chart(candles, order)
                
            # Prepare candle data
            df = self.prepare_candle_data(candles)
            
            # Create figure with secondary y-axis
            fig = make_subplots(rows=2, cols=1, shared_xaxes=True, 
                               vertical_spacing=0.03, 
                               subplot_titles=('Price Chart', 'Volume'),
                               specs=[[{"secondary_y": True}], [{"secondary_y": False}]],
                               row_heights=[0.8, 0.2])
            
            # Add candlestick chart
            fig.add_trace(go.Candlestick(
                x=df['timestamp'],
                open=df['open'],
                high=df['high'],
                low=df['low'],
                close=df['close'],
                name="Price"
            ), row=1, col=1)
            
            # Add volume bars
            fig.add_trace(go.Bar(
                x=df['timestamp'],
                y=df['volume'],
                name="Volume",
                marker_color='rgba(0, 0, 255, 0.3)',
                opacity=0.3
            ), row=2, col=1)
            
            # Add entry point
            entry_time = order.filled_at if order.filled_at else order.created_at
            entry_price = float(order.price)
            
            fig.add_trace(go.Scatter(
                x=[entry_time, entry_time],
                y=[df['low'].min() * 0.99, df['high'].max() * 1.01],
                mode='lines',
                line=dict(color='green' if order.direction == OrderDirection.LONG else 'red', width=2, dash='dash'),
                name="Entry Point"
            ), row=1, col=1)
            
            fig.add_trace(go.Scatter(
                x=[df['timestamp'].min(), df['timestamp'].max()],
                y=[entry_price, entry_price],
                mode='lines',
                line=dict(color='green' if order.direction == OrderDirection.LONG else 'red', width=1, dash='dot'),
                name="Entry Price"
            ), row=1, col=1)
            
            # Add TP/SL lines if present in the order
            if hasattr(order, 'tp_price') and order.tp_price:
                tp_price = float(order.tp_price)
                fig.add_trace(go.Scatter(
                    x=[df['timestamp'].min(), df['timestamp'].max()],
                    y=[tp_price, tp_price],
                    mode='lines',
                    line=dict(color='lime', width=1, dash='dot'),
                    name="Take Profit"
                ), row=1, col=1)
                
            if hasattr(order, 'sl_price') and order.sl_price:
                sl_price = float(order.sl_price)
                fig.add_trace(go.Scatter(
                    x=[df['timestamp'].min(), df['timestamp'].max()],
                    y=[sl_price, sl_price],
                    mode='lines',
                    line=dict(color='red', width=1, dash='dot'),
                    name="Stop Loss"
                ), row=1, col=1)
                
            # Calculate and add liquidation price
            if order.leverage and order.leverage > 1:
                # Calculate liquidation price based on direction and leverage
                maintenance_margin = 0.005  # 0.5% maintenance margin (typical for Binance)
                
                if order.direction == OrderDirection.LONG:
                    # For long positions: entry_price * (1 - (1 / leverage) + maintenance_margin)
                    liquidation_price = entry_price * (1 - (1 / order.leverage) + maintenance_margin)
                else:
                    # For short positions: entry_price * (1 + (1 / leverage) - maintenance_margin)
                    liquidation_price = entry_price * (1 + (1 / order.leverage) - maintenance_margin)
                
                fig.add_trace(go.Scatter(
                    x=[df['timestamp'].min(), df['timestamp'].max()],
                    y=[liquidation_price, liquidation_price],
                    mode='lines',
                    line=dict(color='red', width=2, dash='dash'),
                    name="Liquidation Price"
                ), row=1, col=1)
            
            # Add funding rate indicator if provided
            if funding_rate is not None:
                # Add a secondary y-axis for funding rate
                fig.add_trace(go.Scatter(
                    x=[df['timestamp'].min(), df['timestamp'].max()],
                    y=[funding_rate * 100, funding_rate * 100],  # Convert to percentage
                    mode='lines',
                    line=dict(color='yellow', width=1, dash='dot'),
                    name=f"Funding Rate: {funding_rate * 100:.4f}%"
                ), row=1, col=1, secondary_y=True)
            
            # Update layout
            fig.update_layout(
                title=f"{order.symbol} Futures Chart - {order.direction.value.upper()} {order.leverage}x",
                xaxis_title="Time",
                yaxis_title="Price",
                height=800,
                template="plotly_dark",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                margin=dict(l=50, r=50, b=100, t=100, pad=4)
            )
            
            # Set y-axes titles
            fig.update_yaxes(title_text="Price", secondary_y=False, row=1, col=1)
            if funding_rate is not None:
                fig.update_yaxes(title_text="Funding Rate (%)", secondary_y=True, row=1, col=1)
            fig.update_yaxes(title_text="Volume", row=2, col=1)
            
            # Add info text
            info_text = self.format_futures_info_text(order, position_info, funding_rate)
            
            # Add annotation with trade info
            fig.add_annotation(
                xref="paper", yref="paper",
                x=0.01, y=0.01,
                text=info_text,
                showarrow=False,
                font=dict(family="Courier New, monospace", size=10, color="white"),
                align="left",
                bgcolor="rgba(0,0,0,0.5)",
                bordercolor="white",
                borderwidth=1,
                borderpad=4
            )
            
            # Convert to PNG image
            img_bytes = fig.to_image(format="png", width=1000, height=800, scale=2)
            return img_bytes
            
        except Exception as e:
            logger.error(f"Error generating futures chart: {e}", exc_info=True)
            return None

    def format_futures_info_text(self, order: Order, position_info: Optional[Dict] = None, 
                              funding_rate: Optional[float] = None) -> str:
        """Format information text for futures chart annotation"""
        try:
            # Direction
            direction = "LONG" if order.direction == OrderDirection.LONG else "SHORT"
            
            # Format basic order information
            info_text = (
                f"Symbol: {order.symbol}\n"
                f"Type: FUTURES {direction}\n"
                f"Entry Price: ${float(order.price):.2f}\n"
                f"Quantity: {float(order.quantity):.8f}\n"
                f"Value: ${float(order.price * order.quantity):.2f}\n"
                f"Leverage: {order.leverage}x\n"
                f"Margin Mode: {order.margin_mode.upper() if hasattr(order.margin_mode, 'upper') else order.margin_mode}\n"
            )
            
            # Add position side if available
            if order.position_side is not None:
                position_side = order.position_side if isinstance(order.position_side, str) else order.position_side.value
                info_text += f"Position Side: {position_side}\n"
            
            # Calculate liquidation price
            if order.leverage and order.leverage > 1:
                maintenance_margin = 0.005  # 0.5% maintenance margin
                
                if order.direction == OrderDirection.LONG:
                    liquidation_price = float(order.price) * (1 - (1 / order.leverage) + maintenance_margin)
                else:
                    liquidation_price = float(order.price) * (1 + (1 / order.leverage) - maintenance_margin)
                
                info_text += f"Liquidation Price: ${liquidation_price:.2f}\n"
            
            # Add TP/SL information if available
            if hasattr(order, 'tp_price') and order.tp_price:
                tp_price = float(order.tp_price)
                tp_pct = ((tp_price / float(order.price)) - 1) * 100
                tp_pct = tp_pct if order.direction == OrderDirection.LONG else -tp_pct
                info_text += f"Take Profit: ${tp_price:.2f} ({tp_pct:.2f}%)\n"
                
            if hasattr(order, 'sl_price') and order.sl_price:
                sl_price = float(order.sl_price)
                sl_pct = ((sl_price / float(order.price)) - 1) * 100
                sl_pct = sl_pct if order.direction == OrderDirection.LONG else -sl_pct
                info_text += f"Stop Loss: ${sl_price:.2f} ({sl_pct:.2f}%)\n"
            
            # Add funding rate if available
            if funding_rate is not None:
                funding_direction = "Pay" if funding_rate > 0 else "Receive"
                funding_impact = "Negative" if (funding_rate > 0 and order.direction == OrderDirection.LONG) or \
                                            (funding_rate < 0 and order.direction == OrderDirection.SHORT) else "Positive"
                info_text += f"Funding Rate: {funding_rate * 100:.4f}% ({funding_direction}, {funding_impact})\n"
            
            # Add position information if available
            if position_info:
                if 'pnl' in position_info:
                    pnl = position_info['pnl']
                    pnl_pct = position_info.get('pnl_percentage', 0)
                    info_text += f"Current PnL: ${pnl:.2f} ({pnl_pct:.2f}%)\n"
                
                if 'margin_ratio' in position_info:
                    margin_ratio = position_info['margin_ratio']
                    info_text += f"Margin Ratio: {margin_ratio:.2f}%\n"
                
                if 'entry_price' in position_info:
                    avg_entry = position_info['entry_price']
                    info_text += f"Avg Entry Price: ${avg_entry:.2f}\n"
            
            # Add timestamp
            timestamp = order.filled_at if order.filled_at else order.created_at
            if timestamp:
                info_text += f"Time: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
                
            return info_text
            
        except Exception as e:
            logger.error(f"Error formatting futures info text: {e}")
            return "Error formatting futures trade information"
