from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram import BotCommand, Update
import telegram.error  # Add this import
import asyncio
from colorama import Fore
import logging
from datetime import datetime, timezone, timedelta
from collections import deque
import queue
import time
import random
import sys

class TelegramHandler:
    def __init__(self, token, chat_id, bot_instance):
        self.token = token
        self.chat_id = chat_id
        self.bot = bot_instance  # Reference to main bot for accessing data
        self.app = Application.builder().token(token).build()
        self.commands_setup = False
        self.logger = logging.getLogger('Telegram')
        self.trade_conv_state = {}  # Add this to track conversation states
        self.command_lock = asyncio.Lock()  # Add lock for commands
        self.command_timeout = 30  # Timeout for commands in seconds
        self.processing_commands = set()  # Track processing commands

        # Add message queue and processing flag
        self.message_queue = asyncio.Queue()
        self.is_processing = False
        self.message_lock = asyncio.Lock()
        self.message_processor_task = None
        self.max_queue_size = 1000
        self.batch_size = 5  # Process messages in small batches
        self.batch_delay = 0.1  # Small delay between batches

        self.command_queue = asyncio.Queue()  # Changed from PriorityQueue to regular Queue
        self.command_workers = 3  # Number of workers processing commands
        self.message_processor_task = None
        self.command_workers = []  # Add this to track workers
        self.command_processor_task = None  # Add this line

        self.logger.setLevel(logging.DEBUG)  # Set to DEBUG for more detailed logs
        
        # Add retry parameters
        self.max_retries = 3
        self.base_retry_delay = 1
        self.max_retry_delay = 30
        self.jitter_factor = 0.1
        
        # Add connection status tracking
        self.last_successful_send = 0
        self.consecutive_failures = 0
        self.backoff_until = 0

        self.error_count = 0
        self.max_errors = 10
        self.error_reset_time = time.time()
        self.error_reset_interval = 300  # 5 minutes

        # Add timeout settings
        self.connect_timeout = 30
        self.read_timeout = 30
        self.write_timeout = 30
        self.pool_timeout = 30
        self.command_timeout = 60  # Increased from 30 to 60 seconds
        
        # Add connection health tracking
        self.last_successful_connection = 0
        self.connection_failures = 0
        self.max_failures = 5
        self.backoff_time = 0
        self.connection_check_interval = 300  # 5 minutes

        # Add priority queue
        self.priority_queue = asyncio.PriorityQueue()
        self.normal_queue = asyncio.Queue()
        self.emergency_confirmed = False

        self.initialized = False
        self.startup_sent = False
        self.emergency_stop_code = None

    async def send_startup_notification(self):
        """Send startup notification with retries and feedback"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                print(f"\r{Fore.CYAN}Sending startup message (attempt {attempt + 1})...", end='')
                startup_msg = self._get_startup_message()
                await self.safe_send_message(
                    startup_msg,
                    parse_mode='HTML',
                    priority=True
                )
                print(f"\r{Fore.GREEN}Startup message sent successfully!{' '*20}")
                self.startup_sent = True
                return
            except Exception as e:
                if attempt == max_retries - 1:
                    print(f"\r{Fore.RED}Failed to send startup message: {e}{' '*20}")
                    self.logger.error(f"Failed to send startup notification: {e}")
                await asyncio.sleep(2)

    async def initialize(self):
        """Initialize Telegram bot with single startup"""
        try:
            if self.initialized:
                return True

            self.logger.info("Starting Telegram initialization...")
            
            # Generate emergency stop code once during initialization
            self.emergency_stop_code = ''.join(random.choices('0123456789', k=6))
            self.logger.info(f"Emergency stop code generated: {self.emergency_stop_code}")
            
            # Register command handlers first
            self.register_handlers()
            
            # Initialize application
            await self.app.initialize()
            
            # Start application
            await self.app.start()
            
            # Start polling in background with proper error handling
            self.poll_task = asyncio.create_task(
                self.app.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True,
                    error_callback=self._polling_error_callback
                )
            )
            
            # Start message processor
            self.message_processor_task = asyncio.create_task(
                self._process_message_queue()
            )
            
            # Wait for tasks to start
            await asyncio.sleep(1)
            
            # Verify tasks are running
            if (self.poll_task.done() or 
                self.message_processor_task.done()):
                raise Exception("Failed to start Telegram tasks")
            
            # Send startup message only once
            if not self.startup_sent:
                await self.send_startup_notification()
                self.startup_sent = True
            
            self.initialized = True
            self.logger.info("Telegram bot initialized successfully")
            return True

        except Exception as e:
            self.logger.error(f"Failed to initialize Telegram: {e}")
            # Cleanup any running tasks
            await self.shutdown()
            return False

    def register_handlers(self):
        """Register simplified command handlers"""
        try:
            # Essential commands only
            handlers = {
                "start": self.handle_start,
                "status": self.handle_status,      
                "orders": self.handle_orders,
                "balance": self.handle_balance,
                "trades": self.handle_trades_list,
                "trade": self.handle_trade,
                "add": self.handle_addtrade,
                "stop": self.handle_emergency_stop,
                "summary": self.handle_portfolio_summary,
                "symbol": self.handle_symbol_stats,
                "thresholds": self.handle_thresholds,
                "help": self.handle_help
            }
            
            # Register handlers with wrapper
            for command, handler in handlers.items():
                self.app.add_handler(
                    CommandHandler(
                        command, 
                        lambda update, context, h=handler: self.handle_command_wrapper(h, update, context)
                    )
                )

            # Add general message handler for conversations
            self.app.add_handler(
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    self.handle_message
                )
            )

        except Exception as e:
            self.logger.error(f"Error registering handlers: {e}")
            raise

    async def handle_command_wrapper(self, handler, update, context):
        """Handle commands with improved error handling"""
        try:
            command = update.message.text.split()[0][1:]
            
            if command in self.processing_commands:
                await self.safe_send_message("⏳ Command already processing, please wait...")
                return

            self.logger.debug(f"Processing command: {command}")
            self.processing_commands.add(command)
            
            try:
                # Show typing indicator
                await self.app.bot.send_chat_action(
                    chat_id=update.effective_chat.id,
                    action="typing"
                )
                
                # Execute command with timeout
                await asyncio.wait_for(
                    handler(update, context),
                    timeout=self.command_timeout
                )
                
            except asyncio.TimeoutError:
                await self.safe_send_message(f"❌ Command {command} timed out. Please try again.")
            except Exception as e:
                self.logger.error(f"Error executing {command}: {e}")
                await self.safe_send_message("❌ Error processing command. Please try again.")
            finally:
                self.processing_commands.discard(command)
                
        except Exception as e:
            self.logger.error(f"Error in command wrapper: {e}")
            await self.safe_send_message("❌ An error occurred. Please try again.")

    async def _send_startup_message(self):
        """Send startup message with retries"""
        for attempt in range(3):
            try:
                startup_msg = (
                    "🤖 *Binance Trading Bot Started*\n\n"
                    "*Trading Configuration:*\n"
                    f"• Mode: `{'Testnet' if self.bot.use_testnet else 'Live'}`\n"
                    f"• Market: `Spot`\n"
                    f"• Trading Pairs: `{', '.join(self.bot.valid_symbols)}`\n"
                    f"• USDT Reserve: `{self.bot.reserve_balance_usdt}`\n\n"
                    "*Active Thresholds:*\n"
                    "• Daily: `1%, 2%, 3%`\n"
                    "• Weekly: `3%, 6%, 10%`\n"
                    "• Monthly: `5%, 10%`\n\n"
                    "*Commands Available:*\n"
                    "📊 /status - Show prices & balance\n"
                    "📋 /orders - Show open orders\n"
                    "💰 /balance - Show balance\n"
                    "📈 /trades - List all trades\n"
                    "🔍 /trade - Show trade details\n"
                    "➕ /add - Add manual trade\n"
                    "💱 /symbol - Show pair stats\n"
                    "📑 /summary - Portfolio overview\n\n"
                    "🟢 _Bot is actively monitoring markets_"
                )
                
                await self.send_message(startup_msg, parse_mode='MarkdownV2')
                return True
            except Exception as e:
                self.logger.error(f"Startup message attempt {attempt+1} failed: {e}")
                await asyncio.sleep(2)
        return False

    async def _process_commands(self):
        """Process commands with high priority"""
        while True:
            try:
                _, command = await self.command_queue.get()
                try:
                    await command()
                finally:
                    self.command_queue.task_done()
            except Exception as e:
                self.logger.error(f"Command processing error: {e}")
                await asyncio.sleep(1)

    async def handle_command_wrapper(self, handler, update, context):
        """Handle commands immediately without queueing"""
        try:
            command = update.message.text.split()[0][1:]
            
            if command in self.processing_commands:
                await self.send_message("Command already processing, please wait...")
                return

            self.logger.debug(f"Processing command: {command}")
            self.processing_commands.add(command)
            
            try:
                await self.app.bot.send_chat_action(
                    chat_id=update.effective_chat.id,
                    action="typing"
                )
                await handler(update, context)
            finally:
                self.processing_commands.discard(command)
                
        except Exception as e:
            self.logger.exception(f"Error in command {command}: {e}")
            await self.send_message(
                f"Error processing command. Please try again."
            )

    async def _execute_command(self, handler, update, context, command):
        """Execute command with enhanced logging"""
        try:
            self.processing_commands.add(command)
            self.logger.info(f"Executing command: {command}")
            
            async with self.command_lock:
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
                    self.logger.info(f"Command completed successfully: {command}")
                except asyncio.TimeoutError:
                    self.logger.error(f"Command timed out: {command}")
                    await self.send_message(f"Command {command} timed out. Please try again.")
                    
        except Exception as e:
            self.logger.exception(f"Error executing command {command}: {e}")
            await self.send_message(
                f"Error processing command {command}. Please try again."
            )
        finally:
            self.processing_commands.discard(command)

    def register_handlers(self):
        """Register simplified command handlers"""
        try:
            # Essential commands only - Updated list
            handlers = {
                "start": self.handle_start,
                "status": self.handle_status,      
                "orders": self.handle_orders,
                "balance": self.handle_balance,
                "trades": self.handle_trades_list,  # Changed from handle_trades
                "trade": self.handle_trade,        # Added for single trade lookup
                "add": self.handle_addtrade,       
                "stop": self.handle_emergency_stop,
                "summary": self.handle_portfolio_summary,  # Added
                "symbol": self.handle_symbol_stats,  # Added
                "thresholds": self.handle_thresholds,
                "help": self.handle_help
            }
            
            # Register handlers
            for command, handler in handlers.items():
                self.app.add_handler(CommandHandler(
                    command, 
                    self._wrap_handler(handler)
                ))

        except Exception as e:
            self.logger.error(f"Error registering handlers: {e}")
            raise

    async def handle_thresholds(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show detailed threshold information for all timeframes"""
        try:
            message = "📊 Threshold Status Overview\n\n"
            now = datetime.now(timezone.utc)

            for timeframe, config in self.bot.timeframe_config.items():
                if not config.get('enabled', False):
                    continue

                # Get next reset time
                next_reset = self.bot.next_reset_times[timeframe]
                time_to_reset = next_reset - now
                hours, remainder = divmod(time_to_reset.seconds, 3600)
                minutes, _ = divmod(remainder, 60)

                message += f"⏱️ {timeframe.upper()} Timeframe\n"
                message += f"Next reset in: {time_to_reset.days}d {hours}h {minutes}m\n"
                message += "Thresholds:\n"

                # Get thresholds and their status
                thresholds = config.get('thresholds', [])
                for threshold in thresholds:
                    threshold_pct = threshold * 100
                    triggered = False
                    last_trigger = None

                    # Check if threshold was triggered in orders_placed
                    for symbol in self.bot.valid_symbols:
                        if (symbol in self.bot.orders_placed and 
                            timeframe in self.bot.orders_placed[symbol] and
                            threshold in self.bot.orders_placed[symbol][timeframe]):
                            triggered = True
                            last_trigger = self.bot.orders_placed[symbol][timeframe][threshold]
                            break

                    # Format status
                    if triggered:
                        trigger_time = datetime.fromisoformat(last_trigger)
                        time_since = now - trigger_time
                        message += f"  {threshold_pct:>5.2f}% ✅ ({time_since.days}d {time_since.seconds//3600}h ago)\n"
                    else:
                        message += f"  {threshold_pct:>5.2f}% ⏳ (waiting)\n"

                message += "\n"

            # Add current price changes
            message += "Current Price Changes:\n"
            for symbol in self.bot.valid_symbols:
                ref_prices = self.bot.get_reference_prices(symbol)
                if not ref_prices:
                    continue

                message += f"\n{symbol}:\n"
                for timeframe, prices in ref_prices.items():
                    if not prices['open']:
                        continue
                        
                    # Get current price
                    ticker = await self.bot.api.get_symbol_ticker(symbol)
                    if not ticker:
                        continue
                        
                    current_price = float(ticker['price'])
                    change = ((current_price - prices['open']) / prices['open']) * 100
                    arrow = "↑" if change >= 0 else "↓"
                    
                    message += f"  {timeframe}: {change:+.2f}% {arrow}\n"

            await self.send_message(message)

        except Exception as e:
            await self.send_message(f"❌ Error getting threshold status: {e}")

    async def handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Combined status command showing prices, positions, and balance"""
        try:
            message = "📊 Current Status\n\n"
            
            # Get current prices and 24h changes
            for symbol in self.bot.valid_symbols:
                ticker = await self.bot.api.get_symbol_ticker(symbol)
                stats = await self.bot.api.get_24h_stats(symbol)
                
                if ticker and stats:
                    price = float(ticker['price'])
                    change = float(stats['priceChangePercent'])
                    arrow = "↑" if change >= 0 else "↓"
                    
                    message += (
                        f"{symbol}: {price:.8f} USDT\n"
                        f"24h Change: {change:+.2f}% {arrow}\n\n"
                    )

            # Add balance information
            balance = self.bot.get_balance()
            if balance:
                message += "💰 Balance:\n"
                if 'USDT' in balance:
                    message += f"USDT: {balance['USDT']['free']:.2f}\n"
                for asset, details in balance.items():
                    if asset != 'USDT' and details['total'] > 0:
                        message += f"{asset}: {details['total']:.8f}\n"
                        
            await self.send_message(message)
            
        except Exception as e:
            await self.send_message(f"❌ Error getting status: {e}")

    async def handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show updated available commands"""
        help_text = (
            "Available Commands:\n\n"
            "Core Commands:\n"
            "/status - Show current prices and balance\n"
            "/orders - Show open limit orders\n"
            "/balance - Show current balance\n\n"
            "Trading Commands:\n"
            "/trades - List all trades\n"
            "/trade <ID> - Show specific trade details\n"
            "/add - Add manual trade\n"
            "/symbol <PAIR> - Show detailed stats for pair\n"
            "/summary - Show portfolio summary\n\n"
            "Analysis:\n"
            "/thresholds - Show threshold status\n\n"
            "System:\n"
            "/stop - Emergency stop\n"
            "/help - Show this help message"
        )
        await self.send_message(help_text)

    def _wrap_handler(self, handler):
        """Enhanced command handler wrapper with timeout and retry logic"""
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
            try:
                if not self.initialized:
                    await self.safe_send_message("Bot is still initializing, please wait...")
                    return

                command = update.message.text.split()[0][1:]
                self.logger.debug(f"Processing command: {command}")
                
                # Check connection health before proceeding
                if not await self._check_connection():
                    await self.safe_send_message(
                        "⚠️ Connection issues detected. Command processing may be delayed.",
                        priority=True
                    )
                
                # Send typing action with retry
                for attempt in range(3):
                    try:
                        await self.app.bot.send_chat_action(
                            chat_id=update.effective_chat.id,
                            action="typing",
                            read_timeout=self.read_timeout,
                            write_timeout=self.write_timeout,
                            connect_timeout=self.connect_timeout,
                            pool_timeout=self.pool_timeout
                        )
                        break
                    except telegram.error.TimedOut:
                        if attempt == 2:
                            self.logger.warning("Failed to send typing action after 3 attempts")
                        await asyncio.sleep(1)
                    except Exception as e:
                        self.logger.error(f"Error sending typing action: {e}")
                        break

                # Execute command with timeout
                try:
                    result = await asyncio.wait_for(
                        handler(update, context),
                        timeout=self.command_timeout
                    )
                    # Update connection health on success
                    self.last_successful_connection = time.time()
                    self.connection_failures = 0
                    return result
                    
                except asyncio.TimeoutError:
                    error_msg = f"Command {command} timed out. Please try again."
                    self.logger.error(f"Command timeout: {command}")
                    await self.safe_send_message(error_msg)
                    self.connection_failures += 1
                    
            except Exception as e:
                self.logger.exception(f"Error in command {update.message.text}: {e}")
                await self.safe_send_message(
                    f"Error processing command. Please try again later."
                )
                self.connection_failures += 1
                
        return wrapped

    async def queue_message(self, text, parse_mode=None, reply_markup=None, priority=False):
        """Queue message for sending with optional priority"""
        try:
            if self.message_queue.qsize() >= self.max_queue_size:
                self.logger.warning("Message queue full, dropping oldest message")
                await self.message_queue.get()
            
            message_data = {
                'text': text,
                'parse_mode': parse_mode,
                'reply_markup': reply_markup,
                'priority': priority,
                'timestamp': datetime.now(timezone.utc)
            }
            
            await self.message_queue.put(message_data)
            
        except Exception as e:
            self.logger.error(f"Error queuing message: {e}")

    async def _process_message_queue(self):
        """Process queued messages with rate limiting"""
        while True:
            try:
                if not self.message_queue.empty():
                    message = await self.message_queue.get()
                    try:
                        await self._send_with_retry(
                            text=message['text'],
                            parse_mode=message.get('parse_mode'),
                            reply_markup=message.get('reply_markup')
                        )
                    finally:
                        self.message_queue.task_done()
                        # Add small delay between messages
                        await asyncio.sleep(0.5)
                else:
                    # Don't burn CPU when queue is empty
                    await asyncio.sleep(0.1)
                    
            except Exception as e:
                self.logger.error(f"Error processing message queue: {e}")
                await asyncio.sleep(1)

    async def send_message(self, text, parse_mode=None, reply_markup=None, priority=False, **kwargs):
        """Enhanced message sending with retries"""
        if not text or not self.initialized:
            return None

        try:
            if parse_mode == 'HTML':
                text = (text.replace('<', '&lt;')
                          .replace('>', '&gt;')
                          .replace('&', '&amp;'))

            message_data = {
                'chat_id': self.chat_id,
                'text': text,
                'parse_mode': parse_mode,
                'reply_markup': reply_markup,
                **kwargs
            }

            for attempt in range(3):
                try:
                    return await self.app.bot.send_message(**message_data)
                except telegram.error.RetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                except Exception as e:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1)

        except Exception as e:
            self.logger.error(f"Failed to send message: {e}")
            return None

    async def _send_with_retry(self, text, parse_mode=None, reply_markup=None):
        """Send message with retry and proper escaping"""
        if parse_mode == 'MarkdownV2':
            # Escape special characters
            special_chars = '_*[]()~`>#+-=|{}.!'
            for char in special_chars:
                text = text.replace(char, f'\\{char}')

        for attempt in range(self.max_retries):
            try:
                return await self.app.bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup
                )
            except telegram.error.RetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except Exception as e:
                if attempt == self.max_retries - 1:
                    self.logger.error(f"Failed to send message after {self.max_retries} attempts: {e}")
                    raise
                await asyncio.sleep(1)

    async def safe_send_message(self, text, priority=False, **kwargs):
        """Send message with enhanced safety and retry logic"""
        if not text or not self.initialized:
            return None
            
        if time.time() < self.backoff_time:
            if priority:
                self.logger.warning("High priority message during backoff period")
            else:
                await self.queue_message(text, priority=priority, **kwargs)
                return None

        for attempt in range(self.max_retries):
            try:
                # Split long messages
                if len(text) > 4096:
                    chunks = [text[i:i+4096] for i in range(0, len(text), 4096)]
                    responses = []
                    for chunk in chunks:
                        response = await self.app.bot.send_message(
                            chat_id=self.chat_id,
                            text=chunk,
                            read_timeout=self.read_timeout,
                            write_timeout=self.write_timeout,
                            connect_timeout=self.connect_timeout,
                            pool_timeout=self.pool_timeout,
                            **kwargs
                        )
                        responses.append(response)
                        await asyncio.sleep(0.5)  # Small delay between chunks
                    return responses[-1]
                else:
                    return await self.app.bot.send_message(
                        chat_id=self.chat_id,
                        text=text,
                        read_timeout=self.read_timeout,
                        write_timeout=self.write_timeout,
                        connect_timeout=self.connect_timeout,
                        pool_timeout=self.pool_timeout,
                        **kwargs
                    )
                    
            except telegram.error.TimedOut:
                self.connection_failures += 1
                delay = min(30, (2 ** attempt))
                self.logger.warning(f"Message timed out, retrying in {delay}s")
                await asyncio.sleep(delay)
                
            except Exception as e:
                self.logger.error(f"Error sending message: {e}")
                if attempt == self.max_retries - 1:
                    await self.queue_message(text, priority=priority, **kwargs)
                    return None
                await asyncio.sleep(1)

        return None

    async def _check_connection(self):
        """Check Telegram connection health"""
        current_time = time.time()
        
        # Reset failures if enough time has passed
        if current_time - self.last_successful_connection > self.connection_check_interval:
            self.connection_failures = 0
            
        # Implement exponential backoff if too many failures
        if self.connection_failures >= self.max_failures:
            backoff_duration = min(300, (2 ** (self.connection_failures - self.max_failures)) * 30)
            self.backoff_time = current_time + backoff_duration
            self.logger.warning(f"Too many connection failures. Backing off for {backoff_duration}s")
            return False
            
        return True

    # Update API handler references in handle_positions method
    async def handle_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show positions using new BinanceAPI"""
        try:
            loading_msg = await self.safe_send_message("📊 Fetching positions...")
            
            positions = []
            for symbol in self.bot.valid_symbols:
                # Get current price and stats using new API
                ticker = await self.bot.api.get_symbol_ticker(symbol)
                stats = await self.bot.api.get_24h_stats(symbol)
                
                price = float(ticker['price'])
                change = float(stats['priceChangePercent'])
                
                # Rest of position handling...
                
        except Exception as e:
            await self.safe_send_message(f"❌ Error fetching positions: {e}")

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
        """Show open orders using MongoDB"""
        try:
            pending_orders = await self.bot.mongo.get_pending_orders(
                'testnet' if self.bot.use_testnet else 'live'
            )

            if not pending_orders:
                await self.send_message("No open orders")
                return

            message = "📋 Open Orders:\n\n"
            for order in pending_orders:
                cancel_time = datetime.fromisoformat(order['cancel_time'])
                message += (
                    f"ID: {order['order_id']}\n"
                    f"Symbol: {order['symbol']}\n"
                    f"Price: {order.get('price', 'Market')} USDT\n"
                    f"Quantity: {order['quantity']}\n"
                    f"Cancels at: {cancel_time.strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
                )

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
        """Show list of all trades using MongoDB"""
        try:
            # Get trades from MongoDB
            trades = await self.bot.mongo.get_trade_history(
                'testnet' if self.bot.use_testnet else 'live',
                limit=100
            )

            if not trades:
                await self.send_message("No trades found")
                return

            # Group trades by symbol
            trades_by_symbol = {}
            for trade in trades:
                symbol = trade['trade_info']['symbol']
                if symbol not in trades_by_symbol:
                    trades_by_symbol[symbol] = []
                trades_by_symbol[symbol].append(trade)

            message = "📈 Trading History by Symbol:\n\n"
            
            # Process each symbol
            for symbol in sorted(trades_by_symbol.keys()):
                stats = await self.bot.mongo.get_symbol_stats(
                    'testnet' if self.bot.use_testnet else 'live',
                    symbol
                )
                
                if stats:
                    profit_color = "🟢" if stats['total_pnl'] >= 0 else "🔴"
                    message += (
                        f"{profit_color} {symbol}\n"
                        f"   Trades: {stats['total_trades']}\n"
                        f"   Avg Entry: {stats['avg_entry_price']:.8f}\n"
                        f"   Net P/L: {stats['total_pnl']:+.2f} USDT\n\n"
                    )

            await self.send_message(message)
            
        except Exception as e:
            await self.send_message(f"❌ Error fetching trades: {e}")

    async def handle_symbol_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show symbol statistics using MongoDB"""
        try:
            if not context.args or len(context.args) != 1:
                await self.send_message("❌ Please provide a symbol\nExample: /symbol BTCUSDT")
                return

            symbol = context.args[0].upper()
            stats = await self.bot.mongo.get_symbol_stats(
                'testnet' if self.bot.use_testnet else 'live',
                symbol
            )

            if not stats:
                await self.send_message(f"No trades found for {symbol}")
                return

            message = (
                f"📊 {symbol} Trading Summary\n\n"
                f"Position Size: {stats['total_volume']:.8f}\n"
                f"Total Value: {stats['total_value_usdt']:.2f} USDT\n"
                f"Average Entry: {stats['avg_entry_price']:.8f} USDT\n\n"
                f"Total P/L: {stats['total_pnl']:+.2f} USDT\n"
                f"Number of Trades: {stats['total_trades']}\n"
                f"Last Trade: {stats['last_trade_time'].strftime('%Y-%m-%d %H:%M:%S')} UTC"
            )

            await self.send_message(message)

        except Exception as e:
            await self.send_message(f"❌ Error getting symbol stats: {e}")

    async def handle_portfolio_summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show portfolio summary using MongoDB"""
        try:
            summary = await self.bot.mongo.get_portfolio_summary(
                'testnet' if self.bot.use_testnet else 'live'
            )

            if not summary:
                await self.send_message("No trading data available")
                return

            message = (
                "📊 Portfolio Summary\n\n"
                f"Total Trades: {summary['total_trades']}\n"
                f"Total Value: {summary['total_value_usdt']:.2f} USDT\n"
                f"Total P/L: {summary['total_pnl']:+.2f} USDT\n"
                f"Total Fees: {summary['total_fees']:.2f} USDT\n\n"
                f"Net P/L: {(summary['total_pnl'] - summary['total_fees']):+.2f} USDT"
            )

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

    async def handle_emergency_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle emergency stop command"""
        try:
            if not context.args:
                # First confirmation step
                message = (
                    "⚠️ EMERGENCY STOP REQUESTED\n\n"
                    "This will:\n"
                    "1. Cancel all open orders\n"
                    "2. Stop all trading activities\n"
                    "3. Close WebSocket connections\n\n"
                    f"To confirm, use:\n/emergency {self.emergency_stop_code}"
                )
                await self.send_message(message, priority=True)
                return

            if context.args[0] == self.emergency_stop_code:
                self.emergency_confirmed = True
                
                # Send high priority notification
                await self.send_message(
                    "🚨 EMERGENCY STOP CONFIRMED\nInitiating shutdown...",
                    priority=True
                )
                
                # Cancel all orders
                await self.bot.cancel_all_orders()
                
                # Stop WebSocket
                if self.bot.ws_manager:
                    await self.bot.ws_manager.stop()
                
                # Final confirmation
                await self.send_message(
                    "✅ Emergency stop completed:\n"
                    "• All orders cancelled\n"
                    "• Trading stopped\n"
                    "• Connections closed",
                    priority=True
                )
                
                # Initiate bot shutdown
                asyncio.create_task(self.bot.shutdown())
            else:
                await self.send_message(
                    "❌ Invalid emergency code. Command cancelled.",
                    priority=True
                )
        except Exception as e:
            self.logger.error(f"Error in emergency stop: {e}")
            await self.send_message(
                "❌ Error executing emergency stop",
                priority=True
            )

    async def shutdown(self):
        """Enhanced shutdown with proper task cleanup"""
        try:
            if not self.initialized:
                return

            self.logger.info("Shutting down Telegram bot...")
            
            try:
                # Cancel all tasks
                for task in [self.poll_task, self.message_processor_task, self.command_processor_task]:
                    if task:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

                # Stop the application
                if self.app.running:
                    await self.app.stop()
                    await self.app.shutdown()
                    
                self.initialized = False
                self.logger.info("Telegram bot shutdown complete")
                
            except Exception as e:
                self.logger.error(f"Error during shutdown: {e}")
                
        except Exception as e:
            self.logger.error(f"Fatal error during shutdown: {e}")
        finally:
            self.initialized = False

    async def _polling_error_callback(self, error):
        """Handle polling errors"""
        self.logger.error(f"Polling error: {error}")
        
        # Try to restart polling if it fails
        if self.initialized and self.poll_task and self.poll_task.done():
            self.poll_task.cancel()
            self.poll_task = asyncio.create_task(
                self.app.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True,
                    error_callback=self._polling_error_callback
                )
            )
            self.logger.info("Restarted polling task")

    def _get_startup_message(self):
        """Generate startup message with updated commands"""
        startup_msg = (
            "🤖 Binance Trading Bot Started!\n\n"
            "📈 Trading Configuration:\n"
            f"• Mode: {'Testnet' if self.bot.client.API_URL == 'https://testnet.binance.vision/api' else 'Live'}\n"
            f"• Order Type: {self.bot.order_type.capitalize()}\n"
            f"• Trading Pairs: {', '.join([pair for pair in self.bot.valid_symbols])}\n"
            f"• USDT Reserve: {self.bot.reserve_balance_usdt}\n"
            f"• Tax Rate: 28%\n\n"
            "📊 Available Commands:\n\n"
            "Core Commands:\n"
            "/status - Show current prices and balance\n"
            "/orders - Show open limit orders\n"
            "/balance - Show current balance\n\n"
            "Trading Commands:\n"
            "/trades - List all trades\n"
            "/trade <ID> - Show specific trade details\n"
            "/add - Add manual trade\n"
            "/symbol <PAIR> - Show detailed stats for pair\n"
            "/summary - Show portfolio summary\n\n"
            "Analysis:\n"
            "/thresholds - Show threshold status\n\n"
            "System:\n"
            "/stop - Emergency stop\n"
            "/help - Show this help message\n\n"
            "🟢 Bot is actively monitoring markets!"
        )
        return startup_msg

    async def handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        welcome_msg = (
            "👋 Welcome to the Binance Trading Bot!\n\n"
            "Available commands:\n\n"
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
            "/stats - Show system information"
        )
        await self.send_message(welcome_msg)

    async def handle_leverage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Change leverage for futures trading"""
        try:
            if 'futures' not in self.bot.api.api_mode:
                await self.send_message("❌ Leverage command only available in futures mode")
                return

            if not context.args or len(context.args) != 2:
                await self.send_message("Usage: /leverage <symbol> <1-125>")
                return

            symbol = context.args[0].upper()
            leverage = int(context.args[1])

            if leverage < 1 or leverage > 125:
                await self.send_message("❌ Leverage must be between 1 and 125")
                return

            result = await self.bot.api.change_leverage(symbol, leverage)
            await self.send_message(f"✅ Leverage changed for {symbol} to {leverage}x")

        except Exception as e:
            await self.send_message(f"❌ Error changing leverage: {e}")

    async def handle_margin_type(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Change margin type for futures"""
        try:
            if 'futures' not in self.bot.api.api_mode:
                await self.send_message("❌ Margin type command only available in futures mode")
                return

            if not context.args or len(context.args) != 2:
                await self.send_message("Usage: /margin <symbol> <isolated|cross>")
                return

            symbol = context.args[0].upper()
            margin_type = context.args[1].lower()

            if margin_type not in ['isolated', 'cross']:
                await self.send_message("❌ Margin type must be either 'isolated' or 'cross'")
                return

            result = await self.bot.api.change_margin_type(symbol, margin_type)
            await self.send_message(f"✅ Margin type changed for {symbol} to {margin_type}")

        except Exception as e:
            await self.send_message(f"❌ Error changing margin type: {e}")
