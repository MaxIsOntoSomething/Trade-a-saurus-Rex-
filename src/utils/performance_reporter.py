import logging
import asyncio
import io
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from decimal import Decimal
from matplotlib import pyplot as plt
import matplotlib.dates as mdates
import pandas as pd
import numpy as np
from collections import defaultdict

from ..types.models import TimeFrame, Order, OrderStatus
from ..utils.yahoo_scrapooooor_sp500 import YahooSP500Scraper
from ..utils.yahoo_GOLD_scrapooooor import YahooGoldScraper
from ..utils.chart_generator import ChartGenerator

logger = logging.getLogger(__name__)

class PerformanceReporter:
    """Class to generate performance reports on timeframe resets"""
    
    def __init__(self, exchange_client, mongo_client, telegram_bot, config: Dict):
        """Initialize the performance reporter
        
        Args:
            exchange_client: The exchange client (Binance or Bybit)
            mongo_client: MongoDB client for data storage
            telegram_bot: Telegram bot for sending reports
            config: Application configuration
        """
        self.exchange_client = exchange_client
        self.mongo_client = mongo_client
        self.telegram_bot = telegram_bot
        self.config = config
        self.chart_generator = ChartGenerator()
        self.sp500_scraper = YahooSP500Scraper()
        self.gold_scraper = YahooGoldScraper()
        
        # Performance reporting config
        self.report_config = config.get('performance_reports', {
            'weekly_report': True,
            'monthly_report': True
        })
        
        logger.info("Performance Reporter initialized")
    
    async def handle_timeframe_reset(self, timeframe: TimeFrame, reset_data: Dict) -> None:
        """Handle a timeframe reset event and generate reports if enabled
        
        Args:
            timeframe: The timeframe that was reset
            reset_data: Data about the reset event
        """
        try:
            if timeframe == TimeFrame.WEEKLY and self.report_config.get('weekly_report', True):
                logger.info("Generating weekly performance report")
                await self.generate_weekly_report()
                
            elif timeframe == TimeFrame.MONTHLY and self.report_config.get('monthly_report', True):
                logger.info("Generating monthly performance report")
                await self.generate_monthly_report()
                
        except Exception as e:
            logger.error(f"Error handling timeframe reset for performance reporting: {e}")
    
    async def generate_weekly_report(self) -> None:
        """Generate and send a weekly performance report"""
        try:
            # Get current week number
            current_date = datetime.utcnow()
            week_number = current_date.isocalendar()[1]
            year = current_date.year
            
            # Get start and end of the week
            week_start = current_date - timedelta(days=current_date.weekday(), hours=current_date.hour, 
                                                minutes=current_date.minute, seconds=current_date.second,
                                                microseconds=current_date.microsecond)
            week_end = current_date
            
            # Set report title and timeframe
            report_title = f"Weekly Performance Report - Week {week_number}, {year}"
            timeframe = "weekly"
            
            # Gather data and send report
            report_data = await self._gather_report_data(week_start, week_end, timeframe)
            await self._send_performance_report(report_title, report_data, timeframe)
            
        except Exception as e:
            logger.error(f"Error generating weekly performance report: {e}")
    
    async def generate_monthly_report(self) -> None:
        """Generate and send a monthly performance report"""
        try:
            # Get current month info
            current_date = datetime.utcnow()
            month_name = current_date.strftime('%B')
            year = current_date.year
            
            # Get start and end of the month
            month_start = current_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            month_end = current_date
            
            # Set report title and timeframe
            report_title = f"Monthly Performance Report - {month_name} {year}"
            timeframe = "monthly"
            
            # Gather data and send report
            report_data = await self._gather_report_data(month_start, month_end, timeframe)
            await self._send_performance_report(report_title, report_data, timeframe)
            
        except Exception as e:
            logger.error(f"Error generating monthly performance report: {e}")
    
    async def _gather_report_data(self, start_date: datetime, end_date: datetime, timeframe: str) -> Dict:
        """Gather all necessary data for the performance report
        
        Args:
            start_date: Start date of the period
            end_date: End date of the period
            timeframe: The timeframe (weekly/monthly)
            
        Returns:
            Dict containing all report data
        """
        try:
            # Get orders (purchases) made during the period
            buy_orders = await self.mongo_client.get_buy_orders_in_date_range(start_date, end_date)
            
            # Get portfolio performance
            balance_snapshots = await self.mongo_client.get_balance_snapshots_in_range(start_date, end_date)
            
            # Get balance changes
            start_balance = await self.mongo_client.get_nearest_balance_snapshot(start_date)
            end_balance = await self.mongo_client.get_nearest_balance_snapshot(end_date)
            
            # Get deposits in period
            deposits = await self.mongo_client.get_deposits_in_range(start_date, end_date)
            
            # Get comparison data
            performance_data = {
                'btc': None,
                'sp500': None,
                'gold': None
            }
            
            # Try to get BTC yearly performance for comparison
            try:
                # Get BTC/USD performance
                btc_data = await self.exchange_client.get_historical_klines(
                    "BTCUSDT", "1d", start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')
                )
                
                if btc_data and len(btc_data) > 0:
                    btc_df = pd.DataFrame(btc_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 
                                                             'quote_volume', 'trades', 'taker_buy_base', 'taker_buy_quote', 'ignored'])
                    btc_df['timestamp'] = pd.to_datetime(btc_df['timestamp'], unit='ms')
                    btc_df['close'] = btc_df['close'].astype(float)
                    btc_df = btc_df.set_index('timestamp')
                    
                    performance_data['btc'] = btc_df
            except Exception as e:
                logger.warning(f"Could not fetch BTC data for performance report: {e}")
            
            # Try to get S&P 500 performance
            try:
                sp500_data = await self.sp500_scraper.get_historical_data(
                    start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')
                )
                
                if sp500_data is not None:
                    performance_data['sp500'] = sp500_data
            except Exception as e:
                logger.warning(f"Could not fetch S&P 500 data for performance report: {e}")
            
            # Try to get Gold performance
            try:
                gold_data = await self.gold_scraper.get_historical_data(
                    start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')
                )
                
                if gold_data is not None:
                    performance_data['gold'] = gold_data
            except Exception as e:
                logger.warning(f"Could not fetch Gold data for performance report: {e}")
            
            # Calculate portfolio performance
            portfolio_start = float(start_balance['total_balance']) if start_balance else 0
            portfolio_end = float(end_balance['total_balance']) if end_balance else 0
            
            # Calculate deposit-adjusted performance
            total_deposits = sum(deposit['amount'] for deposit in deposits)
            adjusted_end_balance = portfolio_end - total_deposits
            
            # Calculate performance percentages
            if portfolio_start > 0:
                portfolio_change_pct = ((adjusted_end_balance - portfolio_start) / portfolio_start) * 100
            else:
                portfolio_change_pct = 0
            
            # Return all data
            return {
                'timeframe': timeframe,
                'start_date': start_date,
                'end_date': end_date,
                'buy_orders': buy_orders,
                'balance_snapshots': balance_snapshots,
                'start_balance': portfolio_start,
                'end_balance': portfolio_end,
                'performance_pct': portfolio_change_pct,
                'deposits': deposits,
                'total_deposits': total_deposits,
                'performance_data': performance_data
            }
            
        except Exception as e:
            logger.error(f"Error gathering report data: {e}")
            return {}
    
    async def _send_performance_report(self, title: str, report_data: Dict, timeframe: str) -> None:
        """Format and send the performance report to users
        
        Args:
            title: Report title
            report_data: Report data dictionary
            timeframe: The timeframe (weekly/monthly)
        """
        try:
            if not report_data:
                logger.warning("No report data available to send")
                return
            
            # Base currency
            base_currency = self.config['trading'].get('base_currency', 'USDT')
            
            # Format order summary
            buy_orders = report_data.get('buy_orders', [])
            order_summary = []
            
            if buy_orders:
                symbols_data = {}
                
                # Group orders by symbol
                for order in buy_orders:
                    symbol = order['symbol']
                    price = float(order['price'])
                    quantity = float(order['quantity'])
                    
                    if symbol not in symbols_data:
                        symbols_data[symbol] = {
                            'total_quantity': 0,
                            'total_cost': 0,
                            'orders': []
                        }
                    
                    symbols_data[symbol]['total_quantity'] += quantity
                    symbols_data[symbol]['total_cost'] += price * quantity
                    symbols_data[symbol]['orders'].append(order)
                
                # Generate summary for each symbol
                for symbol, data in symbols_data.items():
                    avg_price = data['total_cost'] / data['total_quantity'] if data['total_quantity'] > 0 else 0
                    
                    # Get current price
                    try:
                        current_price = float(await self.exchange_client.get_current_price(symbol))
                        performance = ((current_price - avg_price) / avg_price) * 100 if avg_price > 0 else 0
                        performance_str = f" ({performance:+.2f}%)" if avg_price > 0 else ""
                    except:
                        current_price = 0
                        performance_str = ""
                    
                    asset = symbol.replace(base_currency, '')
                    order_summary.append(
                        f"💰 {asset}: {data['total_quantity']:.8f} @ ${avg_price:.2f} avg"
                        f"\n   Current: ${current_price:.2f}{performance_str}"
                    )
            
            # Format balance changes
            start_balance = report_data.get('start_balance', 0)
            end_balance = report_data.get('end_balance', 0)
            total_deposits = report_data.get('total_deposits', 0)
            
            balance_change = end_balance - start_balance
            adjusted_balance_change = balance_change - total_deposits
            
            # Calculate performance percentages
            if start_balance > 0:
                balance_change_pct = (balance_change / start_balance) * 100
                adjusted_change_pct = (adjusted_balance_change / start_balance) * 100
            else:
                balance_change_pct = 0
                adjusted_change_pct = 0
            
            # Generate performance chart
            chart_buf = await self._generate_performance_chart(report_data)
            
            # Build message
            message_parts = [
                f"📊 {title}",
                f"\n📅 Period: {report_data['start_date'].strftime('%Y-%m-%d')} to {report_data['end_date'].strftime('%Y-%m-%d')}",
                f"\n💵 Balance Changes:",
                f"  Start: ${start_balance:.2f}",
                f"  End: ${end_balance:.2f}",
                f"  Change: ${balance_change:.2f} ({balance_change_pct:+.2f}%)",
            ]
            
            # Add deposits if any
            if total_deposits > 0:
                message_parts.extend([
                    f"\n📥 Deposits: ${total_deposits:.2f}",
                    f"  Adjusted Change: ${adjusted_balance_change:.2f} ({adjusted_change_pct:+.2f}%)",
                ])
            
            # Add order summary if any
            if order_summary:
                message_parts.extend([
                    f"\n🛒 Purchases Made:",
                    *order_summary
                ])
            else:
                message_parts.append(f"\n🛒 No purchases made during this {timeframe} period")
            
            # Send the message to all allowed users
            for user_id in self.telegram_bot.allowed_users:
                try:
                    # Send text message
                    await self.telegram_bot.send_message(
                        chat_id=user_id,
                        text="\n".join(message_parts)
                    )
                    
                    # Send performance chart if available
                    if chart_buf:
                        await self.telegram_bot.send_photo(
                            chat_id=user_id,
                            photo=chart_buf,
                            caption=f"Performance Comparison ({timeframe.capitalize()})"
                        )
                except Exception as e:
                    logger.error(f"Error sending {timeframe} report to user {user_id}: {e}")
        
        except Exception as e:
            logger.error(f"Error sending performance report: {e}")
    
    async def _generate_performance_chart(self, report_data: Dict) -> Optional[bytes]:
        """Generate a performance comparison chart for the report
        
        Args:
            report_data: Report data dictionary
            
        Returns:
            Bytes buffer of the chart image or None if generation fails
        """
        try:
            performance_data = report_data.get('performance_data', {})
            balance_snapshots = report_data.get('balance_snapshots', [])
            
            if not balance_snapshots or not performance_data:
                return None
            
            # Create portfolio dataframe
            portfolio_df = pd.DataFrame(balance_snapshots)
            portfolio_df['timestamp'] = pd.to_datetime(portfolio_df['timestamp'])
            portfolio_df['total_balance'] = portfolio_df['total_balance'].astype(float)
            portfolio_df = portfolio_df.set_index('timestamp').sort_index()
            
            # Extract comparison data
            btc_df = performance_data.get('btc')
            sp500_df = performance_data.get('sp500')
            gold_df = performance_data.get('gold')
            
            # Create plot
            plt.figure(figsize=(10, 6))
            
            # Normalize data to starting value = 100 for comparison
            first_portfolio_value = portfolio_df['total_balance'].iloc[0]
            portfolio_normalized = (portfolio_df['total_balance'] / first_portfolio_value) * 100
            portfolio_normalized.name = 'Portfolio'
            
            # Plot portfolio performance
            plt.plot(portfolio_normalized.index, portfolio_normalized, label='Portfolio', color='blue', linewidth=2)
            
            # Plot BTC performance if available
            if btc_df is not None and not btc_df.empty:
                first_btc_value = btc_df['close'].iloc[0]
                btc_normalized = (btc_df['close'] / first_btc_value) * 100
                plt.plot(btc_normalized.index, btc_normalized, label='BTC', color='orange', linewidth=2)
            
            # Plot S&P 500 performance if available
            if sp500_df is not None and not sp500_df.empty:
                first_sp500_value = sp500_df['Close'].iloc[0]
                sp500_normalized = (sp500_df['Close'] / first_sp500_value) * 100
                plt.plot(sp500_df.index, sp500_normalized, label='S&P 500', color='green', linewidth=2)
            
            # Plot Gold performance if available
            if gold_df is not None and not gold_df.empty:
                first_gold_value = gold_df['Close'].iloc[0]
                gold_normalized = (gold_df['Close'] / first_gold_value) * 100
                plt.plot(gold_df.index, gold_normalized, label='Gold', color='gold', linewidth=2)
            
            # Format plot
            plt.title(f"Performance Comparison ({report_data['timeframe'].capitalize()})")
            plt.xlabel('Date')
            plt.ylabel('Performance (Base = 100)')
            plt.grid(True, alpha=0.3)
            plt.legend()
            
            # Format x-axis dates
            plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
            plt.gca().xaxis.set_major_locator(mdates.AutoDateLocator())
            
            # Add performance annotations
            final_portfolio_perf = portfolio_normalized.iloc[-1] - 100
            
            y_pos = portfolio_normalized.iloc[-1]
            plt.annotate(f"{final_portfolio_perf:.2f}%", 
                        xy=(portfolio_normalized.index[-1], y_pos),
                        xytext=(8, 0), textcoords='offset points',
                        fontsize=9, fontweight='bold', color='blue')
            
            # Save to buffer
            from io import BytesIO
            buf = BytesIO()
            plt.tight_layout()
            plt.savefig(buf, format='png', dpi=100)
            buf.seek(0)
            plt.close()
            
            return buf
            
        except Exception as e:
            logger.error(f"Error generating performance chart: {e}")
            return None 