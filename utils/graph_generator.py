import matplotlib.pyplot as plt
import seaborn as sns
import pandas as pd
from datetime import datetime
import io
import numpy as np
import logging

class GraphGenerator:
    def __init__(self):
        # Initialize logger
        self.logger = logging.getLogger('GraphGenerator')
        
        # Set style for all plots
        plt.style.use('dark_background')
        self.default_colors = ['#2ecc71', '#3498db', '#9b59b6', '#e74c3c', '#f1c40f']
        
        # Configure default plot settings
        plt.rcParams.update({
            'figure.autolayout': True,
            'axes.grid': True,
            'grid.alpha': 0.3,
            'axes.labelsize': 10,
            'xtick.labelsize': 8,
            'ytick.labelsize': 8
        })

    def safe_generate_graph(self, generate_func, *args):
        """Wrapper to safely generate graphs with enhanced error handling"""
        try:
            # Clear any existing plots and set backend to non-interactive
            plt.close('all')
            plt.switch_backend('Agg')
            
            # Generate the graph with proper DPI and quality
            fig = generate_func(*args)
            
            # Create buffer with high-quality PNG
            buf = io.BytesIO()
            if isinstance(fig, plt.Figure):
                fig.savefig(buf, format='png', dpi=300, bbox_inches='tight')
            else:
                plt.savefig(buf, format='png', dpi=300, bbox_inches='tight')
            buf.seek(0)
            
            return buf
            
        except Exception as e:
            self.logger.error(f"Error generating graph: {str(e)}")
            plt.close('all')
            return None
        finally:
            plt.close('all')

    def _format_axes(self, ax):
        """Apply consistent formatting to graph axes"""
        ax.grid(True, alpha=0.3)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.tick_params(axis='both', which='major', labelsize=8)
        return ax

    def _add_watermark(self, fig):
        """Add subtle watermark to graphs"""
        fig.text(
            0.99, 0.01, 
            'BinanceBot Analysis', 
            fontsize=6, 
            color='gray', 
            alpha=0.5,
            ha='right'
        )
        return fig

    def generate_entry_price_histogram(self, symbol, entry_prices):
        if not entry_prices:
            return None
            
        return self.safe_generate_graph(self._generate_entry_price_histogram, symbol, entry_prices)

    def _generate_entry_price_histogram(self, symbol, entry_prices):
        """Internal method for histogram generation"""
        plt.figure(figsize=(10, 6))
        sns.histplot(entry_prices, bins=20, color=self.default_colors[0])
        plt.title(f'Entry Price Distribution for {symbol}')
        plt.xlabel('Entry Price (USDT)')
        plt.ylabel('Frequency')
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png')
        buf.seek(0)
        return buf

    def generate_position_stacking(self, symbol, timestamps, quantities, prices):
        """Generate enhanced stacking visualization"""
        if not timestamps or not quantities or not prices:
            return None
            
        try:
            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), height_ratios=[2, 1])
            
            # Position size plot
            cumulative_qty = np.cumsum(quantities)
            ax1.plot(timestamps, cumulative_qty, marker='o', color=self.default_colors[1])
            ax1.set_title(f'Position Building Over Time - {symbol}')
            ax1.set_ylabel('Cumulative Position Size')
            
            # Price overlay
            ax3 = ax1.twinx()
            ax3.plot(timestamps, prices, color=self.default_colors[2], alpha=0.5, linestyle='--')
            ax3.set_ylabel('Price USDT', color=self.default_colors[2])
            
            # Entry distribution
            sns.histplot(prices, bins=20, ax=ax2, color=self.default_colors[0])
            ax2.set_title('Entry Price Distribution')
            ax2.set_xlabel('Entry Price (USDT)')
            
            # Format axes
            for ax in [ax1, ax2, ax3]:
                self._format_axes(ax)
                
            plt.tight_layout()
            fig = self._add_watermark(fig)
            
            return fig
            
        except Exception as e:
            self.logger.error(f"Error generating position stacking graph: {e}")
            return None

    def generate_time_between_buys(self, buy_timestamps):
        """Generate time between buys visualization"""
        if len(buy_timestamps) < 2:
            return None
            
        return self.safe_generate_graph(self._generate_time_between_buys, buy_timestamps)

    def _generate_time_between_buys(self, buy_timestamps):
        time_diffs = []
        for i in range(1, len(buy_timestamps)):
            diff = (buy_timestamps[i] - buy_timestamps[i-1]).total_seconds() / 3600  # Convert to hours
            time_diffs.append(diff)
        
        plt.figure(figsize=(10, 6))
        sns.histplot(time_diffs, bins=20, color=self.default_colors[2])
        plt.title('Time Between Buys Distribution')
        plt.xlabel('Hours Between Buys')
        plt.ylabel('Frequency')
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png')
        buf.seek(0)
        return buf

    def generate_portfolio_evolution(self, timestamps, total_values):
        """Generate enhanced portfolio value evolution"""
        if not timestamps or not total_values:
            return None
            
        try:
            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), height_ratios=[3, 1])
            
            # Main portfolio value plot
            ax1.plot(timestamps, total_values, color=self.default_colors[3], linewidth=2)
            ax1.set_title('Portfolio Value Evolution')
            ax1.set_ylabel('Total Value (USDT)')
            
            # Calculate and plot percentage changes
            pct_changes = np.diff(total_values) / total_values[:-1] * 100
            ax2.bar(timestamps[1:], pct_changes, color=self.default_colors[4], alpha=0.6)
            ax2.set_title('Daily Percentage Changes')
            ax2.set_ylabel('Change (%)')
            
            # Format axes
            for ax in [ax1, ax2]:
                self._format_axes(ax)
                ax.tick_params(axis='x', rotation=45)
            
            plt.tight_layout()
            fig = self._add_watermark(fig)
            
            return fig
            
        except Exception as e:
            self.logger.error(f"Error generating portfolio evolution graph: {e}")
            return None

    def generate_asset_allocation(self, assets, values):
        """Generate pie chart of asset allocation"""
        if not assets or not values:
            return None
            
        return self.safe_generate_graph(self._generate_asset_allocation, assets, values)

    def _generate_asset_allocation(self, assets, values):
        plt.figure(figsize=(10, 10))
        plt.pie(values, labels=assets, colors=self.default_colors, autopct='%1.1f%%')
        plt.title('Asset Allocation')
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png')
        buf.seek(0)
        return buf
