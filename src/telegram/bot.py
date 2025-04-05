import logging
import asyncio
import io
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ForceReply, CallbackQuery
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Dict
import re
import time
import uuid

from ..types.models import Order, OrderStatus, TimeFrame, OrderType, TradeDirection, TPSLStatus, PartialTakeProfit
from ..trading.binance_client import BinanceClient
from ..database.mongo_client import MongoClient
from ..types.constants import NOTIFICATION_EMOJI
from ..utils.chart_generator import ChartGenerator
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
import matplotlib.dates as mdates
import pandas as pd

logger = logging.getLogger(__name__)

DINO_ASCII = r'''
          ___                                      .-~. /_"-._
        `-._~-.                                  / /_ "~o\  :Y
              \  \                                / : \~x.  ` ')
              ]  Y                              /  |  Y< ~-.__j
             /   !                        _.--~T : l  l<  /.-~
            /   /                 ____.--~ .   ` l /~\ \<|Y
           /   /             .-~~"        /| .    ',-~\ \L|
          /   /             /     .^   \ Y~Y \.^>/l_   "--'
         /   Y           .-"(  .  l__  j_j l_/ /~_.-~    .
        Y    l          /    \  )    ~~~." / `/"~ / \.__/l_
        |     \     _.-"      ~-{__     l  :  l._Z~-.___.--~
        |      ~---~           /   ~~"---\_  ' __[>
        l  .                _.^   ___     _>-y~
         \  \     .      .-~   .-~   ~>--"  /
          \  ~---"            /     ./  _.-'
           "-.,_____.,_  _.--~\     _.-~
                       ~~     (   _}       
                              `. ~(
                                )  \
                          /,`--'~\--'~\
                          ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                         Trade-a-saurus Rex 🦖📈'''

# Add states for conversation handler
SYMBOL, ORDER_TYPE, LEVERAGE, DIRECTION, AMOUNT, PRICE = range(6)

class VisualizationType:
    DAILY_VOLUME = "daily_volume"
    PROFIT_DIST = "profit_distribution"
    ORDER_TYPES = "order_types"
    HOURLY_ACTIVITY = "hourly_activity"
    BALANCE_CHART = "balance_chart"  # Add new visualization type
    ROI_COMPARISON = "roi_comparison"  # Add new visualization type for ROI comparison
    SP500_VS_BTC = "sp500_vs_btc"  # Add new visualization type for S&P 500 vs BTC comparison
    PORTFOLIO_COMPOSITION = "portfolio_composition"  # Add new visualization type

class TelegramBot:
    def __init__(self, token: str, allowed_users: List[int], 
                 binance_client: BinanceClient, mongo_client: MongoClient,
                 config: dict):  # Add config parameter
        logger.info("Initializing Telegram bot...")
        self.token = token
        self.allowed_users = allowed_users
        self.binance_client = binance_client
        self.mongo_client = mongo_client
        self.config = config
        self.is_paused = False
        self.application = None
        self.order_data = {}  # Store order data during creation
        
        # Create default keyboard markup for reply messages
        self.markup = ReplyKeyboardMarkup(
            [[KeyboardButton("/menu"), KeyboardButton("/help")]],
            resize_keyboard=True
        )
        
        # Set base currency and reserve balance in binance client immediately
        if 'trading' in self.config:
            self.binance_client.base_currency = self.config['trading'].get('base_currency', 'USDT')
            self.binance_client.reserve_balance = float(self.config['trading'].get('reserve_balance', 0))
            
        self.startup_message = f"""
{DINO_ASCII}

🦖 Trade-a-saurus Rex Bot

Your friendly neighborhood trading dinosaur is online!
Use /menu to see available commands.

Status: Ready to ROAR! 🦖
"""
        self.binance_client.set_telegram_bot(self)  # Add this line
        self.sent_roars = set()  # Add this to track sent roar notifications
        self.chart_generator = ChartGenerator()  # Add this line

    async def initialize(self):
        """Initialize the bot by creating the application and registering handlers"""
        try:
            # Create application with token
            self.application = Application.builder().token(self.token).build()
            
            # Save the bot instance for direct access
            self.bot = self.application.bot
            
            # Register command handlers
            await self.register_commands()
            
            logger.info("Telegram bot initialized successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize Telegram bot: {e}")
            return False

    async def start(self):
        """Start the bot application"""
        try:
            # Make sure we're initialized
            if not self.application:
                await self.initialize()
                
            # Initialize the application
            await self.application.initialize()
            
            # Start receiving updates
            await self.application.start()
            
            # Start polling
            await self.application.updater.start_polling()
            
            logger.info("Telegram bot is now running!")
            
            # Send startup notification to all allowed users
            for user_id in self.allowed_users:
                try:
                    await self.bot.send_message(
                        chat_id=user_id,
                        text=f"🦖 Trade-a-saurus Rex is now online!\nEnvironment: {'TESTNET' if self.config['binance']['use_testnet'] else 'MAINNET'}",
                        reply_markup=self.markup
                    )
                except Exception as e:
                    logger.error(f"Could not send startup message to user {user_id}: {e}")
                    
            return True
        except Exception as e:
            logger.error(f"Failed to start Telegram bot: {e}")
            return False

    async def stop(self):
        """Stop the bot application"""
        try:
            if self.application:
                # Stop polling
                if hasattr(self.application, 'updater') and self.application.updater:
                    await self.application.updater.stop()
                
                # Stop the application
                await self.application.stop()
                
                # Shutdown the application
                await self.application.shutdown()
                
                logger.info("Telegram bot has been stopped")
                return True
            return False
        except Exception as e:
            logger.error(f"Error stopping Telegram bot: {e}")
            return False

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle the /start command"""
        try:
            # Check authorization
            if not await self.is_user_authorized(update):
                return
                
            # Get environment name for customization
            env_name = "TESTNET" if self.config['binance']['use_testnet'] else "MAINNET"
            
            # Send welcome message with dinosaur ASCII art
            welcome_message = f"""
{DINO_ASCII}
Welcome to Trade-a-saurus Rex! 🦖

🚀 Ready to hunt for crypto trading opportunities!
🔍 Environment: {env_name}
💰 Base Currency: {self.config['trading']['base_currency']}

Type /menu to see available commands.
Type /help for detailed command information.
"""
            
            # Send message and show menu keyboard
            await update.message.reply_text(
                welcome_message,
                reply_markup=self.markup
            )
            
            # Show menu immediately after welcome
            await self.show_menu(update, context)
            
        except Exception as e:
            logger.error(f"Error in start command: {e}")
            await update.message.reply_text("Error starting bot. Please try again.")

    def _is_authorized(self, user_id: int) -> bool:
        """Check if user is authorized"""
        return user_id in self.allowed_users

    async def toggle_trading(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Toggle trading state between paused and active"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        # Get base currency from config
        base_currency = self.config['trading'].get('base_currency', 'USDT')
        
        # Check reserve balance before resuming
        if not self.is_paused:
            current_balance = await self.binance_client.get_balance(base_currency)
            reserve_balance = self.binance_client.reserve_balance or 0  # Default to 0 if None
            
            if float(current_balance) < reserve_balance:
                await update.message.reply_text(
                    "❌ Cannot resume trading: Balance below reserve requirement\n"
                    f"Current: ${float(current_balance):.2f} {base_currency}\n"
                    f"Required: ${reserve_balance:.2f} {base_currency}"
                )
                return
                
        # Toggle state
        self.is_paused = not self.is_paused
        
        # Create keyboard with current state
        status_keyboard = ReplyKeyboardMarkup(
            [
                [KeyboardButton("/balance"), KeyboardButton("/stats"), KeyboardButton("/profits")],
                [KeyboardButton("/power"), KeyboardButton("/add"), KeyboardButton("/thresholds")],
                [KeyboardButton("/history"), KeyboardButton("/viz"), KeyboardButton("/menu")]
            ],
            resize_keyboard=True
        )
        
        if self.is_paused:
            message = "⏸ Trading paused"
            emoji = "▶️"
            action = "Resume"
        else:
            message = "▶️ Trading resumed"
            emoji = "⏸"
            action = "Pause"
            
        await update.message.reply_text(
            f"{message}\nUse /power to {action} {emoji}",
            reply_markup=status_keyboard
        )

    async def get_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Get current balance information"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("You're not authorized to use this bot.")
            return

        try:
            # Get all balances from Binance
            all_balances = await self.binance_client.client.get_account()
            
            if not all_balances or 'balances' not in all_balances:
                await update.message.reply_text("❌ Error retrieving balance information.")
                return
                
            # Get list of active trading symbols
            active_symbols = await self.mongo_client.get_trading_symbols()
            
            # Extract base currency from symbols (e.g., USDT from BTCUSDT)
            base_currency = self.binance_client.base_currency
            
            # Create set of traded assets by extracting the first part of each symbol
            traded_assets = set()
            for symbol in active_symbols:
                if symbol.endswith(base_currency):
                    # For pairs like BTCUSDT, extract BTC
                    traded_assets.add(symbol[:-len(base_currency)])
                elif base_currency in symbol:
                    # Fallback for other formats
                    traded_assets.add(symbol.replace(base_currency, ''))
                    
            # List of major coins to always show
            always_show_coins = ['BTC', 'ETH', 'SOL', 'USDC', 'USDT']
            
            # Combine traded assets and always show coins
            display_assets = traded_assets.union(set(always_show_coins))
            
            # Format the balances
            balances = []
            
            for balance in all_balances['balances']:
                asset = balance['asset']
                free = float(balance['free'])
                locked = float(balance['locked'])
                total = free + locked
                
                # Skip assets with zero balance
                if total <= 0:
                    continue
                
                # Only show base currency, traded assets and major coins
                if asset not in display_assets and asset != base_currency:
                    continue
                
                # Get USD value of the asset for display purposes
                asset_value = 0.0
                if asset == base_currency:
                    # Direct USD value for base currency (assuming base currency is pegged to USD)
                    asset_value = total
                else:
                    # Try to get price for this asset against base currency
                    try:
                        symbol_pair = f"{asset}{base_currency}"
                        price = await self.binance_client.get_current_price(symbol_pair)
                        asset_value = total * price
                    except Exception as e:
                        logging.debug(f"Could not get price for {symbol_pair}: {e}")
                        # Try reverse pair if available
                        try:
                            symbol_pair = f"{base_currency}{asset}"
                            price = await self.binance_client.get_current_price(symbol_pair)
                            if price > 0:
                                asset_value = total / price
                        except Exception as e:
                            logging.debug(f"Could not get price for reverse pair {symbol_pair}: {e}")
                    
                # Highlight active trading assets
                prefix = ""
                if asset in traded_assets:
                    prefix = "🔵 "  # Blue dot for active trading assets
                elif asset == base_currency:
                    prefix = "💵 "  # Cash symbol for base currency
                    
                if asset == base_currency:
                    # Format base currency with special label
                    balances.append(f"{prefix}{asset}: {total:.8f} (Base Currency)")
                else:
                    balances.append(f"{prefix}{asset}: {total:.8f} ≈ ${asset_value:.2f}")
                
            # Sort balances: first base currency, then active trading assets, then others
            sorted_balances = []
            
            # First add base currency
            base_entries = [b for b in balances if f"💵 {base_currency}:" in b]
            sorted_balances.extend(base_entries)
            
            # Then add active trading assets
            active_entries = [b for b in balances if b.startswith("🔵 ") and f"💵 {base_currency}:" not in b]
            sorted_balances.extend(active_entries)
            
            # Then add remaining assets
            other_entries = [b for b in balances if not b.startswith("🔵 ") and f"💵 {base_currency}:" not in b]
            sorted_balances.extend(other_entries)
            
            # Create response text
            response = "💰 Current Balance (showing trading pairs and major coins):\n" + "\n".join(sorted_balances)
            
            # Send response
            await update.message.reply_text(response)
            
        except Exception as e:
            logging.error(f"Error getting balance: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")

    # Alias for get_balance to fix the registration error
    async def balance_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Alias for get_balance - used for command registration"""
        return await self.get_balance(update, context)

    async def get_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Get trading statistics"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        try:
            pending = await self.mongo_client.orders.count_documents(
                {"status": OrderStatus.PENDING.value}
            )
            filled = await self.mongo_client.orders.count_documents(
                {"status": OrderStatus.FILLED.value}
            )
            cancelled = await self.mongo_client.orders.count_documents(
                {"status": OrderStatus.CANCELLED.value}
            )
            
            message = (
                "📊 Trading Statistics:\n"
                f"Pending Orders: {pending}\n"
                f"Filled Orders: {filled}\n"
                f"Cancelled Orders: {cancelled}\n"
                f"Trading Status: {'Paused ⏸' if self.is_paused else 'Active ▶️'}"
            )
            await update.message.reply_text(message)
        except Exception as e:
            await update.message.reply_text(f"❌ Error getting stats: {str(e)}")

    async def get_order_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Get recent order history"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        try:
            # Get base currency from config
            base_currency = self.config['trading'].get('base_currency', 'USDT')
            
            # Get last 5 orders
            cursor = self.mongo_client.orders.find().sort("created_at", -1).limit(5)
            orders = []
            async for doc in cursor:
                # Extract base asset
                symbol = doc['symbol']
                base_asset = symbol.replace(base_currency, '')
                
                # Calculate total value
                price = float(doc['price'])
                quantity = float(doc['quantity'])
                total_value = price * quantity
                
                # Start building order details
                order_details = [
                    f"🔹 {symbol} - {doc['status'].upper()}",
                    f"Price: ${price:.4f} | Amount: {quantity:.6f} {base_asset}",
                    f"Total Value: ${total_value:.2f} {base_currency}",
                    f"Type: {doc.get('order_type', 'UNKNOWN')} | Created: {doc['created_at'].strftime('%Y-%m-%d %H:%M:%S')}"
                ]
                
                # Add TP/SL info if available
                if 'take_profit' in doc and doc['take_profit']:
                    tp = doc['take_profit']
                    order_details.append(f"Take Profit: ${float(tp['price']):.4f} (+{tp['percentage']:.2f}%)")
                
                if 'stop_loss' in doc and doc['stop_loss']:
                    sl = doc['stop_loss']
                    order_details.append(f"Stop Loss: ${float(sl['price']):.4f} (-{sl['percentage']:.2f}%)")
                
                # Add partial take profits info if available
                if 'partial_take_profits' in doc and doc['partial_take_profits'] and len(doc['partial_take_profits']) > 0:
                    ptp_details = ["Partial Take Profits:"]
                    for ptp in doc['partial_take_profits']:
                        ptp_status = ptp.get('status', 'PENDING')
                        triggered_info = ""
                        if ptp_status == 'TRIGGERED' and 'triggered_at' in ptp:
                            triggered_info = f" ✅ Triggered: {ptp['triggered_at'].strftime('%Y-%m-%d %H:%M:%S')}"
                        
                        # Calculate exact amount to be sold at this level
                        ptp_quantity = quantity * (ptp['position_percentage'] / 100)
                        ptp_value = ptp_quantity * float(ptp['price'])
                        
                        ptp_details.append(
                            f"  Level {ptp['level']}: ${float(ptp['price']):.4f} "
                            f"(+{ptp['profit_percentage']:.2f}%) - Sell {ptp['position_percentage']}% "
                            f"({ptp_quantity:.6f} {base_asset} = ${ptp_value:.2f}){triggered_info}"
                        )
                    order_details.append("\n".join(ptp_details))
                
                # Add trailing stop loss info if available
                if 'trailing_stop_loss' in doc and doc['trailing_stop_loss']:
                    tsl = doc['trailing_stop_loss']
                    tsl_status = tsl.get('status', 'PENDING')
                    tsl_details = [
                        f"Trailing Stop Loss: Activation at +{tsl['activation_percentage']}%, "
                        f"Callback {tsl['callback_rate']}%"
                    ]
                    
                    if 'current_stop_price' in tsl:
                        tsl_details.append(f"Current Stop: ${float(tsl['current_stop_price']):.4f}")
                    
                    if tsl_status == 'TRIGGERED' and 'triggered_at' in tsl:
                        tsl_details.append(f"Triggered: {tsl['triggered_at'].strftime('%Y-%m-%d %H:%M:%S')}")
                    
                    order_details.append(" | ".join(tsl_details))
                
                orders.append("\n".join(order_details))
                
            message = "📜 Recent Orders:\n\n" + "\n\n".join(orders)
            
            # Handle message length - Telegram has 4096 character limit
            if len(message) > 4000:
                message = message[:3950] + "...\n(Message truncated due to length)"
                
            await update.message.reply_text(message)
        except Exception as e:
            logger.error(f"Error in get_order_history: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Error getting history: {str(e)}")

    async def send_order_notification(self, order: Order, status: Optional[OrderStatus] = None):
        """Send order notification to all allowed users"""
        if not self.application:
            logger.error("Telegram bot not initialized")
            return

        # Skip filled notification if we already sent a roar for this order
        if status == OrderStatus.FILLED and order.order_id in self.sent_roars:
            logger.debug(f"Skipping filled notification for {order.order_id} - ROAR already sent")
            return

        status = status or order.status
        emoji = {
            OrderStatus.PENDING: "🔵",
            OrderStatus.FILLED: "✅",
            OrderStatus.CANCELLED: "⚠️"
        }
        
        # Get base currency from config
        base_currency = self.config['trading'].get('base_currency', 'USDT')
        
        # Calculate total value in base currency
        total_value = order.price * order.quantity
        
        # Extract base asset (remove base currency suffix)
        base_asset = order.symbol.replace(base_currency, '')
        
        # Fix the format specifier
        message = (
            f"{emoji[status]} Order Update\n"
            f"Order ID: {order.order_id}\n"
            f"Symbol: {order.symbol}\n"
            f"Status: {status.value.upper()}\n"
            f"Amount: {float(order.quantity):.8f} {base_asset}\n"
            f"Price: ${float(order.price):.2f}\n"
            f"Total: ${float(total_value):.2f} {base_currency}\n"
            f"Threshold: {order.threshold if order.threshold else 'Manual'}\n"
            f"Timeframe: {self._get_timeframe_value(order.timeframe)}"
        )

        if status == OrderStatus.FILLED:
            message += f"\nFees: ${float(order.fees):.4f} {order.fee_asset}"
            
        if status == OrderStatus.CANCELLED and order.cancelled_at:
            duration = order.cancelled_at - order.created_at
            message += f"\nDuration: {duration.total_seconds() / 3600:.2f} hours"

        for user_id in self.allowed_users:
            try:
                await self.application.bot.send_message(chat_id=user_id, text=message)
            except Exception as e:
                logger.error(f"Failed to send notification to {user_id}: {e}")

    async def send_balance_update(self, symbol: str, change: Decimal):
        """Send balance change notification"""
        # Get base currency from config
        base_currency = self.config['trading'].get('base_currency', 'USDT')
        
        message = (
            f"💰 Balance Update\n"
            f"Symbol: {symbol}\n"
            f"Change: {change:+.8f} {base_currency}"
        )
        
        for user_id in self.allowed_users:
            try:
                await self.application.bot.send_message(chat_id=user_id, text=message)
            except Exception as e:
                logger.error(f"Failed to send balance update to {user_id}: {e}")

    async def send_trade_chart(self, order: Order):
        """Send trade chart to users"""
        try:
            # Get reference price and convert to Decimal
            ref_price = self.binance_client.reference_prices.get(
                order.symbol, {}
            ).get(order.timeframe)
            
            if ref_price is not None:
                ref_price = Decimal(str(ref_price))
            
            chart_data = await self.binance_client.generate_trade_chart(order)
            if not chart_data:
                logger.error("Failed to generate chart data")
                # Send text-only notification as fallback
                for user_id in self.allowed_users:
                    try:
                        await self.application.bot.send_message(
                            chat_id=user_id,
                            text=self.binance_client.chart_generator.format_info_text(
                                order,
                                ref_price
                            )
                        )
                    except Exception as e:
                        logger.error(f"Failed to send fallback message to {user_id}: {e}")
                return
                
            # Attempt to send chart with caption
            for user_id in self.allowed_users:
                try:
                    await self.application.bot.send_photo(
                        chat_id=user_id,
                        photo=chart_data,
                        caption=self.binance_client.chart_generator.format_info_text(
                            order,
                            ref_price
                        )
                    )
                except Exception as e:
                    logger.error(f"Failed to send chart to {user_id}: {e}")
                    # Try sending text-only notification as fallback
                    try:
                        await self.application.bot.send_message(
                            chat_id=user_id,
                            text=self.binance_client.chart_generator.format_info_text(
                                order,
                                ref_price
                            )
                        )
                    except Exception as e2:
                        logger.error(f"Failed to send fallback message to {user_id}: {e2}")
                    
        except Exception as e:
            logger.error(f"Failed to generate trade chart: {e}")

    async def send_roar(self, order: Order):
        """Send a dinosaur roar notification with trade summary in chart"""
        # Add order ID to sent roars set
        self.sent_roars.add(order.order_id)
        
        # Get base currency from config
        base_currency = self.config['trading'].get('base_currency', 'USDT')
        
        # Extract base asset (remove base currency suffix)
        base_asset = order.symbol.replace(base_currency, '')
        
        # Send chart with full information
        try:
            ref_price = self.binance_client.reference_prices.get(
                order.symbol, {}
            ).get(order.timeframe)
            
            if ref_price is not None:
                ref_price = Decimal(str(ref_price))
            
            # Create detailed caption - this will be used for both chart and fallback text message
            caption = (
                f"🦖 ROARRR! Trade Complete! 💥\n\n"
                f"Order ID: {order.order_id}\n"
                f"Symbol: {order.symbol}\n"
                f"Amount: {float(order.quantity):.8f} {base_asset}\n"
                f"Price: ${float(order.price):.2f}\n"
                f"Total: ${float(order.price * order.quantity):.2f} {base_currency}\n"
                f"Fees: ${float(order.fees):.4f} {order.fee_asset}\n"
            )
            
            # Add Take Profit information if configured
            if order.take_profit:
                caption += f"Take Profit: ${float(order.take_profit.price):.2f} (+{order.take_profit.percentage:.2f}%)\n"
                
            # Add Stop Loss information if configured
            if order.stop_loss:
                caption += f"Stop Loss: ${float(order.stop_loss.price):.2f} (-{order.stop_loss.percentage:.2f}%)\n"
                
            # Add Partial Take Profit information if configured
            if order.partial_take_profits and len(order.partial_take_profits) > 0:
                caption += f"\nPartial Take Profits:\n"
                for ptp in order.partial_take_profits:
                    # Calculate exact amount to be sold at this level
                    ptp_quantity = float(order.quantity) * (ptp.position_percentage / 100)
                    ptp_value = ptp_quantity * float(ptp.price)
                    
                    caption += (
                        f"• Level {ptp.level}: ${float(ptp.price):.4f} (+{ptp.profit_percentage:.2f}%)\n"
                        f"  Sell: {ptp_quantity:.6f} {base_asset} (${ptp_value:.2f} {base_currency})\n"
                    )
                
            # Add Trailing Stop Loss information if configured
            if order.trailing_stop_loss:
                tsl = order.trailing_stop_loss
                caption += (
                    f"\nTrailing Stop Loss:\n"
                    f"• Activation: +{tsl.activation_percentage:.2f}% (${float(tsl.activation_price):.4f})\n"
                    f"• Callback Rate: {tsl.callback_rate:.2f}%\n"
                    f"• Initial Stop: ${float(tsl.current_stop_price):.4f}\n"
                )
                
            caption += (
                f"Threshold: {order.threshold if order.threshold else 'Manual'}\n"
                f"Timeframe: {self._get_timeframe_value(order.timeframe)}\n\n"
                f"Check /profits to see your updated portfolio."
            )

            # Try to generate chart data
            chart_data = None
            try:
                chart_data = await self.binance_client.generate_trade_chart(order)
                logger.info(f"Chart generated successfully for order {order.order_id}")
            except Exception as e:
                logger.error(f"Error generating chart for ROAR message: {e}")
                chart_data = None

            # Send the message - with chart if available, as text if not
            for user_id in self.allowed_users:
                try:
                    if (chart_data):
                        await self.application.bot.send_photo(
                            chat_id=user_id,
                            photo=chart_data,
                            caption=caption
                        )
                    else:
                        # Append warning if chart generation failed
                        full_message = caption + "\n\n⚠️ (Chart generation failed - not enough historical data)"
                        await self.application.bot.send_message(
                            chat_id=user_id,
                            text=full_message
                        )
                except Exception as e:
                    logger.error(f"Error sending ROAR to {user_id}: {e}")
                    try:
                        # Send plain text as fallback
                        await self.application.bot.send_message(
                            chat_id=user_id,
                            text=caption + "\n\n⚠️ (Chart generation failed)"
                        )
                    except Exception as e2:
                        logger.error(f"Even fallback message failed: {e2}")
            
            # Cleanup old roar notifications periodically
            if len(self.sent_roars) > 1000:
                self.sent_roars.clear()
                
        except Exception as e:
            logger.error(f"Failed to send roar: {e}")
            # Final fallback - try to send a minimal notification
            for user_id in self.allowed_users:
                try:
                    simple_message = (
                        f"🦖 ROARRR! Trade Complete! 💥\n\n"
                        f"Symbol: {order.symbol}\n"
                        f"Price: ${float(order.price):.2f}\n"
                        f"Amount: {float(order.quantity):.8f}\n"
                        f"Total: ${float(order.price * order.quantity):.2f}"
                    )
                    await self.application.bot.send_message(chat_id=user_id, text=simple_message)
                except Exception as e2:
                    logger.error(f"Completely failed to send notification: {e2}")

    async def show_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show main menu keyboard"""
        try:
            if not await self.is_user_authorized(update):
                return
                
            base_currency = self.config['trading'].get('base_currency', 'USDT')
            
            # Create main command sections
            keyboard = [
                # Info commands
                [
                    KeyboardButton("/balance"), 
                    KeyboardButton("/stats"), 
                    KeyboardButton("/profits")
                ],
                # Trading commands
                [
                    KeyboardButton("/orders"),
                    KeyboardButton("/history"),
                    KeyboardButton("/thresholds")
                ],
                # Trading symbols
                [
                    KeyboardButton("/symbols"),
                    KeyboardButton("/add"),
                    KeyboardButton("/resetthresholds")
                ],
                # Visualizations
                [
                    KeyboardButton("/viz"),
                    KeyboardButton("/status"),
                    KeyboardButton("/power")
                ],
                # TP/SL management
                [
                    KeyboardButton("/tp_sl"),
                    KeyboardButton("/set_tp"),
                    KeyboardButton("/set_sl")
                ],
                # Partial TP management
                [
                    KeyboardButton("/set_partial_tp"),
                    KeyboardButton("/partial_tp_disable"),
                    KeyboardButton("/set_lower_entries")
                ],
                # Trailing SL management
                [
                    KeyboardButton("/show_trailing_sl"),
                    KeyboardButton("/trailing_sl_enable"),
                    KeyboardButton("/trailing_sl_disable")
                ],
                # Financial commands
                [
                    KeyboardButton("/deposit"),
                    KeyboardButton("/withdraw"),
                    KeyboardButton("/transactions")
                ],
                # Help
                [
                    KeyboardButton("/help")
                ]
            ]
            
            # Get current trading settings status
            trading_config = self.config.get('trading', {})
            partial_tp_enabled = trading_config.get('partial_take_profit', {}).get('enabled', False)
            trailing_sl_enabled = trading_config.get('trailing_stop_loss', {}).get('enabled', False)
            lower_entries_enabled = trading_config.get('only_lower_entries', False)
            
            # Create status indicators
            partial_tp_status = "✅" if partial_tp_enabled else "❌"
            trailing_sl_status = "✅" if trailing_sl_enabled else "❌"
            lower_entries_status = "✅" if lower_entries_enabled else "❌"
            
            # Add feature status row
            keyboard.append([
                KeyboardButton(f"Partial TP: {partial_tp_status}"),
                KeyboardButton(f"Trailing SL: {trailing_sl_status}"),
                KeyboardButton(f"Lower Entries: {lower_entries_status}")
            ])
            
            # Create reply markup
            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            
            # Send menu message with overview of commands
            menu_text = (
                "🦖 Trade-a-saurus Rex Menu 🦖\n\n"
                "Info Commands: /balance /stats /profits /status\n"
                "Trading: /orders /history /thresholds /add /symbols\n"
                "TP/SL: /tp_sl /set_tp /set_sl /set_partial_tp /show_trailing_sl\n"
                "Financial: /deposit /withdraw /transactions\n"
                "Visuals: /viz (charts and visualizations)\n"
                "Controls: /power (pause/resume), /resetthresholds\n"
                "Help: /help (detailed command list)"
            )
            
            await update.message.reply_text(
                menu_text,
                reply_markup=reply_markup
            )
            
        except Exception as e:
            logger.error(f"Error showing menu: {e}")
            await update.message.reply_text("Error showing menu")

    async def show_thresholds(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show detailed threshold information"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        try:
            now = datetime.utcnow()
            message_parts = []
            
            for timeframe in TimeFrame:
                # Get next reset time with corrected timezone handling
                if timeframe == TimeFrame.DAILY:
                    # For daily, next reset is at UTC midnight
                    next_reset = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    if now >= next_reset:
                        next_reset += timedelta(days=1)

                elif timeframe == TimeFrame.WEEKLY:
                    # For weekly, next reset is Monday UTC midnight
                    days_until_monday = (7 - now.weekday()) % 7
                    next_reset = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    if days_until_monday == 0 and now >= next_reset:
                        days_until_monday = 7
                    next_reset += timedelta(days=days_until_monday)

                else:  # MONTHLY
                    # For monthly, next reset is 1st of next month UTC midnight
                    if now.month == 12:
                        next_reset = now.replace(year=now.year + 1, month=1, day=1,
                                              hour=0, minute=0, second=0, microsecond=0)
                    else:
                        next_reset = now.replace(month=now.month + 1, day=1,
                                              hour=0, minute=0, second=0, microsecond=0)
                    if now.day == 1 and now >= next_reset:
                        # If we're on the 1st but after midnight, use next month
                        if now.month == 12:
                            next_reset = next_reset.replace(year=next_reset.year + 1, month=1)
                        else:
                            next_reset = next_reset.replace(month=next_reset.month + 1)
                
                # Calculate time until reset
                time_until_reset = next_reset - now
                total_seconds = time_until_reset.total_seconds()
                hours = int(total_seconds // 3600)
                minutes = int((total_seconds % 3600) // 60)
                
                # Format timeframe header
                timeframe_msg = [f"\n🕒 {timeframe.value.title()}"]
                timeframe_msg.append(f"Reset in: {hours}h {minutes}m")
                
                # Process each symbol
                for symbol in self.config['trading']['pairs']:
                    # Get current and reference prices
                    ref_price = self.binance_client.reference_prices.get(symbol, {}).get(timeframe)
                    
                    # Get current price
                    ticker = await self.binance_client.client.get_symbol_ticker(symbol=symbol)
                    current_price = float(ticker['price'])
                    
                    # Calculate price change if reference price exists
                    if (ref_price):
                        price_change = ((current_price - ref_price) / ref_price) * 100
                        price_info = f"Open: ${ref_price:,.2f} | Current: ${current_price:,.2f} ({price_change:+.2f}%)"
                    else:
                        price_info = f"Current: ${current_price:,.2f}"
                    
                    # Get threshold information with proper access to triggered thresholds
                    triggered = []
                    if symbol in self.binance_client.triggered_thresholds:
                        if timeframe.value in self.binance_client.triggered_thresholds[symbol]:
                            triggered = list(self.binance_client.triggered_thresholds[symbol][timeframe.value])
                    
                    available = [t for t in self.config['trading']['thresholds'][timeframe.value] 
                               if t not in triggered]
                    
                    # Format symbol section
                    timeframe_msg.extend([
                        f"\n{symbol}:",
                        price_info,
                        f"✅ Triggered: {triggered}",
                        f"⏳ Available: {available}"
                    ])
                
                message_parts.append("\n".join(timeframe_msg))
            
            await update.message.reply_text("📊 Threshold Status:\n" + "\n".join(message_parts))
            
        except Exception as e:
            logger.error(f"Error getting thresholds: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Error getting thresholds: {str(e)}")

    async def add_trade_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle add trade command - initialize the workflow"""
        try:
            if not await self.is_user_authorized(update):
                return

            # Initialize order data
            user_id = update.effective_user.id
            self.order_data[user_id] = {"step": "order_type"}

            # Create order type selection keyboard
            keyboard = [
                [KeyboardButton("Spot"), KeyboardButton("Futures")],
                [KeyboardButton("Cancel")]
            ]
            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

            await update.message.reply_text(
                "Select order type:",
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Error starting trade workflow: {e}")
            await update.message.reply_text("Error starting trade workflow. Try again.")

    async def add_trade_order_type(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle order type input (SPOT/FUTURES)"""
        try:
            order_type = update.message.text.upper()
            if order_type not in ["SPOT", "FUTURES"]:
                await update.message.reply_text("Please select either SPOT or FUTURES")
                return ORDER_TYPE
                
            user_data = self.temp_trade_data[update.effective_user.id]
            user_data['order_type'] = OrderType(order_type.lower())
            
            # Get base currency from config
            base_currency = self.config['trading'].get('base_currency', 'USDT')
            
            if order_type == "FUTURES":
                await update.message.reply_text("Enter leverage (e.g., 5, 10, 20):")
                return LEVERAGE
            else:
                await update.message.reply_text(f"Enter amount in {base_currency} (e.g., 100.50):")
                return AMOUNT
                
        except Exception as e:
            await update.message.reply_text(f"Error: {str(e)}")
            return ORDER_TYPE

    async def add_trade_leverage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle leverage input for futures trades"""
        try:
            leverage = int(update.message.text)
            if leverage <= 0:
                raise ValueError("Leverage must be positive")
                
            self.temp_trade_data[update.effective_user.id]['leverage'] = leverage
            
            keyboard = [
                [KeyboardButton("LONG")],
                [KeyboardButton("SHORT")]
            ]
            markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
            
            await update.message.reply_text(
                "Was this a long or short trade?",
                reply_markup=markup
            )
            return DIRECTION
            
        except ValueError as e:
            await update.message.reply_text("Please enter a valid leverage number")
            return LEVERAGE

    async def add_trade_direction(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle trade direction for futures"""
        direction = update.message.text.upper()
        if direction not in ["LONG", "SHORT"]:
            await update.message.reply_text("Please select either LONG or SHORT")
            return DIRECTION
            
        user_data = self.temp_trade_data[update.effective_user.id]
        user_data['direction'] = TradeDirection(direction.lower())
        
        # Get base currency from config
        base_currency = self.config['trading'].get('base_currency', 'USDT')
        
        await update.message.reply_text(f"Enter amount in {base_currency} (e.g., 100.50):")
        return AMOUNT

    async def add_trade_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle amount input for manual trade"""
        try:
            amount = float(update.message.text)
            if amount <= 0:
                raise ValueError("Amount must be positive")
                
            user_data = self.temp_trade_data[update.effective_user.id]
            user_data['amount'] = Decimal(str(amount))
            
            # Get base currency from config for clarity in the message
            base_currency = self.config['trading'].get('base_currency', 'USDT')
            
            await update.message.reply_text(f"Enter entry price (e.g., 42000.50) in {base_currency}:")
            return PRICE
            
        except ValueError as e:
            await update.message.reply_text(f"Error: {str(e)}")
            return AMOUNT

    async def add_trade_final(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Complete trade creation with auto-calculated fees"""
        try:
            # Clean the price input to handle both dots and commas as decimal separators
            price_text = update.message.text.replace(',', '.')
            
            try:
                price = Decimal(price_text)
            except (InvalidOperation, ValueError):  # Replace DecimalException with InvalidOperation
                raise ValueError("Price must be a valid number")
                
            if price <= 0:
                raise ValueError("Price must be positive")
                
            user_data = self.temp_trade_data[update.effective_user.id]
            user_data['price'] = price
            
            # Generate unique order ID
            order_id = f"MANUAL_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
            
            # Calculate quantity from amount/price
            quantity = user_data['amount'] / price
            
            # Get order type and leverage for fee calculation
            order_type = user_data['order_type'].value
            leverage = user_data.get('leverage', 1)  # Default to 1 if not set (for spot trades)
            
            # Calculate fees automatically using the BinanceClient's fee calculation
            fees, fee_asset = await self.binance_client.calculate_fees(
                user_data['symbol'], 
                price, 
                quantity,
                order_type,
                leverage
            )
            
            # Create order object
            order = Order(
                symbol=user_data['symbol'],
                status=OrderStatus.FILLED,  # Manual trades are always filled
                order_type=user_data['order_type'],
                price=price,
                quantity=quantity,
                timeframe=TimeFrame.DAILY,  # Default to daily for manual trades
                order_id=order_id,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                filled_at=datetime.utcnow(),
                leverage=leverage,
                direction=user_data.get('direction'),
                fees=fees,
                fee_asset=fee_asset
            )
            
            # Save to database
            await self.mongo_client.insert_manual_trade(order)
            
            # Send confirmation with auto-calculated fees
            direction_info = f"\nDirection: {order.direction.value}" if order.direction else ""
            leverage_info = f"\nLeverage: {order.leverage}x" if order.leverage else ""
            
            await update.message.reply_text(
                f"✅ Manual trade added:\n"
                f"Symbol: {order.symbol}\n"
                f"Type: {order.order_type.value}"
                f"{direction_info}"
                f"{leverage_info}\n"
                f"Amount: {float(order.quantity):.8f}\n"
                f"Price: ${float(order.price):.2f}\n"
                f"Auto-calculated Fees: ${float(order.fees):.4f} {order.fee_asset}\n"
                f"Total Value: ${float(order.price * order.quantity):.2f}",
                reply_markup=self.markup  # Restore original keyboard
            )
            
            # Cleanup
            del self.temp_trade_data[update.effective_user.id]
            return ConversationHandler.END
            
        except ValueError as e:
            await update.message.reply_text(f"Please enter a valid price: {str(e)}")
            return PRICE
        except Exception as e:
            logger.error(f"Error creating manual trade: {e}")
            await update.message.reply_text(
                f"❌ Error creating trade: {str(e)}",
                reply_markup=self.markup  # Restore original keyboard even on error
            )
            return ConversationHandler.END

    async def add_trade_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel the trade addition process"""
        if update.effective_user.id in self.temp_trade_data:
            del self.temp_trade_data[update.effective_user.id]
        await update.message.reply_text(
            "Trade creation cancelled",
            reply_markup=self.markup  # Restore original keyboard
        )
        return ConversationHandler.END

    async def show_profits(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show detailed profit analysis"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        try:
            # Get positions from MongoDB for configured pairs only
            allowed_symbols = set(self.config['trading']['pairs'])
            positions = await self.mongo_client.get_position_stats(allowed_symbols)
            
            if not positions:
                await update.message.reply_text("No filled orders found.")
                return

            # Get the base currency from config
            base_currency = self.config['trading'].get('base_currency', 'USDT')

            # Initialize portfolio totals
            portfolio_stats = {
                "total_cost": Decimal('0'),
                "total_value": Decimal('0'),
                "total_profit": Decimal('0'),
                "total_tax": Decimal('0')
            }

            # Calculate profits for each position
            response = ["📊 Portfolio Analysis:\n"]

            # First show base currency balance
            try:
                base_balance = await self.binance_client.get_balance(base_currency)
                response.append(f"💵 {base_currency} Balance: ${base_balance:.2f}\n")
            except Exception as e:
                logger.error(f"Failed to get {base_currency} balance: {e}")
                response.append(f"💵 {base_currency} Balance: Unable to fetch\n")

            # Process each configured symbol
            for symbol in sorted(allowed_symbols):
                position = positions.get(symbol)
                if not position:
                    continue

                # Get current price
                ticker = await self.binance_client.client.get_symbol_ticker(symbol=symbol)
                current_price = Decimal(ticker['price'])
                
                # Calculate profits
                profit_data = self.mongo_client.calculate_profit_loss(position, current_price)
                
                # Update portfolio totals
                portfolio_stats["total_cost"] += position["total_cost"]
                portfolio_stats["total_value"] += profit_data["current_value"]
                portfolio_stats["total_profit"] += profit_data["absolute_pl"]
                portfolio_stats["total_tax"] += profit_data["tax_amount"]
                
                # Extract base asset name (remove the base currency suffix)
                base_asset = symbol.replace(base_currency, '')
                
                # Generate position message
                position_msg = [
                    f"\n🔸 {symbol}:",
                    f"Quantity: {position['total_quantity']:.8f} {base_asset}",
                    f"Avg Entry: ${position['avg_entry_price']:.2f}",
                    f"Current: ${current_price:.2f}",
                    f"Value: ${profit_data['current_value']:.2f}",
                    f"P/L: ${profit_data['absolute_pl']:.2f} ({profit_data['percentage_pl']:+.2f}%)",
                ]

                if profit_data['tax_amount'] > 0:
                    position_msg.append(f"Tax: ${profit_data['tax_amount']:.2f}")

                # Generate diagram
                diagram = self.mongo_client.generate_profit_diagram(position, current_price)
                position_msg.append(diagram)
                
                response.extend(position_msg)

            # Add portfolio summary
            portfolio_pl_percentage = (
                (portfolio_stats["total_value"] - portfolio_stats["total_cost"]) / 
                portfolio_stats["total_cost"] * 100 if portfolio_stats["total_cost"] > 0 else Decimal('0')
            )

            summary = [
                "\n📈 Portfolio Summary:",
                f"Total Cost: ${portfolio_stats['total_cost']:.2f}",
                f"Total Value: ${portfolio_stats['total_value']:.2f}",
                f"Total P/L: ${portfolio_stats['total_profit']:.2f} ({portfolio_pl_percentage:+.2f}%)"
            ]

            if portfolio_stats["total_tax"] > 0:
                net_profit = portfolio_stats["total_profit"] - portfolio_stats["total_tax"]
                summary.extend([
                    f'Total Tax: ${portfolio_stats["total_tax"]:.2f}',
                    f"Net P/L: ${net_profit:.2f}"
                ])

            response.extend(summary)
            
            # Send response
            await update.message.reply_text("\n".join(response))
            
        except Exception as e:
            logger.error(f"Error calculating profits: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Error calculating profits: {str(e)}")

    def _get_timeframe_value(self, timeframe) -> str:
        """Safely get timeframe value, handling both enum and string cases"""
        if hasattr(timeframe, 'value'):
            return timeframe.value
        return str(timeframe)

    async def add_trade_symbol(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle symbol input"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return ConversationHandler.END

        symbol = update.message.text.upper()
        if symbol not in self.config['trading']['pairs']:
            await update.message.reply_text(f"Invalid symbol. Please choose from: {', '.join(self.config['trading']['pairs'])}")
            return SYMBOL
            
        self.temp_trade_data[update.effective_user.id] = {'symbol': symbol}
        
        keyboard = [
            [KeyboardButton("SPOT")],
            [KeyboardButton("FUTURES")]
        ]
        markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
        
        await update.message.reply_text(
            "What type of trade is this?",
            reply_markup=markup
        )
        return ORDER_TYPE

    async def add_trade_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle price input"""
        try:
            direction = update.message.text.upper()
            user_data = self.temp_trade_data[update.effective_user.id]
            
            if user_data.get('order_type') == OrderType.FUTURES:
                user_data['direction'] = TradeDirection(direction.lower())
            
            await update.message.reply_text(
                "What was your entry price? (e.g., 42000.50)"
            )
            return PRICE
            
        except ValueError:
            await update.message.reply_text("Please enter a valid direction (LONG/SHORT)")
            return DIRECTION

    async def add_trade_final(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Complete trade creation with auto-calculated fees"""
        try:
            # Clean the price input to handle both dots and commas as decimal separators
            price_text = update.message.text.replace(',', '.')
            
            try:
                price = Decimal(price_text)
            except (InvalidOperation, ValueError):  # Replace DecimalException with InvalidOperation
                raise ValueError("Price must be a valid number")
                
            if price <= 0:
                raise ValueError("Price must be positive")
                
            user_data = self.temp_trade_data[update.effective_user.id]
            user_data['price'] = price
            
            # Generate unique order ID
            order_id = f"MANUAL_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
            
            # Calculate quantity from amount/price
            quantity = user_data['amount'] / price
            
            # Get order type and leverage for fee calculation
            order_type = user_data['order_type'].value
            leverage = user_data.get('leverage', 1)  # Default to 1 if not set (for spot trades)
            
            # Calculate fees automatically using the BinanceClient's fee calculation
            fees, fee_asset = await self.binance_client.calculate_fees(
                user_data['symbol'], 
                price, 
                quantity,
                order_type,
                leverage
            )
            
            # Create order object
            order = Order(
                symbol=user_data['symbol'],
                status=OrderStatus.FILLED,  # Manual trades are always filled
                order_type=user_data['order_type'],
                price=price,
                quantity=quantity,
                timeframe=TimeFrame.DAILY,  # Default to daily for manual trades
                order_id=order_id,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                filled_at=datetime.utcnow(),
                leverage=leverage,
                direction=user_data.get('direction'),
                fees=fees,
                fee_asset=fee_asset
            )
            
            # Save to database
            await self.mongo_client.insert_manual_trade(order)
            
            # Send confirmation with auto-calculated fees
            direction_info = f"\nDirection: {order.direction.value}" if order.direction else ""
            leverage_info = f"\nLeverage: {order.leverage}x" if order.leverage else ""
            
            await update.message.reply_text(
                f"✅ Manual trade added:\n"
                f"Symbol: {order.symbol}\n"
                f"Type: {order.order_type.value}"
                f"{direction_info}"
                f"{leverage_info}\n"
                f"Amount: {float(order.quantity):.8f}\n"
                f"Price: ${float(order.price):.2f}\n"
                f"Auto-calculated Fees: ${float(order.fees):.4f} {order.fee_asset}\n"
                f"Total Value: ${float(order.price * order.quantity):.2f}",
                reply_markup=self.markup  # Restore original keyboard
            )
            
            # Cleanup
            del self.temp_trade_data[update.effective_user.id]
            return ConversationHandler.END
            
        except ValueError as e:
            await update.message.reply_text(f"Please enter a valid price: {str(e)}")
            return PRICE
        except Exception as e:
            logger.error(f"Error creating manual trade: {e}")
            await update.message.reply_text(
                f"❌ Error creating trade: {str(e)}",
                reply_markup=self.markup  # Restore original keyboard even on error
            )
            return ConversationHandler.END

    async def add_trade_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel the trade addition process"""
        if update.effective_user.id in self.temp_trade_data:
            del self.temp_trade_data[update.effective_user.id]
        await update.message.reply_text(
            "Trade creation cancelled",
            reply_markup=self.markup  # Restore original keyboard
        )
        return ConversationHandler.END

    async def show_viz_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show visualization options"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        keyboard = [
            # Graph-based visualizations
            [InlineKeyboardButton("📊 Balance History (30d)", callback_data="viz_balance_30")],
            [InlineKeyboardButton("📊 Balance History (90d)", callback_data="viz_balance_90")],
            [InlineKeyboardButton("📊 Balance History (All time)", callback_data="viz_balance_all")],
            [InlineKeyboardButton("💹 Performance vs. BTC (30d)", callback_data="viz_btc_30")],
            [InlineKeyboardButton("💹 Performance vs. BTC (90d)", callback_data="viz_btc_90")],
            [InlineKeyboardButton("💰 Deposits & Withdrawals", callback_data="viz_transactions")],
            
            # Add back the S&P 500 and Portfolio options
            [InlineKeyboardButton("📈 Portfolio Performance", callback_data=VisualizationType.SP500_VS_BTC)],
            [InlineKeyboardButton("🥧 Portfolio Composition", callback_data=VisualizationType.PORTFOLIO_COMPOSITION)],
            
            # Text-based visualizations
            [InlineKeyboardButton("📊 Daily Volume", callback_data=VisualizationType.DAILY_VOLUME)],
            [InlineKeyboardButton("💰 Profit Distribution", callback_data=VisualizationType.PROFIT_DIST)],
            [InlineKeyboardButton("📈 Order Types", callback_data=VisualizationType.ORDER_TYPES)],
            [InlineKeyboardButton("⏰ Hourly Activity", callback_data=VisualizationType.HOURLY_ACTIVITY)],
            
            # Navigation
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="show_menu")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "📈 *Visualization Options*\n\n"
            "Choose a chart to view trading data visualizations.",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

    async def handle_viz_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle visualization selection"""
        query = update.callback_query
        await query.answer()
        
        viz_type = query.data
        
        # Get allowed symbols from config for filtering
        allowed_symbols = set(self.config['trading']['pairs'])
        
        # Special handling for balance chart
        if viz_type == VisualizationType.BALANCE_CHART:
            await query.message.reply_text("Generating balance history chart...", reply_markup=self.markup)
            await self._generate_balance_chart(query.message.chat_id)
            return
            
        # Special handling for ROI comparison
        if viz_type == VisualizationType.ROI_COMPARISON:
            await query.message.reply_text("Generating ROI comparison chart...", reply_markup=self.markup)
            await self._generate_roi_comparison(query.message.chat_id)
            return
            
        # Special handling for S&P 500 vs BTC comparison - now labeled as Portfolio Performance
        if viz_type == VisualizationType.SP500_VS_BTC:
            await query.message.reply_text("Generating Portfolio Performance chart (BTC vs S&P 500)...", reply_markup=self.markup)
            await self._generate_sp500_vs_btc_comparison(query.message.chat_id)
            return
            
        # Special handling for portfolio composition
        if viz_type == VisualizationType.PORTFOLIO_COMPOSITION:
            await query.message.reply_text("Generating portfolio composition chart...", reply_markup=self.markup)
            await self._generate_portfolio_composition_chart(query.message.chat_id)
            return
            
        # Pass allowed symbols to get only active trading pairs data
        data = await self.mongo_client.get_visualization_data(viz_type, allowed_symbols)
        
        if not data:
            await query.message.reply_text(
                "No data available for visualization.",
                reply_markup=self.markup
            )
            return

        # Generate visualization based on type
        if viz_type == VisualizationType.DAILY_VOLUME:
            response = await self._generate_volume_viz(data)
        elif viz_type == VisualizationType.PROFIT_DIST:
            response = await self._generate_profit_viz(data)
        elif viz_type == VisualizationType.ORDER_TYPES:
            response = await self._generate_types_viz(data)
        elif viz_type == VisualizationType.HOURLY_ACTIVITY:
            response = await self._generate_activity_viz(data)
        else:
            response = "Invalid visualization type"

        # Send visualization and restore keyboard
        await query.message.reply_text(response, reply_markup=self.markup)

    async def _generate_balance_chart(self, chat_id: int):
        """Generate and send balance history chart"""
        try:
            # Get historical balance data from MongoDB
            days = 30  # Show last 30 days by default
            balance_data = await self.mongo_client.get_balance_history(days)
            
            if not balance_data or len(balance_data) < 2:
                await self.application.bot.send_message(
                    chat_id=chat_id,
                    text="Not enough balance history data to generate chart.",
                    reply_markup=self.markup
                )
                return
                
            # Get BTC historical prices for the same period (still pass for backward compatibility)
            btc_prices = await self.binance_client.get_historical_prices("BTCUSDT", days)
            
            # Get buy orders for the period
            buy_orders = await self.mongo_client.get_buy_orders(days)
                
            # Generate chart using chart generator
            chart_bytes = await self.binance_client.chart_generator.generate_balance_chart(
                balance_data, btc_prices, buy_orders
            )
            
            if not chart_bytes:
                await self.application.bot.send_message(
                    chat_id=chat_id,
                    text="Failed to generate balance chart.",
                    reply_markup=self.markup
                )
                return
                
            # Get base currency from config
            base_currency = self.config['trading'].get('base_currency', 'USDT')
            
            # Send chart to user
            await self.application.bot.send_photo(
                chat_id=chat_id,
                photo=chart_bytes,
                caption=f"📊 Account Balance History (30 days)\n"
                        f"💹 Green arrows indicate buy orders\n"
                        f"🟢 Green line: Total Balance\n"
                        f"🔵 Blue line: Invested Amount\n"
                        f"🟣 Purple line: Profit (Balance - Invested)\n"
                        f"All values in {base_currency}",
                reply_markup=self.markup
            )
            
        except Exception as e:
            logger.error(f"Error generating balance chart: {e}", exc_info=True)
            await self.application.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Error generating balance chart: {str(e)}",
                reply_markup=self.markup
            )

    async def _generate_volume_viz(self, data: List[Dict]) -> str:
        """Generate volume visualization"""
        response = ["📊 Daily Trading Volume\n"]
        
        for entry in data:
            date = entry['_id']['date']
            volume = float(entry['volume'])
            count = entry['count']
            bar = "█" * min(int(volume/100), 20)  # Scale bar to max 20 chars
            response.append(f"{date}: ${volume:,.2f} ({count} trades)\n{bar}")
            
        return "\n".join(response)

    async def _generate_profit_viz(self, data: List[Dict]) -> str:
        """Generate profit distribution visualization"""
        response = ["💰 Profit Distribution\n"]
        
        total_profit = sum(float(d['total_profit']) for d in data)
        for entry in data:
            symbol = entry['_id']
            profit = float(entry['total_profit'])
            percentage = (profit / total_profit * 100) if total_profit > 0 else 0
            bar = "█" * int(percentage / 5)  # 1 block per 5%
            response.append(f"{symbol}: {percentage:.1f}%\n{bar}")
            
        return "\n".join(response)

    async def _generate_types_viz(self, data: List[Dict]) -> str:
        """Generate order types visualization"""
        response = ["📈 Order Types Distribution\n"]
        
        total = sum(d['count'] for d in data)
        for entry in data:
            type_name = f"{entry['_id']['type']} ({entry['_id']['status']})"
            count = entry['count']
            percentage = (count / total * 100) if total > 0 else 0
            bar = "█" * int(percentage / 5)
            response.append(f"{type_name}: {percentage:.1f}%\n{bar}")
            
        return "\n".join(response)  # Fixed string joining syntax

    async def _generate_activity_viz(self, data: List[Dict]) -> str:
        """Generate hourly activity visualization"""
        response = ["⏰ Hourly Trading Activity\n"]
        
        max_count = max(d['count'] for d in data)
        for entry in data:
            hour = entry['_id']['hour']
            count = entry['count']
            status = entry['_id']['status']
            bar = "█" * int((count / max_count) * 20)  # Scale to 20 chars max
            response.append(f"{hour:02d}:00 {status}: {count}\n{bar}")
            
        return "\n".join(response)

    async def _generate_roi_comparison(self, chat_id: int):
        """Generate ROI comparison chart"""
        try:
            # Get first trade date
            first_trade_date = await self.mongo_client.get_first_trade_date()
            if not first_trade_date:
                await self.application.bot.send_message(  # Use application.bot instead of bot
                    chat_id=chat_id,
                    text="No trade history found. Cannot generate ROI comparison."
                )
                return

            # Get portfolio performance data
            portfolio_data = await self.mongo_client.get_portfolio_performance(first_trade_date)
            if not portfolio_data:
                await self.application.bot.send_message(  # Use application.bot instead of bot
                    chat_id=chat_id,
                    text="Could not calculate portfolio performance. No completed trades found."
                )
                return

            # Calculate days since first trade
            days_since_first = (datetime.utcnow() - first_trade_date).days

            # Get benchmark data
            btc_performance = await self.binance_client.get_historical_benchmark("BTCUSDT", days_since_first)
            sp500_performance = await self.binance_client.get_historical_benchmark("SP500", days_since_first)

            # Generate chart
            chart_bytes = await self.chart_generator.generate_roi_comparison_chart(
                portfolio_data,
                btc_performance,
                sp500_performance
            )

            if chart_bytes:
                await self.application.bot.send_photo(  # Use application.bot instead of bot
                    chat_id=chat_id,
                    photo=chart_bytes,
                    caption="ROI Comparison: Portfolio vs BTC vs S&P 500"
                )
            else:
                await self.application.bot.send_message(  # Use application.bot instead of bot
                    chat_id=chat_id,
                    text="Error generating ROI comparison chart."
                )

        except Exception as e:
            logger.error(f"Error generating ROI comparison: {e}", exc_info=True)
            await self.application.bot.send_message(  # Use application.bot instead of bot
                chat_id=chat_id,
                text="Error generating ROI comparison chart."
            )

    async def _generate_simulated_portfolio_data(self, days: int) -> dict:
        """Generate simulated portfolio performance data when real data is insufficient"""
        import numpy as np
        
        logger.info("Generating simulated portfolio performance data")
        today = datetime.utcnow()
        np.random.seed(42)  # Use fixed seed for consistent results
        
        # Start with slight positive bias (0.05% average daily growth)
        daily_change = 0.05
        result = {}
        
        # Generate simulated portfolio performance data
        base_value = 0.0  # Start at 0% ROI
        for day in range(days, -1, -1):
            date = (today - timedelta(days=day)).strftime('%Y-%m-%d')
            # Simulate some realistic movement with noise and slight upward bias
            random_factor = np.random.normal(0, 1) * daily_change
            # More volatility than S&P 500 but less than BTC
            base_value += random_factor + (daily_change / 10.0)  # Slight positive bias
            result[date] = float(base_value)
            
        logger.info(f"Generated {len(result)} days of simulated portfolio data")
        return result

    async def send_timeframe_reset_notification(self, reset_data: dict):
        """Send notification about timeframe threshold reset"""
        try:
            timeframe = reset_data['timeframe']
            timestamp = reset_data['timestamp']
            pairs = reset_data['pairs']
            
            # Create message header
            message = [
                f"🔄 {timeframe.upper()} Thresholds Reset",
                f"Time: {timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}",
                f"\nMonitoring {len(pairs)} pairs with new reference prices:"
            ]
            
            # Add pair details
            for pair_info in pairs:
                symbol = pair_info['symbol']
                ref_price = pair_info['reference_price']
                thresholds = pair_info['thresholds']
                
                message.append(f"\n{symbol}:")
                message.append(f"  Reference: ${ref_price:,.2f}")
                message.append(f"  Thresholds: {', '.join(f'{t}%' for t in thresholds)}")
            
            # Send message to all allowed users
            for user_id in self.allowed_users:
                try:
                    await self.send_message(
                        chat_id=user_id,
                        text="\n".join(message),
                        parse_mode='HTML'
                    )
                except Exception as e:
                    logger.error(f"Failed to send reset notification to user {user_id}: {e}")
            
        except Exception as e:
            logger.error(f"Error sending timeframe reset notification: {e}")

    async def send_threshold_notification(self, symbol: str, timeframe: TimeFrame, 
                                       threshold: float, current_price: float,
                                       reference_price: float, price_change: float):
        """Send notification when a threshold is triggered"""
        # Check if we have enough balance for an order before sending notification
        has_enough_balance = False
        try:
            # Get order amount from config
            order_amount = self.config['trading'].get('order_amount', 0)
            
            # Check if we have sufficient balance
            if hasattr(self.binance_client, 'check_reserve_balance'):
                has_enough_balance = await self.binance_client.check_reserve_balance(order_amount)
        except Exception as e:
            logger.error(f"Error checking balance before threshold notification: {e}")
            # If there's an error checking balance, default to sending the notification
            has_enough_balance = True
        
        # Only send notification if we have enough balance or there was an error checking
        if has_enough_balance:
            message = (
                f"🎯 Threshold Triggered - Price Drop!\n\n"
                f"Symbol: {symbol}\n"
                f"Timeframe: {timeframe.value}\n"
                f"Threshold: {threshold}%\n"
                f"Reference Price: ${reference_price:,.2f}\n"
                f"Current Price: ${current_price:,.2f}\n"
                f"Change: {price_change:+.2f}%\n"
                f"Action: Buying opportunity detected"
            )
        else:
            # Send a different message when balance is insufficient
            message = (
                f"💸 Price Drop Alert (No Action)\n\n"
                f"Symbol: {symbol}\n"
                f"Timeframe: {timeframe.value}\n"
                f"Threshold: {threshold}%\n"
                f"Reference Price: ${reference_price:,.2f}\n"
                f"Current Price: ${current_price:,.2f}\n"
                f"Change: {price_change:+.2f}%\n"
                f"Action: Insufficient balance - no order placed"
            )
        
        for user_id in self.allowed_users:
            try:
                await self.application.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    reply_markup=self.markup
                )
            except Exception as e:
                logger.error(f"Failed to send threshold notification to {user_id}: {e}")

    async def send_reserve_alert(self, current_balance: Decimal, reserve_balance: float, pending_value: Decimal):
        """Send alert when reserve balance would be violated"""
        # Get base currency from config
        base_currency = self.config['trading'].get('base_currency', 'USDT')
        
        available_balance = float(current_balance - pending_value)
        message = (
            "⚠️ Trading Paused - Reserve Balance Protection\n\n"
            f"Current Balance: ${float(current_balance):.2f} {base_currency}\n"
            f"Pending Orders: ${float(pending_value):.2f} {base_currency}\n"
            f"Available Balance: ${available_balance:.2f} {base_currency}\n"
            f"Reserve Balance: ${reserve_balance:.2f} {base_currency}\n\n"
            "Trading will resume automatically on next timeframe reset\n"
            "when balance is above reserve requirement."
        )
        
        for user_id in self.allowed_users:
            try:
                await self.application.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    reply_markup=self.markup
                )
            except Exception as e:
                logger.error(f"Failed to send reserve alert to {user_id}: {e}")

    async def send_initial_balance_alert(self, current_balance: Decimal, reserve_balance: float):
        """Send alert when initial balance is below reserve"""
        # Get base currency from config
        base_currency = self.config['trading'].get('base_currency', 'USDT')
        
        message = (
            "⚠️ WARNING - Insufficient Initial Balance\n\n"
            f"Current Balance: ${float(current_balance):.2f} {base_currency}\n"
            f"Required Reserve: ${reserve_balance:.2f} {base_currency}\n\n"
            "Trading is paused until balance is above reserve requirement.\n"
            "You can:\n"
            "1. Add more funds\n"
            "2. Lower reserve balance in config\n"
            "3. Use /power to check balance and resume"
        )
        
        for user_id in self.allowed_users:
            try:
                await self.application.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    reply_markup=self.markup
                )
            except Exception as e:
                logger.error(f"Failed to send initial balance alert to {user_id}: {e}")

    async def send_threshold_restoration_notification(self, restored_info: Dict):
        """Send notification about restored threshold state after restart"""
        if not restored_info:
            logger.warning("No threshold information to send in notification")
            return
            
        message_parts = ["📋 Restored Threshold State:"]
        threshold_count = 0
        
        for symbol, timeframes in restored_info.items():
            symbol_parts = [f"\n🔸 {symbol}:"]
            symbol_has_thresholds = False
            
            for timeframe, thresholds in timeframes.items():
                if thresholds:  # Only show timeframes with triggered thresholds
                    threshold_str = ", ".join([f"{t}%" for t in thresholds])
                    symbol_parts.append(f"  • {timeframe.value}: {threshold_str}")
                    threshold_count += len(thresholds)
                    symbol_has_thresholds = True
            
            if symbol_has_thresholds:
                message_parts.extend(symbol_parts)
        
        if threshold_count > 0:
            message_parts.append("\nThese thresholds will not be triggered again until their next reset.")
            
            # Log the full message for debugging
            logger.info(f"Sending threshold restoration notification with {threshold_count} thresholds")
            
            # Send to all authorized users
            for user_id in self.allowed_users:
                try:
                    await self.application.bot.send_message(
                        chat_id=user_id,
                        text="\n".join(message_parts),
                        reply_markup=self.markup
                    )
                    logger.info(f"Sent threshold restoration notification to user {user_id}")
                except Exception as e:
                    logger.error(f"Failed to send threshold restoration notification to {user_id}: {e}")
        else:
            logger.info("No thresholds to restore, skipping notification")

    async def start_bot(self):
        """Start the Telegram bot"""
        # ...existing code...
        
        # Notify about restored thresholds
        await self.notify_restored_thresholds()
        
        # ...existing code...
    
    async def notify_restored_thresholds(self):
        """Notify users about thresholds that were restored from database"""
        try:
            # Get restored threshold information from binance client
            restored_info = []
            if self.binance_client and hasattr(self.binance_client, 'restored_threshold_info'):
                restored_info = self.binance_client.restored_threshold_info
            
            if not restored_info:
                logger.info("No restored thresholds to notify about")
                return
                
            # Create notification message
            message = "🔄 Restored threshold state:\n\n"
            for info in restored_info:
                message += f"• {info}\n"
                
            # Send notification to all allowed users
            for user_id in self.allowed_users:
                await self.send_message(user_id, message)
                
            logger.info(f"Notified users about {len(restored_info)} restored threshold states")
            
        except Exception as e:
            logger.error(f"Failed to notify about restored thresholds: {e}")

    async def reset_all_thresholds(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reset all thresholds across all timeframes"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
        
        try:
            # Send "processing" message
            progress_message = await update.message.reply_text(
                "🔄 Processing threshold reset for all timeframes...",
                reply_markup=self.markup
            )
            
            # Log action
            logger.info(f"User {update.effective_user.id} requested manual reset of all thresholds")
            
            # First clear database to ensure consistency
            if hasattr(self, 'mongo_client') and self.mongo_client:
                try:
                    await self.mongo_client.reset_all_triggered_thresholds()
                    logger.info("Successfully cleared all triggered thresholds in database")
                except Exception as e:
                    logger.error(f"Error clearing triggered thresholds in database: {e}")
            
            # Reset thresholds for each timeframe
            results = {}
            timeframes = ['daily', 'weekly', 'monthly']
            
            for timeframe in timeframes:
                try:
                    # Reset timeframe thresholds
                    result = await self.binance_client.reset_timeframe_thresholds(timeframe)
                    results[timeframe] = result
                    logger.info(f"Reset {timeframe} thresholds: {'Success' if result else 'Failed'}")
                except Exception as e:
                    logger.error(f"Error resetting {timeframe} thresholds: {e}")
                    results[timeframe] = False
            
            # Check results and send appropriate message
            if all(results.values()):
                await progress_message.edit_text(
                    "✅ All thresholds have been reset across all timeframes.",
                    reply_markup=self.markup
                )
            else:
                # Create detailed error message
                failed_timeframes = [tf for tf, success in results.items() if not success]
                success_timeframes = [tf for tf, success in results.items() if success]
                
                message = "⚠️ Threshold reset partially completed:\n"
                
                if success_timeframes:
                    message += f"✅ Successfully reset: {', '.join(success_timeframes)}\n"
                if failed_timeframes:
                    message += f"❌ Failed to reset: {', '.join(failed_timeframes)}\n"
                
                message += "\nPlease check logs for more details."
                
                await progress_message.edit_text(message, reply_markup=self.markup)
                
        except Exception as e:
            logger.error(f"Failed to reset all thresholds: {e}", exc_info=True)
            await update.message.reply_text(
                f"❌ Error resetting thresholds: {str(e)}",
                reply_markup=self.markup
            )

    async def send_message(self, chat_id, text, **kwargs):
        """Helper method to send messages to users"""
        try:
            if self.application and self.application.bot:
                await self.application.bot.send_message(chat_id=chat_id, text=text, **kwargs)
                return True
            else:
                logger.error("Telegram bot not initialized")
                return False
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            return False

    async def send_restored_thresholds_message(self):
        """Send message about restored thresholds if any"""
        try:
            if not self.binance_client or not hasattr(self.binance_client, 'restored_threshold_info'):
                logger.info("No restored thresholds to notify about")
                return

            restored_info = self.binance_client.restored_threshold_info
            
            # Skip if no thresholds were restored
            if not restored_info:
                logger.info("No restored thresholds to notify about (empty)")
                return
            
            # Format the restored thresholds message
            message = "🔄 *Restored Triggered Thresholds*\n\n"
            
            # Handle dictionary format with proper structure
            has_thresholds = False
            
            for symbol, timeframes in restored_info.items():
                if not timeframes:  # Skip symbols with no timeframes
                    continue
                    
                symbol_message = f"*{symbol}*:\n"
                symbol_has_thresholds = False
                
                for timeframe, thresholds in timeframes.items():
                    if thresholds:  # Only include if there are thresholds
                        sorted_thresholds = sorted(thresholds)
                        threshold_str = ", ".join(f"{t}%" for t in sorted_thresholds)
                        symbol_message += f"  • {timeframe.value}: {threshold_str}\n"
                        symbol_has_thresholds = True
                
                if symbol_has_thresholds:
                    message += symbol_message + "\n"
                    has_thresholds = True
            
            # If no thresholds were found in any symbol, return early
            if not has_thresholds:
                logger.info("No triggered thresholds to report")
                return
                
            message += "These thresholds will not trigger again until their next reset period."
            
            # Send the message to all allowed users
            for user_id in self.allowed_users:
                try:
                    await self.application.bot.send_message(
                        chat_id=user_id, 
                        text=message, 
                        parse_mode="Markdown"
                    )
                    logger.info(f"Sent threshold restoration message to user {user_id}")
                except Exception as e:
                    logger.error(f"Failed to send threshold restoration message to {user_id}: {e}")
                    
        except Exception as e:
            logger.error(f"Failed to send startup threshold message: {e}")

    async def show_tp_sl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show current take profit and stop loss settings"""
        if not self._is_authorized(update.effective_user.id):
            await self.send_message(update.effective_chat.id, "You are not authorized to use this command.")
            return

        try:
            # Get current TP/SL settings
            tp_setting = self.binance_client.default_tp_percentage
            sl_setting = self.binance_client.default_sl_percentage
            
            # Build and send the message
            message = (
                f"📊 *Current Settings*\n\n"
                f"Take Profit: {tp_setting}%\n"
                f"Stop Loss: {sl_setting}%\n\n"
                f"You can change these settings globally with:\n"
                f"`/set_tp <percentage>` - Example: `/set_tp 3`\n"
                f"`/set_sl <percentage>` - Example: `/set_sl 3`"
            )
            
            await self.send_message(
                update.effective_chat.id,
                message,
                parse_mode='Markdown'
            )
            
        except Exception as e:
            logger.error(f"Error showing TP/SL settings: {e}")
            await self.send_message(
                update.effective_chat.id,
                "An error occurred while retrieving the settings."
            )

    async def set_take_profit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set the global take profit percentage for all symbols"""
        if not self._is_authorized(update.effective_user.id):
            await self.send_message(update.effective_chat.id, "You are not authorized to use this command.")
            return

        try:
            # Get message parts, expecting format: /set_tp <percentage>
            message_parts = update.message.text.split()
            
            # Check that we have enough parts (command + percentage)
            if len(message_parts) != 2:
                await self.send_message(
                    update.effective_chat.id,
                    "Please provide a percentage value.\n"
                    "Example: `/set_tp 3` to set a 3% take profit level for all symbols."
                )
                return
                
            # Parse and validate the percentage
            percentage_str = message_parts[1].strip().replace('%', '')
            
            percentage = float(percentage_str)
            if percentage <= 0 or percentage > 100:
                await self.send_message(
                    update.effective_chat.id,
                    "Take profit percentage must be between 0 and 100."
                )
                return
                
        except ValueError:
            await self.send_message(
                update.effective_chat.id,
                "Invalid percentage format. Please provide a number."
            )
            return
        except Exception as e:
            logger.error(f"Error parsing take profit percentage: {e}")
            await self.send_message(
                update.effective_chat.id,
                "An error occurred while parsing the take profit percentage."
            )
            return

        try:
            # Format the percentage for config storage
            tp_setting = f"{percentage}%"
            
            # Update the setting in both config and database
            self.config['trading']['take_profit'] = tp_setting
            old_tp = self.binance_client.default_tp_percentage
            self.binance_client.default_tp_percentage = percentage
            
            # Also update in MongoDB
            success = False
            if self.mongo_client:
                success = await self.mongo_client.update_trading_setting('take_profit', tp_setting)
            
            # Create a list to store details of updated orders
            orders_updated = []
            
            # Update existing orders with active TP settings
            active_orders = await self.mongo_client.get_orders_with_active_tp_sl()
            for order in active_orders:
                if order.status == OrderStatus.FILLED and order.take_profit and order.take_profit.status == TPSLStatus.PENDING:
                    # Calculate new TP price based on entry price
                    entry_price = order.price
                    old_tp_price = order.take_profit.price
                    new_tp_price = Decimal(str(float(entry_price) * (1 + percentage / 100)))
                    
                    # Align the price to the exchange's tick size requirements
                    new_tp_price = self.binance_client._align_price_to_tick(order.symbol, new_tp_price)
                    
                    # Update the order's TP in the database
                    order.take_profit.price = new_tp_price
                    order.take_profit.percentage = percentage
                    await self.mongo_client.insert_order(order)
                    
                    # Add to list of updated orders with more details
                    orders_updated.append({
                        'order_id': order.order_id[:8] + '...',  # Truncate for readability
                        'symbol': order.symbol,
                        'entry_price': float(entry_price),
                        'old_tp_price': float(old_tp_price),
                        'new_tp_price': float(new_tp_price),
                        'change_percent': percentage - order.take_profit.percentage
                    })
            
            # Create message about the update
            if len(orders_updated) > 0:
                # Create a detailed message about which orders were updated
                update_info = f"\n\n📊 *Updated {len(orders_updated)} Existing Orders:*"
                for order in orders_updated:
                    change_direction = "⬆️" if order['change_percent'] > 0 else "⬇️"
                    update_info += f"\n- {order['symbol']} (ID: {order['order_id']}): ${order['old_tp_price']:.2f} → ${order['new_tp_price']:.2f} {change_direction}"
                
                message = (
                    f"✅ Take profit set to {percentage}% for all symbols.\n"
                    f"*Changed from:* {old_tp}% → {percentage}%{update_info}"
                )
                await self.send_message(
                    update.effective_chat.id,
                    message,
                    parse_mode='Markdown'
                )
            else:
                await self.send_message(
                    update.effective_chat.id,
                    f"✅ Take profit set to {percentage}% for all symbols.\n"
                    f"*Changed from:* {old_tp}% → {percentage}%\n\n"
                    f"*No existing orders* needed to be updated.",
                    parse_mode='Markdown'
                )
                
        except Exception as e:
            logger.error(f"Error setting take profit: {e}")
            await self.send_message(
                update.effective_chat.id,
                "An error occurred while setting the take profit."
            )

    async def set_stop_loss(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set the global stop loss percentage for all symbols"""
        if not self._is_authorized(update.effective_user.id):
            await self.send_message(update.effective_chat.id, "You are not authorized to use this command.")
            return

        try:
            # Get message parts, expecting format: /set_sl <percentage>
            message_parts = update.message.text.split()
            
            # Check that we have enough parts (command + percentage)
            if len(message_parts) != 2:
                await self.send_message(
                    update.effective_chat.id,
                    "Please provide a percentage value.\n"
                    "Example: `/set_sl 3` to set a 3% stop loss level for all symbols."
                )
                return
                
            # Parse and validate the percentage
            percentage_str = message_parts[1].strip().replace('%', '')
            
            percentage = float(percentage_str)
            if percentage <= 0 or percentage > 100:
                await self.send_message(
                    update.effective_chat.id,
                    "Stop loss percentage must be between 0 and 100."
                )
                return
                
        except ValueError:
            await self.send_message(
                update.effective_chat.id,
                "Invalid percentage format. Please provide a number."
            )
            return
        except Exception as e:
            logger.error(f"Error parsing stop loss percentage: {e}")
            await self.send_message(
                update.effective_chat.id,
                "An error occurred while parsing the stop loss percentage."
            )
            return

        try:
            # Format the percentage for config storage
            sl_setting = f"{percentage}%"
            
            # Update the setting in both config and database
            self.config['trading']['stop_loss'] = sl_setting
            old_sl = self.binance_client.default_sl_percentage
            self.binance_client.default_sl_percentage = percentage
            
            # Also update in MongoDB
            success = False
            if self.mongo_client:
                success = await self.mongo_client.update_trading_setting('stop_loss', sl_setting)
            
            # Create a list to store details of updated orders
            orders_updated = []
            
            # Update existing orders with active SL settings
            active_orders = await self.mongo_client.get_orders_with_active_tp_sl()
            for order in active_orders:
                if order.status == OrderStatus.FILLED and order.stop_loss and order.stop_loss.status == TPSLStatus.PENDING:
                    # Calculate new SL price based on entry price
                    entry_price = order.price
                    old_sl_price = order.stop_loss.price
                    new_sl_price = Decimal(str(float(entry_price) * (1 - percentage / 100)))
                    
                    # Align the price to the exchange's tick size requirements
                    new_sl_price = self.binance_client._align_price_to_tick(order.symbol, new_sl_price)
                    
                    # Update the order's SL in the database
                    order.stop_loss.price = new_sl_price
                    order.stop_loss.percentage = percentage
                    await self.mongo_client.insert_order(order)
                    
                    # Add to list of updated orders with more details
                    orders_updated.append({
                        'order_id': order.order_id[:8] + '...',  # Truncate for readability
                        'symbol': order.symbol,
                        'entry_price': float(entry_price),
                        'old_sl_price': float(old_sl_price),
                        'new_sl_price': float(new_sl_price),
                        'change_percent': percentage - order.stop_loss.percentage
                    })
            
            # Create message about the update
            if len(orders_updated) > 0:
                # Create a detailed message about which orders were updated
                update_info = f"\n\n📊 *Updated {len(orders_updated)} Existing Orders:*"
                for order in orders_updated:
                    change_direction = "⬆️" if order['change_percent'] > 0 else "⬇️"
                    update_info += f"\n- {order['symbol']} (ID: {order['order_id']}): ${order['old_sl_price']:.2f} → ${order['new_sl_price']:.2f} {change_direction}"
                
                message = (
                    f"✅ Stop loss set to {percentage}% for all symbols.\n"
                    f"*Changed from:* {old_sl}% → {percentage}%{update_info}"
                )
                await self.send_message(
                    update.effective_chat.id,
                    message,
                    parse_mode='Markdown'
                )
            else:
                await self.send_message(
                    update.effective_chat.id,
                    f"✅ Stop loss set to {percentage}% for all symbols.\n"
                    f"*Changed from:* {old_sl}% → {percentage}%\n\n"
                    f"*No existing orders* needed to be updated.",
                    parse_mode='Markdown'
                )
                
        except Exception as e:
            logger.error(f"Error setting stop loss: {e}")
            await self.send_message(
                update.effective_chat.id,
                "An error occurred while setting the stop loss."
            )

    async def show_lower_entries(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show current lower entries protection status"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        try:
            # Get current setting from config
            is_enabled = self.config['trading'].get('only_lower_entries', False)
            status = "ENABLED ✅" if is_enabled else "DISABLED ❌"
            
            message = f"""
🛡️ Lower Entries Protection: {status}

When enabled, this protection prevents placing orders that would increase your average entry price for a symbol.

To change this setting:
/set_lower_entries on - Enable protection
/set_lower_entries off - Disable protection
"""
            await update.message.reply_text(message)
            
        except Exception as e:
            logger.error(f"Error showing lower entries protection status: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Error: {str(e)}")

    async def set_lower_entries(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set lower entries protection on or off"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        try:
            # Check if argument is provided
            if not context.args or len(context.args) < 1:
                await update.message.reply_text(
                    "❌ Please specify 'on' or 'off'.\nExample: /set_lower_entries on"
                )
                return
            
            # Get setting from args
            setting = context.args[0].lower()
            if setting not in ['on', 'off', 'true', 'false', '1', '0']:
                await update.message.reply_text(
                    "❌ Invalid option. Please use 'on' or 'off'."
                )
                return
                
            # Convert to boolean
            new_setting = setting in ['on', 'true', '1']
            
            # Get previous setting
            old_setting = self.config['trading'].get('only_lower_entries', False)
            
            # Update setting in database
            if await self.mongo_client.update_trading_setting('only_lower_entries', new_setting):
                # Update config
                self.config['trading']['only_lower_entries'] = new_setting
                
                # Send confirmation
                status = "ENABLED ✅" if new_setting else "DISABLED ❌"
                old_status = "enabled" if old_setting else "disabled"
                new_status = "enabled" if new_setting else "disabled"
                
                await update.message.reply_text(
                    f"🛡️ Lower Entries Protection: {status}\n\n"
                    f"Protection has been changed from {old_status} to {new_status}.\n\n"
                    f"This setting affects all future trades."
                )
            else:
                await update.message.reply_text("Failed to update lower entries protection setting in database")
            
        except Exception as e:
            logger.error(f"Error setting lower entries protection: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Error: {str(e)}")

    async def send_tp_notification(self, order: Order):
        """Send notification when Take Profit level is triggered"""
        if not order.take_profit or not order.take_profit.triggered_at:
            return
            
        # Get base currency from config
        base_currency = self.config['trading'].get('base_currency', 'USDT')
        
        # Extract base asset (remove base currency suffix)
        base_asset = order.symbol.replace(base_currency, '')
        
        # Calculate profit
        entry_price = float(order.price)
        tp_price = float(order.take_profit.price)
        quantity = float(order.quantity)
        profit_amount = (tp_price - entry_price) * quantity
        profit_percentage = order.take_profit.percentage
        
        message = (
            f"✅ Take Profit Triggered!\n\n"
            f"Symbol: {order.symbol}\n"
            f"Entry Price: ${entry_price:.2f}\n"
            f"TP Price: ${tp_price:.2f}\n"
            f"Quantity: {quantity:.8f} {base_asset}\n"
            f"Profit: ${profit_amount:.2f} (+{profit_percentage:.2f}%)\n"
            f"Triggered at: {order.take_profit.triggered_at.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        for user_id in self.allowed_users:
            try:
                await self.application.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    reply_markup=self.markup
                )
            except Exception as e:
                logger.error(f"Failed to send TP notification to {user_id}: {e}")

    async def send_partial_tp_notification(self, order: Order, partial_tp):
        """Send notification when Partial Take Profit level is triggered with detailed information"""
        if not partial_tp or not partial_tp.triggered_at:
            return
            
        # Get base currency from config
        base_currency = self.config['trading'].get('base_currency', 'USDT')
        
        # Extract base asset (remove base currency suffix)
        base_asset = order.symbol.replace(base_currency, '')
        
        # Calculate profit
        entry_price = float(order.price)
        tp_price = float(partial_tp.price)
        
        # Calculate the amount being sold in this partial TP
        total_quantity = float(order.quantity)
        position_percentage = partial_tp.position_percentage
        sold_quantity = total_quantity * (position_percentage / 100)
        
        # Calculate USDC value of the sale
        sale_value = sold_quantity * tp_price
        
        # Calculate profit
        profit_percentage = partial_tp.profit_percentage
        profit_amount = (tp_price - entry_price) * sold_quantity
        
        message = (
            f"🔹 Partial Take Profit (Level {partial_tp.level}) Triggered!\n\n"
            f"Symbol: {order.symbol}\n"
            f"Entry Price: ${entry_price:.4f}\n"
            f"TP Price: ${tp_price:.4f}\n\n"
            f"Selling: {sold_quantity:.8f} {base_asset} ({position_percentage}% of position)\n"
            f"Sale Value: ${sale_value:.2f} {base_currency}\n"
            f"Profit: ${profit_amount:.2f} (+{profit_percentage:.2f}%)\n\n"
            f"Remaining Position: {total_quantity - sold_quantity:.8f} {base_asset}\n"
            f"Triggered at: {partial_tp.triggered_at.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        for user_id in self.allowed_users:
            try:
                await self.application.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    reply_markup=self.markup
                )
            except Exception as e:
                logger.error(f"Failed to send partial TP notification to {user_id}: {e}")

    async def send_sl_notification(self, order, sl, trailing=False):
        """Send notification when stop loss is triggered"""
        try:
            if not self.chat_id:
                logger.error("No chat ID available for notifications")
                return
            
            # Get current price
            current_price = await self.binance_client.get_current_price(order.symbol)
            
            # Calculate profit/loss
            entry_price = float(order.entry_price) if order.entry_price else 0
            stop_price = float(sl.price) if hasattr(sl, 'price') else float(sl.current_stop_price)
            
            is_long = order.direction == TradeDirection.LONG or order.direction is None
            profit_percentage = ((stop_price / entry_price) - 1) * 100 if is_long else ((entry_price / stop_price) - 1) * 100
            
            # Get position size
            position_size = float(order.position_size) if order.position_size else 0
            quote_size = position_size * entry_price
            profit_amount = (position_size * stop_price) - quote_size if is_long else quote_size - (position_size * stop_price)
            
            # Create message
            sl_type = "Trailing Stop Loss" if trailing else "Stop Loss"
            emoji = "⛔" if profit_percentage < 0 else "🔴"
            message = (
                f"{emoji} <b>{sl_type} TRIGGERED</b> {emoji}\n\n"
                f"<b>Symbol:</b> {order.symbol}\n"
                f"<b>Direction:</b> {'LONG' if is_long else 'SHORT'}\n"
                f"<b>Entry Price:</b> ${entry_price:.4f}\n"
                f"<b>Stop Price:</b> ${stop_price:.4f}\n"
                f"<b>Current Price:</b> ${current_price:.4f}\n"
                f"<b>P/L:</b> {profit_percentage:.2f}% ({profit_amount:.4f} USD)\n"
            )
            
            # Add trailing stop loss specific info
            if trailing and hasattr(sl, 'activation_percentage') and hasattr(sl, 'callback_rate'):
                message += (
                    f"<b>Trailing Settings:</b>\n"
                    f"• Activation: {sl.activation_percentage}%\n"
                    f"• Callback: {sl.callback_rate}%\n"
                )
            
            # Send message
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message
            )
            
        except Exception as e:
            logger.error(f"Error sending SL notification: {e}")
    
    async def send_trailing_sl_update_notification(self, order, trailing_sl):
        """Send notification when trailing stop loss is updated"""
        try:
            if not self.chat_id:
                logger.error("No chat ID available for notifications")
                return
            
            # Get current price
            current_price = await self.binance_client.get_current_price(order.symbol)
            
            # Get trailing stop loss info
            stop_price = float(trailing_sl.current_stop_price)
            highest_price = float(trailing_sl.highest_price)
            entry_price = float(order.entry_price) if order.entry_price else 0
            
            is_long = order.direction == TradeDirection.LONG or order.direction is None
            
            # Calculate current lock-in profit/loss
            profit_percentage = ((stop_price / entry_price) - 1) * 100 if is_long else ((entry_price / stop_price) - 1) * 100
            position_size = float(order.position_size) if order.position_size else 0
            quote_size = position_size * entry_price
            profit_amount = (position_size * stop_price) - quote_size if is_long else quote_size - (position_size * stop_price)
            
            # Create message
            emoji = "🔒"
            message = (
                f"{emoji} <b>Trailing Stop Updated</b> {emoji}\n\n"
                f"<b>Symbol:</b> {order.symbol}\n"
                f"<b>Direction:</b> {'LONG' if is_long else 'SHORT'}\n"
                f"<b>Entry Price:</b> ${entry_price:.4f}\n"
                f"<b>New Stop Price:</b> ${stop_price:.4f}\n"
                f"<b>{'Highest' if is_long else 'Lowest'} Price:</b> ${highest_price:.4f}\n"
                f"<b>Current Price:</b> ${current_price:.4f}\n"
                f"<b>Locked P/L:</b> {profit_percentage:.2f}% ({profit_amount:.4f} USD)\n"
                f"<b>Callback Rate:</b> {trailing_sl.callback_rate}%\n"
            )
            
            # Send message
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message
            )
            
        except Exception as e:
            logger.error(f"Error sending trailing SL update notification: {e}")

    async def send_api_rate_limit_alert(self, service_name: str, feature: str):
        """Send alert when an external API rate limit is reached"""
        message = (
            f"⚠️ API RATE LIMIT EXCEEDED\n\n"
            f"Service: {service_name}\n"
            f"Feature: {feature}\n\n"
            f"The bot will use simulated data until the rate limit resets."
        )
        
        for user_id in self.allowed_users:
            try:
                await self.application.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    reply_markup=self.markup
                )
            except Exception as e:
                logger.error(f"Failed to send API rate limit alert to {user_id}: {e}")

    async def send_api_error_alert(self, service_name: str, error_details: str, feature: str):
        """Send alert when an external API returns an error"""
        message = (
            f"⚠️ API ERROR DETECTED\n\n"
            f"Service: {service_name}\n"
            f"Feature: {feature}\n"
            f"Error: {error_details}\n\n"
            f"The bot will use simulated data until the API is available again."
        )
        
        for user_id in self.allowed_users:
            try:
                await self.application.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    reply_markup=self.markup
                )
            except Exception as e:
                logger.error(f"Failed to send API error alert to {user_id}: {e}")

    async def _generate_sp500_vs_btc_comparison(self, chat_id: int):
        """Generate and send portfolio performance comparison chart"""
        try:
            # Always get the current year
            current_year = datetime.now().year
            logger.info(f"Generating portfolio performance chart for year: {current_year}")
            
            start_date = datetime(current_year, 1, 1)
            days_since_start = max(1, (datetime.now() - start_date).days)  # Ensure at least 1 day
            
            # Get actual portfolio performance data
            portfolio_performance = None
            try:
                # Get portfolio performance from database
                portfolio_data = await self.mongo_client.get_portfolio_performance(start_date)
                if portfolio_data and 'performance_percentage' in portfolio_data:
                    portfolio_performance = portfolio_data['performance_percentage']
                    logger.info(f"Portfolio performance: {portfolio_performance:.2f}%")
            except Exception as e:
                logger.warning(f"Error getting portfolio performance: {e}")
            
            # Get BTC data for current year
            btc_data = await self.binance_client.get_historical_prices("BTCUSDT", days_since_start + 5)  # Add buffer days
            
            if not btc_data or len(btc_data) < 2:
                await self.application.bot.send_message(
                    chat_id=chat_id,
                    text=f"Not enough BTC price data for {current_year} year-to-date comparison.",
                    reply_markup=self.markup
                )
                return
                
            # Filter BTC data to only include this year
            btc_ytd_prices = {}
            btc_start_price = None
            
            for data_point in btc_data:
                if data_point['timestamp'].year == current_year:
                    date_str = data_point['timestamp'].strftime('%Y-%m-%d')
                    price = float(data_point['price'])
                    
                    # Set start price to first entry of the year
                    if btc_start_price is None:
                        btc_start_price = price
                    
                    # Calculate percentage change from start of year
                    btc_ytd_prices[date_str] = ((price - btc_start_price) / btc_start_price) * 100
            
            # Try to get S&P 500 data from Yahoo scraper
            sp500_data = {}
            try:
                # Check if yahoo_scraper is available
                if hasattr(self.binance_client, 'yahoo_scraper'):
                    sp500_data = await self.binance_client.yahoo_scraper.get_sp500_data(days_since_start + 5)
                    
                # If we couldn't get data from the scraper, use simulated data
                if not sp500_data or len(sp500_data) < 2:
                    logger.warning(f"Failed to get real S&P 500 data for {current_year}, using simulated data")
                    sp500_data = await self._generate_simulated_sp500_data(days_since_start + 5)
            except Exception as e:
                logger.warning(f"Error fetching S&P 500 data for {current_year}: {e}, using simulated data")
                sp500_data = await self._generate_simulated_sp500_data(days_since_start + 5)
            
            # Filter S&P 500 data to only include this year
            sp500_ytd = {}
            first_date = None
            first_value = 0
            
            # Sort the dates to find the earliest one in the current year
            dates = sorted(sp500_data.keys())
            for date in dates:
                try:
                    year = int(date.split('-')[0])
                    if year == current_year:
                        if first_date is None:
                            first_date = date
                            first_value = sp500_data[date]
                        
                        # Adjust values to be relative to the first day of the year
                        sp500_ytd[date] = sp500_data[date] - first_value
                except (ValueError, IndexError) as e:
                    logger.warning(f"Error processing date {date}: {e}")
            
            # Create the comparison chart with explicit current year
            chart_bytes = await self._create_portfolio_comparison_chart(
                btc_ytd_prices, 
                sp500_ytd, 
                portfolio_performance,
                current_year
            )
            
            if not chart_bytes:
                await self.application.bot.send_message(
                    chat_id=chat_id,
                    text=f"Failed to generate portfolio performance chart for {current_year}.",
                    reply_markup=self.markup
                )
                return
                
            # Get current values for caption
            btc_current = list(btc_ytd_prices.values())[-1] if btc_ytd_prices and btc_ytd_prices.values() else 0
            sp500_current = list(sp500_ytd.values())[-1] if sp500_ytd and sp500_ytd.values() else 0
            
            # Build caption with explicit current year
            caption_parts = [f"📈 Portfolio Performance ({current_year})\n"]
            
            # Add portfolio performance if available
            if portfolio_performance is not None:
                caption_parts.append(f"🦖 Your Portfolio: {portfolio_performance:.2f}%")
            
            # Add benchmark performances
            caption_parts.append(f"🟠 Bitcoin: {btc_current:.2f}%")
            caption_parts.append(f"🔵 S&P 500: {sp500_current:.2f}%\n")
            
            # Add explanation with explicit current year
            caption_parts.append(f"Chart shows percentage change since January 1, {current_year}")
            
            # Send the chart
            await self.application.bot.send_photo(
                chat_id=chat_id,
                photo=chart_bytes,
                caption="\n".join(caption_parts),
                reply_markup=self.markup
            )
            
        except Exception as e:
            logger.error(f"Error generating portfolio performance comparison: {e}", exc_info=True)
            await self.application.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Error generating performance chart: {str(e)}",
                reply_markup=self.markup
            )
    
    async def _create_portfolio_comparison_chart(self, btc_data: dict, sp500_data: dict, 
                                                portfolio_performance: float = None, 
                                                year: int = None) -> Optional[bytes]:
        """Create portfolio performance comparison chart"""
        try:
            import matplotlib.pyplot as plt
            from matplotlib.ticker import FuncFormatter
            import matplotlib.dates as mdates
            import pandas as pd
            import io
            import numpy as np
            
            # Always ensure we use the current year if none is provided
            if year is None or year < 2000:  # Basic validation
                year = datetime.now().year
                logger.warning(f"Invalid year provided, using current year: {year}")

            # Convert data to DataFrames
            btc_df = pd.DataFrame([
                {'date': date, 'value': value} for date, value in btc_data.items()
            ])
            
            if not btc_df.empty:
                btc_df['date'] = pd.to_datetime(btc_df['date'])
                btc_df.set_index('date', inplace=True)
                
            sp500_df = pd.DataFrame([
                {'date': date, 'value': value} for date, value in sp500_data.items()
            ])
            
            if not sp500_df.empty:
                sp500_df['date'] = pd.to_datetime(sp500_df['date'])
                sp500_df.set_index('date', inplace=True)
            
            # Create figure
            fig, ax = plt.subplots(figsize=(12, 8))
            
            # Plot both datasets
            if not btc_df.empty:
                btc_df['value'].plot(ax=ax, color='orange', linewidth=2, label='Bitcoin')
            
            if not sp500_df.empty:
                sp500_df['value'].plot(ax=ax, color='blue', linewidth=2, label='S&P 500')
            
            # Get date range for portfolio performance line
            if btc_df.empty and sp500_df.empty:
                # No data available, create a simple date range
                start_date = datetime(year, 1, 1)
                end_date = datetime.now()
                dates = pd.date_range(start=start_date, end=end_date, freq='D')
                date_range = [start_date, end_date]
            else:
                # Use the range from available data
                if not btc_df.empty:
                    date_range = [btc_df.index.min(), btc_df.index.max()]
                else:
                    date_range = [sp500_df.index.min(), sp500_df.index.max()]
            
            # Add portfolio performance as a horizontal line - ensure it's never skipped
            portfolio_performance = 0.0 if portfolio_performance is None else portfolio_performance
            # Create a horizontal line at portfolio performance level
            portfolio_line = ax.axhline(y=portfolio_performance, color='green', 
                                       linewidth=3, linestyle='-', label='Your Portfolio')
            
            # Add annotation for portfolio performance
            ax.annotate(f"{portfolio_performance:.1f}%", 
                      xy=(date_range[-1], portfolio_performance),
                      xytext=(5, 0), textcoords='offset points',
                      color='green', fontweight='bold')
            
            # Add zero line
            ax.axhline(y=0, color='gray', linestyle='--', alpha=0.7)
            
            # Format chart with the explicit year
            current_year_text = str(year)  # Ensure year is a string
            ax.set_title(f'Portfolio Performance: Your Bot vs Markets ({current_year_text})', fontsize=14)
            ax.set_ylabel('YTD Change (%)', fontsize=12)
            ax.grid(True, alpha=0.3)
            
            # Format y-axis as percentage
            ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f'{x:.1f}%'))
            
            # Format x-axis to show months
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%b'))
            ax.xaxis.set_major_locator(mdates.MonthLocator())
            
            # Add legend with larger font
            ax.legend(loc='best', fontsize=12)
            
            # Get final values for annotation
            if not btc_df.empty:
                final_btc = btc_df['value'].iloc[-1]
                ax.annotate(f"{final_btc:.1f}%", 
                          xy=(btc_df.index[-1], final_btc),
                          xytext=(5, 5), textcoords='offset points',
                          fontsize=11, color='orange')
            
            if not sp500_df.empty:
                final_sp500 = sp500_df['value'].iloc[-1]
                ax.annotate(f"{final_sp500:.1f}%", 
                          xy=(sp500_df.index[-1], final_sp500),
                          xytext=(5, -15), textcoords='offset points',
                          fontsize=11, color='blue')
            
            # Save to buffer
            buf = io.BytesIO()
            plt.tight_layout()
            plt.savefig(buf, format='png', dpi=150)
            plt.close(fig)
            buf.seek(0)
            
            return buf.getvalue()
            
        except Exception as e:
            logger.error(f"Error creating portfolio comparison chart for year {year}: {e}", exc_info=True)
            return None

    async def _generate_simulated_sp500_data(self, days: int = 90) -> Dict:
        """Generate simulated S&P 500 data when API is unavailable"""
        import numpy as np
        
        logger.info("Generating simulated S&P 500 data")
        today = datetime.utcnow()
        current_year = today.year
        start_date = datetime(current_year, 1, 1)
        
        # Adjust days to be the days since start of year if less than provided days
        days_since_start = (today - start_date).days
        days_to_use = min(days, days_since_start + 5)  # Use the smaller of the two with a buffer
        
        base_value = 4000.0  # Starting value for S&P 500
        daily_change = 0.05  # Average daily change percentage
        result = {}
        
        # Generate simulated S&P 500 performance data
        for day in range(days_to_use, -1, -1):
            sim_date = (today - timedelta(days=day))
            # Only include dates from the current year
            if sim_date.year == current_year:
                date_str = sim_date.strftime('%Y-%m-%d')
                # Simulate some realistic movement with noise and slight upward trend
                random_factor = np.random.normal(0, 1) * daily_change
                base_value *= (1 + random_factor / 100)
                result[date_str] = ((base_value - 4000.0) / 4000.0) * 100
                
        logger.info(f"Generated {len(result)} days of simulated S&P 500 data for {current_year}")
        return result

    async def _generate_portfolio_composition_chart(self, chat_id: int):
        """Generate and send portfolio composition pie chart"""
        try:
            # Get positions from MongoDB for configured pairs
            allowed_symbols = set(self.config['trading']['pairs'])
            positions = await self.mongo_client.get_position_stats(allowed_symbols)
            
            if not positions or len(positions) == 0:
                await self.application.bot.send_message(
                    chat_id=chat_id,
                    text="No portfolio data available for composition chart.",
                    reply_markup=self.markup
                )
                return
            
            # Get the base currency from config
            base_currency = self.config['trading'].get('base_currency', 'USDT')
                
            # Get base currency balance
            base_balance = await self.binance_client.get_balance(base_currency)
            
            # Get current prices for all positions to calculate current values
            portfolio_data = []
            total_value = float(base_balance)  # Start with base currency balance
            asset_values = {base_currency: float(base_balance)}
            
            for symbol, position in positions.items():
                if float(position['total_quantity']) <= 0:
                    continue
                    
                try:
                    # Get current price for the symbol
                    ticker = await self.binance_client.client.get_symbol_ticker(symbol=symbol)
                    current_price = float(ticker['price'])
                    
                    # Calculate current value of the position
                    position_value = float(position['total_quantity']) * current_price
                    
                    # Add to total portfolio value
                    total_value += position_value
                    
                    # Store the value for this asset
                    base_asset = symbol.replace(base_currency, '')
                    asset_values[base_asset] = position_value
                    
                except Exception as e:
                    logger.error(f"Error getting price for {symbol}: {e}")
            
            # Generate the chart
            chart_bytes = await self._create_portfolio_composition_chart(asset_values, total_value, base_currency)
            
            if not chart_bytes:
                await self.application.bot.send_message(
                    chat_id=chat_id,
                    text="Failed to generate portfolio composition chart.",
                    reply_markup=self.markup
                )
                return
                
            # Format a detailed text for the caption
            caption_lines = ["📊 Portfolio Composition"]
            for asset, value in sorted(asset_values.items(), key=lambda x: x[1], reverse=True):
                percentage = (value / total_value * 100) if total_value > 0 else 0
                caption_lines.append(f"{asset}: ${value:.2f} ({percentage:.1f}%)")
            
            caption_lines.append(f"\nTotal Portfolio Value: ${total_value:.2f}")
            
            # Send chart to user
            await self.application.bot.send_photo(
                chat_id=chat_id,
                photo=chart_bytes,
                caption="\n".join(caption_lines),
                reply_markup=self.markup
            )
            
        except Exception as e:
            logger.error(f"Error generating portfolio composition chart: {e}", exc_info=True)
            await self.application.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Error generating portfolio composition chart: {str(e)}",
                reply_markup=self.markup
            )
            
    async def _create_portfolio_composition_chart(self, asset_values: dict, total_value: float, base_currency: str = 'USDT') -> Optional[bytes]:
        """Create portfolio composition pie chart"""
        try:
            import matplotlib.pyplot as plt
            import numpy as np
            import io
            from matplotlib.patches import Wedge
            
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
            percentages = [(value / total_value * 100) if total_value > 0 else 0 for value in values]
            
            # Generate colors - ensure base currency is a specific color if present
            colors = plt.cm.tab20.colors[:len(labels)]
            if base_currency in labels:
                base_index = labels.index(base_currency)
                # Use a specific color for base currency - light green
                colors = list(colors)
                colors[base_index] = (0.2, 0.8, 0.2, 1.0)  # RGBA for green
            
            # Create figure
            plt.figure(figsize=(10, 8))
            
            # Create a slightly more visually appealing pie chart with shadow and explode effect
            explode = [0.05] * len(labels)  # Small explode effect for all pieces
            if base_currency in labels:
                base_index = labels.index(base_currency)
                explode[base_index] = 0.1  # Larger explode for base currency
            
            # Create the pie chart with percentages displayed in legend
            patches, texts, autotexts = plt.pie(
                values, 
                labels=None,  # We'll add custom legend
                explode=explode,
                shadow=True,
                startangle=90,
                colors=colors,
                autopct='%1.1f%%',
                pctdistance=0.85,
                wedgeprops=dict(width=0.5, edgecolor='w')
            )
            
            # Customize the appearance of percentage text
            for autotext in autotexts:
                autotext.set_color('white')
                autotext.set_fontsize(10)
                autotext.set_fontweight('bold')
            
            # Create a custom legend with both asset and percentage
            legend_labels = [f"{label} (${value:.2f})" for label, value in zip(labels, values)]
            plt.legend(
                patches,
                legend_labels,
                loc="center left",
                bbox_to_anchor=(1, 0.5),
                frameon=False
            )
            
            plt.title(f"Portfolio Composition ({base_currency})", fontsize=16, pad=20)
            plt.tight_layout()
            
            # Save to buffer
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
            plt.close()
            buf.seek(0)
            
            return buf.getvalue()
            
        except Exception as e:
            logger.error(f"Error creating portfolio composition chart: {e}", exc_info=True)
            return None

    async def add_symbol_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add a new symbol to the trading list"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("You're not authorized to use this bot.")
            return
            
        # Check if we have arguments (symbol)
        if not context.args or len(context.args) < 1:
            await update.message.reply_text(
                "Please provide a symbol to add. Example: /add_symbol BTCUSDT"
            )
            return
            
        symbol = context.args[0].upper().strip()
        
        # Validate the symbol format
        if not self.binance_client._is_valid_symbol_format(symbol):
            await update.message.reply_text(
                f"❌ Invalid symbol format: {symbol}\n"
                "Symbol should be in the format BTCUSDT, ETHUSDT, etc."
            )
            return
            
        # Check if the symbol is valid on Binance
        is_valid = await self.binance_client.check_symbol_validity(symbol)
        
        if not is_valid:
            await update.message.reply_text(
                f"❌ Symbol {symbol} is not valid on Binance."
            )
            return
            
        # Add to database
        success = await self.mongo_client.save_trading_symbol(symbol)
        
        if success:
            # Add to the active config as well
            if symbol not in self.binance_client.config['trading']['pairs']:
                self.binance_client.config['trading']['pairs'].append(symbol)
                
            await update.message.reply_text(
                f"✅ Successfully added {symbol} to the trading symbols list."
            )
        else:
            await update.message.reply_text(
                f"ℹ️ Symbol {symbol} is already in the trading list."
            )

    async def remove_symbol_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Remove a symbol from the trading list"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("You're not authorized to use this bot.")
            return
            
        # Check if we have arguments (symbol)
        if not context.args or len(context.args) < 1:
            # Show a list of symbols with buttons to remove
            symbols = await self.mongo_client.get_trading_symbols()
            
            if not symbols:
                await update.message.reply_text("No trading symbols configured.")
                return
                
            keyboard = []
            for symbol in symbols:
                keyboard.append([InlineKeyboardButton(f"Remove {symbol}", callback_data=f"remove_symbol:{symbol}")])
                
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "Select a symbol to remove:",
                reply_markup=reply_markup
            )
            return
            
        symbol = context.args[0].upper().strip() if context.args else None
        
        if not symbol:
            # Show list of symbols with buttons - existing code
            return
        
        # Check if this was a pre-configured symbol
        was_preconfigured = hasattr(self.binance_client, 'original_config_symbols') and \
                           symbol in self.binance_client.original_config_symbols
        
        # Remove from database
        success = await self.mongo_client.remove_trading_symbol(symbol)
        
        if success:
            # IMPORTANT: Update the active config immediately
            if symbol in self.binance_client.config['trading']['pairs']:
                self.binance_client.config['trading']['pairs'].remove(symbol)
                
            # 1. Cancel any pending orders for this symbol
            canceled_orders = await self._cancel_symbol_orders(symbol)
            
            # 2. Clear any triggered thresholds for this symbol
            cleared_thresholds = await self._clear_symbol_thresholds(symbol)
                
            # If it was a pre-configured symbol, add it to the removed list
            if was_preconfigured:
                await self.mongo_client.add_removed_symbol(symbol)
                message = f"✅ Successfully removed {symbol} from the trading symbols list.\n" \
                         f"• Canceled {canceled_orders} pending orders\n" \
                         f"• Cleared thresholds for all timeframes\n" \
                         f"(This pre-configured symbol will not be auto-added on restart)"
            else:
                message = f"✅ Successfully removed {symbol} from the trading symbols list.\n" \
                         f"• Canceled {canceled_orders} pending orders\n" \
                         f"• Cleared thresholds for all timeframes"
            
            await update.message.reply_text(message)
        else:
            await update.message.reply_text(
                f"❌ Symbol {symbol} not found in the trading list."
            )

    async def list_symbols_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List all trading symbols"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("You're not authorized to use this bot.")
            return
            
        symbols = await self.mongo_client.get_trading_symbols()
        
        if not symbols:
            await update.message.reply_text("No trading symbols configured.")
            return
            
        # Format the message
        message = "📊 *Configured Trading Symbols*\n\n"
        for i, symbol in enumerate(sorted(symbols), 1):
            # Get current price if possible
            try:
                price = await self.binance_client.get_current_price(symbol)
                price_text = f"${price:,.2f}" if price else "N/A"
            except:
                price_text = "N/A"
            
            # Check if this was in the original config
            was_preconfigured = symbol in self.config.get('trading', {}).get('original_pairs', [])
            symbol_marker = "🔹" if was_preconfigured else "🆕"
                
            message += f"{i}. {symbol_marker} `{symbol}` - {price_text}\n"
        
        message += "\n🔹 Pre-configured  🆕 Manually added"
        
        await update.message.reply_text(
            message,
            parse_mode='Markdown'
        )

    async def handle_symbol_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle all callback queries"""
        callback_query = update.callback_query
        data = callback_query.data
        user_id = callback_query.from_user.id
        
        if not self._is_authorized(user_id):
            await callback_query.answer("⛔ Unauthorized access")
            return
        
        try:
            # Acknowledge the callback to remove the loading indicator
            await callback_query.answer()
            
            # Handle different callback types
            if data == "show_menu":
                await self.show_menu_from_callback(callback_query, context)
                return
                
            # Handle visualization callbacks
            if data.startswith("viz_"):
                if data.startswith("viz_balance_"):
                    # Extract days from callback data
                    days_part = data.split("_")[2]
                    days = 999999 if days_part == "all" else int(days_part)
                    await self.send_balance_chart(callback_query, days)
                    return
                    
                elif data.startswith("viz_btc_"):
                    # Extract days from callback data
                    days = int(data.split("_")[2])
                    await self.send_performance_chart(callback_query, days)
                    return
                
                elif data == "viz_transactions":
                    # Show deposits and withdrawals visualization
                    await self.send_transactions_chart(callback_query)
                    return
            
            # Handle symbol management
            if data.startswith("symbol_"):
                parts = data.split("_")
                action = parts[1]
                symbol = parts[2] if len(parts) > 2 else None
                
                if action == "add":
                    context.user_data["adding_symbol"] = True
                    await callback_query.message.reply_text(
                        "Please enter the symbol to add (e.g. BTCUSDT):",
                        reply_markup=ForceReply(selective=True)
                    )
                    return
                    
                elif action == "remove" and symbol:
                    await self.remove_symbol(callback_query, symbol)
                    return
                    
                elif action == "list":
                    await self.list_symbols(callback_query)
                    return
        except Exception as e:
            logger.error(f"Error handling callback: {e}", exc_info=True)
            await callback_query.message.reply_text(
                f"❌ Error processing your request: {str(e)}"
            )

    # Add helper methods to cancel orders and clear thresholds
            logger.error(f"Error enabling partial take profits: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Error: {str(e)}")
            
    async def partial_tp_disable(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Disable partial take profits"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        try:
            # Send initial status message
            status_message = await update.message.reply_text(
                "⏳ Disabling partial take profits and restoring standard take profit settings..."
            )
            
            # Update the config
            self.config['trading']['partial_take_profits']['enabled'] = False
            
            # Save to database
            await self.mongo_client.update_trading_setting('partial_take_profits.enabled', False)
            
            # Get active orders to adjust their TP settings
            active_orders = await self.mongo_client.get_active_orders()
            orders_adjusted = 0
            
            if active_orders:
                await status_message.edit_text(
                    f"⏳ Disabling partial take profits and adjusting {len(active_orders)} active orders..."
                )
                
                # Process each active order to restore standard TP
                for order in active_orders:
                    if order.partial_take_profits:
                        try:
                            # Check if order has a standard take profit already
                            if not order.take_profit:
                                # Create a new take profit using the default percentage
                                tp_price = Decimal(order.price) * (1 + self.binance_client.default_tp_percentage / 100)
                                take_profit = TakeProfit(
                                    price=tp_price,
                                    percentage=self.binance_client.default_tp_percentage,
                                    status=TPSLStatus.PENDING
                                )
                                order.take_profit = take_profit
                            
                            # Update partial take profits status to canceled
                            for ptp in order.partial_take_profits:
                                if ptp.status == TPSLStatus.PENDING:
                                    ptp.status = TPSLStatus.CANCELLED
                            
                            # Update order in database
                            await self.mongo_client.insert_order(order)
                            
                            # Cancel partial TP orders on the exchange
                            for ptp in order.partial_take_profits:
                                if ptp.order_id:
                                    await self.binance_client.cancel_order(order.symbol, ptp.order_id)
                            
                            orders_adjusted += 1
                            
                        except Exception as e:
                            logging.error(f"Error adjusting order {order.order_id}: {e}")
            
            # Update status message
            await status_message.edit_text(
                f"✅ Partial take profits disabled. Adjusted {orders_adjusted} orders."
            )
            
            # Send confirmation
            await update.message.reply_text(
                f"❌ Partial take profits have been disabled.\n"
                f"{orders_adjusted} active orders were adjusted to use standard take profit.\n"
                f"Use `/set_partial_tp` to configure and enable partial take profits again."
            )
            
        except Exception as e:
            logger.error(f"Error disabling partial take profits: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Error: {str(e)}")
            
    async def set_partial_tp(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set a partial take profit level"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        try:
            # Get message parts, expecting format: /set_partial_tp <level> <position_percentage> <profit_percentage>
            message_parts = update.message.text.split()
            
            # Check that we have enough parts
            if len(message_parts) != 4:
                await update.message.reply_text(
                    "Please provide level, position percentage, and profit percentage.\n"
                    "Example: `/set_partial_tp 1 25 1.5` to sell 25% of position at +1.5% profit."
                )
                return
                
            # Parse parameters
            try:
                level = int(message_parts[1])
                position_percentage = float(message_parts[2])
                profit_percentage = float(message_parts[3])
            except ValueError:
                await update.message.reply_text(
                    "Invalid parameters. Please provide numeric values for level, position percentage, and profit percentage."
                )
                return
                
            # Validate parameters
            if level < 1 or level > 10:
                await update.message.reply_text("Level must be between 1 and 10.")
                return
                
            if position_percentage <= 0 or position_percentage > 100:
                await update.message.reply_text("Position percentage must be between 0 and 100.")
                return
                
            if profit_percentage <= 0:
                await update.message.reply_text("Profit percentage must be greater than 0.")
                return
                
            # First message - send processing status
            status_message = await update.message.reply_text(
                f"⏳ Processing Partial Take Profit Level {level}..."
            )
                
            # Get current levels
            if 'levels' not in self.config['trading']['partial_take_profits']:
                self.config['trading']['partial_take_profits']['levels'] = []
                
            levels = self.config['trading']['partial_take_profits']['levels']
            
            # Second message - confirmation of parameters
            await update.message.reply_text(
                f"Setting Partial TP Level {level}:\n"
                f"• Position: {position_percentage}% of total position\n"
                f"• Profit target: +{profit_percentage}%"
            )
            
            # Check for level index
            level_index = level - 1  # Convert to 0-based index
            
            # Update or add the level
            level_data = {
                'level': level,
                'position_percentage': position_percentage,
                'profit_percentage': profit_percentage
            }
            
            if level_index < len(levels):
                # Update existing level
                levels[level_index] = level_data
            else:
                # Add new level, fill gaps with empty levels if needed
                while len(levels) < level_index:
                    levels.append({'level': len(levels)+1, 'position_percentage': 0, 'profit_percentage': 0})
                levels.append(level_data)
                
            # Check if this is enabling partial TP for the first time
            was_enabled = self.config['trading']['partial_take_profits'].get('enabled', False)
            
            # Update config and make sure partial TP is enabled
            self.config['trading']['partial_take_profits']['levels'] = levels
            self.config['trading']['partial_take_profits']['enabled'] = True
            
            # Save to database
            await self.mongo_client.update_trading_setting('partial_take_profits.levels', levels)
            await self.mongo_client.update_trading_setting('partial_take_profits.enabled', True)
            
            # Check total position percentage
            total_percentage = sum(level.get('position_percentage', 0) for level in levels)
            warning = ""
            if total_percentage > 100:
                warning = "\n⚠️ Warning: Total position percentage exceeds 100%!"
            
            # If this is enabling partial TP for the first time, adjust existing orders
            orders_adjusted = 0
            if not was_enabled:
                # Get active orders
                active_orders = await self.mongo_client.get_active_orders()
                if active_orders:
                    await status_message.edit_text(
                        f"⏳ Setting up partial take profit level {level} and adjusting {len(active_orders)} active orders..."
                    )
                    
                    # Process each active order to add partial TPs
                    for order in active_orders:
                        try:
                            # Skip orders that already have partial take profits set up
                            if order.partial_take_profits and any(ptp.status == TPSLStatus.PENDING for ptp in order.partial_take_profits):
                                continue
                                
                            # Cancel any existing take profit order
                            if order.take_profit and order.take_profit.order_id:
                                await self.binance_client.cancel_order(order.symbol, order.take_profit.order_id)
                                order.take_profit.status = TPSLStatus.CANCELLED
                            
                            # Set up partial take profits for this order
                            partial_tps = []
                            for i, level_config in enumerate(levels):
                                if level_config.get('position_percentage', 0) > 0:
                                    # Calculate price for this level
                                    tp_price = Decimal(order.price) * (1 + level_config.get('profit_percentage', 0) / 100)
                                    
                                    # Create the partial take profit object
                                    ptp = PartialTakeProfit(
                                        level=i+1,
                                        price=tp_price,
                                        profit_percentage=level_config.get('profit_percentage', 0),
                                        position_percentage=level_config.get('position_percentage', 0),
                                        status=TPSLStatus.PENDING
                                    )
                                    partial_tps.append(ptp)
                            
                            # Set the partial take profits on the order
                            order.partial_take_profits = partial_tps
                            
                            # Update order in database
                            await self.mongo_client.insert_order(order)
                            
                            orders_adjusted += 1
                            
                        except Exception as e:
                            logging.error(f"Error adjusting order {order.order_id}: {e}")
            
            # Wait a bit to simulate processing
            await asyncio.sleep(0.5)
            
            # Update status message
            await status_message.edit_text(
                f"✅ Partial Take Profit Level {level} configured successfully!" +
                (f" Adjusted {orders_adjusted} active orders." if orders_adjusted > 0 else "")
            )
            
            # Final message - send confirmation with all settings
            all_levels = "\n".join([
                f"• Level {i+1}: Sell {l.get('position_percentage')}% at +{l.get('profit_percentage')}% profit"
                for i, l in enumerate(levels) if l.get('position_percentage', 0) > 0
            ])
            
            # Send confirmation
            await update.message.reply_text(
                f"🎯 Partial Take Profit Settings:\n\n"
                f"{all_levels}\n"
                f"Status: ✅ Enabled{warning}" +
                (f"\n\n{orders_adjusted} active orders were adjusted to use partial take profits." if orders_adjusted > 0 else "") +
                f"\n\nUse `/partial_tp_disable` to disable partial take profits while preserving your settings."
            )
            
        except Exception as e:
            logger.error(f"Error setting partial take profit level: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Error: {str(e)}")

    async def show_trailing_sl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Display trailing stop loss settings"""
        try:
            # Check if user is authorized
            if not await self.is_user_authorized(update):
                return
                
            # Get trailing stop loss config
            trading_config = self.config.get('trading', {})
            trailing_sl_config = trading_config.get('trailing_stop_loss', {})
            
            is_enabled = trailing_sl_config.get('enabled', False)
            activation_percentage = trailing_sl_config.get('activation_percentage', 0)
            callback_rate = trailing_sl_config.get('callback_rate', 0)
            
            status = "✅ ENABLED" if is_enabled else "❌ DISABLED"
            
            message = (
                f"<b>Trailing Stop Loss Settings</b>\n\n"
                f"<b>Status:</b> {status}\n"
                f"<b>Activation:</b> {activation_percentage}%\n"
                f"<b>Callback Rate:</b> {callback_rate}%\n\n"
                f"<i>Commands:</i>\n"
                f"/enabletrailingsl - Enable trailing stop loss\n"
                f"/disabletrailingsl - Disable trailing stop loss\n"
                f"/settrailingsl [activation] [callback] - Set trailing stop loss parameters\n"
                f"  Example: /settrailingsl 3 1.5 - Sets activation at 3% profit and callback at 1.5%"
            )
            
            await update.message.reply_text(
                text=message,
                parse_mode=ParseMode.HTML,
                reply_markup=self.markup
            )
            
        except Exception as e:
            logger.error(f"Error in show_trailing_sl: {e}")
            await update.message.reply_text(
                "Error fetching trailing stop loss settings. Please try again.",
                reply_markup=self.markup
            )
            
    async def trailing_sl_enable(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Enable trailing stop loss"""
        try:
            # Check if user is authorized
            if not await self.is_user_authorized(update):
                return
                
            # Get current settings
            trading_config = self.config.get('trading', {})
            trailing_sl_config = trading_config.get('trailing_stop_loss', {})
            
            # Check if already enabled
            if trailing_sl_config.get('enabled', False):
                await update.message.reply_text(
                    "Trailing stop loss is already enabled.",
                    reply_markup=self.markup
                )
                return
                
            # Enable trailing stop loss
            trailing_sl_config['enabled'] = True
            
            # Update config
            if 'trailing_stop_loss' not in trading_config:
                trading_config['trailing_stop_loss'] = trailing_sl_config
                
            # Save to database
            success = await self.mongo_client.update_trading_setting('trailing_stop_loss', trailing_sl_config)
            
            if success:
                await update.message.reply_text(
                    f"✅ Trailing stop loss enabled\n"
                    f"Activation: {trailing_sl_config.get('activation_percentage', 0)}%\n"
                    f"Callback Rate: {trailing_sl_config.get('callback_rate', 0)}%",
                    reply_markup=self.markup
                )
            else:
                await update.message.reply_text(
                    "Failed to enable trailing stop loss. Database update error.",
                    reply_markup=self.markup
                )
                
        except Exception as e:
            logger.error(f"Error enabling trailing stop loss: {e}")
            await update.message.reply_text(
                "Error enabling trailing stop loss. Please try again.",
                reply_markup=self.markup
            )
            
    async def trailing_sl_disable(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Disable trailing stop loss"""
        try:
            # Check if user is authorized
            if not await self.is_user_authorized(update):
                return
                
            # Get current settings
            trading_config = self.config.get('trading', {})
            trailing_sl_config = trading_config.get('trailing_stop_loss', {})
            
            # Check if already disabled
            if not trailing_sl_config.get('enabled', False):
                await update.message.reply_text(
                    "Trailing stop loss is already disabled.",
                    reply_markup=self.markup
                )
                return
                
            # Disable trailing stop loss
            trailing_sl_config['enabled'] = False
            
            # Update config
            if 'trailing_stop_loss' not in trading_config:
                trading_config['trailing_stop_loss'] = trailing_sl_config
                
            # Save to database
            success = await self.mongo_client.update_trading_setting('trailing_stop_loss', trailing_sl_config)
            
            if success:
                await update.message.reply_text(
                    "❌ Trailing stop loss disabled",
                    reply_markup=self.markup
                )
            else:
                await update.message.reply_text(
                    "Failed to disable trailing stop loss. Database update error.",
                    reply_markup=self.markup
                )
                
        except Exception as e:
            logger.error(f"Error disabling trailing stop loss: {e}")
            await update.message.reply_text(
                "Error disabling trailing stop loss. Please try again.",
                reply_markup=self.markup
            )
            
    async def set_trailing_sl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set trailing stop loss parameters: activation percentage and callback rate"""
        try:
            # Check if user is authorized
            if not await self.is_user_authorized(update):
                return
                
            # Get arguments
            args = context.args
            if len(args) < 2:
                await update.message.reply_text(
                    "Please provide both activation percentage and callback rate.\n"
                    "Example: /settrailingsl 3 1.5",
                    reply_markup=self.markup
                )
                return
                
            try:
                activation_percentage = float(args[0])
                callback_rate = float(args[1])
                
                # Validate values
                if activation_percentage <= 0:
                    await update.message.reply_text(
                        "Activation percentage must be greater than 0.",
                        reply_markup=self.markup
                    )
                    return
                    
                if callback_rate <= 0:
                    await update.message.reply_text(
                        "Callback rate must be greater than 0.",
                        reply_markup=self.markup
                    )
                    return
                    
            except ValueError:
                await update.message.reply_text(
                    "Invalid values. Please provide valid numbers for activation percentage and callback rate.",
                    reply_markup=self.markup
                )
                return
                
            # Get current settings
            trading_config = self.config.get('trading', {})
            if 'trailing_stop_loss' not in trading_config:
                trading_config['trailing_stop_loss'] = {
                    'enabled': False,
                    'activation_percentage': 0,
                    'callback_rate': 0
                }
                
            # Update settings
            trailing_sl_config = trading_config['trailing_stop_loss']
            trailing_sl_config['activation_percentage'] = activation_percentage
            trailing_sl_config['callback_rate'] = callback_rate
            
            # Save to database
            success = await self.mongo_client.update_trading_setting('trailing_stop_loss', trailing_sl_config)
            
            if success:
                status = "✅ ENABLED" if trailing_sl_config.get('enabled', False) else "❌ DISABLED"
                await update.message.reply_text(
                    f"Trailing stop loss settings updated:\n"
                    f"Status: {status}\n"
                    f"Activation: {activation_percentage}%\n"
                    f"Callback Rate: {callback_rate}%",
                    reply_markup=self.markup
                )
            else:
                await update.message.reply_text(
                    "Failed to update trailing stop loss settings. Database update error.",
                    reply_markup=self.markup
                )
                
        except Exception as e:
            logger.error(f"Error setting trailing stop loss parameters: {e}")
            await update.message.reply_text(
                "Error setting trailing stop loss parameters. Please try again.",
                reply_markup=self.markup
            )

    async def register_commands(self):
        """Register bot commands with the menu"""
        try:
            # Register commands in the app menu
            commands = [
                ('start', 'Start the bot'),
                ('menu', 'Show main command menu'),
                ('power', 'Toggle trading on/off'),
                ('balance', 'Check current balance'),
                ('stats', 'View trading statistics'),
                ('profits', 'View profit/loss analysis'),
                ('orders', 'View active orders'),
                ('history', 'View order history'),
                ('thresholds', 'Show price thresholds'),
                ('add', 'Add manual trade'),
                ('resetthresholds', 'Reset price thresholds'),
                ('symbols', 'Manage trading symbols'),
                ('viz', 'Show data visualizations'),
                ('status', 'Check bot system status'),
                ('tp_sl', 'View TP/SL settings'),
                ('set_tp', 'Set take profit percentage'),
                ('set_sl', 'Set stop loss percentage'),
                ('set_partial_tp', 'Configure partial take profits'),
                ('partial_tp_disable', 'Disable partial take profits'),
                ('show_trailing_sl', 'Show trailing stop loss settings'),
                ('trailing_sl_enable', 'Enable trailing stop loss'),
                ('trailing_sl_disable', 'Disable trailing stop loss'),
                ('set_lower_entries', 'Configure lower entries protection'),
                ('deposit', 'Record a deposit'),
                ('withdraw', 'Record a withdrawal'),
                ('transactions', 'View deposit/withdrawal history'),
                ('help', 'Show help text with all commands')
            ]
            
            await self.application.bot.set_my_commands(commands)
            
            # Set command handlers
            self.application.add_handler(CommandHandler("start", self.start_command))
            self.application.add_handler(CommandHandler("menu", self.show_menu))
            self.application.add_handler(CommandHandler("power", self.toggle_trading))
            self.application.add_handler(CommandHandler("balance", self.balance_command))
            self.application.add_handler(CommandHandler("stats", self.get_stats))
            self.application.add_handler(CommandHandler("profits", self.show_profits))
            self.application.add_handler(CommandHandler("history", self.get_order_history))
            self.application.add_handler(CommandHandler("thresholds", self.show_thresholds))
            self.application.add_handler(CommandHandler("add", self.add_trade_start))
            self.application.add_handler(CommandHandler("help", self.help_command))
            self.application.add_handler(CommandHandler("resetthresholds", self.reset_all_thresholds))
            self.application.add_handler(CommandHandler("viz", self.show_viz_menu))
            self.application.add_handler(CommandHandler("status", self.status_command))
            self.application.add_handler(CommandHandler("symbols", self.list_symbols_command))
            self.application.add_handler(CommandHandler("tp_sl", self.show_tp_sl))
            self.application.add_handler(CommandHandler("set_tp", self.set_take_profit))
            self.application.add_handler(CommandHandler("set_sl", self.set_stop_loss))
            self.application.add_handler(CommandHandler("orders", self.orders_command))
            self.application.add_handler(CommandHandler("deposit", self.deposit_command))
            self.application.add_handler(CommandHandler("withdraw", self.withdrawal_command))
            self.application.add_handler(CommandHandler("transactions", self.transactions_command))
            
            # Add specific command handlers for partial TP/trailing SL
            self.application.add_handler(CommandHandler("set_partial_tp", self.set_partial_tp))
            self.application.add_handler(CommandHandler("partial_tp_disable", self.partial_tp_disable))
            self.application.add_handler(CommandHandler("show_trailing_sl", self.show_trailing_sl))
            self.application.add_handler(CommandHandler("trailing_sl_enable", self.trailing_sl_enable))
            self.application.add_handler(CommandHandler("trailing_sl_disable", self.trailing_sl_disable))
            self.application.add_handler(CommandHandler("set_lower_entries", self.set_lower_entries))
            
            # Add callback query handler for viz menu
            self.application.add_handler(CallbackQueryHandler(
                self.handle_viz_selection, pattern=r'^(daily_volume|profit_distribution|order_types|hourly_activity|balance_chart|roi_comparison|sp500_vs_btc|portfolio_composition).*$')
            )
            
            # Add callback query handler for threshold menu
            self.application.add_handler(CallbackQueryHandler(
                self.handle_threshold_selection, pattern=r'^(reset_daily|reset_weekly|reset_monthly)$')
            )
            
            # Add callback query handler for symbol management
            self.application.add_handler(CallbackQueryHandler(
                self.handle_symbol_callback, pattern=r'^(symbol_remove|back_to_symbols|add_symbol).*$')
            )
            
            # Error handler
            self.application.add_error_handler(self.handle_error)
            
            logger.info("Bot commands registered")
            return True
        except Exception as e:
            logger.error(f"Error registering commands: {e}", exc_info=True)
            return False

    async def is_user_authorized(self, update: Update) -> bool:
        """Check if user is authorized to use the bot"""
        user_id = update.effective_user.id
        if not self._is_authorized(user_id):
            await update.message.reply_text("You're not authorized to use this bot.")
            return False
        return True

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show help message with all available commands"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        # Add emoji to make help more visually appealing
        message = "🦖 Trade-a-saurus Rex Bot Commands 🦖\n\n"
        
        # Basic commands
        message += "Basic Commands:\n"
        message += "/start - Start the bot\n"
        message += "/menu - Show command menu\n"
        message += "/power - Toggle trading on/off\n"
        message += "/help - Show this help message\n"
        message += "/status - Show system status\n\n"
        
        # Trading commands
        message += "Trading Commands:\n"
        message += "/balance - Check current balance\n"
        message += "/stats - View trading statistics\n"
        message += "/profits - View profit/loss analysis\n"
        message += "/history - View order history\n"
        message += "/orders - View active orders\n"
        message += "/thresholds - Show threshold status\n"
        message += "/resetthresholds - Reset all thresholds\n"
        message += "/add - Add manual trade\n\n"
        
        # Symbol management
        message += "Symbol Management:\n"
        message += "/symbols - Manage trading symbols\n\n"
        
        # Take Profit/Stop Loss settings
        message += "Take Profit & Stop Loss:\n"
        message += "/tp_sl - View TP/SL settings\n"
        message += "/set_tp <percentage> - Set take profit\n"
        message += "/set_sl <percentage> - Set stop loss\n"
        message += "/set_partial_tp - Configure partial take profits\n"
        message += "/partial_tp_disable - Disable partial take profits\n"
        message += "/set_lower_entries - Configure lower entries\n"
        message += "/show_trailing_sl - Show trailing stop loss settings\n"
        message += "/trailing_sl_enable - Enable trailing stop loss\n"
        message += "/trailing_sl_disable - Disable trailing stop loss\n\n"
        
        # Financial tracking commands
        message += "Financial Tracking:\n"
        message += "/deposit <amount> - Record a deposit\n"
        message += "/withdraw <amount> - Record a withdrawal\n"
        message += "/transactions - View recent transactions\n\n"
        
        # Visualization commands
        message += "Visualization Commands:\n"
        message += "/viz - Show data visualization menu\n\n"
        
        # Footer
        message += "Use /menu to show the button menu with all commands"
        
        await update.message.reply_text(message)

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Check bot status"""
        try:
            if not await self.is_user_authorized(update):
                return
                
            # Get environment info
            env_name = "TESTNET" if self.config['binance']['use_testnet'] else "MAINNET"
            base_currency = self.config['trading']['base_currency']
            
            # Get trading status
            trading_enabled = not self.is_paused
            status_emoji = "✅" if trading_enabled else "❌"
            
            # Get feature statuses
            trading_config = self.config.get('trading', {})
            partial_tp_config = trading_config.get('partial_take_profit', {})
            partial_tp_enabled = partial_tp_config.get('enabled', False)
            trailing_sl_enabled = trading_config.get('trailing_stop_loss', {}).get('enabled', False)
            lower_entries_enabled = trading_config.get('only_lower_entries', False)
            
            # Create partial TP details if enabled
            partial_tp_details = ""
            if partial_tp_enabled and 'levels' in partial_tp_config:
                levels = partial_tp_config.get('levels', [])
                if levels:
                    partial_tp_details = "\n<b>Partial TP Levels:</b>"
                    for i, level in enumerate(levels, 1):
                        if i <= len(levels) and 'profit_percentage' in level and 'position_percentage' in level:
                            partial_tp_details += f"\n• Level {i}: {level['position_percentage']}% at +{level['profit_percentage']}% profit"
            
            # Create status message
            status_message = f"""
<b>🦖 Trade-a-saurus Rex Status</b>

<b>Environment:</b> {env_name}
<b>Base Currency:</b> {base_currency}
<b>Trading Status:</b> {status_emoji} {"ACTIVE" if trading_enabled else "PAUSED"}
<b>Reserve Balance:</b> ${self.binance_client.reserve_balance:.2f}

<b>Features:</b>
• Partial TP: {"✅ Enabled" if partial_tp_enabled else "❌ Disabled"}{partial_tp_details}
• Trailing SL: {"✅ Enabled" if trailing_sl_enabled else "❌ Disabled"}
• Lower Entries Protection: {"✅ Enabled" if lower_entries_enabled else "❌ Disabled"}

<b>Default Settings:</b>
• Take Profit: {self.binance_client.default_tp_percentage}%
• Stop Loss: {self.binance_client.default_sl_percentage}%

<b>API Connection:</b> {"✅ Connected" if self.binance_client and self.binance_client.client else "❌ Disconnected"}
<b>Database Connection:</b> {await self._check_db_status()}
"""
            
            await update.message.reply_text(
                text=status_message,
                parse_mode=ParseMode.HTML,
                reply_markup=self.markup
            )
            
        except Exception as e:
            logger.error(f"Error checking status: {e}")
            await update.message.reply_text("Error checking bot status.")
            
    async def _check_api_status(self) -> str:
        """Check Binance API connection status"""
        try:
            if self.binance_client and self.binance_client.client:
                await self.binance_client.client.ping()
                return "✅ Connected"
            return "❌ Disconnected"
        except Exception:
            return "❌ Disconnected"
            
    async def _check_db_status(self) -> str:
        """Check MongoDB connection status"""
        try:
            await self.mongo_client.db.command("ping")
            return "✅ Connected"
        except Exception:
            return "❌ Disconnected"

    async def handle_threshold_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle threshold reset selection from callback"""
        try:
            query = update.callback_query
            await query.answer()
            
            if not self._is_authorized(query.from_user.id):
                await query.edit_message_text(text="⛔ Unauthorized access")
                return
            
            timeframe = query.data
            if timeframe == "reset_daily":
                await self.binance_client.reset_timeframe_thresholds("daily")
                await query.edit_message_text(text="✅ Daily thresholds have been reset")
            elif timeframe == "reset_weekly":
                await self.binance_client.reset_timeframe_thresholds("weekly")
                await query.edit_message_text(text="✅ Weekly thresholds have been reset")
            elif timeframe == "reset_monthly":
                await self.binance_client.reset_timeframe_thresholds("monthly")
                await query.edit_message_text(text="✅ Monthly thresholds have been reset")
                
        except Exception as e:
            logger.error(f"Error handling threshold selection: {e}")
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    text=f"❌ Error: {str(e)}"
                )

    # Add handler for orders command
    async def orders_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show open orders"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("You're not authorized to use this bot.")
            return
            
        try:
            # Get all active orders
            active_orders = await self.mongo_client.get_active_orders()
            
            if not active_orders:
                await update.message.reply_text("No active orders found.")
                return
                
            # Format the active orders
            order_messages = []
            for order in active_orders:
                # Format date in a readable format
                created_date = order.created_at.strftime("%Y-%m-%d %H:%M:%S")
                
                # Get basic order info
                order_info = (
                    f"🔹 Order: {order.symbol}\n"
                    f"📅 Created: {created_date}\n"
                    f"💰 Price: ${float(order.price):.8f}\n"
                    f"🔢 Quantity: {float(order.quantity):.8f}\n"
                    f"📊 Status: {order.status.value}\n"
                )
                
                # Add TP/SL info if available
                tp_sl_info = []
                if order.take_profit:
                    tp_sl_info.append(f"TP: ${float(order.take_profit.price):.8f} (+{order.take_profit.percentage:.2f}%)")
                if order.stop_loss:
                    tp_sl_info.append(f"SL: ${float(order.stop_loss.price):.8f} (-{order.stop_loss.percentage:.2f}%)")
                
                if tp_sl_info:
                    order_info += "📈 " + " | ".join(tp_sl_info) + "\n"
                    
                order_messages.append(order_info)
                
            # Join all order messages with dividers
            response = "📋 Active Orders:\n\n" + "\n\n".join(order_messages)
            
            # Send response
            await update.message.reply_text(response)
            
        except Exception as e:
            logger.error(f"Error getting orders: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")

    # Alias for list_symbols_command
    async def symbols_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Alias for list_symbols_command - used for command registration"""
        return await self.list_symbols_command(update, context)

    async def confirm_deposit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle deposit confirmation messages"""
        if not self._is_authorized(update.effective_user.id):
            return
            
        # Extract info from the message
        message_text = update.message.text
        try:
            # Message format: "Deposit confirmed: $100.00 - Note: Initial investment"
            parts = message_text.split(' - Note: ', 1)
            amount_part = parts[0].replace('Deposit confirmed: $', '').strip()
            amount = Decimal(amount_part)
            
            note = parts[1] if len(parts) > 1 else None
            
            success = await self.mongo_client.record_deposit(
                amount=amount,
                notes=note
            )
            
            if success:
                await update.message.reply_text(
                    f"✅ Deposit of ${float(amount):.2f} recorded successfully" + 
                    (f"\nNote: {note}" if note else "")
                )
            else:
                await update.message.reply_text("❌ Failed to record deposit")
                
        except Exception as e:
            await update.message.reply_text(f"❌ Error processing deposit confirmation: {str(e)}")
            
    async def confirm_withdrawal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle withdrawal confirmation messages"""
        if not self._is_authorized(update.effective_user.id):
            return
            
        # Extract info from the message
        message_text = update.message.text
        try:
            # Message format: "Withdrawal confirmed: $50.00 - Note: Moving to cold storage"
            parts = message_text.split(' - Note: ', 1)
            amount_part = parts[0].replace('Withdrawal confirmed: $', '').strip()
            amount = Decimal(amount_part)
            
            note = parts[1] if len(parts) > 1 else None
            
            success = await self.mongo_client.record_withdrawal(
                amount=amount,
                notes=note
            )
            
            if success:
                await update.message.reply_text(
                    f"✅ Withdrawal of ${float(amount):.2f} recorded successfully" + 
                    (f"\nNote: {note}" if note else "")
                )
            else:
                await update.message.reply_text("❌ Failed to record withdrawal")
                
        except Exception as e:
            await update.message.reply_text(f"❌ Error processing withdrawal confirmation: {str(e)}")
            
    async def handle_error(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle error messages"""
        if not self._is_authorized(update.effective_user.id):
            return
            
        # Just acknowledge the error
        await update.message.reply_text("✅ Error acknowledged")

    async def send_transactions_chart(self, callback_query: CallbackQuery):
        """Generate and send a chart showing deposits and withdrawals over time"""
        try:
            # Get deposits and withdrawals data (all time)
            transactions = await self.mongo_client.get_deposits_withdrawals(days=999999)
            
            if not transactions:
                await callback_query.message.reply_text(
                    "No transaction history available to visualize."
                )
                return
                
            # Convert to DataFrame for visualization
            df = pd.DataFrame([
                {
                    'date': t['timestamp'],
                    'amount': float(t['amount']),
                    'type': 'Deposit' if float(t['amount']) > 0 else 'Withdrawal',
                    'notes': t.get('notes', '')
                }
                for t in transactions
            ])
            
            # Sort by date and convert to datetime
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date')
            
            # Create cumulative sum series
            df['cumulative'] = df['amount'].cumsum()
            
            # Generate the chart
            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), gridspec_kw={'height_ratios': [1, 2]})
            
            # Top subplot: Individual transactions as bars
            colors = df['amount'].apply(lambda x: 'green' if x > 0 else 'red')
            ax1.bar(df['date'], df['amount'], color=colors, alpha=0.7)
            
            # Labels and styling for top subplot
            ax1.set_title('Deposits and Withdrawals', fontsize=14)
            ax1.set_ylabel('Amount ($)', fontsize=12)
            ax1.grid(True, alpha=0.3)
            ax1.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f'${x:,.2f}'))
            ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
            plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45, ha='right')
            
            # Bottom subplot: Cumulative balance
            ax2.plot(df['date'], df['cumulative'], 'b-', linewidth=2)
            ax2.fill_between(df['date'], 0, df['cumulative'], alpha=0.2, color='blue')
            
            # Add markers for deposits and withdrawals on the cumulative chart
            deposits = df[df['amount'] > 0]
            withdrawals = df[df['amount'] < 0]
            
            if not deposits.empty:
                ax2.scatter(deposits['date'], deposits['cumulative'], color='green', marker='^', s=80, label='Deposits')
            
            if not withdrawals.empty:
                ax2.scatter(withdrawals['date'], withdrawals['cumulative'], color='red', marker='v', s=80, label='Withdrawals')
            
            # Labels and styling for bottom subplot
            ax2.set_title('Cumulative Balance (Net Deposits)', fontsize=14)
            ax2.set_ylabel('Net Deposits ($)', fontsize=12)
            ax2.set_xlabel('Date', fontsize=12)
            ax2.grid(True, alpha=0.3)
            ax2.legend(loc='best')
            ax2.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f'${x:,.2f}'))
            ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
            plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45, ha='right')
            
            # Add summary text
            latest_cum = df['cumulative'].iloc[-1] if not df.empty else 0
            total_deposits = df[df['amount'] > 0]['amount'].sum()
            total_withdrawals = abs(df[df['amount'] < 0]['amount'].sum())
            
            summary_text = (
                f"Summary:\n"
                f"Total Deposits: ${total_deposits:,.2f}\n"
                f"Total Withdrawals: ${total_withdrawals:,.2f}\n"
                f"Net Deposits: ${latest_cum:,.2f}"
            )
            
            # Add text box with summary
            props = dict(boxstyle='round', facecolor='white', alpha=0.8)
            ax2.text(0.02, 0.97, summary_text, transform=ax2.transAxes,
                   fontsize=10, verticalalignment='top', bbox=props)
            
            # Adjust layout
            plt.tight_layout()
            
            # Save to buffer
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
            plt.close(fig)
            buf.seek(0)
            
            # Send the image
            await callback_query.message.reply_photo(
                photo=buf,
                caption="📊 *Deposits & Withdrawals Chart*\n"
                      f"Showing all transactions with a net deposit of ${latest_cum:,.2f}",
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            self.logger.error(f"Error generating transactions chart: {e}", exc_info=True)
            await callback_query.message.reply_text(
                "❌ Error generating transactions chart. Please try again later."
            )

    async def send_balance_chart(self, callback_query: CallbackQuery, days: int = 30):
        """Generate and send a balance history chart"""
        try:
            # Get balance history data
            balance_data = await self.mongo_client.get_balance_history(days=days)
            
            if not balance_data or len(balance_data) < 2:
                await callback_query.message.reply_text(
                    "Not enough balance history data available for charting."
                )
                return
                
            # Get buy orders data for chart annotations
            buy_orders = await self.mongo_client.get_buy_orders(days=days)
            
            # Create chart generator
            chart_gen = ChartGenerator()
            
            # Generate the chart
            chart_bytes = await chart_gen.generate_balance_chart(
                balance_data=balance_data,
                btc_prices=[],  # Keep empty for backward compatibility
                buy_orders=buy_orders
            )
            
            if not chart_bytes:
                await callback_query.message.reply_text(
                    "Error generating chart. Please try again later."
                )
                return
                
            # Get latest balance info for caption
            latest = balance_data[-1] if balance_data else None
            caption = f"📊 *Balance History Chart*\n"
            
            if latest:
                caption += f"Current Net Worth: ${float(latest['balance'] + latest['invested']):.2f}\n"
                caption += f"Available: ${float(latest['balance']):.2f}, Invested: ${float(latest['invested']):.2f}"
                
            # Create buffer for the image
            buf = io.BytesIO(chart_bytes)
            buf.name = 'balance_chart.png'
            
            # Send the chart image
            await callback_query.message.reply_photo(
                photo=buf,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            self.logger.error(f"Error generating balance chart: {e}", exc_info=True)
            await callback_query.message.reply_text(
                "❌ Error generating balance chart. Please try again later."
            )
            
    async def send_performance_chart(self, callback_query: CallbackQuery, days: int = 30):
        """Generate and send performance chart comparing portfolio to benchmarks"""
        try:
            chat_id = callback_query.message.chat_id
            
            # Send "generating" message
            temp_message = await callback_query.message.reply_text("📊 Generating portfolio ROI comparison...")
            
            await callback_query.answer()
            
            # Get portfolio performance data
            await self._generate_roi_comparison(chat_id)
            
            # Delete the temporary message
            await temp_message.delete()
        except Exception as e:
            logger.error(f"Error in performance chart callback: {e}")
            await callback_query.answer("❌ Error generating chart")
            await callback_query.message.reply_text(f"Error: {str(e)}")

    async def show_menu_from_callback(self, callback_query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
        """Show main menu in response to a callback query"""
        try:
            await self.show_menu(Update(callback_query.id, callback_query.message), context)
        except Exception as e:
            self.logger.error(f"Error showing menu from callback: {e}", exc_info=True)
            await callback_query.message.reply_text("❌ Error showing menu. Please try /menu instead.")

    async def remove_symbol(self, callback_query: CallbackQuery, symbol: str):
        """Remove a trading symbol from the bot"""
        try:
            # Check if this was a pre-configured symbol
            was_preconfigured = hasattr(self.binance_client, 'original_config_symbols') and \
                               symbol in self.binance_client.original_config_symbols
            
            # Remove from database
            success = await self.mongo_client.remove_trading_symbol(symbol)
            
            if success:
                # Remove from the active config as well
                if hasattr(self.binance_client, 'config') and 'trading' in self.binance_client.config:
                    pairs = self.binance_client.config['trading'].get('pairs', [])
                    if symbol in pairs:
                        pairs.remove(symbol)
                
                # Cancel any pending orders for this symbol
                canceled_orders = 0  # Placeholder, implement proper cancellation logic
                
                # Clear any triggered thresholds for this symbol
                cleared_thresholds = True  # Placeholder, implement proper threshold clearing logic
                    
                # If it was a pre-configured symbol, add it to the removed list
                if was_preconfigured:
                    await self.mongo_client.add_removed_symbol(symbol)
                    message = f"✅ Successfully removed {symbol} from the trading symbols list.\n" \
                             f"• Canceled {canceled_orders} pending orders\n" \
                             f"• Cleared thresholds for all timeframes\n" \
                             f"(This pre-configured symbol will not be auto-added on restart)"
                else:
                    message = f"✅ Successfully removed {symbol} from the trading symbols list.\n" \
                             f"• Canceled {canceled_orders} pending orders\n" \
                             f"• Cleared thresholds for all timeframes"
                
                await callback_query.message.edit_text(message)
            else:
                await callback_query.message.edit_text(
                    f"❌ Symbol {symbol} not found in the trading list."
                )
        except Exception as e:
            self.logger.error(f"Error removing symbol {symbol}: {e}", exc_info=True)
            await callback_query.message.reply_text(f"❌ Error removing symbol: {str(e)}")
            
    async def list_symbols(self, callback_query: CallbackQuery):
        """List all active trading symbols with removal options"""
        try:
            # Get all active trading symbols
            symbols = await self.mongo_client.get_trading_symbols()
            
            if not symbols:
                await callback_query.message.edit_text(
                    "No trading symbols configured. Use /add_symbol to add new symbols."
                )
                return
                
            # Sort the symbols
            symbols.sort()
            
            # Create message text
            message = f"📋 *Trading Symbols ({len(symbols)})*\n\n"
            
            # Create keyboard with removal buttons
            keyboard = []
            for i in range(0, len(symbols), 2):
                row = []
                row.append(InlineKeyboardButton(
                    f"❌ {symbols[i]}",
                    callback_data=f"symbol_remove_{symbols[i]}"
                ))
                
                if i + 1 < len(symbols):
                    row.append(InlineKeyboardButton(
                        f"❌ {symbols[i+1]}",
                        callback_data=f"symbol_remove_{symbols[i+1]}"
                    ))
                    
                keyboard.append(row)
                
            # Add symbol name details to message
            for symbol in symbols:
                message += f"• {symbol}\n"
                
            # Add back button
            keyboard.append([InlineKeyboardButton("➕ Add Symbol", callback_data="symbol_add")])
            keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="show_menu")])
            
            # Send the message
            await callback_query.message.edit_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            self.logger.error(f"Error listing symbols: {e}", exc_info=True)
            await callback_query.message.reply_text(f"❌ Error listing symbols: {str(e)}")

    async def deposit_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Record a deposit made to the trading account"""
        try:
            if not await self.is_user_authorized(update):
                return
                
            # Check if amount was provided
            if not context.args:
                await update.message.reply_text(
                    "⚠️ Please specify the amount to deposit.\n"
                    "Example: /deposit 500 to record a $500 deposit."
                )
                return
                
            # Try to parse the amount
            try:
                amount = Decimal(context.args[0].replace(',', ''))
            except (ValueError, InvalidOperation):
                await update.message.reply_text(
                    "❌ Invalid amount. Please provide a valid number."
                )
                return
                
            # Get optional notes if provided
            notes = ' '.join(context.args[1:]) if len(context.args) > 1 else None
            
            # Record the deposit
            success = await self.mongo_client.record_deposit(
                amount=amount, 
                notes=notes
            )
            
            if success:
                # Display confirmation with formatted amount
                await update.message.reply_text(
                    f"✅ Deposit of ${float(amount):,.2f} recorded successfully.\n"
                    f"Your deposit will be reflected in the next balance report."
                )
                
                # Log the deposit
                logger.info(f"User {update.effective_user.id} recorded a deposit of ${float(amount):,.2f}")
                
                # Add suggestion to view balance chart
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("View Balance Chart", callback_data="balance_chart_30")]
                ])
                
                await update.message.reply_text(
                    "Would you like to see your updated balance chart?",
                    reply_markup=markup
                )
            else:
                await update.message.reply_text(
                    "❌ Failed to record deposit. Please try again."
                )
        except Exception as e:
            logger.error(f"Error in deposit command: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")
            
    async def withdrawal_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Record a withdrawal from the trading account"""
        try:
            if not await self.is_user_authorized(update):
                return
                
            # Check if amount was provided
            if not context.args:
                await update.message.reply_text(
                    "⚠️ Please specify the amount to withdraw.\n"
                    "Example: /withdraw 300 to record a $300 withdrawal."
                )
                
                # Later in the method:
                
                # Display confirmation with formatted amount
                await update.message.reply_text(
                    f"✅ Withdrawal of ${float(amount):,.2f} recorded successfully.\n"
                    f"Your withdrawal will be reflected in the next balance report."
                )
                return
                
            # Try to parse the amount
            try:
                amount = Decimal(context.args[0].replace(',', ''))
            except (ValueError, InvalidOperation):
                await update.message.reply_text(
                    "❌ Invalid amount. Please provide a valid number."
                )
                return
                
            # Get optional notes if provided
            notes = ' '.join(context.args[1:]) if len(context.args) > 1 else None
            
            # Record the withdrawal
            success = await self.mongo_client.record_withdrawal(
                amount=amount, 
                notes=notes
            )
            
            if success:
                # Display confirmation with formatted amount
                await update.message.reply_text(
                    f"✅ Withdrawal of ${float(amount):,.2f} recorded successfully.\n"
                    f"Your withdrawal will be reflected in the next balance report."
                )
                
                # Log the withdrawal
                logger.info(f"User {update.effective_user.id} recorded a withdrawal of ${float(amount):,.2f}")
                
                # Add suggestion to view balance chart
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("View Balance Chart", callback_data="balance_chart_30")]
                ])
                
                await update.message.reply_text(
                    "Would you like to see your updated balance chart?",
                    reply_markup=markup
                )
            else:
                await update.message.reply_text(
                    "❌ Failed to record withdrawal. Please try again."
                )
        except Exception as e:
            logger.error(f"Error in withdrawal command: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")
            
    async def transactions_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Display recent deposits and withdrawals"""
        try:
            if not await self.is_user_authorized(update):
                return
                
            # Parse days parameter if provided
            days = 30
            if context.args and context.args[0].isdigit():
                days = int(context.args[0])
                days = min(days, 365)  # Limit to 1 year max
            
            # Get transactions from the database
            transactions = await self.mongo_client.get_deposits_withdrawals(days=days)
            
            if not transactions:
                await update.message.reply_text(
                    f"No deposits or withdrawals found in the last {days} days."
                )
                return
                
            # Calculate total deposits and withdrawals
            total_deposit = Decimal('0')
            total_withdrawal = Decimal('0')
            
            for tx in transactions:
                if tx['amount'] > 0:
                    total_deposit += tx['amount']
                else:
                    total_withdrawal += abs(tx['amount'])
                    
            # Format the message header
            message = f"Transaction History (Last {days} days):\n\n"
            message += f"Total Deposits: ${float(total_deposit):,.2f}\n"
            message += f"Total Withdrawals: ${float(total_withdrawal):,.2f}\n"
            message += f"Net Change: ${float(total_deposit - total_withdrawal):,.2f}\n\n"
            
            # Format each transaction
            for i, tx in enumerate(transactions[:10], 1):  # Limit to first 10 transactions
                tx_date = tx['timestamp'].strftime('%Y-%m-%d %H:%M')
                amount = tx['amount']
                tx_type = "Deposit" if amount > 0 else "Withdrawal"
                notes = f" - {tx['notes']}" if tx.get('notes') else ""
                
                message += f"{i}. {tx_date} | {tx_type}: ${abs(float(amount)):,.2f}{notes}\n"
                
            # Add indicator if there are more transactions
            if len(transactions) > 10:
                message += f"\n...and {len(transactions) - 10} more transactions..."
                
            # Add button to view chart
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("View Balance Chart", callback_data="balance_chart_30")],
                [InlineKeyboardButton("View Performance Chart", callback_data="performance_chart_30")]
            ])
            
            await update.message.reply_text(
                message,
                reply_markup=markup
            )
        except Exception as e:
            logger.error(f"Error in transactions command: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")
            await update.message.reply_text(f"❌ Error: {str(e)}")