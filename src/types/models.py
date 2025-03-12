from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional, Dict, Any, List, Union

class TimeFrame(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"

class OrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"

class OrderType(str, Enum):
    SPOT = "spot"
    FUTURES = "futures"

class TradeDirection(Enum):
    LONG = "long"
    SHORT = "short"

class OrderDirection(str, Enum):
    LONG = "long"
    SHORT = "short"

class MarginMode(str, Enum):
    ISOLATED = "isolated"
    CROSS = "cross"

class PositionSide(str, Enum):
    BOTH = "BOTH"
    LONG = "LONG"
    SHORT = "SHORT"

@dataclass
class Order:
    def __init__(self, 
                 order_id: Optional[str],
                 symbol: str,
                 price: Decimal,
                 quantity: Decimal,
                 order_type: OrderType,
                 status: OrderStatus,
                 created_at: datetime,
                 filled_at: Optional[datetime] = None,
                 cancelled_at: Optional[datetime] = None,
                 threshold: Optional[float] = None,
                 timeframe: Optional[TimeFrame] = None,
                 reference_price: Optional[Decimal] = None,
                 is_manual: bool = False,
                 fee: Optional[Decimal] = None,
                 fee_currency: Optional[str] = None,
                 balance_change: Optional[Decimal] = None,
                 leverage: Optional[int] = None,
                 direction: Optional[OrderDirection] = None,
                 margin_mode: Optional[str] = "isolated",
                 position_side: Optional[str] = "BOTH"):
        self.order_id = order_id
        self.symbol = symbol
        self.price = price
        self.quantity = quantity
        self.order_type = order_type
        self.status = status
        self.created_at = created_at
        self.filled_at = filled_at
        self.cancelled_at = cancelled_at
        self.threshold = threshold
        self.timeframe = timeframe
        self.reference_price = reference_price
        self.is_manual = is_manual
        self.fee = fee
        self.fee_currency = fee_currency
        self.balance_change = balance_change
        self.leverage = leverage
        self.direction = direction
        self.margin_mode = margin_mode
        self.position_side = position_side
        
    def to_dict(self) -> Dict[str, Any]:
        """Convert order to dictionary for database storage"""
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "price": str(self.price),
            "quantity": str(self.quantity),
            "order_type": self.order_type.value,
            "status": self.status.value,
            "created_at": self.created_at,
            "filled_at": self.filled_at,
            "cancelled_at": self.cancelled_at,
            "threshold": self.threshold,
            "timeframe": self.timeframe.value if self.timeframe else None,
            "reference_price": self.reference_price,
            "is_manual": self.is_manual,
            "fee": str(self.fee) if self.fee is not None else None,
            "fee_currency": self.fee_currency,
            "balance_change": str(self.balance_change) if self.balance_change is not None else None,
            "leverage": self.leverage,
            "direction": self.direction.value if self.direction else None,
            "margin_mode": self.margin_mode,
            "position_side": self.position_side
        }
        
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Order':
        """Create order from dictionary"""
        return cls(
            order_id=data.get("order_id"),
            symbol=data.get("symbol"),
            price=Decimal(data.get("price")),
            quantity=Decimal(data.get("quantity")),
            order_type=OrderType(data.get("order_type")),
            status=OrderStatus(data.get("status")),
            created_at=data.get("created_at"),
            filled_at=data.get("filled_at"),
            cancelled_at=data.get("cancelled_at"),
            threshold=data.get("threshold"),
            timeframe=TimeFrame(data.get("timeframe")) if data.get("timeframe") else None,
            reference_price=Decimal(data.get("reference_price")) if data.get("reference_price") else None,
            is_manual=data.get("is_manual", False),
            fee=Decimal(data.get("fee")) if data.get("fee") else None,
            fee_currency=data.get("fee_currency"),
            balance_change=Decimal(data.get("balance_change")) if data.get("balance_change") else None,
            leverage=data.get("leverage"),
            direction=OrderDirection(data.get("direction")) if data.get("direction") else None,
            margin_mode=data.get("margin_mode"),
            position_side=data.get("position_side")
        )
        
    def get_value(self) -> Decimal:
        """Get total value of the order"""
        return self.price * self.quantity
        
    def get_margin_value(self) -> Decimal:
        """Get margin value for futures orders (considering leverage)"""
        if self.order_type == OrderType.FUTURES and self.leverage and self.leverage > 0:
            return self.get_value() / Decimal(str(self.leverage))
        return self.get_value()
        
    def is_futures(self) -> bool:
        """Check if this is a futures order"""
        return self.order_type == OrderType.FUTURES
        
    def is_long(self) -> bool:
        """Check if this is a long position"""
        return self.direction == OrderDirection.LONG if self.direction else True
        
    def is_short(self) -> bool:
        """Check if this is a short position"""
        return self.direction == OrderDirection.SHORT if self.direction else False
