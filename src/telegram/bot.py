import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, ContextTypes, 
    ConversationHandler, CallbackQueryHandler, MessageHandler,
    filters
)
from datetime import datetime, timedelta
import logging
from decimal import Decimal  # Add this import
from typing import List, Optional, Dict  # Add Dict import
from ..types.models import Order, OrderStatus, TimeFrame, OrderType, TradeDirection  # Update imports
from ..trading.binance_client import BinanceClient
from ..database.mongo_client import MongoClient

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
SYMBOL, ORDER_TYPE, LEVERAGE, DIRECTION, AMOUNT, PRICE, FEES = range(7)

class VisualizationType:
    DAILY_VOLUME = "daily_volume"
    PROFIT_DIST = "profit_distribution"
    ORDER_TYPES = "order_types"
    HOURLY_ACTIVITY = "hourly_activity"

class TelegramBot:
    def __init__(self, token: str, allowed_users: List[int], 
                 binance_client: BinanceClient, mongo_client: MongoClient,
                 config: dict):  # Add config parameter
        self.token = token
        self.allowed_users = allowed_users
        self.binance_client = binance_client
        self.mongo_client = mongo_client
        self.config = config  # Add this line
        self.app = None
        self.is_paused = False
        self.running = False
        self._polling_task = None
        self._update_id = 0
        self.temp_trade_data = {}
        
        # Simplified keyboard with only essential commands
        self.keyboard = [
            [KeyboardButton("/menu"), KeyboardButton("/balance")],
            [KeyboardButton("/trade"), KeyboardButton("/stats")],
            [KeyboardButton("/toggle"), KeyboardButton("/viz")]
        ]
        
        self.markup = ReplyKeyboardMarkup(self.keyboard, resize_keyboard=True)
        self.startup_message = f"""
{DINO_ASCII}

🦖 Trade-a-saurus Rex Bot

Your friendly neighborhood trading dinosaur is online!
Use /menu to see available commands.

Status: Ready to ROAR! 🦖
"""
        self.binance_client.set_telegram_bot(self)  # Add this line
        self.sent_roars = set()  # Add this to track sent roar notifications
        # Add environment info
        self.env_info = (
            "📍 Environment: "
            f"{'Testnet' if config.get('environment', {}).get('testnet', True) else 'Mainnet'} | "
            f"{config['trading']['base_currency']}"
        )
        # Add menu callback patterns
        self.MENU_PATTERNS = {
            'main': 'menu_main',
            'account': 'menu_account',
            'trading': 'menu_trading',
            'analysis': 'menu_analysis',
            'settings': 'menu_settings'
        }

    async def initialize(self):
        """Initialize the bot with essential handlers"""
        try:
            # Create application
            self.app = Application.builder().token(self.token).build()
            
            # Add core command handlers
            self.app.add_handler(CommandHandler("start", self.start_command))
            self.app.add_handler(CommandHandler("menu", self.show_main_menu))
            self.app.add_handler(CommandHandler("balance", self.get_balance_command))
            self.app.add_handler(CommandHandler("stats", self.get_stats))
            self.app.add_handler(CommandHandler("history", self.get_order_history))
            self.app.add_handler(CommandHandler("profits", self.show_profits))
            self.app.add_handler(CommandHandler("viz", self.show_viz_menu))
            self.app.add_handler(CommandHandler("trade", self.add_trade_start))
            self.app.add_handler(CommandHandler("thresholds", self.show_thresholds))
            self.app.add_handler(CommandHandler("toggle", self.toggle_trading))
            
            # Add callback query handlers
            self.app.add_handler(CallbackQueryHandler(self.handle_menu_callback, pattern="^(section_|main_menu|toggle_trading)"))
            self.app.add_handler(CallbackQueryHandler(self.handle_viz_selection, pattern="^viz_"))
            self.app.add_handler(CallbackQueryHandler(self.handle_mode_switch, pattern="^mode_"))
            self.app.add_handler(CallbackQueryHandler(self.handle_submenu_callback, pattern="^confirm_"))
            
            # Add conversation handlers for multi-step processes
            # Add trade conversation
            add_trade_conv = ConversationHandler(
                entry_points=[
                    CommandHandler("trade", self.add_trade_start),
                    CallbackQueryHandler(self.add_trade_start, pattern="^add_trade$")
                ],
                states={
                    1: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_symbol)],
                    2: [CallbackQueryHandler(self.add_trade_order_type)],
                    3: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_leverage)],
                    4: [CallbackQueryHandler(self.add_trade_direction)],
                    5: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_amount)],
                    6: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_fees)],
                    7: [CallbackQueryHandler(self.add_trade_final)]
                },
                fallbacks=[
                    CommandHandler("cancel", self.add_trade_cancel),
                    CallbackQueryHandler(self.add_trade_cancel, pattern="^cancel$")
                ]
            )
            self.app.add_handler(add_trade_conv)
            
            # Add error handler
            self.app.add_error_handler(self.error_handler)
            
            logger.info("Telegram bot initialized with essential handlers")
            return True
            
        except Exception as e:
            logger.error(f"Failed to initialize Telegram bot: {e}")
            return False

    async def start(self):
        """Start the bot and begin polling with improved error handling"""
        self.running = True
        reconnect_delay = 1  # Initial delay in seconds
        max_delay = 300  # Maximum delay of 5 minutes
        consecutive_errors = 0
        
        # Send startup message
        for user_id in self.allowed_users:
            try:
                await self.app.bot.send_message(
                    chat_id=user_id, 
                    text=self.startup_message,
                    reply_markup=self.markup
                )
            except Exception as e:
                logger.error(f"Failed to send startup message to {user_id}: {e}")
        
        # Start polling with automatic recovery
        while self.running:
            try:
                updates = await self.app.bot.get_updates(
                    offset=self._update_id,
                    timeout=30
                )
                
                # Reset error counter and delay on successful update
                consecutive_errors = 0
                reconnect_delay = 1
                
                for update in updates:
                    if update.update_id >= self._update_id:
                        self._update_id = update.update_id + 1
                        await self.app.process_update(update)
                        
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Polling error: {e}")
                
                # Send error notification if too many consecutive errors
                if consecutive_errors >= 5:
                    error_msg = (
                        "⚠️ Bot Connection Warning!\n\n"
                        f"Consecutive errors: {consecutive_errors}\n"
                        f"Last error: {str(e)}\n"
                        f"Next retry in {reconnect_delay}s"
                    )
                    for user_id in self.allowed_users:
                        try:
                            await self.app.bot.send_message(
                                chat_id=user_id,
                                text=error_msg
                            )
                        except Exception as notify_error:
                            logger.error(f"Failed to send error notification: {notify_error}")
                
                # Exponential backoff with maximum delay
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_delay)
                
            await asyncio.sleep(0.1)

    async def stop(self):
        """Stop the Telegram bot with proper cleanup"""
        self.running = False
        if self.app:
            try:
                # Close any pending client sessions
                if hasattr(self.app.bot, '_client_session'):
                    await self.app.bot._client_session.close()
                await self.app.stop()
            except Exception as e:
                logger.error(f"Error during bot shutdown: {e}")

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle the /start command"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        current_mode = self.config['environment']['trading_mode'].upper()
        menu_text = f"""
🦖 Trade-a-saurus Rex is Ready!

Current Mode: {current_mode} 🔄
/mode - Switch between SPOT/FUTURES

Account Info:
/balance - Show {current_mode} balance
/positions - Show open positions
/stats - View trading statistics
/profits - View profit/loss

Trading Actions:
/add - Add new trade
/power - Toggle trading on/off

Market Analysis:
/viz - Data visualizations
/thresholds - Show threshold status
/history - Order history
"""

        # Add futures-specific commands
        if current_mode == "FUTURES":
            menu_text += """
Futures Settings:
/leverage - Set leverage per pair
/margin - Set margin type (ISOLATED/CROSSED)
/hedge - Toggle hedge mode (ONE-WAY/HEDGE)
"""

        await update.message.reply_text(
            menu_text,
            reply_markup=self.markup
        )

    def _is_authorized(self, user_id: int) -> bool:
        """Check if user is authorized"""
        return user_id in self.allowed_users

    async def toggle_trading(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Toggle trading state between paused and active"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        # Check reserve balance before resuming
        if not self.is_paused:
            current_balance = await self.binance_client.get_balance('USDT')
            reserve_balance = self.binance_client.reserve_balance or 0  # Default to 0 if None
            
            if float(current_balance) < reserve_balance:
                await update.message.reply_text(
                    "❌ Cannot resume trading: Balance below reserve requirement\n"
                    f"Current: ${float(current_balance):.2f}\n"
                    f"Required: ${reserve_balance:.2f}"
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

    async def get_balance_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Command handler for balance checking with futures support"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        try:
            current_mode = self.config['environment']['trading_mode'].upper()
            response = [f"💰 Balance Overview ({current_mode} Mode)"]
            
            # Get balance based on mode
            if current_mode == "FUTURES":
                account = await self.binance_client.get_account_info()
                
                # Add balance information
                total_margin = float(account['totalWalletBalance'])
                unrealized_pnl = float(account['totalUnrealizedProfit'])
                available_balance = float(account['availableBalance'])
                
                response.extend([
                    f"\n📈 Futures Account:",
                    f"Total Margin: ${total_margin:.2f}",
                    f"Available Balance: ${available_balance:.2f}",
                    f"Unrealized P/L: ${unrealized_pnl:+.2f}"
                ])
                
                # Add position information
                positions = account.get('positions', [])
                if positions:
                    response.append("\nOpen Positions:")
                    for pos in positions:
                        amt = float(pos['positionAmt'])
                        if amt != 0:
                            direction = "LONG" if amt > 0 else "SHORT"
                            size = abs(amt)
                            entry = float(pos['entryPrice'])
                            response.append(
                                f"\n{pos['symbol']}:\n"
                                f"• {direction} {size:.4f} @ ${entry:.2f}\n"
                                f"• Leverage: {pos['leverage']}x"
                            )
            else:
                # Existing spot balance code
                spot_balance = await self.binance_client.get_balance()
                response.extend([
                    f"\n💱 Spot Balance:",
                    f"• USDT: ${float(spot_balance):.2f}"
                ])

            # Add reserve balance info
            response.extend([
                f"\n📝 Reserve Balance: ${self.binance_client.reserve_balance:.2f}",
                f"Trading Status: {'Paused ⏸' if self.is_paused else 'Active ▶️'}"
            ])
                
            await update.message.reply_text("\n".join(response))
            
        except Exception as e:
            logger.error(f"Error getting balance: {e}")
            await update.message.reply_text(f"❌ Error getting balance: {str(e)}")

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
        """Send order notification with environment info"""
        if not self.app:
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
        
        # Calculate total value in USDT
        total_value = order.price * order.quantity
        
        # Include environment info in message
        message = (
            f"{self.env_info}\n\n"
            f"{emoji[status]} Order Update\n"
            f"Order ID: {order.order_id}\n"
            f"Symbol: {order.symbol}\n"
            f"Status: {status.value.upper()}\n"
            f"Amount: {float(order.quantity):.8f} {order.symbol.replace('USDT', '')}\n"
            f"Price: ${float(order.price):.2f}\n"
            f"Total: ${float(total_value):.2f} USDT\n"  # Fixed format specifier
            f"Threshold: {order.threshold if order.threshold else 'Manual'}\n"
            f"Timeframe: {self._get_timeframe_value(order.timeframe)}"
        )

        if status == OrderStatus.FILLED:
            message += f"\nFees: ${float(order.fees)::.4f} {order.fee_asset}"
            
        if status == OrderStatus.CANCELLED and order.cancelled_at:
            duration = order.cancelled_at - order.created_at
            message += f"\nDuration: {duration.total_seconds() / 3600:.2f} hours"

        for user_id in self.allowed_users:
            try:
                await self.app.bot.send_message(chat_id=user_id, text=message)
            except Exception as e:
                logger.error(f"Failed to send notification to {user_id}: {e}")

    async def send_balance_update(self, symbol: str, change: Decimal):
        """Send balance change notification with environment info"""
        message = (
            f"{self.env_info}\n\n"
            f"💰 Balance Update\n"
            f"Symbol: {symbol}\n"
            f"Change: {change:+.8f} USDT"
        )
        
        for user_id in self.allowed_users:
            try:
                await self.app.bot.send_message(chat_id=user_id, text=message)
            except Exception as e:
                logger.error(f"Failed to send balance update to {user_id}: {e}")

    async def send_trade_chart(self, order: Order):
        """Send trade chart to users"""
        try:
            ref_price = None
            if order.symbol in self.binance_client.reference_prices:
                ref_price = self.binance_client.reference_prices[order.symbol].get(order.timeframe)

            # Get chart data from appropriate client
            chart_data = None
            if order.order_type == OrderType.FUTURES and hasattr(self.binance_client, 'futures_client'):
                chart_data = await self.binance_client.futures_client.generate_trade_chart(order)
            else:
                chart_data = await self.binance_client.generate_trade_chart(order)

            if not chart_data:
                logger.error("Failed to generate chart data")
                # Send text-only notification as fallback
                for user_id in self.allowed_users:
                    try:
                        await self.app.bot.send_message(
                            chat_id=user_id,
                            text=self._format_trade_info(order)
                        )
                    except Exception as e:
                        logger.error(f"Failed to send fallback message to {user_id}: {e}")
                return

            # Attempt to send chart with caption
            caption = self._format_trade_info(order)  # Use consistent formatting
            
            for user_id in self.allowed_users:
                try:
                    await self.app.bot.send_photo(
                        chat_id=user_id,
                        photo=chart_data,
                        caption=caption
                    )
                except Exception as e:
                    logger.error(f"Failed to send chart to {user_id}: {e}")
                    try:
                        await self.app.bot.send_message(
                            chat_id=user_id,
                            text=caption
                        )
                    except Exception as e2:
                        logger.error(f"Failed to send fallback message to {user_id}: {e2}")
                    
        except Exception as e:
            logger.error(f"Failed to generate trade chart: {e}")

    async def send_roar(self, order: Order):
        """Send trade notification with fallback for chart failures"""
        # Add order ID to sent roars set
        self.sent_roars.add(order.order_id)
        
        try:
            # Try to generate chart first
            chart_data = await self.binance_client.generate_trade_chart(order)
            
            # Create detailed caption with environment info
            caption = (
                f"{self.env_info}\n\n"
                f"🦖 ROARRR! Trade Complete! 💥\n\n"
                f"Order ID: {order.order_id}\n"
                f"Symbol: {order.symbol}\n"
                f"Amount: {float(order.quantity):.8f} {order.symbol.replace('USDT', '')}\n"
                f"Price: ${float(order.price):.2f}\n"
                f"Total: ${float(order.price * order.quantity):.2f} USDT\n"  # Fixed double colon here
                f"Fees: ${float(order.fees):.4f} {order.fee_asset}\n"
                f"Threshold: {order.threshold if order.threshold else 'Manual'}\n"
                f"Timeframe: {self._get_timeframe_value(order.timeframe)}\n\n"
                f"Check /profits to see your updated portfolio."
            )
            
            # Send notification with or without chart
            for user_id in self.allowed_users:
                try:
                    if (chart_data):
                        await self.app.bot.send_photo(
                            chat_id=user_id,
                            photo=chart_data,
                            caption=caption
                        )
                    else:
                        # Fallback to text-only with explanation
                        await self.app.bot.send_message(
                            chat_id=user_id,
                            text=f"{caption}\n\n⚠️ Chart not available: Insufficient candle data"
                        )
                except Exception as e:
                    logger.error(f"Failed to send roar to {user_id}: {e}")
                    # Ensure at least basic notification is sent
                    try:
                        await self.app.bot.send_message(
                            chat_id=user_id,
                            text=caption
                        )
                    except Exception as e2:
                        logger.error(f"Failed to send fallback message: {e2}")
                    
        except Exception as e:
            logger.error(f"Failed to send roar: {e}")

    async def show_main_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the main menu with trading operations and market info sections"""
        try:
            # Get current trading mode
            trading_mode = self.config['environment']['trading_mode']
            is_futures = trading_mode == 'futures'
            
            # Create keyboard with two main sections
            keyboard = [
                # Section 1: Trading Operations
                [InlineKeyboardButton("🔄 Trading Operations", callback_data="section_trading")],
                # Section 2: Market & Account Info
                [InlineKeyboardButton("📊 Market & Account Info", callback_data="section_market")]
            ]
            
            # Add trading status button
            trading_status = "⏸️ Paused" if self.is_paused else "▶️ Active"
            keyboard.append([InlineKeyboardButton(f"Trading: {trading_status}", callback_data="toggle_trading")])
            
            # Create the reply markup
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Send message with the menu
            await update.message.reply_text(
                f"*Trade-a-saurus Rex* 🦖\n"
                f"Mode: {'Futures' if is_futures else 'Spot'} "
                f"({'Testnet' if self.config['environment']['testnet'] else 'Mainnet'})\n\n"
                f"Select a menu option:",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Error showing main menu: {e}")
            await update.message.reply_text(f"Error showing menu: {str(e)}")

    async def handle_menu_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle menu callbacks with simplified section handling"""
        query = update.callback_query
        await query.answer()
        
        try:
            # Get current trading mode
            trading_mode = self.config['environment']['trading_mode']
            is_futures = trading_mode == 'futures'
            
            # Handle main sections
            if query.data == "section_trading":
                # Trading Operations Section
                keyboard = []
                
                # Common trading operations
                keyboard.append([
                    InlineKeyboardButton("📈 Add Trade", callback_data="add_trade")
                ])
                
                # Mode-specific operations
                if is_futures:
                    keyboard.append([
                        InlineKeyboardButton("🔍 View Positions", callback_data="show_positions")
                    ])
                else:
                    keyboard.append([
                        InlineKeyboardButton("🔍 View Thresholds", callback_data="show_thresholds")
                    ])
                
                # Back button
                keyboard.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")])
                
                await query.edit_message_text(
                    text=f"*Trading Operations* ({trading_mode.capitalize()})\n"
                         f"Select an operation:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='Markdown'
                )
                
            elif query.data == "section_market":
                # Market & Account Info Section
                keyboard = [
                    [
                        InlineKeyboardButton("💰 Balance", callback_data="get_balance"),
                        InlineKeyboardButton("📊 Stats", callback_data="get_stats")
                    ],
                    [
                        InlineKeyboardButton("📜 Order History", callback_data="order_history"),
                        InlineKeyboardButton("📈 Profits", callback_data="show_profits")
                    ],
                    [
                        InlineKeyboardButton("📊 Visualizations", callback_data="show_viz_menu")
                    ]
                ]
                
                # Back button
                keyboard.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")])
                
                await query.edit_message_text(
                    text=f"*Market & Account Info* ({trading_mode.capitalize()})\n"
                         f"Select an option:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='Markdown'
                )
                
            elif query.data == "main_menu":
                # Return to main menu
                await self.show_main_menu(update, context)
                
            elif query.data == "toggle_trading":
                # Toggle trading status
                await self.toggle_trading(update, context)
                # Return to main menu after toggling
                await self.show_main_menu(update, context)
                
            else:
                # Handle other menu actions
                await self.handle_menu_action(query.data, update, context)
                
        except Exception as e:
            logger.error(f"Error handling menu callback: {e}")
            await query.edit_message_text(f"Error processing menu selection: {str(e)}")

    async def handle_menu_action(self, action: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle menu actions with simplified handling"""
        query = update.callback_query
        
        try:
            # Handle common actions
            if action == "get_balance":
                await self.get_balance_command(update, context)
            elif action == "get_stats":
                await self.get_stats(update, context)
            elif action == "order_history":
                await self.get_order_history(update, context)
            elif action == "show_profits":
                await self.show_profits(update, context)
            elif action == "show_viz_menu":
                await self.show_viz_menu(update, context)
            elif action == "add_trade":
                await self.add_trade_start(update, context)
            elif action == "show_thresholds":
                await self.show_thresholds(update, context)
            elif action == "show_positions":
                await self.show_positions(update, context)
            else:
                await query.edit_message_text(
                    f"Action '{action}' not implemented.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")
                    ]])
                )
        
        except Exception as e:
            logger.error(f"Error handling menu action {action}: {e}")
            await query.edit_message_text(
                f"Error processing action: {str(e)}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")
                ]])
            )

    async def get_balance(self):
        """Get balance with proper client handling"""
        try:
            # Check if we're in futures mode
            if self.config['environment']['trading_mode'] == 'futures':
                if not hasattr(self.binance_client, 'futures_client'):
                    raise AttributeError("Futures client not available")
                return await self.binance_client.futures_client.get_balance()
            else:
                # Use regular spot client
                return await self.binance_client.get_balance(self.config['trading']['base_currency'])
        except Exception as e:
            logger.error(f"Error getting balance: {e}")
            return Decimal('0')

    def _format_trade_info(self, order: Order) -> str:
        """Format trade info for text messages"""
        base_info = (
            f"Trade Details:\n"
            f"Symbol: {order.symbol}\n"
            f"Type: {order.order_type.value}\n"
            f"Price: ${float(order.price):.2f}\n"
            f"Amount: {float(order.quantity):.8f}\n"
            f"Total Value: ${float(order.price * order.quantity):.2f}"
        )
        
        if order.order_type == OrderType.FUTURES:
            base_info += (
                f"\nLeverage: {order.leverage}x\n"
                f"Direction: {order.direction.value if order.direction else 'N/A'}"
            )
        
        return base_info

    async def test_commands(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Test all available commands for current mode"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        current_mode = self.config['environment']['trading_mode'].upper()
        
        await update.message.reply_text(f"🧪 Starting command test for {current_mode} mode...")

        try:
            # Test basic commands
            commands = [
                ("Menu Command", self.show_main_menu),
                ("Balance Check", self.get_balance_command),  # Changed this line
                ("Stats Check", self.get_stats),
                ("Profit Analysis", self.show_profits),
                ("Order History", self.get_order_history),
                ("Threshold Status", self.show_thresholds),
                ("Visualization Menu", self.show_viz_menu)
            ]

            # Add futures-specific commands if in futures mode
            if (current_mode == "FUTURES"):
                commands.extend([
                    ("Leverage Test", lambda u, c: self.set_leverage(u, ["BTCUSDT", "10"])),
                    ("Margin Type Test", lambda u, c: self.set_margin_type(u, ["BTCUSDT", "ISOLATED"])),
                    ("Hedge Mode Test", self.toggle_hedge_mode)
                ])

            # Execute each command with status reporting
            for test_name, command in commands:
                try:
                    await update.message.reply_text(f"Testing: {test_name}...")
                    await command(update, context)
                    await asyncio.sleep(1)  # Add delay between commands
                except Exception as e:
                    await update.message.reply_text(f"❌ {test_name} failed: {str(e)}")
                    continue

            await update.message.reply_text(
                "✅ Command test completed!\n\n"
                "Note: Some commands may have produced their own output above."
            )

        except Exception as e:
            await update.message.reply_text(f"❌ Test sequence failed: {str(e)}")

    def handle_submenu_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        query.answer()
        data = query.data
        # TODO: Process submenu actions based on 'data'
        logger.info(f"Submenu action: {data}")

    async def show_mode_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show mode selection menu"""
        try:
            # Get current trading mode
            current_mode = self.config['environment']['trading_mode']
            new_mode = "futures" if current_mode == "spot" else "spot"
            
            # Create keyboard with mode options
            keyboard = [
                [
                    InlineKeyboardButton(f"Switch to {new_mode.capitalize()}", callback_data=f"mode_{new_mode}")
                ],
                [
                    InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")
                ]
            ]
            
            # Create reply markup
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Send message
            await update.message.reply_text(
                f"*Trading Mode*\n\n"
                f"Current mode: *{current_mode.capitalize()}*\n\n"
                f"Select an option:",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            
        except Exception as e:
            logger.error(f"Error showing mode menu: {e}")
            await update.message.reply_text(f"Error: {str(e)}")
