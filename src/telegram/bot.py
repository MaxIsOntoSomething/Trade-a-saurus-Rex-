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
import os  # Add this import to the top of the file with other imports

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
        """Initialize the telegram bot with streamlined command structure"""
        try:
            # Create application builder with token
            self.app = Application.builder().token(self.token).build()
            
            # Initialize the application
            await self.app.initialize()
            
            # Register streamlined command handlers (10 core commands)
            self.app.add_handler(CommandHandler("start", self.show_main_menu))
            self.app.add_handler(CommandHandler("menu", self.show_main_menu))
            self.app.add_handler(CommandHandler("trade", self.show_trade_menu))
            self.app.add_handler(CommandHandler("account", self.show_account_menu))
            self.app.add_handler(CommandHandler("settings", self.show_settings_menu))
            self.app.add_handler(CommandHandler("stats", self.show_stats_menu))
            self.app.add_handler(CommandHandler("charts", self.show_charts_menu))
            self.app.add_handler(CommandHandler("power", self.toggle_trading))
            self.app.add_handler(CommandHandler("help", self.show_help))
            self.app.add_handler(CommandHandler("status", self.show_status))
            self.app.add_handler(CommandHandler("admin", self.show_admin_menu))
            
            # Add callback handlers for inline buttons
            self.app.add_handler(CallbackQueryHandler(self.handle_menu_callback))
            
            # Add conversation handler for multi-step interactions
            self.app.add_handler(self._create_trade_conversation())
            
            # Add message and error handlers
            self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.message_handler))
            self.app.add_error_handler(self.error_handler)
            
            logger.info("Telegram bot initialized with streamlined command structure")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize telegram bot: {e}")
            return False

    async def start(self):
        """Start the telegram bot with proper shutdown handling"""
        try:
            # Start the bot polling with proper parameters
            await self.app.start()
            await self.app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                poll_interval=1.0,
                timeout=30,
                bootstrap_retries=5,
                read_timeout=30
            )
            
            logger.info("Telegram bot started")
            
            # Notify admin users that the bot is online
            await self.send_startup_message()
            
            return True
        except Exception as e:
            logger.error(f"Failed to start telegram bot: {e}")
            return False

    async def stop(self):
        """Stop the telegram bot properly"""
        try:
            if hasattr(self, 'app'):
                # Stop the updater first
                if hasattr(self.app, 'updater'):
                    await self.app.updater.stop()
                # Then stop the application
                await self.app.stop()
                await self.app.shutdown()
            logger.info("Telegram bot stopped")
            return True
        except Exception as e:
            logger.error(f"Failed to stop telegram bot: {e}")
            return False

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
                    f"• USDT: ${float(spot_balance)::.2f}"
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
        """Show the new streamlined main menu with all core functions"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        # Get trading mode for the header
        trading_mode = self.config['environment']['trading_mode'].upper()
        env_type = "TESTNET" if self.config['environment']['testnet'] else "MAINNET"
        
        # Create a keyboard with all main functions
        keyboard = [
            [
                InlineKeyboardButton("💱 Trading", callback_data="menu_trade"),
                InlineKeyboardButton("👤 Account", callback_data="menu_account")
            ],
            [
                InlineKeyboardButton("📊 Stats", callback_data="menu_stats"),
                InlineKeyboardButton("📈 Charts", callback_data="menu_charts")
            ],
            [
                InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings")
            ],
            [
                InlineKeyboardButton(
                    "⏸️ Pause Trading" if not self.is_paused else "▶️ Resume Trading", 
                    callback_data="toggle_trading"
                )
            ]
        ]
        
        # Create the markup
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Get bot status data
        try:
            balance = await self.get_balance()
            pending_orders = await self.mongo_client.orders.count_documents(
                {"status": OrderStatus.PENDING.value}
            )
        except Exception as e:
            balance = Decimal('0')
            pending_orders = 0
            logger.error(f"Error getting menu data: {e}")
        
        # Create message with bot status
        menu_text = f"""
🦖 *Trade-a-saurus Rex*

*Mode:* {trading_mode} ({env_type})
*Status:* {'⏸️ PAUSED' if self.is_paused else '▶️ ACTIVE'}
*Balance:* ${float(balance):,.2f}
*Pending Orders:* {pending_orders}

Select an option:
"""
        
        # Check if this is from a callback query
        if hasattr(update, 'callback_query'):
            await update.callback_query.edit_message_text(
                menu_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                menu_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )

    async def show_trade_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show trading operations menu"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        keyboard = [
            [
                InlineKeyboardButton("📝 New Manual Trade", callback_data="new_trade"),
                InlineKeyboardButton("⚡ Signals", callback_data="view_signals")
            ],
            [
                InlineKeyboardButton("📋 Pending Orders", callback_data="pending_orders"),
                InlineKeyboardButton("🔍 Order History", callback_data="order_history")
            ]
        ]
        
        # Add futures-specific actions if in futures mode
        if self.config['environment']['trading_mode'] == 'futures':
            keyboard.append([
                InlineKeyboardButton("🔌 Set Leverage", callback_data="set_leverage"),
                InlineKeyboardButton("🛡️ Margin Type", callback_data="set_margin")
            ])
            keyboard.append([
                InlineKeyboardButton("🔄 Toggle Hedge Mode", callback_data="toggle_hedge")
            ])
            
        # Add back button
        keyboard.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="show_main_menu")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message_text = """
💱 *Trading Operations*

Choose an action:
• Create a new manual trade
• View trading signals
• Manage pending orders
• Check order history
"""
        
        if hasattr(update, 'callback_query'):
            await update.callback_query.edit_message_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )

    async def show_account_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show account information menu"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        keyboard = [
            [
                InlineKeyboardButton("💰 Balance", callback_data="show_balance"),
                InlineKeyboardButton("💹 P/L Analysis", callback_data="show_pnl")
            ],
            [
                InlineKeyboardButton("📊 Portfolio", callback_data="show_portfolio")
            ]
        ]
        
        # Add futures-specific options if in futures mode
        if self.config['environment']['trading_mode'] == 'futures':
            keyboard.insert(1, [
                InlineKeyboardButton("⚖️ Positions", callback_data="show_positions"),
                InlineKeyboardButton("🧮 Liquidation", callback_data="show_liquidation")
            ])
            
        # Add back button
        keyboard.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="show_main_menu")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message_text = """
👤 *Account Information*

Choose what information to view:
• Current balance
• Profit/loss analysis
• Portfolio overview
"""
        
        if self.config['environment']['trading_mode'] == 'futures':
            message_text += "• Open positions\n• Liquidation levels"
        
        if hasattr(update, 'callback_query'):
            await update.callback_query.edit_message_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )

    async def show_stats_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show statistics menu"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        keyboard = [
            [
                InlineKeyboardButton("📈 Performance", callback_data="show_performance"),
                InlineKeyboardButton("📊 Trading Stats", callback_data="show_trading_stats")
            ],
            [
                InlineKeyboardButton("🎯 Thresholds", callback_data="show_thresholds"),
                InlineKeyboardButton("📉 Win Rate", callback_data="show_win_rate")
            ],
            [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="show_main_menu")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message_text = """
📊 *Trading Statistics*

View detailed statistics about your trading:
• Overall performance
• Trading statistics
• Threshold triggers
• Win/loss ratio
"""
        
        if hasattr(update, 'callback_query'):
            await update.callback_query.edit_message_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )

    async def show_charts_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show charts menu"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        # Generate buttons for each trading pair
        pairs = self.config['trading']['pairs']
        pair_buttons = []
        
        # Create rows with 2 buttons each
        for i in range(0, len(pairs), 2):
            row = [InlineKeyboardButton(pairs[i], callback_data=f"chart_{pairs[i]}")]
            if i + 1 < len(pairs):
                row.append(InlineKeyboardButton(pairs[i+1], callback_data=f"chart_{pairs[i+1]}"))
            pair_buttons.append(row)
        
        # Add visualization options
        pair_buttons.append([
            InlineKeyboardButton("📉 Equity Curve", callback_data="viz_equity"),
            InlineKeyboardButton("🔄 Performance", callback_data="viz_performance")
        ])
        
        # Add back button
        pair_buttons.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="show_main_menu")])
        
        reply_markup = InlineKeyboardMarkup(pair_buttons)
        
        message_text = """
📈 *Price Charts & Visualizations*

Select a trading pair to view its chart, or choose a visualization:
"""
        
        if hasattr(update, 'callback_query'):
            await update.callback_query.edit_message_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )

    async def show_settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show settings menu"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        # Create settings keyboard
        trading_mode = self.config['environment']['trading_mode']
        
        keyboard = [
            [
                InlineKeyboardButton(
                    f"Mode: {trading_mode.upper()}", 
                    callback_data="toggle_mode"
                )
            ],
            [
                InlineKeyboardButton("💵 Trade Amount", callback_data="set_trade_amount"),
                InlineKeyboardButton("🛡️ Reserve", callback_data="set_reserve")
            ],
            [
                InlineKeyboardButton("🎯 Thresholds", callback_data="edit_thresholds"),
                InlineKeyboardButton("🕒 Timeframes", callback_data="edit_timeframes")
            ]
        ]
        
        # Add futures-specific settings if in futures mode
        if trading_mode == 'futures':
            keyboard.append([
                InlineKeyboardButton("⚙️ Futures Settings", callback_data="futures_settings")
            ])
            
        # Add back button
        keyboard.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="show_main_menu")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message_text = f"""
⚙️ *Bot Settings*

*Current Configuration:*
• Mode: {trading_mode.upper()}
• Base Currency: {self.config['trading']['base_currency']}
• Reserve Balance: ${self.config['trading']['reserve_balance']:,.2f}
• Trading Pairs: {len(self.config['trading']['pairs'])}

Select a setting to modify:
"""
        
        if hasattr(update, 'callback_query'):
            await update.callback_query.edit_message_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )

    async def show_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show current bot status"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
        
        try:
            # Get current status information
            trading_mode = self.config['environment']['trading_mode'].upper()
            is_testnet = self.config['environment']['testnet']
            reserve_balance = self.config['trading']['reserve_balance']
            
            # Get current balance
            current_balance = await self.get_balance()
            
            # Get order counts
            pending_orders = await self.mongo_client.orders.count_documents({"status": OrderStatus.PENDING.value})
            filled_orders = await self.mongo_client.orders.count_documents({"status": OrderStatus.FILLED.value})
            
            # Get position count if in futures mode
            position_count = 0
            unrealized_pnl = 0
            if trading_mode == 'FUTURES':
                positions = await self.binance_client.get_open_positions()
                position_count = len(positions)
                account_info = await self.binance_client.get_account_info()
                unrealized_pnl = float(account_info.get('totalUnrealizedProfit', 0))
            
            # Create status message
            status_message = f"""
🦖 *Trade-a-saurus Rex Status*

*System:*
• Mode: {trading_mode} ({'TESTNET' if is_testnet else 'MAINNET'})
• Status: {'⏸️ PAUSED' if self.is_paused else '▶️ ACTIVE'}
• Uptime: {self._get_uptime()}

*Account:*
• Balance: ${float(current_balance):,.2f}
• Reserve: ${float(reserve_balance):,.2f}
• Available for trading: ${float(current_balance) - float(reserve_balance):,.2f}

*Trading:*
• Pending Orders: {pending_orders}
• Filled Orders: {filled_orders}"""

            # Add futures info if applicable
            if trading_mode == 'FUTURES':
                status_message += f"""
• Open Positions: {position_count}
• Unrealized P/L: ${unrealized_pnl:+,.2f}"""
            
            # Add bot version and environment info
            status_message += f"""

*Bot Version:* 1.0
*Last updated:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

Use /menu to access all commands.
"""
            
            # Create keyboard with refresh button and main menu
            keyboard = [
                [
                    InlineKeyboardButton("🔄 Refresh Status", callback_data="refresh_status"),
                    InlineKeyboardButton("🔙 Main Menu", callback_data="show_main_menu")
                ]
            ]
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                status_message,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            
        except Exception as e:
            logger.error(f"Error showing status: {e}")
            await update.message.reply_text(
                f"❌ Error retrieving status: {str(e)}\n\nPlease try again later."
            )

    def _get_uptime(self):
        """Get bot uptime formatted nicely"""
        if not hasattr(self, '_start_time'):
            self._start_time = datetime.now()
            
        uptime = datetime.now() - self._start_time
        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            return f"{hours}h {minutes}m {seconds}s"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        else:
            return f"{seconds}s"

    async def show_admin_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin menu for privileged operations"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        # Create admin keyboard
        keyboard = [
            [
                InlineKeyboardButton("🔄 Restart Bot", callback_data="admin_restart"),
                InlineKeyboardButton("⚠️ Emergency Stop", callback_data="admin_emergency_stop")
            ],
            [
                InlineKeyboardButton("🧹 Clear History", callback_data="admin_clear_history"),
                InlineKeyboardButton("📊 Debug Info", callback_data="admin_debug")
            ],
            [
                InlineKeyboardButton("📋 View Logs", callback_data="admin_logs")
            ],
            [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="show_main_menu")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message_text = """
🔐 *Admin Functions*

⚠️ These commands perform privileged operations:
• Restart the trading bot
• Emergency stop all trading
• Clear trading history
• View debug information
• View system logs

*Use with caution!*
"""
        
        await update.message.reply_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    async def handle_menu_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Central handler for all menu callbacks"""
        query = update.callback_query
        await query.answer()
        
        callback_data = query.data
        
        # Main menu navigation callbacks
        if callback_data == "show_main_menu":
            await self.show_main_menu(update, context)
        elif callback_data == "menu_trade":
            await self.show_trade_menu(update, context)
        elif callback_data == "menu_account":
            await self.show_account_menu(update, context)
        elif callback_data == "menu_stats":
            await self.show_stats_menu(update, context)
        elif callback_data == "menu_charts":
            await self.show_charts_menu(update, context)
        elif callback_data == "menu_settings":
            await self.show_settings_menu(update, context)
        elif callback_data == "toggle_trading":
            # Toggle trading status
            self.is_paused = not self.is_paused
            await self.show_main_menu(update, context)
            
        # Account section callbacks
        elif callback_data == "show_balance":
            await self.handle_show_balance(update, context)
        elif callback_data == "show_pnl":
            await self.show_profits(update, context)
        elif callback_data == "show_portfolio":
            await self.handle_show_portfolio(update, context)
        elif callback_data == "show_positions":
            await self.show_positions(update, context)
            
        # Trade section callbacks
        elif callback_data == "new_trade":
            await self.add_trade_start(update, context)
        elif callback_data == "pending_orders":
            await self.handle_pending_orders(update, context)
        elif callback_data == "order_history":
            await self.get_order_history(update, context)
        elif callback_data == "set_leverage":
            await self.handle_set_leverage(update, context)
        elif callback_data == "set_margin":
            await self.handle_set_margin(update, context)
        elif callback_data == "toggle_hedge":
            await self.handle_toggle_hedge(update, context)
            
        # Stats section callbacks
        elif callback_data == "show_thresholds":
            await self.show_thresholds(update, context)
        elif callback_data == "show_trading_stats":
            await self.get_stats(update, context)
        
        # Charts section callbacks
        elif callback_data.startswith("chart_"):
            symbol = callback_data[6:]  # Extract symbol from callback_data
            await self.handle_show_chart(update, context, symbol)
        elif callback_data.startswith("viz_"):
            viz_type = callback_data[4:]  # Extract visualization type
            await self.handle_visualization(update, context, viz_type)
            
        # Settings section callbacks
        elif callback_data == "toggle_mode":
            await self.handle_toggle_mode(update, context)
        elif callback_data == "set_trade_amount":
            await self.handle_set_trade_amount(update, context)
        elif callback_data == "set_reserve":
            await self.handle_set_reserve(update, context)
        elif callback_data == "edit_thresholds":
            await self.handle_edit_thresholds(update, context)
        
        # Admin section callbacks
        elif callback_data.startswith("admin_"):
            action = callback_data[6:]
            await self.handle_admin_action(update, context, action)
        
        # Other callbacks
        else:
            await query.edit_message_text(
                f"Unknown callback: {callback_data}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Main Menu", callback_data="show_main_menu")]
                ])
            )

    async def show_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show help information with streamlined commands"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        help_text = """
🦖 *Trade-a-saurus Rex Bot - Help*

*Core Commands:*
/menu - Main command menu
/trade - Trading operations
/account - Account information
/settings - Bot configuration
/stats - Trading statistics
/charts - View price charts
/power - Toggle trading on/off
/status - Check bot status
/help - Show this help
/admin - Admin commands

Use these commands to navigate through all bot functions. 
Each command opens a menu with more specific options.
        """
        
        await update.message.reply_text(
            help_text,
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Main Menu", callback_data="show_main_menu")]
            ])
        )

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
        """Show menu for switching between spot and futures trading modes"""
        query = update.callback_query
        if query:
            await query.answer()
        
        # Get current mode from settings
        settings = await self.mongo_client.get_settings("trading")
        current_mode = settings.get("mode", "spot")
        
        # Create keyboard with mode options
        keyboard = [
            [
                InlineKeyboardButton("Spot Trading", callback_data="mode:spot"),
                InlineKeyboardButton("Futures Trading", callback_data="mode:futures")
            ],
            [InlineKeyboardButton("Back to Main Menu", callback_data="menu:main")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Determine which message to edit or send
        if query:
            await query.edit_message_text(
                text=f"Current trading mode: {current_mode.upper()}\n\n"
                     f"Select trading mode:",
                reply_markup=reply_markup
            )
        else:
            # If called directly without a callback query
            message = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"Current trading mode: {current_mode.upper()}\n\n"
                     f"Select trading mode:",
                reply_markup=reply_markup
            )

    async def handle_mode_switch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle switching between spot and futures trading modes"""
        query = update.callback_query
        await query.answer()
        
        # Extract the selected mode from the callback data
        # Expected format: "mode:spot" or "mode:futures"
        selected_mode = query.data.split(":")[1]
        
        # Update the trading mode in settings
        await self.mongo_client.update_setting("trading", "mode", selected_mode)
        
        # Send confirmation message
        mode_display = "Spot" if selected_mode == "spot" else "Futures"
        await query.edit_message_text(
            text=f"Trading mode switched to {mode_display}. All new trades will use this mode.",
            reply_markup=None
        )
        
        # Show the main menu again
        await self.show_main_menu(update, context)

    async def show_profits(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show profit information with mode awareness"""
        try:
            # Check if this is from a callback query
            is_callback = bool(update.callback_query)
            message = update.callback_query.message if is_callback else update.message
            
            # Get current trading mode
            trading_mode = self.config['environment']['trading_mode']
            is_futures = trading_mode == 'futures'
            
            # Show loading message
            if not is_callback:
                loading_message = await message.reply_text("Calculating profit information...")
            
            # Format message based on mode
            if is_futures:
                # Get futures PnL data
                symbols = self.config['trading']['pairs']
                
                # Create message header
                profit_text = "*Futures Profit/Loss Summary*\n\n"
                
                # Get overall PnL
                summary = await self.mongo_client.get_trading_summary(include_futures=True)
                futures_data = summary.get('futures_orders', {})
                
                profit_text += (
                    f"*Overall Performance*\n"
                    f"Total Realized PnL: ${float(futures_data.get('total_pnl', 0)):+,.2f}\n"
                    f"Total Trades: {futures_data.get('total_orders', 0)}\n\n"
                )
                
                # Add unrealized PnL
                if 'unrealized_pnl' in summary:
                    profit_text += f"Unrealized PnL: ${float(summary['unrealized_pnl']):+,.2f}\n\n"
                
                # Get PnL by symbol
                profit_text += "*PnL by Symbol*\n"
                
                # Process each symbol
                for symbol in symbols:
                    try:
                        # Get current price
                        ticker = await self.binance_client.get_symbol_ticker(symbol)
                        current_price = float(ticker.get('price', 0))
                        
                        # Calculate PnL for this symbol
                        pnl_data = await self.mongo_client.calculate_futures_pnl(symbol)
                        
                        # Get position data if active
                        position_data = await self.mongo_client.get_position(symbol)
                        
                        if pnl_data or position_data:
                            # Add symbol header
                            profit_text += f"\n*{symbol}*\n"
                            
                            # Add realized PnL
                            if pnl_data:
                                realized_pnl = float(pnl_data.get('realized_pnl', 0))
                                trade_count = pnl_data.get('trade_count', 0)
                                profit_text += f"Realized PnL: ${realized_pnl:+,.2f} ({trade_count} trades)\n"
                            
                            # Add active position info
                            if position_data:
                                entry_price = float(position_data.get('entry_price', 0))
                                quantity = float(position_data.get('quantity', 0))
                                direction = position_data.get('direction', 'LONG')
                                leverage = position_data.get('leverage', 1)
                                
                                # Calculate unrealized PnL
                                if direction == 'LONG':
                                    pnl_pct = (current_price - entry_price) / entry_price * 100 * leverage
                                    unrealized_pnl = quantity * (current_price - entry_price)
                                else:
                                    pnl_pct = (entry_price - current_price) / entry_price * 100 * leverage
                                    unrealized_pnl = quantity * (entry_price - current_price)
                                
                                profit_text += (
                                    f"Active Position: {direction} {abs(quantity):.4f} @ ${entry_price:.2f}\n"
                                    f"Current Price: ${current_price:.2f}\n"
                                    f"Unrealized PnL: ${unrealized_pnl:+,.2f} ({pnl_pct:+.2f}%)\n"
                                    f"Leverage: {leverage}x\n"
                                )
                
                    except Exception as e:
                        logger.error(f"Error calculating PnL for {symbol}: {e}")
                        profit_text += f"\n*{symbol}*: Error calculating PnL\n"
                
            else:
                # Get spot portfolio data
                position_stats = await self.mongo_client.get_position_stats()
                
                # Create message header
                profit_text = "*Spot Portfolio Profit Summary*\n\n"
                
                if position_stats:
                    # Add overall portfolio stats
                    profit_text += (
                        f"*Overall Portfolio*\n"
                        f"Total Value: ${float(position_stats.get('total_value', 0)):,.2f}\n"
                        f"Total Cost: ${float(position_stats.get('total_cost', 0)):,.2f}\n"
                        f"Total Profit: ${float(position_stats.get('total_profit', 0)):+,.2f}\n"
                        f"Profit %: {position_stats.get('profit_percentage', 0):+.2f}%\n\n"
                    )
                    
                    # Add positions breakdown
                    if position_stats.get('positions'):
                        profit_text += "*Positions*\n"
                        
                        # Sort positions by profit percentage
                        sorted_positions = sorted(
                            position_stats['positions'], 
                            key=lambda x: x.get('profit_percentage', 0),
                            reverse=True
                        )
                        
                        for pos in sorted_positions:
                            symbol = pos['symbol']
                            quantity = float(pos.get('quantity', 0))
                            avg_price = float(pos.get('avg_price', 0))
                            current_price = float(pos.get('current_price', 0))
                            profit = float(pos.get('profit', 0))
                            profit_pct = pos.get('profit_percentage', 0)
                            
                            profit_text += (
                                f"\n*{symbol}*\n"
                                f"Quantity: {quantity:.6f}\n"
                                f"Avg Price: ${avg_price:.2f}\n"
                                f"Current Price: ${current_price:.2f}\n"
                                f"Profit: ${profit:+,.2f} ({profit_pct:+.2f}%)\n"
                            )
                else:
                    profit_text += "No portfolio data available."
            
            # Delete loading message if exists
            if not is_callback and 'loading_message' in locals():
                await loading_message.delete()
            
            # Send profit message with back button
            if is_callback:
                await update.callback_query.edit_message_text(
                    profit_text,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Back", callback_data="section_market")]
                    ]),
                    parse_mode='Markdown'
                )
            else:
                await message.reply_text(
                    profit_text,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Menu", callback_data="main_menu")]
                    ]),
                    parse_mode='Markdown'
                )
            
        except Exception as e:
            logger.error(f"Error showing profits: {e}")
            
            # Handle error based on update type
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    f"Error calculating profits: {str(e)}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Back", callback_data="section_market")]
                    ])
                )
            else:
                await update.message.reply_text(
                    f"Error calculating profits: {str(e)}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Menu", callback_data="main_menu")]
                    ])
                )

    def _get_timeframe_value(self, timeframe):
        """Convert TimeFrame enum to readable string"""
        if timeframe == TimeFrame.DAILY:
            return "Daily"
        elif timeframe == TimeFrame.WEEKLY:
            return "Weekly"
        elif timeframe == TimeFrame.MONTHLY:
            return "Monthly"
        return str(timeframe)

    async def show_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show open futures positions"""
        query = update.callback_query
        
        try:
            # Check if we're in futures mode
            if self.config['environment']['trading_mode'] != 'futures':
                await query.edit_message_text(
                    "This command is only available in Futures mode.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Back", callback_data="section_market")]
                    ])
                )
                return
            
            # Get positions from futures client
            positions = await self.binance_client.get_open_positions()
            
            if not positions:
                await query.edit_message_text(
                    "No open positions found.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Back", callback_data="section_market")]
                    ])
                )
                return
            
            # Format positions
            message = "*Open Futures Positions*\n\n"
            
            for symbol, position in positions.items():
                entry_price = float(position.get('entryPrice', 0))
                amount = float(position.get('positionAmt', 0))
                leverage = int(position.get('leverage', 1))
                pnl = float(position.get('unrealizedProfit', 0))
                direction = "LONG" if amount > 0 else "SHORT"
                
                # Get current price
                ticker = await self.binance_client.get_symbol_ticker(symbol)
                current_price = float(ticker.get('price', 0))
                
                # Calculate PnL percentage
                if entry_price > 0 and amount != 0:
                    if direction == "LONG":
                        pnl_pct = (current_price - entry_price) / entry_price * 100 * leverage
                    else:
                        pnl_pct = (entry_price - current_price) / entry_price * 100 * leverage
                else:
                    pnl_pct = 0
                
                message += f"*{symbol}* ({direction})\n"
                message += f"Entry: ${entry_price:.2f} | Current: ${current_price:.2f}\n"
                message += f"Size: {abs(amount):.4f} | Leverage: {leverage}x\n"
                message += f"PnL: ${pnl:.2f} ({pnl_pct:.2f}%)\n\n"
            
            # Add back button
            await query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="section_market")]
                ]),
                parse_mode='Markdown'
            )
            
        except Exception as e:
            logger.error(f"Error showing positions: {e}")
            await query.edit_message_text(
                f"Error retrieving positions: {str(e)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="section_market")]
                ])
            )

    async def show_viz_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show visualization options menu"""
        try:
            query = update.callback_query
            if query:
                await query.answer()
            
            keyboard = [
                [InlineKeyboardButton("Equity Curve", callback_data="viz_equity_curve")],
                [InlineKeyboardButton("Trade Distribution", callback_data="viz_trade_distribution")],
                [InlineKeyboardButton("Performance Metrics", callback_data="viz_performance_metrics")],
                [InlineKeyboardButton("Price Charts", callback_data="viz_price_charts")],
                [InlineKeyboardButton("Back to Main Menu", callback_data="back_to_main")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            message = "📊 *Visualization Menu*\n\nSelect a visualization to generate:"
            
            if query:
                await query.edit_message_text(text=message, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text(text=message, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
                
        except Exception as e:
            logger.error(f"Error showing visualization menu: {e}")
            if update.callback_query:
                await update.callback_query.edit_message_text(text=f"Error showing visualization menu: {e}")
            else:
                await update.message.reply_text(text=f"Error showing visualization menu: {e}")
    
    async def handle_viz_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle visualization selection"""
        try:
            query = update.callback_query
            await query.answer()
            
            viz_type = query.data
            
            if viz_type == "viz_equity_curve":
                # Show mode selection for equity curve
                keyboard = [
                    [InlineKeyboardButton("Spot", callback_data="equity_curve_spot")],
                    [InlineKeyboardButton("Futures", callback_data="equity_curve_futures")],
                    [InlineKeyboardButton("Combined", callback_data="equity_curve_combined")],
                    [InlineKeyboardButton("Back", callback_data="back_to_viz_menu")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    text="Select mode for equity curve visualization:",
                    reply_markup=reply_markup
                )
            elif viz_type.startswith("equity_curve_"):
                mode = viz_type.replace("equity_curve_", "")
                await query.edit_message_text(text=f"Generating equity curve for {mode} mode...")
                
                # Generate and send equity curve
                await self.generate_equity_curve(update, context, mode)
                
            elif viz_type == "viz_trade_distribution":
                await query.edit_message_text(text="Generating trade distribution visualization...")
                
                # Generate and send trade distribution
                await self.generate_trade_distribution(update, context)
                
            elif viz_type == "viz_performance_metrics":
                await query.edit_message_text(text="Generating performance metrics visualization...")
                
                # Generate and send performance metrics
                await self.generate_performance_metrics(update, context)
                
            elif viz_type == "viz_price_charts":
                # Show symbol selection for price charts
                symbols = self.config.get("trading", {}).get("symbols", [])
                keyboard = []
                
                # Create rows of 2 symbols each
                for i in range(0, len(symbols), 2):
                    row = []
                    row.append(InlineKeyboardButton(symbols[i], callback_data=f"price_chart_{symbols[i]}"))
                    if i + 1 < len(symbols):
                        row.append(InlineKeyboardButton(symbols[i+1], callback_data=f"price_chart_{symbols[i+1]}"))
                    keyboard.append(row)
                
                keyboard.append([InlineKeyboardButton("Back", callback_data="back_to_viz_menu")])
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await query.edit_message_text(
                    text="Select symbol for price chart:",
                    reply_markup=reply_markup
                )
                
            elif viz_type.startswith("price_chart_"):
                symbol = viz_type.replace("price_chart_", "")
                await query.edit_message_text(text=f"Generating price chart for {symbol}...")
                
                # Generate and send price chart
                await self.generate_price_chart(update, context, symbol)
                
            elif viz_type == "back_to_viz_menu":
                await self.show_viz_menu(update, context)
                
        except Exception as e:
            logger.error(f"Error handling visualization selection: {e}")
            await query.edit_message_text(text=f"Error generating visualization: {e}")
    
    async def generate_equity_curve(self, update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
        """Generate and send equity curve visualization"""
        try:
            # Placeholder for actual implementation
            message = f"Equity curve for {mode} mode would be generated here."
            
            keyboard = [[InlineKeyboardButton("Back to Viz Menu", callback_data="back_to_viz_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.callback_query.edit_message_text(
                text=message,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Error generating equity curve: {e}")
            await update.callback_query.edit_message_text(text=f"Error generating equity curve: {e}")
    
    async def generate_trade_distribution(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Generate and send trade distribution visualization"""
        try:
            # Placeholder for actual implementation
            message = "Trade distribution visualization would be generated here."
            
            keyboard = [[InlineKeyboardButton("Back to Viz Menu", callback_data="back_to_viz_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.callback_query.edit_message_text(
                text=message,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Error generating trade distribution: {e}")
            await update.callback_query.edit_message_text(text=f"Error generating trade distribution: {e}")
    
    async def generate_performance_metrics(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Generate and send performance metrics visualization"""
        try:
            # Placeholder for actual implementation
            message = "Performance metrics visualization would be generated here."
            
            keyboard = [[InlineKeyboardButton("Back to Viz Menu", callback_data="back_to_viz_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.callback_query.edit_message_text(
                text=message,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Error generating performance metrics: {e}")
            await update.callback_query.edit_message_text(text=f"Error generating performance metrics: {e}")
    
    async def generate_price_chart(self, update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str) -> None:
        """Generate and send price chart visualization"""
        try:
            # Placeholder for actual implementation
            message = f"Price chart for {symbol} would be generated here."
            
            keyboard = [[InlineKeyboardButton("Back to Viz Menu", callback_data="back_to_viz_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.callback_query.edit_message_text(
                text=message,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Error generating price chart: {e}")
            await update.callback_query.edit_message_text(text=f"Error generating price chart: {e}")

    async def show_thresholds(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show current trading thresholds"""
        try:
            # Check if this is from a callback query
            is_callback = bool(update.callback_query)
            message = update.callback_query.message if is_callback else update.message
            
            # Get thresholds from config
            thresholds = self.config['trading']['thresholds']
            
            # Format message
            threshold_text = "*Trading Thresholds*\n\n"
            
            for timeframe, values in thresholds.items():
                threshold_text += f"*{timeframe.capitalize()}*: {', '.join([f'{v}%' for v in values])}\n"
            
            # Get triggered thresholds
            triggered = {}
            for symbol in self.config['trading']['pairs']:
                for timeframe in thresholds.keys():
                    triggered_values = await self.mongo_client.get_triggered_thresholds(symbol, timeframe)
                    if triggered_values:
                        if symbol not in triggered:
                            triggered[symbol] = {}
                        triggered[symbol][timeframe] = triggered_values
            
            # Add triggered thresholds to message
            if triggered:
                threshold_text += "\n*Recently Triggered Thresholds*:\n"
                for symbol, timeframes in triggered.items():
                    threshold_text += f"\n{symbol}:\n"
                    for timeframe, values in timeframes.items():
                        threshold_text += f"  {timeframe.capitalize()}: {', '.join([f'{v}%' for v in values])}\n"
            else:
                threshold_text += "\nNo recently triggered thresholds."
            
            # Send message with back button
            if is_callback:
                await update.callback_query.edit_message_text(
                    threshold_text,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Back", callback_data="section_trading")]
                    ]),
                    parse_mode='Markdown'
                )
            else:
                await message.reply_text(
                    threshold_text,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Menu", callback_data="main_menu")]
                    ]),
                    parse_mode='Markdown'
                )
            
        except Exception as e:
            logger.error(f"Error showing thresholds: {e}")
            
            # Handle error based on update type
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    f"Error showing thresholds: {str(e)}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Back", callback_data="section_trading")]
                    ])
                )
            else:
                await update.message.reply_text(
                    f"Error showing thresholds: {str(e)}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Menu", callback_data="main_menu")]
                    ])
                )

    async def add_trade_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Start the process of adding a new trade"""
        try:
            # Check if this is from a callback query
            query = update.callback_query
            if query:
                await query.answer()
                message = query.message
            else:
                message = update.message
                
            # Initialize temp data storage
            user_id = update.effective_user.id
            self.temp_trade_data[user_id] = {}
            
            # Ask for symbol
            await message.reply_text(
                "Please enter the trading pair symbol (e.g., BTCUSDT):",
                reply_markup=ReplyKeyboardMarkup([
                    [KeyboardButton("BTCUSDT"), KeyboardButton("ETHUSDT")],
                    [KeyboardButton("BNBUSDT"), KeyboardButton("ADAUSDT")],
                    [KeyboardButton("/cancel")]
                ], resize_keyboard=True)
            )
            return 1  # Move to symbol state
            
        except Exception as e:
            logger.error(f"Error in add_trade_start: {e}")
            if query:
                await query.edit_message_text(text=f"Error: {e}")
            else:
                await update.message.reply_text(text=f"Error: {e}")
            return ConversationHandler.END

    async def add_trade_symbol(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Process the symbol input and ask for order type"""
        user_id = update.effective_user.id
        symbol = update.message.text.strip().upper()
        
        # Store symbol in temp data
        self.temp_trade_data[user_id]['symbol'] = symbol
        
        # Create keyboard for order type selection
        keyboard = [
            [InlineKeyboardButton("Market Order", callback_data="market")],
            [InlineKeyboardButton("Limit Order", callback_data="limit")],
            [InlineKeyboardButton("Cancel", callback_data="cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"Symbol: {symbol}\nSelect order type:",
            reply_markup=reply_markup
        )
        return 2  # Move to order type state

    async def add_trade_order_type(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Process order type and ask for leverage (if futures)"""
        query = update.callback_query
        await query.answer()
        
        user_id = update.effective_user.id
        order_type = query.data
        
        if order_type == "cancel":
            await query.edit_message_text("Trade creation cancelled.")
            return ConversationHandler.END
            
        # Store order type
        self.temp_trade_data[user_id]['order_type'] = order_type
        
        # Check if we're in futures mode
        trading_mode = self.config['environment']['trading_mode']
        is_futures = trading_mode == 'futures'
        
        if is_futures:
            # Ask for leverage
            await query.edit_message_text(
                f"Symbol: {self.temp_trade_data[user_id]['symbol']}\n"
                f"Order Type: {order_type.capitalize()}\n\n"
                f"Enter leverage (1-125):"
            )
            return 3  # Move to leverage state
        else:
            # Skip leverage for spot trading
            self.temp_trade_data[user_id]['leverage'] = 1
            
            # Ask for direction
            keyboard = [
                [InlineKeyboardButton("BUY", callback_data="long")],
                [InlineKeyboardButton("SELL", callback_data="short")],
                [InlineKeyboardButton("Cancel", callback_data="cancel")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"Symbol: {self.temp_trade_data[user_id]['symbol']}\n"
                f"Order Type: {order_type.capitalize()}\n\n"
                f"Select direction:",
                reply_markup=reply_markup
            )
            return 4  # Move to direction state

    async def add_trade_leverage(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Process leverage input and ask for direction"""
        user_id = update.effective_user.id
        
        try:
            leverage = int(update.message.text.strip())
            if leverage < 1 or leverage > 125:
                await update.message.reply_text(
                    "Leverage must be between 1 and 125. Please try again:"
                )
                return 3  # Stay in leverage state
                
            # Store leverage
            self.temp_trade_data[user_id]['leverage'] = leverage
            
            # Ask for direction
            keyboard = [
                [InlineKeyboardButton("LONG", callback_data="long")],
                [InlineKeyboardButton("SHORT", callback_data="short")],
                [InlineKeyboardButton("Cancel", callback_data="cancel")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"Symbol: {self.temp_trade_data[user_id]['symbol']}\n"
                f"Order Type: {self.temp_trade_data[user_id]['order_type'].capitalize()}\n"
                f"Leverage: {leverage}x\n\n"
                f"Select direction:",
                reply_markup=reply_markup
            )
            return 4  # Move to direction state
            
        except ValueError:
            await update.message.reply_text(
                "Please enter a valid number for leverage:"
            )
            return 3  # Stay in leverage state

    async def add_trade_direction(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Process direction and ask for amount"""
        query = update.callback_query
        await query.answer()
        
        user_id = update.effective_user.id
        direction = query.data
        
        if direction == "cancel":
            await query.edit_message_text("Trade creation cancelled.")
            return ConversationHandler.END
            
        # Store direction
        self.temp_trade_data[user_id]['direction'] = direction
        
        # Ask for amount
        await query.edit_message_text(
            f"Symbol: {self.temp_trade_data[user_id]['symbol']}\n"
            f"Order Type: {self.temp_trade_data[user_id]['order_type'].capitalize()}\n"
            f"Direction: {direction.upper()}\n\n"
            f"Enter amount in USDT:"
        )
        return 5  # Move to amount state

    async def add_trade_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Process amount and ask for fees"""
        user_id = update.effective_user.id
        
        try:
            amount = float(update.message.text.strip())
            if amount <= 0:
                await update.message.reply_text(
                    "Amount must be greater than 0. Please try again:"
                )
                return 5  # Stay in amount state
                
            # Store amount
            self.temp_trade_data[user_id]['amount'] = amount
            
            # Ask for fees
            await update.message.reply_text(
                f"Symbol: {self.temp_trade_data[user_id]['symbol']}\n"
                f"Order Type: {self.temp_trade_data[user_id]['order_type'].capitalize()}\n"
                f"Direction: {self.temp_trade_data[user_id]['direction'].upper()}\n"
                f"Amount: ${amount}\n\n"
                f"Enter fees (or 0 if unknown):"
            )
            return 6  # Move to fees state
            
        except ValueError:
            await update.message.reply_text(
                "Please enter a valid number for amount:"
            )
            return 5  # Stay in amount state

    async def add_trade_fees(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Process fees and show confirmation"""
        user_id = update.effective_user.id
        
        try:
            fees = float(update.message.text.strip())
            if fees < 0:
                await update.message.reply_text(
                    "Fees cannot be negative. Please try again:"
                )
                return 6  # Stay in fees state
                
            # Store fees
            self.temp_trade_data[user_id]['fees'] = fees
            
            # Show confirmation
            trade_data = self.temp_trade_data[user_id]
            
            confirmation_text = (
                f"📝 Trade Summary:\n\n"
                f"Symbol: {trade_data['symbol']}\n"
                f"Order Type: {trade_data['order_type'].capitalize()}\n"
                f"Direction: {trade_data['direction'].upper()}\n"
                f"Amount: ${trade_data['amount']}\n"
                f"Fees: ${trade_data['fees']}\n"
            )
            
            if 'leverage' in trade_data and trade_data['leverage'] > 1:
                confirmation_text += f"Leverage: {trade_data['leverage']}x\n"
                
            confirmation_text += "\nConfirm this trade?"
            
            keyboard = [
                [InlineKeyboardButton("Confirm", callback_data="confirm")],
                [InlineKeyboardButton("Cancel", callback_data="cancel")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                confirmation_text,
                reply_markup=reply_markup
            )
            return 7  # Move to confirmation state
            
        except ValueError:
            await update.message.reply_text(
                "Please enter a valid number for fees:"
            )
            return 6  # Stay in fees state

    async def add_trade_final(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Process final confirmation and create the trade"""
        query = update.callback_query
        await query.answer()
        
        user_id = update.effective_user.id
        action = query.data
        
        if action == "cancel":
            await query.edit_message_text("Trade creation cancelled.")
            return ConversationHandler.END
            
        # Get trade data
        trade_data = self.temp_trade_data[user_id]
        
        try:
            # Create order object
            from ..types.models import Order, OrderStatus, TimeFrame, OrderType, TradeDirection
            from decimal import Decimal
            from datetime import datetime
            import uuid
            
            # Get current price
            ticker = await self.binance_client.get_symbol_ticker(trade_data['symbol'])
            current_price = float(ticker['price'])
            
            # Create order
            order = Order(
                symbol=trade_data['symbol'],
                status=OrderStatus.FILLED,  # Manual trades are always filled
                order_type=OrderType.FUTURES if self.config['environment']['trading_mode'] == 'futures' else OrderType.SPOT,
                price=Decimal(str(current_price)),
                quantity=Decimal(str(trade_data['amount'] / current_price)),
                timeframe=TimeFrame.DAILY,  # Default to daily
                order_id=str(uuid.uuid4()),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                filled_at=datetime.utcnow(),
                fees=Decimal(str(trade_data['fees'])),
                fee_asset='USDT',
                is_manual=True
            )
            
            # Add futures-specific fields
            if order.order_type == OrderType.FUTURES:
                order.leverage = int(trade_data['leverage'])
                order.direction = TradeDirection.LONG if trade_data['direction'] == 'long' else TradeDirection.SHORT
            
            # Insert into database
            result = await self.mongo_client.insert_manual_trade(order)
            
            if result:
                # Send success message
                await query.edit_message_text(
                    f"✅ Trade successfully added!\n\n"
                    f"Symbol: {order.symbol}\n"
                    f"Price: ${float(order.price):.2f}\n"
                    f"Quantity: {float(order.quantity):.8f}\n"
                    f"Total Value: ${float(order.price * order.quantity):.2f}"
                )
                
                # Send notification
                await self.send_order_notification(order, OrderStatus.FILLED)
                
                # Clear temp data
                del self.temp_trade_data[user_id]
                
                # Reset keyboard
                await context.bot.send_message(
                    chat_id=user_id,
                    text="What would you like to do next?",
                    reply_markup=self.markup
                )
            else:
                await query.edit_message_text(
                    "❌ Failed to add trade. Please try again."
                )
                
            return ConversationHandler.END
            
        except Exception as e:
            logger.error(f"Error creating trade: {e}")
            await query.edit_message_text(
                f"❌ Error creating trade: {str(e)}"
            )
            return ConversationHandler.END

    async def add_trade_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Cancel the trade creation process"""
        user_id = update.effective_user.id
        
        # Clear temp data
        if user_id in self.temp_trade_data:
            del self.temp_trade_data[user_id]
        
        # Check if this is from a callback query
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text("Trade creation cancelled.")
        else:
            await update.message.reply_text("Trade creation cancelled.")
        
        # Reset keyboard
        await context.bot.send_message(
            chat_id=user_id,
            text="What would you like to do next?",
            reply_markup=self.markup
        )
        
        return ConversationHandler.END

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle errors in the telegram bot"""
        try:
            # Log the error
            logger.error(f"Exception while handling an update: {context.error}")
            
            # Extract the traceback
            import traceback
            tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
            tb_string = ''.join(tb_list)
            
            # Log the traceback
            logger.error(f"Traceback: {tb_string}")
            
            # Send error message to user if possible
            if update and hasattr(update, 'effective_chat'):
                error_message = (
                    "❌ An error occurred while processing your request.\n"
                    "The error has been logged and will be addressed."
                )
                
                # Add error details for allowed users
                if update.effective_user and update.effective_user.id in self.allowed_users:
                    error_message += f"\n\nError: {str(context.error)}"
                
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=error_message
                )
                
            # Send detailed error to admin
            for admin_id in self.allowed_users:
                try:
                    # Only send first 3000 chars to avoid message size limits
                    error_details = (
                        f"⚠️ Bot Error Report\n\n"
                        f"Error: {str(context.error)}\n\n"
                        f"Traceback (truncated):\n{tb_string[:3000]}..."
                    )
                    
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=error_details
                    )
                except Exception as e:
                    logger.error(f"Failed to send error report to admin {admin_id}: {e}")
                    
        except Exception as e:
            logger.error(f"Error in error handler: {e}")

    async def set_leverage_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Command to set leverage for a specific symbol"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        # Check if we're in futures mode
        if self.config['environment']['trading_mode'] != 'futures':
            await update.message.reply_text(
                "❌ This command is only available in futures mode.\n"
                "Use /trademode to switch to futures mode."
            )
            return

        # Check for correct arguments: /leverage SYMBOL LEVERAGE
        if not context.args or len(context.args) != 2:
            await update.message.reply_text(
                "Usage: /leverage SYMBOL LEVERAGE\n"
                "Example: /leverage BTCUSDT 10"
            )
            return

        symbol = context.args[0].upper()  # Convert to uppercase
        try:
            leverage = int(context.args[1])
            if leverage < 1 or leverage > 125:
                await update.message.reply_text("❌ Leverage must be between 1 and 125")
                return
        except ValueError:
            await update.message.reply_text("❌ Leverage must be a valid integer")
            return

        try:
            # Show "setting leverage" message first
            message = await update.message.reply_text(f"Setting leverage for {symbol} to {leverage}x...")

            # Use futures client to set leverage
            success = await self.binance_client.set_leverage(symbol, leverage)

            if success:
                await message.edit_text(
                    f"✅ Leverage for {symbol} set to {leverage}x successfully!"
                )
            else:
                await message.edit_text(
                    f"❌ Failed to set leverage for {symbol}. Please check if the symbol is valid."
                )
        except Exception as e:
            logger.error(f"Error setting leverage: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")

    async def set_margin_type_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Command to set margin type for a specific symbol"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        # Check if we're in futures mode
        if self.config['environment']['trading_mode'] != 'futures':
            await update.message.reply_text(
                "❌ This command is only available in futures mode.\n"
                "Use /trademode to switch to futures mode."
            )
            return

        # Check for correct arguments: /margin SYMBOL TYPE
        if not context.args or len(context.args) != 2:
            await update.message.reply_text(
                "Usage: /margin SYMBOL TYPE\n"
                "Example: /margin BTCUSDT ISOLATED\n"
                "Valid types: ISOLATED, CROSSED"
            )
            return

        symbol = context.args[0].upper()  # Convert to uppercase
        margin_type = context.args[1].upper()  # Convert to uppercase

        if margin_type not in ["ISOLATED", "CROSSED"]:
            await update.message.reply_text(
                "❌ Margin type must be either ISOLATED or CROSSED"
            )
            return

        try:
            # Show "setting margin type" message first
            message = await update.message.reply_text(f"Setting margin type for {symbol} to {margin_type}...")

            # Use futures client to set margin type
            success = await self.binance_client.set_margin_type(symbol, margin_type)

            if success:
                await message.edit_text(
                    f"✅ Margin type for {symbol} set to {margin_type} successfully!"
                )
            else:
                await message.edit_text(
                    f"❌ Failed to set margin type for {symbol}. Please check if the symbol is valid."
                )
        except Exception as e:
            logger.error(f"Error setting margin type: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")

    async def toggle_hedge_mode_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Command to toggle between ONE-WAY and HEDGE position modes"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        # Check if we're in futures mode
        if self.config['environment']['trading_mode'] != 'futures':
            await update.message.reply_text(
                "❌ This command is only available in futures mode.\n"
                "Use /trademode to switch to futures mode."
            )
            return

        try:
            # Show "checking current mode" message first
            message = await update.message.reply_text("Checking current position mode...")

            # Get current position mode
            current_mode = await self.binance_client.get_position_mode()
            
            # Calculate target mode (toggle)
            target_mode = "HEDGE" if current_mode == "ONE_WAY" else "ONE_WAY"
            
            # Update message to show we're changing the mode
            await message.edit_text(f"Changing position mode from {current_mode} to {target_mode}...")
            
            # Set the new position mode
            success = await self.binance_client.set_position_mode(target_mode)
            
            if success:
                await message.edit_text(
                    f"✅ Position mode changed to {target_mode} successfully!\n\n"
                    f"ONE_WAY: Single position per symbol\n"
                    f"HEDGE: Separate long and short positions"
                )
            else:
                await message.edit_text(
                    f"❌ Failed to change position mode to {target_mode}."
                )
        except Exception as e:
            logger.error(f"Error toggling hedge mode: {e}")
            await update.message.reply_text(f"❌ Error: {str(e)}")

    async def handle_show_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle showing balance from callback"""
        await self.get_balance_command(update, context)

    async def handle_show_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle showing portfolio from callback"""
        try:
            # Get portfolio data from MongoDB
            portfolio_data = await self.mongo_client.get_position_stats()
            
            message_text = "*Portfolio Overview*\n\n"
            
            if portfolio_data and portfolio_data.get('positions'):
                total_value = float(portfolio_data.get('total_value', 0))
                total_cost = float(portfolio_data.get('total_cost', 0))
                total_profit = float(portfolio_data.get('total_profit', 0))
                profit_percentage = portfolio_data.get('profit_percentage', 0)
                
                message_text += (
                    f"Total Value: ${total_value:,.2f}\n"
                    f"Total Cost: ${total_cost:,.2f}\n"
                    f"Total Profit/Loss: ${total_profit:+,.2f} ({profit_percentage:+.2f}%)\n\n"
                    f"*Holdings:*\n"
                )
                
                # Sort positions by value
                sorted_positions = sorted(
                    portfolio_data['positions'], 
                    key=lambda x: float(x.get('value', 0)),
                    reverse=True
                )
                
                for position in sorted_positions:
                    symbol = position['symbol']
                    quantity = float(position.get('quantity', 0))
                    value = float(position.get('value', 0))
                    profit = float(position.get('profit', 0))
                    profit_pct = position.get('profit_percentage', 0)
                    
                    message_text += (
                        f"\n*{symbol}*\n"
                        f"Amount: {quantity:.6f}\n"
                        f"Value: ${value:.2f}\n"
                        f"P/L: ${profit:+,.2f} ({profit_pct:+.2f}%)\n"
                    )
            else:
                message_text += "No portfolio data available."
            
            # Create keyboard with back button
            keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="menu_account")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Send or edit message based on update type
            if hasattr(update, 'callback_query'):
                await update.callback_query.edit_message_text(
                    message_text,
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text(
                    message_text,
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                
        except Exception as e:
            logger.error(f"Error showing portfolio: {e}")
            error_text = f"Error showing portfolio: {str(e)}"
            
            if hasattr(update, 'callback_query'):
                await update.callback_query.edit_message_text(error_text)
            else:
                await update.message.reply_text(error_text)

    async def handle_pending_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle showing pending orders"""
        try:
            # Get pending orders from database
            pending_orders = await self.mongo_client.get_pending_orders()
            
            if not pending_orders:
                message_text = "No pending orders found."
            else:
                message_text = "*Pending Orders*\n\n"
                
                for order in pending_orders:
                    # Calculate time since creation
                    time_since = datetime.utcnow() - order.created_at
                    hours = time_since.total_seconds() / 3600
                    
                    message_text += (
                        f"*{order.symbol}*\n"
                        f"Price: ${float(order.price):.2f}\n"
                        f"Quantity: {float(order.quantity):.8f}\n"
                        f"Total: ${float(order.price * order.quantity):.2f}\n"
                        f"Age: {hours:.1f} hours\n\n"
                    )
            
            # Create keyboard with back button and cancel option
            keyboard = [
                [InlineKeyboardButton("🔙 Back", callback_data="menu_trade")],
            ]
            
            # Add cancel all button if there are pending orders
            if pending_orders:
                keyboard.insert(0, [InlineKeyboardButton("❌ Cancel All", callback_data="cancel_all_orders")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Send or edit message based on update type
            if hasattr(update, 'callback_query'):
                await update.callback_query.edit_message_text(
                    message_text,
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text(
                    message_text,
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                
        except Exception as e:
            logger.error(f"Error showing pending orders: {e}")
            error_text = f"Error showing pending orders: {str(e)}"
            
            if hasattr(update, 'callback_query'):
                await update.callback_query.edit_message_text(error_text)
            else:
                await update.message.reply_text(error_text)

    async def handle_set_leverage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle leverage setting from menu"""
        try:
            # Check if we're in futures mode
            if self.config['environment']['trading_mode'] != 'futures':
                await update.callback_query.edit_message_text(
                    "Leverage settings are only available in futures mode.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Back", callback_data="menu_trade")]
                    ])
                )
                return
            
            # Get available symbols
            symbols = self.config['trading']['pairs']
            
            # Create symbol selection keyboard
            keyboard = []
            row = []
            for i, symbol in enumerate(symbols):
                if i > 0 and i % 2 == 0:  # Create a new row every 2 symbols
                    keyboard.append(row)
                    row = []
                row.append(InlineKeyboardButton(symbol, callback_data=f"leverage_symbol_{symbol}"))
            
            if row:  # Add the last row if it has items
                keyboard.append(row)
            
            # Add back button
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="menu_trade")])
            
            await update.callback_query.edit_message_text(
                "Select a symbol to set leverage:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
        except Exception as e:
            logger.error(f"Error showing leverage dialog: {e}")
            await update.callback_query.edit_message_text(
                f"Error: {str(e)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="menu_trade")]
                ])
            )

    async def handle_set_margin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle margin type setting from menu"""
        try:
            # Check if we're in futures mode
            if self.config['environment']['trading_mode'] != 'futures':
                await update.callback_query.edit_message_text(
                    "Margin type settings are only available in futures mode.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Back", callback_data="menu_trade")]
                    ])
                )
                return
            
            # Get available symbols
            symbols = self.config['trading']['pairs']
            
            # Create symbol selection keyboard
            keyboard = []
            row = []
            for i, symbol in enumerate(symbols):
                if i > 0 and i % 2 == 0:  # Create a new row every 2 symbols
                    keyboard.append(row)
                    row = []
                row.append(InlineKeyboardButton(symbol, callback_data=f"margin_symbol_{symbol}"))
            
            if row:  # Add the last row if it has items
                keyboard.append(row)
            
            # Add back button
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="menu_trade")])
            
            await update.callback_query.edit_message_text(
                "Select a symbol to set margin type:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
        except Exception as e:
            logger.error(f"Error showing margin type dialog: {e}")
            await update.callback_query.edit_message_text(
                f"Error: {str(e)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="menu_trade")]
                ])
            )

    async def handle_toggle_hedge(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle toggling hedge mode from menu"""
        try:
            # Check if we're in futures mode
            if self.config['environment']['trading_mode'] != 'futures':
                await update.callback_query.edit_message_text(
                    "Hedge mode settings are only available in futures mode.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Back", callback_data="menu_trade")]
                    ])
                )
                return
            
            # Get current position mode
            current_mode = await self.binance_client.get_position_mode()
            target_mode = "HEDGE" if current_mode == "ONE_WAY" else "ONE_WAY"
            
            # Create confirmation keyboard
            keyboard = [
                [
                    InlineKeyboardButton(f"Change to {target_mode}", callback_data=f"confirm_hedge_{target_mode}")
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="menu_trade")]
            ]
            
            await update.callback_query.edit_message_text(
                f"Current position mode: {current_mode}\n\n"
                f"ONE_WAY: Single position per symbol\n"
                f"HEDGE: Separate long and short positions\n\n"
                f"Do you want to change to {target_mode} mode?",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
        except Exception as e:
            logger.error(f"Error showing hedge mode dialog: {e}")
            await update.callback_query.edit_message_text(
                f"Error: {str(e)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="menu_trade")]
                ])
            )

    async def handle_show_chart(self, update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str):
        """Handle showing chart for a specific symbol"""
        try:
            # Show loading message
            await update.callback_query.edit_message_text(
                f"Generating chart for {symbol}...",
            )
            
            # Get current price
            ticker = await self.binance_client.get_symbol_ticker(symbol=symbol)
            current_price = float(ticker['price'])
            
            # Get candles for symbol (using daily timeframe)
            candles = await self.binance_client.get_candles_for_chart(
                symbol=symbol, 
                timeframe=TimeFrame.DAILY
            )
            
            if not candles:
                await update.callback_query.edit_message_text(
                    f"No chart data available for {symbol}.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Back", callback_data="menu_charts")]
                    ])
                )
                return
            
            # Generate chart image
            chart_data = await self.binance_client.chart_generator.generate_chart(
                symbol=symbol,
                candles=candles
            )
            
            # Create caption
            caption = (
                f"{symbol} Chart\n"
                f"Current Price: ${current_price:.2f}"
            )
            
            # Send chart photo
            await update.callback_query.delete_message()
            await context.bot.send_photo(
                chat_id=update.effective_user.id,
                photo=chart_data,
                caption=caption,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back to Charts", callback_data="menu_charts")]
                ])
            )
            
        except Exception as e:
            logger.error(f"Error showing chart for {symbol}: {e}")
            await update.callback_query.edit_message_text(
                f"Error generating chart for {symbol}: {str(e)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="menu_charts")]
                ])
            )

    async def handle_visualization(self, update: Update, context: ContextTypes.DEFAULT_TYPE, viz_type: str):
        """Handle generating visualization"""
        try:
            # Show loading message
            await update.callback_query.edit_message_text(
                f"Generating {viz_type} visualization...",
            )
            
            # Generate visualization based on type
            if viz_type == "equity":
                # Generate equity curve
                viz_data = await self.mongo_client.generate_equity_curve()
            elif viz_type == "performance":
                # Generate performance chart
                viz_data = await self.mongo_client.generate_performance_metrics()
            else:
                await update.callback_query.edit_message_text(
                    f"Unknown visualization type: {viz_type}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Back", callback_data="menu_charts")]
                    ])
                )
                return
            
            if not viz_data:
                await update.callback_query.edit_message_text(
                    "No data available for visualization.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Back", callback_data="menu_charts")]
                    ])
                )
                return
            
            # Send visualization
            await update.callback_query.delete_message()
            await context.bot.send_photo(
                chat_id=update.effective_user.id,
                photo=viz_data,
                caption=f"{viz_type.capitalize()} Visualization",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back to Charts", callback_data="menu_charts")]
                ])
            )
            
        except Exception as e:
            logger.error(f"Error generating {viz_type} visualization: {e}")
            await update.callback_query.edit_message_text(
                f"Error generating visualization: {str(e)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="menu_charts")]
                ])
            )

    async def handle_toggle_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle toggling between spot and futures mode"""
        try:
            # Get current mode
            current_mode = self.config['environment']['trading_mode']
            target_mode = "futures" if current_mode == "spot" else "spot"
            
            # Show confirmation dialog
            keyboard = [
                [
                    InlineKeyboardButton(f"Switch to {target_mode.upper()}", callback_data=f"confirm_mode_{target_mode}")
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="menu_settings")]
            ]
            
            await update.callback_query.edit_message_text(
                f"Current trading mode: {current_mode.upper()}\n\n"
                f"Switching modes will restart the bot and close any open positions.\n"
                f"Do you want to switch to {target_mode.upper()} mode?",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
        except Exception as e:
            logger.error(f"Error showing mode toggle dialog: {e}")
            await update.callback_query.edit_message_text(
                f"Error: {str(e)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="menu_settings")]
                ])
            )

    async def handle_set_trade_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle setting trade amount"""
        try:
            # Get current amount settings
            amount_type = self.config['trading'].get('amount_type', 'fixed')
            fixed_amount = self.config['trading'].get('fixed_amount', 100)
            percentage_amount = self.config['trading'].get('percentage_amount', 10)
            
            # Create keyboard for amount type selection
            keyboard = [
                [
                    InlineKeyboardButton("Fixed Amount", callback_data="amount_type_fixed"),
                    InlineKeyboardButton("Percentage", callback_data="amount_type_percentage")
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="menu_settings")]
            ]
            
            await update.callback_query.edit_message_text(
                f"Trade Amount Settings\n\n"
                f"Current Setting: {amount_type.capitalize()}\n"
                f"Fixed Amount: ${fixed_amount:.2f}\n"
                f"Percentage: {percentage_amount:.1f}%\n\n"
                f"Select amount type:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
        except Exception as e:
            logger.error(f"Error showing trade amount dialog: {e}")
            await update.callback_query.edit_message_text(
                f"Error: {str(e)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="menu_settings")]
                ])
            )

    async def handle_set_reserve(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle setting reserve balance"""
        try:
            # Get current reserve balance
            reserve_balance = self.config['trading']['reserve_balance']
            
            # Create keyboard with preset values
            keyboard = [
                [
                    InlineKeyboardButton("$100", callback_data="reserve_100"),
                    InlineKeyboardButton("$500", callback_data="reserve_500"),
                    InlineKeyboardButton("$1000", callback_data="reserve_1000")
                ],
                [
                    InlineKeyboardButton("Custom Amount", callback_data="reserve_custom")
                ],
                [InlineKeyboardButton("🔙 Back", callback_data="menu_settings")]
            ]
            
            await update.callback_query.edit_message_text(
                f"Reserve Balance Settings\n\n"
                f"Current Reserve: ${reserve_balance:.2f}\n\n"
                f"Select a preset or enter a custom amount:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
        except Exception as e:
            logger.error(f"Error showing reserve balance dialog: {e}")
            await update.callback_query.edit_message_text(
                f"Error: {str(e)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="menu_settings")]
                ])
            )

    async def handle_edit_thresholds(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle editing thresholds"""
        try:
            # Get current thresholds
            thresholds = self.config['trading']['thresholds']
            
            # Create keyboard for timeframe selection
            keyboard = [
                [
                    InlineKeyboardButton("Daily", callback_data="threshold_timeframe_daily"),
                    InlineKeyboardButton("Weekly", callback_data="threshold_timeframe_weekly")
                ],
                [InlineKeyboardButton("Monthly", callback_data="threshold_timeframe_monthly")],
                [InlineKeyboardButton("🔙 Back", callback_data="menu_settings")]
            ]
            
            # Format current thresholds for display
            thresholds_text = ""
            for timeframe, values in thresholds.items():
                thresholds_text += f"*{timeframe.capitalize()}*: {', '.join([f'{v}%' for v in values])}\n"
            
            await update.callback_query.edit_message_text(
                f"Threshold Settings\n\n"
                f"Current Thresholds:\n{thresholds_text}\n"
                f"Select timeframe to edit:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
            
        except Exception as e:
            logger.error(f"Error showing thresholds dialog: {e}")
            await update.callback_query.edit_message_text(
                f"Error: {str(e)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="menu_settings")]
                ])
            )

    async def handle_admin_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
        """Handle admin actions"""
        try:
            if action == "restart":
                # Show confirmation dialog
                keyboard = [
                    [InlineKeyboardButton("Confirm Restart", callback_data="confirm_restart")],
                    [InlineKeyboardButton("🔙 Cancel", callback_data="show_admin_menu")]
                ]
                
                await update.callback_query.edit_message_text(
                    "Are you sure you want to restart the bot?",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                
            elif action == "emergency_stop":
                # Show confirmation dialog
                keyboard = [
                    [InlineKeyboardButton("Confirm Emergency Stop", callback_data="confirm_emergency_stop")],
                    [InlineKeyboardButton("🔙 Cancel", callback_data="show_admin_menu")]
                ]
                
                await update.callback_query.edit_message_text(
                    "⚠️ EMERGENCY STOP ⚠️\n\n"
                    "This will:\n"
                    "• Cancel all pending orders\n"
                    "• Close all open positions\n"
                    "• Pause all trading\n\n"
                    "Are you sure?",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                
            elif action == "clear_history":
                # Show confirmation dialog
                keyboard = [
                    [InlineKeyboardButton("Confirm Clear History", callback_data="confirm_clear_history")],
                    [InlineKeyboardButton("🔙 Cancel", callback_data="show_admin_menu")]
                ]
                
                await update.callback_query.edit_message_text(
                    "This will clear trading history older than 30 days.\n\n"
                    "Are you sure?",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                
            elif action == "debug":
                # Get system info
                import platform
                import psutil
                
                # Memory usage
                process = psutil.Process(os.getpid())
                memory_use = process.memory_info().rss / 1024 / 1024  # MB
                
                # Format debug message
                debug_text = (
                    "*System Debug Information*\n\n"
                    f"Python: {platform.python_version()}\n"
                    f"Platform: {platform.platform()}\n"
                    f"Memory Usage: {memory_use:.1f} MB\n\n"
                    f"*Bot Status*\n"
                    f"Mode: {self.config['environment']['trading_mode'].upper()}\n"
                    f"Testnet: {self.config['environment']['testnet']}\n"
                    f"Paused: {self.is_paused}\n"
                    f"Pairs: {len(self.config['trading']['pairs'])}\n"
                )
                
                await update.callback_query.edit_message_text(
                    debug_text,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Back", callback_data="show_admin_menu")]
                    ]),
                    parse_mode='Markdown'
                )
                
            elif action == "logs":
                # Get recent log entries
                log_file = "logs/bot.log"
                if os.path.exists(log_file):
                    with open(log_file, 'r') as f:
                        # Get last 20 lines
                        lines = f.readlines()[-20:]
                        log_text = "".join(lines)
                else:
                    log_text = "Log file not found"
                
                await update.callback_query.edit_message_text(
                    f"Recent Logs:\n\n```\n{log_text}\n```",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Back", callback_data="show_admin_menu")]
                    ]),
                    parse_mode='Markdown'
                )
                
            else:
                await update.callback_query.edit_message_text(
                    f"Unknown admin action: {action}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Back", callback_data="show_admin_menu")]
                    ])
                )
                
        except Exception as e:
            logger.error(f"Error handling admin action {action}: {e}")
            await update.callback_query.edit_message_text(
                f"Error: {str(e)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="show_admin_menu")]
                ])
            )

    async def get_balance(self) -> Decimal:
        """Get balance from appropriate client based on mode"""
        try:
            if self.config['environment']['trading_mode'] == 'futures':
                return await self.binance_client.get_balance()
            else:
                return await self.binance_client.get_balance(self.config['trading']['base_currency'])
        except Exception as e:
            logger.error(f"Error getting balance: {e}")
            return Decimal('0')

    async def message_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle non-command messages"""
        if not self._is_authorized(update.effective_user.id):
            return
            
        message = update.message.text
        
        # Check if this is a response to a conversation handler
        if context.user_data.get('waiting_for_input'):
            input_type = context.user_data['waiting_for_input']
            
            if input_type == 'leverage_value':
                # Handle leverage value input
                symbol = context.user_data.get('selected_symbol')
                
                try:
                    leverage = int(message)
                    if leverage < 1 or leverage > 125:
                        await update.message.reply_text("Leverage must be between 1 and 125. Please try again.")
                        return
                        
                    # Set leverage
                    success = await self.binance_client.set_leverage(symbol, leverage)
                    
                    if success:
                        await update.message.reply_text(
                            f"✅ Leverage for {symbol} set to {leverage}x successfully!"
                        )
                    else:
                        await update.message.reply_text(
                            f"❌ Failed to set leverage for {symbol}"
                        )
                        
                except ValueError:
                    await update.message.reply_text("Please enter a valid number.")
                    return
                    
                # Clear conversation state
                context.user_data.clear()
                
            elif input_type == 'custom_reserve':
                # Handle custom reserve amount input
                try:
                    reserve = float(message)
                    if reserve < 0:
                        await update.message.reply_text("Reserve amount cannot be negative. Please try again.")
                        return
                    
                    # Update reserve balance
                    self.config['trading']['reserve_balance'] = reserve
                    self.binance_client.reserve_balance = reserve
                    
                    # Save to database
                    await self.mongo_client.update_setting('trading', 'reserve_balance', reserve)
                    
                    await update.message.reply_text(
                        f"✅ Reserve balance updated to ${reserve:,.2f}\n\n"
                        f"The bot will maintain at least this much balance in your account."
                    )
                    
                except ValueError:
                    await update.message.reply_text("Please enter a valid number.")
                    return
                    
                # Clear conversation state
                context.user_data.clear()
                
            else:
                # Unknown input type
                context.user_data.clear()
                await update.message.reply_text("I'm not sure what you're trying to do. Please use the menu.")
        else:
            # Show help message for unknown messages
            await update.message.reply_text(
                "Please use the commands menu.\n"
                "Type /help to see available commands."
            )

    def _create_trade_conversation(self) -> ConversationHandler:
        """Create conversation handler for manual trade entry"""
        return ConversationHandler(
            entry_points=[
                CommandHandler("trade", self.add_trade_start),
                CallbackQueryHandler(self.add_trade_start, pattern="^new_trade$")
            ],
            states={
                SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_symbol)],
                ORDER_TYPE: [CallbackQueryHandler(self.add_trade_order_type)],
                LEVERAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_leverage)],
                DIRECTION: [CallbackQueryHandler(self.add_trade_direction)],
                AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_amount)],
                FEES: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_fees)],
                7: [CallbackQueryHandler(self.add_trade_final)]
            },
            fallbacks=[
                CommandHandler("cancel", self.add_trade_cancel),
                CallbackQueryHandler(self.add_trade_cancel, pattern="^cancel$")
            ],
            name="manual_trade"
        )

    async def send_startup_message(self):
        """Send startup message to all allowed users"""
        if not self.app:
            logger.error("Cannot send startup message - bot not initialized")
            return
            
        try:
            # Fix the startup message to avoid Markdown parsing errors
            # Remove potential problematic characters and ensure all entities are properly closed
            clean_message = self.startup_message.replace("```", "").replace("`", "").strip()
            
            for user_id in self.allowed_users:
                try:
                    await self.app.bot.send_message(
                        chat_id=user_id,
                        text=clean_message,
                        parse_mode=None  # Disable Markdown parsing to avoid errors
                    )
                    logger.info(f"Sent startup message to user {user_id}")
                except Exception as e:
                    logger.error(f"Failed to send startup message to {user_id}: {e}")
                    
        except Exception as e:
            logger.error(f"Error sending startup messages: {e}")
