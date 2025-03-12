from binance.client import AsyncClient
from binance.exceptions import BinanceAPIException
from decimal import Decimal
from datetime import datetime, timedelta
import asyncio
import logging
from typing import Dict, List, Optional, Tuple, Union
import aiohttp
import json
from ..types.models import Order, OrderStatus, TimeFrame, OrderType, OrderDirection, MarginMode, PositionSide  # Add OrderType and OrderDirection
from ..utils.rate_limiter import RateLimiter
from ..types.constants import PRECISION, MIN_NOTIONAL, TIMEFRAME_INTERVALS, TRADING_FEES, ORDER_TYPE_FEES
from ..utils.chart_generator import ChartGenerator
from ..utils.yahoo_scrapooooor_sp500 import YahooSP500Scraper  # Import the new Yahoo scraper
from ..database.mongo_client import MongoClient
from .binance_futures import BinanceFuturesClient  # Import the new futures client
from .tp_sl_manager import TPSLManager  # Import the new TP/SL manager
import time
import pandas as pd
import numpy as np
from binance.um_futures import UMFutures  # Add UMFutures import

logger = logging.getLogger(__name__)

class BinanceClient:
    def __init__(self, api_key: str, api_secret: str, telegram_bot=None, mongo_client=None, config=None, testnet: bool = True):
        """Initialize Binance client with API credentials"""
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.telegram_bot = telegram_bot
        self.mongo_client = mongo_client
        self.config = config
        self.client = None
        self.exchange_info = None
        self.symbol_info = {}
        self.is_initialized = False
        self.is_futures_enabled = False
        self.futures_client = None
        self.tp_sl_manager = None
        
        # Initialize trading parameters from config
        if config:
            # Set reserve balance
            self.reserve_balance = config['trading'].get('reserve_balance', 500)
            
            # Set order amount
            self.order_amount = config['trading'].get('order_amount', 100)
            
            # Set TP/SL configuration
            self.tp_sl_enabled = config['trading'].get('tp_sl_enabled', False)
            
            # Parse TP percentage - handle string with % sign
            tp_config = config['trading'].get('tp_percentage', 5.0)
            if isinstance(tp_config, str) and tp_config.endswith('%'):
                tp_config = tp_config[:-1]  # Remove % sign
            self.default_tp_percentage = float(tp_config)
            
            # Parse SL percentage - handle string with % sign
            sl_config = config['trading'].get('sl_percentage', 3.0)
            if isinstance(sl_config, str) and sl_config.endswith('%'):
                sl_config = sl_config[:-1]  # Remove % sign
            self.default_sl_percentage = float(sl_config)
            
            # Set lower entries configuration
            self.only_lower_entries = config['trading'].get('only_lower_entries', True)
            
            # Set futures-specific configuration
            futures_config = config['trading'].get('futures', {})
            self.default_leverage = futures_config.get('default_leverage', 3)
            self.default_margin_mode = futures_config.get('default_margin_mode', 'isolated')
            
            # Validate futures settings
            if self.default_leverage < 1:
                logger.warning(f"Invalid leverage value {self.default_leverage}, setting to minimum of 1")
                self.default_leverage = 1
            elif self.default_leverage > 5:
                logger.warning(f"Leverage exceeds maximum of 5x, capping at 5")
                self.default_leverage = 5
                
            if self.default_margin_mode not in ['isolated', 'cross']:
                logger.warning(f"Invalid margin mode {self.default_margin_mode}, setting to 'isolated'")
                self.default_margin_mode = 'isolated'
        else:
            # Default values if no config provided
            self.reserve_balance = 500
            self.order_amount = 100
            self.tp_sl_enabled = False
            self.default_tp_percentage = 5.0
            self.default_sl_percentage = 3.0
            self.only_lower_entries = True
            self.default_leverage = 3
            self.default_margin_mode = 'isolated'
            
        # Initialize rate limiting
        self.request_count = 0
        self.last_request_time = time.time()
        self.max_requests_per_minute = 1200  # Default Binance rate limit
        
        self.reference_prices = {}
        self.triggered_thresholds = {}
        self.rate_limiter = RateLimiter()
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
        self.chart_generator = ChartGenerator()
        self.trading_enabled = True
        self.last_balance = None
        self.last_balance_check = None
        self.reserve_balance = 500  # Default reserve balance
        self.futures_client = None  # Will be initialized with the main client
        self.tp_sl_manager = None  # Will be initialized after client initialization
        
        # TP/SL configuration
        self.tp_sl_enabled = False
        self.default_tp_percentage = 5.0
        self.default_sl_percentage = 3.0
        
        # Lower Entry Price Protection
        self.only_lower_entries = True  # Default to enabled
        
        # Set reserve balance from config if available
        if config and 'trading' in config:
            try:
                self.reserve_balance = float(config['trading'].get('reserve_balance', 500))
                logger.info(f"Set reserve balance to ${self.reserve_balance:,.2f}")
                
                # Set TP/SL configuration from config
                self.tp_sl_enabled = config['trading'].get('tp_sl_enabled', False)
                
                # Parse TP percentage - handle string with % sign
                tp_config = config['trading'].get('tp_percentage', 5.0)
                if isinstance(tp_config, str) and tp_config.endswith('%'):
                    tp_config = tp_config[:-1]  # Remove % sign
                self.default_tp_percentage = float(tp_config)
                
                # Parse SL percentage - handle string with % sign
                sl_config = config['trading'].get('sl_percentage', 3.0)
                if isinstance(sl_config, str) and sl_config.endswith('%'):
                    sl_config = sl_config[:-1]  # Remove % sign
                self.default_sl_percentage = float(sl_config)
                
                # Set only_lower_entries from config
                self.only_lower_entries = config['trading'].get('only_lower_entries', True)
                
                logger.info(f"TP/SL configuration: Enabled={self.tp_sl_enabled}, TP={self.default_tp_percentage}%, SL={self.default_sl_percentage}%")
                logger.info(f"Lower Entry Price Protection: {self.only_lower_entries}")
            except (ValueError, TypeError) as e:
                logger.error(f"Error setting configuration from config: {e}")
        
        # Set API environment info
        environment = "TESTNET" if testnet else "MAINNET"
        logger.info(f"[INIT] Using Binance {environment} API")
        
        # Set reserve balance and base currency directly from config
        self.base_currency = None
        if config and 'trading' in config:
            self.base_currency = config['trading'].get('base_currency', 'USDT')
            logger.info(f"[INIT] Config loaded directly: Base Currency={self.base_currency}")
        
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
                    f"Current Balance: ${float(current_balance):.2f}\n"
                    f"Reserve Balance: ${self.reserve_balance:.2f}\n"
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
                f"Current Balance: ${float(current_balance):.2f}\n"
                f"Reserve Balance: ${self.reserve_balance:.2f}"
            )
            return True

        except Exception as e:
            logger.error(f"Error checking initial balance: {e}")
            return False

    async def initialize(self):
        """Initialize the Binance client and fetch exchange information"""
        try:
            # Initialize Binance client
            if self.testnet:
                logger.info("Initializing Binance client in TESTNET mode")
                self.client = AsyncClient(self.api_key, self.api_secret, testnet=True)
            else:
                logger.info("Initializing Binance client in PRODUCTION mode")
                self.client = AsyncClient(self.api_key, self.api_secret, testnet=False)
                
            # Initialize futures client if API keys are provided
            if self.api_key and self.api_secret:
                try:
                    self.futures_client = BinanceFuturesClient(self.client)
                    self.is_futures_enabled = True
                    logger.info("Futures trading enabled")
                except Exception as e:
                    logger.error(f"Failed to initialize futures client: {e}")
                    self.is_futures_enabled = False
            
            # Fetch exchange information
            await self._update_exchange_info()
            
            # Initialize TP/SL manager if enabled
            if self.tp_sl_enabled:
                logger.info(f"TP/SL manager initialized with TP={self.default_tp_percentage}%, SL={self.default_sl_percentage}%")
                self.tp_sl_manager = TPSLManager(self, self.mongo_client)
        else:
                logger.info("TP/SL is disabled")
                
            # Log futures settings if enabled
            if self.is_futures_enabled:
                logger.info(f"Futures settings: Leverage={self.default_leverage}x, Margin Mode={self.default_margin_mode.upper()}")
            
            self.is_initialized = True
            logger.info("Binance client initialized successfully")
            
            # Check initial balance
        await self.check_initial_balance()
            
            return True
        except Exception as e:
            logger.error(f"Error initializing Binance client: {e}")
            return False

    async def restore_threshold_state(self):
        """Restore triggered thresholds from database on startup"""
        try:
            logger.info("Restoring threshold state from database...")
            threshold_states = await self.mongo_client.get_triggered_thresholds()
            
            restored_count = 0
            restored_info = {}  # Change to dictionary to properly track thresholds by symbol/timeframe
            
            # Process each threshold state and update the internal dictionary
            for state in threshold_states:
                symbol = state.get("symbol")
                timeframe_str = state.get("timeframe")
                thresholds = state.get("thresholds", [])
                
                # Skip if any required data is missing
                if not symbol or not timeframe_str:
                    continue
                    
                # Ensure pair is in our tracking dictionary
                if symbol not in self.triggered_thresholds:
                    self.triggered_thresholds[symbol] = {
                        'daily': set(),
                        'weekly': set(),
                        'monthly': set()
                    }
                
                # Convert thresholds to a set of floats and update internal state
                threshold_set = set(float(t) for t in thresholds)
                self.triggered_thresholds[symbol][timeframe_str] = threshold_set
                
                # Track restored thresholds in a structured way for notifications
                if symbol not in restored_info:
                    restored_info[symbol] = {}
                    
                if threshold_set:  # Only include timeframes with thresholds
                    timeframe_enum = TimeFrame(timeframe_str)
                    restored_info[symbol][timeframe_enum] = threshold_set
                    restored_count += len(threshold_set)
                    logger.info(f"Restored {len(threshold_set)} thresholds for {symbol} {timeframe_str}")
            
            logger.info(f"Restored {restored_count} thresholds across {len(restored_info)} symbols")
            self.restored_threshold_info = restored_info
            return restored_info
            
        except Exception as e:
            logger.error(f"Failed to restore threshold state: {e}")
            self.restored_threshold_info = {}
            return {}

    async def close(self):
        if self.client:
            await self.client.close_connection()
            
    async def check_timeframe_reset(self, timeframe: TimeFrame) -> bool:
        """Check if timeframe needs to be reset and return True if reset occurred"""
        now = datetime.utcnow()
        interval = TIMEFRAME_INTERVALS[timeframe.value.upper()]
        
        if now - self.last_reset[timeframe] >= interval:
            logger.info(f"Resetting {timeframe.value} thresholds")
            
            # Prepare reset data
            reset_data = {
                "timeframe": timeframe,
                "prices": []
            }
            
            # Get opening prices for all symbols
            for symbol in self.reference_prices.keys():
                ticker = await self.client.get_symbol_ticker(symbol=symbol)
                current_price = float(ticker['price'])
                ref_price = await self.get_reference_price(symbol, timeframe)
                
                price_data = {
                    "symbol": symbol,
                    "current_price": current_price,
                    "reference_price": ref_price,
                    "price_change": ((current_price - ref_price) / ref_price * 100) if ref_price else 0
                }
                reset_data["prices"].append(price_data)
                
                self.reference_prices[symbol][timeframe] = ref_price or current_price
                
                # Clear triggered thresholds
                if symbol in self.triggered_thresholds:
                    self.triggered_thresholds[symbol][timeframe] = set()
                    
                    # Clear in database as well
                    if self.telegram_bot and self.telegram_bot.mongo_client:
                        await self.telegram_bot.mongo_client.save_triggered_threshold(
                            symbol, timeframe.value, []
                        )
                    
            self.last_reset[timeframe] = now
            
            # Send notification via telegram bot if available
            if self.telegram_bot:
                await self.telegram_bot.send_timeframe_reset_notification(reset_data)
            
            return True
            
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
        """Get reference price for symbol at timeframe"""
        try:
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
                
        except Exception as e:
            logger.error(f"Failed to get reference price for {symbol} {timeframe.value}: {e}", exc_info=True)
            return None

    async def update_reference_prices(self, symbols: List[str]):
        """Update reference prices for all timeframes"""
        try:
            for symbol in symbols:
                if (symbol not in self.reference_prices):
                    self.reference_prices[symbol] = {}
                    self.triggered_thresholds[symbol] = {tf: [] for tf in TimeFrame}

                # Get current price first
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

                # Add small delay between symbols
                await asyncio.sleep(0.1)

        except Exception as e:
            logger.error(f"Failed to update prices: {e}", exc_info=True)
            raise
            
    async def check_thresholds(self, symbol: str, timeframe: TimeFrame) -> List[float]:
        """Check if any thresholds are triggered for the given symbol and timeframe"""
        try:
            # Skip if trading is paused
            if hasattr(self, 'telegram_bot') and self.telegram_bot.is_paused:
                return []

            # Get current price and reference price
            current_price = await self.get_current_price(symbol)
            reference_price = await self.get_reference_price(symbol, timeframe)
            
            if not reference_price:
                logger.warning(f"No reference price for {symbol} on {timeframe.value}")
                return []
                
            # Calculate raw price change percentage (no absolute value)
            price_change = ((current_price - reference_price) / reference_price) * 100
            
            # Debug log for price change - show direction
            logger.debug(f"{symbol} {timeframe.value} price change: {price_change:.2f}%")
            
            # Get thresholds for the timeframe from config
            timeframe_thresholds = self.config['trading']['thresholds'][timeframe.value]
            
            # Get already triggered thresholds for this symbol and timeframe
            triggered = set()
            if symbol in self.triggered_thresholds and timeframe.value in self.triggered_thresholds[symbol]:
                triggered = self.triggered_thresholds[symbol][timeframe.value]
            
            # Check which thresholds are triggered but not yet processed
            # Only trigger on price decreases (negative price_change)
            newly_triggered = []
            for threshold in timeframe_thresholds:
                # Only trigger when price_change is negative (price decrease) and exceeds threshold
                if price_change < 0 and abs(price_change) >= threshold and threshold not in triggered:
                    logger.info(f"Threshold triggered for {symbol}: {threshold}% on {timeframe.value} (Price decrease of {price_change:.2f}%)")
                    newly_triggered.append(threshold)
                    
                    # Mark threshold as triggered
                    self.mark_threshold_triggered(symbol, timeframe, threshold)
                    
                    # Send notification if telegram bot is available
                    if hasattr(self, 'telegram_bot') and self.telegram_bot:
                        await self.telegram_bot.send_threshold_notification(
                            symbol, timeframe, threshold, 
                            current_price, reference_price, price_change
                        )
                    
            return newly_triggered
            
        except Exception as e:
            logger.error(f"Error checking thresholds for {symbol} on {timeframe.value}: {e}")
            return []

    def mark_threshold_triggered(self, symbol: str, timeframe: TimeFrame, threshold: float):
        """Mark a threshold as triggered in memory and persist to database"""
        try:
            # Initialize if needed
            if symbol not in self.triggered_thresholds:
                self.triggered_thresholds[symbol] = {}
            if timeframe not in self.triggered_thresholds[symbol]:
                self.triggered_thresholds[symbol][timeframe] = []
                
            # Add threshold if not already in list
            if threshold not in self.triggered_thresholds[symbol][timeframe]:
                self.triggered_thresholds[symbol][timeframe].append(threshold)
                logger.info(f"Marking threshold {threshold}% as triggered for {symbol} {timeframe.value}")
                
            # Persist to database in background
            asyncio.create_task(self.mongo_client.save_triggered_threshold(
                symbol, timeframe, threshold
            ))
        except Exception as e:
            logger.error(f"Error marking threshold triggered: {e}")
            
    async def reset_timeframe_thresholds(self, timeframe_str: str):
        """Reset all thresholds for a specific timeframe"""
        try:
            # Convert string to TimeFrame enum if it's not already
            if isinstance(timeframe_str, str):
                timeframe = TimeFrame(timeframe_str)
            else:
                timeframe = timeframe_str
            
            logger.info(f"Resetting all thresholds for {timeframe.value} timeframe...")
            
            # Get reference prices for all symbols
            reset_data = {
                "timeframe": timeframe,
                "prices": []
            }
            
            # Track which symbols were reset for logging
            reset_symbols = []
            
            for symbol in self.config['trading']['pairs']:
                # Get current price
                current_price = await self.get_current_price(symbol)
                
                # Store current price as new reference
                if symbol not in self.reference_prices:
                    self.reference_prices[symbol] = {}
                
                # Update reference price to current price
                old_reference = self.reference_prices.get(symbol, {}).get(timeframe)
                self.reference_prices[symbol][timeframe] = current_price
                
                # Calculate price change from previous reference (if exists)
                price_change = 0
                if old_reference:
                    price_change = ((current_price - old_reference) / old_reference) * 100
                
                # Add to reset data for notification
                reset_data["prices"].append({
                    "symbol": symbol,
                    "current_price": current_price,
                    "reference_price": current_price,  # New reference is current price
                    "previous_reference": old_reference,
                    "price_change": price_change
                })
                
                # Check if symbol had triggered thresholds to reset
                if symbol in self.triggered_thresholds and timeframe.value in self.triggered_thresholds[symbol]:
                    # Only log if there were thresholds to clear
                    if self.triggered_thresholds[symbol][timeframe.value]:
                        reset_symbols.append(symbol)
                        logger.info(f"Cleared thresholds for {symbol} {timeframe.value}: {list(self.triggered_thresholds[symbol][timeframe.value])}")
                    
                    # Clear triggered thresholds for this symbol and timeframe
                    self.triggered_thresholds[symbol][timeframe.value] = set()
            
            # Persist changes to database
            await self.mongo_client.save_reference_prices(self.reference_prices)
            await self.mongo_client.clear_triggered_thresholds(timeframe)
            
            # Send notification if telegram bot is available
            if reset_data["prices"] and hasattr(self, 'telegram_bot') and self.telegram_bot:
                await self.telegram_bot.send_timeframe_reset_notification(reset_data)
                
            if reset_symbols:
                logger.info(f"Reset thresholds for {len(reset_symbols)} symbols: {', '.join(reset_symbols)}")
            else:
                logger.info(f"No triggered thresholds were found to reset for {timeframe.value} timeframe")
                
            return True
            
        except Exception as e:
            logger.error(f"Error resetting {timeframe_str} thresholds: {e}", exc_info=True)
            return False

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

            # Get current balance in base currency (USDT)
            current_balance = await self.get_balance(self.base_currency)
            logger.info(f"[RESERVE CHECK] Current balance: ${float(current_balance):,.2f}")
            logger.info(f"[RESERVE CHECK] Reserve balance: ${float(self.reserve_balance):,.2f}")
            
            # Get sum of pending orders
            pending_orders_value = Decimal('0')
            if self.telegram_bot and self.telegram_bot.mongo_client:
                cursor = self.telegram_bot.mongo_client.orders.find({"status": "pending"})
                async for order in cursor:
                    pending_orders_value += (Decimal(str(order['price'])) * Decimal(str(order['quantity'])))

            # Calculate remaining balance after pending orders
            available_balance = float(current_balance - pending_orders_value)
            remaining_after_order = available_balance - order_amount

            logger.info(f"[RESERVE CHECK] Available after pending: ${available_balance:,.2f}")
            logger.info(f"[RESERVE CHECK] Remaining after order: ${remaining_after_order:,.2f}")
            logger.info(f"[RESERVE CHECK] Required reserve: ${float(self.reserve_balance):,.2f}")

            # Check if remaining balance would be above reserve
            return remaining_after_order >= self.reserve_balance

        except Exception as e:
            logger.error(f"[RESERVE CHECK] Error checking reserve balance: {e}")
            return False

    async def place_limit_buy_order(self, symbol: str, amount: float, 
                                  threshold: Optional[float] = None,
                                  timeframe: Optional[TimeFrame] = None,
                                  is_manual: bool = False,
                                  order_type: str = "spot",
                                  leverage: int = None,
                                  direction: OrderDirection = OrderDirection.LONG,
                                  tp_percentage: Optional[float] = None,
                                  sl_percentage: Optional[float] = None) -> Order:
        """Place a limit buy order with the specified parameters"""
        try:
            # Use default leverage if not specified
            if leverage is None and order_type.lower() == "futures":
                leverage = self.default_leverage
                
            # Use default margin mode for futures orders
            margin_mode = self.default_margin_mode if order_type.lower() == "futures" else None
            
            # Get current price
            current_price = await self.get_current_price(symbol)
            if not current_price:
                logger.error(f"Failed to get current price for {symbol}")
                return None
                
            # Calculate price based on threshold
            if threshold:
                # For spot orders or long futures, we buy below current price
                if order_type.lower() == "spot" or direction == OrderDirection.LONG:
                    price = current_price * (1 - threshold / 100)
                # For short futures, we sell above current price
                else:
                    price = current_price * (1 + threshold / 100)
            else:
                # Use current price if no threshold specified
                price = current_price
                
            # Convert to Decimal for precision
            price_decimal = Decimal(str(price))
            
            # Calculate quantity based on amount and price
            quantity = Decimal(str(amount)) / price_decimal
            
            # Adjust quantity to lot size
            quantity = self._adjust_quantity_to_lot_size(symbol, quantity)
            
            # Calculate fees
            fee, fee_currency = await self.calculate_fees(
                symbol, price_decimal, quantity, order_type, leverage
            )
            
            # Create order object
            order = Order(
                order_id=None,  # Will be set after order is placed
                symbol=symbol,
                price=price_decimal,
                quantity=quantity,
                order_type=OrderType.SPOT if order_type.lower() == "spot" else OrderType.FUTURES,
                status=OrderStatus.PENDING,
                created_at=datetime.utcnow(),
                filled_at=None,
                cancelled_at=None,
                threshold=threshold,
                timeframe=timeframe,
                reference_price=Decimal(str(current_price)) if current_price else None,
                is_manual=is_manual,
                fee=fee,
                fee_currency=fee_currency,
                leverage=leverage,
                direction=direction,
                margin_mode=margin_mode,
                position_side="BOTH"  # Default to BOTH for now
            )
            
            # Set TP/SL if provided
            if self.tp_sl_enabled:
                if tp_percentage is not None:
                    # Calculate TP price based on direction
                    if order.order_type == OrderType.FUTURES and order.direction == OrderDirection.SHORT:
                        # For short positions, TP is below entry
                        tp_price = price_decimal * (1 - tp_percentage / 100)
                    else:
                        # For long positions and spot, TP is above entry
                        tp_price = price_decimal * (1 + tp_percentage / 100)
                    
                    # Set TP attributes
                    order.tp_price = tp_price
                    order.tp_percentage = tp_percentage
                
                if sl_percentage is not None:
                    # Calculate SL price based on direction
                    if order.order_type == OrderType.FUTURES and order.direction == OrderDirection.SHORT:
                        # For short positions, SL is above entry
                        sl_price = price_decimal * (1 + sl_percentage / 100)
                    else:
                        # For long positions and spot, SL is below entry
                        sl_price = price_decimal * (1 - sl_percentage / 100)
                    
                    # Set SL attributes
                    order.sl_price = sl_price
                    order.sl_percentage = sl_percentage
            
            # Place the order
            if order.order_type == OrderType.SPOT:
                # Place spot order
                params = {
                    'symbol': symbol,
                    'side': 'BUY',
                    'type': 'LIMIT',
                    'timeInForce': 'GTC',
                    'quantity': float(quantity),
                    'price': float(price_decimal)
                }
                
                response = await self.client.create_order(**params)
                order.order_id = response['orderId']
                
            else:
                # Place futures order
                if not self.is_futures_enabled:
                    logger.error("Futures trading is not enabled")
                    return None
                
                # Set leverage for the symbol
                try:
                    await self.set_leverage(symbol, leverage)
                except Exception as e:
                    logger.error(f"Error setting leverage for {symbol}: {e}")
                
                # Set margin mode for the symbol
                try:
                    await self.set_margin_type(symbol, margin_mode)
                except Exception as e:
                    logger.error(f"Error setting margin mode for {symbol}: {e}")
                
                # Determine side based on direction
                side = 'BUY' if direction == OrderDirection.LONG else 'SELL'
                
                # Place futures order
                params = {
                    'symbol': symbol,
                    'side': side,
                    'type': 'LIMIT',
                    'timeInForce': 'GTC',
                    'quantity': float(quantity),
                    'price': float(price_decimal),
                    'reduceOnly': False,
                    'newOrderRespType': 'RESULT'
                }
                
                response = await self._make_request('POST', '/fapi/v1/order', params, is_futures=True, signed=True)
                order.order_id = response['orderId']
            
            logger.info(f"Placed {order.order_type.value} {'LONG' if direction == OrderDirection.LONG else 'SHORT'} order for {symbol} at {float(price_decimal):.2f}")
            
            return order
            
        except Exception as e:
            logger.error(f"Error placing limit buy order: {e}")
            return None

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an order on Binance"""
        try:
            # First, check if this is a futures order
            order_doc = await self.mongo_client.orders.find_one({"order_id": order_id})
            
            if order_doc and order_doc.get("order_type") == OrderType.FUTURES.value:
                # Use futures client to cancel
                if self.futures_client:
                    return await self.futures_client.cancel_order(symbol, order_id)
                else:
                    logger.error("Futures client not initialized")
                    return False
            
            # Otherwise, cancel spot order
            response = await self.client.cancel_order(
                symbol=symbol,
                orderId=order_id
            )
            
            if response and 'orderId' in response:
                logger.info(f"Cancelled order {order_id} for {symbol}")
            return True
                
            return False
        except BinanceAPIException as e:
            # If order does not exist, consider it cancelled
            if e.code == -2011:  # "Unknown order sent."
                logger.warning(f"Order {order_id} already cancelled or filled")
                return True
                
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False
            
    async def check_order_status(self, symbol: str, order_id: str) -> Optional[OrderStatus]:
        """Check the status of an order"""
        try:
            # First, check if this is a futures order
            order_doc = await self.mongo_client.orders.find_one({"order_id": order_id})
            
            if order_doc and order_doc.get("order_type") == OrderType.FUTURES.value:
                # Use futures client to check status
                if self.futures_client:
                    return await self.futures_client.get_order_status(symbol, order_id)
                else:
                    logger.error("Futures client not initialized")
                    return None
            
            # Otherwise, check spot order
            order = await self.client.get_order(
                symbol=symbol,
                orderId=order_id
            )
            
            if not order:
                return None
                
            status = order.get('status', '')
            
            if status == 'FILLED':
                return OrderStatus.FILLED
            elif status == 'CANCELED':
                return OrderStatus.CANCELLED
            elif status == 'REJECTED':
                return OrderStatus.CANCELLED
            elif status == 'EXPIRED':
                return OrderStatus.CANCELLED
            else:
            return OrderStatus.PENDING
                
        except BinanceAPIException as e:
            logger.error(f"Failed to check order status for {order_id}: {e}")
            return None
            
    async def get_balance(self, symbol: str = 'USDT') -> Decimal:
        """Get balance of specific asset"""
        try:
            # Get both spot and futures balances
            spot_balance = Decimal('0')
            futures_balance = Decimal('0')
            
            # Get spot balance
            account = await self.client.get_account()
            if account and 'balances' in account:
            for balance in account['balances']:
                    if balance['asset'] == symbol:
                        spot_balance = Decimal(balance['free']) + Decimal(balance['locked'])
                        break
            
            # Get futures balance if futures client is initialized
            if self.futures_client:
                futures_balance = await self.futures_client.get_balance(symbol)
            
            # Return combined balance
            total_balance = spot_balance + futures_balance
            
            # Update last balance
            self.last_balance = total_balance
            self.last_balance_check = datetime.now()
            
            return total_balance
            
        except Exception as e:
            logger.error(f"Failed to get balance: {e}")
            return Decimal('0')
            
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

    def mark_threshold_triggered(self, pair: str, timeframe: str, threshold: float):
        """Mark a threshold as triggered and persist to database"""
        # Ensure symbol exists in dictionary
        if pair not in self.triggered_thresholds:
            self.triggered_thresholds[pair] = {
                'daily': set(),
                'weekly': set(),
                'monthly': set()
            }
            
        # Ensure timeframe exists in the symbol's dictionary
        if timeframe not in self.triggered_thresholds[pair]:
            self.triggered_thresholds[pair][timeframe] = set()
        
        # Skip if already triggered
        if threshold in self.triggered_thresholds[pair][timeframe]:
            logger.debug(f"Threshold {threshold}% already triggered for {pair} {timeframe}")
            return False
        
        # Mark as triggered
        self.triggered_thresholds[pair][timeframe].add(threshold)
        
        # Save to database - create task but wait for completion to ensure it's saved
        try:
            logger.info(f"Marking threshold {threshold}% as triggered for {pair} {timeframe}")
            if self.mongo_client:
                asyncio.create_task(
                    self.mongo_client.save_triggered_threshold(
                        pair, 
                        timeframe, 
                        list(self.triggered_thresholds[pair][timeframe])
                    )
                )
            return True
        except Exception as e:
            logger.error(f"Failed to persist threshold {threshold}% for {pair} {timeframe}: {e}")
            return False

    async def reset_timeframe_thresholds(self, timeframe: str):
        """Reset thresholds for a specific timeframe and persist to database"""
        try:
            # Reset in memory
            for symbol in self.triggered_thresholds:
                # Ensure timeframe exists in the symbol's dictionary
                if timeframe not in self.triggered_thresholds[symbol]:
                    self.triggered_thresholds[symbol][timeframe] = set()
                else:
                    self.triggered_thresholds[symbol][timeframe] = set()
            
            # Reset in database
            if self.mongo_client:
                await self.mongo_client.reset_timeframe_thresholds(timeframe)
            
            logger.info(f"Reset all thresholds for timeframe: {timeframe}")
            return True
        except Exception as e:
            logger.error(f"Failed to reset thresholds for {timeframe}: {e}")
            return False

    async def get_current_price(self, symbol: str) -> float:
        """
        Get current price for a symbol
        
        Args:
            symbol: Trading pair symbol (e.g. BTCUSDT)
            
        Returns:
            Current price as float
        """
        try:
            # Get ticker price
            ticker = await self.client.get_symbol_ticker(symbol=symbol)
            price = float(ticker['price'])
            
            logger.debug(f"Current price for {symbol}: {price}")
            return price
            
        except Exception as e:
            logger.error(f"Error getting current price for {symbol}: {e}")
            return 0.0

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
        """Calculate fees for a trade"""
        try:
            # Get fee rate based on order type
            if order_type == "futures":
                fee_rate = Decimal(str(ORDER_TYPE_FEES.get('futures', TRADING_FEES.get('FUTURES', 0.002))))
                
                # For futures, fee is on the notional value, not the margin
                notional_value = price * quantity
                fee = notional_value * fee_rate
                
            else:  # spot
                fee_rate = Decimal(str(ORDER_TYPE_FEES.get('spot', TRADING_FEES.get('SPOT', 0.001))))
                fee = price * quantity * fee_rate
            
            # Return fee and currency
            return fee, 'USDT'
            
        except Exception as e:
            logger.error(f"Error calculating fees: {e}")
            return Decimal('0'), 'USDT'

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

    async def get_position_info(self, symbol: str) -> Dict:
        """Get current position information for a futures symbol"""
        try:
            # Check if we're in futures mode
            if not self.futures_client:
                logger.warning("Futures trading is not enabled")
                return {}
                
            # Make API call to get position information
            endpoint = "/fapi/v2/positionRisk"
            params = {"symbol": symbol}
            
            response = await self._make_request("GET", endpoint, params, is_futures=True, signed=True)
            
            if response and isinstance(response, list) and len(response) > 0:
                position = response[0]
                
                # Calculate ROE (Return on Equity)
                entry_price = float(position.get("entryPrice", 0))
                mark_price = float(position.get("markPrice", 0))
                leverage = float(position.get("leverage", 1))
                position_amt = float(position.get("positionAmt", 0))
                
                # Skip if no position
                if position_amt == 0 or entry_price == 0:
                    return {}
                
                # Calculate ROE based on position direction
                if position_amt > 0:  # Long position
                    roe = ((mark_price / entry_price) - 1) * 100 * leverage
                else:  # Short position
                    roe = ((entry_price / mark_price) - 1) * 100 * leverage
                
                return {
                    "symbol": position.get("symbol"),
                    "position_amount": position_amt,
                    "entry_price": entry_price,
                    "mark_price": mark_price,
                    "unrealized_pnl": float(position.get("unRealizedProfit", 0)),
                    "leverage": float(position.get("leverage", 1)),
                    "margin_type": position.get("marginType", "isolated").lower(),
                    "isolated_margin": float(position.get("isolatedMargin", 0)),
                    "position_side": position.get("positionSide", "BOTH"),
                    "margin_ratio": float(position.get("marginRatio", 0)) * 100,  # Convert to percentage
                    "liquidation_price": float(position.get("liquidationPrice", 0)),
                    "roe": roe  # Return on Equity (%)
                }
            else:
                logger.warning(f"Failed to get position info for {symbol}")
                return {}
                
        except Exception as e:
            logger.error(f"Error getting position info: {e}")
            return {}

    async def get_all_positions(self) -> List[Dict]:
        """Get all open futures positions"""
        try:
            # Check if we're in futures mode
            if not self.futures_client:
                logger.warning("Futures trading is not enabled")
                return []
                
            # Make API call to get all positions
            endpoint = "/fapi/v2/positionRisk"
            
            response = await self._make_request("GET", endpoint, {}, is_futures=True, signed=True)
            
            if response and isinstance(response, list):
                # Filter out positions with zero amount
                positions = [pos for pos in response if float(pos.get("positionAmt", 0)) != 0]
                
                # Format positions
                formatted_positions = []
                for position in positions:
                    # Calculate ROE (Return on Equity)
                    entry_price = float(position.get("entryPrice", 0))
                    mark_price = float(position.get("markPrice", 0))
                    leverage = float(position.get("leverage", 1))
                    position_amt = float(position.get("positionAmt", 0))
                    
                    # Calculate ROE based on position direction
                    if position_amt > 0:  # Long position
                        roe = ((mark_price / entry_price) - 1) * 100 * leverage
                        direction = "LONG"
                    else:  # Short position
                        roe = ((entry_price / mark_price) - 1) * 100 * leverage
                        direction = "SHORT"
                    
                    formatted_positions.append({
                        "symbol": position.get("symbol"),
                        "position_amount": position_amt,
                        "entry_price": entry_price,
                        "mark_price": mark_price,
                        "unrealized_pnl": float(position.get("unRealizedProfit", 0)),
                        "leverage": leverage,
                        "margin_type": position.get("marginType", "isolated").lower(),
                        "isolated_margin": float(position.get("isolatedMargin", 0)),
                        "position_side": position.get("positionSide", "BOTH"),
                        "margin_ratio": float(position.get("marginRatio", 0)) * 100,  # Convert to percentage
                        "liquidation_price": float(position.get("liquidationPrice", 0)),
                        "roe": roe,  # Return on Equity (%)
                        "direction": direction
                    })
                
                return formatted_positions
            else:
                logger.warning("Failed to get positions")
                return []
                
        except Exception as e:
            logger.error(f"Error getting all positions: {e}")
            return []

    async def get_funding_rate(self, symbol: str) -> Dict:
        """Get current funding rate for a futures symbol"""
        try:
            # Check if we're in futures mode
            if not self.futures_client:
                logger.warning("Futures trading is not enabled")
                return {"funding_rate": 0, "next_funding_time": None}
                
            # Make API call to get funding rate
            endpoint = "/fapi/v1/premiumIndex"
            params = {"symbol": symbol}
            
            response = await self._make_request("GET", endpoint, params, is_futures=True)
            
            if response and "lastFundingRate" in response:
                return {
                    "symbol": response.get("symbol"),
                    "funding_rate": float(response.get("lastFundingRate", 0)),
                    "mark_price": float(response.get("markPrice", 0)),
                    "next_funding_time": datetime.fromtimestamp(response.get("nextFundingTime", 0) / 1000) if response.get("nextFundingTime") else None
                }
            else:
                logger.warning(f"Failed to get funding rate for {symbol}")
                return {"funding_rate": 0, "next_funding_time": None}
                
        except Exception as e:
            logger.error(f"Error getting funding rate: {e}")
            return {"funding_rate": 0, "next_funding_time": None}

    async def _update_exchange_info(self):
        """Update exchange information for symbols"""
        try:
            exchange_info = await self.client.get_exchange_info()
            
            # Process symbol info
            for symbol_info in exchange_info['symbols']:
                symbol = symbol_info['symbol']
                
                # Extract filters
                filters = {}
                for filter_info in symbol_info['filters']:
                    filter_type = filter_info['filterType']
                    filters[filter_type] = filter_info
                
                # Store symbol info
                self.symbol_info[symbol] = {
                    'baseAsset': symbol_info['baseAsset'],
                    'quoteAsset': symbol_info['quoteAsset'],
                    'filters': filters
                }
            
            logger.info(f"Updated exchange info for {len(self.symbol_info)} symbols")
            
        except Exception as e:
            logger.error(f"Failed to update exchange info: {e}")

    async def place_tp_sl_orders(self, order_id: str, tp_percentage: float = None, sl_percentage: float = None) -> bool:
        """Place TP/SL orders for an existing order"""
        try:
            # Check if TP/SL is enabled
            if not self.tp_sl_enabled:
                logger.warning("TP/SL is disabled, skipping TP/SL order placement")
                return False
                
            # Check if TP/SL manager is initialized
            if not self.tp_sl_manager:
                logger.error("TP/SL manager not initialized")
                return False
            
            # Get order from database
            order_doc = await self.mongo_client.orders.find_one({"order_id": order_id})
            if not order_doc:
                logger.error(f"Order {order_id} not found")
                return False
            
            # Convert to Order object
            order = self.mongo_client._document_to_order(order_doc)
            if not order:
                logger.error(f"Failed to convert order {order_id}")
                return False
            
            # Check if order is filled
            if order.status != OrderStatus.FILLED:
                logger.warning(f"Cannot place TP/SL orders for unfilled order {order_id}")
                return False
            
            # Use provided percentages or defaults
            if tp_percentage is None:
                tp_percentage = order_doc.get("tp_percentage", self.default_tp_percentage)
            if sl_percentage is None:
                sl_percentage = order_doc.get("sl_percentage", self.default_sl_percentage)
            
            # Place TP/SL orders
            result = await self.tp_sl_manager.place_tp_sl_orders(
                order, tp_percentage, sl_percentage
            )
            
            return result['tp_order'] is not None and result['sl_order'] is not None
            
        except Exception as e:
            logger.error(f"Error placing TP/SL orders: {e}")
            return False
    
    async def update_tp_sl_levels(self, order_id: str, tp_percentage: Optional[float] = None, sl_percentage: Optional[float] = None) -> bool:
        """Update TP/SL levels for an existing order"""
        try:
            if not self.tp_sl_manager:
                logger.error("TP/SL manager not initialized")
                return False
            
            return await self.tp_sl_manager.update_tp_sl_levels(
                order_id, tp_percentage, sl_percentage
            )
            
        except Exception as e:
            logger.error(f"Error updating TP/SL levels: {e}")
            return False
    
    async def cancel_tp_sl_orders(self, order_id: str) -> bool:
        """Cancel TP/SL orders for an existing order"""
        try:
            if not self.tp_sl_manager:
                logger.error("TP/SL manager not initialized")
                return False
            
            return await self.tp_sl_manager.cancel_tp_sl_orders(order_id)
            
        except Exception as e:
            logger.error(f"Error cancelling TP/SL orders: {e}")
            return False

    async def set_default_leverage(self, leverage: int) -> bool:
        """Set default leverage for futures trading"""
        try:
            # Validate leverage
            if leverage < 1 or leverage > 125:
                logger.error(f"Invalid leverage: {leverage}. Must be between 1 and 125.")
                return False
                
            # Update default leverage
            self.default_leverage = leverage
            logger.info(f"Default leverage set to {leverage}x")
            
            # Update config if available
            if self.config and 'trading' in self.config:
                self.config['trading']['default_leverage'] = leverage
                
            return True
        except Exception as e:
            logger.error(f"Error setting default leverage: {e}")
            return False
            
    async def set_default_margin_mode(self, margin_mode: str) -> bool:
        """Set default margin mode for futures trading"""
        try:
            # Validate margin mode
            if margin_mode.lower() not in ['isolated', 'cross']:
                logger.error(f"Invalid margin mode: {margin_mode}. Must be 'isolated' or 'cross'.")
                return False
                
            # Update default margin mode
            self.default_margin_mode = margin_mode.lower()
            logger.info(f"Default margin mode set to {margin_mode}")
            
            # Update config if available
            if self.config and 'trading' in self.config:
                self.config['trading']['default_margin_mode'] = margin_mode.lower()
                
            return True
        except Exception as e:
            logger.error(f"Error setting default margin mode: {e}")
            return False
            
    async def set_order_amount(self, amount: float) -> bool:
        """Set order amount for trading"""
        try:
            # Validate amount
            if amount < 10:
                logger.error(f"Invalid order amount: {amount}. Must be at least 10 USDT.")
                return False
                
            # Update order amount
            self.order_amount = amount
            logger.info(f"Order amount set to ${amount:,.2f}")
            
            # Update config if available
            if self.config and 'trading' in self.config:
                self.config['trading']['order_amount'] = amount
                
            return True
        except Exception as e:
            logger.error(f"Error setting order amount: {e}")
            return False

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for a specific symbol"""
        try:
            if not self.is_futures_enabled:
                logger.error("Futures trading is not enabled")
                return False
                
            # Validate leverage
            if leverage < 1 or leverage > 125:
                logger.error(f"Invalid leverage: {leverage}. Must be between 1 and 125.")
                return False
                
            # Set leverage for the symbol
            params = {
                'symbol': symbol,
                'leverage': leverage
            }
            
            response = await self._make_request('POST', '/fapi/v1/leverage', params, is_futures=True, signed=True)
            
            if response and 'leverage' in response:
                logger.info(f"Set leverage for {symbol} to {response['leverage']}x")
                return True
            else:
                logger.error(f"Failed to set leverage for {symbol}")
                return False
                
        except Exception as e:
            logger.error(f"Error setting leverage: {e}")
            return False
            
    async def set_margin_type(self, symbol: str, margin_type: str) -> bool:
        """Set margin type for a specific symbol (ISOLATED or CROSS)"""
        try:
            if not self.is_futures_enabled:
                logger.error("Futures trading is not enabled")
                return False
                
            # Validate margin type
            margin_type = margin_type.upper()
            if margin_type not in ['ISOLATED', 'CROSS']:
                logger.error(f"Invalid margin type: {margin_type}. Must be 'ISOLATED' or 'CROSS'.")
                return False
                
            # Set margin type for the symbol
            params = {
                'symbol': symbol,
                'marginType': margin_type
            }
            
            try:
                response = await self._make_request('POST', '/fapi/v1/marginType', params, is_futures=True, signed=True)
                
                if response and isinstance(response, dict) and response.get('msg') == 'success':
                    logger.info(f"Set margin type for {symbol} to {margin_type}")
                    return True
                else:
                    logger.warning(f"Margin type for {symbol} is already {margin_type}")
                    return True  # Consider it a success if already set
            except Exception as e:
                # Check if error is because margin type is already set
                error_msg = str(e).lower()
                if "already" in error_msg:
                    logger.info(f"Margin type for {symbol} is already {margin_type}")
                    return True
                raise
                
        except Exception as e:
            logger.error(f"Error setting margin type: {e}")
            return False
            
    async def _make_request(self, method: str, endpoint: str, params: dict = None, 
                         is_futures: bool = False, signed: bool = False) -> Any:
            """Make a request to the Binance API with rate limiting"""
            try:
                # Implement rate limiting
                current_time = time.time()
                time_since_last = current_time - self.last_request_time
                
                if time_since_last < 1 and self.request_count >= self.max_requests_per_minute:
                    wait_time = 1 - time_since_last
                    logger.debug(f"Rate limiting: waiting {wait_time:.2f}s")
                    await asyncio.sleep(wait_time)
                    
                self.last_request_time = time.time()
                self.request_count = (self.request_count + 1) % self.max_requests_per_minute
                
                # Determine which client to use
                client = self.futures_client if is_futures else self.client
                
                if not client:
                    logger.error(f"{'Futures' if is_futures else 'Spot'} client not initialized")
                    return None
                    
                # Make the request
                if method.upper() == 'GET':
                    if is_futures:
                        if signed:
                            response = client.get(endpoint, params=params)
                        else:
                            response = client.get_public(endpoint, params=params)
                    else:
                        response = await client.request(endpoint, params=params, method='GET', signed=signed)
                elif method.upper() == 'POST':
                    if is_futures:
                        if signed:
                            response = client.post(endpoint, params=params)
                        else:
                            response = client.post_public(endpoint, params=params)
                    else:
                        response = await client.request(endpoint, params=params, method='POST', signed=signed)
                else:
                    logger.error(f"Unsupported method: {method}")
                    return None
                    
                return response
                
            except Exception as e:
                logger.error(f"API request error ({method} {endpoint}): {e}")
                raise
