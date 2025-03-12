import logging
import asyncio
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Optional, Tuple, Union
from datetime import datetime

from ..types.models import Order, OrderStatus, OrderType, OrderDirection, TimeFrame
from ..trading.binance_client import BinanceClient
from ..database.mongo_client import MongoClient

logger = logging.getLogger(__name__)

class TPSLManager:
    """Manager for Take Profit and Stop Loss orders"""
    
    def __init__(self, binance_client: BinanceClient, mongo_client: MongoClient):
        self.binance_client = binance_client
        self.mongo_client = mongo_client
        self.tp_sl_orders = {}  # Dictionary to track TP/SL orders for each main order
        
    async def calculate_tp_sl_levels(self, 
                                   order: Order, 
                                   tp_percentage: float, 
                                   sl_percentage: float) -> Dict[str, Decimal]:
        """
        Calculate Take Profit and Stop Loss price levels based on entry price and percentages
        
        Args:
            order: The main order
            tp_percentage: Take profit percentage (e.g., 5.0 for 5%)
            sl_percentage: Stop loss percentage (e.g., 3.0 for 3%)
            
        Returns:
            Dictionary with 'tp_price' and 'sl_price'
        """
        entry_price = order.price
        is_futures = order.order_type == OrderType.FUTURES
        is_long = order.direction == OrderDirection.LONG if order.direction else True
        
        # For futures, consider position direction
        if is_futures:
            if is_long:
                # Long position: TP above entry, SL below entry
                tp_price = entry_price * (1 + Decimal(str(tp_percentage)) / 100)
                sl_price = entry_price * (1 - Decimal(str(sl_percentage)) / 100)
            else:
                # Short position: TP below entry, SL above entry
                tp_price = entry_price * (1 - Decimal(str(tp_percentage)) / 100)
                sl_price = entry_price * (1 + Decimal(str(sl_percentage)) / 100)
        else:
            # Spot is always long
            tp_price = entry_price * (1 + Decimal(str(tp_percentage)) / 100)
            sl_price = entry_price * (1 - Decimal(str(sl_percentage)) / 100)
        
        # Adjust prices to correct precision
        tp_price = self._adjust_price_precision(order.symbol, tp_price)
        sl_price = self._adjust_price_precision(order.symbol, sl_price)
        
        return {
            'tp_price': tp_price,
            'sl_price': sl_price
        }
    
    def _adjust_price_precision(self, symbol: str, price: Decimal) -> Decimal:
        """Adjust price to the correct precision for the symbol"""
        try:
            # Get price precision from binance client
            precision = self.binance_client._get_price_precision(symbol)
            
            # Adjust to precision
            adjusted_price = price.quantize(
                Decimal('0.' + '0' * precision),
                rounding=ROUND_DOWN
            )
            
            return adjusted_price
        except Exception as e:
            logger.error(f"Error adjusting price precision for {symbol}: {e}")
            return price
    
    async def place_tp_sl_orders(self, 
                               main_order: Order, 
                               tp_percentage: float, 
                               sl_percentage: float) -> Dict[str, Optional[Order]]:
        """
        Place Take Profit and Stop Loss orders for a main order
        
        Args:
            main_order: The main order
            tp_percentage: Take profit percentage
            sl_percentage: Stop loss percentage
            
        Returns:
            Dictionary with 'tp_order' and 'sl_order'
        """
        try:
            # Calculate TP/SL levels
            levels = await self.calculate_tp_sl_levels(main_order, tp_percentage, sl_percentage)
            tp_price = levels['tp_price']
            sl_price = levels['sl_price']
            
            # Log the calculated levels
            logger.info(
                f"Calculated TP/SL levels for {main_order.symbol}:\n"
                f"Entry: ${float(main_order.price):.2f}\n"
                f"TP ({tp_percentage}%): ${float(tp_price):.2f}\n"
                f"SL ({sl_percentage}%): ${float(sl_price):.2f}"
            )
            
            # Initialize result
            result = {
                'tp_order': None,
                'sl_order': None
            }
            
            # Check if main order is filled
            if main_order.status != OrderStatus.FILLED:
                logger.warning(f"Cannot place TP/SL orders for unfilled order {main_order.order_id}")
                return result
            
            # Place orders based on order type
            if main_order.order_type == OrderType.FUTURES:
                result = await self._place_futures_tp_sl(main_order, tp_price, sl_price)
            else:
                result = await self._place_spot_tp_sl(main_order, tp_price, sl_price)
            
            # Store TP/SL orders in database
            if result['tp_order']:
                await self.mongo_client.insert_order(result['tp_order'])
                
            if result['sl_order']:
                await self.mongo_client.insert_order(result['sl_order'])
            
            # Track TP/SL orders for the main order
            self.tp_sl_orders[main_order.order_id] = {
                'tp_order_id': result['tp_order'].order_id if result['tp_order'] else None,
                'sl_order_id': result['sl_order'].order_id if result['sl_order'] else None
            }
            
            # Update main order with TP/SL info
            await self.mongo_client.orders.update_one(
                {"order_id": main_order.order_id},
                {"$set": {
                    "tp_order_id": result['tp_order'].order_id if result['tp_order'] else None,
                    "sl_order_id": result['sl_order'].order_id if result['sl_order'] else None,
                    "tp_price": str(tp_price),
                    "sl_price": str(sl_price)
                }}
            )
            
            return result
            
        except Exception as e:
            logger.error(f"Error placing TP/SL orders: {e}")
            return {'tp_order': None, 'sl_order': None}
    
    async def _place_futures_tp_sl(self, 
                                 main_order: Order, 
                                 tp_price: Decimal, 
                                 sl_price: Decimal) -> Dict[str, Optional[Order]]:
        """Place TP/SL orders for futures position"""
        try:
            symbol = main_order.symbol
            quantity = main_order.quantity
            is_long = main_order.direction == OrderDirection.LONG if main_order.direction else True
            leverage = main_order.leverage or 1
            
            # Determine sides for TP and SL
            tp_side = 'SELL' if is_long else 'BUY'
            sl_side = 'SELL' if is_long else 'BUY'
            
            # Place Take Profit order
            tp_params = {
                'symbol': symbol,
                'side': tp_side,
                'type': 'LIMIT',
                'timeInForce': 'GTC',
                'quantity': float(quantity),
                'price': float(tp_price),
                'reduceOnly': True,
                'newOrderRespType': 'RESULT'
            }
            
            # Place Stop Loss order
            sl_params = {
                'symbol': symbol,
                'side': sl_side,
                'type': 'STOP_MARKET',
                'stopPrice': float(sl_price),
                'quantity': float(quantity),
                'reduceOnly': True,
                'newOrderRespType': 'RESULT'
            }
            
            # Execute orders
            tp_response = await self.binance_client.client.futures_create_order(**tp_params)
            sl_response = await self.binance_client.client.futures_create_order(**sl_params)
            
            # Create Order objects
            tp_order = None
            sl_order = None
            
            if tp_response and 'orderId' in tp_response:
                tp_order = Order(
                    order_id=str(tp_response['orderId']),
                    symbol=symbol,
                    price=tp_price,
                    quantity=quantity,
                    order_type=OrderType.FUTURES,
                    status=OrderStatus.PENDING,
                    created_at=datetime.utcnow(),
                    direction=OrderDirection.SHORT if is_long else OrderDirection.LONG,
                    leverage=leverage,
                    timeframe=main_order.timeframe,
                    is_manual=False,
                    reference_price=float(main_order.price)
                )
                
            if sl_response and 'orderId' in sl_response:
                sl_order = Order(
                    order_id=str(sl_response['orderId']),
                    symbol=symbol,
                    price=sl_price,
                    quantity=quantity,
                    order_type=OrderType.FUTURES,
                    status=OrderStatus.PENDING,
                    created_at=datetime.utcnow(),
                    direction=OrderDirection.SHORT if is_long else OrderDirection.LONG,
                    leverage=leverage,
                    timeframe=main_order.timeframe,
                    is_manual=False,
                    reference_price=float(main_order.price)
                )
            
            return {
                'tp_order': tp_order,
                'sl_order': sl_order
            }
            
        except Exception as e:
            logger.error(f"Error placing futures TP/SL orders: {e}")
            return {'tp_order': None, 'sl_order': None}
    
    async def _place_spot_tp_sl(self, 
                              main_order: Order, 
                              tp_price: Decimal, 
                              sl_price: Decimal) -> Dict[str, Optional[Order]]:
        """Place TP/SL orders for spot position"""
        try:
            symbol = main_order.symbol
            quantity = main_order.quantity
            
            # For spot, we need to place sell orders
            # Place Take Profit order
            tp_params = {
                'symbol': symbol,
                'side': 'SELL',
                'type': 'LIMIT',
                'timeInForce': 'GTC',
                'quantity': float(quantity),
                'price': float(tp_price),
                'newOrderRespType': 'FULL'
            }
            
            # Place Stop Loss order (OCO order for spot)
            oco_params = {
                'symbol': symbol,
                'side': 'SELL',
                'quantity': float(quantity),
                'price': float(tp_price),  # Limit price (same as TP)
                'stopPrice': float(sl_price),  # Trigger price
                'stopLimitPrice': float(sl_price * Decimal('0.99')),  # Slightly lower to ensure execution
                'stopLimitTimeInForce': 'GTC'
            }
            
            # Execute orders
            tp_response = await self.binance_client.client.create_order(**tp_params)
            
            # Create Order objects
            tp_order = None
            sl_order = None
            
            if tp_response and 'orderId' in tp_response:
                tp_order = Order(
                    order_id=str(tp_response['orderId']),
                    symbol=symbol,
                    price=tp_price,
                    quantity=quantity,
                    order_type=OrderType.SPOT,
                    status=OrderStatus.PENDING,
                    created_at=datetime.utcnow(),
                    timeframe=main_order.timeframe,
                    is_manual=False,
                    reference_price=float(main_order.price)
                )
            
            # For spot, we use OCO orders which combine TP and SL
            # This is a simplification - in a real implementation, you might want to
            # use OCO orders which combine both TP and SL in one order
            
            # Simulate SL order for tracking purposes
            sl_order = Order(
                order_id=f"sl_{main_order.order_id}",  # Placeholder ID
                symbol=symbol,
                price=sl_price,
                quantity=quantity,
                order_type=OrderType.SPOT,
                status=OrderStatus.PENDING,
                created_at=datetime.utcnow(),
                timeframe=main_order.timeframe,
                is_manual=False,
                reference_price=float(main_order.price)
            )
            
            return {
                'tp_order': tp_order,
                'sl_order': sl_order
            }
            
        except Exception as e:
            logger.error(f"Error placing spot TP/SL orders: {e}")
            return {'tp_order': None, 'sl_order': None}
    
    async def cancel_tp_sl_orders(self, main_order_id: str) -> bool:
        """Cancel TP/SL orders for a main order"""
        try:
            # Get TP/SL order IDs
            if main_order_id not in self.tp_sl_orders:
                # Try to get from database
                main_order_doc = await self.mongo_client.orders.find_one({"order_id": main_order_id})
                if not main_order_doc:
                    logger.warning(f"No TP/SL orders found for {main_order_id}")
                    return False
                
                tp_order_id = main_order_doc.get("tp_order_id")
                sl_order_id = main_order_doc.get("sl_order_id")
            else:
                tp_order_id = self.tp_sl_orders[main_order_id]['tp_order_id']
                sl_order_id = self.tp_sl_orders[main_order_id]['sl_order_id']
            
            # Get main order
            main_order_doc = await self.mongo_client.orders.find_one({"order_id": main_order_id})
            if not main_order_doc:
                logger.error(f"Main order {main_order_id} not found")
                return False
            
            main_order = self.mongo_client._document_to_order(main_order_doc)
            if not main_order:
                logger.error(f"Failed to convert main order {main_order_id}")
                return False
            
            symbol = main_order.symbol
            is_futures = main_order.order_type == OrderType.FUTURES
            
            # Cancel TP order
            tp_cancelled = True
            if tp_order_id:
                if is_futures:
                    tp_cancelled = await self.binance_client.futures_client.cancel_order(symbol, tp_order_id)
                else:
                    tp_cancelled = await self.binance_client.cancel_order(symbol, tp_order_id)
                
                if tp_cancelled:
                    await self.mongo_client.update_order_status(
                        tp_order_id, 
                        OrderStatus.CANCELLED,
                        cancelled_at=datetime.utcnow()
                    )
            
            # Cancel SL order
            sl_cancelled = True
            if sl_order_id:
                if is_futures:
                    sl_cancelled = await self.binance_client.futures_client.cancel_order(symbol, sl_order_id)
                else:
                    sl_cancelled = await self.binance_client.cancel_order(symbol, sl_order_id)
                
                if sl_cancelled:
                    await self.mongo_client.update_order_status(
                        sl_order_id, 
                        OrderStatus.CANCELLED,
                        cancelled_at=datetime.utcnow()
                    )
            
            # Update main order
            if tp_cancelled and sl_cancelled:
                await self.mongo_client.orders.update_one(
                    {"order_id": main_order_id},
                    {"$unset": {
                        "tp_order_id": "",
                        "sl_order_id": "",
                        "tp_price": "",
                        "sl_price": ""
                    }}
                )
                
                # Remove from tracking
                if main_order_id in self.tp_sl_orders:
                    del self.tp_sl_orders[main_order_id]
                
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error cancelling TP/SL orders: {e}")
            return False
    
    async def update_tp_sl_levels(self, 
                                main_order_id: str, 
                                new_tp_percentage: Optional[float] = None, 
                                new_sl_percentage: Optional[float] = None) -> bool:
        """
        Update TP/SL levels for an existing order
        
        Args:
            main_order_id: ID of the main order
            new_tp_percentage: New take profit percentage (None to keep current)
            new_sl_percentage: New stop loss percentage (None to keep current)
            
        Returns:
            Success status
        """
        try:
            # Get main order
            main_order_doc = await self.mongo_client.orders.find_one({"order_id": main_order_id})
            if not main_order_doc:
                logger.error(f"Main order {main_order_id} not found")
                return False
            
            main_order = self.mongo_client._document_to_order(main_order_doc)
            if not main_order:
                logger.error(f"Failed to convert main order {main_order_id}")
                return False
            
            # Get current TP/SL percentages
            entry_price = main_order.price
            current_tp_price = Decimal(str(main_order_doc.get("tp_price", "0")))
            current_sl_price = Decimal(str(main_order_doc.get("sl_price", "0")))
            
            is_futures = main_order.order_type == OrderType.FUTURES
            is_long = main_order.direction == OrderDirection.LONG if main_order.direction else True
            
            # Calculate current percentages
            if is_futures:
                if is_long:
                    current_tp_percentage = ((current_tp_price / entry_price) - 1) * 100
                    current_sl_percentage = (1 - (current_sl_price / entry_price)) * 100
                else:
                    current_tp_percentage = (1 - (current_tp_price / entry_price)) * 100
                    current_sl_percentage = ((current_sl_price / entry_price) - 1) * 100
            else:
                # Spot is always long
                current_tp_percentage = ((current_tp_price / entry_price) - 1) * 100
                current_sl_percentage = (1 - (current_sl_price / entry_price)) * 100
            
            # Use new percentages or keep current
            tp_percentage = new_tp_percentage if new_tp_percentage is not None else float(current_tp_percentage)
            sl_percentage = new_sl_percentage if new_sl_percentage is not None else float(current_sl_percentage)
            
            # Cancel existing TP/SL orders
            cancelled = await self.cancel_tp_sl_orders(main_order_id)
            if not cancelled:
                logger.warning(f"Failed to cancel existing TP/SL orders for {main_order_id}")
            
            # Place new TP/SL orders
            result = await self.place_tp_sl_orders(main_order, tp_percentage, sl_percentage)
            
            return result['tp_order'] is not None and result['sl_order'] is not None
            
        except Exception as e:
            logger.error(f"Error updating TP/SL levels: {e}")
            return False
    
    async def monitor_tp_sl_orders(self):
        """Monitor TP/SL orders and handle filled orders"""
        try:
            # Get all main orders with TP/SL
            main_orders = await self.mongo_client.orders.find({
                "status": OrderStatus.FILLED.value,
                "$or": [
                    {"tp_order_id": {"$exists": True}},
                    {"sl_order_id": {"$exists": True}}
                ]
            }).to_list(None)
            
            for main_order_doc in main_orders:
                main_order_id = main_order_doc.get("order_id")
                tp_order_id = main_order_doc.get("tp_order_id")
                sl_order_id = main_order_doc.get("sl_order_id")
                
                if not tp_order_id and not sl_order_id:
                    continue
                
                symbol = main_order_doc.get("symbol")
                is_futures = main_order_doc.get("order_type") == OrderType.FUTURES.value
                
                # Check TP order status
                if tp_order_id:
                    if is_futures:
                        tp_status = await self.binance_client.futures_client.get_order_status(symbol, tp_order_id)
                    else:
                        tp_status = await self.binance_client.check_order_status(symbol, tp_order_id)
                    
                    if tp_status == OrderStatus.FILLED:
                        # TP order filled, cancel SL order
                        logger.info(f"TP order {tp_order_id} filled for {main_order_id}")
                        
                        # Update TP order status
                        await self.mongo_client.update_order_status(
                            tp_order_id,
                            OrderStatus.FILLED,
                            filled_at=datetime.utcnow()
                        )
                        
                        # Cancel SL order
                        if sl_order_id:
                            if is_futures:
                                await self.binance_client.futures_client.cancel_order(symbol, sl_order_id)
                            else:
                                await self.binance_client.cancel_order(symbol, sl_order_id)
                            
                            # Update SL order status
                            await self.mongo_client.update_order_status(
                                sl_order_id,
                                OrderStatus.CANCELLED,
                                cancelled_at=datetime.utcnow()
                            )
                        
                        # Update main order
                        await self.mongo_client.orders.update_one(
                            {"order_id": main_order_id},
                            {"$set": {
                                "tp_filled": True,
                                "tp_filled_at": datetime.utcnow()
                            }}
                        )
                
                # Check SL order status
                if sl_order_id:
                    if is_futures:
                        sl_status = await self.binance_client.futures_client.get_order_status(symbol, sl_order_id)
                    else:
                        sl_status = await self.binance_client.check_order_status(symbol, sl_order_id)
                    
                    if sl_status == OrderStatus.FILLED:
                        # SL order filled, cancel TP order
                        logger.info(f"SL order {sl_order_id} filled for {main_order_id}")
                        
                        # Update SL order status
                        await self.mongo_client.update_order_status(
                            sl_order_id,
                            OrderStatus.FILLED,
                            filled_at=datetime.utcnow()
                        )
                        
                        # Cancel TP order
                        if tp_order_id:
                            if is_futures:
                                await self.binance_client.futures_client.cancel_order(symbol, tp_order_id)
                            else:
                                await self.binance_client.cancel_order(symbol, tp_order_id)
                            
                            # Update TP order status
                            await self.mongo_client.update_order_status(
                                tp_order_id,
                                OrderStatus.CANCELLED,
                                cancelled_at=datetime.utcnow()
                            )
                        
                        # Update main order
                        await self.mongo_client.orders.update_one(
                            {"order_id": main_order_id},
                            {"$set": {
                                "sl_filled": True,
                                "sl_filled_at": datetime.utcnow()
                            }}
                        )
            
        except Exception as e:
            logger.error(f"Error monitoring TP/SL orders: {e}") 