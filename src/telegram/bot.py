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
from typing import List, Optional
from ..types.models import Order, OrderStatus, TimeFrame
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
SYMBOL, AMOUNT, TIMEFRAME, THRESHOLD = range(4)

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
            [KeyboardButton("/pause"), KeyboardButton("/resume"), KeyboardButton("/add")],
            [KeyboardButton("/history"), KeyboardButton("/thresholds"), KeyboardButton("/menu")]
        ]
        self.markup = ReplyKeyboardMarkup(self.keyboard, resize_keyboard=True)
        self.startup_message = f"""
{DINO_ASCII}

🦖 Trade-a-saurus Rex Bot

Your friendly neighborhood trading dinosaur is online!
Use /menu to see available commands.

Status: Ready to ROAR! 🦖
"""

    async def initialize(self):
        """Initialize the Telegram bot"""
        self.app = Application.builder().token(self.token).build()
        
        # Add new command handlers
        add_trade_handler = ConversationHandler(
            entry_points=[CommandHandler("add", self.add_trade_start)],
            states={
                SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_symbol)],
                AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_amount)],
                TIMEFRAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_timeframe)],
                THRESHOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_threshold)],
            },
            fallbacks=[CommandHandler("cancel", self.add_trade_cancel)],
        )
        
        self.app.add_handler(add_trade_handler)
        self.app.add_handler(CommandHandler("thresholds", self.show_thresholds))
        self.app.add_handler(CommandHandler("menu", self.show_menu))
        
        # Register command handlers
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("pause", self.pause_trading))
        self.app.add_handler(CommandHandler("resume", self.resume_trading))
        self.app.add_handler(CommandHandler("balance", self.get_balance))
        self.app.add_handler(CommandHandler("stats", self.get_stats))
        self.app.add_handler(CommandHandler("history", self.get_order_history))
        self.app.add_handler(CommandHandler("profits", self.show_profits))
        
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
/pause - Pause all trading operations
/resume - Resume trading operations

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

    async def pause_trading(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Pause trading operations"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        self.is_paused = True
        await update.message.reply_text("⏸ Trading paused")

    async def resume_trading(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Resume trading operations"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        self.is_paused = False
        await update.message.reply_text("▶️ Trading resumed")

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

        status = status or order.status
        emoji = {
            OrderStatus.PENDING: "🔵",
            OrderStatus.FILLED: "✅",
            OrderStatus.CANCELLED: "⚠️"
        }
        
        # Calculate total value in USDT
        total_value = order.price * order.quantity
        
        message = (
            f"{emoji[status]} Order Update\n"
            f"Order ID: {order.order_id}\n"
            f"Symbol: {order.symbol}\n"
            f"Status: {status.value.upper()}\n"
            f"Amount: {float(order.quantity):.8f} {order.symbol.replace('USDT', '')}\n"
            f"Price: ${float(order.price):.2f}\n"
            f"Total: ${float(total_value):.2f} USDT\n"
            f"Threshold: {order.threshold}%\n"
            f"Timeframe: {order.timeframe.value}"
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

    async def send_roar(self, order: Order):
        """Send a dinosaur roar notification with trade summary"""
        message = (
            f"🦖 ROARRR! Trade Complete! 💥\n"
            f"Order {order.order_id} filled!\n"
            f"Symbol: {order.symbol}\n"
            f"Amount: {float(order.quantity):.8f}\n"
            f"Price: ${float(order.price):.2f}\n"
            f"Check /profits to see your updated portfolio."
        )
        
        for user_id in self.allowed_users:
            try:
                await self.app.bot.send_message(chat_id=user_id, text=message)
            except Exception as e:
                logger.error(f"Failed to send roar to {user_id}: {e}")

    async def show_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show all available commands with descriptions"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        menu_text = """
🦖 Trade-a-saurus Rex Commands:

Trading Controls:
/start - Start the bot and show welcome message
/pause - Pause all trading operations
/resume - Resume trading operations

Trading Information:
/balance - Check current balance
/stats - View trading statistics
/history - View recent order history
/thresholds - Show threshold status and resets

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
                
                # Get triggered and available thresholds
                threshold_info = {}
                for symbol in self.config['trading']['pairs']:
                    triggered = self.binance_client.triggered_thresholds.get(symbol, {}).get(timeframe, [])
                    available = [t for t in self.config['trading']['thresholds'][timeframe.value] 
                               if t not in triggered]
                    threshold_info[symbol] = {
                        'triggered': triggered,
                        'available': available
                    }
                
                # Format timeframe message
                timeframe_msg = f"\n🕒 {timeframe.value.title()}\n"
                timeframe_msg += f"Reset in: {int(hours)}h {int(minutes)}m\n"
                
                for symbol, info in threshold_info.items():
                    timeframe_msg += f"\n{symbol}:\n"
                    timeframe_msg += f"✅ Triggered: {info['triggered']}\n"
                    timeframe_msg += f"⏳ Available: {info['available']}\n"
                
                message_parts.append(timeframe_msg)
            
            await update.message.reply_text("📊 Threshold Status:\n" + "\n".join(message_parts))
            
        except Exception as e:
            await update.message.reply_text(f"❌ Error getting thresholds: {str(e)}")

    async def add_trade_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start the manual trade addition process"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return ConversationHandler.END
            
        self.temp_trade_data[update.effective_user.id] = {}
        
        pairs = self.config['trading']['pairs']
        await update.message.reply_text(
            f"Enter the trading pair symbol:\n"
            f"Available pairs: {', '.join(pairs)}"
        )
        return SYMBOL

    async def add_trade_symbol(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle symbol input"""
        symbol = update.message.text.upper()
        if symbol not in self.config['trading']['pairs']:
            await update.message.reply_text(f"Invalid symbol. Please choose from: {', '.join(self.config['trading']['pairs'])}")
            return SYMBOL
            
        self.temp_trade_data[update.effective_user.id]['symbol'] = symbol
        await update.message.reply_text(f"Enter the amount in {self.config['trading']['base_currency']}:")
        return AMOUNT

    async def add_trade_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle amount input for manual trade"""
        try:
            amount = float(update.message.text)
            if amount <= 0:
                raise ValueError("Amount must be positive")
                
            user_data = self.temp_trade_data[update.effective_user.id]
            symbol = user_data['symbol']
            
            # Get current price for amount calculation
            ticker = await self.binance_client.client.get_symbol_ticker(symbol=symbol)
            price = Decimal(ticker['price'])
            quantity = Decimal(str(amount)) / price
            
            # Calculate fees
            fees, fee_asset = await self.binance_client.calculate_fees(symbol, price, quantity)
            
            # Store values for order placement
            user_data.update({
                'amount': amount,
                'price': price,
                'quantity': quantity,
                'fees': fees,
                'fee_asset': fee_asset
            })
            
            # Show order preview
            await update.message.reply_text(
                f"Order Preview:\n"
                f"Symbol: {symbol}\n"
                f"Amount: ${amount:,.2f} USDT\n"
                f"Price: ${float(price):,.2f}\n"
                f"Quantity: {float(quantity):,.8f}\n"
                f"Fees: ${float(fees):,.2f} {fee_asset}\n"
                f"\nEnter timeframe:"
            )
            return TIMEFRAME
            
        except ValueError as e:
            await update.message.reply_text(f"Error: {str(e)}")
            return AMOUNT

    async def add_trade_timeframe(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle timeframe input"""
        timeframe = update.message.text.lower()
        try:
            tf = TimeFrame(timeframe)
            self.temp_trade_data[update.effective_user.id]['timeframe'] = tf
            
            thresholds = self.config['trading']['thresholds'][timeframe]
            await update.message.reply_text(
                f"Enter the threshold percentage:\n"
                f"Available thresholds: {thresholds}"
            )
            return THRESHOLD
        except ValueError:
            await update.message.reply_text(f"Invalid timeframe. Choose from: {[tf.value for tf in TimeFrame]}")
            return TIMEFRAME

    async def add_trade_threshold(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle threshold input and create the trade"""
        try:
            threshold = float(update.message.text)
            user_data = self.temp_trade_data[update.effective_user.id]
            
            # Create and place the order with is_manual=True
            order = await self.binance_client.place_limit_buy_order(
                symbol=user_data['symbol'],
                amount=user_data['amount'],
                threshold=threshold,
                timeframe=user_data['timeframe'],
                is_manual=True  # Add this parameter
            )
            
            # Save to database
            await self.mongo_client.insert_order(order)
            
            # Send confirmation
            await update.message.reply_text(
                f"✅ Manual trade created:\n"
                f"Symbol: {order.symbol}\n"
                f"Amount: {order.quantity}\n"
                f"Timeframe: {order.timeframe.value}\n"
                f"Threshold: {order.threshold}%"
            )
            
            # Cleanup
            del self.temp_trade_data[update.effective_user.id]
            return ConversationHandler.END
            
        except ValueError:
            await update.message.reply_text("Please enter a valid threshold percentage")
            return THRESHOLD
        except Exception as e:
            await update.message.reply_text(f"❌ Error creating trade: {str(e)}")
            return ConversationHandler.END

    async def add_trade_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel the trade addition process"""
        if update.effective_user.id in self.temp_trade_data:
            del self.temp_trade_data[update.effective_user.id]
        await update.message.reply_text("Trade creation cancelled")
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
                    f"Total Tax: ${portfolio_stats['total_tax']:.2f}",
                    f"Net P/L: ${net_profit:.2f}"
                ])

            response.extend(summary)
            
            # Send response
            await update.message.reply_text("\n".join(response))
            
        except Exception as e:
            logger.error(f"Error calculating profits: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Error calculating profits: {str(e)}")
