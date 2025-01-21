from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram import BotCommand, Update
import asyncio
from colorama import Fore
import logging
from datetime import datetime, timezone, timedelta  # Add timedelta to imports

class TelegramHandler:
    def __init__(self, token, chat_id, bot_instance):
        self.token = token
        self.chat_id = chat_id
        self.bot = bot_instance  # Reference to main bot for accessing data
        self.app = Application.builder().token(token).build()
        self.commands_setup = False
        self.logger = logging.getLogger(__name__)
        self.trade_conv_state = {}  # Add this to track conversation states
        self.command_lock = asyncio.Lock()  # Add lock for commands
        self.command_timeout = 30  # Timeout for commands in seconds
        self.processing_commands = set()  # Track processing commands

    async def send_startup_notification(self):
        """Send comprehensive startup notification"""
        try:
            startup_msg = (
                "🤖 Binance Trading Bot Started!\n\n"
                "📈 Trading Configuration:\n"
                f"• Mode: {'Testnet' if self.bot.client.API_URL == 'https://testnet.binance.vision/api' else 'Live'}\n"
                f"• Order Type: {self.bot.order_type.capitalize()}\n"
                f"• Trading Pairs: {', '.join(self.bot.valid_symbols)}\n"
                f"• USDT Reserve: {self.bot.reserve_balance_usdt}\n"
                f"• Tax Rate: 28%\n\n"
                "📊 Available Commands:\n\n"
                "Market Analysis:\n"
                "/positions - Show current prices and opportunities\n"
                "/orders - Show open limit orders\n\n"
                "Portfolio Management:\n"
                "/balance - Show current balance\n"
                "/trades - List all trades with P/L\n"
                "/addtrade - Add manual trade\n"
                "/symbol <SYMBOL> - Show detailed stats\n"
                "/summary - Show portfolio summary\n\n"
                "Analytics:\n"
                "/profits - Show current profits\n"
                "/distribution - Show entry distribution\n"
                "/stacking - Show position building\n"
                "/buytimes - Show trade timing\n"
                "/portfolio - Show value evolution\n"
                "/allocation - Show asset allocation\n\n"
                "System:\n"
                "/stats - Show system information\n\n"
                "🟢 Bot is actively monitoring markets!"
            )
            
            await self.send_message(startup_msg)
            
        except Exception as e:
            self.logger.error(f"Error sending startup notification: {e}")

    async def initialize(self):
        """Initialize Telegram bot with improved error handling"""
        try:
            if self.commands_setup:
                return True

            await self.app.initialize()
            test_response = await self.app.bot.get_me()
            if not test_response:
                raise Exception("Failed to connect to Telegram")

            # Set up command handlers with timeouts
            self.register_handlers()
            
            # Start bot with proper error handling
            await self.app.start()
            await self.app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
                read_timeout=30,
                write_timeout=30,
                connect_timeout=30,
                pool_timeout=30
            )

            # Send startup notification with retry
            retry_count = 3
            for attempt in range(retry_count):
                try:
                    await self.send_startup_notification()
                    break
                except Exception as e:
                    if attempt == retry_count - 1:
                        raise
                    await asyncio.sleep(1)

            self.commands_setup = True
            return True

        except Exception as e:
            self.logger.error(f"Telegram initialization failed: {e}")
            return False

    async def handle_command_wrapper(self, handler, update, context):
        """Wrapper to handle commands with timeout and cleanup"""
        command = update.message.text.split()[0][1:]  # Extract command name
        if command in self.processing_commands:
            await self.send_message("Command already processing, please wait...")
            return

        try:
            self.processing_commands.add(command)
            async with self.command_lock:
                # Send typing action
                await self.app.bot.send_chat_action(
                    chat_id=update.effective_chat.id,
                    action="typing"
                )
                
                # Execute command with timeout
                try:
                    await asyncio.wait_for(
                        handler(update, context),
                        timeout=self.command_timeout
                    )
                except asyncio.TimeoutError:
                    await self.send_message(
                        f"Command {command} timed out. Please try again."
                    )
                
        except Exception as e:
            self.logger.error(f"Error in command {command}: {e}")
            await self.send_message(
                f"Error processing command {command}. Please try again."
            )
        finally:
            self.processing_commands.discard(command)

    def register_handlers(self):
        """Register command handlers with wrapper"""
        handlers = {
            "start": self.handle_start,
            "positions": self.handle_positions,
            "balance": self.handle_balance,
            "profits": self.handle_profits,
            "stats": self.handle_stats,
            "distribution": self.handle_distribution,
            "stacking": self.handle_stacking,
            "buytimes": self.handle_buy_times,
            "portfolio": self.handle_portfolio,
            "allocation": self.handle_allocation,
            "orders": self.handle_orders,
            "trade": self.handle_trade,
            "trades": self.handle_trades_list,  # Keep only one trades handler
            "symbol": self.handle_symbol_stats,
            "summary": self.handle_portfolio_summary,
            "addtrade": self.handle_addtrade,
        }

        for command, handler in handlers.items():
            wrapped_handler = lambda u, c, h=handler: self.handle_command_wrapper(h, u, c)
            self.app.add_handler(CommandHandler(command, wrapped_handler))
        
        # Add message handler for conversations
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

    async def send_message(self, text, parse_mode=None, reply_markup=None):
        """Send message with improved reliability"""
        try:
            async with self.command_lock:
                if len(text) > 4000:
                    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
                    for chunk in chunks:
                        await self._send_with_retry(chunk, parse_mode, reply_markup)
                else:
                    await self._send_with_retry(text, parse_mode, reply_markup)
        except Exception as e:
            self.logger.error(f"Failed to send message: {e}")

    async def _send_with_retry(self, text, parse_mode=None, reply_markup=None, max_retries=3):
        """Send message with retries"""
        for attempt in range(max_retries):
            try:
                return await self.app.bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    read_timeout=10,
                    write_timeout=10,
                    connect_timeout=10,
                    pool_timeout=10
                )
            except Exception as e:
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(1)

    async def handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        welcome_msg = (
            "🤖 Binance Trading Bot\n\n"
            "Available Commands:\n\n"
            "📊 Market Analysis:\n"
            "/positions - Show current prices and trading opportunities\n"
            "/orders - Show open limit orders with cancel times\n\n"
            "💰 Portfolio & Trading:\n"
            "/balance - Show current balance for all assets\n"
            "/trades - List all trades with P/L after tax\n"
            "/addtrade - Add a manual trade to track\n"
            "/symbol <SYMBOL> - Show detailed symbol stats with tax\n"
            "/summary - Show complete portfolio summary with tax\n"
            "/profits - Show current profits for all positions\n"
            "/portfolio - Show portfolio value evolution\n"
            "/allocation - Show current asset distribution\n\n"
            "📈 Analytics:\n"
            "/distribution - Show entry price distribution\n"
            "/stacking - Show position building patterns\n"
            "/buytimes - Show time between purchases\n\n"
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
        """Show trading positions with thresholds and reset times"""
        try:
            # Send initial response
            processing_message = await self.send_message("📊 Fetching positions...")

            positions = []
            cached_prices = self.bot.ws_manager.last_prices
            now = datetime.now(timezone.utc)

            # Process each trading symbol
            for symbol in sorted(self.bot.valid_symbols):
                price_data = cached_prices.get(symbol, {})
                if not price_data:
                    positions.append(f"⚪ {symbol}: No price data")
                    continue

                price = price_data.get('price', 0)
                change = price_data.get('change', 0)
                arrow = "↑" if change >= 0 else "↓"
                color = "🟢" if change >= 0 else "🔴"

                # Get reference prices for drop calculations
                ref_prices = self.bot.get_reference_prices(symbol)
                
                symbol_info = [f"{color} {symbol}: {price:.8f} ({change:+.2f}%) {arrow}"]
                
                # Add timeframe information
                for timeframe in ['daily', 'weekly', 'monthly']:
                    if not self.bot.timeframe_config[timeframe]['enabled']:
                        continue

                    # Get reference price for timeframe
                    ref_price = ref_prices.get(timeframe, {}).get('open', 0)
                    if ref_price:
                        current_drop = ((ref_price - price) / ref_price) * 100
                        
                        # Get thresholds and their status
                        thresholds = self.bot.timeframe_config[timeframe]['thresholds']
                        threshold_status = []
                        
                        for threshold in thresholds:
                            threshold_pct = threshold * 100
                            if symbol in self.bot.strategy.order_history[timeframe] and \
                               threshold in self.bot.strategy.order_history[timeframe][symbol]:
                                # Calculate time until reset
                                last_order = self.bot.strategy.order_history[timeframe][symbol][threshold]
                                
                                # Calculate reset time based on timeframe
                                if timeframe == 'daily':
                                    reset_time = last_order + timedelta(days=1)
                                elif timeframe == 'weekly':
                                    reset_time = last_order + timedelta(days=7)
                                else:  # monthly
                                    reset_time = last_order + timedelta(days=30)
                                
                                # Rest of the code
                                if now < reset_time:
                                    time_left = reset_time - now
                                    hours = int(time_left.total_seconds() / 3600)
                                    mins = int((time_left.total_seconds() % 3600) / 60)
                                    threshold_status.append(f"🔒 {threshold_pct:.1f}% ({hours}h {mins}m)")
                                else:
                                    threshold_status.append(f"✅ {threshold_pct:.1f}%")
                            else:
                                if current_drop >= threshold_pct:
                                    threshold_status.append(f"🟡 {threshold_pct:.1f}%")
                                else:
                                    threshold_status.append(f"⚪ {threshold_pct:.1f}%")
                        
                        symbol_info.append(f"  {timeframe.capitalize()}: {current_drop:+.2f}%")
                        symbol_info.append(f"    Thresholds: {' | '.join(threshold_status)}")

                positions.extend(symbol_info)
                positions.append("")  # Add blank line between symbols

            # Format message
            message = "🎯 Trading Positions & Thresholds:\n\n"
            message += "Legend:\n"
            message += "⚪ Not Triggered | 🟡 Triggered | ✅ Available | 🔒 Locked (time till reset)\n\n"
            message += "\n".join(positions)
            message += f"\n\nLast Update: {now.strftime('%H:%M:%S UTC')}"

            # Edit the processing message
            await self.app.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=processing_message.message_id,
                text=message
            )

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

    async def handle_portfolio_summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show summary of all trades and total portfolio performance"""
        try:
            if not self.bot.trades:
                await self.send_message("No trades found")
                return

            # Calculate totals
            total_investment = 0
            total_current_value = 0
            total_gross_profit = 0
            symbol_summaries = []

            # Process each symbol
            for symbol in sorted(set(trade['symbol'] for trade in self.bot.trades.values())):
                stats = await self.bot.get_symbol_stats(symbol)
                if stats:
                    total_investment += stats['total_cost']
                    total_current_value += stats['current_value']
                    total_gross_profit += stats['gross_profit_usdt']
                    symbol_summaries.append(stats)

            # Calculate portfolio totals
            total_tax = total_gross_profit * self.bot.tax_rate if total_gross_profit > 0 else 0
            total_net_profit = total_gross_profit - total_tax if total_gross_profit > 0 else total_gross_profit
            total_profit_percentage = (total_net_profit / total_investment * 100) if total_investment > 0 else 0

            # Format message
            message = "📊 Portfolio Summary\n\n"
            
            # Overall summary
            message += f"💼 Total Portfolio:\n"
            message += f"Investment: {total_investment:.2f} USDT\n"
            message += f"Current Value: {total_current_value:.2f} USDT\n"
            message += f"Gross P/L: {total_gross_profit:+.2f} USDT\n"
            message += f"Tax (28%): {total_tax:.2f} USDT\n"
            message += f"Net P/L: {total_net_profit:+.2f} USDT ({total_profit_percentage:+.2f}%)\n\n"
            
            # Individual symbols
            message += "📈 By Symbol:\n"
            for stats in symbol_summaries:
                profit_color = "🟢" if stats['net_profit_usdt'] >= 0 else "🔴"
                message += (
                    f"{profit_color} {stats['symbol']}\n"
                    f"   Cost: {stats['total_cost']:.2f} USDT\n"
                    f"   Value: {stats['current_value']:.2f} USDT\n"
                    f"   Net P/L: {stats['net_profit_usdt']:+.2f} USDT ({stats['net_profit_percentage']:+.2f}%)\n"
                    f"   Trades: {stats['number_of_trades']}\n\n"
                )

            message += "\nUse /symbol <SYMBOL> for detailed symbol statistics"
            await self.send_message(message)
            
        except Exception as e:
            await self.send_message(f"❌ Error getting portfolio summary: {e}")

    async def handle_addtrade(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /addtrade command with step-by-step interaction"""
        chat_id = update.effective_chat.id
        
        # Initialize or reset conversation state
        self.trade_conv_state[chat_id] = {
            'step': 'symbol',
            'symbol': None,
            'entry_price': None,
            'quantity': None
        }
        
        # Start conversation
        await self.send_message(
            "Let's add a manual trade! 📝\n\n"
            "Please enter the trading pair symbol (e.g., BTCUSDT):"
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle conversation messages for adding trades"""
        chat_id = update.effective_chat.id
        text = update.message.text
        
        if chat_id not in self.trade_conv_state:
            return
            
        state = self.trade_conv_state[chat_id]
        
        try:
            if state['step'] == 'symbol':
                symbol = text.upper()
                if not symbol.endswith('USDT'):
                    await self.send_message("❌ Only USDT pairs are supported (e.g., BTCUSDT)\nPlease try again:")
                    return
                    
                state['symbol'] = symbol
                state['step'] = 'entry_price'
                await self.send_message(f"✅ Symbol: {symbol}\n\nPlease enter the entry price in USDT:")
                
            elif state['step'] == 'entry_price':
                entry_price = float(text)
                if entry_price <= 0:
                    await self.send_message("❌ Entry price must be greater than 0\nPlease try again:")
                    return
                    
                state['entry_price'] = entry_price
                state['step'] = 'quantity'
                await self.send_message(
                    f"✅ Entry Price: {entry_price:.8f} USDT\n\n"
                    f"Please enter the quantity of {state['symbol'].replace('USDT', '')}:"
                )
                
            elif state['step'] == 'quantity':
                quantity = float(text)
                if quantity <= 0:
                    await self.send_message("❌ Quantity must be greater than 0\nPlease try again:")
                    return
                
                # Calculate total cost
                total_cost = state['entry_price'] * quantity
                
                # Show summary and confirmation
                confirm_msg = (
                    "📋 Trade Summary\n\n"
                    f"Symbol: {state['symbol']}\n"
                    f"Entry Price: {state['entry_price']:.8f} USDT\n"
                    f"Quantity: {quantity:.8f}\n"
                    f"Total Cost: {total_cost:.2f} USDT\n\n"
                    "Is this correct? Type 'yes' to confirm or 'no' to cancel:"
                )
                
                state['quantity'] = quantity
                state['step'] = 'confirm'
                await self.send_message(confirm_msg)
                
            elif state['step'] == 'confirm':
                if text.lower() == 'yes':
                    # Generate trade ID
                    trade_id = f"MANUAL_{datetime.now().strftime('%Y%m%d%H%M%S')}_{state['symbol']}"
                    
                    # Create trade entry
                    trade_entry = {
                        'symbol': state['symbol'],
                        'entry_price': state['entry_price'],
                        'quantity': state['quantity'],
                        'total_cost': state['entry_price'] * state['quantity'],
                        'type': 'manual',
                        'status': 'FILLED',
                        'filled_time': datetime.now(timezone.utc).isoformat()
                    }
                    
                    # Add to trades
                    self.bot.trades[trade_id] = trade_entry
                    self.bot.save_trades()
                    
                    await self.send_message(f"✅ Trade added successfully!\nTrade ID: {trade_id}")
                    
                elif text.lower() == 'no':
                    await self.send_message("❌ Trade cancelled. Use /addtrade to start over.")
                else:
                    await self.send_message("Please type 'yes' to confirm or 'no' to cancel:")
                    return
                    
                # Clear conversation state
                del self.trade_conv_state[chat_id]
                
        except ValueError:
            await self.send_message("❌ Invalid number format. Please enter a valid number:")
        except Exception as e:
            await self.send_message(f"❌ Error: {str(e)}\nUse /addtrade to start over.")
            del self.trade_conv_state[chat_id]

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
