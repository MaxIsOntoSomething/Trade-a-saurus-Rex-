from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import BotCommand, Update
import asyncio
from colorama import Fore
import logging
from datetime import datetime  # Add this import

class TelegramHandler:
    def __init__(self, token, chat_id, bot_instance):
        self.token = token
        self.chat_id = chat_id
        self.bot = bot_instance  # Reference to main bot for accessing data
        self.app = Application.builder().token(token).build()
        self.commands_setup = False
        self.logger = logging.getLogger(__name__)

    async def initialize(self):
        """Initialize Telegram bot and set up commands"""
        if self.commands_setup:
            return

        try:
            # Test connection before proceeding
            await self.app.initialize()
            await self.app.bot.get_me()  # This will fail if token is invalid
            
            commands = [
                BotCommand("start", "Show available commands and bot status"),
                BotCommand("positions", "Show available trading opportunities"),
                BotCommand("balance", "Show current balance"),
                BotCommand("trades", "Show total number of trades"),
                BotCommand("profits", "Show current profits"),
                BotCommand("stats", "Show system stats and bot information"),
                BotCommand("distribution", "Show entry price distribution"),
                BotCommand("stacking", "Show position building over time"),
                BotCommand("buytimes", "Show time between buys"),
                BotCommand("portfolio", "Show portfolio value evolution"),
                BotCommand("allocation", "Show asset allocation"),
                BotCommand("orders", "Show open limit orders")
            ]

            # Register command handlers
            self.register_handlers()
            
            # Set up commands
            await self.app.bot.set_my_commands(commands)
            
            # Start the bot
            await self.app.start()
            await self.app.updater.start_polling(
                allowed_updates=["message"],
                drop_pending_updates=True
            )
            
            print(f"{Fore.GREEN}Telegram bot started successfully!")
            self.logger.info("Telegram bot started successfully!")
            self.commands_setup = True
            
        except Exception as e:
            self.logger.error(f"Failed to initialize Telegram: {e}")
            print(f"{Fore.RED}Failed to initialize Telegram: {e}")
            # Return False to indicate initialization failed
            return False
            
        return True

    def register_handlers(self):
        """Register all command handlers"""
        handlers = {
            "start": self.handle_start,
            "positions": self.handle_positions,
            "balance": self.handle_balance,
            "trades": self.handle_trades,
            "profits": self.handle_profits,
            "stats": self.handle_stats,
            "distribution": self.handle_distribution,
            "stacking": self.handle_stacking,
            "buytimes": self.handle_buy_times,
            "portfolio": self.handle_portfolio,
            "allocation": self.handle_allocation,
            "orders": self.handle_orders
        }

        for command, handler in handlers.items():
            self.app.add_handler(CommandHandler(command, handler))

    async def send_message(self, text, parse_mode=None, reply_markup=None):
        """Safely send messages with retry logic"""
        max_retries = 3
        retry_delay = 2
        
        for attempt in range(max_retries):
            try:
                if len(text) > 4000:
                    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
                    responses = []
                    for chunk in chunks:
                        response = await self.app.bot.send_message(
                            chat_id=self.chat_id,
                            text=chunk,
                            parse_mode=parse_mode,
                            reply_markup=reply_markup,
                            read_timeout=30,
                            connect_timeout=30,
                            write_timeout=30,
                            pool_timeout=30
                        )
                        responses.append(response)
                    return responses[-1]
                else:
                    return await self.app.bot.send_message(
                        chat_id=self.chat_id,
                        text=text,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                        read_timeout=30,
                        connect_timeout=30,
                        write_timeout=30,
                        pool_timeout=30
                    )
            except Exception as e:
                if attempt == max_retries - 1:
                    self.logger.error(f"Failed to send message after {max_retries} attempts: {e}")
                    raise
                await asyncio.sleep(retry_delay * (attempt + 1))

    async def handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        welcome_msg = (
            "🤖 Binance Trading Bot\n\n"
            "Available Commands:\n"
            "📊 Market Analysis:\n"
            "/positions - Show available trade opportunities\n"
            "/orders - Show open limit orders with cancel times\n\n"
            "💰 Portfolio & Trading:\n"
            "/balance - Show current balance\n"
            "/trades - Show total number of trades\n"
            "/profits - Show current profits\n"
            "/portfolio - Show portfolio value evolution\n"
            "/allocation - Show asset allocation\n\n"
            "📈 Analytics:\n"
            "/distribution - Show entry price distribution\n"
            "/stacking - Show position building over time\n"
            "/buytimes - Show time between buys\n\n"
            "ℹ️ System:\n"
            "/stats - Show system stats and bot information\n\n"
            "🔄 Trading Status:\n"
            f"Mode: {'Testnet' if self.bot.client.API_URL == 'https://testnet.binance.vision/api' else 'Live'}\n"
            f"Order Type: {self.bot.order_type.capitalize()}\n"
            f"USDT Reserve: {self.bot.reserve_balance_usdt}\n"
            "Bot is actively monitoring markets! 🚀"
        )
        await self.send_message(welcome_msg)

    async def handle_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show trading positions"""
        message = "🎯 Available Trading Positions:\n\n"
        for symbol in self.bot.valid_symbols:
            current_price = await self.bot.get_cached_price(symbol)
            message += f"📊 {symbol}: {current_price}\n"
        await self.send_message(message)

    async def handle_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show balance info"""
        balance = await asyncio.to_thread(self.bot.get_balance)
        if balance:
            message = "💰 Current Balance:\n\n"
            for asset, details in balance.items():
                message += f"{asset}: {details['total']:.8f}\n"
            await self.send_message(message)
        else:
            await self.send_message("❌ Error fetching balance")

    async def handle_trades(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show total trades"""
        await self.send_message(f"Total trades: {self.bot.total_trades}")

    async def handle_profits(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show profits"""
        message = "📈 Profit Summary:\n"
        # Add profit calculation logic here
        await self.send_message(message)

    async def handle_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show system stats"""
        stats = f"🤖 Bot Statistics:\n\nUptime: {datetime.now() - self.bot.start_time}\n"
        await self.send_message(stats)

    async def handle_distribution(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show price distribution"""
        await self.send_message("📊 Price distribution analysis coming soon")

    async def handle_stacking(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show position stacking"""
        await self.send_message("📈 Position stacking analysis coming soon")

    async def handle_buy_times(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show buy times analysis"""
        await self.send_message("⏰ Buy times analysis coming soon")

    async def handle_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show portfolio evolution"""
        await self.send_message("💼 Portfolio evolution coming soon")

    async def handle_allocation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show asset allocation"""
        await self.send_message("📊 Asset allocation analysis coming soon")

    async def handle_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show open orders"""
        message = "📋 Open Orders:\n\n"
        # Add open orders logic here
        await self.send_message(message)

    async def shutdown(self):
        """Safely shutdown Telegram bot"""
        try:
            if hasattr(self.app, 'updater'):
                if getattr(self.app.updater, '_running', False):
                    await self.app.updater.stop()
            if getattr(self.app, 'running', False):
                await self.app.stop()
            print(f"{Fore.GREEN}Telegram bot stopped successfully")
        except Exception as e:
            print(f"{Fore.YELLOW}Note: Telegram was already stopped or not running")
            self.logger.info("Telegram was already stopped or not running")
