import logging
from decimal import Decimal
from datetime import datetime
from typing import Dict, List, Optional

import hyperliquid

from ..types.models import (
    Order, OrderStatus, TimeFrame, OrderType
)
from ..utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

class HyperliquidClient:
    """Minimal Hyperliquid exchange client supporting spot trading."""

    def __init__(self, api_key: str, api_secret: str, telegram_bot=None,
                 mongo_client=None, config=None, testnet: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.telegram_bot = telegram_bot
        self.mongo_client = mongo_client
        self.config = config or {}
        self.client = None

        self.base_currency = self.config.get('trading', {}).get('base_currency', 'USDC')
        self.reserve_balance = float(self.config.get('trading', {}).get('reserve_balance', 0))
        self.thresholds = self.config.get('trading', {}).get('thresholds', {
            'daily': [1, 2, 5],
            'weekly': [5, 10, 15],
            'monthly': [10, 20, 30]
        })

        self.reference_prices: Dict[str, Dict[TimeFrame, Decimal]] = {}
        self.reference_timestamps: Dict[TimeFrame, Optional[datetime]] = {
            TimeFrame.DAILY: None,
            TimeFrame.WEEKLY: None,
            TimeFrame.MONTHLY: None
        }
        self.triggered_thresholds: Dict[str, Dict[str, List[float]]] = {}

        self.rate_limiter = RateLimiter()
        self.invalid_symbols = set()
        self.removed_symbols_this_cycle = set()
        self.performance_reporter = None

    async def initialize(self):
        hostname = 'hyperliquid-testnet.xyz' if self.testnet else 'hyperliquid.xyz'
        self.client = hyperliquid.HyperliquidAsync({
            'apiKey': self.api_key,
            'secret': self.api_secret,
            'hostname': hostname,
        })
        await self.client.load_markets()
        env = 'TESTNET' if self.testnet else 'MAINNET'
        logger.info(f"[INIT] Using Hyperliquid {env} API")

    async def close(self):
        if self.client:
            await self.client.close()

    async def get_balance(self, symbol: str = None) -> Decimal:
        symbol = symbol or self.base_currency
        try:
            await self.rate_limiter.acquire()
            balance = await self.client.fetch_balance()
            free = balance.get(symbol, {}).get('free', 0)
            total = balance.get(symbol, {}).get('total', free)
            return Decimal(str(total))
        except Exception as e:
            logger.error(f"Failed to get balance for {symbol}: {e}")
            return Decimal('0')

    async def get_balance_changes(self, symbol: str = None) -> Optional[Decimal]:
        return None

    async def get_current_price(self, symbol: str) -> Optional[Decimal]:
        try:
            await self.rate_limiter.acquire()
            ticker = await self.client.fetch_ticker(symbol)
            return Decimal(str(ticker['last']))
        except Exception as e:
            logger.error(f"Error getting current price for {symbol}: {e}")
            return None

    async def check_reserve_balance(self, order_amount: float) -> bool:
        balance = await self.get_balance(self.base_currency)
        required = Decimal(str(order_amount)) + Decimal(str(self.reserve_balance))
        return balance >= required

    async def place_limit_buy_order(self, symbol: str, amount: float,
                                   threshold: Optional[float] = None,
                                   timeframe: Optional[TimeFrame] = None,
                                   is_manual: bool = False) -> Optional[Order]:
        price = await self.get_current_price(symbol)
        if price is None:
            return None
        quantity = Decimal(str(amount)) / price
        try:
            await self.rate_limiter.acquire()
            order = await self.client.create_order(symbol, 'limit', 'buy', float(quantity), float(price))
            order_id = order.get('id', '')
            created_at = datetime.utcnow()
            return Order(
                symbol=symbol,
                status=OrderStatus.PENDING,
                order_type=OrderType.LIMIT,
                price=price,
                quantity=quantity,
                timeframe=timeframe,
                order_id=str(order_id),
                created_at=created_at,
                updated_at=created_at,
                threshold=threshold,
                is_manual=is_manual
            )
        except Exception as e:
            logger.error(f"Error placing order for {symbol}: {e}")
            return None

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            await self.rate_limiter.acquire()
            await self.client.cancel_order(order_id, symbol)
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    async def check_order_status(self, symbol: str, order_id: str) -> Optional[OrderStatus]:
        try:
            await self.rate_limiter.acquire()
            info = await self.client.fetch_order(order_id, symbol)
            status = info.get('status')
            if status == 'closed':
                return OrderStatus.FILLED
            if status == 'canceled':
                return OrderStatus.CANCELLED
            return OrderStatus.PENDING
        except Exception as e:
            logger.error(f"Error checking order status {order_id}: {e}")
            return None

    async def get_reference_price(self, symbol: str, timeframe: TimeFrame) -> Optional[Decimal]:
        if symbol in self.reference_prices and timeframe in self.reference_prices[symbol]:
            return self.reference_prices[symbol][timeframe]
        if self.mongo_client:
            price = await self.mongo_client.get_reference_price(symbol, timeframe.value)
            if price:
                if symbol not in self.reference_prices:
                    self.reference_prices[symbol] = {}
                self.reference_prices[symbol][timeframe] = Decimal(str(price))
                return self.reference_prices[symbol][timeframe]
        return None

    async def mark_threshold_triggered(self, symbol: str, timeframe: TimeFrame, threshold: float):
        self.triggered_thresholds.setdefault(symbol, {}).setdefault(timeframe.value, []).append(threshold)
        if self.mongo_client:
            await self.mongo_client.save_triggered_threshold(symbol, timeframe.value,
                                                             self.triggered_thresholds[symbol][timeframe.value])

    async def check_thresholds(self, symbol: str, timeframe: TimeFrame) -> List[float]:
        ref_price = await self.get_reference_price(symbol, timeframe)
        if ref_price is None:
            return []
        current_price = await self.get_current_price(symbol)
        if current_price is None:
            return []
        price_change = (current_price - ref_price) / ref_price * Decimal('100')
        triggered = []
        for th in self.thresholds.get(timeframe.value, []):
            if price_change <= -Decimal(str(th)):
                already = th in self.triggered_thresholds.get(symbol, {}).get(timeframe.value, [])
                if not already:
                    await self.mark_threshold_triggered(symbol, timeframe, th)
                    triggered.append(th)
        return triggered

    async def check_timeframe_reset(self, timeframe: TimeFrame) -> bool:
        now = datetime.utcnow()
        reset = False
        if timeframe == TimeFrame.DAILY:
            reset = now.hour == 0 and now.minute < 15
        elif timeframe == TimeFrame.WEEKLY:
            reset = now.weekday() == 0 and now.hour == 0 and now.minute < 15
        elif timeframe == TimeFrame.MONTHLY:
            reset = now.day == 1 and now.hour == 0 and now.minute < 15
        if reset:
            self.reference_timestamps[timeframe] = now
            for symbol in self.config.get('trading', {}).get('pairs', []):
                price = await self.get_current_price(symbol)
                if price is None:
                    continue
                self.reference_prices.setdefault(symbol, {})[timeframe] = price
                if self.mongo_client:
                    await self.mongo_client.save_reference_price(symbol, timeframe.value, float(price))
        return reset

    async def create_tp_sl_orders(self, order: Order) -> tuple:
        return False, False, [], False

    async def check_tp_sl_triggers(self, order: Order) -> Dict[str, bool]:
        return {'tp_triggered': False, 'sl_triggered': False,
                'partial_tp_triggered': [], 'trailing_sl_updated': False}

