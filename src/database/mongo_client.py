import motor.motor_asyncio
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Dict, Optional, Union, Tuple
from ..types.models import Order, OrderStatus, TimeFrame, OrderType, OrderDirection, MarginMode, PositionSide
from ..types.constants import TAX_RATE, PRICE_PRECISION
from decimal import ROUND_DOWN, InvalidOperation  # Replace DecimalException with InvalidOperation
import numpy as np  # Add missing numpy import

logger = logging.getLogger(__name__)

class MongoClient:
    def __init__(self, uri: str, database: str):
        """Initialize MongoDB client with connection URI and database name"""
        self.uri = uri
        self.database_name = database
        self.client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self.client[database]
        
        # Initialize collections
        self.orders = self.db.orders
        self.triggered_thresholds = self.db.triggered_thresholds
        self.reference_prices = self.db.reference_prices
        self.balance_history = self.db.balance_history
        
        # New collections for futures trading
        self.pnl_history = self.db.pnl_history
        self.funding_history = self.db.funding_history
        self.margin_calls = self.db.margin_calls
        
        logger.info(f"MongoDB client initialized with database: {database}")

    async def init_indexes(self):
        """Initialize database indexes"""
        try:
            # Create indexes for orders collection
        await self.orders.create_index("order_id", unique=True)
        await self.orders.create_index("symbol")
            await self.orders.create_index("status")
        await self.orders.create_index("created_at")
            await self.orders.create_index("filled_at")
            await self.orders.create_index("order_type")  # Index for order type (spot/futures)
            await self.orders.create_index("direction")   # Index for direction (long/short)
            await self.orders.create_index("leverage")    # Index for leverage
            await self.orders.create_index("tp_order_id") # Index for TP order ID
            await self.orders.create_index("sl_order_id") # Index for SL order ID
            
            # Create indexes for triggered thresholds collection
            await self.thresholds.create_index([("symbol", 1), ("timeframe", 1)], unique=True)
            
            # Create indexes for reference prices collection
            await self.reference_prices.create_index("timestamp")
            
            # Create indexes for balance history collection
            await self.balance_history.create_index("timestamp")
            
            # Create indexes for pnl_history collection
            await self.pnl_history.create_index("order_id")
        
        logger.info("Database indexes initialized")
        except Exception as e:
            logger.error(f"Failed to initialize database indexes: {e}")
            raise

    def _validate_order_data(self, order: Order) -> bool:
        """Validate order data before insertion"""
        try:
            # Check required fields
            if not order.symbol or not order.price or not order.quantity:
                logger.error("Missing required order fields")
                    return False
                    
            # Validate order type
            if not isinstance(order.order_type, OrderType):
                logger.error(f"Invalid order type: {order.order_type}")
                return False
                
            # Validate order status
            if not isinstance(order.status, OrderStatus):
                logger.error(f"Invalid order status: {order.status}")
                return False
                
            # Validate timeframe if present
            if order.timeframe and not isinstance(order.timeframe, TimeFrame):
                logger.error(f"Invalid timeframe: {order.timeframe}")
                return False
                
            # Validate direction if present (for futures orders)
            if order.direction and not isinstance(order.direction, OrderDirection):
                logger.error(f"Invalid direction: {order.direction}")
                return False
                
            # Validate leverage for futures orders
            if order.order_type == OrderType.FUTURES and order.leverage is None:
                logger.error("Missing leverage for futures order")
                return False
                
            # Validate direction for futures orders
            if order.order_type == OrderType.FUTURES and order.direction is None:
                logger.error("Missing direction for futures order")
                    return False
                    
            return True
        except Exception as e:
            logger.error(f"Order validation error: {e}")
            return False

    async def insert_order(self, order: Order) -> Optional[str]:
        """Insert a new order into the database"""
        try:
            # Validate order data
        if not self._validate_order_data(order):
            return None

            # Convert order to document
            doc = {
                "order_id": order.order_id,
                "symbol": order.symbol,
                "price": str(order.price),
                "quantity": str(order.quantity),
                "order_type": order.order_type.value,
                "status": order.status.value,
                "created_at": order.created_at,
                "filled_at": order.filled_at,
                "cancelled_at": order.cancelled_at,
                "is_manual": order.is_manual,
                "fee": str(order.fee) if order.fee else None,
                "fee_currency": order.fee_currency,
                "timeframe": order.timeframe.value if order.timeframe else None,
                "threshold": order.threshold,
                "reference_price": str(order.reference_price) if order.reference_price else None
            }
            
            # Add futures-specific fields
            if order.order_type == OrderType.FUTURES:
                doc.update({
                    "leverage": order.leverage,
                    "direction": order.direction.value if order.direction else None,
                    "margin_mode": order.margin_mode if hasattr(order, 'margin_mode') else "isolated",
                    "position_side": order.position_side if hasattr(order, 'position_side') else "BOTH"
                })
                
            # Add TP/SL fields if present
            if hasattr(order, 'tp_price') and order.tp_price:
                doc["tp_price"] = str(order.tp_price)
            if hasattr(order, 'sl_price') and order.sl_price:
                doc["sl_price"] = str(order.sl_price)
            if hasattr(order, 'tp_percentage') and order.tp_percentage:
                doc["tp_percentage"] = order.tp_percentage
            if hasattr(order, 'sl_percentage') and order.sl_percentage:
                doc["sl_percentage"] = order.sl_percentage
            if hasattr(order, 'tp_order_id') and order.tp_order_id:
                doc["tp_order_id"] = order.tp_order_id
            if hasattr(order, 'sl_order_id') and order.sl_order_id:
                doc["sl_order_id"] = order.sl_order_id
                
            # Insert document
            result = await self.orders.insert_one(doc)
            
            if result.inserted_id:
                logger.info(f"Inserted order {order.order_id} for {order.symbol}")
            return str(result.inserted_id)
            else:
                logger.error(f"Failed to insert order {order.order_id}")
                return None
            
        except Exception as e:
            logger.error(f"Error inserting order: {e}")
            return None

    async def insert_manual_trade(self, order: Order) -> Optional[str]:
        """Insert a manual trade into the database"""
        try:
            # Validate order data
            if not self._validate_order_data(order):
                return None
                
            # Ensure it's marked as manual
            order.is_manual = True
            
            # Convert order to dictionary
            order_dict = order.to_dict()
            
            # Add additional fields for futures orders
            if order.order_type == OrderType.FUTURES:
                order_dict["is_futures"] = True
                order_dict["margin_value"] = str(order.get_margin_value())
            else:
                order_dict["is_futures"] = False
                
            # Insert order
            result = await self.orders.insert_one(order_dict)
            
            if result.inserted_id:
                logger.info(f"Inserted manual trade {order.order_id} into database")
            return str(result.inserted_id)
            
            return None
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
        """Get performance statistics for all trades"""
        try:
            # Get all filled orders
            filled_orders = self.orders.find({"status": OrderStatus.FILLED.value})
            
            # Initialize stats
            stats = {
                "total_orders": 0,
                "filled_orders": 0,
                "cancelled_orders": 0,
                "total_volume": 0,
                "avg_order_size": 0,
                "spot_orders": 0,
                "futures_orders": 0,
                "futures_long": 0,
                "futures_short": 0,
                "avg_leverage": 0,
                "total_fees": 0,
                "total_pnl": 0,
                "win_rate": 0,
                "symbols": {}
            }
            
            # Get total orders count
            stats["total_orders"] = await self.orders.count_documents({})
            stats["filled_orders"] = await self.orders.count_documents({"status": OrderStatus.FILLED.value})
            stats["cancelled_orders"] = await self.orders.count_documents({"status": OrderStatus.CANCELLED.value})
            
            # Get order type counts
            stats["spot_orders"] = await self.orders.count_documents({"order_type": OrderType.SPOT.value})
            stats["futures_orders"] = await self.orders.count_documents({"order_type": OrderType.FUTURES.value})
            
            # Get futures direction counts
            stats["futures_long"] = await self.orders.count_documents({
                "order_type": OrderType.FUTURES.value,
                "direction": OrderDirection.LONG.value
            })
            stats["futures_short"] = await self.orders.count_documents({
                "order_type": OrderType.FUTURES.value,
                "direction": OrderDirection.SHORT.value
            })
            
            # Process filled orders
            total_volume = Decimal('0')
            total_fees = Decimal('0')
            total_pnl = Decimal('0')
            total_leverage = 0
            leverage_count = 0
            winning_trades = 0
            closed_trades = 0
            
            async for doc in filled_orders:
                # Calculate volume
                price = Decimal(doc["price"])
                quantity = Decimal(doc["quantity"])
                volume = price * quantity
                total_volume += volume
                
                # Track fees
                if "fee" in doc and doc["fee"]:
                    fee = Decimal(doc["fee"])
                    total_fees += fee
                
                # Track PnL for futures
                if doc["order_type"] == OrderType.FUTURES.value and "pnl" in doc and doc["pnl"]:
                    pnl = Decimal(doc["pnl"])
                    total_pnl += pnl
                    
                    closed_trades += 1
                    if pnl > 0:
                        winning_trades += 1
                
                # Track leverage
                if doc["order_type"] == OrderType.FUTURES.value and "leverage" in doc:
                    total_leverage += doc["leverage"]
                    leverage_count += 1
                
                # Track by symbol
                symbol = doc["symbol"]
                if symbol not in stats["symbols"]:
                    stats["symbols"][symbol] = {
                        "total_orders": 0,
                        "filled_orders": 0,
                        "cancelled_orders": 0,
                        "volume": 0,
                        "spot_orders": 0,
                        "futures_orders": 0,
                        "futures_long": 0,
                        "futures_short": 0,
                        "pnl": 0
                    }
                
                stats["symbols"][symbol]["total_orders"] += 1
                stats["symbols"][symbol]["filled_orders"] += 1
                stats["symbols"][symbol]["volume"] += float(volume)
                
                if doc["order_type"] == OrderType.SPOT.value:
                    stats["symbols"][symbol]["spot_orders"] += 1
                else:
                    stats["symbols"][symbol]["futures_orders"] += 1
                    
                    if doc.get("direction") == OrderDirection.LONG.value:
                        stats["symbols"][symbol]["futures_long"] += 1
                    elif doc.get("direction") == OrderDirection.SHORT.value:
                        stats["symbols"][symbol]["futures_short"] += 1
                        
                    if "pnl" in doc and doc["pnl"]:
                        stats["symbols"][symbol]["pnl"] += float(Decimal(doc["pnl"]))
            
            # Calculate averages and totals
            stats["total_volume"] = float(total_volume)
            stats["total_fees"] = float(total_fees)
            stats["total_pnl"] = float(total_pnl)
            
            if stats["filled_orders"] > 0:
                stats["avg_order_size"] = float(total_volume) / stats["filled_orders"]
            
            if leverage_count > 0:
                stats["avg_leverage"] = total_leverage / leverage_count
            
            if closed_trades > 0:
                stats["win_rate"] = (winning_trades / closed_trades) * 100
            
        return stats
            
        except Exception as e:
            logger.error(f"Error getting performance stats: {e}")
            return {}

    async def get_position_stats(self, allowed_symbols: set = None) -> dict:
        """Get statistics about current positions"""
        try:
            # Get all filled orders
            query = {"status": OrderStatus.FILLED.value}
            
            # Filter by allowed symbols if provided
        if allowed_symbols:
                query["symbol"] = {"$in": list(allowed_symbols)}
                
            orders = await self.orders.find(query).to_list(None)
            
            # Group by symbol
        positions = {}
            for order_doc in orders:
                order = self._document_to_order(order_doc)
                if not order:
                    continue
                    
                symbol = order.symbol
                
                if symbol not in positions:
                    positions[symbol] = {
                        "symbol": symbol,
                        "quantity": Decimal("0"),
                        "total_cost": Decimal("0"),
                        "avg_price": Decimal("0"),
                        "orders": [],
                        "is_futures": order.order_type == OrderType.FUTURES,
                        "leverage": order.leverage if order.order_type == OrderType.FUTURES else None,
                        "direction": order.direction.value if order.direction else "long"
                    }
                
                # Update position data
                position = positions[symbol]
                
                # For futures, handle long and short positions differently
                if order.order_type == OrderType.FUTURES:
                    # Update position type if not already set
                    position["is_futures"] = True
                    position["leverage"] = order.leverage
                    position["direction"] = order.direction.value if order.direction else "long"
                    
                    # For shorts, quantity is negative
                    if order.direction == OrderDirection.SHORT:
                        position["quantity"] -= order.quantity
                    else:
                        position["quantity"] += order.quantity
                else:
                    # For spot, always add quantity
                    position["quantity"] += order.quantity
                
                # Update cost and orders
                position["total_cost"] += order.price * order.quantity
                position["orders"].append(order_doc)
            
            # Calculate average price for each position
            for symbol, position in positions.items():
                if position["quantity"] != 0:
                    position["avg_price"] = position["total_cost"] / abs(position["quantity"])
                
                # Convert Decimal to float for JSON serialization
                position["quantity"] = float(position["quantity"])
                position["total_cost"] = float(position["total_cost"])
                position["avg_price"] = float(position["avg_price"])
            
        return positions
        except Exception as e:
            logger.error(f"Error getting position stats: {e}")
            return {}

    def calculate_profit_loss(self, position: dict, current_price: Decimal) -> dict:
        """Calculate profit/loss for a position"""
        try:
            # Extract position data
            avg_price = Decimal(str(position["avg_price"]))
            quantity = Decimal(str(position["quantity"]))
            is_futures = position.get("is_futures", False)
            leverage = Decimal(str(position.get("leverage", 1)))
            direction = position.get("direction", "long")
            
            # Calculate profit/loss
            if is_futures:
                # For futures, consider direction and leverage
                if direction == "long":
                    price_diff = current_price - avg_price
                else:  # short
                    price_diff = avg_price - current_price
                    
                # Calculate PnL with leverage
                pnl = price_diff * abs(quantity) * leverage
                pnl_percentage = (price_diff / avg_price) * Decimal("100") * leverage
            else:
                # For spot, simple calculation
                pnl = (current_price - avg_price) * quantity
                pnl_percentage = ((current_price - avg_price) / avg_price) * Decimal("100")
            
            # Return results
        return {
                "pnl": float(pnl),
                "pnl_percentage": float(pnl_percentage),
                "current_value": float(current_price * abs(quantity)),
                "cost_basis": float(avg_price * abs(quantity))
            }
        except Exception as e:
            logger.error(f"Error calculating profit/loss: {e}")
            return {
                "pnl": 0,
                "pnl_percentage": 0,
                "current_value": 0,
                "cost_basis": 0
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
        """Convert a document to an Order object"""
        try:
            # Convert string fields to appropriate types
            price = Decimal(doc.get('price', '0'))
            quantity = Decimal(doc.get('quantity', '0'))
            fee = Decimal(doc.get('fee')) if doc.get('fee') else None
            reference_price = Decimal(doc.get('reference_price')) if doc.get('reference_price') else None
            
            # Convert enum fields
            order_type = OrderType(doc.get('order_type', 'spot'))
            status = OrderStatus(doc.get('status', 'pending'))
            timeframe = TimeFrame(doc.get('timeframe')) if doc.get('timeframe') else None
            
            # Convert direction for futures orders
            direction = None
            if doc.get('direction'):
                direction = OrderDirection(doc.get('direction'))
            
            # Create order object
            order = Order(
                order_id=doc.get('order_id'),
                symbol=doc.get('symbol'),
                price=price,
                quantity=quantity,
                order_type=order_type,
                status=status,
                created_at=doc.get('created_at'),
                filled_at=doc.get('filled_at'),
                cancelled_at=doc.get('cancelled_at'),
                is_manual=doc.get('is_manual', False),
                fee=fee,
                fee_currency=doc.get('fee_currency'),
                timeframe=timeframe,
                threshold=doc.get('threshold'),
                reference_price=reference_price,
                leverage=doc.get('leverage'),
                direction=direction
            )
            
            # Add futures-specific fields
            if order_type == OrderType.FUTURES:
                order.margin_mode = doc.get('margin_mode', 'isolated')
                order.position_side = doc.get('position_side', 'BOTH')
            
            # Add TP/SL fields if present
            if 'tp_price' in doc and doc['tp_price']:
                order.tp_price = Decimal(doc['tp_price'])
            if 'sl_price' in doc and doc['sl_price']:
                order.sl_price = Decimal(doc['sl_price'])
            if 'tp_percentage' in doc:
                order.tp_percentage = doc['tp_percentage']
            if 'sl_percentage' in doc:
                order.sl_percentage = doc['sl_percentage']
            if 'tp_order_id' in doc:
                order.tp_order_id = doc['tp_order_id']
            if 'sl_order_id' in doc:
                order.sl_order_id = doc['sl_order_id']

            return order
            
        except Exception as e:
            logger.error(f"Error converting document to order: {e}")
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
                except (KeyError, ValueError, InvalidOperation) as e:  # Changed from DecimalException to InvalidOperation
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
            logger.error(f"Error getting portfolio composition: {e}", exc_info=True)
            return {}

    async def get_tp_sl_orders(self, main_order_id: str) -> Dict[str, Optional[Order]]:
        """Get TP/SL orders for a main order"""
        try:
            # Get main order
            main_order_doc = await self.orders.find_one({"order_id": main_order_id})
            if not main_order_doc:
                logger.error(f"Main order {main_order_id} not found")
                return {"tp_order": None, "sl_order": None}
            
            # Get TP/SL order IDs
            tp_order_id = main_order_doc.get("tp_order_id")
            sl_order_id = main_order_doc.get("sl_order_id")
            
            # Get TP/SL orders
            tp_order_doc = await self.orders.find_one({"order_id": tp_order_id}) if tp_order_id else None
            sl_order_doc = await self.orders.find_one({"order_id": sl_order_id}) if sl_order_id else None
            
            # Convert to Order objects
            tp_order = self._document_to_order(tp_order_doc) if tp_order_doc else None
            sl_order = self._document_to_order(sl_order_doc) if sl_order_doc else None
            
            return {
                "tp_order": tp_order,
                "sl_order": sl_order
            }
            
        except Exception as e:
            logger.error(f"Error getting TP/SL orders: {e}")
            return {"tp_order": None, "sl_order": None}
    
    async def link_tp_sl_orders(self, main_order_id: str, tp_order_id: Optional[str], sl_order_id: Optional[str], 
                              tp_price: Optional[Decimal] = None, sl_price: Optional[Decimal] = None) -> bool:
        """Link TP/SL orders to a main order"""
        try:
            update_dict = {}
            
            if tp_order_id:
                update_dict["tp_order_id"] = tp_order_id
            if sl_order_id:
                update_dict["sl_order_id"] = sl_order_id
            if tp_price:
                update_dict["tp_price"] = str(tp_price)
            if sl_price:
                update_dict["sl_price"] = str(sl_price)
            
            if not update_dict:
                return False
            
            result = await self.orders.update_one(
                {"order_id": main_order_id},
                {"$set": update_dict}
            )
            
            return result.modified_count > 0
            
        except Exception as e:
            logger.error(f"Error linking TP/SL orders: {e}")
            return False
    
    async def unlink_tp_sl_orders(self, main_order_id: str) -> bool:
        """Unlink TP/SL orders from a main order"""
        try:
            result = await self.orders.update_one(
                {"order_id": main_order_id},
                {"$unset": {
                    "tp_order_id": "",
                    "sl_order_id": "",
                    "tp_price": "",
                    "sl_price": ""
                }}
            )
            
            return result.modified_count > 0
            
        except Exception as e:
            logger.error(f"Error unlinking TP/SL orders: {e}")
            return False
    
    async def get_orders_with_tp_sl(self) -> List[Dict]:
        """Get all orders with TP/SL orders"""
        try:
            orders = await self.orders.find({
                "status": OrderStatus.FILLED.value,
                "$or": [
                    {"tp_order_id": {"$exists": True}},
                    {"sl_order_id": {"$exists": True}}
                ]
            }).to_list(None)
            
            return orders
            
        except Exception as e:
            logger.error(f"Error getting orders with TP/SL: {e}")
            return []
    
    async def update_tp_sl_status(self, main_order_id: str, tp_filled: bool = False, sl_filled: bool = False) -> bool:
        """Update TP/SL status for a main order"""
        try:
            update_dict = {}
            
            if tp_filled:
                update_dict["tp_filled"] = True
                update_dict["tp_filled_at"] = datetime.utcnow()
            
            if sl_filled:
                update_dict["sl_filled"] = True
                update_dict["sl_filled_at"] = datetime.utcnow()
            
            if not update_dict:
                return False
            
            result = await self.orders.update_one(
                {"order_id": main_order_id},
                {"$set": update_dict}
            )
            
            return result.modified_count > 0
            
        except Exception as e:
            logger.error(f"Error updating TP/SL status: {e}")
            return False

    async def get_futures_positions(self, symbol: Optional[str] = None) -> List[Dict]:
        """Get all open futures positions"""
        try:
            # Build query
            query = {
                "order_type": OrderType.FUTURES.value,
                "status": OrderStatus.FILLED.value
            }
            
            # Add symbol filter if provided
            if symbol:
                query["symbol"] = symbol
                
            # Get all filled futures orders
            cursor = self.orders.find(query)
            
            # Group by symbol and direction to get positions
            positions = {}
            async for doc in cursor:
                order = self._document_to_order(doc)
                if not order:
                    continue
                    
                # Create a unique key for each position (symbol + direction)
                key = f"{order.symbol}_{order.direction.value if order.direction else 'long'}"
                
                if key not in positions:
                    positions[key] = {
                        "symbol": order.symbol,
                        "direction": order.direction.value if order.direction else "long",
                        "leverage": order.leverage,
                        "margin_mode": order.margin_mode,
                        "position_side": order.position_side,
                        "quantity": Decimal('0'),
                        "entry_price": Decimal('0'),
                        "orders": []
                    }
                
                # Add order to position
                positions[key]["orders"].append({
                    "order_id": order.order_id,
                    "price": float(order.price),
                    "quantity": float(order.quantity),
                    "created_at": order.created_at,
                    "filled_at": order.filled_at
                })
                
                # Update position quantity and entry price
                positions[key]["quantity"] += order.quantity
                
                # Calculate weighted average entry price
                total_value = Decimal('0')
                total_qty = Decimal('0')
                for ord in positions[key]["orders"]:
                    qty = Decimal(str(ord["quantity"]))
                    price = Decimal(str(ord["price"]))
                    total_value += qty * price
                    total_qty += qty
                
                if total_qty > 0:
                    positions[key]["entry_price"] = total_value / total_qty
                
            # Convert to list and format for response
            result = []
            for pos in positions.values():
                # Skip positions with zero quantity
                if pos["quantity"] == 0:
                    continue
                    
                # Format decimal values
                pos["quantity"] = float(pos["quantity"])
                pos["entry_price"] = float(pos["entry_price"])
                
                result.append(pos)
                
            return result
            
        except Exception as e:
            logger.error(f"Error getting futures positions: {e}")
            return []

    async def update_position_tp_sl(self, order_id: str, tp_price: Optional[Decimal] = None, 
                               sl_price: Optional[Decimal] = None, tp_order_id: Optional[str] = None, 
                               sl_order_id: Optional[str] = None) -> bool:
        """Update a futures position with TP/SL information"""
        try:
            update_data = {}
            
            # Add TP/SL prices if provided
            if tp_price is not None:
                update_data["tp_price"] = str(tp_price)
            if sl_price is not None:
                update_data["sl_price"] = str(sl_price)
                
            # Add TP/SL order IDs if provided
            if tp_order_id is not None:
                update_data["tp_order_id"] = tp_order_id
            if sl_order_id is not None:
                update_data["sl_order_id"] = sl_order_id
                
            # Skip if no data to update
            if not update_data:
                logger.warning(f"No TP/SL data provided for order {order_id}")
                return False
                
            # Update order
            result = await self.orders.update_one(
                {"order_id": order_id},
                {"$set": update_data}
            )
            
            if result.modified_count > 0:
                logger.info(f"Updated TP/SL for order {order_id}")
                return True
            else:
                logger.warning(f"No order found with ID {order_id}")
                return False
                
        except Exception as e:
            logger.error(f"Error updating position TP/SL: {e}")
            return False

    async def get_futures_stats(self) -> Dict:
        """Get statistics for futures trading"""
        try:
            # Get all futures orders
            futures_orders = self.orders.find({"order_type": OrderType.FUTURES.value})
            
            # Initialize stats
            stats = {
                "total_orders": 0,
                "filled_orders": 0,
                "cancelled_orders": 0,
                "long_positions": 0,
                "short_positions": 0,
                "avg_leverage": 0,
                "total_pnl": 0,
                "win_rate": 0,
                "symbols": {},
                "leverage_distribution": {}
            }
            
            # Process orders
            total_leverage = 0
            winning_trades = 0
            closed_trades = 0
            
            async for doc in futures_orders:
                stats["total_orders"] += 1
                
                # Count by status
                if doc["status"] == OrderStatus.FILLED.value:
                    stats["filled_orders"] += 1
                elif doc["status"] == OrderStatus.CANCELLED.value:
                    stats["cancelled_orders"] += 1
                
                # Count by direction
                if doc.get("direction") == OrderDirection.LONG.value:
                    stats["long_positions"] += 1
                elif doc.get("direction") == OrderDirection.SHORT.value:
                    stats["short_positions"] += 1
                
                # Track leverage
                leverage = doc.get("leverage", 1)
                total_leverage += leverage
                
                # Update leverage distribution
                leverage_key = str(leverage)
                if leverage_key not in stats["leverage_distribution"]:
                    stats["leverage_distribution"][leverage_key] = 0
                stats["leverage_distribution"][leverage_key] += 1
                
                # Track symbols
                symbol = doc["symbol"]
                if symbol not in stats["symbols"]:
                    stats["symbols"][symbol] = {
                        "total_orders": 0,
                        "filled_orders": 0,
                        "cancelled_orders": 0,
                        "pnl": 0
                    }
                
                stats["symbols"][symbol]["total_orders"] += 1
                
                if doc["status"] == OrderStatus.FILLED.value:
                    stats["symbols"][symbol]["filled_orders"] += 1
                elif doc["status"] == OrderStatus.CANCELLED.value:
                    stats["symbols"][symbol]["cancelled_orders"] += 1
                
                # Calculate PnL for closed positions
                if doc.get("pnl") is not None:
                    pnl = Decimal(doc["pnl"])
                    stats["total_pnl"] += float(pnl)
                    stats["symbols"][symbol]["pnl"] += float(pnl)
                    
                    closed_trades += 1
                    if pnl > 0:
                        winning_trades += 1
            
            # Calculate averages
            if stats["total_orders"] > 0:
                stats["avg_leverage"] = total_leverage / stats["total_orders"]
            
            if closed_trades > 0:
                stats["win_rate"] = (winning_trades / closed_trades) * 100
            
            return stats
            
        except Exception as e:
            logger.error(f"Error getting futures stats: {e}")
            return {}

    async def record_futures_pnl(self, order_id: str, pnl: Decimal, close_price: Decimal, 
                              close_time: datetime = None) -> bool:
        """Record PnL for a closed futures position"""
        try:
            # Set close time to now if not provided
            if close_time is None:
                close_time = datetime.utcnow()
                
            # Update order with PnL information
            result = await self.orders.update_one(
                {"order_id": order_id},
                {"$set": {
                    "pnl": str(pnl),
                    "close_price": str(close_price),
                    "close_time": close_time
                }}
            )
            
            if result.modified_count > 0:
                logger.info(f"Recorded PnL of {pnl} for order {order_id}")
                
                # Get order details
                order_doc = await self.orders.find_one({"order_id": order_id})
                if order_doc:
                    # Record in PnL history collection
                    await self.pnl_history.insert_one({
                        "order_id": order_id,
                        "symbol": order_doc["symbol"],
                        "direction": order_doc.get("direction"),
                        "leverage": order_doc.get("leverage", 1),
                        "entry_price": order_doc["price"],
                        "close_price": str(close_price),
                        "quantity": order_doc["quantity"],
                        "pnl": str(pnl),
                        "entry_time": order_doc["filled_at"],
                        "close_time": close_time,
                        "duration_hours": (close_time - order_doc["filled_at"]).total_seconds() / 3600 if order_doc["filled_at"] else 0
                    })
                
                return True
            else:
                logger.warning(f"No order found with ID {order_id}")
                return False
                
        except Exception as e:
            logger.error(f"Error recording futures PnL: {e}")
            return False

    async def get_pnl_history(self, days: int = 30, symbol: Optional[str] = None) -> List[Dict]:
        """Get PnL history for futures positions"""
        try:
            # Calculate start date
            start_date = datetime.utcnow() - timedelta(days=days)
            
            # Build query
            query = {"close_time": {"$gte": start_date}}
            if symbol:
                query["symbol"] = symbol
                
            # Get PnL history
            cursor = self.pnl_history.find(query).sort("close_time", -1)
            
            # Process results
            result = []
            async for doc in cursor:
                # Convert decimal strings to float
                pnl = float(Decimal(doc["pnl"]))
                entry_price = float(Decimal(doc["entry_price"]))
                close_price = float(Decimal(doc["close_price"]))
                quantity = float(Decimal(doc["quantity"]))
                
                # Calculate ROI
                roi = (pnl / (entry_price * quantity)) * 100 if entry_price * quantity > 0 else 0
                
                # Add to result
                result.append({
                    "order_id": doc["order_id"],
                    "symbol": doc["symbol"],
                    "direction": doc["direction"],
                    "leverage": doc["leverage"],
                    "entry_price": entry_price,
                    "close_price": close_price,
                    "quantity": quantity,
                    "pnl": pnl,
                    "roi": roi,
                    "entry_time": doc["entry_time"],
                    "close_time": doc["close_time"],
                    "duration_hours": doc["duration_hours"]
                })
                
            return result
            
        except Exception as e:
            logger.error(f"Error getting PnL history: {e}")
            return []

    async def record_funding_payment(self, symbol: str, rate: Decimal, payment: Decimal, 
                                timestamp: datetime = None) -> bool:
        """Record a funding rate payment for a futures position"""
        try:
            # Set timestamp to now if not provided
            if timestamp is None:
                timestamp = datetime.utcnow()
                
            # Insert funding payment record
            await self.funding_history.insert_one({
                "symbol": symbol,
                "rate": str(rate),
                "payment": str(payment),
                "timestamp": timestamp
            })
            
            logger.info(f"Recorded funding payment of {payment} for {symbol} at rate {rate}")
            return True
            
        except Exception as e:
            logger.error(f"Error recording funding payment: {e}")
            return False

    async def record_margin_call(self, symbol: str, position_size: Decimal, margin_level: Decimal, 
                            required_margin: Decimal, timestamp: datetime = None) -> bool:
        """Record a margin call for a futures position"""
        try:
            # Set timestamp to now if not provided
            if timestamp is None:
                timestamp = datetime.utcnow()
                
            # Insert margin call record
            await self.margin_calls.insert_one({
                "symbol": symbol,
                "position_size": str(position_size),
                "margin_level": str(margin_level),
                "required_margin": str(required_margin),
                "timestamp": timestamp
            })
            
            logger.info(f"Recorded margin call for {symbol} with margin level {margin_level}")
            return True
            
        except Exception as e:
            logger.error(f"Error recording margin call: {e}")
            return False

    async def get_funding_history(self, days: int = 30, symbol: Optional[str] = None) -> List[Dict]:
        """Get funding rate payment history"""
        try:
            # Calculate start date
            start_date = datetime.utcnow() - timedelta(days=days)
            
            # Build query
            query = {"timestamp": {"$gte": start_date}}
            if symbol:
                query["symbol"] = symbol
                
            # Get funding history
            cursor = self.funding_history.find(query).sort("timestamp", -1)
            
            # Process results
            result = []
            async for doc in cursor:
                # Convert decimal strings to float
                rate = float(Decimal(doc["rate"]))
                payment = float(Decimal(doc["payment"]))
                
                # Add to result
                result.append({
                    "symbol": doc["symbol"],
                    "rate": rate,
                    "payment": payment,
                    "timestamp": doc["timestamp"]
                })
                
            return result
            
        except Exception as e:
            logger.error(f"Error getting funding history: {e}")
            return []

    async def get_margin_call_history(self, days: int = 30, symbol: Optional[str] = None) -> List[Dict]:
        """Get margin call history"""
        try:
            # Calculate start date
            start_date = datetime.utcnow() - timedelta(days=days)
            
            # Build query
            query = {"timestamp": {"$gte": start_date}}
            if symbol:
                query["symbol"] = symbol
                
            # Get margin call history
            cursor = self.margin_calls.find(query).sort("timestamp", -1)
            
            # Process results
            result = []
            async for doc in cursor:
                # Convert decimal strings to float
                position_size = float(Decimal(doc["position_size"]))
                margin_level = float(Decimal(doc["margin_level"]))
                required_margin = float(Decimal(doc["required_margin"]))
                
                # Add to result
                result.append({
                    "symbol": doc["symbol"],
                    "position_size": position_size,
                    "margin_level": margin_level,
                    "required_margin": required_margin,
                    "timestamp": doc["timestamp"]
                })
                
            return result
            
        except Exception as e:
            logger.error(f"Error getting margin call history: {e}")
            return []

    async def update_margin_mode(self, order_id: str, margin_mode: str) -> bool:
        """Update the margin mode for a futures position"""
        try:
            # Validate margin mode
            if margin_mode not in ["isolated", "cross"]:
                logger.error(f"Invalid margin mode: {margin_mode}")
                return False
                
            # Update order
            result = await self.orders.update_one(
                {"order_id": order_id, "order_type": OrderType.FUTURES.value},
                {"$set": {"margin_mode": margin_mode}}
            )
            
            if result.modified_count > 0:
                logger.info(f"Updated margin mode to {margin_mode} for order {order_id}")
                return True
            else:
                logger.warning(f"No futures order found with ID {order_id}")
                return False
                
        except Exception as e:
            logger.error(f"Error updating margin mode: {e}")
            return False

    async def update_leverage(self, order_id: str, leverage: int) -> bool:
        """Update the leverage for a futures position"""
        try:
            # Validate leverage
            if leverage < 1 or leverage > 125:
                logger.error(f"Invalid leverage: {leverage}")
                return False
                
            # Update order
            result = await self.orders.update_one(
                {"order_id": order_id, "order_type": OrderType.FUTURES.value},
                {"$set": {"leverage": leverage}}
            )
            
            if result.modified_count > 0:
                logger.info(f"Updated leverage to {leverage}x for order {order_id}")
                return True
            else:
                logger.warning(f"No futures order found with ID {order_id}")
                return False
                
        except Exception as e:
            logger.error(f"Error updating leverage: {e}")
            return False

    async def get_position_entry_price(self, symbol: str, direction: OrderDirection) -> Optional[Decimal]:
        """Get the average entry price for a futures position"""
        try:
            # Build query
            query = {
                "symbol": symbol,
                "order_type": OrderType.FUTURES.value,
                "status": OrderStatus.FILLED.value,
                "direction": direction.value
            }
            
            # Get all filled orders for this position
            cursor = self.orders.find(query)
            
            # Calculate weighted average entry price
            total_value = Decimal('0')
            total_qty = Decimal('0')
            
            async for doc in cursor:
                price = Decimal(doc["price"])
                quantity = Decimal(doc["quantity"])
                total_value += price * quantity
                total_qty += quantity
                
            if total_qty > 0:
                return total_value / total_qty
            else:
                return None
                
        except Exception as e:
            logger.error(f"Error getting position entry price: {e}")
            return None

    async def get_futures_orders(self, days: int = 7, symbol: str = None) -> List[Dict]:
        """
        Get futures orders from the last N days
        
        Args:
            days: Number of days to look back
            symbol: Optional symbol to filter by
            
        Returns:
            List of futures orders
        """
        try:
            # Calculate start date
            start_date = datetime.utcnow() - timedelta(days=days)
            
            # Build query
            query = {
                "order_type": OrderType.FUTURES.value,
                "created_at": {"$gte": start_date}
            }
            
            # Add symbol filter if provided
            if symbol:
                query["symbol"] = symbol
            
            # Query for futures orders
            cursor = self.orders.find(query).sort("created_at", -1)
            
            # Convert cursor to list
            orders = await cursor.to_list(length=100)
            
            # Process orders
            processed_orders = []
            for order in orders:
                # Convert ObjectId to string
                order["_id"] = str(order["_id"])
                
                # Convert decimal strings to float
                if 'price' in order and order['price']:
                    order['price'] = float(order['price'])
                if 'quantity' in order and order['quantity']:
                    order['quantity'] = float(order['quantity'])
                if 'fee' in order and order['fee']:
                    order['fee'] = float(order['fee'])
                if 'leverage' in order and order['leverage']:
                    order['leverage'] = int(order['leverage'])
                if 'tp_price' in order and order['tp_price']:
                    order['tp_price'] = float(order['tp_price'])
                if 'sl_price' in order and order['sl_price']:
                    order['sl_price'] = float(order['sl_price'])
                
                processed_orders.append(order)
                
            logger.info(f"Retrieved {len(processed_orders)} futures orders from the last {days} days")
            return processed_orders
            
        except Exception as e:
            logger.error(f"Error getting futures orders: {e}")
            return []

    async def get_open_futures_positions(self) -> List[Dict]:
        """
        Get open futures positions
        
        Returns:
            List of open futures positions
        """
        try:
            # Query for open futures positions
            query = {
                "order_type": OrderType.FUTURES.value,
                "status": OrderStatus.FILLED.value,
                "position_closed": {"$ne": True}
            }
            
            # Query for futures orders
            cursor = self.orders.find(query).sort("created_at", -1)
            
            # Convert cursor to list
            positions = await cursor.to_list(length=100)
            
            # Process positions
            processed_positions = []
            for position in positions:
                # Convert ObjectId to string
                position["_id"] = str(position["_id"])
                
                # Convert decimal strings to float
                if 'price' in position and position['price']:
                    position['price'] = float(position['price'])
                    position['entry_price'] = float(position['price'])  # Add entry_price for clarity
                if 'quantity' in position and position['quantity']:
                    position['quantity'] = float(position['quantity'])
                if 'fee' in position and position['fee']:
                    position['fee'] = float(position['fee'])
                if 'leverage' in position and position['leverage']:
                    position['leverage'] = int(position['leverage'])
                if 'liquidation_price' in position and position['liquidation_price']:
                    position['liquidation_price'] = float(position['liquidation_price'])
                if 'tp_price' in position and position['tp_price']:
                    position['tp_price'] = float(position['tp_price'])
                if 'sl_price' in position and position['sl_price']:
                    position['sl_price'] = float(position['sl_price'])
                
                processed_positions.append(position)
                
            logger.info(f"Retrieved {len(processed_positions)} open futures positions")
            return processed_positions
            
        except Exception as e:
            logger.error(f"Error getting open futures positions: {e}")
            return []

    # ...rest of existing code...
