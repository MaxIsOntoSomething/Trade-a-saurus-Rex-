from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional, List, Dict

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

class TPSLStatus(Enum):
    PENDING = "pending"
    TRIGGERED = "triggered"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

@dataclass
class PartialTakeProfit:
    level: int  # 1, 2, or 3
    price: Decimal
    profit_percentage: float  # The profit percentage target for this level
    position_percentage: float  # The percentage of the position to sell at this level
    status: TPSLStatus = TPSLStatus.PENDING
    triggered_at: Optional[datetime] = None
    order_id: Optional[str] = None

@dataclass
class TakeProfit:
    price: Decimal
    percentage: float
    status: TPSLStatus = TPSLStatus.PENDING
    triggered_at: Optional[datetime] = None
    order_id: Optional[str] = None

@dataclass
class StopLoss:
    price: Decimal
    percentage: float
    status: TPSLStatus = TPSLStatus.PENDING
    triggered_at: Optional[datetime] = None
    order_id: Optional[str] = None

@dataclass
class TrailingStopLoss:
    activation_percentage: float  # Profit % to activate trailing
    callback_rate: float  # How much to trail behind peak price (%)
    initial_price: Decimal  # Entry price for the order
    activation_price: Decimal  # Price at which trailing becomes active
    current_stop_price: Decimal  # Current stop loss price (updates as price rises)
    highest_price: Decimal  # Tracks the highest price seen once activated
    status: TPSLStatus = TPSLStatus.PENDING
    triggered_at: Optional[datetime] = None
    order_id: Optional[str] = None
    activated_at: Optional[datetime] = None  # When trailing became active

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
    take_profit: Optional[TakeProfit] = None
    stop_loss: Optional[StopLoss] = None
    partial_take_profits: List[PartialTakeProfit] = field(default_factory=list)  # List to store partial take profits
    trailing_stop_loss: Optional[TrailingStopLoss] = None  # Trailing stop loss
