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
        self.keyboard = [
            [KeyboardButton("/menu"), KeyboardButton("/power")],
            [KeyboardButton("/add"), KeyboardButton("/balance")],
            [KeyboardButton("/positions"), KeyboardButton("/mode")]
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
        """Initialize the Telegram bot"""
        self.app = Application.builder().token(self.token).build()
        
        # Add new command handlers
        add_trade_handler = ConversationHandler(
            entry_points=[CommandHandler("add", self.add_trade_start)],
            states={
                SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_symbol)],
                ORDER_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_order_type)],
                LEVERAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_leverage)],
                DIRECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_direction)],
                AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_amount)],
                PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_fees)],
                FEES: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_final)]
            },
            fallbacks=[CommandHandler("cancel", self.add_trade_cancel)],
        )
        
        self.app.add_handler(add_trade_handler)
        self.app.add_handler(CommandHandler("thresholds", self.show_thresholds))
        self.app.add_handler(CommandHandler("menu", self.show_menu))
        
        # Register command handlers
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("power", self.toggle_trading))  # Change command name
        self.app.add_handler(CommandHandler("balance", self.get_balance_command))
        self.app.add_handler(CommandHandler("stats", self.get_stats))
        self.app.add_handler(CommandHandler("history", self.get_order_history))
        self.app.add_handler(CommandHandler("profits", self.show_profits))
        
        # Add visualization command
        self.app.add_handler(CommandHandler("viz", self.show_viz_menu))
        self.app.add_handler(CallbackQueryHandler(self.handle_viz_selection, pattern="^(daily_volume|profit_distribution|order_types|hourly_activity)$"))
        
        # Add mode switching handler
        self.app.add_handler(CommandHandler("mode", self.switch_mode))
        self.app.add_handler(CallbackQueryHandler(self.handle_mode_switch, pattern="^switch_mode_"))
        
        # Add futures-specific command handlers
        self.app.add_handler(CommandHandler("leverage", self.set_leverage))
        self.app.add_handler(CommandHandler("margin", self.set_margin_type))
        self.app.add_handler(CommandHandler("hedge", self.toggle_hedge_mode))
        
        # Add menu handlers
        self.app.add_handler(CommandHandler("menu", self.show_main_menu))
        self.app.add_handler(CallbackQueryHandler(self.handle_menu_callback, 
                                                pattern='^menu_'))

        # Add test command
        self.app.add_handler(CommandHandler("test", self.test_commands))

        # Add submenu handler
        self.app.add_handler(CallbackQueryHandler(self.handle_submenu_callback, pattern='^submenu_'))

        # Set up persistent menu for each authorized user
        for user_id in self.allowed_users:
            try:
                await self.app.bot.set_chat_menu_button(
                    chat_id=user_id,
                    menu_button={"type": "default"}
                )
            except Exception as e:
                logger.error(f"Failed to set menu for {user_id}: {e}")

        await self.app.initialize()
        await self.app.start()

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

    async def show_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show main menu with submenus"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        current_mode = self.config['environment']['trading_mode'].upper()
        
        menu_text = f"""
🦖 Trade-a-saurus Rex Commands:

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

        keyboard = [
            [InlineKeyboardButton("👤 Account Info", callback_data="menu_account")],
            [InlineKeyboardButton("📈 Trading Actions", callback_data="menu_trading")],
            [InlineKeyboardButton("📊 Market Analysis", callback_data="menu_analysis")],
            [InlineKeyboardButton("🔄 Switch Mode", callback_data="menu_switch_mode")]
        ]

        await update.message.reply_text(
            menu_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def handle_menu_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle menu callback queries with submenus"""
        query = update.callback_query
        await query.answer()
        
        current_mode = self.config['environment']['trading_mode'].upper()
        
        menus = {
            "menu_account": {
                "title": "👤 Account Info Menu",
                "buttons": [
                    [InlineKeyboardButton("💰 Balance", callback_data="action_balance")],
                    [InlineKeyboardButton("📊 Portfolio Stats", callback_data="action_stats")],
                    [InlineKeyboardButton("💵 Profits & Loss", callback_data="action_profits")],
                    [InlineKeyboardButton("📜 Trade History", callback_data="action_history")],
                    [InlineKeyboardButton("« Back to Main Menu", callback_data="menu_main")]
                ]
            },
            "menu_trading": {
                "title": "📈 Trading Actions Menu",
                "buttons": [
                    [InlineKeyboardButton("➕ New Manual Trade", callback_data="action_add")],
                    [InlineKeyboardButton("⏯️ Toggle Auto-Trading", callback_data="action_power")],
                    [InlineKeyboardButton("« Back to Main Menu", callback_data="menu_main")]
                ]
            },
            "menu_analysis": {
                "title": "📊 Market Analysis Menu",
                "buttons": [
                    [InlineKeyboardButton("📈 Price Charts", callback_data="action_viz")],
                    [InlineKeyboardButton("🎯 Current Thresholds", callback_data="action_thresholds")],
                    [InlineKeyboardButton("📜 Order History", callback_data="action_history")],
                    [InlineKeyboardButton("« Back to Main Menu", callback_data="menu_main")]
                ]
            }
        }

        # Add futures-specific buttons if in futures mode
        if current_mode == "FUTURES":
            menus["menu_trading"]["buttons"].insert(-1, [
                InlineKeyboardButton("⚙️ Set Leverage", callback_data="action_leverage"),
                InlineKeyboardButton("⚡ Set Margin", callback_data="action_margin")
            ])
            menus["menu_trading"]["buttons"].insert(-1, [
                InlineKeyboardButton("🔄 Toggle Hedge Mode", callback_data="action_hedge")
            ])

        if query.data in menus:
            menu = menus[query.data]
            await query.edit_message_text(
                f"{menu['title']}\n\nCurrent Mode: {current_mode}",
                reply_markup=InlineKeyboardMarkup(menu["buttons"])
            )
        elif query.data == "menu_main":
            await self.show_menu(update, context)
        elif query.data.startswith("action_"):
            action = query.data.replace("action_", "")
            await self.handle_menu_action(action, update, context)

    async def show_thresholds(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show detailed threshold information with futures support"""
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
                    next_reset += timedelta(days_until_monday)

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
                    
                    # Get current price based on mode
                    current_mode = self.config['environment']['trading_mode'].upper()
                    if current_mode == "FUTURES":
                        ticker = await self.binance_client.get_symbol_ticker(symbol)
                        current_price = float(ticker['price'])
                    else:
                        # Existing spot price check
                        ticker = await self.binance_client.client.get_symbol_ticker(symbol=symbol)
                        current_price = float(ticker['price'])
                    
                    # Calculate price change if reference price exists
                    if (ref_price):
                        price_change = ((current_price - ref_price) / ref_price) * 100
                        price_info = f"Open: ${ref_price:,.2f} | Current: ${current_price:,.2f} ({price_change:+.2f}%)"
                    else:
                        price_info = f"Current: ${current_price:,.2f}"
                    
                    # Get threshold information
                    triggered = self.binance_client.triggered_thresholds.get(symbol, {}).get(timeframe, [])
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
        """Start the manual trade addition process"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return ConversationHandler.END
            
        self.temp_trade_data[update.effective_user.id] = {}
        
        pairs = self.config['trading']['pairs']
        keyboard = [[KeyboardButton(pair)] for pair in pairs]
        markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
        
        await update.message.reply_text(
            "What trading pair did you trade?",
            reply_markup=markup
        )
        return SYMBOL

    async def add_trade_symbol(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle symbol input"""
        symbol = update.message.text.upper()
        if symbol not in self.config['trading']['pairs']:
            await update.message.reply_text("Invalid symbol. Please select from the list.")
            return SYMBOL
            
        user_data = self.temp_trade_data[update.effective_user.id]
        user_data['symbol'] = symbol
        
        keyboard = [
            [KeyboardButton("SPOT")],
            [KeyboardButton("FUTURES")]
        ]
        markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
        
        await update.message.reply_text(
            "What type of trade was this?",
            reply_markup=markup
        )
        return ORDER_TYPE

    async def add_trade_order_type(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle order type input (SPOT/FUTURES)"""
        try:
            order_type = update.message.text.upper()
            if order_type not in ["SPOT", "FUTURES"]:
                await update.message.reply_text("Please select either SPOT or FUTURES")
                return ORDER_TYPE
                
            user_data = self.temp_trade_data[update.effective_user.id]
            user_data['order_type'] = OrderType(order_type.lower())
            
            if order_type == "FUTURES":
                await update.message.reply_text("Enter leverage (e.g., 5, 10, 20):")
                return LEVERAGE
            else:
                await update.message.reply_text("Enter amount in USDT (e.g., 100.50):")
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
        
        await update.message.reply_text("Enter amount in USDT (e.g., 100.50):")
        return AMOUNT

    async def add_trade_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle amount input for manual trade"""
        try:
            amount = float(update.message.text)
            if amount <= 0:
                raise ValueError("Amount must be positive")
                
            user_data = self.temp_trade_data[update.effective_user.id]
            user_data['amount'] = Decimal(str(amount))
            
            await update.message.reply_text("Enter entry price (e.g., 42000.50):")
            return PRICE
            
        except ValueError as e:
            await update.message.reply_text(f"Error: {str(e)}")
            return AMOUNT

    async def add_trade_fees(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle amount and ask for fees"""
        try:
            price = Decimal(update.message.text)
            if price <= 0:
                raise ValueError("Price must be positive")
                
            user_data = self.temp_trade_data[update.effective_user.id]
            user_data['price'] = price
            
            await update.message.reply_text("Enter the trading fees in USDT (e.g., 0.25):")
            return FEES
            
        except ValueError as e:
            await update.message.reply_text(f"Please enter a valid price: {str(e)}")
            return PRICE

    async def add_trade_final(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Complete trade creation with chart generation"""
        try:
            fees = Decimal(update.message.text)
            if fees < 0:
                raise ValueError("Fees cannot be negative")
                
            user_data = self.temp_trade_data[update.effective_user.id]
            
            # Generate unique order ID
            order_id = f"MANUAL_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
            
            # Create order object
            order = Order(
                symbol=user_data['symbol'],
                status=OrderStatus.FILLED,  # Manual trades are always filled
                order_type=user_data['order_type'],
                price=user_data['price'],
                quantity=user_data['amount'] / user_data['price'],
                timeframe=TimeFrame.DAILY,  # Default to daily for manual trades
                order_id=order_id,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                filled_at=datetime.utcnow(),
                leverage=user_data.get('leverage'),
                direction=user_data.get('direction'),
                fees=fees,
                fee_asset='USDT'
            )
            
            # Save to database
            await self.mongo_client.insert_manual_trade(order)
            
            # Generate chart for manual trade - Fix spot/futures chart generation
            chart_data = None
            if order.order_type == OrderType.FUTURES and hasattr(self.binance_client, 'futures_client'):
                chart_data = await self.binance_client.futures_client.generate_trade_chart(order)
            else:
                # For spot trades, use the binance_client directly
                chart_data = await self.binance_client.generate_trade_chart(order)

            # Send confirmation with chart if available
            direction_info = f"\nDirection: {order.direction.value}" if order.direction else ""
            leverage_info = f"\nLeverage: {order.leverage}x" if order.leverage else ""
            
            message = (
                f"✅ Manual trade added:\n"
                f"Symbol: {order.symbol}\n"
                f"Type: {order.order_type.value}"
                f"{direction_info}"
                f"{leverage_info}\n"
                f"Amount: {float(order.quantity):.8f}\n"
                f"Price: ${float(order.price):.2f}\n"
                f"Fees: ${float(order.fees):.2f}\n"
                f"Total Value: ${float(order.price * order.quantity)::.2f}"
            )

            if chart_data:
                await update.message.reply_photo(
                    photo=chart_data,
                    caption=message,
                    reply_markup=self.markup
                )
            else:
                await update.message.reply_text(
                    message,
                    reply_markup=self.markup
                )

            # Cleanup
            del self.temp_trade_data[update.effective_user.id]
            return ConversationHandler.END
            
        except ValueError as e:
            await update.message.reply_text(f"Please enter valid fees: {str(e)}")
            return FEES
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
            current_mode = self.config['environment']['trading_mode'].upper()
            
            if current_mode == "FUTURES":
                # Use futures specific profit calculation
                positions = await self.binance_client.futures_client.get_open_positions()
                response = ["📊 Futures Portfolio Analysis:\n"]
                
                total_pnl = 0
                for symbol, pos in positions.items():
                    amt = float(pos['positionAmt'])
                    if amt != 0:
                        entry_price = float(pos['entryPrice'])
                        unrealized_pnl = float(pos['unrealizedProfit'])
                        leverage = int(pos['leverage'])
                        
                        response.extend([
                            f"\n{symbol}:",
                            f"Position Size: {abs(amt):.4f}",
                            f"Entry Price: ${entry_price:.2f}",
                            f"Leverage: {leverage}x",
                            f"Unrealized P/L: ${unrealized_pnl:+.2f}"
                        ])
                        total_pnl += unrealized_pnl
                
                response.extend([
                    f"\nTotal Unrealized P/L: ${total_pnl:+.2f}"
                ])
            else:
                # Use existing spot profit calculation
                # Get positions from MongoDB for configured pairs only
                allowed_symbols = set(self.config['trading']['pairs'])
                positions = await self.mongo_client.get_position_stats(allowed_symbols)
                
                if not positions:
                    await update.message.reply_text("No filled orders found.")
                    return

                # Initialize portfolio totals
                portfolio_stats = {
                    "total_cost": Decimal('0'),
                    "total_value": Decimal('0'),
                    "total_profit": Decimal('0'),
                    "total_tax": Decimal('0')
                }

                # Calculate profits for each position
                response = ["📊 Portfolio Analysis:\n"]

                # First show USDT balance
                try:
                    usdt_balance = await self.binance_client.get_balance('USDT')
                    response.append(f"💵 USDT Balance: ${usdt_balance:.2f}\n")
                except Exception as e:
                    logger.error(f"Failed to get USDT balance: {e}")
                    response.append("💵 USDT Balance: Unable to fetch\n")

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
                    
                    # Generate position message
                    position_msg = [
                        f"\n🔸 {symbol}:",
                        f"Quantity: {position['total_quantity']:.8f}",
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
                        f"Total Tax: ${portfolio_stats['total_tax']:.2f}",
                        f"Net P/L: ${net_profit:.2f}"
                    ])  # Close the extend() call properly

                response.extend(summary)

            await update.message.reply_text("\n".join(response))

        except Exception as e:
            logger.error(f"Error getting profits: {e}")
            await update.message.reply_text(f"❌ Error getting profits: {str(e)}")

    async def show_viz_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show data visualization options"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        keyboard = [
            [InlineKeyboardButton("📊 Daily Volume", callback_data=VisualizationType.DAILY_VOLUME)],
            [InlineKeyboardButton("💰 Profit Distribution", callback_data=VisualizationType.PROFIT_DIST)],
            [InlineKeyboardButton("📈 Order Types", callback_data=VisualizationType.ORDER_TYPES)],
            [InlineKeyboardButton("⏰ Hourly Activity", callback_data=VisualizationType.HOURLY_ACTIVITY)]
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
        data = await self.mongo_client.get_visualization_data(viz_type)
        
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

    async def send_timeframe_reset_notification(self, reset_data: dict):
        """Send detailed timeframe reset notification"""
        try:
            timeframe = reset_data["timeframe"]
            emoji_map = {
                TimeFrame.DAILY: "📅",
                TimeFrame.WEEKLY: "📆",
                TimeFrame.MONTHLY: "📊"
            }
            
            message_parts = [
                f"{self.env_info}\n",
                f"{emoji_map.get(timeframe, '🔄')} {timeframe.value.title()} Timeframe Reset\n",
                f"Reset Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC\n",
                "\nPrice Summary:"
            ]
            
            # Add price information for each symbol
            for price_data in reset_data["prices"]:
                symbol = price_data["symbol"]
                current = price_data["current_price"]
                reference = price_data["reference_price"]
                change = price_data["price_change"]
                
                message_parts.append(
                    f"\n{symbol}:"
                    f"\n• Previous Open: ${reference:,.2f}"
                    f"\n• Current Price: ${current:,.2f}"
                    f"\n• Change: {change:+.2f}%"
                )
            
            message_parts.append(f"\n\nAll {timeframe.value} thresholds have been reset.")
            message_parts.append("\nUse /thresholds to see new tracking status.")
            
            # Send to all authorized users
            for user_id in self.allowed_users:
                try:
                    await self.app.bot.send_message(
                        chat_id=user_id,
                        text="\n".join(message_parts),
                        reply_markup=self.markup
                    )
                except Exception as e:
                    logger.error(f"Failed to send reset notification to {user_id}: {e}")
                    
        except Exception as e:
            logger.error(f"Error sending timeframe reset notification: {e}")

    async def send_threshold_notification(self, symbol: str, timeframe: TimeFrame, 
                                       threshold: float, current_price: float,
                                       reference_price: float, price_change: float):
        """Send notification when a threshold is triggered"""
        message = (
            f"🎯 Threshold Triggered!\n\n"
            f"Symbol: {symbol}\n"
            f"Timeframe: {timeframe.value}\n"
            f"Threshold: {threshold}%\n"
            f"Reference Price: ${reference_price:,.2f}\n"
            f"Current Price: ${current_price:,.2f}\n"
            f"Change: {price_change:+.2f}%"
        )
        
        for user_id in self.allowed_users:
            try:
                await self.app.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    reply_markup=self.markup
                )
            except Exception as e:
                logger.error(f"Failed to send threshold notification to {user_id}: {e}")

    async def send_reserve_alert(self, current_balance: Decimal, reserve_balance: float, pending_value: Decimal):
        """Send alert when reserve balance would be violated"""
        available_balance = float(current_balance - pending_value)
        message = (
            "⚠️ Trading Paused - Reserve Balance Protection\n\n"
            f"Current Balance: ${float(current_balance):.2f}\n"
            f"Pending Orders: ${float(pending_value):.2f}\n"
            f"Available Balance: ${available_balance:.2f}\n"
            f"Reserve Balance: ${reserve_balance:.2f}\n\n"
            "Trading will resume automatically on next timeframe reset\n"
            "when balance is above reserve requirement."
        )
        
        for user_id in self.allowed_users:
            try:
                await self.app.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    reply_markup=self.markup
                )
            except Exception as e:
                logger.error(f"Failed to send reserve alert to {user_id}: {e}")

    async def send_initial_balance_alert(self, current_balance: Decimal, reserve_balance: float):
        """Send alert when initial balance is below reserve"""
        message = (
            "⚠️ WARNING - Insufficient Initial Balance\n\n"
            f"Current Balance: ${float(current_balance)::.2f}\n"
            f"Required Reserve: ${reserve_balance:.2f}\n\n"
            "Trading is paused until balance is above reserve requirement.\n"
            "You can:\n"
            "1. Add more funds\n"
            "2. Lower reserve balance in config\n"
            "3. Use /power to check balance and resume"  # Changed from /trading to /power
        )
        
        for user_id in self.allowed_users:
            try:
                await self.app.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    reply_markup=self.markup
                )
            except Exception as e:
                logger.error(f"Failed to send initial balance alert to {user_id}: {e}")

    async def switch_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Switch between spot and futures trading modes"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        current_mode = self.config['environment']['trading_mode']
        new_mode = 'futures' if current_mode == 'spot' else 'spot'
        
        keyboard = [
            [InlineKeyboardButton(f"Confirm switch to {new_mode.upper()}", 
                                callback_data=f"switch_mode_{new_mode}")],
            [InlineKeyboardButton("Cancel", callback_data="switch_mode_cancel")]
        ]
        
        await update.message.reply_text(
            f"🔄 Current mode: {current_mode.upper()}\n"
            f"Switch to {new_mode.upper()} mode?\n\n"
            "⚠️ This will close all open positions",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def handle_mode_switch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle mode switch confirmation"""
        query = update.callback_query
        await query.answer()
        
        if query.data == "switch_mode_cancel":
            await query.edit_message_text("Mode switch cancelled")
            return
            
        new_mode = query.data.split('_')[-1]
        try:
            # Only cancel pending orders, don't close positions
            if self.binance_client:
                # Get pending orders first
                pending_orders = await self.mongo_client.get_pending_orders()
                for order in pending_orders:
                    await self.binance_client.cancel_order(order.symbol, order.order_id)
                
                # Update config
                self.config['environment']['trading_mode'] = new_mode
                
                # Send notification
                await query.edit_message_text(
                    f"✅ Switching to {new_mode.upper()} mode\n"
                    "All pending orders have been cancelled.\n"
                    "Bot will restart with new mode..."
                )
                
                # Restart the bot
                await self.stop()
                await self.initialize()
                await self.start()
                
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to switch mode: {str(e)}")

    async def set_leverage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set leverage for a trading pair"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        if self.config['environment']['trading_mode'] != 'futures':
            await update.message.reply_text("❌ This command is only available in futures mode")
            return
            
        args = context.args
        if len(args) != 2:
            await update.message.reply_text(
                "⚠️ Usage: /leverage SYMBOL LEVERAGE\n"
                "Example: /leverage BTCUSDT 10"
            )
            return
            
        symbol, leverage = args[0].upper(), args[1]
        try:
            leverage = int(leverage)
            if not 1 <= leverage <= 125:
                raise ValueError("Leverage must be between 1 and 125")
                
            result = await self.binance_client.set_leverage(symbol, leverage)
            if result:
                await update.message.reply_text(
                    f"✅ Leverage set for {symbol}:\n"
                    f"Leverage: {leverage}x"
                )
            else:
                await update.message.reply_text("❌ Failed to set leverage")
                
        except ValueError as e:
            await update.message.reply_text(f"❌ Invalid leverage: {str(e)}")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")

    async def set_margin_type(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set margin type for a trading pair"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        if self.config['environment']['trading_mode'] != 'futures':
            await update.message.reply_text("❌ This command is only available in futures mode")
            return
            
        args = context.args
        if len(args) != 2:
            await update.message.reply_text(
                "⚠️ Usage: /margin SYMBOL TYPE\n"
                "Example: /margin BTCUSDT ISOLATED\n"
                "Types: ISOLATED, CROSSED"
            )
            return
            
        symbol, margin_type = args[0].upper(), args[1].upper()
        if margin_type not in ['ISOLATED', 'CROSSED']:
            await update.message.reply_text("❌ Margin type must be ISOLATED or CROSSED")
            return
            
        try:
            result = await self.binance_client.set_margin_type(symbol, margin_type)
            if result:
                await update.message.reply_text(
                    f"✅ Margin type set for {symbol}:\n"
                    f"Type: {margin_type}"
                )
            else:
                await update.message.reply_text("❌ Failed to set margin type")
                
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")

    async def toggle_hedge_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Toggle hedge mode for futures trading"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        if self.config['environment']['trading_mode'] != 'futures':
            await update.message.reply_text("❌ This command is only available in futures mode")
            return
            
        try:
            current_mode = await self.binance_client.get_position_mode()
            new_mode = 'HEDGE' if current_mode == 'ONE_WAY' else 'ONE_WAY'
            
            result = await self.binance_client.set_position_mode(new_mode)
            if result:
                await update.message.reply_text(
                    f"✅ Position mode changed:\n"
                    f"New mode: {new_mode}"
                )
            else:
                await update.message.reply_text("❌ Failed to change position mode")
                
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")

    def _get_timeframe_value(self, timeframe: TimeFrame) -> str:
        """Convert TimeFrame enum to display string"""
        if not timeframe:
            return "N/A"
            
        display_map = {
            TimeFrame.DAILY: "Daily",
            TimeFrame.WEEKLY: "Weekly",
            TimeFrame.MONTHLY: "Monthly"
        }
        return display_map.get(timeframe, str(timeframe))

    async def show_main_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show interactive main menu"""
        if not self._is_authorized(update.effective_user.id):
            return
        
        # Handle both message and callback query updates
        message = update.message or update.callback_query.message
        if not message:
            return
            
        current_mode = self.config['environment']['trading_mode'].upper()
        keyboard = [
            [InlineKeyboardButton("👤 Account Info", callback_data="menu_account"),
             InlineKeyboardButton("📈 Trading", callback_data="menu_trading")],
            [InlineKeyboardButton("📊 Analysis", callback_data="menu_analysis"),
             InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings")],
            [InlineKeyboardButton("🔄 Switch Mode", callback_data="menu_switch_mode")]
        ]

        menu_text = (
            f"🦖 Trade-a-saurus Rex Menu\n\n"
            f"Current Mode: {current_mode}\n"
            f"Trading Status: {'Paused ⏸' if self.is_paused else 'Active ▶️'}\n\n"
            f"Select a category:"
        )

        if update.callback_query:
            await update.callback_query.edit_message_text(
                menu_text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await message.reply_text(
                menu_text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    async def handle_menu_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle menu callback queries"""
        query = update.callback_query
        await query.answer()
        
        current_mode = self.config['environment']['trading_mode'].upper()
        
        if query.data == "menu_account":
            keyboard = [
                [InlineKeyboardButton("💰 Balance", callback_data="action_balance"),
                 InlineKeyboardButton("📊 Stats", callback_data="action_stats")],
                [InlineKeyboardButton("💵 Profits", callback_data="action_profits"),
                 InlineKeyboardButton("📜 History", callback_data="action_history")]
            ]
            if current_mode == "FUTURES":
                keyboard.append([InlineKeyboardButton("📈 Positions", callback_data="action_positions")])
            keyboard.append([InlineKeyboardButton("« Back", callback_data="menu_main")])
            
            await query.edit_message_text(
                f"👤 Account Menu ({current_mode})\n"
                "Select an option:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        elif query.data == "menu_trading":
            # Trading menu with clear distinction for futures settings
            keyboard = [
                [InlineKeyboardButton("➕ Add Manual Trade", callback_data="action_add"),
                 InlineKeyboardButton("⏯️ Toggle Auto Trading", callback_data="action_power")]
            ]
            if current_mode == "FUTURES":
                keyboard.extend([
                    [InlineKeyboardButton("═ Active Trade Settings ═", callback_data="none")],
                    [InlineKeyboardButton("Set Trade Leverage", callback_data="action_leverage"),
                     InlineKeyboardButton("Set Trade Margin", callback_data="action_margin")],
                ])
            keyboard.append([InlineKeyboardButton("« Back", callback_data="menu_main")])
            
            await query.edit_message_text(
                f"📈 Trading Menu ({current_mode})\n"
                "Select an option:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        elif query.data == "menu_analysis":
            keyboard = [
                [InlineKeyboardButton("📊 Visualizations", callback_data="action_viz"),
                 InlineKeyboardButton("🎯 Thresholds", callback_data="action_thresholds")],
                [InlineKeyboardButton("📈 Market Data", callback_data="action_market")],
                [InlineKeyboardButton("« Back", callback_data="menu_main")]
            ]
            
            await query.edit_message_text(
                f"📊 Analysis Menu ({current_mode})\n"
                "Select an option:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        elif query.data == "menu_settings":
            # Settings menu with clear distinction for futures defaults
            keyboard = [
                [InlineKeyboardButton("⚙️ Trading Mode", callback_data="action_mode")]
            ]
            if current_mode == "FUTURES":
                keyboard.extend([
                    [InlineKeyboardButton("═ Futures Default Settings ═", callback_data="none")],
                    [InlineKeyboardButton("Default Leverage", callback_data="action_def_leverage")],
                    [InlineKeyboardButton("Default Margin Type", callback_data="action_def_margin")],
                    [InlineKeyboardButton("Position Mode (ONE-WAY/HEDGE)", callback_data="action_pos_mode")]
                ])
            keyboard.append([InlineKeyboardButton("« Back", callback_data="menu_main")])
            
            await query.edit_message_text(
                f"⚙️ Settings Menu ({current_mode})\n\n"
                f"Current Settings:\n"
                f"• Mode: {current_mode}\n"
                + (f"• Default Leverage: {self.config['trading']['futures_settings']['default_leverage']}x\n"
                   f"• Default Margin: {self.config['trading']['futures_settings']['margin_type']}\n"
                   f"• Position Mode: {self.config['trading']['futures_settings']['position_mode']}"
                   if current_mode == "FUTURES" else ""),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        elif query.data == "menu_main":
            # Return to main menu
            await self.show_main_menu(update, context)

        elif query.data.startswith("action_"):
            # Handle action callbacks
            action = query.data.replace("action_", "")
            await self.handle_menu_action(action, update, context)

    async def handle_menu_action(self, action: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle menu action callbacks"""
        query = update.callback_query
        
        # Map actions to existing command handlers
        action_map = {
            'balance': self.get_balance_command,
            'stats': self.get_stats,
            'profits': self.show_profits,
            'history': self.get_order_history,
            'add': self.add_trade_start,
            'power': self.toggle_trading,
            'leverage': self.set_leverage,
            'margin': self.set_margin_type,
            'hedge': self.toggle_hedge_mode,
            'viz': self.show_viz_menu,
            'thresholds': self.show_thresholds,
            'mode': self.switch_mode
        }
        
        if action in action_map:
            # Create a dummy message update for command handlers
            dummy_message = query.message
            dummy_message.text = f"/{action}"
            dummy_update = Update(update.update_id, message=dummy_message)
            
            # Call the corresponding handler
            await action_map[action](dummy_update, context)
        else:
            await query.edit_message_text(
                f"Action '{action}' not implemented yet.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("« Back", callback_data="menu_main")]
                ])
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
                ("Menu Command", self.show_menu),
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
