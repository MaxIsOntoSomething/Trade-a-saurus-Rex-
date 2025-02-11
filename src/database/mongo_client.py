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
        self.positions = self.db.positions  # Add positions collection
        self.thresholds = self.db.thresholds  # Add thresholds collection

    async def init_indexes(self):
        await self.orders.create_index("order_id", unique=True)
        await self.orders.create_index("status")
        await self.orders.create_index("symbol")
        
        # Add indexes for positions collection
        await self.positions.create_index([
            ("symbol", 1),
            ("order_type", 1)
        ])
        await self.positions.create_index("last_updated")
        await self.positions.create_index("status")
        
        # Add indexes for thresholds
        await self.thresholds.create_index([
            ("symbol", 1),
            ("timeframe", 1),
            ("threshold", 1)
        ], unique=True)
        await self.thresholds.create_index("triggered_at")

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
                "order_type": OrderType.SPOT.value,  # Marked as spot order
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
                        "volume": {
                            "$sum": {
                                "$toDouble": {  # Convert to double before multiplication
                                    "$multiply": [
                                        {"$toDouble": "$price"},  # Convert price to double
                                        {"$toDouble": "$quantity"}  # Convert quantity to double
                                    ]
                                }
                            }
                        },
                        "count": {"$sum": 1}
                    }},
                    {"$sort": {"_id.date": 1}}
                ]
            
            elif viz_type == "profit_distribution":
                pipeline = [
                    {"$match": {"status": OrderStatus.FILLED.value}},
                    {"$group": {
                        "_id": "$symbol",
                        "total_profit": {
                            "$sum": {"$toDouble": "$profit"}  # Convert profit to double
                        },
                        "avg_profit": {
                            "$avg": {"$toDouble": "$profit"}  # Convert profit to double
                        },
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
                # Convert all numeric values to float
                if 'volume' in doc:
                    doc['volume'] = float(doc['volume'])
                if 'total_profit' in doc:
                    doc['total_profit'] = float(doc['total_profit'])
                if 'avg_profit' in doc:
                    doc['avg_profit'] = float(doc['avg_profit'])
                results.append(doc)
            return results

        except Exception as e:
            logger.error(f"Error getting visualization data: {e}")
            return []

    async def update_futures_position(self, position_data: Dict) -> bool:
        """Update or create futures position"""
        try:
            # Map position data fields correctly
            update_data = {
                "symbol": position_data["symbol"],
                "positionAmt": str(position_data["positionAmt"]),  # Use positionAmt instead of amount
                "entryPrice": str(position_data["entryPrice"]),
                "leverage": position_data["leverage"],
                "marginType": position_data["marginType"],
                "unrealizedProfit": str(position_data["unrealizedProfit"]),
                "last_updated": datetime.utcnow(),
                "status": "OPEN" if float(position_data["positionAmt"]) != 0 else "CLOSED",
                "order_type": "futures"
            }

            result = await self.positions.update_one(
                {
                    "symbol": position_data["symbol"],
                    "order_type": "futures"
                },
                {"$set": update_data},
                upsert=True
            )

            logger.debug(f"Updated position for {position_data['symbol']}: {update_data}")
            return bool(result.modified_count or result.upserted_id)

        except Exception as e:
            logger.error(f"Failed to update futures position: {e}")
            return False

    async def get_futures_positions(self, active_only: bool = True) -> List[Dict]:
        """Get futures positions"""
        try:
            query = {"order_type": "futures"}
            if active_only:
                query["status"] = "OPEN"
            
            cursor = self.positions.find(query)
            positions = []
            async for pos in cursor:
                positions.append(pos)
            return positions
        except Exception as e:
            logger.error(f"Failed to get futures positions: {e}")
            return []

    async def calculate_futures_pnl(self, symbol: str, timeframe: str = "all") -> Dict:
        """Calculate futures P&L for a symbol"""
        try:
            match_stage = {
                "symbol": symbol,
                "order_type": OrderType.FUTURES.value
            }

            if timeframe != "all":
                now = datetime.utcnow()
                if timeframe == "daily":
                    match_stage["created_at"] = {"$gte": now - timedelta(days=1)}
                elif timeframe == "weekly":
                    match_stage["created_at"] = {"$gte": now - timedelta(weeks=1)}
                elif timeframe == "monthly":
                    match_stage["created_at"] = {"$gte": now - timedelta(days=30)}

            pipeline = [
                {"$match": match_stage},
                {"$group": {
                    "_id": "$direction",
                    "total_volume": {"$sum": {"$multiply": ["$price", "$quantity"]}},
                    "total_fees": {"$sum": "$fees"},
                    "count": {"$sum": 1},
                    "pnl": {"$sum": "$realized_pnl"}
                }}
            ]

            results = await self.orders.aggregate(pipeline).to_list(None)
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "long": next((r for r in results if r["_id"] == "long"), 
                           {"total_volume": 0, "total_fees": 0, "count": 0, "pnl": 0}),
                "short": next((r for r in results if r["_id"] == "short"), 
                            {"total_volume": 0, "total_fees": 0, "count": 0, "pnl": 0})
            }
        except Exception as e:
            logger.error(f"Failed to calculate futures PNL: {e}")
            return {}

    async def update_order_pnl(self, order_id: str, realized_pnl: Decimal) -> bool:
        """Update order with realized PNL"""
        try:
            result = await self.orders.update_one(
                {"order_id": order_id},
                {
                    "$set": {
                        "realized_pnl": str(realized_pnl),
                        "updated_at": datetime.utcnow()
                    }
                }
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Failed to update order PNL: {e}")
            return False

    async def get_trading_summary(self, include_futures: bool = True) -> Dict:
        """Get comprehensive trading summary"""
        try:
            pipeline = [
                {
                    "$facet": {
                        "spot_orders": [
                            {"$match": {"order_type": "spot", "status": "filled"}},
                            {"$group": {
                                "_id": None,
                                "total_volume": {"$sum": {"$multiply": ["$price", "$quantity"]}},
                                "total_fees": {"$sum": "$fees"},
                                "count": {"$sum": 1}
                            }}
                        ],
                        "futures_orders": [
                            {"$match": {"order_type": "futures", "status": "filled"}},
                            {"$group": {
                                "_id": "$direction",
                                "total_volume": {"$sum": {"$multiply": ["$price", "$quantity"]}},
                                "total_fees": {"$sum": "$fees"},
                                "total_pnl": {"$sum": "$realized_pnl"},
                                "count": {"$sum": 1}
                            }}
                        ]
                    }
                }
            ]

            results = await self.orders.aggregate(pipeline).to_list(None)
            summary = results[0] if results else {}

            # Add active positions if including futures
            if include_futures:
                active_positions = await self.get_futures_positions(active_only=True)
                summary["active_positions"] = len(active_positions)
                summary["unrealized_pnl"] = sum(
                    float(pos["unrealized_profit"]) for pos in active_positions
                )

            return summary
        except Exception as e:
            logger.error(f"Failed to get trading summary: {e}")
            return {}

    async def track_futures_position(self, symbol: str, price: Decimal, quantity: Decimal, 
                                   direction: str, leverage: int) -> bool:
        """Track new futures position or update existing one"""
        try:
            # Get existing position
            position = await self.positions.find_one({
                "symbol": symbol,
                "status": "OPEN"
            })

            if position:
                # Update existing position with average entry
                old_quantity = Decimal(position['quantity'])
                old_entry = Decimal(position['entry_price'])
                total_quantity = old_quantity + quantity
                
                # Calculate new average entry price
                avg_entry = ((old_quantity * old_entry) + (quantity * price)) / total_quantity
                
                await self.positions.update_one(
                    {"_id": position["_id"]},
                    {
                        "$set": {
                            "quantity": str(total_quantity),
                            "entry_price": str(avg_entry),
                            "last_updated": datetime.utcnow()
                        }
                    }
                )
            else:
                # Create new position
                await self.positions.insert_one({
                    "symbol": symbol,
                    "quantity": str(quantity),
                    "entry_price": str(price),
                    "direction": direction,
                    "leverage": leverage,
                    "created_at": datetime.utcnow(),
                    "last_updated": datetime.utcnow(),
                    "status": "OPEN",
                    "trades": [{
                        "price": str(price),
                        "quantity": str(quantity),
                        "timestamp": datetime.utcnow()
                    }]
                })

            return True

        except Exception as e:
            logger.error(f"Failed to track futures position: {e}")
            return False

    async def get_position_pnl(self, symbol: str, current_price: Decimal) -> Dict:
        """Calculate PnL for a position"""
        try:
            position = await self.positions.find_one({
                "symbol": symbol,
                "status": "OPEN"
            })

            if not position:
                return {}

            entry_price = Decimal(position['entry_price'])
            quantity = Decimal(position['quantity'])
            leverage = int(position['leverage'])
            direction = position['direction']

            # Calculate PnL
            if direction == "LONG":
                pnl = (current_price - entry_price) * quantity * leverage
            else:
                pnl = (entry_price - current_price) * quantity * leverage

            pnl_percentage = (pnl / (entry_price * quantity)) * 100

            return {
                "entry_price": float(entry_price),
                "current_price": float(current_price),
                "quantity": float(quantity),
                "leverage": leverage,
                "direction": direction,
                "unrealized_pnl": float(pnl),
                "pnl_percentage": float(pnl_percentage),
                "position_value": float(entry_price * quantity),
                "trades": position.get('trades', [])
            }

        except Exception as e:
            logger.error(f"Failed to calculate position PnL: {e}")
            return {}

    async def update_position_status(self, symbol: str, status: str, 
                                   closed_at: datetime = None,
                                   closing_order_id: str = None,
                                   realized_pnl: Decimal = None) -> bool:
        """Update position status and add closing details"""
        try:
            update_data = {
                "status": status,
                "updated_at": datetime.utcnow()
            }
            
            if status == "CLOSED":
                update_data.update({
                    "closed_at": closed_at,
                    "closing_order_id": closing_order_id,
                    "realized_pnl": str(realized_pnl) if realized_pnl else None
                })
            
            result = await self.positions.update_one(
                {"symbol": symbol, "status": "OPEN"},
                {"$set": update_data}
            )
            return result.modified_count > 0
            
        except Exception as e:
            logger.error(f"Failed to update position status: {e}")
            return False

    async def get_position(self, symbol: str) -> Optional[Dict]:
        """Get single position by symbol"""
        try:
            return await self.positions.find_one({
                "symbol": symbol,
                "status": "OPEN"
            })
        except Exception as e:
            logger.error(f"Failed to get position: {e}")
            return None

    async def track_spot_order(self, symbol: str, price: Decimal, quantity: Decimal) -> bool:
        """Track a new spot order"""
        try:
            # Calculate total cost
            total_cost = price * quantity
            
            # Create order tracking document
            order_doc = {
                "symbol": symbol,
                "quantity": str(quantity),
                "price": str(price), 
                "total_cost": str(total_cost),
                "order_type": "spot",
                "created_at": datetime.utcnow(),
                "last_updated": datetime.utcnow(),
                "status": "OPEN"
            }
            
            await self.orders.insert_one(order_doc)
            return True
            
        except Exception as e:
            logger.error(f"Failed to track spot order: {e}")
            return False

    async def get_triggered_thresholds(self, symbol: str, timeframe: str) -> List[float]:
        """Get triggered thresholds for symbol and timeframe"""
        try:
            cursor = self.thresholds.find({
                "symbol": symbol,
                "timeframe": timeframe,
                "active": True
            })
            thresholds = []
            async for doc in cursor:
                thresholds.append(float(doc['threshold']))
            return thresholds
        except Exception as e:
            logger.error(f"Error getting triggered thresholds: {e}")
            return []

    async def add_triggered_threshold(self, symbol: str, timeframe: str, 
                                   threshold: float, price: float,
                                   reference_price: float, price_change: float) -> bool:
        """Store triggered threshold"""
        try:
            await self.thresholds.update_one(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "threshold": threshold
                },
                {
                    "$set": {
                        "triggered_at": datetime.utcnow(),
                        "price": price,
                        "reference_price": reference_price,
                        "price_change": price_change,
                        "active": True
                    }
                },
                upsert=True
            )
            return True
        except Exception as e:
            logger.error(f"Error storing triggered threshold: {e}")
            return False

    async def reset_timeframe_thresholds(self, timeframe: str) -> bool:
        """Reset thresholds for timeframe"""
        try:
            result = await self.thresholds.update_many(
                {"timeframe": timeframe},
                {"$set": {"active": False}}
            )
            return True
        except Exception as e:
            logger.error(f"Error resetting thresholds: {e}")
            return False

    # ...rest of existing code...
