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
            [KeyboardButton("/balance"), KeyboardButton("/stats"), KeyboardButton("/profits")],
            [KeyboardButton("/power"), KeyboardButton("/add"), KeyboardButton("/thresholds")],  # Changed /trading to /power
            [KeyboardButton("/history"), KeyboardButton("/viz"), KeyboardButton("/menu")]
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
        self.app.add_handler(CommandHandler("balance", self.get_balance))
        self.app.add_handler(CommandHandler("stats", self.get_stats))
        self.app.add_handler(CommandHandler("history", self.get_order_history))
        self.app.add_handler(CommandHandler("profits", self.show_profits))
        
        # Add visualization command
        self.app.add_handler(CommandHandler("viz", self.show_viz_menu))
        self.app.add_handler(CallbackQueryHandler(self.handle_viz_selection, pattern="^(daily_volume|profit_distribution|order_types|hourly_activity)$"))
        
        await self.app.initialize()
        await self.app.start()

    async def start(self):
        """Start the bot and begin polling"""
        self.running = True
        
        # Send startup message without ASCII art to avoid encoding issues
        for user_id in self.allowed_users:
            try:
                await self.app.bot.send_message(
                    chat_id=user_id, 
                    text=self.startup_message,
                    reply_markup=self.markup
                )
            except Exception as e:
                logger.error(f"Failed to send startup message to {user_id}: {e}")
        
        # Start polling in the background
        while self.running:
            try:
                updates = await self.app.bot.get_updates(
                    offset=self._update_id,
                    timeout=30
                )
                
                for update in updates:
                    if update.update_id >= self._update_id:
                        self._update_id = update.update_id + 1
                        await self.app.process_update(update)
                        
            except Exception as e:
                logger.error(f"Polling error: {e}")
                await asyncio.sleep(1)
                
            await asyncio.sleep(0.1)

    async def stop(self):
        """Stop the Telegram bot"""
        self.running = False
        if self.app:
            await self.app.stop()

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle the /start command"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        welcome_message = """
🦖 Trade-a-saurus Rex is ready!

Available commands:

Trading Controls:
/power - Toggle trading on/off  # Updated command name here

Trading Information:
/balance - Check current balance
/stats - View trading statistics
/profits - View portfolio profits
/history - View recent order history
/thresholds - Show threshold status

Trading Actions:
/add - Add a manual trade

Menu:
/menu - Show all commands
"""
        await update.message.reply_text(
            welcome_message,
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

    async def get_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Get current balance"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        try:
            account = await self.binance_client.client.get_account()
            balances = [
                f"{asset['asset']}: {asset['free']}"
                for asset in account['balances']
                if float(asset['free']) > 0
            ]
            message = "💰 Current Balance:\n" + "\n".join(balances)
            await update.message.reply_text(message)
        except Exception as e:
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
        """Send order notification to all allowed users"""
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
        
        # Fix the format specifier
        message = (
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
            message += f"\nFees: ${float(order.fees):.4f} {order.fee_asset}"
            
        if status == OrderStatus.CANCELLED and order.cancelled_at:
            duration = order.cancelled_at - order.created_at
            message += f"\nDuration: {duration.total_seconds() / 3600:.2f} hours"

        for user_id in self.allowed_users:
            try:
                await self.app.bot.send_message(chat_id=user_id, text=message)
            except Exception as e:
                logger.error(f"Failed to send notification to {user_id}: {e}")

    async def send_balance_update(self, symbol: str, change: Decimal):
        """Send balance change notification"""
        message = (
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
                        await self.app.bot.send_message(
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
                    await self.app.bot.send_photo(
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
                        await self.app.bot.send_message(
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
        
        # Send chart with full information
        try:
            ref_price = self.binance_client.reference_prices.get(
                order.symbol, {}
            ).get(order.timeframe)
            
            if ref_price is not None:
                ref_price = Decimal(str(ref_price))
            
            chart_data = await self.binance_client.generate_trade_chart(order)
            if not chart_data:
                logger.error("Failed to generate chart data")
                return
                
            # Create detailed caption with fixed format specifier
            caption = (
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
            
            # Send single message with chart and all info
            for user_id in self.allowed_users:
                try:
                    await self.app.bot.send_photo(
                        chat_id=user_id,
                        photo=chart_data,
                        caption=caption
                    )
                except Exception as e:
                    logger.error(f"Failed to send roar to {user_id}: {e}")
                    
            # Cleanup old roar notifications periodically
            if len(self.sent_roars) > 1000:
                self.sent_roars.clear()
                
        except Exception as e:
            logger.error(f"Failed to send roar: {e}")

    async def show_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show all available commands with descriptions"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        menu_text = """
🦖 Trade-a-saurus Rex Commands:

Trading Controls:
/start - Start the bot and show welcome message
/power - Toggle trading on/off  # Updated command name here

Trading Information:
/balance - Check current balance
/stats - View trading statistics
/history - View recent order history
/thresholds - Show threshold status and resets
/viz - Show data visualizations 📊

Trading Actions:
/add - Add a manual trade (interactive)

Menu:
/menu - Show this command list
"""
        await update.message.reply_text(menu_text)

    async def show_thresholds(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show detailed threshold information"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        try:
            now = datetime.utcnow()
            message_parts = []
            
            for timeframe in TimeFrame:
                # Get next reset time
                if timeframe == TimeFrame.DAILY:
                    next_reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                elif timeframe == TimeFrame.WEEKLY:
                    days_until_monday = (7 - now.weekday()) % 7
                    next_reset = (now + timedelta(days=days_until_monday)).replace(hour=0, minute=0, second=0, microsecond=0)
                else:  # MONTHLY
                    if now.month == 12:
                        next_reset = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
                    else:
                        next_reset = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
                
                time_until_reset = next_reset - now
                hours, remainder = divmod(time_until_reset.total_seconds(), 3600)
                minutes, _ = divmod(remainder, 60)
                
                # Format timeframe header
                timeframe_msg = [f"\n🕒 {timeframe.value.title()}"]
                timeframe_msg.append(f"Reset in: {int(hours)}h {int(minutes)}m")
                
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
        """Complete trade creation with fees"""
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
            
            # Send confirmation
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
                f"Fees: ${float(order.fees):.2f}\n"
                f"Total Value: ${float(order.price * order.quantity):.2f}",  # Fixed double colon here
                reply_markup=self.markup  # Restore original keyboard
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
                    f'Total Tax: ${portfolio_stats["total_tax"]:.2f}',  # Changed double quotes to single quotes
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

    async def add_trade_fees(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle fees input and create the trade"""
        try:
            price = Decimal(update.message.text)
            if price <= 0:
                raise ValueError("Price must be positive")
                
            user_data = self.temp_trade_data[update.effective_user.id]
            user_data['price'] = price
            
            await update.message.reply_text(
                "What were the trading fees? (in USDT, e.g., 0.25)"
            )
            return FEES
            
        except ValueError as e:
            await update.message.reply_text(f"Please enter a valid price: {str(e)}")
            return PRICE

    async def add_trade_final(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Complete trade creation with fees"""
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
                status=OrderStatus.FILLED,
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
            
            # Save to database using manual trade method
            if await self.mongo_client.insert_manual_trade(order):
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
                    f"Fees: ${float(order.fees):.2f}\n"
                    f"Total Value: ${float(order.price * order.quantity)::.2f}",  # Fixed double colon here
                    reply_markup=self.markup  # Restore original keyboard
                )
            else:
                await update.message.reply_text("❌ Failed to save trade")
            
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

    async def send_timeframe_reset_notification(self, timeframe: TimeFrame, message: str):
        """Send notification when a timeframe resets"""
        emoji_map = {
            TimeFrame.DAILY: "📅",
            TimeFrame.WEEKLY: "📆",
            TimeFrame.MONTHLY: "📊"
        }
        
        formatted_message = (
            f"{emoji_map.get(timeframe, '🔄')} Timeframe Reset\n\n"
            f"{message}\n\n"
            f"All {timeframe.value} thresholds have been reset."
        )
        
        for user_id in self.allowed_users:
            try:
                await self.app.bot.send_message(
                    chat_id=user_id,
                    text=formatted_message,
                    reply_markup=self.markup
                )
            except Exception as e:
                logger.error(f"Failed to send reset notification to {user_id}: {e}")

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

    # ...rest of existing code...

