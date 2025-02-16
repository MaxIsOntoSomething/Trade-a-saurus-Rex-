import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, ConversationHandler, CallbackQueryHandler, MessageHandler, filters
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Optional, Dict
import asyncio
import os
import sys
import signal
import traceback
from aiohttp import web
from pathlib import Path

# Import utility modules
from ..market.analysis import MarketAnalyzer
from ..database.mongo_client import MongoClient
from ..trading.binance_client import BinanceClient

# Set up logging
logger = logging.getLogger(__name__)

# Define constants
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

# Define states for conversation handler
SYMBOL, ORDER_TYPE, LEVERAGE, DIRECTION, AMOUNT, PRICE, FEES = range(7)

class VisualizationType:
    """Enum for visualization types"""
    DAILY_VOLUME = "daily_volume"
    PROFIT_DIST = "profit_distribution"
    ORDER_TYPES = "order_types"
    HOURLY_ACTIVITY = "hourly_activity"

class InfoManager:
    """Manages system information display"""
    def __init__(self, bot: TelegramBot):
        self.bot = bot
        self.start_time = datetime.utcnow()
        self.error_count = 0
        self.last_error = None

    def log_error(self, error: Exception):
        """Log error for tracking"""
        self.error_count += 1
        self.last_error = {
            'message': str(error),
            'time': datetime.utcnow()
        }

    async def show_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Display system information"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        try:
            # Calculate uptime
            uptime = datetime.utcnow() - self.start_time
            hours = int(uptime.total_seconds() // 3600)
            minutes = int((uptime.total_seconds() % 3600) // 60)

            # Get current mode and settings
            current_mode = self.bot.config['environment']['trading_mode'].upper()
            is_testnet = self.bot.config['environment']['testnet']

            # Get active pairs
            active_pairs = self.bot.config['trading']['pairs']

            # Format message sections
            mode_info = [
                "🔧 Operating Mode:",
                f"• Mode: {current_mode}",
                f"• Environment: {'Testnet' if is_testnet else 'Mainnet'}",
                f"• Trading Status: {'Paused ⏸' if self.bot.is_paused else 'Active ▶️'}"
            ]

            config_info = [
                "\n⚙️ Configuration:",
                f"• Base Currency: {self.bot.config['trading']['base_currency']}",
                f"• Reserve Balance: ${self.bot.binance_client.reserve_balance:.2f}",
                f"• Active Pairs: {', '.join(active_pairs)}"
            ]

            # Add futures-specific settings if in futures mode
            if current_mode == "FUTURES":
                futures_settings = self.bot.config['trading'].get('futures_settings', {})
                config_info.extend([
                    f"• Default Leverage: {futures_settings.get('leverage', 'Not set')}x",
                    f"• Margin Type: {futures_settings.get('margin_type', 'Not set')}",
                    f"• Position Mode: {futures_settings.get('position_mode', 'Not set')}"
                ])

            system_info = [
                "\n📊 System Status:",
                f"• Uptime: {hours}h {minutes}m",
                f"• Error Count: {self.error_count}",
            ]

            # Add last error info if exists
            if self.last_error:
                time_since_error = datetime.utcnow() - self.last_error['time']
                system_info.extend([
                    "• Last Error:",
                    f"  - {self.last_error['message']}",
                    f"  - {time_since_error.seconds // 60}m ago"
                ])

            # Combine all sections
            full_message = "\n".join([
                *mode_info,
                *config_info,
                *system_info,
                f"\nLast Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
            ])

            await update.message.reply_text(
                full_message,
                reply_markup=self.bot.markup
            )

        except Exception as e:
            logger.error(f"Error showing system info: {e}")
            self.log_error(e)
            await update.message.reply_text("❌ Error fetching system information")

class TelegramBot:
    """Main Telegram bot class with improved organization"""
    def __init__(self, token: str, allowed_users: List[int], 
                 binance_client: BinanceClient, mongo_client: MongoClient,
                 config: dict):
        # Core attributes
        self.token = token
        self.allowed_users = allowed_users
        self.binance_client = binance_client
        self.mongo_client = mongo_client
        self.config = config
        
        # Bot state
        self.app = None
        self.is_paused = False
        self.running = False
        self._polling_task = None
        self._update_id = 0
        self.temp_trade_data = {}
        self.sent_roars = set()
        
        # UI elements
        self.keyboard = [
            [KeyboardButton("/menu"), KeyboardButton("/power")],
            [KeyboardButton("/add"), KeyboardButton("/balance")],
            [KeyboardButton("/positions"), KeyboardButton("/mode")]
        ]
        self.markup = ReplyKeyboardMarkup(self.keyboard, resize_keyboard=True)
        
        # Environment info
        self.env_info = (
            "📍 Environment: "
            f"{'Testnet' if config.get('environment', {}).get('testnet', True) else 'Mainnet'} | "
            f"{config['trading']['base_currency']}"
        )
        
        # Menu patterns for callback handling
        self.MENU_PATTERNS = {
            'main': 'menu_main',
            'account': 'menu_account',
            'trading': 'menu_trading',
            'analysis': 'menu_analysis',
            'settings': 'menu_settings'
        }
        
        # Set this bot instance in binance client
        self.binance_client.set_telegram_bot(self)

        # Add category managers
        self.trade_manager = TradeCommandManager(self)
        self.settings_manager = SettingsManager(self)
        self.portfolio_manager = PortfolioManager(self)
        self.automation_manager = AutomationManager(self)
        self.menu_manager = MenuManager(self)
        self.info_manager = InfoManager(self)

        # Add crash handling
        sys.excepthook = self._handle_crash

    async def initialize(self):
        """Initialize the Telegram bot with modular command structure"""
        self.app = Application.builder().token(self.token).build()
        
        # Initialize market analyzer
        self.market_analyzer = MarketAnalyzer()
        
        # Register command handlers by category
        await self._register_trade_commands()
        await self._register_settings_commands()
        await self._register_portfolio_commands()
        await self._register_automation_commands()
        await self._register_info_commands()
        
        # Register core conversation handlers
        add_trade_handler = ConversationHandler(
            entry_points=[CommandHandler("add", self.trade_manager.add_trade_start)],
            states=self.trade_manager.get_conversation_states(),
            fallbacks=[CommandHandler("cancel", self.trade_manager.add_trade_cancel)]
        )
        self.app.add_handler(add_trade_handler)
        
        # Register additional command handlers
        self.app.add_handler(CommandHandler("openOrders", self.trade_manager.show_open_orders))
        self.app.add_handler(CommandHandler("changemode", self.settings_manager.change_mode))
        self.app.add_handler(CallbackQueryHandler(
            self.settings_manager.handle_mode_confirmation, 
            pattern="^mode_"
        ))
        
        # Register menu handlers
        self.app.add_handler(CommandHandler("menu", self.menu_manager.show_main_menu))
        self.app.add_handler(CallbackQueryHandler(self.menu_manager.handle_menu_callback, pattern="^menu_"))
        self.app.add_handler(CallbackQueryHandler(self.menu_manager.handle_submenu_callback, pattern="^submenu_"))
        
        await self.app.initialize()
        await self.app.start()

    async def _register_trade_commands(self):
        """Register trading-related commands"""
        handlers = [
            CommandHandler("stats", self.trade_manager.show_stats),
            CommandHandler("orders", self.trade_manager.show_open_orders),
            CommandHandler("market", self.trade_manager.show_market_info),
            CommandHandler("info", self.trade_manager.show_pair_info),
            CommandHandler("help", self.trade_manager.show_help),
            CommandHandler("positions", self.trade_manager.show_positions),
            CommandHandler("market", self.trade_manager.show_market),
            CommandHandler("thresholds", self.trade_manager.show_thresholds),
            CommandHandler("history", self.trade_manager.show_history),
            CommandHandler("profits", self.trade_manager.show_profits)
        ]
        for handler in handlers:
            self.app.add_handler(handler)

    async def _register_settings_commands(self):
        """Register settings-related commands"""
        handlers = [
            CommandHandler("mode", self.settings_manager.switch_mode),
            CommandHandler("leverage", self.settings_manager.set_leverage),
            CommandHandler("margin", self.settings_manager.set_margin_type),
            CommandHandler("hedge", self.settings_manager.toggle_hedge_mode)
        ]
        for handler in handlers:
            self.app.add_handler(handler)

    async def _register_portfolio_commands(self):
        """Register portfolio-related commands"""
        handlers = [
            CommandHandler("balance", self.portfolio_manager.show_balance),
            CommandHandler("portfolio", self.portfolio_manager.show_portfolio),
            CommandHandler("viz", self.portfolio_manager.show_viz_menu),
            CallbackQueryHandler(self.portfolio_manager.handle_viz_selection, 
                               pattern="^(daily_volume|profit_distribution|order_types|hourly_activity)$")
        ]
        for handler in handlers:
            self.app.add_handler(handler)

    async def _register_automation_commands(self):
        """Register automation-related commands"""
        handlers = [
            CommandHandler("power", self.automation_manager.toggle_trading),
            CommandHandler("summary", self.automation_manager.show_weekly_summary)
        ]
        for handler in handlers:
            self.app.add_handler(handler)

    async def _register_info_commands(self):
        """Register information-related commands"""
        handlers = [
            CommandHandler("info", self.info_manager.show_info),
            CommandHandler("marketinfo", self.trade_manager.show_market_info),
            CommandHandler("pairinfo", self.trade_manager.show_pair_info)
        ]
        for handler in handlers:
            self.app.add_handler(handler)

    # Keep core utility methods
    def _is_authorized(self, user_id: int) -> bool:
        """Check if user is authorized"""
        return user_id in self.allowed_users

    async def send_notification(self, message: str, include_markup: bool = True):
        """Send notification to all authorized users"""
        for user_id in self.allowed_users:
            try:
                await self.app.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    reply_markup=self.markup if include_markup else None
                )
            except Exception as e:
                logger.error(f"Failed to send notification to {user_id}: {e}")

    # Remove redundant handler methods that have been moved to managers
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
            f"Price: ${float(order.price):,.2f}\n"
            f"Total: ${float(total_value):,.2f} USDT\n"
        )

        for user_id in self.allowed_users:
            try:
                await self.app.bot.send_message(
                    chat_id=user_id,
                    text=message
                )
            except Exception as e:
                logger.error(f"Failed to send order notification to {user_id}: {e}")

        # Mark order as notified
        if status == OrderStatus.FILLED:
            self.sent_roars.add(order.order_id)

    async def start_monitoring_server(self):
        """Start health check server"""
        app = web.Application()
        app.router.add_get('/health', self._health_check)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, 'localhost', 8080)
        await site.start()
        return runner

    async def _health_check(self, request):
        """Health check endpoint"""
        return web.Response(text="OK")

    def _handle_crash(self, exc_type, exc_value, exc_traceback):
        """Handle crashes and save reports"""
        crash_time = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        crash_file = f"crash_reports/crash_{crash_time}.txt"
        
        try:
            os.makedirs("crash_reports", exist_ok=True)
            with open(crash_file, "w") as f:
                f.write(f"Crash Report - {crash_time}\n\n")
                traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)
                
            logger.critical(f"Bot crashed! Report saved to {crash_file}")
            
            # Force restart container if in Docker
            if os.getenv('RUNNING_IN_DOCKER') == 'true':
                os.kill(1, signal.SIGTERM)
        except Exception as e:
            logger.critical(f"Failed to save crash report: {e}")

    async def run(self):
        """Main entry point with crash recovery"""
        print(DINO_ASCII)
        logger.info("Starting Trade-a-saurus Rex...")

        # Start monitoring server
        monitor = await self.start_monitoring_server()
        
        try:
            # Initialize bot
            await self.initialize()

            # Initialize scheduler
            scheduler = WeeklySummaryScheduler(self, self.mongo_client)
            
            # Run components with automatic recovery
            while True:
                try:
                    # Start both bot and scheduler
                    tasks = [
                        asyncio.create_task(self.start()),
                        asyncio.create_task(scheduler.run())
                    ]
                    
                    # Monitor tasks
                    while True:
                        for task in tasks:
                            if task.done() and task.exception():
                                raise task.exception()
                            await asyncio.sleep(1)
                            
                except Exception as e:
                    logger.error(f"Service error, restarting: {e}")
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await asyncio.sleep(5)
                    continue

        except Exception as e:
            logger.critical(f"Fatal error: {e}", exc_info=True)
            raise
        finally:
            await monitor.cleanup()
            await self.stop()
            logger.info("Bot shutdown complete")

    @classmethod
    async def create(cls, config: dict):
        """Factory method to create bot instance"""
        try:
            # Initialize clients
            binance_client = BinanceClient(
                api_key=config['binance']['api_key'],
                api_secret=config['binance']['api_secret'],
                testnet=config['environment']['testnet'],
                base_currency=config['trading']['base_currency'],
                reserve_balance=config['trading']['reserve_balance'],
                config=config
            )
            await binance_client.initialize()

            mongo_client = MongoClient(
                uri=config['mongodb']['uri'],
                database=config['mongodb']['database']
            )
            await mongo_client.init_indexes()

            # Create bot instance
            bot = cls(
                token=config['telegram']['bot_token'],
                allowed_users=config['telegram']['allowed_users'],
                binance_client=binance_client,
                mongo_client=mongo_client,
                config=config
            )

            return bot

        except Exception as e:
            logger.critical(f"Failed to create bot: {e}")
            raise

class TradeCommandManager:
    """Manages trading-related commands"""
    def __init__(self, bot: TelegramBot):
        self.bot = bot
        self.temp_trade_data = {}

        # Add new command placeholders
        self.commands = {
            'stats': 'Show trading statistics',
            'openOrders': 'Show open orders',
            'market': 'Show market information',
            'info': 'Show trading pair information',
            'portfolio': 'Show portfolio overview',
            'addTrade': 'Add manual trade'
        }
        self.SPOT_FEE = Decimal('0.001')  # 0.1%
        self.FUTURES_FEE = Decimal('0.0004')  # 0.04%

    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show detailed trading statistics"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        try:
            # Get current mode
            current_mode = self.bot.config['environment']['trading_mode'].upper()
            message_parts = [f"📊 Trading Statistics ({current_mode})"]
            
            # Get current balance
            if current_mode == "FUTURES":
                account = await self.bot.binance_client.get_account_info()
                balance_info = [
                    f"\n💰 Account Balance:",
                    f"• Wallet Balance: ${float(account['totalWalletBalance']):,.2f}",
                    f"• Unrealized P/L: ${float(account['totalUnrealizedProfit']):+,.2f}",
                    f"• Available: ${float(account['availableBalance']):,.2f}"
                ]
            else:
                spot_balance = await self.bot.binance_client.get_balance()
                balance_info = [
                    f"\n💰 Spot Balance:",
                    f"• USDT: ${float(spot_balance):,.2f}"
                ]
            message_parts.extend(balance_info)

            # Get order metrics
            order_stats = await self._get_order_metrics()
            message_parts.extend([
                f"\n📈 Order Metrics (24h):",
                f"• Total Orders: {order_stats['total']}",
                f"• Successful: {order_stats['filled']} ({order_stats['fill_rate']}%)",
                f"• Cancelled: {order_stats['cancelled']}",
                f"• Pending: {order_stats['pending']}"
            ])

            # Get execution history
            exec_stats = await self._get_execution_stats()
            message_parts.extend([
                f"\n⚡ Execution Stats:",
                f"• Avg Fill Time: {exec_stats['avg_fill_time']}s",
                f"• Success Rate: {exec_stats['success_rate']}%",
                f"• Total Volume: ${float(exec_stats['volume']):,.2f}"
            ])

            # Get performance metrics
            perf = await self._get_performance_metrics()
            message_parts.extend([
                f"\n🎯 Performance (7d):",
                f"• Win Rate: {perf['win_rate']}%",
                f"• Avg Profit: ${float(perf['avg_profit']):+,.2f}",
                f"• Total P/L: ${float(perf['total_pnl']):+,.2f}",
                f"• Total Fees: ${float(perf['total_fees']):,.2f}"
            ])

            # Add trading status
            message_parts.append(
                f"\n🤖 Bot Status: {'Paused ⏸' if self.bot.is_paused else 'Active ▶️'}"
            )

            await update.message.reply_text(
                "\n".join(message_parts),
                reply_markup=self.bot.markup
            )

        except Exception as e:
            logger.error(f"Error getting stats: {e}")
            await update.message.reply_text(
                "❌ Error fetching statistics. Please try again later."
            )

    async def _get_order_metrics(self) -> dict:
        """Get order metrics for the last 24 hours"""
        try:
            now = datetime.utcnow()
            yesterday = now - timedelta(days=1)
            
            # Query orders from last 24h
            pipeline = [
                {"$match": {
                    "created_at": {"$gte": yesterday}
                }},
                {"$group": {
                    "_id": "$status",
                    "count": {"$sum": 1}
                }}
            ]
            
            stats = {
                "total": 0,
                "filled": 0,
                "cancelled": 0,
                "pending": 0,
                "fill_rate": 0
            }
            
            async for doc in self.bot.mongo_client.orders.aggregate(pipeline):
                status = doc["_id"]
                count = doc["count"]
                stats["total"] += count
                
                if status == OrderStatus.FILLED.value:
                    stats["filled"] = count
                elif status == OrderStatus.CANCELLED.value:
                    stats["cancelled"] = count
                elif status == OrderStatus.PENDING.value:
                    stats["pending"] = count
            
            # Calculate fill rate
            if stats["total"] > 0:
                stats["fill_rate"] = round((stats["filled"] / stats["total"]) * 100, 2)
            
            return stats
            
        except Exception as e:
            logger.error(f"Error getting order metrics: {e}")
            return {
                "total": 0,
                "filled": 0,
                "cancelled": 0,
                "pending": 0,
                "fill_rate": 0
            }

    async def _get_execution_stats(self) -> dict:
        """Get execution statistics"""
        try:
            pipeline = [
                {"$match": {
                    "status": OrderStatus.FILLED.value,
                    "filled_at": {"$exists": True}
                }},
                {"$project": {
                    "fill_time": {
                        "$divide": [
                            {"$subtract": ["$filled_at", "$created_at"]},
                            1000  # Convert to seconds
                        ]
                    },
                    "value": {"$multiply": [
                        {"$toDecimal": "$price"},
                        {"$toDecimal": "$quantity"}
                    ]}
                }},
                {"$group": {
                    "_id": None,
                    "avg_fill_time": {"$avg": "$fill_time"},
                    "total_orders": {"$sum": 1},
                    "volume": {"$sum": "$value"}
                }}
            ]
            
            result = await self.bot.mongo_client.orders.aggregate(pipeline).to_list(1)
            if result:
                doc = result[0]
                # Calculate success rate from total orders
                total_orders = await self.bot.mongo_client.orders.count_documents({})
                success_rate = round((doc["total_orders"] / total_orders) * 100, 2) if total_orders > 0 else 0
                
                return {
                    "avg_fill_time": round(doc["avg_fill_time"], 2),
                    "success_rate": success_rate,
                    "volume": float(doc["volume"])
                }
            
            return {"avg_fill_time": 0, "success_rate": 0, "volume": 0}
            
        except Exception as e:
            logger.error(f"Error getting execution stats: {e}")
            return {"avg_fill_time": 0, "success_rate": 0, "volume": 0}

    async def _get_performance_metrics(self) -> dict:
        """Get performance metrics for the last 7 days"""
        try:
            week_ago = datetime.utcnow() - timedelta(days=7)
            
            pipeline = [
                {"$match": {
                    "status": OrderStatus.FILLED.value,
                    "filled_at": {"$gte": week_ago}
                }},
                {"$group": {
                    "_id": None,
                    "total_trades": {"$sum": 1},
                    "winning_trades": {
                        "$sum": {"$cond": [{"$gt": ["$realized_pnl", 0]}, 1, 0]}
                    },
                    "total_pnl": {"$sum": {"$toDecimal": "$realized_pnl"}},
                    "total_fees": {"$sum": {"$toDecimal": "$fees"}},
                    "profits": {
                        "$push": {"$cond": [{"$gt": ["$realized_pnl", 0]}, "$realized_pnl", None]}
                    }
                }}
            ]
            
            result = await self.bot.mongo_client.orders.aggregate(pipeline).to_list(1)
            if result:
                doc = result[0]
                win_rate = round((doc["winning_trades"] / doc["total_trades"]) * 100, 2) if doc["total_trades"] > 0 else 0
                
                # Calculate average profit from winning trades only
                profits = [p for p in doc["profits"] if p is not None]
                avg_profit = sum(profits) / len(profits) if profits else 0
                
                return {
                    "win_rate": win_rate,
                    "avg_profit": float(avg_profit),
                    "total_pnl": float(doc["total_pnl"]),
                    "total_fees": float(doc["total_fees"])
                }
            
            return {"win_rate": 0, "avg_profit": 0, "total_pnl": 0, "total_fees": 0}
            
        except Exception as e:
            logger.error(f"Error getting performance metrics: {e}")
            return {"win_rate": 0, "avg_profit": 0, "total_pnl": 0, "total_fees": 0}

    def get_conversation_states(self) -> Dict:
        """Get states for add trade conversation"""
        return {
            SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_symbol)],
            ORDER_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_order_type)],
            LEVERAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_leverage)],
            DIRECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_direction)],
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_amount)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_price)],
            FEES: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_final)],
            POSITION_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_position_type)],
            TP_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_tp)],
            SL_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_trade_sl)]
        }

    async def show_open_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show detailed open orders with profit/loss info"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        try:
            current_mode = self.bot.config['environment']['trading_mode'].upper()
            pending_orders = await self.bot.mongo_client.get_pending_orders()
            
            if not pending_orders:
                await update.message.reply_text(
                    "📝 No open orders found.\n\n"
                    "Use /add to create a new order."
                )
                return

            # Group orders by type
            spot_orders = []
            futures_orders = []
            
            for order in pending_orders:
                if order.order_type == OrderType.FUTURES:
                    futures_orders.append(order)
                else:
                    spot_orders.append(order)

            message_parts = [f"📖 Open Orders ({current_mode})\n"]

            # Add Spot Orders section
            if spot_orders:
                message_parts.extend(await self._format_spot_orders(spot_orders))

            # Add Futures Orders section
            if futures_orders:
                message_parts.extend(await self._format_futures_orders(futures_orders))

            # Add note about canceling orders
            message_parts.append(
                "\n⚠️ To cancel orders, use /cancelALLOrders"
            )

            # Split message if too long
            message = "\n".join(message_parts)
            if len(message) > 4096:
                # Split and send in parts
                for i in range(0, len(message), 4096):
                    await update.message.reply_text(message[i:i+4096])
            else:
                await update.message.reply_text(message)

        except Exception as e:
            logger.error(f"Error showing open orders: {e}")
            await update.message.reply_text("❌ Error fetching open orders")

    async def _format_spot_orders(self, orders: List[Order]) -> List[str]:
        """Format spot orders for display"""
        if not orders:
            return []

        formatted = ["📈 Spot Orders:"]
        
        for order in orders:
            current_price = await self.bot.binance_client.get_current_price(order.symbol)
            price_diff = ((current_price - float(order.price)) / float(order.price)) * 100

            order_info = [
                f"\n{order.symbol}:",
                f"• Amount: {float(order.quantity):.8f}",
                f"• Order Price: ${float(order.price):.2f}",
                f"• Current Price: ${current_price:.2f} ({price_diff:+.2f}%)",
                f"• Total Value: ${float(order.price * order.quantity):,.2f}",
                f"• Created: {order.created_at.strftime('%Y-%m-%d %H:%M')}"
            ]

            # Add TP/SL info if present
            if order.tp_price:
                order_info.append(f"• Take Profit: ${float(order.tp_price):.2f}")
            if order.sl_price:
                order_info.append(f"• Stop Loss: ${float(order.sl_price)::.2f}")

            formatted.extend(order_info)

        return formatted

    async def _format_futures_orders(self, orders: List[Order]) -> List[str]:
        """Format futures orders for display"""
        if not orders:
            return []

        formatted = ["\n📊 Futures Orders:"]
        
        for order in orders:
            current_price = await self.bot.binance_client.get_current_price(order.symbol)
            price_diff = ((current_price - float(order.price)) / float(order.price)) * 100
            
            # Calculate potential PnL
            pnl = self._calculate_futures_pnl(
                order.direction,
                float(order.price),
                current_price,
                float(order.quantity),
                order.leverage
            )

            order_info = [
                f"\n{order.symbol}:",
                f"• Direction: {order.direction.value}",
                f"• Leverage: {order.leverage}x",
                f"• Amount: {float(order.quantity)::.8f}",
                f"• Order Price: ${float(order.price):,.2f}",
                f"• Current Price: ${current_price:.2f} ({price_diff:+.2f}%)",
                f"• Total Value: ${float(order.price * order.quantity):,.2f}",
                f"• Unrealized P/L: ${pnl:+.2f}",
                f"• Created: {order.created_at.strftime('%Y-%m-%d %H:%M')}"
            ]

            # Add TP/SL info if present
            if order.tp_price:
                tp_pnl = self._calculate_futures_pnl(
                    order.direction,
                    float(order.price),
                    float(order.tp_price),
                    float(order.quantity),
                    order.leverage
                )
                order_info.append(
                    f"• Take Profit: ${float(order.tp_price):.2f} (P/L: ${tp_pnl:+.2f})"
                )

            if order.sl_price:
                sl_pnl = self._calculate_futures_pnl(
                    order.direction,
                    float(order.price),
                    float(order.sl_price),
                    float(order.quantity),
                    order.leverage
                )
                order_info.append(
                    f"• Stop Loss: ${float(order.sl_price):,.2f} (P/L: ${sl_pnl:+.2f})"
                )

            formatted.extend(order_info)

        return formatted

    def _calculate_futures_pnl(self, direction: TradeDirection, entry_price: float, 
                             current_price: float, quantity: float, leverage: int) -> float:
        """Calculate futures PnL"""
        price_diff = current_price - entry_price
        if direction == TradeDirection.SHORT:
            price_diff = -price_diff

        return (price_diff * quantity * leverage)

    async def show_open_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show open orders with status"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        await update.message.reply_text(
            "🔄 Open Orders (Under Development)\n"
            "This command will show all open orders with their status."
        )

    async def show_market_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show market information for trading pairs"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        await update.message.reply_text(
            "📊 Market Information (Under Development)\n"
            "This command will show current market data for configured pairs."
        )

    async def show_pair_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show detailed information for a trading pair"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        await update.message.reply_text(
            "ℹ️ Trading Pair Info (Under Development)\n"
            "This command will show detailed information about trading pairs."
        )

    async def show_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show help information for available commands"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
        
        help_text = ["📚 Available Commands:"]
        for cmd, desc in self.commands.items():
            help_text.append(f"/{cmd} - {desc}")
        
        await update.message.reply_text("\n".join(help_text))

    async def show_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show open positions"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        # Implement your open positions logic here
        await update.message.reply_text("Open Positions")

    async def show_market(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show detailed market analysis"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        try:
            # Get configured pairs
            pairs = self.bot.config['trading']['pairs']
            if not pairs:
                await update.message.reply_text("No trading pairs configured.")
                return

            # Show loading message
            loading_message = await update.message.reply_text(
                "🔄 Analyzing market data..."
            )

            # Process each symbol
            analyses = []
            for symbol in pairs:
                # Get historical prices
                prices = await self.bot.binance_client.get_historical_prices(symbol)
                if not prices:
                    continue

                # Get market analysis
                analysis = await self.bot.market_analyzer.get_market_summary(symbol, prices)
                if analysis:
                    analyses.append((symbol, analysis))

            if not analyses:
                await loading_message.edit_text("❌ No market data available.")
                return

            # Format results
            message_parts = ["📊 Market Analysis\n"]
            
            for symbol, analysis in analyses:
                price_info = analysis['price']
                tech = analysis['technical']
                ichimoku = analysis['ichimoku']
                sentiment = analysis['sentiment']
                
                symbol_analysis = [
                    f"\n🔸 {symbol}:",
                    f"Price: ${price_info['current']:,.2f} ({price_info['change_24h']:+.2f}%)",
                    f"Volatility: {price_info['volatility']:.1f}%",
                    
                    "\nTechnical Indicators:",
                    f"• RSI: {tech['rsi']:.1f}" + (" 💹" if tech['signals']['rsi_oversold'] else " 📉" if tech['signals']['rsi_overbought'] else ""),
                    f"• Trend: {tech['trend']['direction']} (Strength: {tech['trend']['strength']:.1f}%)",
                    f"• MACD: {tech['macd']['histogram']:+.2f}" + (" 🔼" if tech['signals']['macd_crossover'] else ""),
                    
                    "\nCloud Analysis:",
                    f"• Position: {ichimoku['position']}",
                    f"• Strength: {ichimoku['strength']}",
                    
                    "\nMarket Sentiment:",
                    f"• Google Trends: {sentiment['relative_interest']}",
                    f"• Direction: {sentiment['trend_direction']}"
                ]
                
                # Add trading signals
                signals = []
                if tech['signals']['rsi_oversold']:
                    signals.append("Oversold")
                if tech['signals']['rsi_overbought']:
                    signals.append("Overbought")
                if tech['signals']['macd_crossover']:
                    signals.append("MACD Crossover")
                
                if signals:
                    symbol_analysis.append(f"\n⚡ Signals: {', '.join(signals)}")
                
                message_parts.append("\n".join(symbol_analysis))

            # Add analysis timestamp
            message_parts.append(f"\n\nLast Updated: {analyses[0][1]['analysis_time']}")

            # Delete loading message and send analysis
            await loading_message.delete()
            
            # Split message if too long
            full_message = "\n".join(message_parts)
            if len(full_message) > 4096:
                # Split into parts
                for i in range(0, len(full_message), 4096):
                    await update.message.reply_text(full_message[i:i+4096])
            else:
                await update.message.reply_text(full_message)

        except Exception as e:
            logger.error(f"Error showing market analysis: {e}")
            await update.message.reply_text(
                "❌ Error analyzing market data. Please try again later."
            )

    async def show_thresholds(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show threshold status"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        # Implement your threshold status logic here
        await update.message.reply_text("Threshold Status")

    async def show_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Get recent order history"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        try:
            # Get last 5 orders
            cursor = self.bot.mongo_client.orders.find().sort("created_at", -1).limit(5)
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

    async def show_profits(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show profit/loss information"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        # Implement your profit/loss logic here
        await update.message.reply_text("Profit/Loss Information")

    async def add_trade_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start the add trade conversation"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        await update.message.reply_text("Enter the symbol (e.g., BTCUSDT):")
        return SYMBOL

    async def add_trade_symbol(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle symbol input"""
        self.temp_trade_data['symbol'] = update.message.text.upper()
        await update.message.reply_text("Enter the order type (LIMIT/MARKET):")
        return ORDER_TYPE

    async def add_trade_order_type(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle order type input"""
        self.temp_trade_data['order_type'] = update.message.text.upper()
        await update.message.reply_text("Enter the leverage (e.g., 10):")
        return LEVERAGE

    async def add_trade_leverage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle leverage input"""
        self.temp_trade_data['leverage'] = int(update.message.text)
        await update.message.reply_text("Enter the direction (LONG/SHORT):")
        return DIRECTION

    async def add_trade_direction(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle direction input"""
        self.temp_trade_data['direction'] = update.message.text.upper()
        await update.message.reply_text("Enter the amount:")
        return AMOUNT

    async def add_trade_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle amount input"""
        self.temp_trade_data['amount'] = Decimal(update.message.text)
        await update.message.reply_text("Enter the price:")
        return PRICE

    async def add_trade_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle price input"""
        self.temp_trade_data['price'] = Decimal(update.message.text)
        await update.message.reply_text("Enter the fees:")
        return FEES

    async def add_trade_final(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle fees input and finalize trade"""
        self.temp_trade_data['fees'] = Decimal(update.message.text)
        await update.message.reply_text("Trade added successfully!")
        # Here you would add the trade to your database or trading system
        self.temp_trade_data.clear()
        return ConversationHandler.END

    async def add_trade_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel the add trade conversation"""
        self.temp_trade_data.clear()
        await update.message.reply_text("Trade addition cancelled.")
        return ConversationHandler.END

    async def add_trade_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start manual trade entry"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return ConversationHandler.END

        self.temp_trade_data.clear()
        self.temp_trade_data['is_manual'] = True
        
        await update.message.reply_text(
            "Enter the trading symbol (e.g., BTCUSDT):"
        )
        return SYMBOL

    async def add_trade_symbol(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle symbol input"""
        symbol = update.message.text.upper()
        
        # Validate symbol
        try:
            info = await self.bot.binance_client.get_symbol_info(symbol)
            if not info:
                await update.message.reply_text("❌ Invalid symbol. Please try again:")
                return SYMBOL
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}\nPlease try again:")
            return SYMBOL

        self.temp_trade_data['symbol'] = symbol
        
        # Ask for position type
        keyboard = [
            [KeyboardButton("SPOT"), KeyboardButton("FUTURES")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
        await update.message.reply_text(
            "Select position type:",
            reply_markup=reply_markup
        )
        return POSITION_TYPE

    async def add_trade_position_type(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle position type selection"""
        position_type = update.message.text.upper()
        if position_type not in ['SPOT', 'FUTURES']:
            await update.message.reply_text("❌ Invalid position type. Please select SPOT or FUTURES:")
            return POSITION_TYPE

        self.temp_trade_data['position_type'] = position_type
        
        if position_type == 'FUTURES':
            await update.message.reply_text(
                "Enter leverage (1-125):",
                reply_markup=ReplyKeyboardRemove()
            )
            return LEVERAGE
        else:
            await update.message.reply_text(
                "Enter investment amount in USDT:",
                reply_markup=ReplyKeyboardRemove()
            )
            return AMOUNT

    async def add_trade_leverage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle leverage input"""
        try:
            leverage = int(update.message.text)
            if not 1 <= leverage <= 125:
                raise ValueError("Leverage must be between 1 and 125")
            
            self.temp_trade_data['leverage'] = leverage
            
            # Ask for direction
            keyboard = [
                [KeyboardButton("LONG"), KeyboardButton("SHORT")]
            ]
            reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
            await update.message.reply_text(
                "Select position direction:",
                reply_markup=reply_markup
            )
            return DIRECTION
            
        except ValueError as e:
            await update.message.reply_text(f"❌ {str(e)}. Please try again:")
            return LEVERAGE

    async def add_trade_direction(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle direction input"""
        direction = update.message.text.upper()
        if direction not in ['LONG', 'SHORT']:
            await update.message.reply_text("❌ Invalid direction. Please select LONG or SHORT:")
            return DIRECTION

        self.temp_trade_data['direction'] = direction
        await update.message.reply_text(
            "Enter investment amount in USDT:",
            reply_markup=ReplyKeyboardRemove()
        )
        return AMOUNT

    async def add_trade_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle amount input"""
        try:
            amount = Decimal(update.message.text)
            if amount <= 0:
                raise ValueError("Amount must be positive")
            
            self.temp_trade_data['amount'] = amount
            await update.message.reply_text("Enter entry price:")
            return PRICE
            
        except (ValueError, DecimalException):
            await update.message.reply_text("❌ Invalid amount. Please enter a valid number:")
            return AMOUNT

    async def add_trade_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle price input"""
        try:
            price = Decimal(update.message.text)
            if price <= 0:
                raise ValueError("Price must be positive")
            
            self.temp_trade_data['price'] = price
            
            # Calculate quantity and fees
            amount = self.temp_trade_data['amount']
            quantity = amount / price
            
            fee_rate = self.FUTURES_FEE if self.temp_trade_data.get('position_type') == 'FUTURES' else self.SPOT_FEE
            fees = amount * fee_rate
            
            self.temp_trade_data['quantity'] = quantity
            self.temp_trade_data['fees'] = fees
            
            # Ask for TP
            keyboard = [[KeyboardButton("SKIP")]]
            reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
            await update.message.reply_text(
                "Enter Take Profit price (or SKIP):",
                reply_markup=reply_markup
            )
            return TP_PRICE
            
        except (ValueError, DecimalException):
            await update.message.reply_text("❌ Invalid price. Please enter a valid number:")
            return PRICE

    async def add_trade_tp(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle TP input"""
        if update.message.text.upper() != "SKIP":
            try:
                tp_price = Decimal(update.message.text)
                if tp_price <= 0:
                    raise ValueError("Price must be positive")
                self.temp_trade_data['tp_price'] = tp_price
            except (ValueError, DecimalException):
                await update.message.reply_text("❌ Invalid price. Please enter a valid number or SKIP:")
                return TP_PRICE

        # Ask for SL
        keyboard = [[KeyboardButton("SKIP")]]
        reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
        await update.message.reply_text(
            "Enter Stop Loss price (or SKIP):",
            reply_markup=reply_markup
        )
        return SL_PRICE

    async def add_trade_sl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle SL input"""
        if update.message.text.upper() != "SKIP":
            try:
                sl_price = Decimal(update.message.text)
                if sl_price <= 0:
                    raise ValueError("Price must be positive")
                self.temp_trade_data['sl_price'] = sl_price
            except (ValueError, DecimalException):
                await update.message.reply_text("❌ Invalid price. Please enter a valid number or SKIP:")
                return SL_PRICE

        # Show summary and confirm
        summary = self._generate_trade_summary()
        keyboard = [
            [KeyboardButton("CONFIRM"), KeyboardButton("CANCEL")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
        await update.message.reply_text(
            f"Review Trade:\n\n{summary}\n\nConfirm or Cancel?",
            reply_markup=reply_markup
        )
        return FEES

    def _validate_trade_input(self, trade_data: dict) -> Optional[str]:
        """Validate trade input data"""
        try:
            # Required fields validation
            required_fields = ['symbol', 'price', 'amount']
            missing = [f for f in required_fields if f not in trade_data]
            if missing:
                return f"Missing required fields: {', '.join(missing)}"

            # Symbol validation
            if not trade_data['symbol'].endswith('USDT'):
                return "Only USDT pairs are supported"

            # Amount validation
            amount = Decimal(str(trade_data['amount']))
            if amount <= 0:
                return "Amount must be positive"
            if amount < Decimal('5'):
                return "Minimum trade amount is 5 USDT"

            # Price validation
            price = Decimal(str(trade_data['price']))
            if price <= 0:
                return "Price must be positive"

            # Futures-specific validation
            if trade_data.get('position_type') == 'FUTURES':
                # Leverage validation
                leverage = int(trade_data.get('leverage', 1))
                if not 1 <= leverage <= 125:
                    return "Leverage must be between 1 and 125"

                # Direction validation
                if trade_data.get('direction') not in ['LONG', 'SHORT']:
                    return "Invalid direction for futures trade"

            # TP/SL validation
            if 'tp_price' in trade_data:
                tp_price = Decimal(str(trade_data['tp_price']))
                if tp_price <= 0:
                    return "Take profit price must be positive"

            if 'sl_price' in trade_data:
                sl_price = Decimal(str(trade_data['sl_price']))
                if sl_price <= 0:
                    return "Stop loss price must be positive"

            return None
        except (ValueError, DecimalException) as e:
            return f"Invalid numeric value: {str(e)}"

    async def _check_balance_and_limits(self, trade_data: dict) -> Optional[str]:
        """Check balance and trading limits"""
        try:
            # Get current balance
            balance = await self.bot.binance_client.get_balance()
            
            # Calculate required amount including fees
            amount = Decimal(str(trade_data['amount']))
            fee_rate = self.FUTURES_FEE if trade_data.get('position_type') == 'FUTURES' else self.SPOT_FEE
            total_required = amount * (1 + fee_rate)

            # Check sufficient balance
            if total_required > balance:
                return f"Insufficient balance. Required: ${float(total_required):.2f}, Available: ${float(balance):.2f}"

            # Check against reserve balance
            reserve = self.bot.binance_client.reserve_balance
            if (balance - total_required) < reserve:
                return f"Trade would break reserve balance of ${float(reserve):.2f}"

            return None
        except Exception as e:
            logger.error(f"Error checking balance and limits: {e}")
            return "Error checking trading limits"

    async def add_trade_final(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle trade confirmation with validation"""
        response = update.message.text.upper()
        if response == "CANCEL":
            self.temp_trade_data.clear()
            await update.message.reply_text(
                "Trade cancelled.",
                reply_markup=self.bot.markup
            )
            return ConversationHandler.END

        if response != "CONFIRM":
            await update.message.reply_text("Please select CONFIRM or CANCEL:")
            return FEES

        # Validate trade data
        validation_error = self._validate_trade_input(self.temp_trade_data)
        if validation_error:
            await update.message.reply_text(
                f"❌ Validation error: {validation_error}",
                reply_markup=self.bot.markup
            )
            self.temp_trade_data.clear()
            return ConversationHandler.END

        # Check balance and limits
        limit_error = await self._check_balance_and_limits(self.temp_trade_data)
        if limit_error:
            await update.message.reply_text(
                f"❌ {limit_error}",
                reply_markup=self.bot.markup
            )
            self.temp_trade_data.clear()
            return ConversationHandler.END

        try:
            # Create order with retry mechanism
            for attempt in range(3):
                try:
                    order = Order(
                        symbol=self.temp_trade_data['symbol'],
                        order_type=OrderType.FUTURES if self.temp_trade_data['position_type'] == 'FUTURES' else OrderType.SPOT,
                        price=self.temp_trade_data['price'],
                        quantity=self.temp_trade_data['quantity'],
                        order_id=f"MANUAL_{int(datetime.utcnow().timestamp())}",
                        created_at=datetime.utcnow(),
                        updated_at=datetime.utcnow(),
                        filled_at=datetime.utcnow(),
                        fees=self.temp_trade_data['fees'],
                        fee_asset='USDT',
                        status=OrderStatus.FILLED,
                        is_manual=True
                    )

                    # Add futures-specific fields
                    if self.temp_trade_data['position_type'] == 'FUTURES':
                        order.leverage = self.temp_trade_data['leverage']
                        order.direction = TradeDirection.LONG if self.temp_trade_data['direction'] == 'LONG' else TradeDirection.SHORT

                    # Add TP/SL
                    if 'tp_price' in self.temp_trade_data:
                        order.tp_price = self.temp_trade_data['tp_price']
                    if 'sl_price' in self.temp_trade_data:
                        order.sl_price = self.temp_trade_data['sl_price']

                    # Store with timeout
                    async with async_timeout.timeout(5):
                        result = await self.bot.mongo_client.insert_manual_trade(order)
                        if not result:
                            raise Exception("Failed to store trade")
                        break
                except asyncio.TimeoutError:
                    if attempt == 2:  # Last attempt
                        raise Exception("Database timeout")
                    continue

            await update.message.reply_text(
                "✅ Trade added successfully!",
                reply_markup=self.bot.markup
            )
            
            # Send confirmation ROAR
            await self.bot.automation_manager.send_roar(order)
            
            self.temp_trade_data.clear()
            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error adding manual trade: {e}")
            await update.message.reply_text(
                f"❌ Error adding trade: {str(e)}",
                reply_markup=self.bot.markup
            )
            self.temp_trade_data.clear()
            return ConversationHandler.END

    def _generate_trade_summary(self) -> str:
        """Generate trade summary text"""
        position_type = self.temp_trade_data['position_type']
        direction = self.temp_trade_data.get('direction', '')
        leverage = self.temp_trade_data.get('leverage', 1)
        
        summary = [
            f"Symbol: {self.temp_trade_data['symbol']}",
            f"Type: {position_type}",
            f"Entry Price: ${float(self.temp_trade_data['price']):.2f}",
            f"Amount: ${float(self.temp_trade_data['amount']):.2f}",
            f"Quantity: {float(self.temp_trade_data['quantity']):.8f}",
            f"Fees: ${float(self.temp_trade_data['fees']):.4f}"
        ]
        
        if position_type == 'FUTURES':
            summary.extend([
                f"Direction: {direction}",
                f"Leverage: {leverage}x"
            ])
            
        if 'tp_price' in self.temp_trade_data:
            summary.append(f"Take Profit: ${float(self.temp_trade_data['tp_price']):.2f}")
        if 'sl_price' in self.temp_trade_data:
            summary.append(f"Stop Loss: ${float(self.temp_trade_data['sl_price'])::.2f}")
            
        return "\n".join(summary)

class SettingsManager:
    """Manages settings-related commands"""
    def __init__(self, bot: TelegramBot):
        self.bot = bot
        self.settings_cache = {}
        self.setting_states = {}
        
        # Define setting categories and their options
        self.CATEGORIES = {
            'futures': {
                'default_leverage': ('number', 'Default leverage for new positions (1-125)'),
                'margin_type': ('select', 'Default margin type', ['ISOLATED', 'CROSSED']),
                'hedge_mode': ('bool', 'Enable hedge mode trading'),
                'tp_enabled': ('bool', 'Enable take profit orders'),
                'sl_enabled': ('bool', 'Enable stop loss orders'),
                'tp_percent': ('number', 'Default take profit percentage'),
                'sl_percent': ('number', 'Default stop loss percentage')
            },
            'general': {
                'reserve_balance': ('number', 'Minimum USDT balance to maintain'),
                'auto_cancel_hours': ('number', 'Hours before auto-canceling orders'),
                'notifications': ('bool', 'Enable trading notifications'),
                'weekly_summary': ('bool', 'Enable weekly performance summary')
            },
            'pairs': {
                'enabled_pairs': ('multiselect', 'Trading pairs to monitor', None)
            }
        }

    async def show_settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show main settings menu"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        # Get current settings
        settings = await self.bot.mongo_client.get_settings()
        self.settings_cache = settings

        keyboard = [
            [InlineKeyboardButton("⚙️ General Settings", callback_data="settings_general")],
            [InlineKeyboardButton("📈 Futures Settings", callback_data="settings_futures")],
            [InlineKeyboardButton("🔣 Trading Pairs", callback_data="settings_pairs")],
            [InlineKeyboardButton("« Back to Menu", callback_data="menu_main")]
        ]

        await update.message.reply_text(
            "🛠 Settings Menu\nSelect a category to configure:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def handle_settings_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle settings menu callbacks"""
        query = update.callback_query
        await query.answer()
        data = query.data.split('_')
        
        if len(data) < 2:
            return
            
        action = data[1]
        category = data[2] if len(data) > 2 else None
        setting = data[3] if len(data) > 3 else None

        if action == "menu":
            await self.show_category_menu(query, category)
        elif action == "edit":
            await self.start_edit_setting(query, category, setting)
        elif action == "save":
            await self.save_setting(query, category, setting)
        elif action == "cancel":
            await self.cancel_edit(query)

    async def show_category_menu(self, query: CallbackQuery, category: str):
        """Show settings for a specific category"""
        settings = self.settings_cache.get(category, {})
        category_settings = self.CATEGORIES[category]
        
        message_parts = [f"⚙️ {category.title()} Settings\n"]
        keyboard = []
        
        for key, (setting_type, description, *options) in category_settings.items():
            current_value = settings.get(key, "Not set")
            message_parts.append(f"\n{description}:")
            message_parts.append(f"Current: {current_value}")
            
            keyboard.append([
                InlineKeyboardButton(
                    f"Edit {key.replace('_', ' ').title()}", 
                    callback_data=f"settings_edit_{category}_{key}"
                )
            ])

        keyboard.append([InlineKeyboardButton("« Back", callback_data="settings_menu")])
        
        await query.edit_message_text(
            "\n".join(message_parts),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def start_edit_setting(self, query: CallbackQuery, category: str, key: str):
        """Start editing a setting"""
        setting_info = self.CATEGORIES[category][key]
        setting_type = setting_info[0]
        description = setting_info[1]
        
        if setting_type == "select":
            options = setting_info[2]
            keyboard = [
                [InlineKeyboardButton(opt, callback_data=f"settings_save_{category}_{key}_{opt}")]
                for opt in options
            ]
            keyboard.append([InlineKeyboardButton("Cancel", callback_data="settings_cancel")])
            
            await query.edit_message_text(
                f"Select {description}:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            # Store state for text input
            self.setting_states[query.from_user.id] = {
                "category": category,
                "key": key,
                "type": setting_type
            }
            
            instructions = {
                "number": "Enter a number",
                "bool": "Enter 'yes' or 'no'",
                "multiselect": "Enter comma-separated values"
            }
            
            await query.edit_message_text(
                f"Edit {description}\n"
                f"{instructions[setting_type]}:"
            )

    async def handle_setting_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text input for settings"""
        if update.effective_user.id not in self.setting_states:
            return
            
        state = self.setting_states[update.effective_user.id]
        value = update.message.text
        
        try:
            # Validate and convert input
            if state["type"] == "number":
                value = float(value)
            elif state["type"] == "bool":
                value = value.lower() in ("yes", "true", "1")
            elif state["type"] == "multiselect":
                value = [v.strip() for v in value.split(",")]
                
            # Save setting
            success = await self.bot.mongo_client.update_setting(
                state["category"],
                state["key"],
                value
            )
            
            if success:
                await update.message.reply_text(
                    f"✅ Setting updated successfully!\n"
                    f"Use /settings to continue configuration.",
                    reply_markup=self.bot.markup
                )
            else:
                await update.message.reply_text("❌ Failed to update setting")
                
        except ValueError:
            await update.message.reply_text("❌ Invalid input. Please try again.")
            return
            
        finally:
            # Clear state
            del self.setting_states[update.effective_user.id]

    async def save_setting(self, query: CallbackQuery, category: str, key: str):
        """Save a setting from callback data"""
        value = query.data.split('_')[-1]
        
        success = await self.bot.mongo_client.update_setting(category, key, value)
        
        if success:
            # Update cache
            if category not in self.settings_cache:
                self.settings_cache[category] = {}
            self.settings_cache[category][key] = value
            
            # Show category menu
            await self.show_category_menu(query, category)
        else:
            await query.edit_message_text("❌ Failed to update setting")

    async def cancel_edit(self, query: CallbackQuery):
        """Cancel setting edit"""
        if query.from_user.id in self.setting_states:
            del self.setting_states[query.from_user.id]
            
        category = self.setting_states.get(query.from_user.id, {}).get("category")
        if category:
            await self.show_category_menu(query, category)
        else:
            await self.show_settings_menu(query, None)

    async def switch_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Switch between spot and futures modes"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        keyboard = [
            [InlineKeyboardButton("SPOT", callback_data='switch_mode_spot')],
            [InlineKeyboardButton("FUTURES", callback_data='switch_mode_futures')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Select trading mode:", reply_markup=reply_markup)

    async def set_leverage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set leverage for futures trading"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        await update.message.reply_text("Enter the leverage (e.g., 10):")
        return LEVERAGE

    async def set_margin_type(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set margin type for futures trading"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        keyboard = [
            [InlineKeyboardButton("ISOLATED", callback_data='margin_isolated')],
            [InlineKeyboardButton("CROSSED", callback_data='margin_crossed')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Select margin type:", reply_markup=reply_markup)

    async def toggle_hedge_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Toggle hedge mode for futures trading"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        keyboard = [
            [InlineKeyboardButton("ONE-WAY", callback_data='hedge_one_way')],
            [InlineKeyboardButton("HEDGE", callback_data='hedge_hedge')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Select hedge mode:", reply_markup=reply_markup)

class PortfolioManager:
    """Manages portfolio-related commands"""
    def __init__(self, bot: TelegramBot):
        self.bot = bot
        self.visualizer = PortfolioVisualizer()

    async def show_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show current balance"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        try:
            current_mode = self.bot.config['environment']['trading_mode'].upper()
            response = [f"💰 Balance Overview ({current_mode} Mode)"]
            
            # Get balance based on mode
            if current_mode == "FUTURES":
                account = await self.bot.binance_client.get_account_info()
                
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
                spot_balance = await self.bot.binance_client.get_balance()
                response.extend([
                    f"\n💱 Spot Balance:",
                    f"• USDT: ${float(spot_balance):,.2f}"
                ])

            # Add reserve balance info
            response.extend([
                f"\n📝 Reserve Balance: ${self.bot.binance_client.reserve_balance:.2f}",
                f"Trading Status: {'Paused ⏸' if self.bot.is_paused else 'Active ▶️'}"
            ])
                
            await update.message.reply_text("\n".join(response))
            
        except Exception as e:
            logger.error(f"Error getting balance: {e}")
            await update.message.reply_text(f"❌ Error getting balance: {str(e)}")

    async def show_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show detailed portfolio analysis with visualizations"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        try:
            # Show loading message
            loading_msg = await update.message.reply_text(
                "📊 Generating portfolio analysis..."
            )

            # Get portfolio data
            history = await self.bot.mongo_client.get_portfolio_history()
            fee_metrics = await self.bot.mongo_client.get_fee_metrics()
            
            # Get current positions
            spot_value = await self.bot.binance_client.get_spot_portfolio_value()
            futures_value = await self.bot.binance_client.get_futures_portfolio_value()
            
            # Get recent orders
            recent_orders = await self.bot.mongo_client.get_recent_orders()

            # Generate visualizations
            timeline_img = self.visualizer.generate_portfolio_timeline(
                recent_orders, history
            )
            allocation_img = self.visualizer.generate_allocation_pie(
                spot_value, futures_value
            )
            fee_img = self.visualizer.generate_fee_metrics(fee_metrics)

            # Send overview message
            overview = (
                "📈 Portfolio Overview\n\n"
                f"Total Value: ${float(spot_value + futures_value):,.2f}\n"
                f"Spot Value: ${float(spot_value):,.2f}\n"
                f"Futures Value: ${float(futures_value):,.2f}\n"
                f"Total Fees: ${float(fee_metrics['total']):,.2f}\n\n"
                "Sending visualizations..."
            )
            await loading_msg.edit_text(overview)

            # Send visualizations
            if timeline_img:
                await update.message.reply_photo(
                    timeline_img,
                    caption="Portfolio Value Timeline"
                )
            
            if allocation_img:
                await update.message.reply_photo(
                    allocation_img,
                    caption="USDT Allocation"
                )
            
            if fee_img:
                await update.message.reply_photo(
                    fee_img,
                    caption="Fee Metrics"
                )

        except Exception as e:
            logger.error(f"Error showing portfolio: {e}")
            await update.message.reply_text(
                "❌ Error generating portfolio analysis"
            )

    async def show_viz_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show visualization menu"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        keyboard = [
            [InlineKeyboardButton("Daily Volume", callback_data=VisualizationType.DAILY_VOLUME)],
            [InlineKeyboardButton("Profit Distribution", callback_data=VisualizationType.PROFIT_DIST)],
            [InlineKeyboardButton("Order Types", callback_data=VisualizationType.ORDER_TYPES)],
            [InlineKeyboardButton("Hourly Activity", callback_data=VisualizationType.HOURLY_ACTIVITY)]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Select a visualization:", reply_markup=reply_markup)

    async def handle_viz_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle visualization selection"""
        query = update.callback_query
        await query.answer()
        viz_type = query.data

        if viz_type == VisualizationType.DAILY_VOLUME:
            await self.show_daily_volume(update, context)
        elif viz_type == VisualizationType.PROFIT_DIST:
            await self.show_profit_distribution(update, context)
        elif viz_type == VisualizationType.ORDER_TYPES:
            await self.show_order_types(update, context)
        elif viz_type == VisualizationType.HOURLY_ACTIVITY:
            await self.show_hourly_activity(update, context)

    async def show_daily_volume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show daily volume visualization"""
        # Implement your visualization logic here
        await update.message.reply_text("Daily Volume Visualization")

    async def show_profit_distribution(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show profit distribution visualization"""
        # Implement your visualization logic here
        await update.message.reply_text("Profit Distribution Visualization")

    async def show_order_types(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show order types visualization"""
        # Implement your visualization logic here
        await update.message.reply_text("Order Types Visualization")

    async def show_hourly_activity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show hourly activity visualization"""
        # Implement your visualization logic here
        await update.message.reply_text("Hourly Activity Visualization")

class AutomationManager:
    """Manages automation-related commands"""
    def __init__(self, bot: TelegramBot):
        self.bot = bot

    async def toggle_trading(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Toggle automated trading"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return
            
        # Check reserve balance before resuming
        if not self.bot.is_paused:
            current_balance = await self.bot.binance_client.get_balance('USDT')
            reserve_balance = self.bot.binance_client.reserve_balance or 0  # Default to 0 if None
            
            if float(current_balance) < reserve_balance:
                await update.message.reply_text(
                    "❌ Cannot resume trading: Balance below reserve requirement\n"
                    f"Current: ${float(current_balance):.2f}\n"
                    f"Required: ${reserve_balance:.2f}"
                )
                return
                
        # Toggle state
        self.bot.is_paused = not self.bot.is_paused
        
        # Create keyboard with current state
        status_keyboard = ReplyKeyboardMarkup(
            [
                [KeyboardButton("/balance"), KeyboardButton("/stats"), KeyboardButton("/profits")],
                [KeyboardButton("/power"), KeyboardButton("/add"), KeyboardButton("/thresholds")],
                [KeyboardButton("/history"), KeyboardButton("/viz"), KeyboardButton("/menu")]
            ],
            resize_keyboard=True
        )
        
        if self.bot.is_paused:
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

    async def show_weekly_summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Generate and show weekly trading summary"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        try:
            # Get weekly orders
            orders = await self.bot.mongo_client.get_weekly_orders()
            
            # Get triggered thresholds
            thresholds = await self.bot.mongo_client.get_weekly_triggered_thresholds()
            
            # Calculate statistics
            total_trades = len(orders)
            if total_trades == 0:
                await update.message.reply_text("No trading activity in the past week.")
                return

            # Calculate trading metrics
            total_volume = sum(order.price * order.quantity for order in orders)
            total_fees = sum(order.fees for order in orders)
            
            # Group orders by symbol
            symbol_stats = {}
            for order in orders:
                if order.symbol not in symbol_stats:
                    symbol_stats[order.symbol] = {
                        'count': 0,
                        'volume': Decimal('0'),
                        'thresholds_hit': 0
                    }
                stats = symbol_stats[order.symbol]
                stats['count'] += 1
                stats['volume'] += order.price * order.quantity

            # Count thresholds by symbol
            for threshold in thresholds:
                symbol = threshold['symbol']
                if symbol in symbol_stats:
                    symbol_stats[symbol]['thresholds_hit'] += 1

            # Generate summary message
            message_parts = [
                "📊 Weekly Trading Summary\n",
                f"Total Trades: {total_trades}",
                f"Total Volume: ${float(total_volume):,.2f}",
                f"Total Fees: ${float(total_fees):,.2f}\n",
                "Symbol Breakdown:"
            ]

            for symbol, stats in symbol_stats.items():
                message_parts.append(
                    f"\n{symbol}:"
                    f"\n• Trades: {stats['count']}"
                    f"\n• Volume: ${float(stats['volume']):,.2f}"
                    f"\n• Thresholds Hit: {stats['thresholds_hit']}"
                )

            # Send summary
            await update.message.reply_text(
                "\n".join(message_parts),
                reply_markup=self.bot.markup
            )

        except Exception as e:
            logger.error(f"Error generating weekly summary: {e}")
            await update.message.reply_text("❌ Error generating weekly summary")

    async def send_roar(self, order: Order):
        """Send trade notification with advanced chart generation"""
        # Add order ID to sent roars set
        self.bot.sent_roars.add(order.order_id)
        
        try:
            # Try to generate chart first
            chart_data = await self.bot.binance_client.generate_trade_chart(order)
            
            # Create detailed caption with environment info
            caption = (
                f"{self.bot.env_info}\n\n"
                f"🦖 ROARRR! Trade Complete! 💥\n\n"
                f"Order ID: {order.order_id}\n"
                f"Symbol: {order.symbol}\n"
                f"Amount: {float(order.quantity):.8f} {order.symbol.replace('USDT', '')}\n"
                f"Price: ${float(order.price):.2f}\n"
                f"Total: ${float(order.price * order.quantity):.2f} USDT\n"
                f"Fees: ${float(order.fees)::.4f} {order.fee_asset}\n"
                f"Threshold: {order.threshold if order.threshold else 'Manual'}\n"
                f"Timeframe: {self._get_timeframe_value(order.timeframe)}\n\n"
                f"Check /profits to see your updated portfolio."
            )
            
            # Send notification with or without chart
            for user_id in self.bot.allowed_users:
                try:
                    if chart_data:
                        await self.bot.app.bot.send_photo(
                            chat_id=user_id,
                            photo=chart_data,
                            caption=caption
                        )
                    else:
                        await self.bot.app.bot.send_message(
                            chat_id=user_id,
                            text=f"{caption}\n\n⚠️ Chart not available: Insufficient candle data"
                        )
                except Exception as e:
                    logger.error(f"Failed to send roar to {user_id}: {e}")
                    try:
                        await self.bot.app.bot.send_message(
                            chat_id=user_id,
                            text=caption
                        )
                    except Exception as e2:
                        logger.error(f"Failed to send fallback message: {e2}")

    async def send_timeframe_reset_notification(self, reset_data: dict):
        """Send notification when a timeframe resets"""
        try:
            timeframe = reset_data["timeframe"]
            emoji_map = {
                TimeFrame.DAILY: "📅",
                TimeFrame.WEEKLY: "📆",
                TimeFrame.MONTHLY: "📊"
            }
            
            message_parts = [
                f"{self.bot.env_info}\n",
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
                
                price_info = (
                    f"\n{symbol}:"
                    f"\n• Previous Open: ${reference:,.2f}"
                    f"\n• Current Price: ${current:,.2f}"
                    f"\n• Change: {change:+.2f}%"
                )
                message_parts.append(price_info)
            
            message_parts.append(f"\n\nAll {timeframe.value} thresholds have been reset.")
            message_parts.append("\nUse /thresholds to see new tracking status.")
            
            # Join all message parts
            final_message = "\n".join(message_parts)
            
            # Send to all authorized users
            for user_id in self.bot.allowed_users:
                try:
                    await self.bot.app.bot.send_message(
                        chat_id=user_id,
                        text=final_message,
                        reply_markup=self.bot.markup
                    )
                except Exception as e:
                    logger.error(f"Failed to send reset notification to {user_id}: {e}")
        except Exception as e:
            logger.error(f"Failed to send timeframe reset notification: {e}")

    async def generate_weekly_summary(self):
        """Generate and send comprehensive weekly summary"""
        try:
            # Get weekly data
            week_ago = datetime.utcnow() - timedelta(days=7)
            trades = await self.bot.mongo_client.get_weekly_orders()
            thresholds = await self.bot.mongo_client.get_weekly_triggered_thresholds()
            
            # Generate summary sections
            transaction_summary = await self._generate_transaction_summary(trades)
            threshold_summary = await self._generate_threshold_summary(thresholds)
            pair_analysis = await self._generate_pair_analysis(trades)
            pnl_summary = await self._generate_pnl_summary(trades)
            equity_report = await self._generate_equity_report()
            
            # Combine all sections
            message_parts = [
                "🦖 Weekly Trading Summary",
                f"Period: {week_ago.strftime('%Y-%m-%d')} to {datetime.utcnow().strftime('%Y-%m-%d')}\n",
                transaction_summary,
                threshold_summary,
                pair_analysis,
                pnl_summary,
                equity_report
            ]
            
            message = "\n\n".join(filter(None, message_parts))
            
            # Send to all authorized users
            for user_id in self.bot.allowed_users:
                try:
                    await self.bot.app.bot.send_message(
                        chat_id=user_id,
                        text=message,
                        parse_mode='HTML'
                    )
                except Exception as e:
                    logger.error(f"Failed to send weekly summary to {user_id}: {e}")
                    
        except Exception as e:
            logger.error(f"Error generating weekly summary: {e}")
            
    async def _generate_transaction_summary(self, trades: List[Order]) -> str:
        """Generate transaction overview"""
        if not trades:
            return "📊 Transactions:\nNo trades this week"
            
        spot_trades = [t for t in trades if t.order_type == OrderType.SPOT]
        futures_trades = [t for t in trades if t.order_type == OrderType.FUTURES]
        
        summary = ["📊 Transaction Overview:"]
        
        if spot_trades:
            total_spot_volume = sum(t.price * t.quantity for t in spot_trades)
            summary.extend([
                "\nSpot Trades:",
                f"• Count: {len(spot_trades)}",
                f"• Volume: ${float(total_spot_volume):,.2f}"
            ])
            
        if futures_trades:
            total_futures_volume = sum(t.price * t.quantity for t in futures_trades)
            long_trades = sum(1 for t in futures_trades if t.direction == TradeDirection.LONG)
            short_trades = len(futures_trades) - long_trades
            
            summary.extend([
                "\nFutures Trades:",
                f"• Count: {len(futures_trades)}",
                f"• Volume: ${float(total_futures_volume):,.2f}",
                f"• Long/Short: {long_trades}/{short_trades}"
            ])
            
        return "\n".join(summary)
        
    async def _generate_threshold_summary(self, thresholds: List[Dict]) -> str:
        """Generate threshold adjustment summary"""
        if not thresholds:
            return "🎯 Thresholds:\nNo threshold triggers this week"
            
        # Group by symbol
        threshold_map = {}
        for t in thresholds:
            symbol = t['symbol']
            if symbol not in threshold_map:
                threshold_map[symbol] = []
            threshold_map[symbol].append(t)
            
        summary = ["🎯 Threshold Activity:"]
        
        for symbol, triggers in threshold_map.items():
            triggered_values = [f"{t['threshold']}%" for t in triggers]
            summary.extend([
                f"\n{symbol}:",
                f"• Triggers: {len(triggers)}",
                f"• Values: {', '.join(triggered_values)}"
            ])
            
        return "\n.join(summary)
        
    async def _generate_pair_analysis(self, trades: List[Order]) -> str:
        """Generate trading pair analysis"""
        if not trades:
            return "📈 Pairs:\nNo trading activity this week"
            
        # Group by symbol
        pair_stats = {}
        for trade in trades:
            if trade.symbol not in pair_stats:
                pair_stats[trade.symbol] = {
                    'count': 0,
                    'volume': Decimal('0'),
                    'fees': Decimal('0'),
                    'pnl': Decimal('0')
                }
                
            stats = pair_stats[trade.symbol]
            stats['count'] += 1
            stats['volume'] += trade.price * trade.quantity
            stats['fees'] += trade.fees
            if hasattr(trade, 'realized_pnl'):
                stats['pnl'] += Decimal(str(trade.realized_pnl))
                
        summary = ["📈 Pair Performance:"]
        
        for symbol, stats in pair_stats.items():
            summary.extend([
                f"\n{symbol}:",
                f"• Trades: {stats['count']}",
                f"• Volume: ${float(stats['volume']):,.2f}",
                f"• Fees: ${float(stats['fees']):,.2f}",
                f"• P/L: ${float(stats['pnl']):+,.2f}"
            ])
            
        return "\n".join(summary)
        
    async def _generate_pnl_summary(self, trades: List[Order]) -> str:
        """Generate profit/loss summary"""
        if not trades:
            return "💰 P/L:\nNo profit/loss data this week"
            
        total_pnl = sum(Decimal(str(t.realized_pnl)) for t in trades if hasattr(t, 'realized_pnl'))
        total_fees = sum(t.fees for t in trades)
        net_profit = total_pnl - total_fees
        
        profitable_trades = sum(1 for t in trades if hasattr(t, 'realized_pnl') and t.realized_pnl > 0)
        win_rate = (profitable_trades / len(trades) * 100) if trades else 0
        
        summary = [
            "💰 Profit/Loss Summary:",
            f"• Gross P/L: ${float(total_pnl):+,.2f}",
            f"• Total Fees: ${float(total_fees):,.2f}",
            f"• Net Profit: ${float(net_profit):+,.2f}",
            f"• Win Rate: {win_rate:.1f}%"
        ]
        
        return "\n".join(summary)
        
    async def _generate_equity_report(self) -> str:
        """Generate equity distribution report"""
        try:
            spot_value = await self.bot.binance_client.get_spot_portfolio_value()
            futures_value = await self.bot.binance_client.get_futures_portfolio_value()
            total_value = spot_value + futures_value
            
            if total_value == 0:
                return "📊 Equity:\nNo equity data available"
            
            spot_percentage = (spot_value / total_value * 100)
            futures_percentage = (futures_value / total_value * 100)
            
            summary = [
                "📊 Equity Distribution:",
                f"• Total Value: ${float(total_value):,.2f}",
                f"• Spot: ${float(spot_value):,.2f} ({spot_percentage:.1f}%)",
                f"• Futures: ${float(futures_value):,.2f} ({futures_percentage:.1f}%)"
            ]
            
            return "\n.join(summary)
            
        except Exception as e:
            logger.error(f"Error generating equity report: {e}")
            return "📊 Equity:\nError generating equity report"

class MenuManager:
    """Manages menu-related functionality"""
    def __init__(self, bot: TelegramBot):
        self.bot = bot

    async def show_main_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show main menu"""
        if not self.bot._is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Unauthorized access")
            return

        keyboard = [
            [InlineKeyboardButton("Account Info", callback_data='menu_account')],
            [InlineKeyboardButton("Trading", callback_data='menu_trading')],
            [InlineKeyboardButton("Analysis", callback_data='menu_analysis')],
            [InlineKeyboardButton("Settings", callback_data='menu_settings')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Main Menu:", reply_markup=reply_markup)

    async def handle_menu_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle main menu callbacks"""
        query = update.callback_query
        await query.answer()
        menu_type = query.data.split('_')[1]
        await self.show_submenu(query, menu_type)

    async def show_submenu(self, query, menu_type):
        """Show submenu based on menu type"""
        if menu_type == 'account':
            keyboard = [
                [InlineKeyboardButton("Balance", callback_data='submenu_balance')],
                [InlineKeyboardButton("Positions", callback_data='submenu_positions')],
                [InlineKeyboardButton("Stats", callback_data='submenu_stats')],
                [InlineKeyboardButton("Profits", callback_data='submenu_profits')]
            ]
        elif menu_type == 'trading':
            keyboard = [
                [InlineKeyboardButton("Add Trade", callback_data='submenu_add_trade')],
                [InlineKeyboardButton("Toggle Trading", callback_data='submenu_toggle_trading')],
                [InlineKeyboardButton("Order History", callback_data='submenu_order_history')]
            ]
        elif menu_type == 'analysis':
            keyboard = [
                [InlineKeyboardButton("Visualizations", callback_data='submenu_viz')],
                [InlineKeyboardButton("Thresholds", callback_data='submenu_thresholds')]
            ]
        elif menu_type == 'settings':
            keyboard = [
                [InlineKeyboardButton("Leverage", callback_data='submenu_leverage')],
                [InlineKeyboardButton("Margin Type", callback_data='submenu_margin')],
                [InlineKeyboardButton("Hedge Mode", callback_data='submenu_hedge')]
            ]
        else:
            return

        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"{menu_type.capitalize()} Menu:", reply_markup=reply_markup)

    async def handle_submenu_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle submenu callbacks"""
        query = update.callback_query
        await query.answer()
        submenu_type = query.data.split('_')[1]

        if submenu_type == 'balance':
            await self.bot.portfolio_manager.show_balance(update, context)
        elif submenu_type == 'positions':
            await self.bot.trade_manager.show_positions(update, context)
        elif submenu_type == 'stats':
            await self.bot.trade_manager.show_stats(update, context)
        elif submenu_type == 'profits':
            await self.bot.trade_manager.show_profits(update, context)
        elif submenu_type == 'add_trade':
            await self.bot.trade_manager.add_trade_start(update, context)
        elif submenu_type == 'toggle_trading':
            await self.bot.automation_manager.toggle_trading(update, context)
        elif submenu_type == 'order_history':
            await self.bot.trade_manager.show_history(update, context)
        elif submenu_type == 'viz':
            await self.bot.portfolio_manager.show_viz_menu(update, context)
        elif submenu_type == 'thresholds':
            await self.bot.trade_manager.show_thresholds(update, context)
        elif submenu_type == 'leverage':
            await self.bot.settings_manager.set_leverage(update, context)
        elif submenu_type == 'margin':
            await self.bot.settings_manager.set_margin_type(update, context)
        elif submenu_type == 'hedge':
            await self.bot.settings_manager.toggle_hedge_mode(update, context)

async def main():
    """Main function"""
    try:
        # Load configuration
        config = load_and_merge_config()
        
        # Create and run bot
        bot = await TelegramBot.create(config)
        await bot.run()

    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt, shutting down...")
    except Exception as e:
        logger.critical(f"Fatal error in main: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())