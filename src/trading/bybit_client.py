import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Union, Any, Set, Tuple
from decimal import Decimal, InvalidOperation
import platform
import pandas as pd
import json
import traceback
from pybit.unified_trading import HTTP, WebSocket
from urllib.parse import urlencode
import uuid
from ..types.models import (
    Order, OrderStatus, TimeFrame, OrderType, TPSLStatus,
    TakeProfit, StopLoss, TrailingStopLoss, TradeDirection, PartialTakeProfit
)
from ..types.constants import TRADING_FEES, TIMEFRAME_INTERVALS
from ..database.mongo_client import MongoClient
from ..utils.rate_limiter import RateLimiter
from ..utils.chart_generator import ChartGenerator
from ..utils.yahoo_scrapooooor_sp500 import YahooSP500Scraper
from collections import defaultdict
import random
import hmac
import hashlib
import aiohttp

logger = logging.getLogger(__name__)

class BybitClient:
    """Bybit API client implementation
    
    Implements BaseClient interface for Bybit exchange
    """
    
    # Common Bybit API error codes
    ERROR_CODES = {
        # Auth errors
        10001: "Invalid parameter(s)",
        10002: "Invalid request",
        10003: "Invalid API key",
        10004: "Invalid sign",
        10005: "Permission denied (API key has insufficient permissions)",
        10006: "Too many requests (rate limit exceeded)",
        10007: "API key expired",
        10010: "IP request restricted",
        
        # System errors
        10016: "Service unavailable (internal system error)",
        10017: "Service timeout",
        10018: "Service is currently updating",
        
        # Order errors
        110001: "Order does not exist",
        110003: "Order price exceeds limits",
        110004: "Insufficient wallet balance",
        110005: "Position in closing process",
        110006: "Not in trading time",
        110007: "Invalid order type for current instruction",
        110008: "Invalid trading pair/contract",
        
        # Other errors
        170001: "Internal error",
        170005: "Duplication request ID"
    }
    
    # Error codes that should trigger retries
    RETRY_ERROR_CODES = [10016, 10006, 10004, 10017, 10018, 170001]
    
    # Error codes that are related to API limits but not server issues
    LIMIT_ERROR_CODES = [10006, 10010]
    
    # Error codes that indicate invalid credentials but a working API
    AUTH_ERROR_CODES = [10003, 10004, 10005, 10007]
    
    # Map from system error codes to retry times in seconds
    ERROR_RETRY_MAP = {
        10006: 1,   # Rate limit - retry quickly
        10016: 5,   # System error - wait a bit longer
        10017: 3,   # Service timeout - medium wait
        10018: 10,  # Service updating - wait longer
        170001: 5,  # Internal error - wait a bit longer
    }
    
    def __init__(self, api_key: str, api_secret: str, telegram_bot=None, mongo_client=None, config=None, testnet: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.telegram_bot = telegram_bot
        self.mongo_client = mongo_client
        self.config = config or {}
        self.client = None
        self.time_offset = 0
        
        # Trading properties
        self.base_currency = self.config['trading'].get('base_currency', 'USDT')
        self.reserve_balance = float(self.config['trading'].get('reserve_balance', 500))
        self.only_lower_entries = self.config['trading'].get('only_lower_entries', True)
        
        # TP/SL settings
        self.default_tp_percentage = self._parse_tp_sl_setting(
            self.config['trading'].get('take_profit', '5%')
        )
        self.default_sl_percentage = self._parse_tp_sl_setting(
            self.config['trading'].get('stop_loss', '3%')
        )
        
        # Trading settings
        self.thresholds = self.config['trading'].get('thresholds', {
            'daily': [1, 2, 5],
            'weekly': [5, 10, 15],
            'monthly': [10, 20, 30]
        })
        
        # Exchange info
        self.exchange_info = None
        self.client_initialized = False
        self.symbol_details = {}
        self.invalid_symbols = set()
        
        # State management for thresholds
        self.triggered_thresholds = defaultdict(lambda: {
            TimeFrame.DAILY: set(),
            TimeFrame.WEEKLY: set(),
            TimeFrame.MONTHLY: set()
        })
        
        # Store reference timestamps and prices for timeframes
        self.reference_timestamps = {
            TimeFrame.DAILY: None,
            TimeFrame.WEEKLY: None,
            TimeFrame.MONTHLY: None
        }
        self.reference_prices = {}
        
        # Performance reporter for generating period reports
        self.performance_reporter = None
        
        # Determine category based on testnet status
        # IMPORTANT: testnet uses "linear" category, mainnet uses "spot" for spot trading
        self._trading_category = "linear" if self.testnet else "spot"
        
        logger.info(f"Initialized BybitClient with testnet={testnet}, "
                   f"trading_category={self._trading_category}, "
                   f"base_currency={self.base_currency}")
        
        # Add clear warning if using testnet with USDC pairs
        if testnet and config and 'trading' in config:
            base_currency = config['trading'].get('base_currency', 'USDT')
            if base_currency == 'USDC':
                logger.warning(f"[INIT] ⚠️ WARNING: USDC spot trading is NOT available on Bybit Testnet!")
                logger.warning(f"[INIT] Please switch to USDT as base currency when using testnet, or switch to mainnet for USDC trading.")
        
        self.rate_limiter = RateLimiter()
        self.symbol_info = {}
        self.last_reset = {
            tf: datetime.utcnow() for tf in TimeFrame
        }
        self.balance_cache = {}
        self.removed_symbols_this_cycle = set()
        
        # Set API environment info
        environment = "TESTNET" if testnet else "MAINNET"
        logger.info(f"[INIT] Using Bybit {environment} API")
        
        # Initialize chart generator
        self.chart_generator = ChartGenerator()
        
        # Initialize Yahoo SP500 scraper
        self.yahoo_scraper = YahooSP500Scraper()
        
        # Initialize thresholds dictionary with nested structure to track triggered thresholds
        self.triggered_thresholds = {}
        
        # Performance reporter for generating period reports
        self.performance_reporter = None
        
    def set_telegram_bot(self, bot):
        """Set the Telegram bot reference after initialization"""
        self.telegram_bot = bot
        
    async def check_initial_balance(self) -> bool:
        """Check if there's enough initial balance for trading"""
        try:
            # Get balance for base currency
            balance = await self.get_balance(self.base_currency)
            
            # Check if balance meets the reserve requirement
            if balance < Decimal(str(self.reserve_balance)):
                logger.warning(f"Insufficient balance: {balance} {self.base_currency} < {self.reserve_balance} {self.base_currency}")
                
                # Send alert via Telegram if available
                if self.telegram_bot:
                    await self.telegram_bot.send_initial_balance_alert(balance, self.reserve_balance)
                    
                return False
                
            logger.info(f"Initial balance check passed: {balance} {self.base_currency} >= {self.reserve_balance} {self.base_currency}")
            return True
            
        except Exception as e:
            logger.error(f"Error checking initial balance: {e}")
            return False
            
    async def update_time_offset(self):
        """Update the time offset between local and server time"""
        try:
            # Get server time
            server_response = self.client.get_server_time()
            
            if 'retCode' in server_response and server_response['retCode'] == 0:
                server_time = int(server_response['result']['timeNano']) // 1000000  # Convert nano to milliseconds
                local_time = int(time.time() * 1000)  # Local time in milliseconds
                
                # Calculate the offset
                self.time_offset = server_time - local_time
                self.time_offset_updated = True
                
                logger.info(f"Updated time offset: {self.time_offset} ms")
                return True
            else:
                error_msg = server_response.get('retMsg', 'Unknown error')
                logger.error(f"Failed to get server time: {error_msg}")
                return False
        except Exception as e:
            logger.error(f"Error updating time offset: {e}")
            return False
            
    async def initialize(self):
        """Initialize the Bybit client"""
        try:
            # Initialize HTTP client
            self.client = HTTP(
                api_key=self.api_key,
                api_secret=self.api_secret,
                testnet=self.testnet,
                recv_window=10000
            )
            
            # Initialize WebSocket client if needed
            if self.testnet:
                ws_endpoint = f"wss://stream-testnet.bybit.com/v5/public/{self._trading_category}"
            else:
                ws_endpoint = f"wss://stream.bybit.com/v5/public/{self._trading_category}"
                
            self.ws_client = WebSocket(
                testnet=self.testnet,
                api_key=self.api_key,
                api_secret=self.api_secret,
                channel_type=self._trading_category
            )
            
            # Initialize rate limiter
            self.rate_limiter = RateLimiter()
            
            # Update time offset with server
            await self.update_time_offset()
            
            # Get exchange information and symbols
            try:
                await self.rate_limiter.acquire()
                # Get instruments info for the specified trading category
                response = await self.make_request('get_instruments_info', params={"category": self._trading_category})
                
                if response and isinstance(response, dict) and response.get('retCode') == 0:
                    instruments = response.get('result', {}).get('list', [])
                    # Initialize symbol info dictionary
                    for symbol_info in instruments:
                        self.symbol_info[symbol_info["symbol"]] = symbol_info
                    
                    logger.info(f"Initialized with {len(self.symbol_info)} symbols")
                else:
                    logger.error(f"Failed to get instruments info: {response}")
                    
            except Exception as e:
                logger.error(f"Error getting instruments info: {e}")
                traceback.print_exc()
                return False
            
            # Get trading symbols from configuration or database
            trading_symbols = []
            if self.mongo_client:
                # Try to get trading symbols from the database
                db_symbols = await self.mongo_client.get_trading_symbols()
                if db_symbols:
                    trading_symbols = db_symbols
                    logger.info(f"Loaded {len(trading_symbols)} trading symbols from database")
            
            if not trading_symbols and self.config and 'trading' in self.config and 'pairs' in self.config['trading']:
                # Fallback to configuration if no symbols in database
                trading_symbols = self.config['trading']['pairs']
                logger.info(f"Using {len(trading_symbols)} trading symbols from config")
                
                # Save symbols to database if we have a mongo client
                if self.mongo_client:
                    for symbol in trading_symbols:
                        await self.mongo_client.save_trading_symbol(symbol)
            
            # Filter out invalid symbols
            if trading_symbols:
                valid_symbols = await self.filter_valid_symbols(trading_symbols)
                logger.info(f"Found {len(valid_symbols)} valid trading symbols")
            
            # Restore threshold state
            await self.restore_threshold_state()
            
            logger.info("Bybit client initialized successfully")
            return True
            
        except Exception as e:
            logger.error(f"Error initializing Bybit client: {e}")
            traceback.print_exc()  # Print full stack trace
            return False
            
    async def restore_threshold_state(self):
        """Restore threshold state from database"""
        try:
            # Only proceed if we have a MongoDB client
            if not self.mongo_client:
                logger.warning("No MongoDB client available, skipping threshold restoration")
                return
                
            # Load reference prices
            db_reference_prices = await self.mongo_client.get_reference_prices()
            if db_reference_prices:
                # Convert string keys to symbols
                for symbol, timeframes in db_reference_prices.items():
                    if symbol not in self.reference_prices:
                        self.reference_prices[symbol] = {}
                        
                    for timeframe_str, price in timeframes.items():
                        try:
                            # Convert string timeframe to enum
                            tf = TimeFrame(timeframe_str)
                            self.reference_prices[symbol][tf] = float(price)
                        except (ValueError, KeyError) as e:
                            logger.error(f"Error parsing timeframe {timeframe_str}: {e}")
                            
                logger.info(f"Restored reference prices for {len(self.reference_prices)} symbols")
                
            # Load triggered thresholds
            await self.restore_triggered_thresholds()
            
        except Exception as e:
            logger.error(f"Error restoring threshold state: {e}")
            
    async def close(self):
        """Close the client connection"""
        # WebSocket connections need to be closed
        if hasattr(self, 'ws_client') and self.ws_client:
            self.ws_client.exit()
        logger.info("Bybit client connections closed")
            
    async def check_timeframe_reset(self, timeframe: TimeFrame) -> bool:
        """Check if a timeframe needs to be reset and handle the reset if needed."""
        try:
            logger.debug(f"Checking {timeframe.value} timeframe reset")

            current_time = datetime.utcnow()
            last_reset = self.last_reset.get(timeframe)

            # Determine if this timeframe should be reset
            if timeframe == TimeFrame.DAILY:
                scheduled = current_time.hour == 0 and current_time.minute < 5
                overdue = (
                    last_reset is None
                    or (current_time - last_reset) >= TIMEFRAME_INTERVALS['DAILY']
                ) and (last_reset is None or current_time.date() != last_reset.date())
                reset_needed = scheduled or overdue

            elif timeframe == TimeFrame.WEEKLY:
                scheduled = (
                    current_time.weekday() == 0
                    and current_time.hour == 0
                    and current_time.minute < 5
                )
                if last_reset is None:
                    overdue = True
                else:
                    overdue = (current_time - last_reset) >= TIMEFRAME_INTERVALS['WEEKLY'] or (
                        current_time.isocalendar()[1] != last_reset.isocalendar()[1]
                    )
                reset_needed = scheduled or overdue

            elif timeframe == TimeFrame.MONTHLY:
                scheduled = current_time.day == 1 and current_time.hour == 0 and current_time.minute < 5
                if last_reset is None:
                    overdue = True
                else:
                    overdue = (
                        current_time.month != last_reset.month
                        or current_time.year != last_reset.year
                    )
                reset_needed = scheduled or overdue

            else:
                logger.error(f"Unknown timeframe: {timeframe}")
                return False
                
            if reset_needed:
                logger.info(f"Resetting {timeframe.value} timeframe thresholds")

                # Clear triggered thresholds for this timeframe
                await self.reset_timeframe_thresholds(timeframe.value)

                # Update reference timestamps
                self.reference_timestamps[timeframe] = int(current_time.timestamp() * 1000)

                # Record last reset time so future checks know this period was processed
                self.last_reset[timeframe] = current_time
                
                # Update reference prices
                symbols = self.config['trading'].get('pairs', [])
                
                # Create a list for pair information to send with the notification
                pairs_info = []
                
                # Filter out invalid symbols first
                valid_symbols = []
                for symbol in symbols:
                    if self._is_valid_symbol_format(symbol) and symbol not in self.invalid_symbols:
                        valid_symbols.append(symbol)
                    else:
                        logger.warning(f"Skipping invalid symbol: {symbol}")
                
                # Update reference prices for each valid symbol
                for symbol in valid_symbols:
                    try:
                        # Get current price to use as the new reference
                        current_price = await self.get_current_price(symbol)
                        
                        if current_price:
                            # Store the new reference price
                            if symbol not in self.reference_prices:
                                self.reference_prices[symbol] = {}
                                
                            self.reference_prices[symbol][timeframe] = current_price
                            logger.info(f"Updated {timeframe.value} reference price for {symbol}: ${float(current_price):,.2f}")
                            
                            # Save to database
                            if self.mongo_client:
                                await self.mongo_client.save_reference_price(
                                    symbol, timeframe.value, float(current_price)
                                )
                            
                            # Add to pairs info for notification
                            pairs_info.append({
                                'symbol': symbol,
                                'reference_price': float(current_price),
                                'thresholds': self.thresholds[timeframe.value]
                            })
                    except Exception as e:
                        logger.error(f"Error updating reference price for {symbol}: {e}")
                
                # Send notification if telegram bot is available
                if self.telegram_bot:
                    reset_data = {
                        'timeframe': timeframe.value,
                        'timestamp': current_time,
                        'pairs': pairs_info
                    }
                    await self.telegram_bot.send_timeframe_reset_notification(reset_data)
                
                # Generate performance report if applicable and performance reporter is available
                if self.performance_reporter:
                    try:
                        await self.performance_reporter.handle_timeframe_reset(timeframe, reset_data)
                    except Exception as e:
                        logger.error(f"Error generating performance report for {timeframe.value}: {e}")
                
                return True
                
            return False
            
        except Exception as e:
            logger.error(f"Error checking timeframe reset: {e}")
            return False

    async def reset_timeframe_thresholds(self, timeframe_str: str):
        """Reset triggered thresholds for a specific timeframe"""
        try:
            logger.info(f"Resetting thresholds for {timeframe_str} timeframe")
            
            # Clear local cache for this timeframe
            TimeFrame_enum = TimeFrame(timeframe_str)
            
            for symbol in self.triggered_thresholds:
                if TimeFrame_enum in self.triggered_thresholds[symbol]:
                    self.triggered_thresholds[symbol][TimeFrame_enum] = set()
            
            # Reset in database
            if self.mongo_client:
                await self.mongo_client.reset_timeframe_thresholds(timeframe_str)
                
            # Get current time for notification
            current_time = datetime.utcnow()
            
            # Create list of pairs with reference prices for notification
            symbols = self.config['trading'].get('pairs', [])
            pairs_info = []
            
            # Only include valid symbols
            valid_symbols = [s for s in symbols 
                            if self._is_valid_symbol_format(s) and s not in self.invalid_symbols]
            
            # Get reference prices for all valid symbols
            for symbol in valid_symbols:
                try:
                    # Use existing reference price or get current price
                    if (symbol in self.reference_prices and 
                        TimeFrame_enum in self.reference_prices[symbol]):
                        ref_price = float(self.reference_prices[symbol][TimeFrame_enum])
                    else:
                        current_price = await self.get_current_price(symbol)
                        ref_price = float(current_price) if current_price else 0.0
                    
                    # Add to pairs info for notification
                    if ref_price > 0:
                        pairs_info.append({
                            'symbol': symbol,
                            'reference_price': ref_price,
                            'thresholds': self.thresholds[timeframe_str]
                        })
                except Exception as e:
                    logger.error(f"Error getting reference price for {symbol}: {e}")
            
            # Prepare reset data
            reset_data = {
                "timeframe": timeframe_str,
                "timestamp": current_time,
                "pairs": pairs_info
            }
            
            # Send notification if telegram bot is available
            if self.telegram_bot:
                await self.telegram_bot.send_timeframe_reset_notification(reset_data)
                
            # Generate performance report if applicable
            if self.performance_reporter:
                try:
                    await self.performance_reporter.handle_timeframe_reset(TimeFrame_enum, reset_data)
                except Exception as e:
                    logger.error(f"Error generating performance report for {timeframe_str}: {e}")
                
            logger.info(f"Successfully reset thresholds for {timeframe_str}")
            return True
            
        except Exception as e:
            logger.error(f"Error resetting {timeframe_str} thresholds: {e}")
            return False

    async def get_reference_timestamp(self, timeframe: TimeFrame) -> int:
        """Get reference timestamp for a timeframe"""
        try:
            # If we already have a timestamp for this timeframe, return it
            if self.reference_timestamps[timeframe] is not None:
                return self.reference_timestamps[timeframe]
                
            # Calculate reference timestamp based on timeframe
            now = datetime.utcnow()
            
            if timeframe == TimeFrame.DAILY:
                # Beginning of the current day
                reference_time = datetime(now.year, now.month, now.day)
                
            elif timeframe == TimeFrame.WEEKLY:
                # Beginning of the current week (Monday)
                days_since_monday = now.weekday()
                reference_time = datetime(now.year, now.month, now.day) - timedelta(days=days_since_monday)
                
            elif timeframe == TimeFrame.MONTHLY:
                # Beginning of the current month
                reference_time = datetime(now.year, now.month, 1)
                
            else:
                logger.error(f"Unknown timeframe: {timeframe}")
                return int(now.timestamp() * 1000)
                
            # Convert to milliseconds timestamp and store
            reference_timestamp = int(reference_time.timestamp() * 1000)
            self.reference_timestamps[timeframe] = reference_timestamp
            
            return reference_timestamp
            
        except Exception as e:
            logger.error(f"Error getting reference timestamp: {e}")
            return int(datetime.utcnow().timestamp() * 1000)
            
    async def get_reference_price(self, symbol: str, timeframe: TimeFrame) -> Optional[float]:
        """Get reference price for a symbol and timeframe"""
        try:
            # Check if we have a cached reference price
            if (
                symbol in self.reference_prices and 
                timeframe in self.reference_prices[symbol]
            ):
                return float(self.reference_prices[symbol][timeframe])
            
            # Get current price as initial reference
            current_price = await self.get_current_price(symbol)
            if not current_price:
                logger.warning(f"Failed to get current price for {symbol}")
                return None
            
            # Store as reference price
            if symbol not in self.reference_prices:
                self.reference_prices[symbol] = {}
            self.reference_prices[symbol][timeframe] = Decimal(str(current_price))
            
            # Save to database if available
            if self.mongo_client:
                await self.mongo_client.save_reference_prices({
                    symbol: {timeframe.value: float(current_price)}
                })
            
            return float(current_price)
            
        except Exception as e:
            logger.error(f"Error getting reference price for {symbol} {timeframe.value}: {e}")
            return None

    async def update_reference_prices(self, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        """Update reference prices for a list of symbols"""
        logger.info(f"Updating reference prices for {len(symbols)} symbols")
        
        price_updates = {}
        
        for symbol in symbols:
            try:
                # Skip invalid symbols
                if symbol in self.invalid_symbols:
                    continue
                    
                # Initialize nested dictionary if needed
                if symbol not in self.reference_prices:
                    self.reference_prices[symbol] = {}
                    
                # Update reference prices for each timeframe
                price_updates[symbol] = {}
                
                for timeframe in TimeFrame:
                    # Skip if we already have a reference price for this timeframe
                    existing_price = self.reference_prices[symbol].get(timeframe)
                    if existing_price is not None:
                        logger.debug(f"Already have reference price for {symbol} {timeframe.value}: ${existing_price:,.2f}")
                        price_updates[symbol][timeframe.value] = existing_price
                        continue
                        
                    # Get reference price
                    ref_price = await self.get_reference_price(symbol, timeframe)
                    
                    if ref_price is not None:
                        self.reference_prices[symbol][timeframe] = ref_price
                        price_updates[symbol][timeframe.value] = ref_price
                
            except Exception as e:
                logger.error(f"Error updating reference prices for {symbol}: {e}")
                
        # Save reference prices to database if available
        if price_updates and self.mongo_client:
            try:
                await self.mongo_client.save_reference_prices(price_updates)
                logger.info(f"Saved reference prices to database for {len(price_updates)} symbols")
            except Exception as e:
                logger.error(f"Error saving reference prices to database: {e}")
                
        return price_updates
        
    async def check_thresholds(self, symbol: str, timeframe: TimeFrame) -> List[float]:
        """Check price thresholds for a symbol and timeframe"""
        try:
            # Skip if symbol is invalid
            if not self._is_valid_symbol_format(symbol) or symbol in self.invalid_symbols:
                logger.warning(f"Invalid symbol format or known invalid: {symbol}")
                return []
            
            # Get reference price
            reference_price = await self.get_reference_price(symbol, timeframe)
            if not reference_price:
                logger.warning(f"No reference price for {symbol} {timeframe.value}")
                return []
            
            # Convert reference price to Decimal
            reference_price_dec = Decimal(str(reference_price))
            
            # Get current price
            current_price = await self.get_current_price(symbol)
            if not current_price:
                logger.warning(f"Failed to get current price for {symbol}")
                return []
            
            # Convert current price to Decimal
            current_price_dec = Decimal(str(current_price))
            
            # Calculate price change as a percentage
            price_change = ((current_price_dec - reference_price_dec) / reference_price_dec) * Decimal('100')
            
            # Check for BTC price jump if this is BTCUSDT/BTCUSDC
            if symbol.startswith('BTC') and timeframe == TimeFrame.DAILY and self.telegram_bot:
                # Convert to float for easier comparison
                price_change_float = float(price_change)
                if price_change_float > 10:
                    # BTC has increased by more than 10% - call the method to send image
                    await self.telegram_bot.check_btc_price_jump(
                        float(current_price_dec), float(reference_price_dec)
                    )
            
            # Get thresholds for this timeframe
            thresholds = self.config['trading']['thresholds'][timeframe.value]
            
            # Check if we've triggered any thresholds
            triggered = []
            
            for threshold in thresholds:
                # Convert threshold to Decimal for comparison
                threshold_dec = Decimal(str(threshold))
                
                # Check if price has decreased by more than the threshold percentage
                # and we haven't triggered this threshold before
                if price_change <= -threshold_dec:
                    # Only trade on dropping prices if configured
                    if self.only_lower_entries or price_change <= 0:
                        is_triggered = await self.mongo_client.check_triggered_threshold(
                            symbol, timeframe.value, threshold
                        )
                        
                        if not is_triggered:
                            # Mark as triggered and save
                            await self.mark_threshold_triggered(symbol, timeframe, threshold)
                            triggered.append(threshold)
                            
                            # Log and send notification
                            logger.info(f"Threshold triggered for {symbol} {timeframe.value} {threshold}%: {price_change}%")
                            if self.telegram_bot:
                                await self.telegram_bot.send_threshold_notification(
                                    symbol, timeframe, threshold,
                                    float(current_price_dec), float(reference_price_dec), float(price_change)
                                )
            
            return triggered
            
        except Exception as e:
            logger.error(f"Error checking thresholds for {symbol}: {e}")
            return []
            
    async def mark_threshold_triggered(self, symbol: str, timeframe: TimeFrame, threshold: float):
        """Mark a threshold as triggered for a symbol and timeframe"""
        try:
            # Initialize nested data structure if needed
            if symbol not in self.triggered_thresholds:
                self.triggered_thresholds[symbol] = {}
                
            if timeframe.value not in self.triggered_thresholds[symbol]:
                self.triggered_thresholds[symbol][timeframe.value] = []
                
            # Add to triggered thresholds if not already there
            if threshold not in self.triggered_thresholds[symbol][timeframe.value]:
                self.triggered_thresholds[symbol][timeframe.value].append(threshold)
                
                # Update in database if available
                if self.mongo_client:
                    await self.mongo_client.save_triggered_threshold(
                        symbol, timeframe.value, self.triggered_thresholds[symbol][timeframe.value]
                    )
                    
                logger.info(f"Marked {threshold}% threshold as triggered for {symbol} on {timeframe.value}")
                
        except Exception as e:
            logger.error(f"Error marking threshold as triggered: {e}")
            
    async def restore_triggered_thresholds(self):
        """Restore triggered thresholds from database"""
        try:
            # Only proceed if we have a MongoDB client
            if not self.mongo_client:
                logger.warning("No MongoDB client available, skipping threshold restoration")
                return
                
            # Get all triggered thresholds from database
            db_thresholds = await self.mongo_client.get_triggered_thresholds()
            
            if not db_thresholds:
                logger.info("No triggered thresholds found in database")
                return
                
            # Restore thresholds
            for symbol, timeframes in db_thresholds.items():
                if symbol not in self.triggered_thresholds:
                    self.triggered_thresholds[symbol] = {}
                    
                for timeframe_str, thresholds in timeframes.items():
                    self.triggered_thresholds[symbol][timeframe_str] = thresholds
                    
            # Log restoration
            symbol_count = len(self.triggered_thresholds)
            threshold_count = sum(
                len(thresholds)
                for symbol_data in self.triggered_thresholds.values()
                for thresholds in symbol_data.values()
            )
            
            logger.info(f"Restored {threshold_count} triggered thresholds for {symbol_count} symbols")
            
            # Send notification if telegram bot is available
            if self.telegram_bot and threshold_count > 0:
                await self.telegram_bot.send_restored_thresholds_notification({
                    'symbols': symbol_count,
                    'thresholds': threshold_count
                })
                
        except Exception as e:
            logger.error(f"Error restoring triggered thresholds: {e}")
            
    async def check_reserve_balance(self, order_amount: float) -> bool:
        """Check if there's enough balance for an order plus reserve"""
        try:
            # Get current balance
            balance = await self.get_balance(self.base_currency)
            
            # Calculate required balance (order amount + reserve)
            required_balance = Decimal(str(order_amount)) + Decimal(str(self.reserve_balance))
            
            # Check if balance is sufficient
            has_enough = balance >= required_balance
            
            if not has_enough:
                logger.warning(f"Insufficient balance for order: {float(balance):,.2f} < {float(required_balance):,.2f}")
                
                # Send reserve alert if telegram bot is available
                if self.telegram_bot:
                    # Calculate pending value
                    pending_value = Decimal('0')
                    try:
                        if self.mongo_client:
                            pending_orders = await self.mongo_client.get_pending_orders()
                            for order in pending_orders:
                                pending_value += Decimal(str(order.price)) * Decimal(str(order.quantity))
                    except Exception as e:
                        logger.error(f"Error calculating pending value: {e}")
                        
                    await self.telegram_bot.send_reserve_alert(balance, self.reserve_balance, pending_value)
                    
            return has_enough
            
        except Exception as e:
            logger.error(f"Error checking reserve balance: {e}")
            return False
            
    async def place_limit_buy_order(self, symbol: str, amount: float, 
                                   threshold: Optional[float] = None,
                                   timeframe: Optional[TimeFrame] = None,
                                   is_manual: bool = False) -> Order:
        """Place a limit buy order for a symbol with the given parameters"""
        try:
            # Check for invalid symbol
            if symbol in self.invalid_symbols:
                logger.warning(f"Skipping order for known invalid symbol: {symbol}")
                return None
                
            # Get current price
            current_price = await self.get_current_price(symbol)
            if current_price is None:
                logger.error(f"Unable to get current price for {symbol}")
                return None
                
            # If only_lower_entries is enabled and not a manual trade, enforce it
            if (not is_manual and 
                self.config and 'trading' in self.config and 
                self.config['trading'].get('only_lower_entries', False)):
                
                # Check if we already have a position for this symbol
                if self.mongo_client:
                    position = await self.mongo_client.get_position_for_symbol(symbol)
                    
                    if position and position.get('size', 0) > 0:
                        avg_price = position.get('avg_price', 0)
                        
                        # Skip if current price would increase average entry price
                        if current_price > avg_price:
                            logger.warning(f"Skipping order for {symbol} at ${current_price:,.2f} - higher than current avg price ${avg_price:,.2f}")
                            return None
            
            # Prepare order parameters
            # Calculate price with a small discount to improve fill chances (0.1% below current)
            price = Decimal(str(current_price)) * Decimal('0.999')
            price = self._align_price_to_tick(symbol, price)
            
            # Calculate quantity based on amount and price
            quantity = Decimal(str(amount)) / price
            quantity = self._adjust_quantity_to_lot_size(symbol, quantity)
            
            if quantity <= 0:
                logger.error(f"Invalid quantity calculated for {symbol}: {quantity}")
                return None
                
            # Prepare order creation parameters for Bybit API
            order_params = {
                "category": self._trading_category,
                "symbol": symbol,
                "side": "Buy",
                "orderType": "Limit",
                "price": str(price),
                "qty": str(quantity),
                "timeInForce": "GTC"  # Good Till Canceled
            }
            
            # Place order
            await self.rate_limiter.acquire()
            response = self.client.place_order(**order_params)
            
            # Check for successful order
            if 'result' in response and 'orderId' in response['result']:
                order_id = response['result']['orderId']
                
                # Calculate fees
                fees, fee_asset = await self.calculate_fees(symbol, price, quantity)
                
                # Create order object
                created_at = datetime.utcnow()
                
                order = Order(
                    symbol=symbol,
                    status=OrderStatus.PENDING,
                    order_type=OrderType.LIMIT,
                    price=price,
                    quantity=quantity,
                    timeframe=timeframe,
                    order_id=order_id,
                    created_at=created_at,
                    updated_at=created_at,
                    is_manual=is_manual,
                    threshold=threshold,
                    fees=fees,
                    fee_asset=fee_asset
                )
                
                logger.info(f"Created limit buy order for {symbol} at ${float(price):,.2f} - {quantity} units")
                return order
                
            else:
                logger.error(f"Failed to create order: {response}")
                return None
                
        except Exception as e:
            logger.error(f"Error placing limit buy order for {symbol}: {e}")
            return None
            
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an existing order"""
        try:
            await self.rate_limiter.acquire()
            
            # Cancelling order through Bybit API
            response = self.client.cancel_order(
                category=self._trading_category,
                symbol=symbol,
                orderId=order_id
            )
            
            if 'retCode' in response and response['retCode'] == 0:
                logger.info(f"Cancelled order {order_id} for {symbol}")
                return True
            else:
                logger.error(f"Failed to cancel order {order_id} for {symbol}: {response}")
                return False
                
        except Exception as e:
            logger.error(f"Error cancelling order {order_id} for {symbol}: {e}")
            return False
            
    async def check_order_status(self, symbol: str, order_id: str) -> Optional[OrderStatus]:
        """Check the status of an order"""
        try:
            await self.rate_limiter.acquire()
            
            # Get order details from Bybit API
            response = self.client.get_order_history(
                category=self._trading_category,
                symbol=symbol,
                orderId=order_id
            )
            
            if 'result' in response and 'list' in response['result'] and len(response['result']['list']) > 0:
                order_data = response['result']['list'][0]
                status = order_data['orderStatus']
                
                # Map Bybit statuses to our OrderStatus enum
                if status == 'Filled':
                    return OrderStatus.FILLED
                elif status in ['Cancelled', 'Rejected', 'ExchangeRejected', 'Expired']:
                    return OrderStatus.CANCELLED
                else:
                    return OrderStatus.PENDING
            else:
                logger.warning(f"Could not find order {order_id} for {symbol}")
                return None
                
        except Exception as e:
            logger.error(f"Error checking order status for {symbol} {order_id}: {e}")
            return None
            
    async def get_balance(self, symbol: str = None) -> Decimal:
        """Get current balance for a symbol"""
        try:
            # Use specified symbol or default to base currency
            if not symbol:
                symbol = self.base_currency
                
            # Get wallet balance with proper category
            response = await self.make_request(
                'get_wallet_balance',
                accountType="UNIFIED"
            )
            
            if not response or 'retCode' not in response or response['retCode'] != 0:
                logger.error(f"Failed to get balance: {response.get('retMsg', 'Unknown error')}")
                return Decimal('0')
                
            # Extract balance from response
            try:
                result = response.get('result', {})
                list_data = result.get('list', [])
                
                # Find the coin in the response
                for account in list_data:
                    coin_data = account.get('coin', [])
                    for coin in coin_data:
                        if coin.get('coin') == symbol:
                            # Sum available and frozen balances
                            available = Decimal(str(coin.get('walletBalance', '0')))
                            frozen = Decimal(str(coin.get('locked', '0')))
                            total = available + frozen
                            logger.info(f"Found {symbol} balance: {total} (Available: {available}, Locked: {frozen})")
                            return total
                            
                # If coin not found, log warning and return 0
                logger.warning(f"No balance found for {symbol}")
                return Decimal('0')
                
            except Exception as e:
                logger.error(f"Error parsing balance response for {symbol}: {str(e)}")
                return Decimal('0')
                
        except Exception as e:
            logger.error(f"Failed to get balance: {e}")
            return Decimal('0')
            
    async def get_balance_changes(self, symbol: str = None) -> Optional[Decimal]:
        """Get balance changes since last check"""
        # Use configured base currency if no symbol provided
        if not symbol:
            symbol = self.base_currency
            
        current_balance = await self.get_balance(symbol)
        previous_balance = self.balance_cache.get(symbol)
        self.balance_cache[symbol] = current_balance
        
        if previous_balance is not None:
            return current_balance - previous_balance
        return None
        
    async def get_current_price(self, symbol: str) -> Optional[Decimal]:
        """Get the current price for a symbol from Bybit"""
        try:
            # Validate symbol format
            if not symbol or not isinstance(symbol, str):
                logger.error(f"Invalid symbol format: {symbol}")
                return None

            # Get ticker info using the correct endpoint
            await self.rate_limiter.acquire()
            response = await self.make_request(
                method="get_tickers",
                params={"category": self._trading_category, "symbol": symbol}
            )
            
            # Validate response
            if not response or not isinstance(response, dict):
                logger.error(f"Invalid response format from get_tickers: {response}")
                return None
                
            if response.get('retCode') != 0:
                logger.error(f"Error getting ticker for {symbol}: {response.get('retMsg')}")
                return None
            
            # Extract last price from response
            result = response.get('result', {})
            ticker_list = result.get('list', [])
            if not ticker_list:
                logger.error(f"No ticker data found for {symbol}")
                return None
                
            last_price = ticker_list[0].get('lastPrice')
            if not last_price:
                logger.error(f"No last price found in ticker data for {symbol}")
                return None
                
            try:
                return Decimal(str(last_price))
            except (TypeError, ValueError, InvalidOperation) as e:
                logger.error(f"Error converting price to Decimal for {symbol}: {e}")
                return None
                
        except Exception as e:
            logger.error(f"Error getting current price for {symbol}: {e}")
            return None
            
    def _is_valid_symbol_format(self, symbol: str) -> bool:
        """Check if a symbol has valid format"""
        # Basic format validation: letters and numbers only, at least 2 characters
        if not symbol or len(symbol) < 2:
            return False
            
        # Check against known invalid symbols
        if symbol in self.invalid_symbols:
            return False
            
        return True
        
    def _get_quantity_precision(self, symbol: str) -> int:
        """Get the quantity precision for a symbol"""
        try:
            if symbol in self.symbol_info:
                # In Bybit, this is stored in lotSizeFilter.qtyStep
                for filter_type in self.symbol_info[symbol]['lotSizeFilter']:
                    if 'qtyStep' in filter_type:
                        # Calculate precision from step
                        step = Decimal(str(filter_type['qtyStep']))
                        if step > 0:
                            precision = abs(step.as_tuple().exponent)
                            return precision
            return 8  # Default precision if symbol info not available
        except Exception as e:
            logger.error(f"Error getting quantity precision for {symbol}: {e}")
            return 8  # Default safe value
            
    def _get_price_precision(self, symbol: str) -> int:
        """Get the price precision for a symbol"""
        try:
            if symbol in self.symbol_info:
                # In Bybit, this is stored in priceFilter.tickSize
                for filter_type in self.symbol_info[symbol]['priceFilter']:
                    if 'tickSize' in filter_type:
                        # Calculate precision from step
                        step = Decimal(str(filter_type['tickSize']))
                        if step > 0:
                            precision = abs(step.as_tuple().exponent)
                            return precision
            return 8  # Default precision if symbol info not available
        except Exception as e:
            logger.error(f"Error getting price precision for {symbol}: {e}")
            return 8  # Default safe value
            
    def _get_tick_size(self, symbol: str) -> Decimal:
        """Get the minimum price increment (tick size) for a symbol"""
        try:
            if symbol in self.symbol_info:
                return Decimal(str(self.symbol_info[symbol]['priceFilter']['tickSize']))
            return Decimal('0.00000001')  # Default tick size if not available
        except Exception as e:
            logger.error(f"Error getting tick size for {symbol}: {e}")
            return Decimal('0.00000001')  # Default safe value
            
    def _align_price_to_tick(self, symbol: str, price: Decimal) -> Decimal:
        """Align a price value to the symbol's tick size"""
        try:
            tick_size = self._get_tick_size(symbol)
            if tick_size <= 0:
                return price
                
            # Round down to nearest tick
            return (price // tick_size) * tick_size
            
        except Exception as e:
            logger.error(f"Error aligning price to tick for {symbol}: {e}")
            return price
            
    def _adjust_quantity_to_lot_size(self, symbol: str, quantity: Decimal) -> Decimal:
        """Adjust quantity to conform to lot size requirements"""
        try:
            if symbol in self.symbol_info:
                min_qty = Decimal(str(self.symbol_info[symbol]['lotSizeFilter']['minOrderQty']))
                max_qty = Decimal(str(self.symbol_info[symbol]['lotSizeFilter']['maxOrderQty']))
                step_size = Decimal(str(self.symbol_info[symbol]['lotSizeFilter']['qtyStep']))
                
                # Check min quantity
                if quantity < min_qty:
                    logger.warning(f"Quantity {quantity} below minimum {min_qty} for {symbol}")
                    return Decimal('0')
                    
                # Check max quantity
                if quantity > max_qty:
                    logger.warning(f"Quantity {quantity} above maximum {max_qty} for {symbol}")
                    quantity = max_qty
                    
                # Adjust to step size
                if step_size > 0:
                    quantity = (quantity // step_size) * step_size
                    
                return quantity
                
            return quantity
            
        except Exception as e:
            logger.error(f"Error adjusting quantity to lot size for {symbol}: {e}")
            return quantity

    async def calculate_fees(self, symbol: str, price: Decimal, quantity: Decimal, order_type: str = "spot", leverage: int = 1) -> Tuple[Decimal, str]:
        """Calculate trading fees for an order based on order type and leverage"""
        try:
            # Get fee rate based on order type (spot vs futures)
            fee_rate = TRADING_FEES.get(order_type.upper(), TRADING_FEES['DEFAULT'])
            
            # Calculate order value
            order_value = price * quantity
            
            # Calculate fee amount
            fee_amount = order_value * Decimal(str(fee_rate))
            
            # For futures orders with leverage, adjust the fee calculation
            if order_type.lower() == 'futures' and leverage > 1:
                # Calculate effective order value with leverage
                effective_order_value = order_value * Decimal(str(leverage))
                # Recalculate fees on effective order value
                fee_amount = effective_order_value * Decimal(str(fee_rate))
            
            # Determine fee asset (typically the quote currency or base currency)
            fee_asset = self.base_currency or "USDT"
            
            # Extract base currency from symbol (e.g., BTCUSDT -> USDT)
            if symbol.endswith(self.base_currency):
                fee_asset = self.base_currency
            elif symbol.endswith("USDT"):
                fee_asset = "USDT"
            elif symbol.endswith("BTC"):
                fee_asset = "BTC"
            elif symbol.endswith("ETH"):
                fee_asset = "ETH"
            elif symbol.endswith("USDC"):
                fee_asset = "USDC"
            
            logger.debug(f"Calculated fee for {symbol}: {fee_amount} {fee_asset}")
            return fee_amount, fee_asset
            
        except Exception as e:
            logger.error(f"Error calculating fees: {e}")
            # Use base_currency for default fee asset if available
            base_currency = self.base_currency or "USDT"
            return Decimal('0'), base_currency

    async def check_symbol_validity(self, symbol: str) -> bool:
        """Check if a symbol is valid on Bybit"""
        try:
            # First try to get ticker info
            response = await self.make_request('get_tickers', params={"category": self._trading_category, "symbol": symbol})
            
            if response['retCode'] == 0 and response['result']:
                logger.info(f"Symbol {symbol} validated via ticker info")
                return True
            
            # If ticker fails, try instruments info
            instruments_response = await self.make_request('get_instruments_info', params={"category": self._trading_category, "symbol": symbol})
            
            if instruments_response['retCode'] == 0 and instruments_response['result']:
                logger.info(f"Symbol {symbol} validated via instruments info")
                return True
            
            # If both checks fail, symbol is invalid
            logger.warning(f"Symbol {symbol} is invalid on Bybit")
            
            # Save invalid symbol to database if configured
            if hasattr(self, 'mongo_client') and self.mongo_client:
                try:
                    await self.mongo_client.save_invalid_symbol(symbol)
                    logger.info(f"Saved invalid symbol {symbol} to database")
                except Exception as e:
                    logger.error(f"Failed to save invalid symbol {symbol} to database: {e}")
            
            return False
            
        except Exception as e:
            logger.error(f"Error checking symbol validity for {symbol}: {e}")
            return False

    async def filter_valid_symbols(self, symbols: List[str]) -> List[str]:
        """Filter out invalid symbols from a list
        
        Args:
            symbols: List of symbols to check
            
        Returns:
            List[str]: List of valid symbols
        """
        valid_symbols = []
        invalid_symbols = set()
        
        for symbol in symbols:
            # Skip known invalid symbols
            if symbol in invalid_symbols:
                continue
            
            try:
                if await self.check_symbol_validity(symbol):
                    valid_symbols.append(symbol)
                    logger.info(f"Symbol {symbol} is valid")
                else:
                    invalid_symbols.add(symbol)
                    logger.warning(f"Symbol {symbol} is invalid")
            except Exception as e:
                logger.error(f"Error checking symbol {symbol}: {e}")
                invalid_symbols.add(symbol)
        
        return valid_symbols

    async def create_tp_sl_orders(self, order: Order) -> tuple:
        """Create take profit and stop loss orders for a filled order"""
        try:
            tp_created = False
            sl_created = False
            partial_tps_created = []
            trailing_sl_created = False
            
            # Don't proceed if order is not filled
            if order.status != OrderStatus.FILLED:
                logger.warning(f"Cannot create TP/SL for order {order.order_id} that is not filled")
                return False, False, [], False
                
            # Get the current configuration for TP/SL
            tp_percentage = self.default_tp_percentage
            sl_percentage = self.default_sl_percentage
            
            # Check for partial take profits config
            partial_tp_enabled = False
            partial_tp_levels = []
            
            if self.config and 'trading' in self.config:
                if 'partial_take_profits' in self.config['trading']:
                    tp_config = self.config['trading']['partial_take_profits']
                    partial_tp_enabled = tp_config.get('enabled', False)
                    partial_tp_levels = tp_config.get('levels', [])
                    
            # Check for trailing stop loss config
            trailing_sl_enabled = False
            trailing_sl_activation = 0.0
            trailing_sl_callback = 0.0
            
            if self.config and 'trading' in self.config:
                if 'trailing_stop_loss' in self.config['trading']:
                    sl_config = self.config['trading']['trailing_stop_loss']
                    trailing_sl_enabled = sl_config.get('enabled', False)
                    trailing_sl_activation = float(sl_config.get('activation_percentage', 1.0))
                    trailing_sl_callback = float(sl_config.get('callback_rate', 0.5))
                    
            # Calculate TP and SL prices
            entry_price = order.price
            
            # Process partial take profits if enabled in config
            if partial_tp_enabled:
                try:
                    order.partial_take_profits = []
                    for level_config in partial_tp_levels:
                        level = level_config['level'] 
                        profit_percentage = Decimal(str(level_config['profit_percentage']))
                        position_percentage = Decimal(str(level_config['position_percentage']))
                        
                        # Calculate TP price based on entry price
                        tp_price = entry_price * (Decimal('1') + profit_percentage / Decimal('100'))
                        
                        # Create partial TP object
                        partial_tp = PartialTakeProfit(
                            level=level,
                            price=tp_price,
                            profit_percentage=float(profit_percentage),
                            position_percentage=float(position_percentage),
                            status=TPSLStatus.PENDING
                        )
                        
                        order.partial_take_profits.append(partial_tp)
                        
                    logger.info(f"Added {len(order.partial_take_profits)} partial take profit levels")
                except Exception as e:
                    logger.error(f"Error setting up partial take profits: {e}")
            
            # Always convert to Decimal when doing calculations, even if TP is disabled
            if tp_percentage is not None and tp_percentage > 0:
                tp_percentage = Decimal(str(tp_percentage))
                tp_price = entry_price * (Decimal('1') + tp_percentage / Decimal('100'))
                order.take_profit = TakeProfit(
                    price=tp_price,
                    percentage=float(tp_percentage),
                    status=TPSLStatus.PENDING
                )
            else:
                # If TP percentage is 0 or None, disable take profit
                order.take_profit = None
                logger.info(f"Take profit disabled (percentage is {tp_percentage})")
            
            if sl_percentage is not None and sl_percentage > 0:
                sl_percentage = Decimal(str(sl_percentage))
                sl_price = entry_price * (Decimal('1') - sl_percentage / Decimal('100'))
                order.stop_loss = StopLoss(
                    price=sl_price,
                    percentage=float(sl_percentage),
                    status=TPSLStatus.PENDING
                )
            else:
                # If SL percentage is 0 or None, disable stop loss
                order.stop_loss = None
                logger.info(f"Stop loss disabled (percentage is {sl_percentage})")
            
            # Setup trailing stop loss if enabled
            if trailing_sl_enabled:
                try:
                    activation_percentage = Decimal(str(self.config['trading']['trailing_stop_loss']['activation_percentage']))
                    callback_rate = Decimal(str(self.config['trading']['trailing_stop_loss']['callback_rate']))
                    
                    # Calculate activation price (price at which trailing begins)
                    activation_price = entry_price * (Decimal('1') + activation_percentage / Decimal('100'))
                    
                    # Initial stop price is based on callback from activation price
                    initial_stop_price = activation_price * (Decimal('1') - callback_rate / Decimal('100'))
                    
                    # Create trailing stop loss object
                    order.trailing_stop_loss = TrailingStopLoss(
                        activation_percentage=float(activation_percentage),
                        callback_rate=float(callback_rate),
                        initial_price=entry_price,
                        activation_price=activation_price,
                        current_stop_price=initial_stop_price,
                        highest_price=Decimal('0'),  # Will be set when activated
                        status=TPSLStatus.PENDING
                    )
                    
                    logger.info(f"Added trailing stop loss: activation at {float(activation_percentage)}%, callback rate {float(callback_rate)}%")
                except Exception as e:
                    logger.error(f"Error setting up trailing stop loss: {e}")
            
            return (order.take_profit, order.stop_loss)
            
        except Exception as e:
            logger.error(f"Error creating TP/SL orders: {e}")
            return False, False, [], False
            
    async def check_tp_sl_triggers(self, order: Order) -> Dict[str, bool]:
        """Check if take profit or stop loss has been triggered for an order"""
        try:
            result = {
                'tp_triggered': False,
                'sl_triggered': False,
                'partial_tp_triggered': [],
                'trailing_sl_updated': False
            }
            
            # Don't proceed if order is not filled
            if order.status != OrderStatus.FILLED:
                return result
                
            # Get current price
            current_price = await self.get_current_price(order.symbol)
            if current_price is None:
                logger.warning(f"Unable to get current price for {order.symbol}, skipping TP/SL check")
                return result
                
            current_price = Decimal(str(current_price))
            
            # Check take profit
            if order.take_profit and order.take_profit.status == TPSLStatus.PENDING:
                if current_price >= order.take_profit.price:
                    logger.info(f"Take profit triggered for {order.symbol} at ${float(current_price):,.2f}")
                    
                    # Update take profit status
                    order.take_profit.status = TPSLStatus.TRIGGERED
                    order.take_profit.triggered_at = datetime.utcnow()
                    
                    result['tp_triggered'] = True
                    
            # Check stop loss
            if order.stop_loss and order.stop_loss.status == TPSLStatus.PENDING:
                if current_price <= order.stop_loss.price:
                    logger.info(f"Stop loss triggered for {order.symbol} at ${float(current_price):,.2f}")
                    
                    # Update stop loss status
                    order.stop_loss.status = TPSLStatus.TRIGGERED
                    order.stop_loss.triggered_at = datetime.utcnow()
                    
                    result['sl_triggered'] = True
                    
            # Check partial take profits
            for partial_tp in order.partial_take_profits:
                if partial_tp.status == TPSLStatus.PENDING and current_price >= partial_tp.price:
                    logger.info(f"Partial take profit level {partial_tp.level} triggered for {order.symbol} at ${float(current_price):,.2f}")
                    
                    # Update partial take profit status
                    partial_tp.status = TPSLStatus.TRIGGERED
                    partial_tp.triggered_at = datetime.utcnow()
                    
                    result['partial_tp_triggered'].append(partial_tp.level)
                    
            # Check and update trailing stop loss
            if order.trailing_stop_loss and order.trailing_stop_loss.status == TPSLStatus.PENDING:
                # Check if price has reached activation level
                if current_price >= order.trailing_stop_loss.activation_price and not order.trailing_stop_loss.activated_at:
                    logger.info(f"Trailing stop loss activated for {order.symbol} at ${float(current_price):,.2f}")
                    
                    # Mark as activated
                    order.trailing_stop_loss.activated_at = datetime.utcnow()
                    order.trailing_stop_loss.highest_price = current_price
                    
                    # Calculate new stop price
                    callback_amount = current_price * (order.trailing_stop_loss.callback_rate / 100)
                    new_stop_price = current_price - callback_amount
                    
                    # Update stop price
                    order.trailing_stop_loss.current_stop_price = self._align_price_to_tick(order.symbol, new_stop_price)
                    
                    result['trailing_sl_updated'] = True
                    
                # If already activated, check if price has moved higher
                elif order.trailing_stop_loss.activated_at and current_price > order.trailing_stop_loss.highest_price:
                    logger.info(f"Trailing stop updated for {order.symbol} - new high: ${float(current_price):,.2f}")
                    
                    # Update highest seen price
                    order.trailing_stop_loss.highest_price = current_price
                    
                    # Calculate new stop price
                    callback_amount = current_price * (order.trailing_stop_loss.callback_rate / 100)
                    new_stop_price = current_price - callback_amount
                    
                    # Update stop price if new one is higher
                    if new_stop_price > order.trailing_stop_loss.current_stop_price:
                        order.trailing_stop_loss.current_stop_price = self._align_price_to_tick(order.symbol, new_stop_price)
                        result['trailing_sl_updated'] = True
                        
                # Check if price has dropped below stop level (for activated trailing stops)
                elif (order.trailing_stop_loss.activated_at and 
                     current_price <= order.trailing_stop_loss.current_stop_price and
                     not result['sl_triggered']):  # Don't trigger both SL and trailing SL
                    
                    logger.info(f"Trailing stop loss triggered for {order.symbol} at ${float(current_price):,.2f}")
                    
                    # Update trailing stop loss status
                    order.trailing_stop_loss.status = TPSLStatus.TRIGGERED
                    order.trailing_stop_loss.triggered_at = datetime.utcnow()
                    
                    # Also mark regular SL as triggered to avoid confusion
                    if order.stop_loss:
                        order.stop_loss.status = TPSLStatus.TRIGGERED
                        order.stop_loss.triggered_at = datetime.utcnow()
                        
                    result['sl_triggered'] = True
                    
            return result
            
        except Exception as e:
            logger.error(f"Error checking TP/SL triggers: {e}")
            return {
                'tp_triggered': False,
                'sl_triggered': False,
                'partial_tp_triggered': [],
                'trailing_sl_updated': False
            }

    async def get_candles_for_chart(self, symbol: str, timeframe: TimeFrame, count: int = 15) -> List[Dict]:
        """Get candle data for chart generation - Bybit implementation"""
        try:
            # Map timeframe to Bybit interval
            interval_map = {
                TimeFrame.DAILY: "60",  # 1h
                TimeFrame.WEEKLY: "240",  # 4h 
                TimeFrame.MONTHLY: "D"  # 1d
            }
            
            bybit_interval = interval_map.get(timeframe, "60")  # default to 1h
            
            # Calculate start time (count+5 candles back for better visualization)
            now = datetime.utcnow()
            
            # Calculate how many minutes to go back based on interval
            if bybit_interval == "60":
                minutes_back = (count + 5) * 60  # 1h interval
            elif bybit_interval == "240":
                minutes_back = (count + 5) * 240  # 4h interval
            elif bybit_interval == "D":
                minutes_back = (count + 5) * 1440  # 1d interval
            else:
                minutes_back = (count + 5) * 60  # Default to 1h
            
            start_time = int((now - timedelta(minutes=minutes_back)).timestamp() * 1000)
            
            # Get kline/candle data from Bybit
            await self.rate_limiter.acquire()
            response = self.client.get_kline(
                category=self._trading_category,
                symbol=symbol,
                interval=bybit_interval,
                limit=count + 5  # Request a few extra candles
            )
            
            if not response or response.get("retCode") != 0:
                logger.error(f"Failed to get candles: {response.get('retMsg', 'Unknown error')}")
                return []
            
            candles_data = response.get("result", {}).get("list", [])
            
            # Bybit returns candles in reverse order (newest first), so reverse them
            candles_data.reverse()
            
            # Format candles for chart generator
            # Bybit format: [timestamp, open, high, low, close, volume, ...]
            formatted_candles = []
            for candle in candles_data:
                # Only take the most recent 'count' candles
                if len(formatted_candles) >= count:
                    break
                
                formatted_candles.append({
                    'open_time': int(candle[0]),  # timestamp
                    'timestamp': int(candle[0]),  # add timestamp field for validator
                    'open': float(candle[1]),     # open
                    'high': float(candle[2]),     # high
                    'low': float(candle[3]),      # low
                    'close': float(candle[4]),    # close
                    'volume': float(candle[5])    # volume
                })
            
            return formatted_candles
            
        except Exception as e:
            logger.error(f"Error getting candles for {symbol}: {e}")
            return []
        
    async def generate_trade_chart(self, order: Order) -> Optional[bytes]:
        """Generate a chart for a trade order - Bybit implementation"""
        try:
            # Get candles for the symbol
            candles = await self.get_candles_for_chart(
                order.symbol, 
                order.timeframe,
                count=15  # Show 15 candles
            )
            
            if not candles:
                logger.warning(f"No candle data available for {order.symbol}")
                return None
            
            # Get reference price if available
            reference_price = None
            if order.symbol in self.reference_prices and order.timeframe in self.reference_prices[order.symbol]:
                reference_price = Decimal(str(self.reference_prices[order.symbol][order.timeframe]))
            
            # Generate chart using the chart generator
            return await self.chart_generator.generate_trade_chart(
                candles, 
                order,
                reference_price=reference_price
            )
        except Exception as e:
            logger.error(f"Error generating trade chart: {e}")
            return None

    async def check_connection(self, retries=3):
        """Check connection to Bybit API with robust error handling.
        
        Args:
            retries: Number of retries to attempt
            
        Returns:
            bool: True if connection is valid, False otherwise
        """
        logger.debug("Checking connection to Bybit API...")
        
        # First check: Get server time
        server_time = await self.get_server_time()
        if not server_time:
            logger.error("Failed to get server time from Bybit API")
            
            # Try one more time with longer timeout
            logger.debug("Retrying server time check with longer timeout...")
            server_time = await self.get_server_time()
            if not server_time:
                return False
            
        # Second check: Try to get ticker or balances
        try:
            if self.authenticated:
                # Try to get wallet balances - we're not concerned with the actual values,
                # just whether the API responds properly
                balance_response = await self.get_balances()
                
                if not balance_response or (isinstance(balance_response, dict) and balance_response.get('retCode', -1) != 0):
                    # If we get an auth error but server is responding, consider it "connected"
                    if isinstance(balance_response, dict) and balance_response.get('retCode') in self.AUTH_ERROR_CODES:
                        logger.warning(f"API credentials issue: {balance_response.get('retMsg', 'Unknown error')}")
                        return True
                        
                    # Fall back to public endpoint check if auth fails
                    logger.warning("Authenticated request failed, falling back to public endpoint check")
                    ticker = await self.get_ticker("BTCUSDT")
                    
                    if not ticker or not isinstance(ticker, dict) or len(ticker) == 0:
                        logger.error("Failed to get BTCUSDT ticker from Bybit API")
                        return False
                        
                    logger.info("Successfully connected to Bybit API (public endpoints only)")
                    return True
                    
                # If we got a successful response, connection is good
                logger.debug("Successfully connected to Bybit API (authenticated)")
                return True
            else:
                # For unauthenticated clients, just check if we can get public data
                # Try multiple major coins in case one has API issues
                for test_symbol in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
                    ticker = await self.get_ticker(test_symbol)
                    if ticker and isinstance(ticker, dict) and len(ticker) > 0:
                        logger.debug(f"Successfully connected to Bybit API using {test_symbol} ticker")
                        return True
                
                logger.error("Failed to get any test ticker from Bybit API")
                return False
                
        except Exception as e:
            logger.error(f"Error checking connection to Bybit API: {str(e)}")
            return False

    async def get_historical_prices(self, symbol: str, days: int = 30) -> List[Dict]:
        """Get historical price data for a symbol for specified number of days"""
        try:
            now = datetime.utcnow()
            start_time = int((now - timedelta(days=days)).timestamp() * 1000)
            
            # Use daily klines for longer periods
            await self.rate_limiter.acquire()
            response = self.client.get_kline(
                category=self._trading_category,
                symbol=symbol,
                interval="D",  # Daily candles
                limit=200  # Maximum allowed by Bybit
            )
            
            if not response or response.get("retCode") != 0:
                logger.error(f"Failed to get historical prices: {response.get('retMsg', 'Unknown error')}")
                return []
            
            candles_data = response.get("result", {}).get("list", [])
            
            # Bybit returns candles in reverse order (newest first), so reverse for chronological order
            candles_data.reverse()
            
            # Format candles for historical prices
            # Bybit format: [timestamp, open, high, low, close, volume, ...]
            formatted_data = []
            for candle in candles_data:
                # Convert timestamp to datetime
                dt = datetime.fromtimestamp(int(candle[0]) / 1000)
                
                # Only include data within the requested time period
                if dt >= (now - timedelta(days=days)):
                    formatted_data.append({
                        'timestamp': dt,
                        'open': float(candle[1]),
                        'high': float(candle[2]),
                        'low': float(candle[3]),
                        'close': float(candle[4]),
                        'volume': float(candle[5])
                    })
                
            return formatted_data
            
        except Exception as e:
            logger.error(f"Error getting historical prices for {symbol}: {e}")
            return []
        
    async def get_historical_benchmark(self, symbol: str, days: int = 90) -> Dict:
        """Get historical benchmark data for comparison"""
        try:
            # Get historical prices for the specified symbol
            prices = await self.get_historical_prices(symbol, days)
            
            if not prices:
                logger.warning(f"No historical prices available for {symbol}")
                return {'status': 'error', 'message': 'No data available'}
            
            # Get simulated S&P 500 data for comparison
            sp500_data = await self._get_simulated_sp500_data(days)
            
            # Calculate daily returns
            btc_returns = []
            dates = []
            
            for i in range(1, len(prices)):
                prev_close = prices[i-1]['close']
                curr_close = prices[i]['close']
                
                if prev_close > 0:
                    daily_return = (curr_close - prev_close) / prev_close * 100
                    btc_returns.append(daily_return)
                    dates.append(prices[i]['timestamp'])
                
            # Calculate cumulative returns
            btc_cumulative = [0]
            for ret in btc_returns:
                btc_cumulative.append(btc_cumulative[-1] + ret)
            
            # Combine data
            benchmark_data = {
                'status': 'ok',
                'symbol': symbol,
                'days': days,
                'dates': dates,
                'btc_daily_returns': btc_returns,
                'btc_cumulative_returns': btc_cumulative[1:],  # Skip the initial 0
                'sp500_daily_returns': sp500_data.get('daily_returns', []),
                'sp500_cumulative_returns': sp500_data.get('cumulative_returns', [])
            }
            
            return benchmark_data
            
        except Exception as e:
            logger.error(f"Error getting benchmark data: {e}")
            return {'status': 'error', 'message': str(e)}
        
    async def _get_simulated_sp500_data(self, days: int = 90) -> Dict:
        """Get simulated S&P 500 data for comparison"""
        try:
            # Use the Yahoo scraper to get real S&P 500 data
            sp500_data = await self.yahoo_scraper.get_sp500_data(days)
            
            if sp500_data and 'status' in sp500_data and sp500_data['status'] == 'ok':
                return sp500_data
            
            # Fallback to simulated data if scraper fails
            logger.warning("Using simulated S&P 500 data as fallback")
            
            # Create simulated data with realistic volatility
            avg_daily_return = 0.03  # ~8% annualized
            volatility = 1.0  # Standard deviation of daily returns
            
            # Generate random daily returns with realistic parameters
            import numpy as np
            np.random.seed(42)  # For reproducibility
            
            daily_returns = np.random.normal(avg_daily_return / 252, volatility / np.sqrt(252), days)
            daily_returns = daily_returns.tolist()
            
            # Calculate cumulative returns
            cumulative_returns = [0]
            for ret in daily_returns:
                cumulative_returns.append(cumulative_returns[-1] + ret * 100)  # Convert to percentage
            
            # Generate dates
            now = datetime.utcnow()
            dates = [(now - timedelta(days=days-i)).strftime('%Y-%m-%d') for i in range(days)]
            
            return {
                'status': 'ok',
                'source': 'simulated',
                'dates': dates,
                'daily_returns': daily_returns,
                'cumulative_returns': cumulative_returns[1:]  # Skip the initial 0
            }
            
        except Exception as e:
            logger.error(f"Error getting S&P 500 data: {e}")
            return {'status': 'error', 'message': str(e)}
        
    async def get_btc_ytd_performance(self) -> Dict[str, float]:
        """Get Bitcoin year-to-date performance data"""
        try:
            # Get the current year
            current_year = datetime.utcnow().year
            start_of_year = datetime(current_year, 1, 1)
            days_since_start = (datetime.utcnow() - start_of_year).days + 1
            
            # Get historical prices starting from January 1st
            prices = await self.get_historical_prices("BTCUSDT", days=days_since_start)
            
            if not prices:
                logger.warning("No BTC price data available for YTD calculation")
                return {'status': 'error', 'message': 'No data available'}
            
            # Get first and most recent prices
            if len(prices) > 0:
                start_price = prices[0]['close'] if prices[0]['close'] > 0 else None
                current_price = prices[-1]['close'] if prices[-1]['close'] > 0 else None
                
                if start_price and current_price:
                    ytd_change = (current_price - start_price) / start_price * 100
                    
                    # Get highest price in the period
                    highest_price = max(price['high'] for price in prices)
                    peak_change = (highest_price - start_price) / start_price * 100
                    
                    # Get lowest price in the period
                    lowest_price = min(price['low'] for price in prices)
                    trough_change = (lowest_price - start_price) / start_price * 100
                    
                    # Calculate monthly performance
                    monthly_data = {}
                    for price_point in prices:
                        date = price_point['timestamp']
                        month = date.month
                        
                        if month not in monthly_data:
                            monthly_data[month] = {
                                'start_price': price_point['close'],
                                'end_price': price_point['close'],
                                'month_name': date.strftime('%b')
                            }
                        else:
                            monthly_data[month]['end_price'] = price_point['close']
                    
                    # Calculate monthly changes
                    monthly_changes = {}
                    for month, data in monthly_data.items():
                        if data['start_price'] > 0:
                            pct_change = (data['end_price'] - data['start_price']) / data['start_price'] * 100
                            monthly_changes[data['month_name']] = pct_change
                    
                    return {
                        'status': 'ok',
                        'ytd_change': ytd_change,
                        'peak_change': peak_change,
                        'trough_change': trough_change,
                        'start_price': start_price,
                        'current_price': current_price,
                        'highest_price': highest_price,
                        'lowest_price': lowest_price,
                        'monthly_performance': monthly_changes,
                        'year': current_year
                    }
            
            return {'status': 'error', 'message': 'Insufficient data for calculation'}
            
        except Exception as e:
            logger.error(f"Error getting BTC YTD performance: {e}")
            return {'status': 'error', 'message': str(e)}
        
    async def generate_ytd_comparison_chart(self) -> Optional[bytes]:
        """Generate a year-to-date comparison chart between BTC and S&P 500"""
        try:
            # Get BTC YTD performance
            btc_data = await self.get_btc_ytd_performance()
            
            if btc_data.get('status') != 'ok':
                logger.warning("Failed to get BTC YTD data for chart")
                return None
            
            # Get S&P 500 YTD data
            current_year = datetime.utcnow().year
            start_of_year = datetime(current_year, 1, 1)
            days_since_start = (datetime.utcnow() - start_of_year).days + 1
            
            sp500_data = await self._get_simulated_sp500_data(days=days_since_start)
            
            if sp500_data.get('status') != 'ok':
                logger.warning("Failed to get S&P 500 YTD data for chart")
                return None
            
            # Use chart generator to create comparison chart
            chart_bytes = await self.chart_generator.generate_ytd_comparison_chart(
                btc_data=btc_data,
                sp500_data=sp500_data,
                year=current_year
            )
            
            return chart_bytes
            
        except Exception as e:
            logger.error(f"Error generating YTD comparison chart: {e}")
            return None

    async def make_request(self, method_name, params=None, retry_count=3, initial_backoff=1):
        """Make a request to the Bybit API with improved error handling and retry logic.
        
        Args:
            method_name: API method name
            params: Request parameters
            retry_count: Number of retries for certain errors
            initial_backoff: Initial backoff time in seconds
            
        Returns:
            API response or None if failed
        """
        if not self.session:
            self.session = self.create_session()
            
        if method_name not in self.methods:
            logger.error(f"Method {method_name} not found in Bybit API methods")
            return None
            
        method = self.methods[method_name]
        
        # Setup default parameters and merge with provided ones
        request_params = {}
        if params:
            request_params.update(params)
            
        current_retry = 0
        max_retries = retry_count
        
        # For system errors (10016), use more aggressive retry strategy
        system_error_retry_count = 0
        max_system_error_retries = 5  # Allow more retries for system errors
        
        while current_retry <= max_retries:
            try:
                # Different structure for GET vs POST
                if method['method'] == 'GET':
                    if 'header' in method and method['header']:
                        # Authenticated request
                        timestamp = int(time.time() * 1000)
                        request_params['timestamp'] = timestamp
                        
                        # Add authentication headers
                        headers = self.generate_headers(request_params)
                        
                        url = f"{self.api_url}{method['url']}"
                        if request_params:
                            # Convert params to query string
                            query = '&'.join([f"{k}={v}" for k, v in request_params.items()])
                            url = f"{url}?{query}"
                            
                        async with self.session.get(url, headers=headers) as response:
                            response_json = await response.json()
                    else:
                        # Public request
                        url = f"{self.api_url}{method['url']}"
                        if request_params:
                            # Convert params to query string
                            query = '&'.join([f"{k}={v}" for k, v in request_params.items()])
                            url = f"{url}?{query}"
                            
                        async with self.session.get(url) as response:
                            response_json = await response.json()
                else:
                    # POST request
                    url = f"{self.api_url}{method['url']}"
                    timestamp = int(time.time() * 1000)
                    request_params['timestamp'] = timestamp
                    
                    headers = self.generate_headers(request_params)
                    
                    async with self.session.post(url, headers=headers, data=json.dumps(request_params)) as response:
                        response_json = await response.json()
                
                # Check for specific error codes that should trigger retry
                if isinstance(response_json, dict) and 'retCode' in response_json:
                    retcode = response_json['retCode']
                    
                    # Special handling for different error types
                    if retcode in self.RETRY_ERROR_CODES:
                        # For critical system errors, use dedicated retry strategy
                        if retcode == 10016:  # Internal system error
                            if system_error_retry_count < max_system_error_retries:
                                # Use longer backoff times for system errors, with increasing duration
                                backoff_time = initial_backoff * (2 ** system_error_retry_count) + random.uniform(1, 3)
                                logger.warning(f"Bybit system error (10016) for {method_name}. Applying special retry strategy in {backoff_time:.2f}s ({system_error_retry_count+1}/{max_system_error_retries})")
                                await asyncio.sleep(backoff_time)
                                system_error_retry_count += 1
                                # Don't increment the regular retry counter, use separate counter
                                continue
                        # For rate limit errors, use specific backoff
                        elif retcode in self.LIMIT_ERROR_CODES:
                            if current_retry < max_retries:
                                # Use longer backoff for rate limits
                                backoff_time = max(initial_backoff * (2 ** current_retry), 3) + random.uniform(0, 1)
                                logger.warning(f"Bybit rate limit error {retcode} for {method_name}. Retrying in {backoff_time:.2f}s ({current_retry+1}/{max_retries})")
                                await asyncio.sleep(backoff_time)
                                current_retry += 1
                                continue
                        # For other retryable errors
                        elif current_retry < max_retries:
                            backoff_time = initial_backoff * (2 ** current_retry) + random.uniform(0, 1)
                            logger.warning(f"Bybit API error {retcode} for {method_name}. Retrying in {backoff_time:.2f}s ({current_retry+1}/{max_retries})")
                            await asyncio.sleep(backoff_time)
                            current_retry += 1
                            continue
                
                return response_json
                
            except aiohttp.ClientConnectionError as e:
                # Network connection errors should have aggressive retry
                if current_retry < max_retries:
                    backoff_time = initial_backoff * (2 ** current_retry) + random.uniform(0, 2)
                    logger.error(f"Connection error for {method_name}: {str(e)}. Retrying in {backoff_time:.2f}s ({current_retry+1}/{max_retries})")
                    await asyncio.sleep(backoff_time)
                    current_retry += 1
                else:
                    logger.error(f"Connection error for {method_name} after {max_retries} retries: {str(e)}")
                    return None
                    
            except Exception as e:
                if current_retry < max_retries:
                    # Calculate backoff with exponential increase and jitter
                    backoff_time = initial_backoff * (2 ** current_retry) + random.uniform(0, 1)
                    logger.error(f"Error making request to {method_name}: {str(e)}. Retrying in {backoff_time:.2f}s ({current_retry+1}/{max_retries})")
                    await asyncio.sleep(backoff_time)
                    current_retry += 1
                else:
                    logger.error(f"Failed to make request to {method_name} after {max_retries} retries: {str(e)}")
                    return None
        
        # If we get here, we've exhausted retries
        if system_error_retry_count >= max_system_error_retries:
            logger.error(f"Exhausted {max_system_error_retries} system error retries for {method_name}")
        else:
            logger.error(f"Exhausted {max_retries} retries for {method_name}")
            
        return None

    async def get_server_time(self):
        """Get server time with improved error handling.
        
        Returns:
            int: Server timestamp in milliseconds or None if failed
        """
        try:
            response = await self.make_request('server_time', retry_count=3)
            
            if not response:
                logger.warning("Empty response when requesting server time")
                return None
                
            if isinstance(response, dict):
                if 'retCode' in response and response['retCode'] != 0:
                    error_code = response.get('retCode')
                    error_msg = response.get('retMsg', 'Unknown error')
                    
                    error_desc = self.ERROR_CODES.get(error_code, "Unknown error")
                    logger.error(f"Error getting server time: {error_msg} (Code: {error_code} - {error_desc})")
                    return None
                    
                if 'result' in response and 'timeSecond' in response['result']:
                    return int(response['result']['timeSecond']) * 1000
                
            logger.warning("Failed to extract server time from response")
            return None
            
        except Exception as e:
            logger.error(f"Error getting server time: {str(e)}")
            return None

    def _parse_tp_sl_setting(self, setting) -> float:
        """Parse take profit or stop loss setting that can be in percentage format or numeric
        
        Args:
            setting: Take profit or stop loss setting (e.g., "5%", 5)
            
        Returns:
            Parsed value as float
        """
        try:
            if isinstance(setting, str) and '%' in setting:
                # Remove percentage sign and convert to float
                return float(setting.replace('%', ''))
            elif isinstance(setting, (int, float)):
                # Already a number, just convert to float
                return float(setting)
            else:
                # Default case if input is unexpected
                logger.warning(f"Unexpected TP/SL setting format: {setting}, using default value")
                return 0.0
        except Exception as e:
            logger.error(f"Error parsing TP/SL setting '{setting}': {e}")
            return 0.0

    async def get_balances(self, asset=None):
        """Get account balances with improved error handling.
        
        Args:
            asset: Optional specific asset to get balance for
            
        Returns:
            dict: Account balances or empty dict if failed
        """
        try:
            params = {"accountType": "UNIFIED"}
            if self.category == "spot":
                params["accountType"] = "SPOT"
            elif self.category == "linear":
                params["accountType"] = "CONTRACT"
                
            response = await self.make_request('get_wallet_balance', params=params, retry_count=3)
                
            if not response:
                logger.warning("Empty response when requesting balances")
                return {}
                
            # Check for API errors
            if isinstance(response, dict) and 'retCode' in response:
                if response['retCode'] != 0:
                    error_code = response.get('retCode')
                    error_msg = response.get('retMsg', 'Unknown error')
                    
                    error_desc = self.ERROR_CODES.get(error_code, "Unknown error")
                    
                    # Special handling for internal system errors
                    if error_code == 10016:
                        logger.error(f"Bybit internal system error (10016) when getting balances: {error_msg}")
                        # Return empty dict to prevent downstream errors
                        return {}
                    else:
                        logger.error(f"Error getting balances: {error_msg} (Code: {error_code} - {error_desc})")
                        # Return empty dict to prevent downstream errors
                        return {}
                        
            # If we have a specific asset requested, filter the response
            if asset and response and 'retCode' in response and response['retCode'] == 0:
                if 'result' in response and 'list' in response['result']:
                    for account in response['result']['list']:
                        if 'coin' in account:
                            for coin_data in account['coin']:
                                if coin_data.get('coin') == asset:
                                    # Return the specific asset data
                                    return {
                                        asset: {
                                            'free': float(coin_data.get('free', 0)),
                                            'locked': float(coin_data.get('locked', 0)),
                                            'total': float(coin_data.get('walletBalance', 0))
                                        }
                                    }
            
            # Process full balance response in a consistent format
            if 'result' in response and 'list' in response['result']:
                processed_balances = {}
                
                for account in response['result']['list']:
                    if 'coin' in account:
                        for coin_data in account['coin']:
                            symbol = coin_data.get('coin')
                            
                            if symbol:
                                processed_balances[symbol] = {
                                    'free': float(coin_data.get('free', 0)),
                                    'locked': float(coin_data.get('locked', 0)),
                                    'total': float(coin_data.get('walletBalance', 0))
                                }
                
                return processed_balances
            
            # If we couldn't process the response in a structured way, just return it as-is
            return response
            
        except Exception as e:
            logger.error(f"Error getting balances: {str(e)}")
            return {}

    async def get_ticker(self, symbol):
        """Get current ticker information with robust error handling.
        
        Args:
            symbol: Trading pair symbol (e.g., "BTCUSDT")
            
        Returns:
            dict: Ticker information or empty dict if failed
        """
        try:
            params = {"symbol": symbol, "category": self.category}
            response = await self.make_request('get_tickers', params=params, retry_count=3)
            
            if not response:
                logger.warning(f"Empty response when requesting ticker for {symbol}")
                return {}
                
            if isinstance(response, dict):
                if 'retCode' in response and response['retCode'] != 0:
                    error_code = response.get('retCode')
                    error_msg = response.get('retMsg', 'Unknown error')
                    
                    error_desc = self.ERROR_CODES.get(error_code, "Unknown error")
                    
                    # Special handling for system errors that often require more retries or backoff
                    if error_code == 10016:
                        logger.error(f"Bybit internal system error when getting ticker for {symbol}: {error_msg}")
                        # Try an alternative approach if possible
                        if symbol == "BTCUSDT" and self.category == "spot":
                            # Fall back to a different endpoint or category
                            logger.info(f"Trying alternative endpoint for {symbol}")
                            alt_params = {"symbol": symbol, "category": "linear"}
                            alt_response = await self.make_request('get_tickers', params=alt_params, retry_count=2)
                            if alt_response and 'retCode' in alt_response and alt_response['retCode'] == 0:
                                logger.info(f"Successfully retrieved {symbol} ticker via alternative endpoint")
                                return self._extract_ticker_data(alt_response)
                    else:
                        logger.error(f"Error getting ticker for {symbol}: {error_msg} (Code: {error_code} - {error_desc})")
                    
                    # Return empty dict to avoid downstream errors
                    return {}
                    
                return self._extract_ticker_data(response)
            
            # If we couldn't extract data properly, return empty dict
            logger.warning(f"Failed to extract ticker data for {symbol}")
            return {}
            
        except Exception as e:
            logger.error(f"Error getting ticker for {symbol}: {str(e)}")
            return {}
            
    def _extract_ticker_data(self, response):
        """Extract ticker data from response.
        
        Args:
            response: API response dictionary
            
        Returns:
            dict: Extracted ticker data or empty dict
        """
        try:
            if 'result' in response and 'list' in response['result']:
                ticker_list = response['result']['list']
                if ticker_list and len(ticker_list) > 0:
                    return ticker_list[0]
            return {}
        except Exception as e:
            logger.error(f"Error extracting ticker data: {str(e)}")
            return {}

    def create_session(self):
        """Create an aiohttp session for API requests.
        
        Returns:
            aiohttp.ClientSession: The created session
        """
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'Trade-a-saurus-Rex/1.0'
        }
        
        return aiohttp.ClientSession(headers=headers)
        
    def generate_headers(self, params):
        """Generate authentication headers for API requests.
        
        Args:
            params: The request parameters
            
        Returns:
            dict: Headers with authentication information
        """
        # Create a sorted query string
        query_string = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
        
        # Generate HMAC signature
        timestamp = str(int(time.time() * 1000))
        payload = timestamp + self.api_key + str(params.get('recv_window', 5000)) + query_string
        signature = hmac.new(
            bytes(self.api_secret, 'utf-8'),
            msg=bytes(payload, 'utf-8'),
            digestmod=hashlib.sha256
        ).hexdigest()
        
        # Create headers
        headers = {
            'X-BAPI-SIGN': signature,
            'X-BAPI-API-KEY': self.api_key,
            'X-BAPI-TIMESTAMP': timestamp,
            'X-BAPI-RECV-WINDOW': str(params.get('recv_window', 5000)),
            'Content-Type': 'application/json'
        }
        
        return headers