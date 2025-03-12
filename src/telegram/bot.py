import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, BotCommand, ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes, 
    ConversationHandler, CallbackQueryHandler, MessageHandler,
    filters
)
from datetime import datetime, timedelta
import logging
from decimal import Decimal, InvalidOperation  # Add InvalidOperation for exception handling
from typing import List, Optional, Dict  # Add Dict import
from ..types.models import Order, OrderStatus, TimeFrame, OrderType, TradeDirection  # Update imports
from ..trading.binance_client import BinanceClient
from ..database.mongo_client import MongoClient
import io  # Add for handling bytes from chart
from ..trading.tpsl_manager import TPSLManager
import json
import os

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
        """Initialize the Telegram bot with token and allowed users"""
        self.token = token
        self.allowed_users = allowed_users
        self.binance_client = binance_client
        self.mongo_client = mongo_client
        self.config = config
        self.bot = Bot(token=token)
        self.dp = Dispatcher(self.bot)
        self.markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        self.markup.add(types.KeyboardButton('/menu'))
        self.is_paused = False
        self.chart_generator = ChartGenerator()
        self.user_states = {}  # Store user conversation states
        self.current_mode = "spot"  # Default to spot mode
        
        # Initialize user data storage
        self.user_data = {}
        
        # Create buttons for main menu
        self.markup.add(
            types.KeyboardButton('/balance'),
            types.KeyboardButton('/stats'),
            types.KeyboardButton('/toggle')
        )
        self.markup.add(
            types.KeyboardButton('/trade'),
            types.KeyboardButton('/futures'),
            types.KeyboardButton('/viz')
        )
        
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
        """Initialize the bot and register handlers"""
        try:
            # Register command handlers
            self.dp.register_message_handler(self.cmd_start, commands=["start"])
            self.dp.register_message_handler(self.cmd_help, commands=["help"])
            self.dp.register_message_handler(self.cmd_status, commands=["status"])
            self.dp.register_message_handler(self.cmd_balance, commands=["balance"])
            self.dp.register_message_handler(self.cmd_orders, commands=["orders"])
            self.dp.register_message_handler(self.cmd_positions, commands=["positions"])
            self.dp.register_message_handler(self.cmd_futures, commands=["futures"])
            self.dp.register_message_handler(self.cmd_chart, commands=["chart"])
            self.dp.register_message_handler(self.cmd_stats, commands=["stats"])
            self.dp.register_message_handler(self.cmd_thresholds, commands=["thresholds"])
            self.dp.register_message_handler(self.cmd_reset, commands=["reset"])
            self.dp.register_message_handler(self.cmd_tpsl, commands=["tpsl"])
            self.dp.register_message_handler(self.cmd_updatetpsl, commands=["updatetpsl"])
            self.dp.register_message_handler(self.cmd_toggletpsl, commands=["toggletpsl"])
            
            # Register futures commands
            self.dp.register_message_handler(self.cmd_leverage, commands=["leverage"])
            self.dp.register_message_handler(self.cmd_margin_mode, commands=["marginmode"])
            self.dp.register_message_handler(self.cmd_order_amount, commands=["orderamount"])
            
            # Register mode switching command
            self.dp.register_message_handler(self.cmd_switch_mode, commands=["mode"])
            
            # Register callback query handlers
            self.dp.register_callback_query_handler(self.callback_handler)
            
            # Register message handler for all other messages
            self.dp.register_message_handler(self.handle_message)
            
            # Initialize trading mode based on config
            await self._initialize_trading_mode()
            
            logger.info("Telegram bot initialized")
            return True
        except Exception as e:
            logger.error(f"Error initializing Telegram bot: {e}")
            return False

    async def _initialize_trading_mode(self):
        """Initialize the trading mode based on config"""
        # Check if futures is enabled in the binance client
        if hasattr(self.binance_client, 'is_futures_enabled') and self.binance_client.is_futures_enabled:
            # Default to spot mode, but allow futures mode
            self.current_mode = "spot"
            logger.info("Bot initialized with Spot mode as default, Futures mode is available")
        else:
            # Only spot mode is available
            self.current_mode = "spot"
            logger.info("Bot initialized with Spot mode only, Futures mode is not available")
    
    def get_current_mode(self) -> str:
        """Get the current trading mode (spot or futures)"""
        return self.current_mode
    
    async def cmd_switch_mode(self, message: types.Message):
        """Handle /mode command - switch between spot and futures modes"""
        try:
            chat_id = message.chat.id
            user_id = message.from_user.id
            
            # Check if user is authorized
            if not self._is_authorized(user_id):
                await self.send_message(chat_id, "⛔ Unauthorized access")
                return
                
            # Check if futures trading is enabled
            if not hasattr(self.binance_client, 'is_futures_enabled') or not self.binance_client.is_futures_enabled:
                await self.send_message(chat_id, "Futures trading is not enabled. Only Spot mode is available.")
                return
                
            # Create keyboard with mode options
            keyboard = [
                [InlineKeyboardButton("SPOT", callback_data="mode_spot")],
                [InlineKeyboardButton("FUTURES", callback_data="mode_futures")]
            ]
                
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await self.send_message(
                chat_id,
                f"🔄 *Switch Trading Mode*\n\nCurrent mode: *{self.current_mode.upper()}*\n\nSelect trading mode:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
            
        except Exception as e:
            logger.error(f"Error handling mode switch command: {e}")
            await self.send_message(message.chat.id, f"Error: {str(e)}")

    async def start(self):
        """Start the bot"""
        try:
            # Initialize bot
            await self.initialize()
            
            # Create application
            self.application = Application.builder().token(self.token).build()
            
            # Add handlers
            self.add_handlers()
            
            # Start the bot
            await self.application.initialize()
            await self.application.start()
            await self.application.updater.start_polling()
            
            # Set running flag
            self.running = True
            
            # Start position monitoring task
            self.position_monitor_task = asyncio.create_task(self.monitor_positions())
            
            logger.info("Bot started successfully")
            
        except Exception as e:
            logger.error(f"Error starting bot: {e}")
            raise

    async def stop(self):
        """Stop the bot"""
        try:
            # Set running flag to False to stop monitoring
            self.running = False
            
            # Cancel position monitoring task if it exists
            if hasattr(self, 'position_monitor_task'):
                self.position_monitor_task.cancel()
                try:
                    await self.position_monitor_task
                except asyncio.CancelledError:
                    pass
            
            # Stop the application
            if self.application:
                await self.application.stop()
                await self.application.shutdown()
                
            logger.info("Bot stopped successfully")
            
        except Exception as e:
            logger.error(f"Error stopping bot: {e}")
            raise

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
        """Get order history"""
        try:
            chat_id = update.effective_chat.id
            user_id = update.effective_user.id
            
            # Check if user is authorized
            if not self._is_authorized(user_id):
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="⛔ Unauthorized access"
                )
            return
            
            # Get order history based on current mode
            if self.current_mode == "futures":
                # Get futures order history
                orders = await self.mongo_client.get_futures_orders(days=7)
                
                if not orders:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="No futures orders found in the last 7 days."
                    )
                    return
                    
                # Format futures order history
                message = "📜 *Recent Futures Orders*\n\n"
                
                for order in orders:
                    # Extract order details
                    symbol = order.get('symbol', 'Unknown')
                    direction = order.get('direction', 'LONG')
                    leverage = order.get('leverage', 1)
                    status = order.get('status', 'Unknown')
                    price = float(order.get('price', 0))
                    quantity = float(order.get('quantity', 0))
                    created_at = order.get('created_at', datetime.now()).strftime('%Y-%m-%d %H:%M')
                    
                    # Calculate value
                    value = price * quantity
                    
                    # Format status with emoji
                    status_emoji = "✅" if status == "filled" else "❌" if status == "cancelled" else "⏳"
                    
                    # Format direction with emoji
                    direction_emoji = "📈" if direction == "LONG" else "📉"
                    
                    # Add order to message
                    message += f"{status_emoji} {direction_emoji} *{symbol}* ({leverage}x)\n"
                    message += f"   Price: ${price:.2f} | Qty: {quantity:.8f}\n"
                    message += f"   Value: ${value:.2f} | {created_at}\n\n"
                
            else:
                # Get spot order history
                orders = await self.mongo_client.get_buy_orders(days=7)
                
                if not orders:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="No spot orders found in the last 7 days."
                    )
                    return
                    
                # Format spot order history
                message = "📜 *Recent Spot Orders*\n\n"
                
                for order in orders:
                    # Extract order details
                    symbol = order.get('symbol', 'Unknown')
                    status = order.get('status', 'Unknown')
                    price = float(order.get('price', 0))
                    quantity = float(order.get('quantity', 0))
                    created_at = order.get('created_at', datetime.now()).strftime('%Y-%m-%d %H:%M')
                    
                    # Calculate value
                    value = price * quantity
                    
                    # Format status with emoji
                    status_emoji = "✅" if status == "filled" else "❌" if status == "cancelled" else "⏳"
                    
                    # Add order to message
                    message += f"{status_emoji} *{symbol}*\n"
                    message += f"   Price: ${price:.2f} | Qty: {quantity:.8f}\n"
                    message += f"   Value: ${value:.2f} | {created_at}\n\n"
            
            # Send message
            await context.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            logger.error(f"Error getting order history: {e}")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Error getting order history: {str(e)}"
            )

    async def send_order_notification(self, order: Order, status: Optional[OrderStatus] = None):
        """Send notification about order status change"""
        if not self.allowed_users:
            return

        # Use provided status or order status
        status = status or order.status
        
        # Create message based on status
        if status == OrderStatus.PENDING:
            emoji = "🔄"
            action = "placed"
        elif status == OrderStatus.FILLED:
            emoji = "✅"
            action = "filled"
        elif status == OrderStatus.CANCELLED:
            emoji = "❌"
            action = "cancelled"
        else:
            emoji = "❓"
            action = "updated"
            
        # Format order details
        order_type = "SPOT" if order.order_type == OrderType.SPOT else "FUTURES"
        leverage_text = f" ({order.leverage}x)" if order.leverage else ""
        direction_text = f" {order.direction.value.upper()}" if order.direction else ""
        
        # Format price with appropriate precision
        price_str = f"${float(order.price):.2f}"
        
        # Format quantity with appropriate precision
        if float(order.quantity) < 0.001:
            quantity_str = f"{float(order.quantity):.8f}"
        elif float(order.quantity) < 0.1:
            quantity_str = f"{float(order.quantity):.6f}"
        else:
            quantity_str = f"{float(order.quantity):.4f}"
            
        # Calculate total value
        total_value = float(order.price * order.quantity)
        
        # Add TP/SL info if available
        tp_sl_text = ""
        if hasattr(order, 'tp_price') and hasattr(order, 'sl_price') and order.tp_price and order.sl_price:
            tp_sl_text = (
                f"\n\nTP: ${float(order.tp_price):.2f}\n"
                f"SL: ${float(order.sl_price):.2f}"
            )
            
        # Create message
        message = (
            f"{emoji} Order {action.upper()}\n\n"
            f"Symbol: {order.symbol}\n"
            f"Type: {order_type}{leverage_text}{direction_text}\n"
            f"Price: {price_str}\n"
            f"Quantity: {quantity_str}\n"
            f"Total: ${total_value:.2f}"
            f"{tp_sl_text}"
        )
        
        # Add threshold info if available
        if order.threshold is not None and order.timeframe is not None:
            message += f"\n\nTriggered by {order.threshold}% {order.timeframe.value} threshold"
            
        # Add reference price if available
        if order.reference_price is not None:
            price_change = ((float(order.price) - order.reference_price) / order.reference_price) * 100
            message += f"\nReference: ${order.reference_price:.2f} ({price_change:+.2f}%)"
            
        # Add timestamp
        if status == OrderStatus.PENDING:
            timestamp = order.created_at
        elif status == OrderStatus.FILLED:
            timestamp = order.filled_at or datetime.now()
        elif status == OrderStatus.CANCELLED:
            timestamp = order.cancelled_at or datetime.now()
        else:
            timestamp = datetime.now()
            
        message += f"\n\nTime: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
        
        # Send to all allowed users
        for user_id in self.allowed_users:
            try:
                await self.application.bot.send_message(
                    chat_id=user_id,
                    text=message
                )
            except Exception as e:
                logger.error(f"Failed to send order notification to {user_id}: {e}")

    async def send_balance_update(self, symbol: str, change: Decimal):
        """Send balance change notification"""
        message = (
            f"💰 Balance Update\n"
            f"Symbol: {symbol}\n"
            f"Change: {change:+.8f} USDT"
        )
        
        for user_id in self.allowed_users:
            try:
                await self.application.bot.send_message(chat_id=user_id, text=message)
            except Exception as e:
                logger.error(f"Failed to send balance update to {user_id}: {e}")

    async def send_trade_chart(self, chat_id: int, order: Order, candles: List[Dict], 
                             reference_price: Optional[Decimal] = None,
                             funding_rate: Optional[float] = None) -> bool:
        """Send a chart for a trade with entry point and reference price"""
        try:
            # Generate chart based on order type
            if order.order_type == OrderType.FUTURES:
                # Get position info if available
                position_info = None
                if self.binance_client:
                    try:
                        position = await self.binance_client.get_position_info(order.symbol)
                        if position:
                            position_info = {
                                'entry_price': position.get('entry_price', float(order.price)),
                                'pnl': position.get('unrealized_pnl', 0),
                                'pnl_percentage': position.get('roe', 0),
                                'margin_ratio': position.get('margin_ratio', 0)
                            }
                    except Exception as e:
                        logger.error(f"Error getting position info: {e}")
                
                # Get funding rate if not provided
                if funding_rate is None and self.binance_client:
                    try:
                        funding_info = await self.binance_client.get_funding_rate(order.symbol)
                        if funding_info:
                            funding_rate = funding_info.get('funding_rate', 0)
                except Exception as e:
                        logger.error(f"Error getting funding rate: {e}")
                
                # Generate futures-specific chart
                chart_bytes = await self.chart_generator.generate_futures_chart(
                    candles, order, funding_rate, position_info
                )
            else:
                # Generate regular chart for spot orders
                chart_bytes = await self.chart_generator.generate_trade_chart(
                    candles, order, reference_price
                )
            
            if not chart_bytes:
                await self.send_message(chat_id, "Failed to generate chart")
                return False
                
            # Send chart as photo
            await self.bot.send_photo(
                chat_id=chat_id,
                photo=io.BytesIO(chart_bytes),
                caption=f"Trade chart for {order.symbol}"
            )
            
            return True
                    
        except Exception as e:
            logger.error(f"Error sending trade chart: {e}")
            await self.send_message(chat_id, f"Error sending chart: {str(e)}")
            return False

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
            
            # Create detailed caption - this will be used for both chart and fallback text message
            caption = (
                f"🦖 ROARRR! Trade Complete! 💥\n\n"
                f"Order ID: {order.order_id}\n"
                f"Symbol: {order.symbol}\n"
                f"Amount: {float(order.quantity):.8f} {order.symbol.replace('USDT', '')}\n"
                f"Price: ${float(order.price):.2f}\n"
                f"Total: ${float(order.price * order.quantity):.2f} USDT\n"  # Fixed double colon "::" -> ":"
                f"Fees: ${float(order.fees):.4f} {order.fee_asset}\n"
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
                    if chart_data:
                        # Send with chart if available
                        await self.application.bot.send_photo(
                            chat_id=user_id,
                            photo=chart_data,
                            caption=caption
                        )
                        logger.info(f"Sent ROAR with chart to user {user_id}")
                    else:
                        # Send text-only message if chart generation failed
                        text_message = caption + "\n\n⚠️ (Chart generation failed - not enough historical data)"
                        await self.application.bot.send_message(
                            chat_id=user_id,
                            text=text_message
                        )
                        logger.info(f"Sent text-only ROAR to user {user_id} due to chart failure")
                except Exception as e:
                    logger.error(f"Failed to send ROAR to {user_id}: {e}")
                    # Last resort fallback - try to send minimal message
                    try:
                        minimal_msg = f"🦖 Trade Complete! {order.symbol} at ${float(order.price):.2f}"
                        await self.application.bot.send_message(
                            chat_id=user_id,
                            text=minimal_msg
                        )
                    except Exception:
                        pass  # If even this fails, we've logged the error above
                    
            # Cleanup old roar notifications periodically
            if len(self.sent_roars) > 1000:
                self.sent_roars.clear()
                
        except Exception as e:
            logger.error(f"Failed to send roar: {e}")
            # Final fallback - try to send a minimal notification
            for user_id in self.allowed_users:
                try:
                    await self.application.bot.send_message(
                        chat_id=user_id,
                        text=f"🦖 ROAR! {order.symbol} trade completed. Check /profits for details."
                    )
                except Exception as e2:
                    logger.error(f"Even fallback roar failed for {user_id}: {e2}")

    async def show_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the main menu"""
        if not await self.is_user_authorized(update, context):
            return
            
        try:
            # Get current mode
            mode_text = "FUTURES" if self.current_mode == "futures" else "SPOT"
            
            # Create menu message
            message = f"🤖 *TRADE-A-SAURUS REX* 🦖\n\n"
            message += f"*Current Mode:* {mode_text}\n"
            
            # Add mode-specific information
            if self.current_mode == "futures":
                # Add futures-specific information
                leverage = self.config.get("futures", {}).get("leverage", 1)
                margin_mode = self.config.get("futures", {}).get("margin_mode", "isolated").upper()
                order_amount = self.config.get("futures", {}).get("order_amount", 0)
                
                message += f"*Leverage:* {leverage}x\n"
                message += f"*Margin Mode:* {margin_mode}\n"
                message += f"*Order Amount:* {order_amount} USDT\n"
            else:
                # Add spot-specific information
                order_amount = self.config.get("spot", {}).get("order_amount", 0)
                message += f"*Order Amount:* {order_amount} USDT\n"
            
            # Add TP/SL information
            tp_sl_status = "ENABLED" if self.tp_sl_enabled else "DISABLED"
            message += f"*TP/SL:* {tp_sl_status}\n"
            
            if self.tp_sl_enabled:
                message += f"*TP %:* {self.default_tp_percentage}%\n"
                message += f"*SL %:* {self.default_sl_percentage}%\n"
            
            # Add commands
            message += "\n*Commands:*\n"
            message += "/trade - Start a new trade\n"
            message += "/cancel - Cancel active orders\n"
            message += "/orders - View order history\n"
            
            if self.current_mode == "futures":
                message += "/positions - View open positions\n"
                
            message += "/balance - Check account balance\n"
            message += "/stats - View performance statistics\n"
            message += "/chart - Get price chart\n"
            message += "/mode - Toggle between Spot/Futures\n"
            message += "/tpsl - TP/SL settings\n"
            
            if self.current_mode == "futures":
                message += "/leverage - Set leverage\n"
                message += "/marginmode - Set margin mode\n"
                
            message += "/orderamount - Set default order amount\n"
            message += "/help - Show help\n"

            # Send menu
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=message,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Error showing menu: {e}")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"Error showing menu: {e}"
            )

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
        """Start the process of adding a manual trade"""
        try:
            chat_id = update.effective_chat.id
            user_id = update.effective_user.id
            
            # Check if user is authorized
            if not self._is_authorized(user_id):
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="⛔ Unauthorized access"
                )
            return ConversationHandler.END
            
            # Initialize user data for this conversation
            context.user_data['trade'] = {}
            
            # Set order type based on current mode
            if self.current_mode == "futures":
                context.user_data['trade']['order_type'] = "futures"
                
                # Create keyboard with order type options
                keyboard = [
                    [InlineKeyboardButton("LONG", callback_data="direction_long")],
                    [InlineKeyboardButton("SHORT", callback_data="direction_short")]
                ]
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="🔄 *New Futures Trade*\n\nSelect position direction:",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup
                )
                
                return "direction"
            else:
                # Default to spot trading
                context.user_data['trade']['order_type'] = "spot"
                
                # For spot trading, we don't need to ask for direction
                # Proceed directly to symbol selection
                symbols = self.config['trading']['pairs']
                
                keyboard = []
                row = []
                
                for i, symbol in enumerate(symbols):
                    row.append(InlineKeyboardButton(symbol, callback_data=f"symbol_{symbol}"))
                    
                    # Create rows of 3 buttons each
                    if (i + 1) % 3 == 0 or i == len(symbols) - 1:
                        keyboard.append(row)
                        row = []
                
                # Add cancel button
                keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="🔄 *New Spot Trade*\n\nSelect trading pair:",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup
                )
                
                return "symbol"
                
        except Exception as e:
            logger.error(f"Error starting trade conversation: {e}")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Error: {str(e)}"
            )
            return ConversationHandler.END

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
                
            # Send chart to user
            await self.application.bot.send_photo(
                chat_id=chat_id,
                photo=chart_bytes,
                caption="📊 Account Balance History (30 days)\n"
                        "💹 Green arrows indicate buy orders\n"
                        "🟢 Green line: Total Balance\n"
                        "🔵 Blue line: Invested Amount\n"
                        "🟣 Purple line: Profit (Balance - Invested)",
                reply_markup=self.markup
            )
            
        except Exception as e:
            logger.error(f"Error generating balance chart: {e}", exc_info=True)
            await self.app.bot.send_message(
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
        """Generate and send ROI comparison chart"""
        try:
            # Get historical performance data
            days = 90  # Look at the last 90 days for comparison
            allowed_symbols = set(self.config['trading']['pairs'])
            
            # Get portfolio performance data (returns a dict with dates and ROI percentages)
            portfolio_data = await self.mongo_client.get_portfolio_performance(days, allowed_symbols)
            
            # Check if portfolio data is sufficient, generate simulated data if needed
            if not portfolio_data or len(portfolio_data) < 5:  # Need at least 5 data points
                logger.warning(f"Insufficient portfolio data ({len(portfolio_data) if portfolio_data else 0} points), using simulated data")
                portfolio_data = await self._generate_simulated_portfolio_data(days)
                
                # Notify the user about simulation
                await self.app.bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ Not enough real portfolio data available. Generating simulated performance chart for demonstration.",
                    reply_markup=self.markup
                )
            
            # Get benchmark data (BTC performance)
            btc_performance = await self.binance_client.get_historical_benchmark("BTCUSDT", days)
            
            # Get S&P 500 performance if available (through binance client's API or mock data)
            sp500_performance = await self.binance_client.get_historical_benchmark("SP500", days)
            
            # Generate chart using chart generator
            chart_bytes = await self.binance_client.chart_generator.generate_roi_comparison_chart(
                portfolio_data, btc_performance, sp500_performance
            )
            
            if not chart_bytes:
                await self.app.bot.send_message(
                    chat_id=chat_id,
                    text="Failed to generate ROI comparison chart.",
                    reply_markup=self.markup
                )
                return
                
            # Send chart to user
            await self.app.bot.send_photo(
                chat_id=chat_id,
                photo=chart_bytes,
                caption="📊 ROI Comparison (90 days)\n"
                        "🟢 Green line: Portfolio Performance\n"
                        "🟠 Orange line: Bitcoin Performance\n"
                        "🔵 Blue line: S&P 500 Performance\n\n"
                        "Values show percentage return relative to initial investment",
                reply_markup=self.markup
            )
            
        except Exception as e:
            logger.error(f"Error generating ROI comparison chart: {e}", exc_info=True)
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Error generating ROI comparison chart: {str(e)}",
                reply_markup=self.markup
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
        """Send notification when a timeframe resets with price information"""
        emoji_map = {
            TimeFrame.DAILY: "📅",
            TimeFrame.WEEKLY: "📆",
            TimeFrame.MONTHLY: "📊"
        }
        
        timeframe = reset_data["timeframe"]
        message_parts = [
            f"{emoji_map.get(timeframe, '🔄')} {timeframe.value.title()} Reset",
            f"\nOpening Prices:"
        ]
        
        # Add price information for each symbol
        for price_data in reset_data["prices"]:
            symbol = price_data["symbol"]
            current = price_data["current_price"]
            reference = price_data["reference_price"]
            change = price_data["price_change"]
            
            message_parts.append(
                f"\n{symbol}:"
                f"\nOpening: ${reference:,.2f}"
                f"\nCurrent: ${current:,.2f}"
                f"\nChange: {change:+.2f}%"
            )
        
        message_parts.append(f"\n\nAll {timeframe.value} thresholds have been reset.")
        
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

    async def send_threshold_notification(self, symbol: str, timeframe: TimeFrame, 
                                       threshold: float, current_price: float,
                                       reference_price: float, price_change: float):
        """Send notification when a threshold is triggered"""
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
            f"Pending Orders: ${float(pending_value)::.2f}\n"
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
            f"Current Balance: ${float(current_balance):.2f}\n"  # Fixed double colon here
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
                    await self.app.bot.send_message(
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
            # Reset daily thresholds
            await self.binance_client.reset_timeframe_thresholds('daily')
            
            
            # Reset weekly thresholds
            await self.binance_client.reset_timeframe_thresholds('weekly')
            
            # Reset monthly thresholds
            await self.binance_client.reset_timeframe_thresholds('monthly')
            
            await update.message.reply_text(
                "✅ All thresholds have been reset across all timeframes.",
                reply_markup=self.markup
            )
        except Exception as e:
            logger.error(f"Failed to reset all thresholds: {e}")
            await update.message.reply_text(
                f"❌ Error resetting thresholds: {str(e)}",
                reply_markup=self.markup
            )

    async def send_message(self, chat_id, text, **kwargs):
        """Helper method to send messages to users"""
        try:
            if self.app and self.app.bot:
                await self.app.bot.send_message(chat_id=chat_id, text=text, **kwargs)
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
                    await self.app.bot.send_message(
                        chat_id=user_id, 
                        text=message, 
                        parse_mode="Markdown"
                    )
                    logger.info(f"Sent threshold restoration message to user {user_id}")
                except Exception as e:
                    logger.error(f"Failed to send threshold restoration message to {user_id}: {e}")
                    
        except Exception as e:
            logger.error(f"Failed to send startup threshold message: {e}")

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
                await self.app.bot.send_message(
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
                await self.app.bot.send_message(
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
                await self.app.bot.send_message(
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
                await self.app.bot.send_message(
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
                await self.app.bot.send_message(
                    chat_id=chat_id,
                    text="Failed to generate comparison chart.",
                    reply_markup=self.markup
                )
                return
                
            # Get current values for caption
            btc_current = list(btc_ytd_prices.values())[-1] if btc_ytd_prices else 0
            sp500_current = list(sp500_ytd.values())[-1] if sp500_ytd else 0
            
            # Send the chart
            await self.app.bot.send_photo(
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
            await self.app.bot.send_message(
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
                await self.app.bot.send_message(
                    chat_id=chat_id,
                    text="No portfolio data available for composition chart.",
                    reply_markup=self.markup
                )
                return
                
            # Get USDT balance
            usdt_balance = await self.binance_client.get_balance('USDT')
            
            # Get current prices for all positions to calculate current values
            portfolio_data = []
            total_value = float(usdt_balance)  # Start with USDT balance
            asset_values = {'USDT': float(usdt_balance)}
            
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
                    base_asset = symbol.replace('USDT', '')
                    asset_values[base_asset] = position_value
                    
                except Exception as e:
                    logger.error(f"Error getting price for {symbol}: {e}")
            
            # Generate the chart
            chart_bytes = await self._create_portfolio_composition_chart(asset_values, total_value)
            
            if not chart_bytes:
                await self.app.bot.send_message(
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
            await self.app.bot.send_photo(
                chat_id=chat_id,
                photo=chart_bytes,
                caption="\n".join(caption_lines),
                reply_markup=self.markup
            )
            
        except Exception as e:
            logger.error(f"Error generating portfolio composition chart: {e}", exc_info=True)
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Error generating portfolio composition chart: {str(e)}",
                reply_markup=self.markup
            )
            
    async def _create_portfolio_composition_chart(self, asset_values: dict, total_value: float) -> Optional[bytes]:
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
            
            # Generate colors - ensure USDT is a specific color if present
            colors = plt.cm.tab20.colors[:len(labels)]
            if 'USDT' in labels:
                usdt_index = labels.index('USDT')
                # Use a specific color for USDT - light green
                colors = list(colors)
                colors[usdt_index] = (0.2, 0.8, 0.2, 1.0)  # RGBA for green
            
            # Create figure
            plt.figure(figsize=(10, 8))
            
            # Create a slightly more visually appealing pie chart with shadow and explode effect
            explode = [0.05] * len(labels)  # Small explode effect for all pieces
            if 'USDT' in labels:
                usdt_index = labels.index('USDT')
                explode[usdt_index] = 0.1  # Larger explode for USDT
            
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
            
            plt.title("Portfolio Composition", fontsize=16, pad=20)
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

    async def toggle_tpsl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Toggle TP/SL on/off"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        # Toggle TP/SL
        self.binance_client.tp_sl_enabled = not self.binance_client.tp_sl_enabled
        
        # Initialize or clear TP/SL manager
        if self.binance_client.tp_sl_enabled:
            if not self.binance_client.tp_sl_manager:
                self.binance_client.tp_sl_manager = TPSLManager(self.binance_client, self.mongo_client)
            message = (
                "✅ TP/SL is now *ENABLED*\n\n"
                f"Take Profit: {self.binance_client.default_tp_percentage}%\n"
                f"Stop Loss: {self.binance_client.default_sl_percentage}%\n\n"
                "TP/SL orders will be placed automatically for new filled orders."
            )
        else:
            message = "❌ TP/SL is now *DISABLED*\n\nNo TP/SL orders will be placed for new orders."
        
        await update.message.reply_text(message, parse_mode='Markdown')

    async def show_tpsl_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show TP/SL management menu"""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("You are not authorized to use this bot.")
            return
            
        # Check if TP/SL is enabled
        tp_sl_enabled = self.binance_client.tp_sl_enabled
        tp_percentage = self.binance_client.default_tp_percentage
        sl_percentage = self.binance_client.default_sl_percentage
        
        if not tp_sl_enabled:
            await update.message.reply_text(
                "⚠️ TP/SL is currently *DISABLED*\n\n"
                "Use /toggletpsl to enable TP/SL first.",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Enable TP/SL", callback_data="tpsl_enable")]
                ])
            )
            return
            
        keyboard = [
            [InlineKeyboardButton("Update TP/SL Levels", callback_data="tpsl_update")],
            [InlineKeyboardButton("View TP/SL Settings", callback_data="tpsl_view")],
            [InlineKeyboardButton("Cancel TP/SL Orders", callback_data="tpsl_cancel")],
            [InlineKeyboardButton("Configure Default TP/SL", callback_data="tpsl_config")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"📊 *TP/SL Management*\n\n"
            f"Current Default Settings:\n"
            f"Take Profit: {tp_percentage}%\n"
            f"Stop Loss: {sl_percentage}%\n\n"
            f"Manage Take Profit and Stop Loss orders for your positions.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    async def handle_tpsl_enable(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle enabling TP/SL from callback"""
        query = update.callback_query
        await query.answer()
        
        # Enable TP/SL
        self.binance_client.tp_sl_enabled = True
        
        # Initialize TP/SL manager if needed
        if not self.binance_client.tp_sl_manager:
            self.binance_client.tp_sl_manager = TPSLManager(self.binance_client, self.mongo_client)
        
        # Show TP/SL menu
        await self.show_tpsl_menu(update, context)

    async def handle_tpsl_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle configuring default TP/SL percentages"""
        query = update.callback_query
        await query.answer()
        
        await query.edit_message_text(
            "*Configure Default TP/SL Percentages*\n\n"
            f"Current Settings:\n"
            f"Take Profit: {self.binance_client.default_tp_percentage}%\n"
            f"Stop Loss: {self.binance_client.default_sl_percentage}%\n\n"
            "Enter new Take Profit percentage (e.g., 5 for 5%):",
            parse_mode='Markdown'
        )
        
        # Set next state
        return "tpsl_config_tp"
    
    async def handle_tpsl_config_tp(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle input of new default TP percentage"""
        try:
            # Parse TP percentage
            tp_percentage = float(update.message.text.strip())
            
            # Validate percentage
            if tp_percentage <= 0:
                await update.message.reply_text(
                    "TP percentage must be greater than 0. Please try again:"
                )
                return "tpsl_config_tp"
                
            # Store in context
            context.user_data["tpsl_config_tp"] = tp_percentage
            
            # Ask for SL percentage
            await update.message.reply_text(
                f"Take Profit set to {tp_percentage}%.\n\n"
                "Enter new Stop Loss percentage (e.g., 3 for 3%):"
            )
            
            # Set next state
            return "tpsl_config_sl"
            
        except ValueError:
            await update.message.reply_text(
                "Invalid input. Please enter a number for TP percentage:"
            )
            return "tpsl_config_tp"
    
    async def handle_tpsl_config_sl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle input of new default SL percentage"""
        try:
            # Parse SL percentage
            sl_percentage = float(update.message.text.strip())
            
            # Validate percentage
            if sl_percentage <= 0:
                await update.message.reply_text(
                    "SL percentage must be greater than 0. Please try again:"
                )
                return "tpsl_config_sl"
                
            # Get TP percentage from context
            tp_percentage = context.user_data.get("tpsl_config_tp")
            
            # Update default percentages
            self.binance_client.default_tp_percentage = tp_percentage
            self.binance_client.default_sl_percentage = sl_percentage
            
            # Update config if available
            if self.config and 'trading' in self.config:
                self.config['trading']['tp_percentage'] = tp_percentage
                self.config['trading']['sl_percentage'] = sl_percentage
            
            # Confirm update
            await update.message.reply_text(
                "✅ Default TP/SL percentages updated:\n\n"
                f"Take Profit: {tp_percentage}%\n"
                f"Stop Loss: {sl_percentage}%\n\n"
                "These settings will be applied to all new orders.",
                reply_markup=self.markup
            )
            
            # Clear context data
            context.user_data.clear()
            
            return ConversationHandler.END
            
        except ValueError:
            await update.message.reply_text(
                "Invalid input. Please enter a number for SL percentage:"
            )
            return "tpsl_config_sl"

    def get_tpsl_conversation(self) -> ConversationHandler:
        """Create conversation handler for TP/SL management"""
        return ConversationHandler(
            entry_points=[
                CommandHandler("tpsl", self.show_tpsl_menu),
                CommandHandler("updatetpsl", self.handle_tpsl_config),
                CallbackQueryHandler(self.handle_tpsl_selection, pattern=r"^tpsl_select_"),
                CallbackQueryHandler(self.handle_tpsl_enable, pattern=r"^tpsl_enable$"),
                CallbackQueryHandler(self.handle_tpsl_config, pattern=r"^tpsl_config$")
            ],
            states={
                "tpsl_tp_percentage": [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_tpsl_tp_percentage)],
                "tpsl_sl_percentage": [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_tpsl_sl_percentage)],
                "tpsl_confirm": [CallbackQueryHandler(self.handle_tpsl_confirm, pattern=r"^tpsl_")],
                "tpsl_config_tp": [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_tpsl_config_tp)],
                "tpsl_config_sl": [MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_tpsl_config_sl)]
            },
            fallbacks=[
                CommandHandler("cancel", self.add_trade_cancel),
                CallbackQueryHandler(self.handle_tpsl_menu, pattern=r"^tpsl_menu$"),
                CallbackQueryHandler(self.handle_tpsl_view, pattern=r"^tpsl_view$"),
                CallbackQueryHandler(self.handle_tpsl_cancel_select, pattern=r"^tpsl_cancel$"),
                CallbackQueryHandler(self.handle_tpsl_cancel_confirm, pattern=r"^tpsl_cancel_")
            ],
            name="tpsl_conversation",
            persistent=False
        )

    async def toggle_lower_entries(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Toggle Lower Entry Price Protection on/off"""
        try:
            # Check if user is authorized
            if not self._is_authorized(update.effective_user.id):
                await update.message.reply_text("You are not authorized to use this bot.")
                return
                
            # Toggle the setting
            current_state = self.binance_client.only_lower_entries
            new_state = not current_state
            self.binance_client.only_lower_entries = new_state
            
            # Update config if available
            if self.config and 'trading' in self.config:
                self.config['trading']['only_lower_entries'] = new_state
            
            # Send confirmation message
            if new_state:
                await update.message.reply_text(
                    "✅ Lower Entry Price Protection is now ENABLED.\n\n"
                    "New futures positions will only be opened if the entry price is lower than the previous average price.",
                    reply_markup=self.markup
                )
            else:
                await update.message.reply_text(
                    "⚠️ Lower Entry Price Protection is now DISABLED.\n\n"
                    "New futures positions will be opened regardless of the entry price compared to previous average.",
                    reply_markup=self.markup
                )
                
            logger.info(f"Lower Entry Price Protection toggled to: {new_state}")
            
        except Exception as e:
            logger.error(f"Error toggling Lower Entry Price Protection: {e}")
            await update.message.reply_text(
                "❌ Error toggling Lower Entry Price Protection. Please try again.",
                reply_markup=self.markup
            )

    async def cmd_help(self, message: types.Message):
        """Handle /help command"""
        try:
            # Get current mode
            current_mode = self.current_mode.upper()
            
            # Common commands
            common_commands = (
                "🦖 *Trade-a-saurus Rex Help* 🦖\n\n"
                f"Current Mode: *{current_mode}*\n\n"
                "*Basic Commands:*\n"
                "/start - Start the bot\n"
                "/menu - Show main menu\n"
                "/mode - Switch between Spot/Futures modes\n"
                "/toggle - Toggle trading on/off\n"
                "/balance - Get current balance\n"
                "/stats - Get performance stats\n"
                "/history - Get order history\n"
            )
            
            # Mode-specific commands
            spot_commands = (
                "*Spot Trading Commands:*\n"
                "/thresholds - Show price thresholds\n"
                "/reset - Reset all thresholds\n"
            )
            
            futures_commands = (
                "*Futures Trading Commands:*\n"
                "/futures - Show open futures positions\n"
                "/positions - Show position details\n"
                "/leverage - Set default leverage (max 5x)\n"
                "/marginmode - Set default margin mode (isolated/cross)\n"
            )
            
            # Common settings commands
            settings_commands = (
                "*Trading Settings:*\n"
                "/orderamount - Set USDT amount per order (min $10)\n"
                "/tpsl - Manage Take Profit/Stop Loss settings\n"
                "/toggletpsl - Toggle TP/SL on/off\n"
                "/togglelower - Toggle Lower Entry Price Protection\n\n"
            )
            
            # Visualization commands
            viz_commands = (
                "*Visualization Commands:*\n"
                "/viz - Show visualizations\n"
                "/profits - Show profit analysis\n"
            )
            
            # Combine commands based on current mode
            if self.current_mode == "spot":
                help_text = common_commands + spot_commands + settings_commands + viz_commands
            else:  # futures mode
                help_text = common_commands + futures_commands + settings_commands + viz_commands
            
            await message.reply_text(
                help_text,
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            logger.error(f"Error sending help: {e}")
            await message.reply_text("Error sending help. Please try again.")

    async def cmd_stats(self, message: types.Message):
        """Handle /stats command - show performance statistics"""
        try:
            chat_id = message.chat.id
            user_id = message.from_user.id
            
            # Check if user is authorized
            if not self._is_authorized(user_id):
                await self.send_message(chat_id, "⛔ Unauthorized access")
                return
                
            # Send typing action
            await self.bot.send_chat_action(chat_id=chat_id, action="typing")
            
            # Get performance stats based on current mode
            if self.current_mode == "spot":
                stats = await self.mongo_client.get_performance_stats()
            else:  # futures mode
                stats = await self.mongo_client.get_futures_stats()
            
            if not stats:
                await self.send_message(chat_id, "No performance statistics available yet.")
                return
                
            # Format stats message based on mode
            if self.current_mode == "spot":
                # Format spot stats
                stats_text = self._format_spot_stats(stats)
            else:
                # Format futures stats
                stats_text = self._format_futures_stats(stats)
                
            # Send stats message
            await self.send_message(chat_id, stats_text, parse_mode=ParseMode.MARKDOWN)
            
        except Exception as e:
            logger.error(f"Error getting stats: {e}")
            await self.send_message(message.chat.id, f"Error getting stats: {str(e)}")
            
    def _format_spot_stats(self, stats: dict) -> str:
        """Format spot trading statistics"""
        try:
            # Extract stats
            total_orders = stats.get('total_orders', 0)
            filled_orders = stats.get('filled_orders', 0)
            cancelled_orders = stats.get('cancelled_orders', 0)
            total_volume = stats.get('total_volume', 0)
            avg_order_size = stats.get('avg_order_size', 0)
            total_fees = stats.get('total_fees', 0)
            profit_loss = stats.get('profit_loss', 0)
            win_rate = stats.get('win_rate', 0)
            
            # Format stats message
            stats_text = (
                "📊 *SPOT TRADING STATISTICS*\n\n"
                f"Total Orders: {total_orders}\n"
                f"Filled Orders: {filled_orders}\n"
                f"Cancelled Orders: {cancelled_orders}\n"
                f"Total Volume: ${float(total_volume):,.2f}\n"
                f"Average Order Size: ${float(avg_order_size):,.2f}\n"
                f"Total Fees: ${float(total_fees):,.2f}\n"
                f"Profit/Loss: ${float(profit_loss):,.2f}\n"
                f"Win Rate: {win_rate:.2f}%\n\n"
            )
            
            # Add symbol stats if available
            if 'symbols' in stats:
                stats_text += "*Symbol Performance:*\n"
                for symbol, symbol_stats in stats['symbols'].items():
                    symbol_volume = symbol_stats.get('volume', 0)
                    symbol_pnl = symbol_stats.get('pnl', 0)
                    symbol_orders = symbol_stats.get('orders', 0)
                    
                    # Add emoji based on PnL
                    emoji = "🟢" if symbol_pnl > 0 else "🔴" if symbol_pnl < 0 else "⚪"
                    
                    stats_text += f"{emoji} *{symbol}*: ${float(symbol_pnl):,.2f} ({symbol_orders} orders, ${float(symbol_volume):,.2f})\n"
            
            return stats_text
            
        except Exception as e:
            logger.error(f"Error formatting spot stats: {e}")
            return "Error formatting statistics."
            
    def _format_futures_stats(self, stats: dict) -> str:
        """Format futures trading statistics"""
        try:
            # Extract stats
            total_positions = stats.get('total_positions', 0)
            open_positions = stats.get('open_positions', 0)
            closed_positions = stats.get('closed_positions', 0)
            long_positions = stats.get('long_positions', 0)
            short_positions = stats.get('short_positions', 0)
            total_volume = stats.get('total_volume', 0)
            avg_leverage = stats.get('avg_leverage', 1)
            total_fees = stats.get('total_fees', 0)
            realized_pnl = stats.get('realized_pnl', 0)
            unrealized_pnl = stats.get('unrealized_pnl', 0)
            win_rate = stats.get('win_rate', 0)
            
            # Format stats message
            stats_text = (
                "📊 *FUTURES TRADING STATISTICS*\n\n"
                f"Total Positions: {total_positions}\n"
                f"Open Positions: {open_positions}\n"
                f"Closed Positions: {closed_positions}\n"
                f"Long Positions: {long_positions}\n"
                f"Short Positions: {short_positions}\n"
                f"Total Volume: ${float(total_volume):,.2f}\n"
                f"Average Leverage: {avg_leverage:.2f}x\n"
                f"Total Fees: ${float(total_fees):,.2f}\n"
                f"Realized PnL: ${float(realized_pnl):,.2f}\n"
                f"Unrealized PnL: ${float(unrealized_pnl):,.2f}\n"
                f"Win Rate: {win_rate:.2f}%\n\n"
            )
            
            # Add symbol stats if available
            if 'symbols' in stats:
                stats_text += "*Symbol Performance:*\n"
                for symbol, symbol_stats in stats['symbols'].items():
                    symbol_volume = symbol_stats.get('volume', 0)
                    symbol_pnl = symbol_stats.get('pnl', 0)
                    symbol_positions = symbol_stats.get('positions', 0)
                    
                    # Add emoji based on PnL
                    emoji = "🟢" if symbol_pnl > 0 else "🔴" if symbol_pnl < 0 else "⚪"
                    
                    stats_text += f"{emoji} *{symbol}*: ${float(symbol_pnl):,.2f} ({symbol_positions} positions, ${float(symbol_volume):,.2f})\n"
            
            return stats_text
            
        except Exception as e:
            logger.error(f"Error formatting futures stats: {e}")
            return "Error formatting statistics."

    async def set_leverage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set leverage for futures trading"""
        if not await self.is_user_authorized(update, context):
            return
            
        # Check if in futures mode
        if self.current_mode != "futures":
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="This command is only available in futures mode."
            )
            return
            
        # Check if user provided a valid leverage
        try:
            leverage = int(context.args[0])
            if 1 <= leverage <= 125:
                self.binance_client.leverage = leverage
                await update.message.reply_text(
                    f"Leverage set to {leverage}x for futures trading."
                )
            else:
                await update.message.reply_text(
                    "Invalid leverage format. Please use a number between 1 and 125."
                )
        except (IndexError, ValueError):
            await update.message.reply_text(
                "Invalid leverage format. Please use /leverage <number>."
            )

    async def help(self, update: Update, context: CallbackContext) -> None:
        """Show help message"""
        if not await self.is_user_authorized(update, context):
            return

        try:
            message = "🤖 *TRADE-A-SAURUS REX HELP* 🦖\n\n"
            
            # General commands
            message += "*General Commands:*\n"
            message += "/start - Start the bot\n"
            message += "/menu - Show main menu\n"
            message += "/help - Show this help message\n"
            message += "/mode - Toggle between Spot/Futures mode\n\n"
            
            # Trading commands
            message += "*Trading Commands:*\n"
            message += "/trade - Start a new trade\n"
            message += "/cancel - Cancel active orders\n"
            message += "/orders - View order history\n"
            message += "/balance - Check account balance\n"
            message += "/stats - View performance statistics\n"
            message += "/chart - Get price chart\n"
            message += "/orderamount - Set default order amount\n\n"
            
            # TP/SL commands
            message += "*TP/SL Commands:*\n"
            message += "/tpsl - Show TP/SL menu\n"
            message += "/updatetpsl - Update TP/SL settings\n"
            message += "/toggletpsl - Toggle TP/SL on/off\n\n"
            
            # Futures-specific commands
            message += "*Futures-Specific Commands:*\n"
            message += "/positions - View open futures positions\n"
            message += "/leverage - Set leverage (1-125)\n"
            message += "/marginmode - Set margin mode (isolated/cross)\n\n"
            
            # Examples
            message += "*Examples:*\n"
            message += "/trade - Start a new trade\n"
            message += "/leverage 10 - Set futures leverage to 10x\n"
            message += "/marginmode isolated - Set margin mode to isolated\n"
            message += "/orderamount 100 - Set default order amount to 100 USDT\n"
            
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=message,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Error showing help: {e}")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"Error showing help: {e}"
            )

    async def is_user_authorized(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Check if the user is authorized"""
        return update.effective_user.id in self.allowed_users

    async def handle_tpsl_tp_percentage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle input of new TP percentage"""
        try:
            tp_percentage = float(update.message.text.strip())
            if 0 < tp_percentage <= 100:
                self.binance_client.default_tp_percentage = tp_percentage
                await update.message.reply_text(
                    f"New Take Profit percentage set to {tp_percentage}%"
                )
                return "tpsl_tp_percentage"

    async def get_positions(self, update: Update, context: CallbackContext) -> None:
        """Get open futures positions"""
        if not await self.is_user_authorized(update, context):
            return

        try:
            # Check if in futures mode
            if self.current_mode != "futures":
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="This command is only available in futures mode."
                )
                return
                
            # Get open positions
            positions = await self.mongo_client.get_open_futures_positions()
            
            if not positions:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="No open positions found."
                )
                return

            # Format position information
            message = "📊 *Open Futures Positions*\n\n"
            for pos in positions:
                symbol = pos['symbol']
                direction = "LONG 📈" if pos['direction'] == "LONG" else "SHORT 📉"
                leverage = pos['leverage']
                entry_price = float(pos['entry_price'])
                current_price = await self.binance_client.get_current_price(symbol)
                quantity = float(pos['quantity'])
                liq_price = float(pos.get('liquidation_price', 0))
                
                # Calculate PnL
                if direction == "LONG 📈":
                    pnl = (current_price - entry_price) * quantity * leverage
                else:
                    pnl = (entry_price - current_price) * quantity * leverage
                
                # Format TP/SL info if available
                tp_info = f"TP: ${float(pos['tp_price']):.2f}" if pos.get('tp_price') else "No TP"
                sl_info = f"SL: ${float(pos['sl_price']):.2f}" if pos.get('sl_price') else "No SL"

                message += (
                    f"*{symbol}* - {direction}\n"
                    f"Leverage: {leverage}x\n"
                    f"Entry: ${entry_price:.2f}\n"
                    f"Current: ${current_price:.2f}\n"
                    f"Size: {quantity:.4f}\n"
                    f"Liq.Price: ${liq_price:.2f}\n"
                    f"{tp_info} | {sl_info}\n"
                    f"PnL: ${pnl:.2f} ({((current_price/entry_price - 1) * 100 * leverage):.2f}%)\n\n"
                )

            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=message,
                parse_mode='Markdown'
            )

        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Error retrieving positions. Please try again."
            )

    async def monitor_positions(self):
        """Monitor open positions for TP/SL hits and potential risks"""
        logger.info("Starting position monitoring...")
        
        while self.running:
            try:
                # Get all open positions
                positions = await self.mongo_client.get_open_futures_positions()
                
                for position in positions:
                    symbol = position['symbol']
                    current_price = await self.binance_client.get_current_price(symbol)
                    
                    # Get margin information
                    margin_info = await self.binance_client.get_position_margin_info(symbol)
                    if margin_info:
                        required_margin = Decimal(str(margin_info['required_margin']))
                        available_margin = Decimal(str(margin_info['available_margin']))
                        margin_ratio = (available_margin / required_margin) * 100
                        
                        # Check margin levels
                        if margin_ratio < 150:  # Warning at 150% margin ratio
                    # Check TP hit
                    if position.get('tp_price') and float(position['tp_price']):
                        tp_price = float(position['tp_price'])
                        if (position['direction'] == 'LONG' and current_price >= tp_price) or \
                           (position['direction'] == 'SHORT' and current_price <= tp_price):
                            # Calculate PnL
                            entry_price = float(position['entry_price'])
                            quantity = float(position['quantity'])
                            leverage = position['leverage']
                            
                            if position['direction'] == 'LONG':
                                pnl = (tp_price - entry_price) * quantity * leverage
                            else:
                                pnl = (entry_price - tp_price) * quantity * leverage
                            
                            # Record TP hit and PnL
                            await self.mongo_client.record_futures_pnl(
                                order_id=position['order_id'],
                                pnl=pnl,
                                close_price=tp_price,
                                close_time=datetime.utcnow()
                            )
                            
                            # Send notification
                            await self.send_message(
                                chat_id=position['user_id'],
                                text=f"🎯 Take Profit Hit for {symbol}\n"
                                     f"Entry: ${entry_price:.2f}\n"
                                     f"TP: ${tp_price:.2f}\n"
                                     f"PnL: ${pnl:.2f}"
                            )
                    
                    # Check SL hit
                    if position.get('sl_price') and float(position['sl_price']):
                        sl_price = float(position['sl_price'])
                        if (position['direction'] == 'LONG' and current_price <= sl_price) or \
                           (position['direction'] == 'SHORT' and current_price >= sl_price):
                            # Calculate PnL
                            entry_price = float(position['entry_price'])
                            quantity = float(position['quantity'])
                            leverage = position['leverage']
                            
                            if position['direction'] == 'LONG':
                                pnl = (sl_price - entry_price) * quantity * leverage
                            else:
                                pnl = (entry_price - sl_price) * quantity * leverage
                            
                            # Record SL hit and PnL
                            await self.mongo_client.record_futures_pnl(
                                order_id=position['order_id'],
                                pnl=pnl,
                                close_price=sl_price,
                                close_time=datetime.utcnow()
                            )
                            
                            # Send notification
                            await self.send_message(
                                chat_id=position['user_id'],
                                text=f"🛑 Stop Loss Hit for {symbol}\n"
                                     f"Entry: ${entry_price:.2f}\n"
                                     f"SL: ${sl_price:.2f}\n"
                                     f"PnL: ${pnl:.2f}"
                            )
                
                await asyncio.sleep(5)  # Check every 5 seconds
                
            except Exception as e:
                logger.error(f"Error in position monitoring: {e}")
                await asyncio.sleep(30)  # Sleep longer on error
