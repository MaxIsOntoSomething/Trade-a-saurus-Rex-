from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from ..types.models import TimeFrame, Order
from decimal import Decimal

class BaseClient(ABC):
    """Base client interface that both spot and futures clients must implement"""
    
    @abstractmethod
    async def initialize(self):
        """Initialize the client"""
        pass
        
    @abstractmethod
    async def update_reference_prices(self, symbols: List[str]):
        """Update reference prices for given symbols"""
        pass
        
    @abstractmethod
    async def check_thresholds(self, symbol: str, thresholds: Dict[str, List[float]]):
        """Check price thresholds"""
        pass
        
    @abstractmethod
    async def check_timeframe_reset(self, timeframe: TimeFrame) -> bool:
        """Check if timeframe needs reset"""
        pass
        
    @abstractmethod
    async def get_balance(self, symbol: str = 'USDT') -> Decimal:
        """Get balance for symbol"""
        pass
        
    @abstractmethod
    async def cleanup(self):
        """Cleanup client resources"""
        pass

    @abstractmethod
    async def get_candles_for_chart(self, symbol: str, timeframe: TimeFrame, count: int = 15) -> List[Dict]:
        """Get candles for chart generation"""
        pass

    @abstractmethod
    async def generate_trade_chart(self, order: Order) -> Optional[bytes]:
        """Generate chart for trade"""
        pass
