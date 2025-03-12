import logging
import asyncio
from typing import Dict, List, Optional, Tuple
from decimal import Decimal
from datetime import datetime

from binance import AsyncClient
from binance.exceptions import BinanceAPIException

from ..types.models import Order, OrderStatus, OrderType, TimeFrame, OrderDirection
from ..types.constants import TRADING_FEES, ORDER_TYPE_FEES

logger = logging.getLogger(__name__)

class BinanceFuturesClient:
    """Client for interacting with Binance Futures API"""
    
    def __init__(self, client: AsyncClient):
        """Initialize with an existing AsyncClient instance"""
        self.client = client
        self.leverage_cache = {}  # Cache for symbol leverage settings
        
    async def get_account_info(self) -> Dict:
        """Get futures account information"""
        try:
            account_info = await self.client.futures_account()
            return account_info
        except BinanceAPIException as e:
            logger.error(f"Failed to get futures account info: {e}")
            return {}
            
    async def get_balance(self, asset: str = 'USDT') -> Decimal:
        """Get balance of specific asset in futures account"""
        try:
            account_info = await self.get_account_info()
            
            if not account_info:
                return Decimal('0')
                
            # Find the asset in the assets list
            for balance in account_info.get('assets', []):
                if balance.get('asset') == asset:
                    return Decimal(str(balance.get('walletBalance', '0')))
                    
            return Decimal('0')
        except Exception as e:
            logger.error(f"Error getting futures balance for {asset}: {e}")
            return Decimal('0')
            
    async def get_positions(self, symbol: Optional[str] = None) -> List[Dict]:
        """Get all open positions or for a specific symbol"""
        try:
            positions = await self.client.futures_position_information()
            
            # Filter positions with non-zero amounts
            active_positions = [
                pos for pos in positions 
                if float(pos.get('positionAmt', 0)) != 0
            ]
            
            # Filter by symbol if provided
            if symbol:
                active_positions = [
                    pos for pos in active_positions 
                    if pos.get('symbol') == symbol
                ]
                
            return active_positions
        except BinanceAPIException as e:
            logger.error(f"Failed to get futures positions: {e}")
            return []
            
    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for a specific symbol"""
        try:
            # Check if we already have this leverage set
            if symbol in self.leverage_cache and self.leverage_cache[symbol] == leverage:
                logger.debug(f"Leverage for {symbol} already set to {leverage}x")
                return True
                
            # Set leverage
            response = await self.client.futures_change_leverage(
                symbol=symbol,
                leverage=leverage
            )
            
            # Update cache
            if response and 'leverage' in response:
                self.leverage_cache[symbol] = int(response['leverage'])
                logger.info(f"Set leverage for {symbol} to {leverage}x")
                return True
                
            return False
        except BinanceAPIException as e:
            logger.error(f"Failed to set leverage for {symbol}: {e}")
            return False
            
    async def set_margin_type(self, symbol: str, margin_type: str = 'ISOLATED') -> bool:
        """Set margin type for a specific symbol (ISOLATED or CROSSED)"""
        try:
            # Validate margin type
            if margin_type not in ['ISOLATED', 'CROSSED']:
                logger.error(f"Invalid margin type: {margin_type}")
                return False
                
            # Set margin type
            await self.client.futures_change_margin_type(
                symbol=symbol,
                marginType=margin_type
            )
            
            logger.info(f"Set margin type for {symbol} to {margin_type}")
            return True
        except BinanceAPIException as e:
            # If already set, consider it a success
            if e.code == -4046:  # "No need to change margin type."
                logger.debug(f"Margin type for {symbol} already set to {margin_type}")
                return True
                
            logger.error(f"Failed to set margin type for {symbol}: {e}")
            return False
            
    async def open_position(self, 
                          symbol: str, 
                          quantity: Decimal, 
                          price: Decimal, 
                          direction: OrderDirection,
                          leverage: int = 1,
                          order_type: OrderType = OrderType.LIMIT) -> Optional[Order]:
        """Open a futures position"""
        try:
            # Set leverage and margin type first
            leverage_set = await self.set_leverage(symbol, leverage)
            margin_set = await self.set_margin_type(symbol, 'ISOLATED')
            
            if not leverage_set or not margin_set:
                logger.error(f"Failed to set leverage or margin type for {symbol}")
                return None
                
            # Determine side based on direction
            side = 'BUY' if direction == OrderDirection.LONG else 'SELL'
            
            # Create order parameters
            params = {
                'symbol': symbol,
                'side': side,
                'quantity': float(quantity),
                'reduceOnly': False,
                'newOrderRespType': 'RESULT'
            }
            
            # Add price for limit orders
            if order_type == OrderType.LIMIT:
                params['type'] = 'LIMIT'
                params['price'] = float(price)
                params['timeInForce'] = 'GTC'  # Good Till Cancelled
            else:
                params['type'] = 'MARKET'
            
            # Place the order
            response = await self.client.futures_create_order(**params)
            
            if not response or 'orderId' not in response:
                logger.error(f"Failed to create futures order: {response}")
                return None
                
            # Create Order object
            order = Order(
                order_id=str(response['orderId']),
                symbol=symbol,
                price=Decimal(str(response.get('price', price))),
                quantity=Decimal(str(response['origQty'])),
                order_type=OrderType.FUTURES,
                status=OrderStatus.PENDING,
                created_at=datetime.utcnow(),
                direction=direction,
                leverage=leverage,
                timeframe=TimeFrame.DAILY  # Default timeframe
            )
            
            logger.info(f"Created futures {side} order for {symbol} at {price} with {leverage}x leverage")
            return order
            
        except BinanceAPIException as e:
            logger.error(f"Failed to open futures position for {symbol}: {e}")
            return None
            
    async def close_position(self, 
                           symbol: str, 
                           quantity: Optional[Decimal] = None,
                           price: Optional[Decimal] = None) -> bool:
        """Close a futures position"""
        try:
            # Get current position
            positions = await self.get_positions(symbol)
            
            if not positions:
                logger.warning(f"No open position found for {symbol}")
                return False
                
            position = positions[0]
            position_amt = Decimal(str(position.get('positionAmt', '0')))
            
            # If no quantity specified, close entire position
            if quantity is None:
                quantity = abs(position_amt)
                
            # Determine side (opposite of current position)
            side = 'SELL' if float(position_amt) > 0 else 'BUY'
            
            # Create order parameters
            params = {
                'symbol': symbol,
                'side': side,
                'quantity': float(quantity),
                'reduceOnly': True,
                'newOrderRespType': 'RESULT'
            }
            
            # Add price for limit orders
            if price is not None:
                params['type'] = 'LIMIT'
                params['price'] = float(price)
                params['timeInForce'] = 'GTC'
            else:
                params['type'] = 'MARKET'
            
            # Place the order
            response = await self.client.futures_create_order(**params)
            
            if not response or 'orderId' not in response:
                logger.error(f"Failed to close futures position: {response}")
                return False
                
            logger.info(f"Closed futures position for {symbol} with {side} order")
            return True
            
        except BinanceAPIException as e:
            logger.error(f"Failed to close futures position for {symbol}: {e}")
            return False
            
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel a futures order"""
        try:
            response = await self.client.futures_cancel_order(
                symbol=symbol,
                orderId=order_id
            )
            
            if response and 'orderId' in response:
                logger.info(f"Cancelled futures order {order_id} for {symbol}")
                return True
                
            return False
        except BinanceAPIException as e:
            logger.error(f"Failed to cancel futures order {order_id}: {e}")
            return False
            
    async def get_order_status(self, symbol: str, order_id: str) -> Optional[OrderStatus]:
        """Get status of a futures order"""
        try:
            order = await self.client.futures_get_order(
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
            logger.error(f"Failed to get futures order status for {order_id}: {e}")
            return None
            
    async def get_mark_price(self, symbol: str) -> Optional[Decimal]:
        """Get current mark price for a futures symbol"""
        try:
            response = await self.client.futures_mark_price(symbol=symbol)
            
            if isinstance(response, list):
                response = response[0]
                
            if response and 'markPrice' in response:
                return Decimal(str(response['markPrice']))
                
            return None
        except BinanceAPIException as e:
            logger.error(f"Failed to get mark price for {symbol}: {e}")
            return None
            
    async def calculate_fees(self, symbol: str, price: Decimal, quantity: Decimal, leverage: int = 1) -> Tuple[Decimal, str]:
        """Calculate fees for a futures trade"""
        try:
            # Get fee rate from constants
            fee_rate = Decimal(str(ORDER_TYPE_FEES.get('futures', TRADING_FEES.get('FUTURES', 0.002))))
            
            # Calculate notional value
            notional_value = price * quantity
            
            # Calculate fee (fee is on the notional value, not the margin)
            fee = notional_value * fee_rate
            
            # Return fee and currency
            return fee, 'USDT'
        except Exception as e:
            logger.error(f"Error calculating futures fees: {e}")
            return Decimal('0'), 'USDT'
            
    async def get_funding_rate(self, symbol: str) -> Optional[Decimal]:
        """Get current funding rate for a futures symbol"""
        try:
            response = await self.client.futures_funding_rate(symbol=symbol, limit=1)
            
            if response and len(response) > 0 and 'fundingRate' in response[0]:
                return Decimal(str(response[0]['fundingRate']))
                
            return None
        except BinanceAPIException as e:
            logger.error(f"Failed to get funding rate for {symbol}: {e}")
            return None 