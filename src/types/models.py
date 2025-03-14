from enum import Enum
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

class TimeFrame(Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"

class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"

class OrderType(Enum):
    SPOT = "spot"
    FUTURES = "futures"

class TradeDirection(Enum):
    LONG = "long"
    SHORT = "short"

@dataclass
class Order:
    symbol: str
    status: OrderStatus
    order_type: OrderType
    price: Decimal
    quantity: Decimal
    timeframe: TimeFrame
    order_id: str
    created_at: datetime
    updated_at: datetime
    leverage: Optional[int] = None
    direction: Optional[TradeDirection] = None
    filled_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    fees: Decimal = Decimal('0')
    fee_asset: str = None  # Remove default USDT to make this dynamic
    threshold: Optional[float] = None
    is_manual: bool = False
