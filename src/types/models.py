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
    SPOT = 'spot'
    FUTURES = 'futures'

class TradeDirection(Enum):
    LONG = 'long'
    SHORT = 'short'

class MarginType(Enum):
    ISOLATED = 'ISOLATED'
    CROSSED = 'CROSSED'

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
    fee_asset: str = 'USDT'
    threshold: Optional[float] = None
    is_manual: bool = False
    margin_type: Optional[MarginType] = None
    metadata: dict = None
    balance_change: Optional[Decimal] = None
    realized_pnl: Optional[Decimal] = None
    unrealized_pnl: Optional[Decimal] = None
    # Add TP/SL fields
    tp_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None
    tp_price: Optional[float] = None
    sl_price: Optional[float] = None
    position_side: Optional[str] = None  # Add position side field

    def __post_init__(self):
        # Convert numeric strings to Decimal
        if isinstance(self.price, (str, float)):
            self.price = Decimal(str(self.price))
        if isinstance(self.quantity, (str, float)):
            self.quantity = Decimal(str(self.quantity))
        if isinstance(self.fees, (str, float)):
            self.fees = Decimal(str(self.fees))
        
        # Initialize metadata if None
        if self.metadata is None:
            self.metadata = {
                'inserted_at': datetime.utcnow(),
                'last_checked': datetime.utcnow(),
                'check_count': 0,
                'error_count': 0
            }

    @property
    def total_value(self) -> Decimal:
        """Calculate total value in quote currency"""
        return self.price * self.quantity

    @property
    def is_futures(self) -> bool:
        """Check if this is a futures order"""
        return self.order_type == OrderType.FUTURES

    @property
    def age(self) -> float:
        """Get order age in hours"""
        return (datetime.utcnow() - self.created_at).total_seconds() / 3600
