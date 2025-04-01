import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes, 
    ConversationHandler, CallbackQueryHandler, MessageHandler,
    filters
)
from datetime import datetime, timedelta
import logging
from decimal import Decimal, InvalidOperation  # Add InvalidOperation for exception handling
from typing import List, Optional, Dict  # Add Dict import
from ..types.models import Order, OrderStatus, TimeFrame, OrderType, TradeDirection, TPSLStatus, PartialTakeProfit  # Update imports
from ..trading.binance_client import BinanceClient
from ..database.mongo_client import MongoClient
import io  # Add for handling bytes from chart
from ..types.constants import NOTIFICATION_EMOJI
from ..utils.chart_generator import ChartGenerator  # Add this import

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
                reply_markup=self.markup,
                parse_mode=ParseMode.HTML
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
                    balances.append(f"{prefix}{asset}: {total:.8f}")
                
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
            response = "💰 Current Balance:\n" + "\n".join(sorted_balances)
            
            # Send response
            await update.message.reply_text(response)
            
        except Exception as e:
            logging.error(f"Error getting balance: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")

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
            # Get last 5 orders
            cursor = self.mongo_client.orders.find().sort("created_at", -1).limit(5)
            orders = []
            async for doc in cursor:
                orders.append(
                    f"{doc['symbol']} - {doc['status']}\n"
                    f"Price: {doc['price']} Amount: {doc['quantity']}\n"
                    f"Created: {doc['created_at'].strftime('%Y-%m-%d %H:%M:%S')}"
                )
                
            message = "📜 Recent Orders:\n\n" + "\n\n".join(orders)
            await update.message.reply_text(message)
        except Exception as e:
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
                # Price check buttons
                [
                    KeyboardButton(f"/price BTC{base_currency}"), 
                    KeyboardButton(f"/price ETH{base_currency}"), 
                    KeyboardButton(f"/price BNB{base_currency}")
                ],
                # Info commands
                [
                    KeyboardButton("/balance"), 
                    KeyboardButton("/status"), 
                    KeyboardButton("/orders")
                ],
                # Trading commands
                [
                    KeyboardButton("/symbols"), 
                    KeyboardButton("/help")
                ],
                # Control commands
                [
                    KeyboardButton("/pausebot"), 
                    KeyboardButton("/resumebot")
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
            
            # Add TP/SL management commands
            keyboard.append([
                KeyboardButton("/showpartialtps"),
                KeyboardButton("/showtrailingsl")
            ])
            
            # Add enable/disable buttons
            keyboard.append([
                KeyboardButton("/enablepartialtps"),
                KeyboardButton("/enabletrailingsl"),
                KeyboardButton("/enablelowerentries")
            ])
            
            keyboard.append([
                KeyboardButton("/disablepartialtps"),
                KeyboardButton("/disabletrailingsl"),
                KeyboardButton("/disablelowerentries")
            ])
            
            # Create reply markup
            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            
            # Send menu message
            await update.message.reply_text(
                "Trade-a-saurus Rex Menu:",
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
        """Show data visualization options"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        keyboard = [
            [InlineKeyboardButton("📊 Daily Volume", callback_data=VisualizationType.DAILY_VOLUME)],
            [InlineKeyboardButton("💰 Profit Distribution", callback_data=VisualizationType.PROFIT_DIST)],
            [InlineKeyboardButton("📈 Order Types", callback_data=VisualizationType.ORDER_TYPES)],
            [InlineKeyboardButton("⏰ Hourly Activity", callback_data=VisualizationType.HOURLY_ACTIVITY)],
            [InlineKeyboardButton("💹 Balance History", callback_data=VisualizationType.BALANCE_CHART)],
            [InlineKeyboardButton("🔄 ROI Comparison", callback_data=VisualizationType.ROI_COMPARISON)],
            [InlineKeyboardButton("⚔️ S&P 500 vs BTC (YTD)", callback_data=VisualizationType.SP500_VS_BTC)],
            [InlineKeyboardButton("🥧 Portfolio Composition", callback_data=VisualizationType.PORTFOLIO_COMPOSITION)]
        ]
        
        await update.message.reply_text(
            "📊 Select Data Visualization:",
            reply_markup=InlineKeyboardMarkup(keyboard)
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
            
        # Special handling for S&P 500 vs BTC comparison
        if viz_type == VisualizationType.SP500_VS_BTC:
            await query.message.reply_text("Generating S&P 500 vs BTC year-to-date comparison...", reply_markup=self.markup)
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
                text=message,
                parse_mode=ParseMode.HTML
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
                text=message,
                parse_mode=ParseMode.HTML
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
        """Generate and send S&P 500 vs BTC year-to-date comparison chart"""
        try:
            # Get current year
            current_year = datetime.now().year
            start_date = datetime(current_year, 1, 1)
            days_since_start = (datetime.now() - start_date).days
            
            # Get BTC data for current year
            btc_data = await self.binance_client.get_historical_prices("BTCUSDT", days_since_start + 5)  # Add buffer days
            
            if not btc_data or len(btc_data) < 2:
                await self.application.bot.send_message(
                    chat_id=chat_id,
                    text="Not enough BTC price data for year-to-date comparison.",
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
            
            # Get S&P 500 data from Yahoo scraper
            sp500_data = await self.binance_client.yahoo_scraper.get_sp500_data(days_since_start + 5)
            
            if not sp500_data or len(sp500_data) < 2:
                await self.application.bot.send_message(
                    chat_id=chat_id,
                    text="Failed to get S&P 500 data for year-to-date comparison.",
                    reply_markup=self.markup
                )
                return
                
            # Filter S&P 500 data to only include this year
            sp500_ytd = {}
            first_date = None
            first_value = 0
            
            # Sort the dates to find the earliest one in the current year
            dates = sorted(sp500_data.keys())
            for date in dates:
                year = int(date.split('-')[0])
                if year == current_year:
                    if first_date is None:
                        first_date = date
                        first_value = sp500_data[date]
                    
                    # Adjust values to be relative to the first day of the year
                    sp500_ytd[date] = sp500_data[date] - first_value
            
            # Create the comparison chart
            chart_bytes = await self._create_ytd_comparison_chart(btc_ytd_prices, sp500_ytd, current_year)
            
            if not chart_bytes:
                await self.application.bot.send_message(
                    chat_id=chat_id,
                    text="Failed to generate comparison chart.",
                    reply_markup=self.markup
                )
                return
                
            # Get current values for caption
            btc_current = list(btc_ytd_prices.values())[-1] if btc_ytd_prices else 0
            sp500_current = list(sp500_ytd.values())[-1] if sp500_ytd else 0
            
            # Send the chart
            await self.application.bot.send_photo(
                chat_id=chat_id,
                photo=chart_bytes,
                caption=f"📈 {current_year} Year-to-Date Performance Comparison\n\n"
                        f"🟠 Bitcoin: {btc_current:.2f}%\n"
                        f"🔵 S&P 500: {sp500_current:.2f}%\n\n"
                        f"Chart shows percentage change since January 1, {current_year}",
                reply_markup=self.markup
            )
            
        except Exception as e:
            logger.error(f"Error generating S&P 500 vs BTC comparison: {e}", exc_info=True)
            await self.application.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Error generating comparison chart: {str(e)}",
                reply_markup=self.markup
            )
            
    async def _create_ytd_comparison_chart(self, btc_data: dict, sp500_data: dict, year: int) -> Optional[bytes]:
        """Create year-to-date comparison chart between BTC and S&P 500"""
        try:
            import matplotlib.pyplot as plt
            from matplotlib.ticker import FuncFormatter
            import matplotlib.dates as mdates
            import pandas as pd
            import io

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
            
            # Add zero line
            ax.axhline(y=0, color='gray', linestyle='--', alpha=0.7)
            
            # Format chart
            ax.set_title(f'BTC vs S&P 500 Year-to-Date Performance ({year})')
            ax.set_ylabel('YTD Change (%)')
            ax.grid(True, alpha=0.3)
            
            # Format y-axis as percentage
            ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f'{x:.1f}%'))
            
            # Format x-axis to show months
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%b'))
            ax.xaxis.set_major_locator(mdates.MonthLocator())
            
            # Add legend
            ax.legend(loc='best')
            
            # Get final values for annotation
            if not btc_df.empty:
                final_btc = btc_df['value'].iloc[-1]
                ax.annotate(f"{final_btc:.1f}%", 
                          xy=(btc_df.index[-1], final_btc),
                          xytext=(5, 5), textcoords='offset points')
            
            if not sp500_df.empty:
                final_sp500 = sp500_df['value'].iloc[-1]
                ax.annotate(f"{final_sp500:.1f}%", 
                          xy=(sp500_df.index[-1], final_sp500),
                          xytext=(5, -15), textcoords='offset points')
            
            # Save to buffer
            buf = io.BytesIO()
            plt.tight_layout()
            plt.savefig(buf, format='png', dpi=150)
            plt.close(fig)
            buf.seek(0)
            
            return buf.getvalue()
            
        except Exception as e:
            logger.error(f"Error creating YTD comparison chart: {e}", exc_info=True)
            return None

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
        """Handle callback for symbol management"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        
        if data.startswith("remove_symbol:"):
            symbol = data.split(":")[1]
            
            # Check if this was a pre-configured symbol
            was_preconfigured = hasattr(self.binance_client, 'original_config_symbols') and \
                               symbol in self.binance_client.original_config_symbols
            
            # Remove from database
            success = await self.mongo_client.remove_trading_symbol(symbol)
            
            if success:
                # Remove from the active config as well
                if symbol in self.binance_client.config['trading']['pairs']:
                    self.binance_client.config['trading']['pairs'].remove(symbol)
                
                # Cancel any pending orders for this symbol
                canceled_orders = await self._cancel_symbol_orders(symbol)
                
                # Clear any triggered thresholds for this symbol
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
                
                await query.edit_message_text(message)
            else:
                await query.edit_message_text(
                    f"❌ Symbol {symbol} not found in the trading list."
                )

    # Add helper methods to cancel orders and clear thresholds
    async def _cancel_symbol_orders(self, symbol: str) -> int:
        """Cancel all pending orders for a specific symbol"""
        try:
            # Find all pending orders for this symbol
            pending_orders = await self.mongo_client.orders.find({
                "symbol": symbol,
                "status": "pending"
            }).to_list(None)
            
            cancelled_count = 0
            skipped_count = 0
            for order in pending_orders:
                order_id = order.get("order_id")
                if order_id:
                    try:
                        # Try to cancel on Binance
                        if await self.binance_client.cancel_order(symbol, order_id):
                            # Update status in database
                            await self.mongo_client.update_order_status(
                                order_id, 
                                OrderStatus.CANCELLED,
                                cancelled_at=datetime.utcnow()
                            )
                            cancelled_count += 1
                        else:
                            # If cancel_order returns False, still update DB status
                            await self.mongo_client.update_order_status(
                                order_id, 
                                OrderStatus.CANCELLED,
                                cancelled_at=datetime.utcnow()
                            )
                            cancelled_count += 1
                    except Exception as e:
                        # Handle "Unknown order" errors by updating DB anyway
                        if "Unknown order sent" in str(e) or "code=-2011" in str(e):
                            logging.warning(f"Order {order_id} already cancelled or filled on exchange, updating database")
                            await self.mongo_client.update_order_status(
                                order_id, 
                                OrderStatus.CANCELLED,
                                cancelled_at=datetime.utcnow()
                            )
                            cancelled_count += 1
                        else:
                            logging.error(f"Error cancelling order {order_id}: {e}")
                            skipped_count += 1
                        
            # Return the results
            if skipped_count > 0:
                logging.warning(f"Cancelled {cancelled_count} orders, skipped {skipped_count} orders due to errors")
            return cancelled_count
        except Exception as e:
            logging.error(f"Error canceling orders for {symbol}: {e}")
            return 0

    async def _clear_symbol_thresholds(self, symbol: str) -> bool:
        """Clear all triggered thresholds for a specific symbol"""
        try:
            # Clear from database - update to be more thorough
            for timeframe in TimeFrame:
                # Delete from threshold_state collection
                await self.mongo_client.threshold_state.delete_one({
                    "symbol": symbol,
                    "timeframe": timeframe.value
                })
                
                # Also clear from triggered_thresholds collection
                await self.mongo_client.triggered_thresholds.delete_one({
                    "symbol": symbol,
                    "timeframe": timeframe.value
                })
            
            # Clear from BinanceClient memory
            if hasattr(self.binance_client, 'triggered_thresholds') and symbol in self.binance_client.triggered_thresholds:
                del self.binance_client.triggered_thresholds[symbol]
                
            # Also remove from reference_prices if it exists there
            if hasattr(self.binance_client, 'reference_prices') and symbol in self.binance_client.reference_prices:
                for timeframe in TimeFrame:
                    timeframe_key = f"{symbol}_{timeframe.value}"
                    if timeframe_key in self.binance_client.reference_prices:
                        del self.binance_client.reference_prices[timeframe_key]
                    
            # Add to a "blacklist" for the current trading cycle to prevent immediate reprocessing
            if not hasattr(self.binance_client, 'removed_symbols_this_cycle'):
                self.binance_client.removed_symbols_this_cycle = set()
            self.binance_client.removed_symbols_this_cycle.add(symbol)
                
            return True
        except Exception as e:
            logging.error(f"Error clearing thresholds for {symbol}: {e}")
            return False

    async def send_partial_tp_notification(self, order: Order, partial_tp: PartialTakeProfit):
        """Send notification when a partial take profit level is triggered"""
        if not self.chat_id:
            logger.warning("Cannot send partial TP notification: No chat ID available")
            return
            
        try:
            emoji = "🎯"
            current_price = await self.binance_client.get_current_price(order.symbol)
            
            # Calculate profit details
            entry_price = float(order.price)
            tp_price = float(partial_tp.price)
            position_percentage = partial_tp.position_percentage
            profit_percentage = partial_tp.profit_percentage
            
            # Calculate the amount being sold at this level
            original_quantity = float(order.quantity)
            sell_quantity = original_quantity * (position_percentage / 100)
            
            # Calculate profit in currency
            profit_per_unit = tp_price - entry_price
            total_profit = profit_per_unit * sell_quantity
            
            message = (
                f"{emoji} *Partial Take Profit Triggered - Level {partial_tp.level}* {emoji}\n\n"
                f"Symbol: `{order.symbol}`\n"
                f"Entry Price: `${entry_price:.4f}`\n"
                f"TP Price: `${tp_price:.4f}` (+{profit_percentage:.2f}%)\n"
                f"Current Price: `${current_price:.4f}`\n\n"
                f"Position: {position_percentage:.0f}% of holdings "
                f"({sell_quantity:.6f} {order.symbol.replace(self.binance_client.base_currency, '')})\n"
                f"Profit: `${total_profit:.2f}` (+{profit_percentage:.2f}%)\n\n"
                f"🦖 *RAWR!* Successfully locked in partial profits!"
            )
            
            await self.send_message(self.chat_id, message, parse_mode="Markdown")
            
        except Exception as e:
            logger.error(f"Error sending partial TP notification: {e}", exc_info=True)
            
    async def show_partial_tp(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show current partial take profit settings"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        try:
            # Get current partial TP configuration
            partial_tp_config = self.config['trading']['partial_take_profits']
            enabled = partial_tp_config.get('enabled', False)
            
            # Get the levels
            levels = []
            if 'levels' in partial_tp_config:
                for level_num, level in enumerate(partial_tp_config['levels'], 1):
                    levels.append({
                        'level': level_num,
                        'position_percentage': level.get('position_percentage', 0),
                        'profit_percentage': level.get('profit_percentage', 0)
                    })
            
            # Create status message
            message = f"🎯 *Partial Take Profit Settings*\n\n"
            message += f"Status: {'✅ Enabled' if enabled else '❌ Disabled'}\n\n"
            
            if levels:
                message += "*Configured Levels:*\n"
                for level in levels:
                    message += f"- Level {level['level']}: {level['position_percentage']}% of position at +{level['profit_percentage']}% profit\n"
            else:
                message += "*No levels configured*\n"
                
            # Add command examples
            message += "\n*Available Commands:*\n"
            message += "`/partial_tp_enable` - Enable partial take profits\n"
            message += "`/partial_tp_disable` - Disable partial take profits\n"
            message += "`/set_partial_tp <level> <position_percentage> <profit_percentage>` - Set a partial TP level\n"
            message += "Example: `/set_partial_tp 1 25 1.5` sets level 1 to sell 25% of position at +1.5% profit"
            
            await update.message.reply_text(message, parse_mode="Markdown")
            
        except Exception as e:
            logger.error(f"Error showing partial TP settings: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Error: {str(e)}")
            
    async def partial_tp_enable(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Enable partial take profits"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        try:
            # Update the config
            self.config['trading']['partial_take_profits']['enabled'] = True
            
            # Save to database
            await self.mongo_client.update_trading_setting('partial_take_profits.enabled', True)
            
            # Get the number of configured levels
            levels = self.config['trading']['partial_take_profits'].get('levels', [])
            
            # Send confirmation
            await update.message.reply_text(
                f"✅ Partial take profits have been enabled.\n"
                f"Currently {len(levels)} levels are configured.\n"
                f"Use `/show_partial_tp` to see current settings."
            )
            
        except Exception as e:
            logger.error(f"Error enabling partial take profits: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Error: {str(e)}")
            
    async def partial_tp_disable(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Disable partial take profits"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        try:
            # Update the config
            self.config['trading']['partial_take_profits']['enabled'] = False
            
            # Save to database
            await self.mongo_client.update_trading_setting('partial_take_profits.enabled', False)
            
            # Send confirmation
            await update.message.reply_text(
                f"❌ Partial take profits have been disabled.\n"
                f"Configured levels are preserved but will not be triggered.\n"
                f"Use `/partial_tp_enable` to re-enable."
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
                
            # Get current levels
            if 'levels' not in self.config['trading']['partial_take_profits']:
                self.config['trading']['partial_take_profits']['levels'] = []
                
            levels = self.config['trading']['partial_take_profits']['levels']
            
            # Check for level index
            level_index = level - 1  # Convert to 0-based index
            
            # Update or add the level
            level_data = {
                'position_percentage': position_percentage,
                'profit_percentage': profit_percentage
            }
            
            if level_index < len(levels):
                # Update existing level
                levels[level_index] = level_data
            else:
                # Add new level, fill gaps with empty levels if needed
                while len(levels) < level_index:
                    levels.append({'position_percentage': 0, 'profit_percentage': 0})
                levels.append(level_data)
                
            # Update config
            self.config['trading']['partial_take_profits']['levels'] = levels
            
            # Save to database
            await self.mongo_client.update_trading_setting('partial_take_profits.levels', levels)
            
            # Check total position percentage
            total_percentage = sum(level.get('position_percentage', 0) for level in levels)
            warning = ""
            if total_percentage > 100:
                warning = "\n⚠️ Warning: Total position percentage exceeds 100%!"
            
            # Send confirmation
            await update.message.reply_text(
                f"✅ Partial take profit level {level} has been set:\n"
                f"- Sell {position_percentage}% of position at +{profit_percentage}% profit.{warning}\n\n"
                f"Use `/show_partial_tp` to see all settings."
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
        """Register command handlers"""
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("menu", self.show_menu))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler("balance", self.balance_command))
        self.application.add_handler(CommandHandler("orders", self.orders_command))
        self.application.add_handler(CommandHandler("symbols", self.symbols_command))

    async def is_user_authorized(self, update: Update) -> bool:
        """Check if user is authorized to use the bot"""
        user_id = update.effective_user.id
        if not self._is_authorized(user_id):
            await update.message.reply_text("You're not authorized to use this bot.")
            return False
        return True

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send help message"""
        try:
            if not await self.is_user_authorized(update):
                return
                
            help_text = """
<b>Trade-a-saurus Rex Bot Help</b>

<b>🔍 Information Commands:</b>
/menu - Show main menu
/help - Show this help message
/status - Check bot status
/balance - Check account balance
/orders - List active orders
/symbols - List configured symbols
/price [symbol] - Get current price for symbol

<b>📊 Trading Commands:</b>
/buy [symbol] [amount] - Create buy order
/sell [symbol] [amount] - Create sell order
/buylimit [symbol] [price] [amount] - Place limit buy order
/selllimit [symbol] [price] [amount] - Place limit sell order
/marketbuy [symbol] [amount] - Place market buy order
/marketsell [symbol] [amount] - Place market sell order
/cancel [orderid] - Cancel order

<b>⚙️ Configuration Commands:</b>
/pausebot - Pause trading
/resumebot - Resume trading
/setdefault [symbol] [amount] - Set default order amount
/settakeproft [percentage] - Set default take profit %
/setstoploss [percentage] - Set default stop loss %
/setleverage [value] - Set futures leverage
/setmargin [type] - Set margin type (ISOLATED/CROSS)
/setmode [mode] - Set trading mode (spot/futures)
/setheartbeat [minutes] - Set heartbeat interval
/enabletrading - Enable trading
/disabletrading - Disable trading
/enableaddorders - Enable adding new orders
/disableaddorders - Disable adding new orders

<b>📉 Lower Entries Protection:</b>
/enablelowerentries - Enable only lower entries protection
/disablelowerentries - Disable only lower entries protection
/setlowerentries [on/off] - Set lower entries protection

<b>📈 Partial Take Profit Commands:</b>
/showpartialtps - Show partial TP settings
/enablepartialtps - Enable partial take profits
/disablepartialtps - Disable partial take profits
/setpartialtps [level] [percentage] [position%] - Set partial TP level
  Example: /setpartialtps 1 2.5 25 - Level 1, +2.5% from entry, 25% of position

<b>🛑 Trailing Stop Loss Commands:</b>
/showtrailingsl - Show trailing SL settings
/enabletrailingsl - Enable trailing stop loss
/disabletrailingsl - Disable trailing stop loss
/settrailingsl [activation] [callback] - Set trailing SL parameters
  Example: /settrailingsl 3 1.5 - Activate at +3% profit, 1.5% callback
"""
            
            await update.message.reply_text(
                text=help_text,
                parse_mode=ParseMode.HTML,
                reply_markup=self.markup
            )
            
        except Exception as e:
            logger.error(f"Error sending help: {e}")
            await update.message.reply_text("Error sending help message")