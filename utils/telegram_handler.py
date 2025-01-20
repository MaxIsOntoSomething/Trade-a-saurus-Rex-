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
                BotCommand("trades", "Show all trades with profit/loss after tax"),
                BotCommand("profits", "Show current profits"),
                BotCommand("stats", "Show system stats and bot information"),
                BotCommand("distribution", "Show entry price distribution"),
                BotCommand("stacking", "Show position building over time"),
                BotCommand("buytimes", "Show time between buys"),
                BotCommand("portfolio", "Show portfolio value evolution"),
                BotCommand("allocation", "Show asset allocation"),
                BotCommand("orders", "Show open limit orders"),
                BotCommand("symbol", "Show detailed stats for a symbol including tax")
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
            "orders": self.handle_orders,
            "trade": self.handle_trade,
            "trades": self.handle_trades_list,  # New command to list all trades
            "symbol": self.handle_symbol_stats,  # Add new handler
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
            "/trades - List all trades with P/L after tax\n"
            "/symbol <SYMBOL> - Show detailed symbol stats with tax\n"
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
            "Tax Rate: 28%\n"
            "Bot is actively monitoring markets! 🚀"
        )
        await self.send_message(welcome_msg)

    async def handle_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show trading positions"""
        try:
            positions = []
            for symbol in self.bot.valid_symbols:
                price = self.bot.ws_manager.last_prices.get(symbol, {}).get('price', 0)
                positions.append(f"📊 {symbol}: {price:.8f}")
            
            message = "🎯 Available Trading Positions:\n\n" + "\n".join(positions)
            await self.send_message(message)
        except Exception as e:
            await self.send_message(f"❌ Error fetching positions: {e}")

    async def handle_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show balance info"""
        try:
            balance = self.bot.get_balance()
            if balance:
                # Filter and format significant balances
                significant_balances = []
                
                # Always show USDT first
                if 'USDT' in balance:
                    significant_balances.append(f"USDT: {balance['USDT']['total']:.2f}")
                
                # Add other significant balances
                for asset, details in balance.items():
                    if asset != 'USDT' and details['total'] > 0:
                        # Format with appropriate precision
                        if details['total'] < 1:
                            significant_balances.append(f"{asset}: {details['total']:.8f}")
                        else:
                            significant_balances.append(f"{asset}: {details['total']:.3f}")
                
                message = "💰 Current Balance:\n\n" + "\n".join(significant_balances)
                await self.send_message(message)
            else:
                await self.send_message("❌ Error fetching balance")
        except Exception as e:
            await self.send_message(f"❌ Error: {e}")

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
        try:
            message = "📋 Open Orders:\n\n"
            for order_id, order in self.bot.pending_orders.items():
                cancel_time = datetime.fromisoformat(order['cancel_time'])
                message += (f"ID: {order_id}\n"
                          f"Symbol: {order['symbol']}\n"
                          f"Price: {order['price']} USDT\n"
                          f"Quantity: {order['quantity']}\n"
                          f"Cancels at: {cancel_time.strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n")
            
            if not self.bot.pending_orders:
                message += "No open orders"
                
            await self.send_message(message)
        except Exception as e:
            await self.send_message(f"❌ Error fetching orders: {e}")

    async def handle_trade(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show specific trade details"""
        try:
            # Check if trade ID was provided
            if not context.args or len(context.args) != 1:
                await self.send_message("❌ Please provide a trade ID\nExample: /trade BOT_20250120232418_SOLUSDT_1")
                return

            trade_id = context.args[0]
            trade = await self.bot.get_trade_profit(trade_id)
            
            if not trade:
                await self.send_message(f"❌ Trade not found: {trade_id}")
                return
            
            # Format trade details
            message = (
                f"📊 Trade Details [ID: {trade_id}]\n\n"
                f"Symbol: {trade['symbol']}\n"
                f"Entry Price: {trade['entry_price']:.8f} USDT\n"
                f"Quantity: {trade['quantity']:.8f}\n"
                f"Total Cost: {trade['total_cost']:.2f} USDT\n\n"
                f"Current Value: {trade['current_value']:.2f} USDT\n"
                f"Last Price: {trade['last_price']:.8f} USDT\n"
                f"Profit/Loss: {trade['profit_usdt']:+.2f} USDT ({trade['profit_percentage']:+.2f}%)\n"
                f"Status: {trade['status']}\n"
                f"Filled: {trade['filled_time']}\n"
                f"Last Update: {trade['last_update']}"
            )
            
            await self.send_message(message)
            
        except Exception as e:
            await self.send_message(f"❌ Error fetching trade: {e}")

    async def handle_trades_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show list of all trades with tax calculations"""
        try:
            if not self.bot.trades:
                await self.send_message("No trades found")
                return

            # Group trades by symbol
            trades_by_symbol = {}
            for trade_id, trade in self.bot.trades.items():
                symbol = trade['symbol']
                if symbol not in trades_by_symbol:
                    trades_by_symbol[symbol] = []
                trades_by_symbol[symbol].append(trade_id)

            message = "📈 Trading History by Symbol:\n\n"
            
            # Process each symbol
            for symbol in sorted(trades_by_symbol.keys()):
                stats = await self.bot.get_symbol_stats(symbol)
                if stats:
                    profit_color = "🟢" if stats['net_profit_usdt'] >= 0 else "🔴"
                    message += (
                        f"{profit_color} {symbol}\n"
                        f"   Trades: {stats['number_of_trades']}\n"
                        f"   Avg Entry: {stats['average_price']:.8f}\n"
                        f"   Net P/L: {stats['net_profit_usdt']:+.2f} USDT "
                        f"({stats['net_profit_percentage']:+.2f}%) after tax\n\n"
                    )

            message += "\nUse /symbol <SYMBOL> for detailed statistics"
            await self.send_message(message)
            
        except Exception as e:
            await self.send_message(f"❌ Error fetching trades: {e}")

    async def handle_symbol_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show symbol statistics including tax calculations"""
        try:
            if not context.args or len(context.args) != 1:
                await self.send_message("❌ Please provide a symbol\nExample: /symbol BTCUSDT")
                return

            symbol = context.args[0].upper()
            if symbol not in self.bot.valid_symbols:
                await self.send_message(f"❌ Invalid symbol: {symbol}")
                return

            stats = await self.bot.get_symbol_stats(symbol)
            if not stats:
                await self.send_message(f"No trades found for {symbol}")
                return

            # Format message with detailed statistics
            message = (
                f"📊 {symbol} Trading Summary\n\n"
                f"Position Size: {stats['total_quantity']:.8f}\n"
                f"Total Cost: {stats['total_cost']:.2f} USDT\n"
                f"Average Entry: {stats['average_price']:.8f} USDT\n\n"
                f"Current Price: {stats['current_price']:.8f} USDT\n"
                f"Current Value: {stats['current_value']:.2f} USDT\n\n"
                f"Gross P/L: {stats['gross_profit_usdt']:+.2f} USDT "
                f"({stats['gross_profit_percentage']:+.2f}%)\n"
                f"Tax (28%): {stats['tax_amount']:.2f} USDT\n"
                f"Net P/L: {stats['net_profit_usdt']:+.2f} USDT "
                f"({stats['net_profit_percentage']:+.2f}%)\n\n"
                f"Number of Trades: {stats['number_of_trades']}\n"
                f"Last Update: {stats['last_update']}"
            )

            await self.send_message(message)

        except Exception as e:
            await self.send_message(f"❌ Error getting symbol stats: {e}")

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
