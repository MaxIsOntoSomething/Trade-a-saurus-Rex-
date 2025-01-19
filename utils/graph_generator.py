import matplotlib.pyplot as plt
import seaborn as sns
import pandas as pd
from datetime import datetime
import io
import numpy as np

class GraphGenerator:
    def __init__(self):
        # Set style for all plots
        plt.style.use('dark_background')
        self.default_colors = ['#2ecc71', '#3498db', '#9b59b6', '#e74c3c', '#f1c40f']

    def safe_generate_graph(self, generate_func, *args):
        """Wrapper to safely generate graphs with error handling"""
        try:
            # Clear any existing plots
            plt.close('all')
            
            # Generate the graph
            return generate_func(*args)
        except Exception as e:
            print(f"Error generating graph: {str(e)}")
            plt.close('all')  # Cleanup on error
            return None
        finally:
            # Ensure all plots are closed to prevent memory leaks
            plt.close('all')

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
        """Generate stacking visualization of positions over time"""
        if not timestamps or not quantities or not prices:
            return None
            
        return self.safe_generate_graph(self._generate_position_stacking, symbol, timestamps, quantities, prices)

    def _generate_position_stacking(self, symbol, timestamps, quantities, prices):
        plt.figure(figsize=(12, 6))
        cumulative_qty = np.cumsum(quantities)
        
        plt.plot(timestamps, cumulative_qty, marker='o', color=self.default_colors[1])
        plt.title(f'Position Building Over Time for {symbol}')
        plt.xlabel('Date')
        plt.ylabel('Cumulative Position Size')
        plt.xticks(rotation=45)
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png')
        buf.seek(0)
        return buf

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
        """Generate portfolio value evolution over time"""
        if not timestamps or not total_values:
            return None
            
        return self.safe_generate_graph(self._generate_portfolio_evolution, timestamps, total_values)

    def _generate_portfolio_evolution(self, timestamps, total_values):
        plt.figure(figsize=(12, 6))
        plt.plot(timestamps, total_values, color=self.default_colors[3])
        plt.title('Portfolio Value Evolution')
        plt.xlabel('Date')
        plt.ylabel('Total Value (USDT)')
        plt.xticks(rotation=45)
        plt.grid(True, alpha=0.3)
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png')
        buf.seek(0)
        return buf

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
