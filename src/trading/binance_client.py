from binance.client import AsyncClient
from binance.exceptions import BinanceAPIException
from decimal import Decimal
from datetime import datetime, timedelta
import asyncio
import logging
from typing import Dict, List, Optional, Tuple  # Add Tuple for return type hints
from ..types.models import Order, OrderStatus, TimeFrame
from ..utils.rate_limiter import RateLimiter
from ..types.constants import PRECISION, MIN_NOTIONAL, TIMEFRAME_INTERVALS, TRADING_FEES

logger = logging.getLogger(__name__)

class BinanceClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.client: Optional[AsyncClient] = None
        self.reference_prices: Dict[str, Dict[TimeFrame, float]] = {}
        self.triggered_thresholds: Dict[str, Dict[TimeFrame, List[float]]] = {}
        self.rate_limiter = RateLimiter()
        self.symbol_info = {}
        self.last_reset: Dict[TimeFrame, datetime] = {
            tf: datetime.utcnow() for tf in TimeFrame
        }
        self.balance_cache = {}
        self.reference_timestamps = {
            TimeFrame.DAILY: None,
            TimeFrame.WEEKLY: None,
            TimeFrame.MONTHLY: None
        }
        logger.setLevel(logging.DEBUG)  # Add this line
        
    async def initialize(self):
        self.client = await AsyncClient.create(
            api_key=self.api_key,
            api_secret=self.api_secret,
            testnet=self.testnet
        )
        # Get exchange info for precision
        exchange_info = await self.client.get_exchange_info()
        for symbol in exchange_info['symbols']:
            self.symbol_info[symbol['symbol']] = {
                'baseAssetPrecision': symbol['baseAssetPrecision'],
                'quotePrecision': symbol['quotePrecision'],
                'filters': {f['filterType']: f for f in symbol['filters']}
            }
        
    async def close(self):
        if self.client:
            await self.client.close_connection()
            
    async def check_timeframe_reset(self, timeframe: TimeFrame):
        """Check if timeframe needs to be reset"""
        now = datetime.utcnow()
        interval = TIMEFRAME_INTERVALS[timeframe.value.upper()]
        
        if now - self.last_reset[timeframe] >= interval:
            # Reset reference prices and triggered thresholds for this timeframe
            logger.info(f"Resetting {timeframe.value} thresholds")
            for symbol in self.reference_prices:
                self.reference_prices[symbol][timeframe] = None
                # Clear duplicates from triggered thresholds
                if symbol in self.triggered_thresholds:
                    unique_thresholds = list(dict.fromkeys(
                        self.triggered_thresholds[symbol][timeframe]
                    ))
                    self.triggered_thresholds[symbol][timeframe] = unique_thresholds
            self.last_reset[timeframe] = now
            
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
                if symbol not in self.reference_prices:
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
            
    async def check_thresholds(self, symbol: str, thresholds: Dict[str, List[float]]) -> Optional[tuple]:
        """Check price against thresholds and return (timeframe, threshold) if triggered"""
        try:
            await self.rate_limiter.acquire()
            ticker = await self.client.get_symbol_ticker(symbol=symbol)
            current_price = float(ticker['price'])
            
            for timeframe in TimeFrame:
                # Skip if timeframe not in thresholds
                if timeframe.value not in thresholds:
                    continue

                if symbol not in self.reference_prices:
                    continue
                    
                ref_price = self.reference_prices[symbol].get(timeframe)
                if not ref_price:
                    continue
                    
                price_change = ((ref_price - current_price) / ref_price) * 100
                logger.debug(f"{symbol} {timeframe.value} price change: {price_change:.2f}%")
                
                # Check thresholds from lowest to highest
                for threshold in sorted(thresholds[timeframe.value]):
                    if (price_change >= threshold and 
                        threshold not in self.triggered_thresholds[symbol][timeframe]):
                        logger.info(f"Threshold triggered for {symbol}: {threshold}% on {timeframe.value}")
                        self.triggered_thresholds[symbol][timeframe].append(threshold)
                        return timeframe, threshold
                        
            return None
            
        except Exception as e:
            logger.error(f"Error checking thresholds for {symbol}: {e}", exc_info=True)
            return None
            
    def _get_quantity_precision(self, symbol: str) -> int:
        """Get the required decimal precision for quantity"""
        if symbol in self.symbol_info:
            return self.symbol_info[symbol]['baseAssetPrecision']
        return PRECISION['DEFAULT']
        
    def _get_price_precision(self, symbol: str) -> int:
        """Get the required decimal precision for price"""
        if symbol in self.symbol_info:
            return self.symbol_info[symbol]['quotePrecision']
        return PRECISION['DEFAULT']
        
    async def calculate_fees(self, symbol: str, price: Decimal, quantity: Decimal) -> Tuple[Decimal, str]:
        """Calculate fees for an order"""
        if self.testnet:
            # Testnet simulation
            fee_rate = TRADING_FEES['DEFAULT']
            fee_amount = price * quantity * Decimal(str(fee_rate))
            fee_asset = 'USDT'
        else:
            try:
                trade_fee = await self.client.get_trade_fee(symbol=symbol)
                if trade_fee and trade_fee[0]:
                    fee_rate = Decimal(str(trade_fee[0].get('makerCommission', TRADING_FEES['MAKER'])))
                    fee_amount = price * quantity * fee_rate
                    fee_asset = trade_fee[0].get('feeCoin', 'USDT')
                else:
                    fee_rate = Decimal(str(TRADING_FEES['DEFAULT']))
                    fee_amount = price * quantity * fee_rate
                    fee_asset = 'USDT'
            except Exception as e:
                logger.warning(f"Failed to get trading fees for {symbol}, using default: {e}")
                fee_rate = Decimal(str(TRADING_FEES['DEFAULT']))
                fee_amount = price * quantity * fee_rate
                fee_asset = 'USDT'

        return fee_amount.quantize(Decimal('0.00000001')), fee_asset

    def _adjust_quantity_to_lot_size(self, symbol: str, quantity: Decimal) -> Decimal:
        """Adjust quantity to comply with lot size filter"""
        if symbol not in self.symbol_info:
            return quantity

        lot_size_filter = self.symbol_info[symbol]['filters'].get('LOT_SIZE', {})
        if not lot_size_filter:
            return quantity

        min_qty = Decimal(str(lot_size_filter.get('minQty', '0')))
        max_qty = Decimal(str(lot_size_filter.get('maxQty', '999999')))
        step_size = Decimal(str(lot_size_filter.get('stepSize', '0')))

        if step_size == 0:
            return quantity

        # Calculate precision from step size
        step_precision = abs(Decimal(str(step_size)).as_tuple().exponent)
        
        # Round to step size
        adjusted_qty = Decimal(str(float(quantity) - (float(quantity) % float(step_size))))
        adjusted_qty = adjusted_qty.quantize(Decimal('0.' + '0' * step_precision))

        # Ensure quantity is within bounds
        adjusted_qty = max(min_qty, min(adjusted_qty, max_qty))
        
        logger.debug(f"Adjusted quantity from {quantity} to {adjusted_qty} (step size: {step_size})")
        return adjusted_qty

    async def place_limit_buy_order(self, symbol: str, amount: float, 
                                  threshold: float, timeframe: TimeFrame, 
                                  is_manual: bool = False) -> Order:
        await self.rate_limiter.acquire()
        
        try:
            # Get current price
            ticker = await self.client.get_symbol_ticker(symbol=symbol)
            price = Decimal(ticker['price'])
            
            # Calculate quantity based on USDT amount
            quantity = Decimal(str(amount)) / price
            
            # Get and apply precision
            quantity_precision = self._get_quantity_precision(symbol)
            price_precision = self._get_price_precision(symbol)
            
            # Round quantity to precision and adjust for lot size
            quantity = Decimal(str(round(quantity, quantity_precision)))
            quantity = self._adjust_quantity_to_lot_size(symbol, quantity)
            price = Decimal(str(round(price, price_precision)))
            
            # Log order details before placement
            logger.info(f"Placing order: {symbol} quantity={quantity} price=${price}")
            
            # Calculate fees
            fees, fee_asset = await self.calculate_fees(symbol, price, quantity)
            
            # Check minimum notional
            min_notional = MIN_NOTIONAL.get(symbol, MIN_NOTIONAL['DEFAULT'])
            if price * quantity < Decimal(str(min_notional)):
                raise ValueError(f"Order value below minimum notional: {min_notional} USDT")
            
            # Only update triggered thresholds if it's not a manual trade
            # And only do it here, not when order is filled
            if not is_manual and symbol in self.triggered_thresholds:
                # Check if threshold isn't already marked
                if threshold not in self.triggered_thresholds[symbol][timeframe]:
                    self.triggered_thresholds[symbol][timeframe].append(threshold)
            
            # Place order
            order = await self.client.create_order(
                symbol=symbol,
                side='BUY',
                type='LIMIT',
                timeInForce='GTC',
                quantity=float(quantity),
                price=float(price)
            )
            
            return Order(
                symbol=symbol,
                status=OrderStatus.PENDING,
                price=price,
                quantity=quantity,
                threshold=threshold,
                timeframe=timeframe,
                order_id=str(order['orderId']),  # Convert to string
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                is_manual=is_manual,
                fees=fees,
                fee_asset=fee_asset
            )
            
        except BinanceAPIException as e:
            logger.error(f"Failed to place order: {e}")
            raise
            
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an order"""
        try:
            await self.client.cancel_order(symbol=symbol, orderId=order_id)
            return True
        except BinanceAPIException as e:
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
            
    async def get_balance(self, symbol: str = 'USDT') -> Decimal:
        """Get balance for a specific asset"""
        await self.rate_limiter.acquire()
        try:
            account = await self.client.get_account()
            for balance in account['balances']:
                if balance['asset'] == symbol:
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
