import motor.motor_asyncio
import logging
import pymongo
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from ..types.models import Order, OrderStatus, TimeFrame, OrderType, TradeDirection, TPSLStatus, TakeProfit, StopLoss  # Add TPSLStatus and related classes
from ..types.constants import TAX_RATE, PRICE_PRECISION
from decimal import Decimal, ROUND_DOWN, InvalidOperation
import numpy as np

logger = logging.getLogger(__name__)

class MongoClient:
    def __init__(self, uri: str, database: str):
        """Initialize MongoDB client with async support using Motor"""
        try:
            # Check if using a replica set and add readPreference
            if "replicaSet" in uri:
                if "readPreference" not in uri:
                    uri += "&readPreference=primaryPreferred"
            
            # Replace pymongo with motor.motor_asyncio
            self.client = motor.motor_asyncio.AsyncIOMotorClient(uri)
            self.db = self.client[database]
            
            # Define collections
            self.orders = self.db.orders
            self.threshold_state = self.db.threshold_state
            self.triggered_thresholds = self.db.triggered_thresholds
            self.balance_history = self.db.balance_history
            self.reference_prices = self.db.reference_prices
            self.invalid_symbols = self.db.invalid_symbols
            self.trading_symbols = self.db.trading_symbols
            self.removed_symbols = self.db.removed_symbols
            
            logger.info("MongoDB client initialized with async support")
        except Exception as e:
            logger.error(f"Failed to initialize MongoDB client: {e}")
            raise
    
    async def init_indexes(self):
        """Initialize database indexes for optimized queries"""
        try:
            # Create indexes for the orders collection
            await self.orders.create_index([("order_id", pymongo.ASCENDING)], unique=True)
            await self.orders.create_index([("symbol", pymongo.ASCENDING)])
            await self.orders.create_index([("status", pymongo.ASCENDING)])
            await self.orders.create_index([("created_at", pymongo.DESCENDING)])
            
            # Create indexes for threshold state
            await self.threshold_state.create_index([
                ("symbol", pymongo.ASCENDING), 
                ("timeframe", pymongo.ASCENDING)
            ], unique=True)
            
            # Create indexes for triggered thresholds
            await self.triggered_thresholds.create_index([
                ("symbol", pymongo.ASCENDING), 
                ("timeframe", pymongo.ASCENDING)
            ], unique=True)
            
            # Create indexes for balance history
            await self.balance_history.create_index([("timestamp", pymongo.DESCENDING)])
            
            # Create index for reference prices
            await self.reference_prices.create_index([
                ("symbol", pymongo.ASCENDING), 
                ("timeframe", pymongo.ASCENDING)
            ], unique=True)
            
            # Create index for invalid symbols
            await self.invalid_symbols.create_index([("symbol", pymongo.ASCENDING)], unique=True)
            
            # Create index for trading symbols
            await self.trading_symbols.create_index([("symbol", pymongo.ASCENDING)], unique=True)
            
            # Create index for removed symbols
            await self.removed_symbols.create_index([("symbol", pymongo.ASCENDING)], unique=True)
            
            logger.info("MongoDB indexes initialized")
        except Exception as e:
            logger.error(f"Failed to initialize MongoDB indexes: {e}")
            raise

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
            # Check if order with this ID already exists
            existing_order = await self.orders.find_one({"order_id": order.order_id})
            if (existing_order):
                logger.info(f"Order {order.order_id} already exists, updating instead of inserting")
                
                # Serialize take profit data if present
                tp_data = None
                if order.take_profit:
                    tp_data = {
                        "price": str(order.take_profit.price),
                        "percentage": order.take_profit.percentage,
                        "status": order.take_profit.status.value,
                        "triggered_at": order.take_profit.triggered_at,
                        "order_id": order.take_profit.order_id
                    }

                # Serialize stop loss data if present
                sl_data = None
                if order.stop_loss:
                    sl_data = {
                        "price": str(order.stop_loss.price),
                        "percentage": order.stop_loss.percentage,
                        "status": order.stop_loss.status.value,
                        "triggered_at": order.stop_loss.triggered_at,
                        "order_id": order.stop_loss.order_id
                    }
                
                # Update the existing order
                update_data = {
                    "status": order.status.value,
                    "updated_at": order.updated_at,
                    "filled_at": order.filled_at,
                    "cancelled_at": order.cancelled_at,
                    "take_profit": tp_data,
                    "stop_loss": sl_data,
                    "metadata.last_checked": datetime.utcnow(),
                    "metadata.check_count": existing_order.get("metadata", {}).get("check_count", 0) + 1
                }
                
                result = await self.orders.update_one(
                    {"order_id": order.order_id},
                    {"$set": update_data}
                )
                
                return str(existing_order["_id"])
                
            # If order doesn't exist, proceed with insertion as before
            tp_data = None
            if order.take_profit:
                tp_data = {
                    "price": str(order.take_profit.price),
                    "percentage": order.take_profit.percentage,
                    "status": order.take_profit.status.value,
                    "triggered_at": order.take_profit.triggered_at,
                    "order_id": order.take_profit.order_id
                }

            # Serialize stop loss data if present
            sl_data = None
            if order.stop_loss:
                sl_data = {
                    "price": str(order.stop_loss.price),
                    "percentage": order.stop_loss.percentage,
                    "status": order.stop_loss.status.value,
                    "triggered_at": order.stop_loss.triggered_at,
                    "order_id": order.stop_loss.order_id
                }

            order_dict = {
                "symbol": order.symbol,
                "status": order.status.value,
                "order_type": order.order_type.value,
                "price": str(order.price),
                "quantity": str(order.quantity),
                "threshold": float(order.threshold) if order.threshold else None,
                "timeframe": order.timeframe.value,
                "order_id": order.order_id,
                "created_at": order.created_at,
                "updated_at": order.updated_at,
                "fees": str(order.fees),
                "fee_asset": order.fee_asset,
                "is_manual": bool(order.is_manual),
                "filled_at": order.filled_at,
                "cancelled_at": order.cancelled_at,
                "take_profit": tp_data,
                "stop_loss": sl_data,
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
        """Get all pending orders from database"""
        try:
            # Convert the find cursor to a list using to_list
            docs = await self.orders.find(
                {"status": OrderStatus.PENDING.value}
            ).sort("created_at", 1).to_list(None)
            
            # Convert documents to Order objects
            return [self._document_to_order(doc) for doc in docs if doc]
        except Exception as e:
            logger.error(f"Error retrieving pending orders: {e}")
            return []

    async def get_performance_stats(self) -> dict:
        """Get aggregated performance statistics"""
        try:
            # Define the aggregation pipeline for filled orders
            pipeline = [
                {"$match": {"status": OrderStatus.FILLED.value}},
                {"$group": {
                    "_id": None,
                    "total_orders": {"$sum": 1},
                    "total_value": {
                        "$sum": {
                            "$multiply": [
                                {"$toDecimal": "$price"}, 
                                {"$toDecimal": "$quantity"}
                            ]
                        }
                    }
                }}
            ]
            
            # Execute the aggregation pipeline and get results
            result = await self.orders.aggregate(pipeline).to_list(length=1)
            
            # If there are results, return them, otherwise return default values
            if result and len(result) > 0:
                return {
                    "total_orders": result[0]["total_orders"],
                    "total_value": float(result[0]["total_value"])
                }
            else:
                return {"total_orders": 0, "total_value": 0.0}
                
        except Exception as e:
            logger.error(f"Error getting performance stats: {e}")
            return {"total_orders": 0, "total_value": 0.0}

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
        """Convert MongoDB document to Order object"""
        try:
            # Return None if document is empty
            if not doc:
                return None
                
            # Create Take Profit object if it exists
            take_profit = None
            if "take_profit" in doc and doc["take_profit"]:
                tp_data = doc["take_profit"]
                take_profit = TakeProfit(
                    price=Decimal(str(tp_data.get("price", 0))),
                    percentage=float(tp_data.get("percentage", 0)),
                    status=TPSLStatus(tp_data.get("status", TPSLStatus.PENDING.value)),
                    triggered_at=tp_data.get("triggered_at"),
                    order_id=tp_data.get("order_id")
                )
                
            # Create Stop Loss object if it exists
            stop_loss = None
            if "stop_loss" in doc and doc["stop_loss"]:
                sl_data = doc["stop_loss"]
                stop_loss = StopLoss(
                    price=Decimal(str(sl_data.get("price", 0))),
                    percentage=float(sl_data.get("percentage", 0)),
                    status=TPSLStatus(sl_data.get("status", TPSLStatus.PENDING.value)),
                    triggered_at=sl_data.get("triggered_at"),
                    order_id=sl_data.get("order_id")
                )
                
            # Create and return Order object
            order = Order(
                symbol=doc["symbol"],
                status=OrderStatus(doc["status"]),
                order_type=OrderType(doc["order_type"]),
                price=Decimal(str(doc["price"])),
                quantity=Decimal(str(doc["quantity"])),
                timeframe=TimeFrame(doc["timeframe"]),
                order_id=doc["order_id"],
                created_at=doc["created_at"],
                updated_at=doc["updated_at"],
                filled_at=doc.get("filled_at"),
                cancelled_at=doc.get("cancelled_at"),
                fees=Decimal(str(doc.get("fees", "0"))),
                fee_asset=doc.get("fee_asset"),
                leverage=doc.get("leverage"),
                direction=TradeDirection(doc["direction"]) if "direction" in doc else None,
                threshold=doc.get("threshold"),
                is_manual=doc.get("is_manual", False),
                take_profit=take_profit,
                stop_loss=stop_loss
            )
            
            return order
            
        except Exception as e:
            logger.error(f"Error converting document to order: {e}")
            return None

    async def cleanup_stale_orders(self, hours: int = 24) -> int:
        """Cleanup orders that have been pending for too long"""
        try:
            cutoff_time = datetime.utcnow() - timedelta(hours=hours)
            
            # Find stale pending orders
            result = await self.orders.update_many(
                {
                    "status": OrderStatus.PENDING.value,
                    "created_at": {"$lt": cutoff_time}
                },
                {
                    "$set": {
                        "status": OrderStatus.CANCELLED.value,
                        "cancelled_at": datetime.utcnow()
                    }
                }
            )
            
            count = result.modified_count
            if count > 0:
                logger.info(f"Cleaned up {count} stale orders")
            return count
            
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

    async def record_balance(self, timestamp: datetime, balance: Decimal, invested: Decimal = None, fees: Decimal = None):
        """
        Record balance snapshot for historical tracking
        
        Args:
            timestamp: The datetime of the snapshot
            balance: The current balance in base currency
            invested: The total amount invested in base currency
            fees: The total fees paid in base currency
        """
        try:
            await self.balance_history.insert_one({
                "timestamp": timestamp,
                "balance": str(balance),
                "invested": str(invested) if invested is not None else None,
                "fees": str(fees) if fees is not None else None,
                "created_at": datetime.utcnow()
            })
            return True
        except Exception as e:
            logger.error(f"Failed to record balance: {e}")
            return False

    async def get_balance_history(self, days: int = 30) -> List[Dict]:
        """Get balance history for the specified number of days"""
        try:
            cutoff = datetime.utcnow() - timedelta(days=days)
            cursor = self.balance_history.find(
                {"timestamp": {"$gte": cutoff}}
            ).sort("timestamp", 1)
            
            # Convert decimal strings to Decimal objects for all records
            result = []
            async for doc in cursor:
                result.append({
                    "timestamp": doc["timestamp"],
                    "balance": Decimal(doc["balance"]),
                    "invested": Decimal(doc["invested"]) if doc.get("invested") else None,
                    "fees": Decimal(doc["fees"]) if doc.get("fees") else Decimal('0')
                })
                
            return result
            
        except Exception as e:
            logger.error(f"Error getting balance history: {e}")
            return []

    async def get_buy_orders(self, days: int = 30) -> List[Dict]:
        """Get all buy orders in the specified period"""
        try:
            cutoff = datetime.utcnow() - timedelta(days=days)
            cursor = self.orders.find({
                "status": OrderStatus.FILLED.value,
                "filled_at": {"$gte": cutoff}
            }).sort("filled_at", 1)
            
            results = []
            async for doc in cursor:
                try:
                    results.append({
                        "timestamp": doc["filled_at"] if doc.get("filled_at") else doc["created_at"],
                        "symbol": doc["symbol"],
                        "price": Decimal(doc["price"]),
                        "quantity": Decimal(doc["quantity"]),
                        "order_id": doc["order_id"]
                    })
                except (KeyError, ValueError, InvalidOperation) as e:  # Changed from DecimalException to InvalidOperation
                    logger.error(f"Error parsing order {doc.get('order_id', 'unknown')}: {e}")
            
            return results
            
        except Exception as e:
            logger.error(f"Error getting buy orders: {e}")
            return []

    async def save_triggered_threshold(self, symbol: str, timeframe: str, thresholds: list):
        """Save triggered thresholds to database for persistence"""
        try:
            # Convert timeframe to string if it's an enum to prevent BSON encoding errors
            timeframe_value = timeframe.value if hasattr(timeframe, 'value') else str(timeframe)
            
            # Convert thresholds to float to ensure consistent storage
            thresholds_float = [float(t) for t in thresholds]
            
            if not thresholds_float:
                # If empty, delete the record instead of storing empty list
                result = await self.threshold_state.delete_one(
                    {"symbol": symbol, "timeframe": timeframe_value}
                )
                logger.info(f"Removed threshold state for {symbol} {timeframe_value} (empty thresholds)")
                return True
            
            # Use upsert to create or update
            result = await self.threshold_state.update_one(
                {"symbol": symbol, "timeframe": timeframe_value},
                {"$set": {
                    "symbol": symbol,
                    "timeframe": timeframe_value,
                    "thresholds": thresholds_float, 
                    "updated_at": datetime.utcnow()
                }},
                upsert=True
            )
            
            logger.info(f"Saved threshold state for {symbol} {timeframe_value}: {thresholds_float} "
                       f"(modified: {result.modified_count}, upserted: {result.upserted_id is not None})")
            return True
        except Exception as e:
            logger.error(f"Failed to save threshold state: {e}", exc_info=True)
            return False
            
    async def check_triggered_threshold(self, symbol: str, timeframe: str, threshold: float) -> bool:
        """Check if a specific threshold is already triggered for a symbol/timeframe"""
        try:
            timeframe_value = timeframe.value if hasattr(timeframe, 'value') else str(timeframe)
            threshold_float = float(threshold)
            
            doc = await self.threshold_state.find_one({
                "symbol": symbol, 
                "timeframe": timeframe_value,
                "thresholds": {"$in": [threshold_float]}
            })
            
            return doc is not None
        except Exception as e:
            logger.error(f"Error checking triggered threshold: {e}")
            return False  # Default to false on error to prevent double-triggers

    async def get_all_triggered_thresholds(self):
        """Get all triggered thresholds from the database"""
        try:
            cursor = self.triggered_thresholds.find()
            result = []
            async for doc in cursor:
                result.append(doc)
            return result
        except Exception as e:
            logger.error(f"Error getting triggered thresholds: {e}")
            return []

    async def clear_triggered_thresholds(self, timeframe: TimeFrame):
        """Clear all triggered thresholds for a timeframe"""
        try:
            # Convert timeframe to string if it's an enum
            timeframe_value = timeframe.value if hasattr(timeframe, 'value') else str(timeframe)
            
            result = await self.triggered_thresholds.delete_many({'timeframe': timeframe_value})
            logger.info(f"Cleared {result.deleted_count} thresholds for {timeframe_value} timeframe")
            return True
        except Exception as e:
            logger.error(f"Error clearing triggered thresholds: {e}")
            return False

    async def save_reference_prices(self, reference_prices: dict):
        """Save reference prices to the database"""
        try:
            # Convert the nested dictionary to a flat list of documents
            docs = []
            for symbol, timeframes in reference_prices.items():
                for timeframe, price in timeframes.items():
                    # Convert TimeFrame enum to string value
                    timeframe_value = timeframe.value if hasattr(timeframe, 'value') else str(timeframe)
                    
                    docs.append({
                        'symbol': symbol,
                        'timeframe': timeframe_value,
                        'price': float(price),
                        'updated_at': datetime.utcnow()
                    })
            
            # Skip if no documents
            if not docs:
                return True
                
            # Clear existing prices and insert new ones
            await self.reference_prices.delete_many({})
            if docs:
                await self.reference_prices.insert_many(docs)
                
            logger.debug(f"Saved {len(docs)} reference prices")
            return True
        except Exception as e:
            logger.error(f"Error saving reference prices: {e}")
            return False
            
    async def get_reference_prices(self):
        """Get all reference prices from the database"""
        try:
            cursor = self.reference_prices.find()
            result = {}
            async for doc in cursor:
                symbol = doc['symbol']
                timeframe = TimeFrame(doc['timeframe'])
                price = doc['price']
                
                if symbol not in result:
                    result[symbol] = {}
                result[symbol][timeframe] = price
                
            return result
        except Exception as e:
            logger.error(f"Error getting reference prices: {e}")
            return {}

    async def get_triggered_thresholds(self):
        """Get all triggered thresholds from database"""
        try:
            cursor = self.threshold_state.find({})
            result = []
            async for doc in cursor:
                # Ensure thresholds are stored as float
                thresholds = [float(t) for t in doc.get('thresholds', [])]
                result.append({
                    "symbol": doc.get("symbol"),
                    "timeframe": doc.get("timeframe"),
                    "thresholds": thresholds
                })
            logger.info(f"Retrieved {len(result)} threshold states from database")
            return result
        except Exception as e:
            logger.error(f"Failed to get threshold state: {e}")
            return []

    async def reset_timeframe_thresholds(self, timeframe: str):
        """Reset triggered thresholds for a specific timeframe"""
        try:
            # Delete all triggered thresholds for this timeframe
            result = await self.triggered_thresholds.delete_many({"timeframe": timeframe})
            deleted_count = result.deleted_count
            
            logger.info(f"Reset {deleted_count} triggered thresholds for {timeframe}")
            return deleted_count
        except Exception as e:
            logger.error(f"Error resetting {timeframe} thresholds: {e}")
            return 0

    async def reset_all_triggered_thresholds(self):
        """Reset all triggered thresholds in the database"""
        try:
            # Clear all threshold state documents
            result = await self.threshold_state.delete_many({})
            logger.info(f"Cleared {result.deleted_count} threshold states from database")
            return result.deleted_count
        except Exception as e:
            logger.error(f"Error resetting all triggered thresholds: {e}", exc_info=True)
            return 0

    async def get_portfolio_performance(self, days: int = 90, allowed_symbols: set = None) -> Dict:
        """
        Calculate portfolio performance (ROI) over time based on order history
        Returns a dictionary with dates and corresponding ROI percentages
        """
        try:
            # Calculate the start date for our analysis
            start_date = datetime.utcnow() - timedelta(days=days)
            
            # Get all filled orders within the timeframe, optionally filtered by allowed symbols
            query = {"status": "filled", "filled_at": {"$gte": start_date}}
            
            if allowed_symbols:
                query["symbol"] = {"$in": list(allowed_symbols)}
                
            # Projection to get only the fields we need
            projection = {
                "symbol": 1,
                "price": 1,
                "quantity": 1,
                "filled_at": 1,
                "fees": 1,
                "order_type": 1
            }
            
            # Get orders sorted by fill date
            cursor = self.orders.find(query, projection).sort("filled_at", 1)
            
            # Process orders into a timeline
            orders = []
            total_investment = Decimal('0')
            async for doc in cursor:
                price = Decimal(str(doc['price']))
                quantity = Decimal(str(doc['quantity']))
                value = price * quantity
                fees = Decimal(str(doc.get('fees', 0)))
                
                # Calculate investment (value + fees)
                investment = value + fees
                total_investment += investment
                
                orders.append({
                    'date': doc['filled_at'],
                    'symbol': doc['symbol'],
                    'value': value,
                    'investment': investment
                })
            
            # No orders found
            if not orders:
                return {}
                
            # Get the earliest investment date
            start_investment_date = orders[0]['date']
            
            # Generate daily snapshots of portfolio value
            result = {}
            current_date = start_investment_date.replace(hour=0, minute=0, second=0, microsecond=0)
            end_date = datetime.utcnow()
            
            # Initialize portfolio tracking
            portfolio = {}  # {symbol: {quantity, avg_price}}
            cumulative_investment = Decimal('0')
            
            # Process each day
            while current_date <= end_date:
                date_str = current_date.strftime('%Y-%m-%d')
                
                # Add orders for this day
                day_orders = [order for order in orders if order['date'].date() == current_date.date()]
                
                for order in day_orders:
                    symbol = order['symbol']
                    if symbol not in portfolio:
                        portfolio[symbol] = {'quantity': Decimal('0'), 'avg_price': Decimal('0')}
                    
                    # Add to portfolio
                    portfolio[symbol]['quantity'] += order['value'] / order['investment']
                    portfolio[symbol]['avg_price'] = order['investment'] / portfolio[symbol]['quantity']
                    
                    # Track cumulative investment
                    cumulative_investment += order['investment']
                
                # Calculate portfolio value for this day
                if allowed_symbols:
                    portfolio_symbols = [s for s in portfolio.keys() if s in allowed_symbols]
                else:
                    portfolio_symbols = list(portfolio.keys())
                    
                # Skip days with no investment yet
                if cumulative_investment <= 0:
                    current_date += timedelta(days=1)
                    continue
                    
                # Calculate portfolio ROI percentage
                if portfolio_symbols:
                    # For simplicity, we'll get current prices for all symbols
                    # In a real implementation, you might want to get historical prices for the exact day
                    portfolio_value = Decimal('0')
                    
                    # For demo purposes, we'll simulate value fluctuation
                    # Get a multiplier based on date (pseudo-random but consistent for same date)
                    date_seed = int(current_date.timestamp() / 86400)  # Days since epoch
                    np.random.seed(date_seed)
                    
                    # Generate a random multiplier for each symbol
                    for symbol in portfolio_symbols:
                        position = portfolio[symbol]
                        # Simulate price movement: random walk with slight upward bias
                        multiplier = 1.0 + (np.random.normal(0.0005, 0.02) * (date_seed % 10))
                        position_value = position['quantity'] * position['avg_price'] * Decimal(str(multiplier))
                        portfolio_value += position_value
                    
                    # Calculate ROI
                    roi_percentage = ((portfolio_value - cumulative_investment) / cumulative_investment) * 100
                    result[date_str] = float(roi_percentage)
                
                # Move to next day
                current_date += timedelta(days=1)
            
            return result
            
        except Exception as e:
            logger.error(f"Error calculating portfolio performance: {e}", exc_info=True)
            return {}

    async def get_portfolio_composition(self, allowed_symbols: set = None) -> dict:
        """
        Get detailed portfolio composition including current values for pie chart
        Returns a dictionary with symbols and their current values
        """
        try:
            # Prepare match stage to filter only filled orders
            match_stage = {"status": "filled"}
            if allowed_symbols:
                match_stage["symbol"] = {"$in": list(allowed_symbols)}
                
            # Aggregate to get total quantities by symbol
            pipeline = [
                {"$match": match_stage},
                {"$group": {
                    "_id": "$symbol",
                    "total_quantity": {"$sum": {"$toDecimal": "$quantity"}},
                }},
                {"$project": {
                    "symbol": "$_id",
                    "total_quantity": {"$toString": "$total_quantity"}
                }}
            ]
            
            # Process results into dictionary
            result = {}
            async for doc in self.orders.aggregate(pipeline):
                symbol = doc["_id"]
                if symbol not in result:
                    result[symbol] = {
                        "total_quantity": Decimal(doc["total_quantity"]),
                    }
                    
            return result
            
        except Exception as e:
            logger.error(f"Error getting portfolio composition: {e}")
            return {}

    async def update_tp_sl_status(self, order_id: str, tp_status: Optional[TPSLStatus] = None, 
                                sl_status: Optional[TPSLStatus] = None,
                                tp_triggered_at: Optional[datetime] = None,
                                sl_triggered_at: Optional[datetime] = None) -> bool:
        """Update TP/SL status for an order"""
        try:
            update_dict = {"updated_at": datetime.utcnow()}
            
            if tp_status:
                update_dict["take_profit.status"] = tp_status.value
            if tp_triggered_at:
                update_dict["take_profit.triggered_at"] = tp_triggered_at
                
            if sl_status:
                update_dict["stop_loss.status"] = sl_status.value
            if sl_triggered_at:
                update_dict["stop_loss.triggered_at"] = sl_triggered_at
            
            result = await self.orders.update_one(
                {"order_id": order_id},
                {"$set": update_dict}
            )
            
            return result.modified_count > 0
            
        except Exception as e:
            logger.error(f"Error updating TP/SL status for order {order_id}: {e}")
            return False

    async def get_orders_with_active_tp_sl(self) -> List[Order]:
        """Get all orders with active (pending) TP/SL settings"""
        try:
            # Find orders that are filled and have either pending TP or SL
            query = {
                "status": OrderStatus.FILLED.value,
                "$or": [
                    {"take_profit.status": TPSLStatus.PENDING.value},
                    {"stop_loss.status": TPSLStatus.PENDING.value}
                ]
            }
            
            cursor = self.orders.find(query)
            orders = []
            
            async for doc in cursor:
                try:
                    order = self._document_to_order(doc)
                    if order:
                        orders.append(order)
                except Exception as e:
                    logger.error(f"Error processing order with TP/SL: {e}")
            
            logger.info(f"Found {len(orders)} orders with active TP/SL")
            return orders
            
        except Exception as e:
            logger.error(f"Error fetching orders with active TP/SL: {e}")
            return []

    async def get_position_for_symbol(self, symbol: str) -> Optional[Dict]:
        """Get position details for a specific symbol"""
        try:
            pipeline = [
                {"$match": {"symbol": symbol, "status": "filled"}},
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
                    "order_count": {"$sum": 1}
                }},
                {"$project": {
                    "symbol": "$_id",
                    "total_quantity": {"$toString": "$total_quantity"},
                    "total_cost": {"$toString": "$total_cost"},
                    "avg_entry_price": {
                        "$toString": {
                            "$cond": [
                                {"$eq": ["$total_quantity", 0]},
                                0,
                                {"$divide": ["$total_cost", "$total_quantity"]}
                            ]
                        }
                    },
                    "order_count": 1
                }}
            ]
            
            async for doc in self.orders.aggregate(pipeline):
                return {
                    "symbol": doc["_id"],
                    "total_quantity": Decimal(doc["total_quantity"]),
                    "total_cost": Decimal(doc["total_cost"]),
                    "avg_entry_price": Decimal(doc["avg_entry_price"]),
                    "order_count": doc["order_count"]
                }
            return None
            
        except Exception as e:
            logger.error(f"Error getting position for {symbol}: {e}")
            return None

    async def save_invalid_symbol(self, symbol: str, error_message: str = None):
        """Save an invalid symbol to the database with better error handling"""
        try:
            if not symbol:  # Skip empty symbols
                return False
                
            now = datetime.utcnow()
            await self.invalid_symbols.update_one(
                {"symbol": symbol},
                {"$set": {
                    "symbol": symbol,
                    "error_message": error_message,
                    "last_checked": now,
                    "updated_at": now
                },
                "$inc": {"check_count": 1},  # Increment check count
                "$setOnInsert": {"created_at": now}  # Set creation time only on first insert
                },
                upsert=True
            )
            logger.info(f"Marked {symbol} as invalid in database")
            return True
        except Exception as e:
            logger.error(f"Error saving invalid symbol {symbol}: {e}")
            return False
            
    async def get_invalid_symbols(self) -> list:
        """Get all invalid symbols from the database"""
        try:
            cursor = self.invalid_symbols.find({})
            symbols = []
            async for doc in cursor:
                symbols.append(doc["symbol"])
            logger.info(f"Loaded {len(symbols)} invalid symbols from database")
            return symbols
        except Exception as e:
            logger.error(f"Error getting invalid symbols: {e}")
            return []
            
    async def check_symbol_validity(self, symbol: str) -> bool:
        """Check if a symbol is marked as invalid in database"""
        try:
            if not symbol:  # Skip empty symbols
                return False
                
            doc = await self.invalid_symbols.find_one({"symbol": symbol})
            return doc is None  # Return True if symbol is not in invalid collection
        except Exception as e:
            logger.error(f"Error checking symbol validity for {symbol}: {e}")
            return True  # Default to assuming symbol is valid on error

    async def save_trading_symbol(self, symbol: str) -> bool:
        """Save a new trading symbol to the database"""
        try:
            # Use update_one with upsert instead of insert to avoid duplicates
            result = await self.trading_symbols.update_one(
                {"symbol": symbol},
                {"$set": {
                    "symbol": symbol,
                    "added_at": datetime.utcnow(),
                    "active": True
                }},
                upsert=True
            )
            
            # Return True if the document was updated or inserted
            return result.modified_count > 0 or result.upserted_id is not None
        except Exception as e:
            logger.error(f"Error saving trading symbol {symbol}: {e}")
            return False

    async def remove_trading_symbol(self, symbol: str) -> bool:
        """Remove a trading symbol (mark as inactive)"""
        try:
            # Update the symbol to be inactive rather than deleting
            result = await self.trading_symbols.update_one(
                {"symbol": symbol},
                {"$set": {
                    "active": False,
                    "removed_at": datetime.utcnow()
                }}
            )
            
            # Return true if a document was modified
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error removing trading symbol {symbol}: {e}")
            return False

    async def get_trading_symbols(self) -> List[str]:
        """Get list of all active trading symbols"""
        try:
            # Convert the find cursor to a list using to_list
            symbols_docs = await self.trading_symbols.find(
                {"active": True}
            ).to_list(length=None)
            
            # Extract symbol field from each document
            symbols = [doc["symbol"] for doc in symbols_docs]
            return symbols
        except Exception as e:
            logger.error(f"Error retrieving trading symbols: {e}")
            return []

    async def add_removed_symbol(self, symbol: str) -> bool:
        """Add a symbol to the removed symbols list"""
        try:
            # Normalize the symbol
            symbol = symbol.upper().strip()
            
            # Add to the removed symbols collection with timestamp
            await self.removed_symbols.update_one(
                {"symbol": symbol},
                {"$set": {
                    "symbol": symbol,
                    "removed_at": datetime.utcnow(),
                }},
                upsert=True
            )
            
            logger.info(f"Added {symbol} to removed symbols list")
            return True
        except Exception as e:
            logger.error(f"Error adding symbol to removed list: {e}")
            return False

    async def get_removed_symbols(self) -> List[str]:
        """Get all symbols that were intentionally removed"""
        try:
            cursor = self.removed_symbols.find({})
            removed_symbols = []
            
            async for doc in cursor:
                removed_symbols.append(doc["symbol"])
            
            logger.info(f"Found {len(removed_symbols)} removed symbols")
            return removed_symbols
        except Exception as e:
            logger.error(f"Error getting removed symbols: {e}")
            return []
