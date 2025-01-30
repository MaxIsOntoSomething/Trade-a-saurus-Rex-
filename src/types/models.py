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

@dataclass
class Order:
    symbol: str
    status: OrderStatus
    price: Decimal
    quantity: Decimal
    threshold: float
    timeframe: TimeFrame
    order_id: str
    created_at: datetime
    updated_at: datetime
    filled_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    is_manual: bool = False  # Add this field with default False
    fees: Decimal = Decimal('0')  # Add fees field
    fee_asset: str = 'USDT'  # Add fee asset field
