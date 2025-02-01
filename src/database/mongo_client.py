import motor.motor_asyncio
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from ..types.models import Order, OrderStatus, TimeFrame, OrderType, TradeDirection  # Add imports
from ..types.constants import TAX_RATE, PRICE_PRECISION
from decimal import Decimal, ROUND_DOWN, DecimalException
import logging

logger = logging.getLogger(__name__)

class MongoClient:
    def __init__(self, uri: str, database: str):
        self.client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self.client[database]
        self.orders = self.db.orders

    async def init_indexes(self):
        await self.orders.create_index("order_id", unique=True)
        await self.orders.create_index("status")
        await self.orders.create_index("symbol")

    def _validate_order_data(self, order: Order) -> bool:
        """Validate order data before insertion"""
        required_fields = {
            'symbol': str,
            'status': OrderStatus,
            'order_type': OrderType,  # Add order_type validation
            'price': (Decimal, float),
            'quantity': (Decimal, float),
            'order_id': str,
            'created_at': datetime,
            'updated_at': datetime
        }
        
        # Optional fields with their types
        optional_fields = {
            'leverage': (int, type(None)),
            'direction': (TradeDirection, type(None)),
            'fees': (Decimal, float),
            'fee_asset': str
        }
        
        try:
            # Check required fields
            for field, expected_type in required_fields.items():
                value = getattr(order, field)
                if not isinstance(value, expected_type):
                    logger.error(f"Invalid type for {field}: expected {expected_type}, got {type(value)}")
                    return False
                    
            # Check optional fields if present
            for field, expected_type in optional_fields.items():
                value = getattr(order, field, None)
                if value is not None and not isinstance(value, expected_type):
                    logger.error(f"Invalid type for optional field {field}: expected {expected_type}, got {type(value)}")
                    return False
                    
            return True
        except AttributeError as e:
            logger.error(f"Missing required field: {e}")
            return False

    async def insert_order(self, order: Order) -> Optional[str]:
        """Insert order with validation"""
        if not self._validate_order_data(order):
            logger.error("Order validation failed")
            return None

        try:
            order_dict = {
                "symbol": order.symbol,
                "status": order.status.value,
                "price": str(order.price),
                "quantity": str(order.quantity),
                "threshold": float(order.threshold),
                "timeframe": order.timeframe.value,
                "order_id": order.order_id,
                "created_at": order.created_at,
                "updated_at": order.updated_at,
                "fees": str(order.fees),
                "fee_asset": order.fee_asset,
                "is_manual": bool(order.is_manual),
                "filled_at": order.filled_at,
                "cancelled_at": order.cancelled_at,
                "metadata": {  # Add metadata for better tracking
                    "inserted_at": datetime.utcnow(),
                    "last_checked": datetime.utcnow(),
                    "check_count": 0,
                    "error_count": 0
                }
            }
            
            result = await self.orders.insert_one(order_dict)
            return str(result.inserted_id)
            
        except Exception as e:
            logger.error(f"Failed to insert order: {e}")
            return None

    async def insert_manual_trade(self, order: Order) -> Optional[str]:
        """Insert a manually executed trade"""
        try:
            order_dict = {
                "symbol": order.symbol,
                "status": OrderStatus.FILLED.value,  # Always FILLED for manual trades
                "order_type": order.order_type.value,
                "price": str(order.price),
                "quantity": str(order.quantity),
                "timeframe": order.timeframe.value,
                "order_id": order.order_id,
                "created_at": order.created_at,
                "updated_at": order.updated_at,
                "filled_at": order.filled_at,
                "fees": str(order.fees),
                "fee_asset": order.fee_asset,
                "threshold": "Manual",  # Add manual threshold marker
            }
            
            # Add futures-specific fields if applicable
            if order.order_type == OrderType.FUTURES:
                order_dict.update({
                    "leverage": order.leverage,
                    "direction": order.direction.value
                })
            
            result = await self.orders.insert_one(order_dict)
            return str(result.inserted_id)
            
        except Exception as e:
            logger.error(f"Failed to insert manual trade: {e}")
            return None

    async def update_order_status(self, order_id: str, status: OrderStatus, 
                                filled_at: Optional[datetime] = None,
                                cancelled_at: Optional[datetime] = None) -> bool:
        update_dict = {
            "status": status.value,
            "updated_at": datetime.utcnow()
        }
        if filled_at:
            update_dict["filled_at"] = filled_at
        if cancelled_at:
            update_dict["cancelled_at"] = cancelled_at

        result = await self.orders.update_one(
            {"order_id": order_id},
            {"$set": update_dict}
        )
        return result.modified_count > 0

    async def get_pending_orders(self) -> List[Order]:
        """Get pending orders with improved error handling"""
        orders = []
        try:
            cursor = self.orders.find({
                "status": OrderStatus.PENDING.value
            }).sort("created_at", 1)  # Sort by creation time
            
            async for doc in cursor:
                try:
                    order = self._document_to_order(doc)
                    if order:
                        orders.append(order)
                except Exception as e:
                    logger.error(f"Error converting document to order: {e}")
                    # Update error count in metadata
                    await self.orders.update_one(
                        {"_id": doc["_id"]},
                        {
                            "$inc": {"metadata.error_count": 1},
                            "$set": {"metadata.last_error": str(e)}
                        }
                    )
            
            # Update last checked time for all fetched orders
            if orders:
                order_ids = [order.order_id for order in orders]
                await self.orders.update_many(
                    {"order_id": {"$in": order_ids}},
                    {
                        "$set": {"metadata.last_checked": datetime.utcnow()},
                        "$inc": {"metadata.check_count": 1}
                    }
                )
                
        except Exception as e:
            logger.error(f"Error fetching pending orders: {e}")
            
        return orders

    async def get_performance_stats(self) -> dict:
        """Get trading performance statistics"""
        pipeline = [
            {"$match": {"status": "filled"}},
            {"$group": {
                "_id": "$symbol",
                "total_orders": {"$sum": 1},
                "avg_price": {"$avg": {"$toDecimal": "$price"}},
                "total_quantity": {"$sum": {"$toDecimal": "$quantity"}}
            }}
        ]
        
        stats = {}
        async for result in self.orders.aggregate(pipeline):
            stats[result["_id"]] = {
                "total_orders": result["total_orders"],
                "avg_price": float(result["avg_price"]),
                "total_quantity": float(result["total_quantity"])
            }
        return stats

    async def get_position_stats(self, allowed_symbols: set = None) -> dict:
        """Get detailed position statistics including profits"""
        match_stage = {"status": "filled"}
        if allowed_symbols:
            match_stage["symbol"] = {"$in": list(allowed_symbols)}

        pipeline = [
            {"$match": match_stage},
            {"$group": {
                "_id": "$symbol",
                "total_quantity": {"$sum": {"$toDecimal": "$quantity"}},
                "total_cost": {
                    "$sum": {
                        "$multiply": [
                            {"$toDecimal": "$quantity"},
                            {"$toDecimal": "$price"}
                        ]
                    }
                },
                "orders": {"$push": {
                    "price": "$price",
                    "quantity": "$quantity",
                    "filled_at": "$filled_at"
                }},
                "order_count": {"$sum": 1}
            }},
            {"$project": {
                "symbol": "$_id",
                "total_quantity": {"$toString": "$total_quantity"},
                "total_cost": {"$toString": "$total_cost"},
                "avg_entry_price": {
                    "$toString": {
                        "$divide": ["$total_cost", "$total_quantity"]
                    }
                },
                "orders": 1,
                "order_count": 1
            }}
        ]

        positions = {}
        async for doc in self.orders.aggregate(pipeline):
            positions[doc["_id"]] = {
                "total_quantity": Decimal(doc["total_quantity"]),
                "total_cost": Decimal(doc["total_cost"]),
                "avg_entry_price": Decimal(doc["avg_entry_price"]),
                "order_count": doc["order_count"],
                "orders": doc["orders"]
            }
        return positions

    def calculate_profit_loss(self, position: dict, current_price: Decimal) -> dict:
        """Calculate profit/loss for a position including fees"""
        current_value = position["total_quantity"] * current_price
        total_fees = sum(Decimal(str(order.get('fees', 0))) for order in position['orders'])
        
        # Include fees in profit calculation
        absolute_pl = current_value - position["total_cost"] - total_fees
        percentage_pl = (absolute_pl / position["total_cost"]) * 100

        # Apply tax if profitable
        tax_amount = Decimal('0')
        if absolute_pl > 0:
            tax_amount = absolute_pl * Decimal(str(TAX_RATE))
            absolute_pl -= tax_amount

        return {
            "current_value": current_value.quantize(
                Decimal('0.01'), rounding=ROUND_DOWN
            ),
            "absolute_pl": absolute_pl.quantize(
                Decimal('0.01'), rounding=ROUND_DOWN
            ),
            "percentage_pl": percentage_pl.quantize(
                Decimal('0.01'), rounding=ROUND_DOWN
            ),
            "tax_amount": tax_amount.quantize(
                Decimal('0.01'), rounding=ROUND_DOWN
            ),
            "total_fees": total_fees.quantize(Decimal('0.01'), rounding=ROUND_DOWN),
        }

    def generate_profit_diagram(self, position: dict, current_price: Decimal) -> str:
        """Generate ASCII diagram of profit/loss"""
        pl_percentage = float((current_price - position["avg_entry_price"]) / 
                            position["avg_entry_price"] * 100)
        
        # Create diagram
        diagram = "🎯 P/L Diagram:\n"
        diagram += "Entry: " + "▼".rjust(10) + "\n"
        diagram += "Now:   " + ("△" if pl_percentage >= 0 else "▽").rjust(
            int(10 + min(max(pl_percentage, -10), 10))
        ) + "\n"
        
        # Add scale
        diagram += "-10%" + "─" * 8 + "0%" + "─" * 8 + "+10%\n"
        
        return diagram

    def _document_to_order(self, doc: dict) -> Optional[Order]:
        """Convert MongoDB document to Order object with error handling"""
        try:
            # Updated required fields list
            required_fields = ['symbol', 'status', 'price', 'quantity', 
                             'order_id', 'created_at', 'updated_at']
            
            if not all(field in doc for field in required_fields):
                missing = [field for field in required_fields if field not in doc]
                logger.error(f"Document missing required fields: {missing}")
                return None

            # Create order with mandatory fields
            order = Order(
                symbol=doc["symbol"],
                status=OrderStatus(doc["status"]),
                order_type=OrderType(doc.get("order_type", "spot")),  # Default to spot
                price=Decimal(doc["price"]),
                quantity=Decimal(doc["quantity"]),
                timeframe=TimeFrame(doc.get("timeframe", "daily")),  # Default to daily
                order_id=doc["order_id"],
                created_at=doc["created_at"],
                updated_at=doc["updated_at"],
                filled_at=doc.get("filled_at"),
                cancelled_at=doc.get("cancelled_at"),
                fees=Decimal(doc.get("fees", "0")),
                fee_asset=doc.get("fee_asset", "USDT"),
                threshold=float(doc["threshold"]) if doc.get("threshold") not in [None, "Manual"] else None
            )

            # Add futures-specific fields if present
            if doc.get("leverage") is not None:
                order.leverage = int(doc["leverage"])
            if doc.get("direction"):
                order.direction = TradeDirection(doc["direction"])

            return order
            
        except (ValueError, KeyError, TypeError, DecimalException) as e:
            logger.error(f"Error converting document {doc.get('order_id', 'unknown')}: {e}")
            return None

    async def cleanup_stale_orders(self, hours: int = 24) -> int:
        """Cleanup orders that haven't been checked in a while"""
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        try:
            result = await self.orders.update_many(
                {
                    "status": OrderStatus.PENDING.value,
                    "metadata.last_checked": {"$lt": cutoff}
                },
                {
                    "$set": {
                        "status": OrderStatus.CANCELLED.value,
                        "cancelled_at": datetime.utcnow(),
                        "metadata.cleanup_reason": "stale"
                    }
                }
            )
            return result.modified_count
        except Exception as e:
            logger.error(f"Error cleaning up stale orders: {e}")
            return 0

    async def get_visualization_data(self, viz_type: str, days: int = 30) -> List[Dict]:
        """Get data for visualizations"""
        try:
            cutoff = datetime.utcnow() - timedelta(days=days)
            
            if viz_type == "daily_volume":
                pipeline = [
                    {"$match": {
                        "created_at": {"$gte": cutoff},
                        "status": OrderStatus.FILLED.value
                    }},
                    {"$group": {
                        "_id": {
                            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
                            "symbol": "$symbol"
                        },
                        "volume": {"$sum": {"$multiply": [
                            {"$toDecimal": "$price"},
                            {"$toDecimal": "$quantity"}
                        ]}},
                        "count": {"$sum": 1}
                    }},
                    {"$sort": {"_id.date": 1}}
                ]
            
            elif viz_type == "profit_distribution":
                pipeline = [
                    {"$match": {"status": OrderStatus.FILLED.value}},
                    {"$group": {
                        "_id": "$symbol",
                        "total_profit": {"$sum": "$profit"},
                        "avg_profit": {"$avg": "$profit"},
                        "count": {"$sum": 1}
                    }}
                ]
            
            elif viz_type == "order_types":
                pipeline = [
                    {"$match": {"created_at": {"$gte": cutoff}}},
                    {"$group": {
                        "_id": {
                            "type": "$order_type",
                            "status": "$status"
                        },
                        "count": {"$sum": 1}
                    }}
                ]
            
            elif viz_type == "hourly_activity":
                pipeline = [
                    {"$match": {"created_at": {"$gte": cutoff}}},
                    {"$group": {
                        "_id": {
                            "hour": {"$hour": "$created_at"},
                            "status": "$status"
                        },
                        "count": {"$sum": 1}
                    }},
                    {"$sort": {"_id.hour": 1}}
                ]
            else:
                raise ValueError(f"Unknown visualization type: {viz_type}")

            results = []
            async for doc in self.orders.aggregate(pipeline):
                results.append(doc)
            return results

        except Exception as e:
            logger.error(f"Error getting visualization data: {e}")
            return []
