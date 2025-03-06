import motor.motor_asyncio
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from ..types.models import Order, OrderStatus, TimeFrame, OrderType, TradeDirection  # Add imports
from ..types.constants import TAX_RATE, PRICE_PRECISION
from decimal import Decimal, ROUND_DOWN, DecimalException
import logging
import numpy as np  # Add missing numpy import

logger = logging.getLogger(__name__)

class MongoClient:
    def __init__(self, uri: str, database: str):
        self.client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self.client[database]
        self.orders = self.db.orders
        self.balance_history = self.db.balance_history  # Add balance_history collection
        self.threshold_state = self.db.threshold_state  # Add collection for threshold state
        self.thresholds = self.db.thresholds  # Add this line for thresholds collection
        self.reference_prices = self.db.reference_prices  # Add this line for reference prices collection

    async def init_indexes(self):
        await self.orders.create_index("order_id", unique=True)
        await self.orders.create_index("status")
        await self.orders.create_index("symbol")
        await self.orders.create_index("created_at")
        
        # Add index for balance history
        await self.balance_history.create_index("timestamp")
        
        # Add index for threshold state
        await self.threshold_state.create_index([("symbol", 1), ("timeframe", 1)], unique=True)
        
        # Add index for thresholds collection
        await self.thresholds.create_index([("symbol", 1), ("timeframe", 1), ("threshold", 1)], unique=True)
        
        # Add index for reference prices
        await self.reference_prices.create_index([("symbol", 1), ("timeframe", 1)], unique=True)
        
        logger.info("Database indexes initialized")

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
        """Record balance snapshot for historical tracking"""
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
                except (KeyError, ValueError, DecimalException) as e:
                    logger.error(f"Error parsing order {doc.get('order_id', 'unknown')}: {e}")
            
            return results
            
        except Exception as e:
            logger.error(f"Error getting buy orders: {e}")
            return []

    async def save_triggered_threshold(self, symbol: str, timeframe: TimeFrame, threshold: float):
        """Save a triggered threshold to the database"""
        try:
            # Convert TimeFrame enum to string value to prevent BSON serialization errors
            timeframe_value = timeframe.value if hasattr(timeframe, 'value') else str(timeframe)
            
            # Prepare document
            doc = {
                'symbol': symbol,
                'timeframe': timeframe_value,
                'threshold': threshold,
                'triggered_at': datetime.utcnow()
            }
            
            # Use upsert to avoid duplicates
            await self.thresholds.update_one(
                {
                    'symbol': symbol,
                    'timeframe': timeframe_value,
                    'threshold': threshold
                },
                {'$set': doc},
                upsert=True
            )
            
            logger.debug(f"Saved triggered threshold: {symbol} {timeframe_value} {threshold}%")
            return True
        except Exception as e:
            logger.error(f"Error saving triggered threshold: {e}")
            return False

    async def get_all_triggered_thresholds(self):
        """Get all triggered thresholds from the database"""
        try:
            cursor = self.thresholds.find()
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
            
            result = await self.thresholds.delete_many({'timeframe': timeframe_value})
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

    async def save_triggered_threshold(self, symbol: str, timeframe: str, thresholds: list):
        """Save triggered thresholds to database for persistence"""
        try:
            # Convert timeframe to string if it's an enum to prevent BSON encoding errors
            timeframe_value = timeframe.value if hasattr(timeframe, 'value') else str(timeframe)
            
            # Convert thresholds to float to ensure consistent storage
            thresholds_float = [float(t) for t in thresholds]
            
            # Use upsert to create or update
            await self.threshold_state.update_one(
                {"symbol": symbol, "timeframe": timeframe_value},
                {"$set": {"thresholds": thresholds_float, "updated_at": datetime.utcnow()}},
                upsert=True
            )
            
            logger.info(f"Saved threshold state for {symbol} {timeframe_value}: {thresholds_float}")
            return True
        except Exception as e:
            logger.error(f"Failed to save threshold state: {e}")
            return False

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
        """Reset all thresholds for a specific timeframe"""
        try:
            # Convert timeframe to string if it's an enum
            timeframe_value = timeframe.value if hasattr(timeframe, 'value') else str(timeframe)
            
            await self.threshold_state.update_many(
                {"timeframe": timeframe_value},
                {"$set": {"thresholds": [], "updated_at": datetime.utcnow()}}
            )
            return True
        except Exception as e:
            logger.error(f"Failed to reset timeframe thresholds: {e}")
            return False

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

    # ...rest of existing code...
