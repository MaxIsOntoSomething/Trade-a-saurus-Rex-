from binance.client import AsyncClient
from binance.exceptions import BinanceAPIException
from decimal import Decimal
from datetime import datetime, timedelta
import asyncio
import logging
from typing import Dict, List, Optional, Tuple
import aiohttp
import json
import re  # Add import for regex support
from ..types.models import Order, OrderStatus, TimeFrame, OrderType, TradeDirection, TakeProfit, StopLoss, TPSLStatus  # Add TP/SL imports
from ..utils.rate_limiter import RateLimiter
from ..types.constants import PRECISION, MIN_NOTIONAL, TIMEFRAME_INTERVALS, TRADING_FEES, ORDER_TYPE_FEES
from ..utils.chart_generator import ChartGenerator
from ..utils.yahoo_scrapooooor_sp500 import YahooSP500Scraper  # Import the new Yahoo scraper
import time

logger = logging.getLogger(__name__)

class BinanceClient:
    def __init__(self, api_key: str, api_secret: str, telegram_bot=None, mongo_client=None, config=None, testnet: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.client = None
        self.reference_prices = {}
        self.triggered_thresholds = {}
        self.rate_limiter = RateLimiter()
        self.symbol_info = {}
        self.last_reset = {
            tf: datetime.utcnow() for tf in TimeFrame
        }
        self.balance_cache = {}
        self.reference_timestamps = {
            TimeFrame.DAILY: None,
            TimeFrame.WEEKLY: None,
            TimeFrame.MONTHLY: None
        }
        logger.setLevel(logging.DEBUG)
        self.telegram_bot = telegram_bot
        self.chart_generator = ChartGenerator()
        self.mongo_client = mongo_client
        self.config = config
        
        # Set API environment info
        environment = "TESTNET" if testnet else "MAINNET"
        logger.info(f"[INIT] Using Binance {environment} API")
        
        # Set reserve balance and base currency directly from config
        self.base_currency = None
        self.reserve_balance = 0
        if config and 'trading' in config:
            self.base_currency = config['trading'].get('base_currency', 'USDT')
            self.reserve_balance = float(config['trading'].get('reserve_balance', 0))
            logger.info(f"[INIT] Config loaded directly: Base Currency={self.base_currency}, Reserve=${self.reserve_balance:,.2f}")
        
        # Initialize Yahoo SP500 scraper
        self.yahoo_scraper = YahooSP500Scraper()
        
        # Initialize thresholds dictionary with nested structure to track triggered thresholds
        self.triggered_thresholds = {}
        
        # Initialize pairs with nested dictionaries for each timeframe if config is provided
        if config and 'trading' in config and 'pairs' in config['trading']:
            pairs = config['trading']['pairs']
            for pair in pairs:
                self.triggered_thresholds[pair] = {
                    'daily': set(),
                    'weekly': set(),
                    'monthly': set()
                }
        
        # Add default TP/SL values
        self.default_tp_percentage = 0
        self.default_sl_percentage = 0
        
        if config and 'trading' in config:
            # Parse TP/SL settings from config
            tp_setting = config['trading'].get('take_profit', '0%')
            sl_setting = config['trading'].get('stop_loss', '0%')
            
            # Extra logging to debug the raw values
            logger.info(f"[TP/SL DEBUG] Raw settings from config - TP: '{tp_setting}', SL: '{sl_setting}'")
            
            # Extract percentage values (remove % sign and convert to float)
            try:
                # Handle both string and numeric inputs
                if isinstance(tp_setting, (int, float)):
                    self.default_tp_percentage = float(tp_setting)
                else:
                    cleaned_tp = tp_setting.strip().replace('%', '')
                    self.default_tp_percentage = float(cleaned_tp) if cleaned_tp else 0
                logger.info(f"[INIT] Take Profit configured: {self.default_tp_percentage}%")
            except (ValueError, AttributeError) as e:
                logger.warning(f"[INIT] Invalid Take Profit setting: '{tp_setting}', using 0% (Error: {e})")
                self.default_tp_percentage = 0
                
            try:
                # Handle both string and numeric inputs
                if isinstance(sl_setting, (int, float)):
                    self.default_sl_percentage = float(sl_setting)
                else:
                    cleaned_sl = sl_setting.strip().replace('%', '')
                    self.default_sl_percentage = float(cleaned_sl) if cleaned_sl else 0
                logger.info(f"[INIT] Stop Loss configured: {self.default_sl_percentage}%")
            except (ValueError, AttributeError) as e:
                logger.warning(f"[INIT] Invalid Stop Loss setting: '{sl_setting}', using 0% (Error: {e})")
                self.default_sl_percentage = 0
        
        # Add tracking of invalid symbols
        self.invalid_symbols = set()
        
        # Initialize thresholds dictionary with nested structure to track triggered thresholds
        self.triggered_thresholds = {}
        
        # Add regex pattern for valid Binance symbols
        self.valid_symbol_pattern = re.compile(r'^[A-Z0-9\-.]{1,20}$')
        
    def set_telegram_bot(self, bot):
        """Set telegram bot for notifications"""
        self.telegram_bot = bot
        
    async def check_initial_balance(self) -> bool:
        """Check if current balance is above reserve requirement"""
        try:
            if self.reserve_balance is None or self.reserve_balance <= 0:
                logger.info(f"Initial balance check skipped - no reserve requirement (value: {self.reserve_balance})")
                return True

            if not self.base_currency:
                logger.warning("Initial balance check skipped - no base currency specified")
                return True

            current_balance = await self.get_balance(self.base_currency)
            if float(current_balance) < self.reserve_balance:
                logger.error(
                    f"Initial balance check failed:\n"
                    f"Current Balance: ${float(current_balance):.2f} {self.base_currency}\n"
                    f"Reserve Balance: ${self.reserve_balance:.2f} {self.base_currency}\n"
                    f"Trading will be paused until balance is above reserve."
                )
                if self.telegram_bot:
                    self.telegram_bot.is_paused = True
                    await self.telegram_bot.send_initial_balance_alert(
                        current_balance=current_balance,
                        reserve_balance=self.reserve_balance
                    )
                return False

            logger.info(
                f"Initial balance check passed:\n"
                f"Current Balance: ${float(current_balance):.2f} {self.base_currency}\n"
                f"Reserve Balance: ${self.reserve_balance:.2f} {self.base_currency}"
            )
            return True

        except Exception as e:
            logger.error(f"Error checking initial balance: {e}")
            return False

    async def initialize(self):
        """Initialize client with reserve balance and base currency"""
        self.client = await AsyncClient.create(
            api_key=self.api_key,
            api_secret=self.api_secret,
            testnet=self.testnet
        )

        # Set reserve balance directly from config with debug logging (UPDATED CODE)
        if self.telegram_bot and self.telegram_bot.config:
            # Use values from telegram_bot only if they weren't already set from config
            if not self.base_currency or self.base_currency == 'USDT':
                self.base_currency = self.telegram_bot.config['trading'].get('base_currency', 'USDT')
            if self.reserve_balance <= 0:
                self.reserve_balance = float(self.telegram_bot.config['trading'].get('reserve_balance', 0))
                
            logger.info(f"[INIT] Base Currency: {self.base_currency}")
            logger.info(f"[INIT] Reserve Balance: ${self.reserve_balance:,.2f}")
        else:
            # Only log a warning if we haven't already set values from direct config
            if not self.config:
                logger.warning("[INIT] No config found for reserve balance!")

        # Initialize restored threshold info
        self.restored_threshold_info = []

        # Get exchange info for precision
        exchange_info = await self.client.get_exchange_info()
        for symbol in exchange_info['symbols']:
            self.symbol_info[symbol['symbol']] = {
                'baseAssetPrecision': symbol['baseAssetPrecision'],
                'quotePrecision': symbol['quotePrecision'],
                'filters': {f['filterType']: f for f in symbol['filters']}
            }
            
        # Restore triggered thresholds from database
        if self.mongo_client:
            self.restored_threshold_info = await self.restore_threshold_state()
            
        # Add initial balance check
        await self.check_initial_balance()

        # Load previously identified invalid symbols from database
        if self.mongo_client:
            invalid_symbols = await self.mongo_client.get_invalid_symbols()
            self.invalid_symbols = set(invalid_symbols)
            if invalid_symbols:
                logger.info(f"Loaded {len(invalid_symbols)} known invalid symbols: {', '.join(invalid_symbols)}")

        # Initialize the trading symbols in the database with pre-configured symbols
        if self.mongo_client and self.config and 'trading' in self.config and 'pairs' in self.config['trading']:
            # First, check if there's a record of removed pre-configured symbols
            removed_symbols = await self.mongo_client.get_removed_symbols()
            
            # Filter out any symbols that were intentionally removed
            configured_symbols = [symbol for symbol in self.config['trading']['pairs'] 
                                 if symbol not in removed_symbols]
            
            # Store the original config symbols for reference
            self.original_config_symbols = set(self.config['trading']['pairs'])
            
            # Get existing symbols from database
            existing_symbols = await self.mongo_client.get_trading_symbols()
            
            # Add configured symbols that aren't in the database yet
            for symbol in configured_symbols:
                if symbol not in existing_symbols:
                    await self.mongo_client.save_trading_symbol(symbol)
                    logger.info(f"Added pre-configured symbol {symbol} to database")
            
            # Update the config with all symbols (including any that might have been in DB but not config)
            db_symbols = await self.mongo_client.get_trading_symbols()
            if db_symbols:
                logger.info(f"Loaded {len(db_symbols)} trading symbols from database")
                self.config['trading']['pairs'] = db_symbols

    async def restore_threshold_state(self):
        """Restore the state of triggered thresholds with skip for removed symbols"""
        try:
            if not self.mongo_client:
                logger.error("Cannot restore thresholds without MongoDB client")
                return
            
            # Get list of removed symbols
            removed_symbols = set(await self.mongo_client.get_removed_symbols())
            
            # Get all triggered thresholds from database
            all_thresholds = await self.mongo_client.get_triggered_thresholds()
            
            if not all_thresholds:
                logger.info("No previously triggered thresholds to restore")
                return
            
            # Dictionary to store by symbol for telegram notification
            restored_by_symbol = {}
            
            for entry in all_thresholds:
                symbol = entry.get('symbol')
                timeframe = entry.get('timeframe')
                thresholds = entry.get('thresholds', [])
                
                # Skip if this symbol has been removed by the user
                if symbol in removed_symbols:
                    logger.info(f"Skipping threshold restoration for removed symbol: {symbol}")
                    continue
                
                # Skip if invalid data
                if not symbol or not timeframe or not thresholds:
                    continue
                
                # Skip if the symbol is not in configured trading pairs
                if symbol not in self.config['trading']['pairs']:
                    logger.warning(f"Skipping threshold restoration for unconfigured symbol: {symbol}")
                    continue
                
                # Restore to in-memory state
                if symbol not in self.triggered_thresholds:
                    self.triggered_thresholds[symbol] = {}
                
                self.triggered_thresholds[symbol][timeframe] = thresholds
                
                # Build data for telegram notification
                if symbol not in restored_by_symbol:
                    restored_by_symbol[symbol] = {}
                
                restored_by_symbol[symbol][timeframe] = thresholds
                
            # Notify via telegram
            if restored_by_symbol and self.telegram_bot:
                await self.telegram_bot.send_threshold_restoration_notification(restored_by_symbol)
            
            logger.info(f"Restored triggered thresholds: {self.triggered_thresholds}")
            
        except Exception as e:
            logger.error(f"Error restoring triggered thresholds: {e}")

    async def close(self):
        if self.client:
            await self.client.close_connection()
            
    async def check_timeframe_reset(self, timeframe: TimeFrame) -> bool:
        """Check if a timeframe needs to be reset based on elapsed time"""
        try:
            # Get reference timestamp for timeframe
            reference_ts = await self.get_reference_timestamp(timeframe)
            current_time = datetime.now()
            reference_time = datetime.fromtimestamp(reference_ts / 1000)  # Convert ms to seconds
            
            # Determine if reset is needed based on timeframe
            reset_needed = False
            
            if timeframe == TimeFrame.DAILY:
                # Reset if day has changed
                reset_needed = current_time.day != reference_time.day or current_time.month != reference_time.month or current_time.year != reference_time.year
                
            elif timeframe == TimeFrame.WEEKLY:
                # Reset if week has changed (using ISO week number for consistency)
                current_week = current_time.isocalendar()[1]
                reference_week = reference_time.isocalendar()[1]
                reset_needed = current_week != reference_week or current_time.year != reference_time.year
                
            elif timeframe == TimeFrame.MONTHLY:
                # Reset if month has changed
                reset_needed = current_time.month != reference_time.month or current_time.year != reference_time.year
            
            if reset_needed:
                logger.info(f"Time to reset {timeframe.value} thresholds. " +
                           f"Last reset: {reference_time.strftime('%Y-%m-%d')}, Current: {current_time.strftime('%Y-%m-%d')}")
                
                # Perform the reset
                await self.reset_timeframe_thresholds(timeframe.value)
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error checking timeframe reset: {e}")
            return False

    async def get_reference_timestamp(self, timeframe: TimeFrame) -> int:
        """Get the reference timestamp for a timeframe"""
        now = datetime.utcnow()
        
        if timeframe == TimeFrame.DAILY:
            # Get previous day's midnight UTC
            reference = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if now.hour == 0 and now.minute < 1:  # Within first minute of new day
                reference -= timedelta(days=1)
        
        elif timeframe == TimeFrame.WEEKLY:
            # Get last Monday midnight UTC
            days_since_monday = now.weekday()
            reference = now.replace(hour=0, minute=0, second=0, microsecond=0)
            reference -= timedelta(days=days_since_monday)
            if now.weekday() == 0 and now.hour == 0 and now.minute < 1:
                reference -= timedelta(days=7)
        
        elif timeframe == TimeFrame.MONTHLY:
            # Get 1st of current month midnight UTC
            reference = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if now.day == 1 and now.hour == 0 and now.minute < 1:
                # If within first minute of new month, use last month
                if now.month == 1:
                    reference = reference.replace(year=now.year-1, month=12)
                else:
                    reference = reference.replace(month=now.month-1)
        
        return int(reference.timestamp() * 1000)  # Convert to milliseconds

    async def get_reference_price(self, symbol: str, timeframe: TimeFrame) -> float:
        """Get reference price for symbol at timeframe with format validation"""
        try:
            # Validate symbol format first
            if not self._is_valid_symbol_format(symbol) or symbol in self.invalid_symbols:
                logger.warning(f"Invalid symbol format or known invalid: {symbol}")
                return None
                
            # Use the current candle's open price instead of historical data
            interval_map = {
                TimeFrame.DAILY: '1d',    # Daily candle
                TimeFrame.WEEKLY: '1w',   # Weekly candle
                TimeFrame.MONTHLY: '1M'   # Monthly candle
            }
            
            interval = interval_map[timeframe]
            
            await self.rate_limiter.acquire()
            
            # Get current candle
            klines = await self.client.get_klines(
                symbol=symbol,
                interval=interval,
                limit=1  # Just get the current candle
            )
            
            if klines and len(klines) > 0:
                ref_price = float(klines[0][1])  # Current candle's open price
                logger.info(f"    {timeframe.value} reference: ${ref_price:,.2f}")
                return ref_price
            else:
                logger.warning(f"No kline data for {symbol} {timeframe.value}")
                return None
                
        except BinanceAPIException as e:
            if e.code == -1121 or e.code == -1100:
                logger.warning(f"Invalid symbol during reference price check: {symbol}")
                self.invalid_symbols.add(symbol)
                if self.mongo_client:
                    await self.mongo_client.save_invalid_symbol(symbol, str(e))
                return None
            else:
                logger.error(f"Failed to get reference price for {symbol} {timeframe.value}: {e}", exc_info=True)
                return None
        except Exception as e:
            logger.error(f"Failed to get reference price for {symbol} {timeframe.value}: {e}", exc_info=True)
            return None

    async def update_reference_prices(self, symbols: List[str]):
        """Update reference prices for all timeframes with symbol pre-validation"""
        try:
            # Filter out known invalid symbols
            valid_symbols = [symbol for symbol in symbols if symbol not in self.invalid_symbols]
            
            # Also filter by format before attempting API calls
            valid_symbols = [symbol for symbol in valid_symbols if self._is_valid_symbol_format(symbol)]
            
            for symbol in valid_symbols:
                if (symbol not in self.reference_prices):
                    self.reference_prices[symbol] = {}
                    self.triggered_thresholds[symbol] = {
                        'daily': set(),
                        'weekly': set(),
                        'monthly': set()
                    }

                # Get current price first with better error handling
                try:
                    await self.rate_limiter.acquire()
                    ticker = await self.client.get_symbol_ticker(symbol=symbol)
                    current_price = float(ticker['price'])

                    # Print symbol header and current price together
                    logger.info(f"\n=== Checking {symbol} ===")
                    logger.info(f"Current price for {symbol}: ${current_price:,.2f}")
                
                    # Process each timeframe
                    for timeframe in TimeFrame:
                        logger.info(f"  ▶ Getting {timeframe.value} reference price")
                        ref_price = await self.get_reference_price(symbol, timeframe)
                        
                        if ref_price is not None:
                            self.reference_prices[symbol][timeframe] = ref_price
                        else:
                            logger.warning(f"    Using current price as {timeframe.value} reference")
                            self.reference_prices[symbol][timeframe] = current_price
                except BinanceAPIException as e:
                    if e.code == -1121 or e.code == -1100:  # Added -1100 error code
                        logger.error(f"Invalid symbol: {symbol}. Adding to invalid symbols list.")
                        self.invalid_symbols.add(symbol)
                        if self.mongo_client:
                            await self.mongo_client.save_invalid_symbol(symbol, str(e))
                        continue
                    else:
                        raise

                # Add small delay between symbols
                await asyncio.sleep(0.1)

        except Exception as e:
            logger.error(f"Failed to update prices: {e}", exc_info=True)
            raise
            
    async def check_thresholds(self, symbol: str, timeframe: TimeFrame) -> List[float]:
        """Check price thresholds for a symbol and timeframe with format validation"""
        try:
            # Validate symbol format first
            if not self._is_valid_symbol_format(symbol) or symbol in self.invalid_symbols:
                logger.warning(f"Invalid symbol format or known invalid: {symbol}")
                return []
                
            # Get reference price (or calculate if not available)
            reference_price = await self.get_reference_price(symbol, timeframe)
            if not reference_price:
                logger.warning(f"No reference price for {symbol} {timeframe.value}")
                return []
                
            # Get current price
            current_price = await self.get_current_price(symbol)
            if not current_price:
                logger.warning(f"Failed to get current price for {symbol}")
                return []
                
            # Calculate price change as a percentage
            price_change = ((current_price - reference_price) / reference_price) * 100
            
            # Get thresholds for this timeframe
            thresholds = self.config['trading']['thresholds'][timeframe.value]
            
            # Check if we've triggered any thresholds
            triggered = []
            
            for threshold in thresholds:
                # Skip if this threshold has already been triggered
                if (
                    symbol in self.triggered_thresholds and
                    timeframe.value in self.triggered_thresholds[symbol] and
                    threshold in self.triggered_thresholds[symbol][timeframe.value]
                ):
                    logger.debug(f"Threshold {threshold}% for {symbol} {timeframe.value} already triggered, skipping")
                    continue
                    
                # Check if price dropped by the threshold percentage or more
                if price_change <= -threshold:
                    triggered.append(threshold)
                    logger.info(f"✅ Threshold triggered: {symbol} {threshold}% on {timeframe.value}")
                    
                    # Mark threshold as triggered immediately after detection
                    await self.mark_threshold_triggered(symbol, timeframe, threshold)
                    
                    # If we have a Telegram bot, send notification
                    if hasattr(self, 'telegram_bot') and self.telegram_bot:
                        await self.telegram_bot.send_threshold_notification(
                            symbol, timeframe, threshold, 
                            current_price, reference_price, price_change
                        )
            
            return triggered
            
        except Exception as e:
            logger.error(f"Error checking thresholds: {e}", exc_info=True)
            return []

    async def mark_threshold_triggered(self, symbol: str, timeframe: TimeFrame, threshold: float):
        """Mark a threshold as triggered in memory and persist to database"""
        try:
            # Convert timeframe to value string if it's an enum
            timeframe_value = timeframe.value if hasattr(timeframe, 'value') else timeframe
            
            # Initialize if needed - ensure we have a standardized structure
            if symbol not in self.triggered_thresholds:
                self.triggered_thresholds[symbol] = {
                    'daily': set(),
                    'weekly': set(),
                    'monthly': set()
                }
            
            # Ensure the timeframe exists in the dictionary
            if timeframe_value not in self.triggered_thresholds[symbol]:
                self.triggered_thresholds[symbol][timeframe_value] = set()
                
            # Add threshold if not already in list
            if threshold not in self.triggered_thresholds[symbol][timeframe_value]:
                self.triggered_thresholds[symbol][timeframe_value].add(threshold)
                logger.info(f"Marking threshold {threshold}% as triggered for {symbol} {timeframe_value}")
                
                # Persist to database immediately with await instead of background task
                if self.mongo_client:
                    threshold_list = list(self.triggered_thresholds[symbol][timeframe_value])
                    try:
                        success = await self.mongo_client.save_triggered_threshold(
                            symbol, timeframe_value, threshold_list
                        )
                        if success:
                            logger.info(f"Successfully saved triggered threshold state to database for {symbol} {timeframe_value}")
                        else:
                            logger.error(f"Failed to save triggered threshold state to database for {symbol} {timeframe_value}")
                    except Exception as db_error:
                        logger.error(f"Error saving threshold state to database: {db_error}", exc_info=True)
            else:
                logger.debug(f"Threshold {threshold}% already marked as triggered for {symbol} {timeframe_value}")
                
        except Exception as e:
            logger.error(f"Error marking threshold triggered: {e}", exc_info=True)

    async def reset_timeframe_thresholds(self, timeframe_str: str):
        """Reset triggered thresholds for a specific timeframe"""
        try:
            # Reset triggered thresholds in database
            if self.mongo_client:
                await self.mongo_client.reset_timeframe_thresholds(timeframe_str)
            
            # Update reference prices for all trading pairs
            await self.update_reference_prices(self.config['trading']['pairs'])
            
            # Prepare reset information for notification
            reset_info = {
                'timeframe': timeframe_str,
                'timestamp': datetime.now(),
                'pairs': []
            }
            
            # Gather information about reference prices for notification
            for symbol in self.config['trading']['pairs']:
                if symbol in self.invalid_symbols:
                    continue
                
                timeframe = TimeFrame(timeframe_str)
                price = await self.get_reference_price(symbol, timeframe)
                
                if price:
                    reset_info['pairs'].append({
                        'symbol': symbol,
                        'reference_price': price,
                        'thresholds': self.config['trading']['thresholds'][timeframe_str]
                    })
            
            # Log the reset
            logger.info(f"Reset {timeframe_str} thresholds for {len(reset_info['pairs'])} pairs")
            
            # Send notification about the reset
            if self.telegram_bot:
                await self.telegram_bot.send_timeframe_reset_notification(reset_info)
            
            return reset_info
            
        except Exception as e:
            logger.error(f"Error resetting {timeframe_str} thresholds: {e}")
            return None

    async def restore_triggered_thresholds(self):
        """Restore triggered thresholds from database on startup"""
        try:
            # Get thresholds from database
            stored_thresholds = await self.mongo_client.get_all_triggered_thresholds()
            
            # Initialize dictionary for notification data
            restored_info = {}
            
            # Process each stored threshold
            for threshold_data in stored_thresholds:
                symbol = threshold_data['symbol']
                timeframe = TimeFrame(threshold_data['timeframe'])
                threshold = threshold_data['threshold']
                
                # Initialize nested dictionaries if needed
                if symbol not in self.triggered_thresholds:
                    self.triggered_thresholds[symbol] = {}
                if timeframe not in self.triggered_thresholds[symbol]:
                    self.triggered_thresholds[symbol][timeframe] = []
                    
                # Add threshold to in-memory storage
                if threshold not in self.triggered_thresholds[symbol][timeframe]:
                    self.triggered_thresholds[symbol][timeframe].append(threshold)
                    
                # Add to notification data
                if symbol not in restored_info:
                    restored_info[symbol] = {}
                if timeframe not in restored_info[symbol]:
                    restored_info[symbol][timeframe] = []
                restored_info[symbol][timeframe].append(threshold)
                
            # Store restored threshold info for notification
            self.restored_threshold_info = restored_info
            
            # Log restoration results
            total_count = sum(len(thresholds) for symbol_data in self.triggered_thresholds.values() 
                            for thresholds in symbol_data.values())
            logger.info(f"Restored {total_count} triggered thresholds from database")
            
            return self.triggered_thresholds
            
        except Exception as e:
            logger.error(f"Error restoring triggered thresholds: {e}")
            return {}

    async def check_reserve_balance(self, order_amount: float) -> bool:
        """Check if placing an order would violate reserve balance"""
        try:
            logger.info("[RESERVE CHECK] Starting reserve balance check...")
            
            if self.reserve_balance is None or self.reserve_balance <= 0:
                logger.info(f"[RESERVE CHECK] No valid reserve balance set (value: {self.reserve_balance})")
                return True

            # Get current balance in base currency
            current_balance = await self.get_balance(self.base_currency)
            logger.info(f"[RESERVE CHECK] Current balance: ${float(current_balance):,.2f} {self.base_currency}")
            logger.info(f"[RESERVE CHECK] Reserve balance: ${float(self.reserve_balance):,.2f} {self.base_currency}")
            
            # Get sum of pending orders
            pending_orders_value = Decimal('0')
            if self.telegram_bot and self.telegram_bot.mongo_client:
                cursor = self.telegram_bot.mongo_client.orders.find({"status": "pending"})
                async for order in cursor:
                    pending_orders_value += (Decimal(str(order['price'])) * Decimal(str(order['quantity'])))

            # Calculate remaining balance after pending orders
            available_balance = float(current_balance - pending_orders_value)
            remaining_after_order = available_balance - order_amount

            logger.info(f"[RESERVE CHECK] Available after pending: ${available_balance:.2f} {self.base_currency}")
            logger.info(f"[RESERVE CHECK] Remaining after order: ${remaining_after_order:.2f} {self.base_currency}")
            logger.info(f"[RESERVE CHECK] Required reserve: ${float(self.reserve_balance):,.2f} {self.base_currency}")

            # Check if remaining balance would be above reserve
            return remaining_after_order >= self.reserve_balance

        except Exception as e:
            logger.error(f"[RESERVE CHECK] Error checking reserve balance: {e}")
            return False

    async def place_limit_buy_order(self, symbol: str, amount: float, 
                                  threshold: Optional[float] = None,
                                  timeframe: Optional[TimeFrame] = None,
                                  is_manual: bool = False) -> Order:
        """Place a limit buy order with reserve balance check"""
        # Check reserve balance first
        if not is_manual and not await self.check_reserve_balance(amount):
            raise ValueError("Order would violate reserve balance")

        await self.rate_limiter.acquire()
        
        try:
            # Get current price
            ticker = await self.client.get_symbol_ticker(symbol=symbol)
            price = Decimal(ticker['price'])
            
            # Check if this would raise the average entry price when only_lower_entries is enabled
            if not is_manual and self.config and 'trading' in self.config and self.config['trading'].get('only_lower_entries', False):
                # Get existing position average entry price from MongoDB
                current_avg_price = None
                if self.mongo_client:
                    position = await self.mongo_client.get_position_for_symbol(symbol)
                    if position and 'avg_entry_price' in position:
                        current_avg_price = Decimal(position['avg_entry_price'])
                
                # If we already have a position and current price is higher than avg entry
                if current_avg_price is not None and price > current_avg_price:
                    logger.warning(f"Skipping order: Current price ${float(price):.2f} is higher than average entry price ${float(current_avg_price):.2f}")
                    logger.warning(f"The 'only_lower_entries' protection is enabled")
                    raise ValueError(f"Current price ${float(price):.2f} would increase average entry of ${float(current_avg_price):.2f}")
            
            # Original requested amount (for logging if adjustment needed)
            original_amount = amount
            
            # Calculate quantity based on USDT amount
            quantity = Decimal(str(amount)) / price
            
            # Get and apply precision
            quantity_precision = self._get_quantity_precision(symbol)
            price_precision = self._get_price_precision(symbol)
            
            # Round quantity to precision and adjust for lot size
            quantity = Decimal(str(round(quantity, quantity_precision)))
            quantity = self._adjust_quantity_to_lot_size(symbol, quantity)
            price = Decimal(str(round(price, price_precision)))
            
            # Calculate order value after adjustments
            order_value = price * quantity
            
            # Check minimum notional
            min_notional = MIN_NOTIONAL.get(symbol, MIN_NOTIONAL['DEFAULT'])
            if order_value < Decimal(str(min_notional)):
                # Auto-increase quantity to meet minimum notional requirement
                logger.warning(f"Order value ${float(order_value):.2f} below minimum notional ${min_notional}. Adjusting quantity...")
                
                # Calculate required quantity to meet minimum notional
                required_quantity = Decimal(str(min_notional)) / price
                required_quantity = Decimal(str(round(required_quantity, quantity_precision)))
                required_quantity = self._adjust_quantity_to_lot_size(symbol, required_quantity)
                
                # Update the quantity and log the adjustment
                adjusted_amount = float(required_quantity * price)
                logger.info(f"Adjusted order amount from ${original_amount:.2f} to ${adjusted_amount:.2f} to meet minimum notional")
                quantity = required_quantity
                
                # If this order would now violate reserve balance, check again
                if not is_manual and adjusted_amount > original_amount and not await self.check_reserve_balance(adjusted_amount):
                    raise ValueError(f"Adjusted order (${adjusted_amount:.2f}) would violate reserve balance")
            
            # Log order details before placement
            logger.info(f"Placing order: {symbol} quantity={quantity} price=${price}")
            
            # Calculate fees
            fees, fee_asset = await self.calculate_fees(symbol, price, quantity)
            
            # Only update triggered thresholds if it's not a manual trade
            if not is_manual and threshold and symbol in self.triggered_thresholds and timeframe:
                self.mark_threshold_triggered(symbol, timeframe.value, threshold)
            
            # Generate unique order ID
            order_id = str(int(datetime.utcnow().timestamp() * 1000))
            
            if not is_manual:
                # Place order on Binance
                order_response = await self.client.create_order(
                    symbol=symbol,
                    side='BUY',
                    type='LIMIT',
                    timeInForce='GTC',
                    quantity=float(quantity),
                    price=float(price)
                )
                order_id = str(order_response['orderId'])
            
            # Create order object with all required fields
            order = Order(
                symbol=symbol,
                status=OrderStatus.PENDING if not is_manual else OrderStatus.FILLED,
                order_type=OrderType.SPOT,
                price=price,
                quantity=quantity,
                timeframe=timeframe or TimeFrame.DAILY,
                order_id=order_id,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                filled_at=datetime.utcnow() if is_manual else None,
                fees=fees,
                fee_asset=fee_asset,
                threshold=threshold  # This is now handled by the Order class
            )
            
            # If the order is considered filled (manual orders), create TP/SL
            if order.status == OrderStatus.FILLED:
                await self.create_tp_sl_orders(order)
            
            return order
            
        except BinanceAPIException as e:
            logger.error(f"Failed to place order: {e}")
            raise
        except Exception as e:
            logger.error(f"Error placing order: {str(e)}")
            raise

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an order with proper error handling"""
        try:
            await self.client.cancel_order(symbol=symbol, orderId=order_id)
            logger.info(f"Successfully cancelled order {order_id} for {symbol}")
            return True
        except Exception as e:
            error_msg = str(e)
            
            # Handle specific API errors more gracefully
            if "Unknown order sent" in error_msg or "code=-2011" in error_msg:
                # Order already filled, cancelled, or doesn't exist
                logger.warning(f"Order {order_id} for {symbol} already cancelled or doesn't exist")
                # Return True so the calling code knows to update the DB
                return True
            
            logger.error(f"Failed to cancel order: {e}")
            return False
            
    async def check_order_status(self, symbol: str, order_id: str) -> Optional[OrderStatus]:
        """Check the status of an order"""
        try:
            order = await self.client.get_order(symbol=symbol, orderId=order_id)
            if order['status'] == 'FILLED':
                return OrderStatus.FILLED
            elif order['status'] == 'CANCELED':
                return OrderStatus.CANCELLED
            return OrderStatus.PENDING
        except BinanceAPIException as e:
            logger.error(f"Failed to check order status: {e}")
            return None
            
    async def get_balance(self, symbol: str = None) -> Decimal:
        """Get balance for a specific asset"""
        await self.rate_limiter.acquire()
        try:
            # Use the instance base_currency if no symbol is provided
            if symbol is None:
                symbol = self.base_currency or 'USDT'
                
            account = await self.client.get_account()
            for balance in account['balances']:
                if (balance['asset'] == symbol):
                    return Decimal(balance['free'])
            return Decimal('0')
        except BinanceAPIException as e:
            logger.error(f"Failed to get balance: {e}")
            raise
            
    async def get_balance_changes(self, symbol: str = 'USDT') -> Optional[Decimal]:
        """Get balance changes since last check"""
        current_balance = await self.get_balance(symbol)
        previous_balance = self.balance_cache.get(symbol)
        self.balance_cache[symbol] = current_balance
        
        if previous_balance is not None:
            return current_balance - previous_balance
        return None

    async def get_candles_for_chart(self, symbol: str, timeframe: TimeFrame, count: int = 15) -> List[Dict]:
        """Get historical candles for chart generation with flexible data handling"""
        try:
            # Map timeframes to intervals and milliseconds
            interval_map = {
                TimeFrame.DAILY: ('1d', 24 * 60 * 60 * 1000),
                TimeFrame.WEEKLY: ('1w', 7 * 24 * 60 * 60 * 1000),
                TimeFrame.MONTHLY: ('1M', 30 * 24 * 60 * 60 * 1000)
            }
            
            interval, ms_per_candle = interval_map[timeframe]
            
            # First attempt: Get recent candles without time constraints
            logger.info(f"Fetching {count} candles for {symbol} on {timeframe.value} timeframe")
            await self.rate_limiter.acquire()
            
            # Start with a simple request for the most recent candles
            klines = await self.client.get_klines(
                symbol=symbol,
                interval=interval,
                limit=count + 5  # Request extra candles to handle potential gaps
            )
            
            if not klines:
                logger.warning(f"No candles returned for {symbol} {timeframe.value}")
                # Try alternative interval for new pairs
                alternative_interval = '1h' if timeframe == TimeFrame.DAILY else '4h'
                logger.info(f"Trying alternative interval {alternative_interval} for {symbol}")
                
                # Get more frequent candles and aggregate them if needed
                alternative_klines = await self.client.get_klines(
                    symbol=symbol,
                    interval=alternative_interval,
                    limit=100  # Get more candles at a higher frequency
                )
                
                if not alternative_klines:
                    logger.error(f"Still no candles available for {symbol}")
                    return []
                    
                # Use alternative candles directly (just a few for chart visualization)
                klines = alternative_klines[-count:] if len(alternative_klines) > count else alternative_klines
            
            # Process and convert candles
            candles = []
            for k in klines:
                candles.append({
                    'timestamp': k[0],
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4]),
                    'volume': float(k[5])
                })
            
            # Set minimum required candles for a proper chart
            min_candles_required = 3
            
            # Check if we have enough candles, but DON'T try to generate synthetic ones
            if len(candles) < min_candles_required:
                logger.warning(f"Only {len(candles)} candles available for {symbol} {timeframe.value} - not enough for proper chart visualization")
                # Return the candles we have without trying to generate synthetic ones
                # The calling code will handle the case where there aren't enough candles
            
            # Limit to requested count if we have more
            candles = candles[-count:] if len(candles) > count else candles
            
            # Log actual candles retrieved
            candle_dates = [datetime.fromtimestamp(c['timestamp']/1000).strftime('%Y-%m-%d') for c in candles]
            logger.info(f"Got {len(candles)} candles for {symbol} {timeframe.value}: {', '.join(candle_dates)}")
            
            return candles
            
        except Exception as e:
            logger.error(f"Failed to get candles for chart: {e}", exc_info=True)
            return []

    async def generate_trade_chart(self, order: Order) -> Optional[bytes]:
        """Generate chart for a trade with improved error handling"""
        try:
            # Get candles for chart
            candles = await self.get_candles_for_chart(
                order.symbol,
                order.timeframe
            )
            
            # If we got fewer than 3 candles, we can't make a good chart
            if len(candles) < 3:
                logger.warning(f"Insufficient data: Only {len(candles)} candles available for {order.symbol} {order.timeframe.value} chart - minimum 3 required")
                return None
                
            ref_price = self.reference_prices.get(order.symbol, {}).get(order.timeframe)
            
            # Attempt to generate chart with available candles
            try:
                return await self.chart_generator.generate_trade_chart(
                    candles,
                    order,
                    Decimal(str(ref_price)) if ref_price else None
                )
            except Exception as chart_error:
                logger.error(f"Chart generation error for {order.symbol}: {chart_error}")
                
                # If chart generation fails, try with fewer features as fallback
                try:
                    logger.info("Trying simplified chart generation")
                    return await self.chart_generator.generate_simple_chart(
                        candles,
                        order,
                        Decimal(str(ref_price)) if ref_price else None
                    )
                except Exception as simple_error:
                    logger.error(f"Simplified chart generation also failed: {simple_error}")
                    return None
                    
        except Exception as e:
            logger.error(f"Failed to prepare data for trade chart: {e}")
            return None

    async def get_historical_prices(self, symbol: str, days: int = 30) -> List[Dict]:
        """Get historical daily prices for a symbol"""
        try:
            # Calculate start time
            end_time = int(datetime.utcnow().timestamp() * 1000)
            start_time = int((datetime.utcnow() - timedelta(days=days)).timestamp() * 1000)
            
            await self.rate_limiter.acquire(weight=10)  # Higher weight for klines request
            
            # Get klines (daily candles)
            klines = await self.client.get_klines(
                symbol=symbol,
                interval='1d',
                startTime=start_time,
                endTime=end_time,
                limit=1000  # Maximum allowed
            )
            
            # Process the klines data
            results = []
            for k in klines:
                timestamp = datetime.fromtimestamp(k[0] / 1000)  # Convert ms to datetime
                results.append({
                    'timestamp': timestamp,
                    'price': Decimal(str(k[4])),  # Use closing price
                    'open': Decimal(str(k[1])),
                    'high': Decimal(str(k[2])),
                    'low': Decimal(str(k[3])),
                    'volume': Decimal(str(k[5]))
                })
                
            logger.info(f"Retrieved {len(results)} historical prices for {symbol}")
            return results
            
        except Exception as e:
            logger.error(f"Failed to get historical prices for {symbol}: {e}")
            return []

    async def get_current_price(self, symbol: str) -> float:
        """Get the current price for a symbol with enhanced validation"""
        try:
            # First check if it's a known invalid symbol
            if symbol in self.invalid_symbols:
                logger.debug(f"Skipping known invalid symbol: {symbol}")
                return None
                
            # Validate symbol format before sending API request
            if not self._is_valid_symbol_format(symbol):
                logger.warning(f"Invalid symbol format: {symbol}. Adding to invalid symbols list.")
                self.invalid_symbols.add(symbol)
                
                # Save to database if possible
                if self.mongo_client:
                    await self.mongo_client.save_invalid_symbol(symbol, "Invalid symbol format")
                return None
                
            await self.rate_limiter.acquire()
            ticker = await self.client.get_symbol_ticker(symbol=symbol)
            return float(ticker['price'])
        except BinanceAPIException as e:
            # Check specifically for invalid symbol error
            if e.code == -1121 or e.code == -1100:  # Add code -1100 for illegal character errors
                logger.warning(f"Invalid symbol detected: {symbol}. Adding to invalid symbols list.")
                self.invalid_symbols.add(symbol)
                
                # Save to database if possible
                if self.mongo_client:
                    await self.mongo_client.save_invalid_symbol(symbol, str(e))
            else:
                logger.error(f"Failed to get current price for {symbol}: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to get current price for {symbol}: {e}")
            return None

    def _get_quantity_precision(self, symbol: str) -> int:
        """Get the quantity precision for a symbol"""
        try:
            if symbol in self.symbol_info:
                return self.symbol_info[symbol]['baseAssetPrecision']
            return 8  # Default precision if symbol info not available
        except Exception as e:
            logger.error(f"Error getting quantity precision for {symbol}: {e}")
            return 8  # Default safe value

    def _get_price_precision(self, symbol: str) -> int:
        """Get the price precision for a symbol"""
        try:
            if symbol in self.symbol_info:
                return self.symbol_info[symbol]['quotePrecision']
            return 8  # Default precision if symbol info not available
        except Exception as e:
            logger.error(f"Error getting price precision for {symbol}: {e}")
            return 8  # Default safe value

    def _adjust_quantity_to_lot_size(self, symbol: str, quantity: Decimal) -> Decimal:
        """Adjust quantity to valid lot size"""
        try:
            if symbol not in self.symbol_info:
                return quantity  # Return unchanged if symbol info not available
                
            filters = self.symbol_info[symbol]['filters']
            if 'LOT_SIZE' in filters:
                lot_filter = filters['LOT_SIZE']
                min_qty = Decimal(str(lot_filter['minQty']))
                max_qty = Decimal(str(lot_filter['maxQty']))
                step_size = Decimal(str(lot_filter['stepSize']))
                
                # Adjust to step size
                if step_size != Decimal('0'):
                    decimal_places = abs(step_size.as_tuple().exponent)
                    quantity = (quantity // step_size) * step_size
                    quantity = quantity.quantize(Decimal('0.' + '0' * decimal_places))
                    
                # Ensure within limits
                quantity = max(min_qty, min(max_qty, quantity))
                
            return quantity
        except Exception as e:
            logger.error(f"Error adjusting quantity for {symbol}: {e}")
            return quantity

    async def calculate_fees(self, symbol: str, price: Decimal, quantity: Decimal, order_type: str = "spot", leverage: int = 1) -> Tuple[Decimal, str]:
        """Calculate trading fees for an order based on order type and leverage"""
        try:
            # Normalize order_type to lowercase
            order_type = order_type.lower() if isinstance(order_type, str) else "spot"
            
            # Get fee rate based on order type or use the symbol-specific rate if available
            if symbol in TRADING_FEES:
                fee_rate = Decimal(str(TRADING_FEES[symbol]))
            else:
                # Use order type specific fee rate
                fee_rate = Decimal(str(ORDER_TYPE_FEES.get(order_type, TRADING_FEES['DEFAULT'])))
            
            # Calculate trade value
            trade_value = price * quantity
            
            # For futures trades, account for leverage in fee calculation
            if order_type == "futures" and leverage > 1:
                # In futures trading, fees apply to the effective position size (with leverage)
                effective_value = trade_value * Decimal(str(leverage))
                fee_amount = effective_value * fee_rate
                logger.info(f"Calculated futures fees with leverage {leverage}x: Value=${float(trade_value):.2f}, Effective=${float(effective_value):.2f}")
            else:
                # Standard fee calculation for spot trades
                fee_amount = trade_value * fee_rate
            
            # Get base currency for fee asset
            base_currency = self.base_currency or "USDT"  # Default to USDT if base_currency not set
            
            # Default fee asset is the base currency for most trades
            fee_asset = base_currency
            
            logger.info(f"Calculated fees for {symbol} ({order_type}): {float(fee_amount):.4f} {fee_asset} (rate: {float(fee_rate)*100:.4f}%)")
            
            return fee_amount, fee_asset
        except Exception as e:
            logger.error(f"Error calculating fees: {e}")
            # Use base_currency for default fee asset if available
            base_currency = self.base_currency or "USDT"
            return Decimal('0'), base_currency

    async def get_historical_benchmark(self, symbol: str, days: int = 90) -> Dict:
        """Get historical performance data for a benchmark asset"""
        try:
            # For cryptocurrency benchmarks like BTC
            if symbol in ["BTCUSDT", "ETHUSDT"]:
                # Get historical prices
                prices = await self.get_historical_prices(symbol, days)
                if not prices or len(prices) < 2:
                    logger.warning(f"Not enough historical data for {symbol}")
                    return {}
                    
                # Calculate daily ROI percentages relative to first day
                base_price = float(prices[0]['price'])
                result = {}
                
                for price_data in prices:
                    date = price_data['timestamp'].strftime('%Y-%m-%d')
                    current_price = float(price_data['price'])
                    roi = ((current_price - base_price) / base_price) * 100
                    result[date] = roi
                    
                return result
                
            # For S&P 500 data using Yahoo scraper
            elif symbol == "SP500":
                try:
                    # Use Yahoo Finance scraper to get S&P 500 historical data
                    return await self.yahoo_scraper.get_sp500_data(days)
                except Exception as e:
                    logger.error(f"Error getting Yahoo S&P 500 data: {e}", exc_info=True)
                    logger.info("Falling back to simulated S&P 500 data")
                    return await self._get_simulated_sp500_data(days)
            else:
                logger.warning(f"Unsupported benchmark symbol: {symbol}")
                return {}
                
        except Exception as e:
            logger.error(f"Error getting historical benchmark data for {symbol}: {e}", exc_info=True)
            return {}
            
    async def _get_simulated_sp500_data(self, days: int = 90) -> Dict:
        """Generate simulated S&P 500 data when API is unavailable"""
        import numpy as np
        
        logger.info("Generating simulated S&P 500 data")
        today = datetime.utcnow()
        base_value = 4000.0  # Starting value
        daily_change = 0.05  # Average daily change percentage
        result = {}
        
        # Generate simulated S&P 500 performance data
        for day in range(days, -1, -1):
            date = (today - timedelta(days=day)).strftime('%Y-%m-%d')
            # Simulate some realistic movement with noise and slight upward trend
            random_factor = np.random.normal(0, 1) * daily_change
            base_value *= (1 + random_factor / 100)
            result[date] = ((base_value - 4000.0) / 4000.0) * 100
                
        logger.info(f"Generated {len(result)} days of simulated S&P 500 data")
        return result

    async def get_btc_ytd_performance(self) -> Dict[str, float]:
        """Get BTC year-to-date performance data"""
        try:
            current_year = datetime.now().year
            start_date = datetime(current_year, 1, 1)
            days_since_start = (datetime.now() - start_date).days
            
            # Get historical prices for BTC
            prices = await self.get_historical_prices("BTCUSDT", days_since_start + 10)  # Add buffer
            
            if not prices or len(prices) < 2:
                logger.warning("Not enough BTC historical data for YTD performance")
                return {}
                
            # Find the first trading day of the current year
            first_price = None
            ytd_data = {}
            
            for price_data in prices:
                date = price_data['timestamp']
                if date.year == current_year:
                    # Found first day of current year with data
                    if first_price is None:
                        first_price = float(price_data['price'])
                    
                    # Calculate percentage change from first day
                    current_price = float(price_data['price'])
                    change_pct = ((current_price - first_price) / first_price) * 100
                    
                    # Store with date string key
                    date_str = date.strftime('%Y-%m-%d')
                    ytd_data[date_str] = change_pct
            
            logger.info(f"Generated BTC YTD performance data with {len(ytd_data)} data points")
            return ytd_data
            
        except Exception as e:
            logger.error(f"Error getting BTC YTD performance: {e}")
            return {}

    async def generate_ytd_comparison_chart(self) -> Optional[bytes]:
        """Generate year-to-date comparison chart for BTC vs S&P 500"""
        try:
            # Get BTC year-to-date performance
            btc_data = await self.get_btc_ytd_performance()
            if not btc_data:
                logger.error("Failed to get BTC YTD data")
                return None
                
            # Get S&P 500 year-to-date performance
            sp500_data = await self.yahoo_scraper.get_ytd_data()
            if not sp500_data:
                logger.error("Failed to get S&P 500 YTD data")
                return None
                
            # Generate chart
            chart_bytes = await self.chart_generator.generate_ytd_comparison_chart(
                btc_data,
                sp500_data,
                datetime.now().year
            )
            
            return chart_bytes
            
        except Exception as e:
            logger.error(f"Error generating YTD comparison chart: {e}", exc_info=True)
            return None

    async def create_tp_sl_orders(self, order: Order) -> tuple:
        """Create take profit and stop loss orders for an existing order"""
        tp_order_id = None
        sl_order_id = None
        
        # Skip if no TP/SL is configured
        if self.default_tp_percentage <= 0 and self.default_sl_percentage <= 0:
            logger.info(f"No TP/SL configured, skipping for {order.symbol}")
            return None, None
            
        if not order.filled_at:
            logger.warning(f"Cannot create TP/SL for unfilled order {order.order_id}")
            return None, None
        
        try:
            # Calculate TP/SL prices based on entry price and direction
            is_long = not order.direction or order.direction == TradeDirection.LONG
            
            # For spot orders or long futures, TP is above entry, SL is below
            # For short futures, TP is below entry, SL is above
            tp_multiplier = 1 + (self.default_tp_percentage / 100) if is_long else 1 - (self.default_tp_percentage / 100)
            sl_multiplier = 1 - (self.default_sl_percentage / 100) if is_long else 1 + (self.default_sl_percentage / 100)
            
            # Calculate TP/SL prices
            tp_price = order.price * Decimal(str(tp_multiplier))
            sl_price = order.price * Decimal(str(sl_multiplier))
            
            # Round prices to appropriate precision
            price_precision = self._get_price_precision(order.symbol)
            tp_price = Decimal(str(round(tp_price, price_precision)))
            sl_price = Decimal(str(round(sl_price, price_precision)))
            
            logger.info(f"Calculated TP/SL for {order.symbol}: Entry={float(order.price)}, TP={float(tp_price)} ({self.default_tp_percentage}%), SL={float(sl_price)} ({self.default_sl_percentage}%)")
            
            # Create TP object if applicable
            if self.default_tp_percentage > 0:
                order.take_profit = TakeProfit(
                    price=tp_price,
                    percentage=self.default_tp_percentage,
                    status=TPSLStatus.PENDING
                )
                
                # For futures orders, place actual TP order
                if order.order_type == OrderType.FUTURES:
                    # Implementation for actual TP order placement would go here
                    logger.info(f"Placing TP order for {order.symbol} at {float(tp_price)}")
                    # tp_order_id = "tp_" + order.order_id  # In a real implementation, this would be the actual order ID
                    # order.take_profit.order_id = tp_order_id
            
            # Create SL object if applicable
            if self.default_sl_percentage > 0:
                order.stop_loss = StopLoss(
                    price=sl_price,
                    percentage=self.default_sl_percentage,
                    status=TPSLStatus.PENDING
                )
                
                # For futures orders, place actual SL order
                if order.order_type == OrderType.FUTURES:
                    # Implementation for actual SL order placement would go here
                    logger.info(f"Placing SL order for {order.symbol} at {float(sl_price)}")
                    # sl_order_id = "sl_" + order.order_id  # In a real implementation, this would be the actual order ID
                    # order.stop_loss.order_id = sl_order_id
            
            return tp_order_id, sl_order_id
            
        except Exception as e:
            logger.error(f"Error creating TP/SL orders for {order.symbol}: {e}")
            return None, None

    # Add method to check TP/SL triggers
    async def check_tp_sl_triggers(self, order: Order) -> Dict[str, bool]:
        """Check if take profit or stop loss levels have been triggered"""
        result = {'tp_triggered': False, 'sl_triggered': False}
        
        if not order or order.status != OrderStatus.FILLED:
            return result
            
        # Skip if already triggered
        if (order.take_profit and order.take_profit.status == TPSLStatus.TRIGGERED) and \
           (order.stop_loss and order.stop_loss.status == TPSLStatus.TRIGGERED):
            return result
            
        try:
            # Get current price
            current_price = await self.get_current_price(order.symbol)
            if not current_price:
                logger.warning(f"Failed to get current price for {order.symbol}")
                return result
                
            current_price_dec = Decimal(str(current_price))
            
            # Determine trade direction
            is_long = not order.direction or order.direction == TradeDirection.LONG
            
            # Check take profit
            if order.take_profit and order.take_profit.status == TPSLStatus.PENDING:
                tp_triggered = (current_price_dec >= order.take_profit.price) if is_long else (current_price_dec <= order.take_profit.price)
                
                if tp_triggered:
                    logger.info(f"🎯 TP triggered for {order.symbol}: Target={float(order.take_profit.price)}, Current={current_price}")
                    order.take_profit.status = TPSLStatus.TRIGGERED
                    order.take_profit.triggered_at = datetime.utcnow()
                    result['tp_triggered'] = True
            
            # Check stop loss
            if order.stop_loss and order.stop_loss.status == TPSLStatus.PENDING:
                sl_triggered = (current_price_dec <= order.stop_loss.price) if is_long else (current_price_dec >= order.stop_loss.price)
                
                if sl_triggered:
                    logger.info(f"⛔ SL triggered for {order.symbol}: Target={float(order.stop_loss.price)}, Current={current_price}")
                    order.stop_loss.status = TPSLStatus.TRIGGERED
                    order.stop_loss.triggered_at = datetime.utcnow()
                    result['sl_triggered'] = True
                    
            return result
            
        except Exception as e:
            logger.error(f"Error checking TP/SL for {order.symbol}: {e}")
            return result

    async def check_connection(self) -> dict:
        """Check connection to Binance and return status data for health checks"""
        try:
            # Test connection
            await self.client.ping()
            
            # Get server time to verify working connection
            time_resp = await self.client.get_server_time()
            server_time = datetime.fromtimestamp(time_resp['serverTime']/1000)
            
            # Get account info
            account = await self.client.get_account()
            balances = {
                asset['asset']: float(asset['free']) 
                for asset in account['balances'] 
                if float(asset['free']) > 0
            }
            
            # Default to USDT if base_currency is not specified
            base_cur = self.base_currency or 'USDT'
            base_balance = balances.get(base_cur, 0)
            
            return {
                "status": "connected",
                "server_time": server_time.isoformat(),
                "base_currency": base_cur,
                "base_balance": base_balance,
                "reserve_balance": self.reserve_balance,
                "balances": balances,
                "is_paused": self.telegram_bot.is_paused if self.telegram_bot else True,
                "invalid_symbols": list(self.invalid_symbols)
            }
        except Exception as e:
            logger.error(f"Connection check failed: {e}")
            return {
                "status": "error",
                "error": str(e)
            }

    async def check_symbol_validity(self, symbol: str) -> bool:
        """Check if a symbol is valid on Binance with format pre-validation"""
        # Return early if already known to be invalid
        if symbol in self.invalid_symbols:
            logger.debug(f"Symbol {symbol} previously identified as invalid, skipping check")
            return False
            
        # First check symbol format before making an API call
        if not self._is_valid_symbol_format(symbol):
            logger.warning(f"Invalid symbol format: {symbol}")
            self.invalid_symbols.add(symbol)
            
            # Save to database if possible
            if self.mongo_client:
                await self.mongo_client.save_invalid_symbol(symbol, "Invalid symbol format")
            return False
            
        try:
            # Try to get symbol ticker, which will fail if symbol is invalid
            await self.rate_limiter.acquire()
            await self.client.get_symbol_ticker(symbol=symbol)
            return True
        except BinanceAPIException as e:
            if e.code == -1121 or e.code == -1100:  # Add code -1100 for illegal character errors
                logger.warning(f"Invalid symbol detected: {symbol}")
                self.invalid_symbols.add(symbol)
                
                # Save to database if possible
                if self.mongo_client:
                    await self.mongo_client.save_invalid_symbol(symbol, str(e))
                return False
            else:
                # For other errors, treat as a temporary issue
                logger.error(f"Error checking symbol {symbol}: {e}")
                return True  # Consider valid for now, in case of temporary API issues
        except Exception as e:
            logger.error(f"Unexpected error checking symbol {symbol}: {e}")
            return True  # Consider valid for now
            
    async def filter_valid_symbols(self, symbols: List[str]) -> List[str]:
        """Filter out invalid symbols from a list with format pre-validation"""
        valid_symbols = []
        for symbol in symbols:
            # First check format without API call
            if not self._is_valid_symbol_format(symbol):
                logger.debug(f"Filtering out invalid symbol format: {symbol}")
                
                # Add to invalid symbols list
                if symbol not in self.invalid_symbols:
                    self.invalid_symbols.add(symbol)
                    if self.mongo_client:
                        await self.mongo_client.save_invalid_symbol(symbol, "Invalid symbol format")
                continue
                
            # Then check validity with API
            if await self.check_symbol_validity(symbol):
                valid_symbols.append(symbol)
                
        filtered_count = len(symbols) - len(valid_symbols)
        if filtered_count > 0:
            logger.info(f"Filtered out {filtered_count} invalid symbols, {len(valid_symbols)} symbols remaining")
        return valid_symbols

    def _is_valid_symbol_format(self, symbol: str) -> bool:
        """Check if symbol format is valid according to Binance requirements"""
        return bool(self.valid_symbol_pattern.match(symbol))

    # ...rest of existing code...
