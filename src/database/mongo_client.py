import logging
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Union, Any
import os
import uuid
from dotenv import load_dotenv

# Import drivers
import motor.motor_asyncio
import pymongo
import pymongo.errors
from pymongo.client_session import ClientSession

from ..types.models import Order, OrderStatus, TimeFrame, OrderType, TradeDirection, TPSLStatus, TakeProfit, StopLoss, PartialTakeProfit  # Add TPSLStatus and related classes
from ..types.constants import TAX_RATE, PRICE_PRECISION
from decimal import ROUND_DOWN, InvalidOperation
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

class MongoClient:
    def __init__(self, uri=None, database_name=None, env_file='.env', driver=None):
        """Initialize MongoDB client"""
        # Load environment variables
        load_dotenv(env_file)
        
        # Override with passed parameters if provided
        self.connection_string = uri or os.getenv('MONGODB_URI', 'mongodb://localhost:27017')
        self.database_name = database_name or os.getenv('MONGODB_DATABASE', 'tradeasaurus')
        
        # Validate and set driver type
        self.driver = self._validate_driver(driver or os.getenv('MONGODB_DRIVER', 'motor'))
        
        self.client = None
        self.db = None
        self.trading_pairs = None
        self.settings = None
        self.thresholds = None
        self.orders = None
        self.balance_history = None
        self.reference_prices = None  
        self.trading_symbols = None
        self.deposits_withdrawals = None  # Collection for deposits and withdrawals
        
        if not self.connection_string:
            raise ValueError("MongoDB connection string not provided")
            
        self.connect()
    
    def connect(self):
        """Connect to MongoDB"""
        try:
            self.client = motor.motor_asyncio.AsyncIOMotorClient(self.connection_string)
            self.db = self.client[self.database_name]
            self.orders = self.db.orders
            self.orders_collection = self.db.orders  # Add this alias
            logger.info(f"MongoClient initialized with Motor driver for {self.database_name}")
            
            # Initialize collections
            self.threshold_state = self.db.threshold_state
            self.triggered_thresholds = self.db.triggered_thresholds
            self.balance_history = self.db.balance_history
            self.reference_prices = self.db.reference_prices
            self.invalid_symbols = self.db.invalid_symbols
            self.trading_symbols = self.db.trading_symbols
            self.removed_symbols = self.db.removed_symbols
            self.trading_config = self.db.trading_config  # New collection for trading config
            self.deposits_withdrawals = self.db.deposits_withdrawals  # Add deposits_withdrawals collection
            
            logger.info(f"Successfully connected to MongoDB at {self.connection_string}")
            
        except Exception as e:
            logger.error(f"Failed to connect to MongoDB: {e}")
            raise
    
    def _validate_driver(self, driver: str) -> str:
        """Validate and normalize the driver selection"""
        driver = driver.lower()
        valid_drivers = ["motor", "pymongo_async", "pymongo"]
        if driver not in valid_drivers:
            logger.warning(f"Invalid driver '{driver}'. Falling back to 'motor'")
            return "motor"
        return driver

    async def init_indexes(self):
        """Create indexes for collections"""
        # Create indexes for trading_symbols collection
        self.trading_symbols.create_index([("symbol", pymongo.ASCENDING)], unique=True)
        
        # Create indexes for orders collection
        self.orders.create_index([("order_id", pymongo.ASCENDING)], unique=True)
        self.orders.create_index([("symbol", pymongo.ASCENDING)])
        self.orders.create_index([("side", pymongo.ASCENDING)])
        self.orders.create_index([("status", pymongo.ASCENDING)])
        self.orders.create_index([("created_at", pymongo.DESCENDING)])
        
        # Create indexes for balance_history collection
        self.balance_history.create_index([("timestamp", pymongo.DESCENDING)])
        
        # Create indexes for threshold_state collection
        self.threshold_state.create_index([
            ("symbol", pymongo.ASCENDING),
            ("timeframe", pymongo.ASCENDING),
            ("type", pymongo.ASCENDING)
        ])
        
        # Create indexes for reference_prices collection
        self.reference_prices.create_index([("symbol", pymongo.ASCENDING)], unique=True)
        
        # Create indexes for deposits_withdrawals collection
        self.deposits_withdrawals.create_index([("timestamp", pymongo.DESCENDING)])
        self.deposits_withdrawals.create_index([("transaction_id", pymongo.ASCENDING)], unique=True)
        self.deposits_withdrawals.create_index([("transaction_type", pymongo.ASCENDING)])
        
        logger.info("MongoDB indexes initialized successfully")
        return True

    def _validate_order_data(self, order: Order) -> bool:
        """Validate order data before insertion"""
        required_fields = {
            'symbol': str,
            'status': OrderStatus,
            'order_type': (OrderType, str),  # Accept both enum and string
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
                if isinstance(expected_type, tuple):
                    if not isinstance(value, expected_type):
                        logger.error(f"Invalid type for {field}: expected one of {expected_type}, got {type(value)}")
                        return False
                else:
                    if not isinstance(value, expected_type):
                        # Special handling for OrderType
                        if field == 'order_type' and isinstance(value, str):
                            try:
                                # Try to convert string to enum
                                order.order_type = OrderType(value)
                                logger.info(f"Converted order_type string '{value}' to enum")
                            except ValueError:
                                logger.error(f"Invalid order_type string: {value}")
                                return False
                        else:
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
            # Ensure order_type is converted to string value if it's an enum
            order_type_value = order.order_type.value if isinstance(order.order_type, OrderType) else str(order.order_type)
            
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
                "order_type": order_type_value,  # Use the converted value
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
                "metadata": {
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
        """Get performance statistics for all filled orders"""
        try:
            pipeline = [
                {"$match": {"status": OrderStatus.FILLED.value}},
                {"$group": {
                    "_id": None,
                    "total_orders": {"$sum": 1},
                    "total_volume": {"$sum": {"$multiply": ["$price", "$quantity"]}},
                    "average_price": {"$avg": "$price"}
                }}
            ]
            
            result = await self._execute_aggregate(self.orders, pipeline)
            
            if result and len(result) > 0:
                return result[0]
            return {"total_orders": 0, "total_volume": 0, "average_price": 0}
        except Exception as e:
            logger.error(f"Error getting performance stats: {e}")
            return {"total_orders": 0, "total_volume": 0, "average_price": 0}

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
                        "$cond": [
                            {"$eq": ["$total_quantity", 0]},
                            0,
                            {"$divide": ["$total_cost", "$total_quantity"]}
                        ]
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
        """Convert a MongoDB document to an Order object"""
        try:
            # Handle order_type conversion
            order_type_str = doc.get("order_type")
            try:
                # Try to convert to enum
                order_type = OrderType(order_type_str)
            except (ValueError, TypeError):
                # Fallback to default if conversion fails
                logger.warning(f"Invalid order_type '{order_type_str}', defaulting to 'spot'")
                order_type = OrderType.SPOT

            # Create the base Order object
            order = Order(
                symbol=doc["symbol"],
                status=OrderStatus(doc["status"]),
                order_type=order_type,  # Use the converted order_type
                price=Decimal(str(doc["price"])),
                quantity=Decimal(str(doc["quantity"])),
                timeframe=TimeFrame(doc["timeframe"]),
                order_id=doc["order_id"],
                created_at=doc["created_at"],
                updated_at=doc["updated_at"],
                leverage=doc.get("leverage"),
                direction=TradeDirection(doc["direction"]) if doc.get("direction") else None,
                filled_at=doc.get("filled_at"),
                cancelled_at=doc.get("cancelled_at"),
                fees=Decimal(str(doc.get("fees", 0))),
                fee_asset=doc.get("fee_asset"),
                threshold=doc.get("threshold"),
                is_manual=doc.get("is_manual", False)
            )

            # Process take profit if exists
            if "take_profit" in doc and doc["take_profit"]:
                tp_data = doc["take_profit"]
                order.take_profit = TakeProfit(
                    price=Decimal(str(tp_data["price"])),
                    percentage=float(tp_data["percentage"]),
                    status=TPSLStatus(tp_data["status"]),
                    triggered_at=tp_data.get("triggered_at"),
                    order_id=tp_data.get("order_id")
                )

            # Process stop loss if exists
            if "stop_loss" in doc and doc["stop_loss"]:
                sl_data = doc["stop_loss"]
                order.stop_loss = StopLoss(
                    price=Decimal(str(sl_data["price"])),
                    percentage=float(sl_data["percentage"]),
                    status=TPSLStatus(sl_data["status"]),
                    triggered_at=sl_data.get("triggered_at"),
                    order_id=sl_data.get("order_id")
                )

            # Process partial take profits if exist
            if "partial_take_profits" in doc and doc["partial_take_profits"]:
                for tp_data in doc["partial_take_profits"]:
                    partial_tp = PartialTakeProfit(
                        level=int(tp_data["level"]),
                        price=Decimal(str(tp_data["price"])),
                        profit_percentage=float(tp_data["profit_percentage"]),
                        position_percentage=float(tp_data["position_percentage"]),
                        status=TPSLStatus(tp_data["status"]),
                        triggered_at=tp_data.get("triggered_at"),
                        order_id=tp_data.get("order_id")
                    )
                    order.partial_take_profits.append(partial_tp)

            return order
        except Exception as e:
            logger.error(f"Error converting document to Order: {e}")
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

    async def record_balance(self, timestamp=None, balance=None, invested=None, fees=None):
        """Record balance snapshot for historical tracking"""
        timestamp = timestamp or datetime.now()
        
        # Get net deposits since the last snapshot
        net_deposits = await self._get_net_deposits_since_last_snapshot(timestamp)
        
        document = {
            "timestamp": timestamp,
            "balance": str(balance) if balance is not None else "0",
            "invested": str(invested) if invested is not None else "0",
            "fees": str(fees) if fees is not None else "0",
            "net_deposits": str(net_deposits) if net_deposits is not None else "0"
        }

        try:
            await self.balance_history.insert_one(document)
            return True
        except Exception as e:
            logger.error(f"Error recording balance: {e}")
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
                    "fees": Decimal(doc["fees"]) if doc.get("fees") else Decimal('0'),
                    "net_deposits": Decimal(doc.get("net_deposits", "0"))
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

    async def get_first_trade_date(self) -> Optional[datetime]:
        """Get the date of the first trade"""
        try:
            # Find the earliest filled order
            pipeline = [
                {"$match": {"status": "filled"}},
                {"$sort": {"filled_at": 1}},
                {"$limit": 1},
                {"$project": {"filled_at": 1}}
            ]
            
            result = await self._execute_aggregate(self.orders, pipeline)
            if result and len(result) > 0:
                first_date = result[0]['filled_at']
                logger.info(f"Found first trade date: {first_date}")
                return first_date
            
            logger.info("No filled trades found in database")
            return None
        except Exception as e:
            logger.error(f"Error getting first trade date: {e}")
            return None

    async def get_portfolio_performance(self, start_date: Optional[datetime] = None) -> Dict:
        """Calculate portfolio performance (ROI) over time based on order history and current prices"""
        try:
            # Get the start date (either provided or from first trade)
            if not start_date:
                start_date = await self.get_first_trade_date()
                if not start_date:
                    logger.warning("No trade history found")
                    return {}

            # Create a date range from start_date to today
            end_date = datetime.utcnow()
            date_range = pd.date_range(start=start_date.date(), end=end_date.date(), freq='D')
            result = {}

            # Get all filled orders since start date
            cursor = self.orders.find({
                "status": "filled",
                "filled_at": {"$gte": start_date}
            }).sort("filled_at", 1)

            # Process orders and calculate daily ROI
            orders = []
            async for doc in cursor:
                orders.append({
                    'date': doc['filled_at'].date(),
                    'symbol': doc['symbol'],
                    'price': Decimal(str(doc['price'])),
                    'quantity': Decimal(str(doc['quantity'])),
                    'fees': Decimal(str(doc.get('fees', 0)))
                })

            if not orders:
                logger.warning("No filled orders found for ROI calculation")
                return {}

            # Initialize tracking variables
            portfolio = {}  # {symbol: {quantity, avg_price, total_cost}}
            total_investment = Decimal('0')
            portfolio_value = Decimal('0')

            # Get current prices for all symbols in portfolio
            unique_symbols = set(order['symbol'] for order in orders)
            current_prices = {}
            for symbol in unique_symbols:
                try:
                    price = await self.binance_client.get_current_price(symbol)
                    if price:
                        current_prices[symbol] = Decimal(str(price))
                except Exception as e:
                    logger.error(f"Error getting current price for {symbol}: {e}")

            # Calculate daily ROI
            for date in date_range:
                date_str = date.strftime('%Y-%m-%d')
                
                # Process orders for this day
                day_orders = [order for order in orders if order['date'] == date.date()]
                
                # Update portfolio with day's orders
                for order in day_orders:
                    symbol = order['symbol']
                    if symbol not in portfolio:
                        portfolio[symbol] = {
                            'quantity': Decimal('0'),
                            'total_cost': Decimal('0'),
                            'avg_price': Decimal('0')
                        }
                    
                    # Update position
                    prev_quantity = portfolio[symbol]['quantity']
                    new_quantity = prev_quantity + order['quantity']
                    new_cost = portfolio[symbol]['total_cost'] + (order['price'] * order['quantity'])
                    
                    portfolio[symbol].update({
                        'quantity': new_quantity,
                        'total_cost': new_cost,
                        'avg_price': new_cost / new_quantity if new_quantity > 0 else Decimal('0')
                    })
                    
                    total_investment += (order['price'] * order['quantity'])

                # Calculate portfolio value for this day using current prices for held positions
                if total_investment > 0:
                    day_portfolio_value = Decimal('0')
                    for symbol, position in portfolio.items():
                        if position['quantity'] > 0:
                            # Use current price for valuation if available, otherwise use average price
                            current_price = current_prices.get(symbol, position['avg_price'])
                            position_value = position['quantity'] * current_price
                            day_portfolio_value += position_value

                    # Calculate ROI percentage
                    roi_percentage = ((day_portfolio_value - total_investment) / total_investment) * 100
                    result[date_str] = float(roi_percentage)

            logger.info(f"Generated ROI data from {start_date.date()} to {end_date.date()} with {len(result)} data points")
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
                                sl_triggered_at: Optional[datetime] = None,
                                partial_tp_updates: Optional[List[Dict]] = None) -> bool:
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
            
            # Handle partial take profit updates
            if partial_tp_updates:
                for update in partial_tp_updates:
                    level = update.get('level')
                    status = update.get('status')
                    triggered_at = update.get('triggered_at')
                    
                    if level is not None and status:
                        # Find the index of the partial TP with this level
                        # We need to use positional $ operator which requires a separate query
                        partial_tp_query = {"order_id": order_id, f"partial_take_profits.level": level}
                        partial_tp_update = {f"partial_take_profits.$.status": status.value}
                        
                        if triggered_at:
                            partial_tp_update[f"partial_take_profits.$.triggered_at"] = triggered_at
                        
                        await self.orders.update_one(
                            partial_tp_query,
                            {"$set": partial_tp_update}
                        )
            
            # Update regular TP/SL
            result = await self.orders.update_one(
                {"order_id": order_id},
                {"$set": update_dict}
            )
            
            return result.modified_count > 0
            
        except Exception as e:
            logger.error(f"Error updating TP/SL status for order {order_id}: {e}")
            return False

    async def get_orders_with_active_tp_sl(self) -> List[Order]:
        """Get all orders with active (pending) TP/SL or partial TP settings"""
        try:
            # Find orders that are filled and have either pending TP, SL, or partial TP
            query = {
                "status": OrderStatus.FILLED.value,
                "$or": [
                    {"take_profit.status": TPSLStatus.PENDING.value},
                    {"stop_loss.status": TPSLStatus.PENDING.value},
                    {"partial_take_profits": {"$elemMatch": {"status": TPSLStatus.PENDING.value}}}
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
            
            logger.info(f"Found {len(orders)} orders with active TP/SL/partial TP")
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
        """Add a new trading symbol"""
        try:
            # Prepare the update operation
            update_data = {
                "$set": {
                    "symbol": symbol,
                    "active": True,
                    "updated_at": datetime.utcnow()
                },
                "$setOnInsert": {
                    "created_at": datetime.utcnow()
                }
            }
            
            if self.driver in ["motor", "pymongo_async"]:
                # Async update for Motor or PyMongo async
                result = await self.trading_symbols.update_one(
                    {"symbol": symbol},
                    update_data,
                    upsert=True
                )
                success = result.modified_count > 0 or result.upserted_id is not None
            else:
                # Sync update for PyMongo
                result = self.trading_symbols.update_one(
                    {"symbol": symbol},
                    update_data,
                    upsert=True
                )
                success = result.modified_count > 0 or result.upserted_id is not None
                
            return success
        except Exception as e:
            logger.error(f"Error saving trading symbol {symbol}: {e}")
            return False

    async def remove_trading_symbol(self, symbol: str) -> bool:
        """Remove a trading symbol by marking it as inactive"""
        try:
            if self.driver in ["motor", "pymongo_async"]:
                # Async update for Motor or PyMongo async
                result = await self.trading_symbols.update_one(
                    {"symbol": symbol},
                    {"$set": {"active": False, "updated_at": datetime.utcnow()}}
                )
                success = result.modified_count > 0
            else:
                # Sync update for PyMongo
                result = self.trading_symbols.update_one(
                    {"symbol": symbol},
                    {"$set": {"active": False, "updated_at": datetime.utcnow()}}
                )
                success = result.modified_count > 0
                
            return success
        except Exception as e:
            logger.error(f"Error removing trading symbol {symbol}: {e}")
            return False

    async def get_trading_symbols(self) -> List[str]:
        """Get list of all active trading symbols"""
        try:
            documents = await self._execute_find(
                self.trading_symbols,
                {"active": True}
            )
            return [doc["symbol"] for doc in documents]
        except Exception as e:
            logger.error(f"Error getting trading symbols: {e}")
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

    async def _execute_find(self, collection, query: dict, **kwargs):
        """Execute find operation with proper driver handling"""
        try:
            if self.driver in ["motor", "pymongo_async"]:
                cursor = collection.find(query, **kwargs)
                return await cursor.to_list(None)
            else:
                return list(collection.find(query, **kwargs))
        except Exception as e:
            logger.error(f"Error executing find: {e}")
            return []

    async def _execute_aggregate(self, collection, pipeline: list, **kwargs):
        """Execute aggregation with proper driver handling"""
        try:
            if self.driver in ["motor", "pymongo_async"]:
                cursor = collection.aggregate(pipeline, **kwargs)
                return await cursor.to_list(None)
            else:
                return list(collection.aggregate(pipeline, **kwargs))
        except Exception as e:
            logger.error(f"Error executing aggregate: {e}")
            return []

    async def _execute_update_one(self, collection, filter_dict: dict, update_dict: dict, **kwargs):
        """Execute update_one with proper driver handling"""
        try:
            if self.driver in ["motor", "pymongo_async"]:
                result = await collection.update_one(filter_dict, update_dict, **kwargs)
            else:
                result = collection.update_one(filter_dict, update_dict, **kwargs)
            return result
        except Exception as e:
            logger.error(f"Error executing update_one: {e}")
            return None
            
    async def save_trading_config(self, config: dict) -> bool:
        """Save trading configuration to database"""
        try:
            # We only save the trading section of the config
            if 'trading' not in config:
                logger.error("No trading section in config")
                return False
                
            # Extract trading settings
            trading_config = config['trading']
            
            # Save each setting as a separate document for easier updates
            for key, value in trading_config.items():
                # Skip complex settings like thresholds and pairs which are managed separately
                if key in ['thresholds', 'pairs']:
                    continue
                    
                # Format for storage
                update_data = {
                    "$set": {
                        "key": key,
                        "value": value,
                        "updated_at": datetime.utcnow()
                    },
                    "$setOnInsert": {
                        "created_at": datetime.utcnow()
                    }
                }
                
                # Save to database
                await self.trading_config.update_one(
                    {"key": key},
                    update_data,
                    upsert=True
                )
            
            # Handle trading pairs - save to trading_symbols collection
            if 'pairs' in trading_config and trading_config['pairs']:
                # Check if trading_symbols collection is empty
                symbols_count = await self.trading_symbols.count_documents({})
                
                # Only import from config if no symbols exist in the database yet
                if symbols_count == 0:
                    logger.info(f"No trading symbols found in database, importing {len(trading_config['pairs'])} symbols from config")
                    for symbol in trading_config['pairs']:
                        await self.save_trading_symbol(symbol)
                else:
                    logger.info(f"Trading symbols already exist in database, skipping import from config")
                
            logger.info(f"Saved trading configuration to database")
            return True
        except Exception as e:
            logger.error(f"Error saving trading config: {e}")
            return False
            
    async def load_trading_config(self) -> Optional[dict]:
        """Load trading configuration from database"""
        try:
            # Get all config entries
            cursor = self.trading_config.find({})
            config = {}
            
            async for doc in cursor:
                key = doc["key"]
                value = doc["value"]
                config[key] = value
                
            if config:
                logger.info(f"Loaded trading configuration from database with {len(config)} settings")
                return config
            else:
                logger.info("No trading configuration found in database")
                return None
        except Exception as e:
            logger.error(f"Error loading trading config: {e}")
            return None
            
    async def update_trading_setting(self, setting: str, value: Any) -> bool:
        """Update a specific trading setting"""
        try:
            update_data = {
                "$set": {
                    "key": setting,
                    "value": value,
                    "updated_at": datetime.utcnow()
                },
                "$setOnInsert": {
                    "created_at": datetime.utcnow()
                }
            }
            
            result = await self.trading_config.update_one(
                {"key": setting},
                update_data,
                upsert=True
            )
            
            logger.info(f"Updated trading setting '{setting}' to '{value}'")
            return True
        except Exception as e:
            logger.error(f"Error updating trading setting: {e}")
            return False

    async def get_active_orders(self) -> List[Order]:
        """Get all orders with FILLED status for TP/SL monitoring"""
        try:
            cursor = self.orders_collection.find({"status": OrderStatus.FILLED.value})
            documents = await cursor.to_list(length=100)  # Limit to 100 active orders
            
            orders = []
            for doc in documents:
                order = self._document_to_order(doc)
                if order:
                    orders.append(order)
                    
            return orders
        except Exception as e:
            logger.error(f"Error retrieving active orders: {e}")
            return []

    async def record_deposit(self, amount, timestamp=None, transaction_id=None, notes=None):
        """Record a deposit to the account"""
        if not amount or amount <= 0:
            logger.error("Invalid deposit amount")
            return False
            
        timestamp = timestamp or datetime.now()
        transaction_id = transaction_id or str(uuid.uuid4())
        
        document = {
            "timestamp": timestamp,
            "transaction_id": transaction_id,
            "transaction_type": "deposit",
            "amount": str(amount),
            "notes": notes
        }
        
        try:
            await self.deposits_withdrawals.insert_one(document)
            logger.info(f"Deposit of {amount} recorded with ID {transaction_id}")
            return True
        except Exception as e:
            logger.error(f"Error recording deposit: {e}")
            return False
            
    async def record_withdrawal(self, amount, timestamp=None, transaction_id=None, notes=None):
        """Record a withdrawal from the account (stored as negative amount)"""
        if not amount or amount <= 0:
            logger.error("Invalid withdrawal amount")
            return False
            
        timestamp = timestamp or datetime.now()
        transaction_id = transaction_id or str(uuid.uuid4())
        
        # Store withdrawal as negative amount
        document = {
            "timestamp": timestamp,
            "transaction_id": transaction_id,
            "transaction_type": "withdrawal",
            "amount": str(-amount),  # Negative value for withdrawals
            "notes": notes
        }
        
        try:
            await self.deposits_withdrawals.insert_one(document)
            logger.info(f"Withdrawal of {amount} recorded with ID {transaction_id}")
            return True
        except Exception as e:
            logger.error(f"Error recording withdrawal: {e}")
            return False
            
    async def get_deposits_withdrawals(self, days=30):
        """Get all deposits and withdrawals for a specified number of days"""
        cutoff_date = datetime.now() - timedelta(days=days)
        
        try:
            cursor = self.deposits_withdrawals.find(
                {"timestamp": {"$gte": cutoff_date}}
            ).sort("timestamp", pymongo.DESCENDING)
            
            transactions = []
            async for doc in cursor:
                # Convert string amounts to Decimal
                doc["amount"] = Decimal(doc["amount"])
                transactions.append(doc)
                
            return transactions
        except Exception as e:
            logger.error(f"Error getting deposits/withdrawals: {e}")
            return []
            
    async def get_net_deposits(self, days=30):
        """Calculate net deposits over a specified period"""
        transactions = await self.get_deposits_withdrawals(days)
        
        if not transactions:
            return Decimal("0")
            
        # Sum all transaction amounts (withdrawals are already stored as negative)
        net_deposits = sum(t["amount"] for t in transactions)
        return net_deposits
        
    async def _get_net_deposits_since_last_snapshot(self, current_timestamp):
        """Calculate net deposits since the last balance snapshot"""
        # Get timestamp of last balance record
        last_record = await self.balance_history.find_one(
            {"timestamp": {"$lt": current_timestamp}},
            sort=[("timestamp", pymongo.DESCENDING)]
        )
        
        if not last_record:
            # If no previous record, get all deposits/withdrawals
            return await self.get_net_deposits(days=36500)  # ~100 years
            
        # Get deposits/withdrawals since last record
        try:
            cursor = self.deposits_withdrawals.find(
                {"timestamp": {"$gt": last_record["timestamp"], "$lte": current_timestamp}}
            )
            
            net_deposits = Decimal("0")
            async for doc in cursor:
                net_deposits += Decimal(doc["amount"])
                
            return net_deposits
        except Exception as e:
            logger.error(f"Error calculating net deposits: {e}")
            return Decimal("0")
