import motor.motor_asyncio
import asyncio
from datetime import datetime, timezone
import logging
from typing import Optional, Dict, List, Any, Union
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import DuplicateKeyError
import time

class MongoDBHandler:
    def __init__(self, connection_string: str, database_name: str):
        self.client = motor.motor_asyncio.AsyncIOMotorClient(connection_string)
        self.db = self.client[database_name]
        self.logger = logging.getLogger('MongoDB')
        
        # Update collections structure with new indexes
        self.collections = {
            'trades': [
                IndexModel([('trade_id', ASCENDING)], unique=True),
                IndexModel([('symbol', ASCENDING)]),
                IndexModel([('status', ASCENDING)]),
                IndexModel([('created_at', DESCENDING)]),
                IndexModel([('exchange', ASCENDING)])
            ],
            'orders': [
                IndexModel([('order_id', ASCENDING)], unique=True),
                IndexModel([('trade_id', ASCENDING)]),
                IndexModel([('symbol', ASCENDING)]),
                IndexModel([('status', ASCENDING)]),
                IndexModel([('created_at', DESCENDING)])
            ],
            'symbols': [
                IndexModel([('symbol', ASCENDING)], unique=True),
                IndexModel([('exchange', ASCENDING)])
            ],
            'system_settings': [
                IndexModel([('key', ASCENDING)], unique=True)
            ]
        }

        # Add timing decorator for logging
        def log_operation(func):
            async def wrapper(*args, **kwargs):
                start_time = time.time()
                try:
                    result = await func(*args, **kwargs)
                    duration = (time.time() - start_time) * 1000
                    
                    # Log successful operation
                    self.logger.debug(
                        f"{func.__name__} completed",
                        extra={
                            'operation': func.__name__,
                            'collection': kwargs.get('collection', 'N/A'),
                            'query': str(kwargs.get('filter', args[1] if len(args) > 1 else 'N/A')),
                            'duration': duration,
                            'result': 'Success'
                        }
                    )
                    return result
                except Exception as e:
                    duration = (time.time() - start_time) * 1000
                    # Log failed operation
                    self.logger.error(
                        f"{func.__name__} failed: {str(e)}",
                        extra={
                            'operation': func.__name__,
                            'collection': kwargs.get('collection', 'N/A'),
                            'query': str(kwargs.get('filter', args[1] if len(args) > 1 else 'N/A')),
                            'duration': duration,
                            'result': f'Error: {str(e)}'
                        }
                    )
                    raise
            return wrapper

        # Apply decorator to database operations
        self.save_trade = log_operation(self.save_trade)
        self.update_trade = log_operation(self.update_trade)
        self.get_trade_history = log_operation(self.get_trade_history)
        # ...apply to other methods...

    async def initialize(self):
        """Initialize database with new structure"""
        try:
            # Create indexes for each collection
            for collection_name, indexes in self.collections.items():
                # Create collection if it doesn't exist
                if collection_name not in await self.db.list_collection_names():
                    await self.db.create_collection(collection_name)
                # Create or update indexes
                await self.db[collection_name].create_indexes(indexes)

            self.logger.info("Database initialized successfully")
            return True

        except Exception as e:
            self.logger.error(f"Database initialization failed: {e}")
            return False

    async def save_trade(self, trade_data: Dict):
        """Save trade with enhanced validation and error handling"""
        try:
            # Validate required fields
            required_fields = {
                'trade_id': (str,),  # Fix: Use tuple for isinstance check
                'exchange': (str,),
                'trade_info': (dict,)  # Fix: Use tuple for isinstance check
            }

            trade_info_fields = {
                'symbol': (str,),
                'entry_price': (float, int),  # Allow both float and int
                'quantity': (float, int),
                'total_cost': (float, int),
                'status': (str,),
                'type': (str,)
            }

            # Validate trade_data structure
            if not isinstance(trade_data, dict):
                raise ValueError("Trade data must be a dictionary")

            # Check required top-level fields
            for field, field_type in required_fields.items():
                if field not in trade_data:
                    raise ValueError(f"Missing required field: {field}")
                if not isinstance(trade_data[field], field_type):
                    raise ValueError(f"Invalid type for {field}: expected {field_type}")

            # Check trade_info fields
            trade_info = trade_data.get('trade_info', {})
            for field, field_type in trade_info_fields.items():
                if field not in trade_info:
                    raise ValueError(f"Missing required field in trade_info: {field}")
                if not isinstance(trade_info[field], field_type):
                    try:
                        # Convert numeric values
                        if field in ['entry_price', 'quantity', 'total_cost']:
                            trade_info[field] = float(trade_info[field])
                    except (ValueError, TypeError):
                        raise ValueError(f"Invalid type for trade_info.{field}: expected {field_type}")

            # Ensure symbol is uppercase and valid format
            trade_info['symbol'] = trade_info['symbol'].upper()
            if not trade_info['symbol'].endswith('USDT'):
                raise ValueError(f"Invalid symbol format: {trade_info['symbol']}")

            # Prepare trade document
            trade_doc = {
                'trade_id': trade_data['trade_id'],
                'exchange': trade_data['exchange'],
                'symbol': trade_info['symbol'],  # Add symbol at top level for easier querying
                'trade_info': trade_info,
                'created_at': datetime.now(timezone.utc),
                'updated_at': datetime.now(timezone.utc)
            }

            # Add order metadata if present
            if 'order_metadata' in trade_data:
                trade_doc['order_metadata'] = trade_data['order_metadata']

            # Insert with retry logic
            for attempt in range(3):
                try:
                    result = await self.db.trades.insert_one(trade_doc)
                    self.logger.debug(
                        "save_trade completed",
                        extra={
                            'operation': 'save_trade',
                            'collection': 'trades',
                            'query': str(trade_doc['trade_id']),
                            'duration': 0.0,
                            'result': 'Success'
                        }
                    )
                    return result.inserted_id
                except Exception as e:
                    if attempt == 2:  # Last attempt
                        raise
                    await asyncio.sleep(1)

        except Exception as e:
            self.logger.error(
                f"Error saving trade: {str(e)}",
                extra={
                    'operation': 'save_trade',
                    'collection': 'trades',
                    'query': str(trade_data.get('trade_id', 'N/A')),
                    'duration': 0.0,
                    'result': f'Error: {str(e)}'
                }
            )
            raise

    async def update_trade(self, trade_id: str, update_data: Dict):
        """Update existing trade"""
        try:
            update_data['updated_at'] = datetime.now(timezone.utc)
            result = await self.db.trades.update_one(
                {'_id': trade_id},
                {'$set': update_data}
            )
            return result.modified_count > 0
        except Exception as e:
            self.logger.error(f"Error updating trade: {e}")
            return False

    async def save_symbol_info(self, symbol_data: Dict):
        """Save or update symbol information"""
        try:
            result = await self.db.symbols.update_one(
                {'symbol': symbol_data['symbol'], 'exchange': symbol_data['exchange']},
                {'$set': {**symbol_data, 'updated_at': datetime.now(timezone.utc)}},
                upsert=True
            )
            return True
        except Exception as e:
            self.logger.error(f"Error saving symbol info: {e}")
            return False

    async def get_open_positions(self, exchange: str) -> List[Dict]:
        """Get all open positions for an exchange"""
        try:
            cursor = self.db.positions.find({
                'exchange': exchange,
                'status': 'OPEN'
            })
            return await cursor.to_list(length=None)
        except Exception as e:
            self.logger.error(f"Error getting open positions: {e}")
            return []

    async def get_pending_orders(self, exchange: str) -> List[Dict]:
        """Get all pending orders for an exchange"""
        try:
            cursor = self.db.orders.find({
                'exchange': exchange,
                'status': 'PENDING'
            })
            return await cursor.to_list(length=None)
        except Exception as e:
            self.logger.error(f"Error getting pending orders: {e}")
            return []

    async def get_trade_history(self, exchange: str, symbol: Optional[str] = None, 
                              limit: int = 100) -> List[Dict]:
        """Get trade history with simplified structure"""
        try:
            query = {'exchange': exchange}
            if symbol:
                query['symbol'] = symbol

            cursor = self.db.trades.find(query).sort('created_at', DESCENDING).limit(limit)
            return await cursor.to_list(length=None)
        except Exception as e:
            self.logger.error(f"Error getting trade history: {e}")
            return []

    async def get_symbol_stats(self, exchange: str, symbol: str) -> Optional[Dict]:
        """Get aggregated statistics for a symbol"""
        try:
            pipeline = [
                {'$match': {'exchange': exchange, 'symbol': symbol}},
                {'$group': {
                    '_id': '$symbol',
                    'total_trades': {'$sum': 1},
                    'total_volume': {'$sum': '$quantity'},
                    'total_value_usdt': {'$sum': '$value_usdt'},
                    'avg_entry_price': {'$avg': '$entry_price'},
                    'total_pnl': {'$sum': '$realized_pnl'},
                    'last_trade_time': {'$max': '$created_at'}
                }}
            ]
            result = await self.db.trades.aggregate(pipeline).to_list(length=1)
            return result[0] if result else None
        except Exception as e:
            self.logger.error(f"Error getting symbol stats: {e}")
            return None

    async def save_leverage_setting(self, exchange: str, symbol: str, leverage: int):
        """Save leverage setting for symbol"""
        try:
            await self.db.settings.update_one(
                {
                    'exchange': exchange,
                    'key': f'leverage_{symbol}'
                },
                {
                    '$set': {
                        'value': leverage,
                        'updated_at': datetime.now(timezone.utc)
                    }
                },
                upsert=True
            )
            return True
        except Exception as e:
            self.logger.error(f"Error saving leverage setting: {e}")
            return False

    async def get_portfolio_summary(self, exchange: str) -> Dict:
        """Get portfolio summary with PnL calculations"""
        try:
            pipeline = [
                {'$match': {'exchange': exchange}},
                {'$group': {
                    '_id': None,
                    'total_trades': {'$sum': 1},
                    'total_value_usdt': {'$sum': '$value_usdt'},
                    'total_pnl': {'$sum': '$realized_pnl'},
                    'total_fees': {'$sum': '$fees_usdt'}
                }}
            ]
            result = await self.db.trades.aggregate(pipeline).to_list(length=1)
            return result[0] if result else {
                'total_trades': 0,
                'total_value_usdt': 0,
                'total_pnl': 0,
                'total_fees': 0
            }
        except Exception as e:
            self.logger.error(f"Error getting portfolio summary: {e}")
            return None

    async def mark_order_filled(self, order_id: str, fill_data: dict):
        """Move filled order from open_orders to trades with proper cleanup"""
        try:
            async with await self.client.start_session() as session:
                async with session.start_transaction():
                    # Get order from open_orders collection with status check
                    order = await self.db.open_orders.find_one(
                        {'order_id': order_id, 'status': {'$ne': 'FILLED'}},
                        session=session
                    )
                    
                    if order:
                        # Update trade record with FILLED status
                        update_data = {
                            'status': 'FILLED',
                            'fill_price': float(fill_data['price']),
                            'filled_quantity': float(fill_data['quantity']),
                            'fill_time': datetime.now(timezone.utc),
                            'fees': float(fill_data.get('fees', 0)),
                            'fee_asset': fill_data.get('fee_asset', 'USDT'),
                            'updated_at': datetime.now(timezone.utc)
                        }
                        
                        await self.db.trades.update_one(
                            {'trade_id': order['trade_id']},
                            {'$set': update_data},
                            session=session
                        )

                        # Move order to filled_orders collection
                        filled_order = {
                            **order,
                            **update_data
                        }
                        await self.db.filled_orders.insert_one(filled_order, session=session)
                        
                        # Remove from open_orders
                        await self.db.open_orders.delete_one(
                            {'order_id': order_id},
                            session=session
                        )

            self.logger.info(f"Order {order_id} marked as filled and archived")
            return True
            
        except DuplicateKeyError:
            self.logger.warning(f"Order {order_id} already processed")
            return False
        except Exception as e:
            self.logger.error(f"Error marking order filled: {e}")
            return False

    async def cleanup_duplicate_orders(self):
        """Remove duplicate open orders and sync with trades"""
        try:
            async with await self.client.start_session() as session:
                async with session.start_transaction():
                    # Find orders that should be closed
                    filled_trades = await self.db.trades.find(
                        {'status': 'FILLED'},
                        {'trade_id': 1}
                    ).to_list(length=None)
                    
                    filled_trade_ids = [t['trade_id'] for t in filled_trades]
                    
                    # Remove corresponding open orders
                    if filled_trade_ids:
                        result = await self.db.open_orders.delete_many({
                            'trade_id': {'$in': filled_trade_ids}
                        })
                        
                        if result.deleted_count > 0:
                            self.logger.info(f"Cleaned up {result.deleted_count} stale open orders")
                    
                    # Remove duplicates keeping only the latest
                    pipeline = [
                        {'$group': {
                            '_id': '$order_id',
                            'latest_doc': {'$last': '$$ROOT'},
                            'count': {'$sum': 1}
                        }},
                        {'$match': {
                            'count': {'$gt': 1}
                        }}
                    ]
                    
                    duplicates = await self.db.open_orders.aggregate(pipeline).to_list(length=None)
                    
                    for dup in duplicates:
                        # Keep only the latest version
                        await self.db.open_orders.delete_many({
                            'order_id': dup['_id'],
                            '_id': {'$ne': dup['latest_doc']['_id']}
                        })

            return True
            
        except Exception as e:
            self.logger.error(f"Error cleaning up orders: {e}")
            return False

    async def save_invalid_symbol(self, symbol: str):
        """Store invalid symbol in database"""
        try:
            await self.db.invalid_symbols.update_one(
                {'symbol': symbol},
                {
                    '$set': {
                        'symbol': symbol,
                        'added_at': datetime.now(timezone.utc)
                    }
                },
                upsert=True
            )
            return True
        except Exception as e:
            self.logger.error(f"Error saving invalid symbol: {e}")
            return False

    async def get_invalid_symbols(self) -> List[str]:
        """Get list of invalid symbols"""
        try:
            cursor = self.db.invalid_symbols.find({})
            symbols = await cursor.to_list(length=None)
            return [symbol['symbol'] for symbol in symbols]
        except Exception as e:
            self.logger.error(f"Error getting invalid symbols: {e}")
            return []

    async def move_order_to_trades(self, order_id, order_status):
        """Move an order from the orders collection to the trades collection"""
        try:
            order = await self.db.orders.find_one({'order_id': order_id})
            if order:
                order['status'] = 'FILLED'
                order['filled_time'] = datetime.now(timezone.utc).isoformat()
                order['actual_price'] = float(order_status['price'])
                order['actual_quantity'] = float(order_status['executedQty'])
                
                await self.db.trades.insert_one(order)
                await self.db.orders.delete_one({'order_id': order_id})
                self.logger.info(f"Order moved to trades: {order_id}")
            else:
                self.logger.warning(f"Order not found in orders collection: {order_id}")
        except Exception as e:
            self.logger.error(f"Error moving order to trades: {e}")

    async def save_order(self, order_data: Dict):
        """Save order separately from trade"""
        try:
            order_doc = {
                'order_id': order_data['order_id'],
                'trade_id': order_data['trade_id'],
                'symbol': order_data['symbol'],
                'price': float(order_data['price']),
                'quantity': float(order_data['quantity']),
                'side': order_data['side'],
                'type': order_data['type'],
                'status': 'PENDING',
                'created_at': datetime.now(timezone.utc),
                'updated_at': datetime.now(timezone.utc)
            }
            
            result = await self.db.orders.update_one(
                {'order_id': order_data['order_id']},
                {'$set': order_doc},
                upsert=True
            )
            return True
        except Exception as e:
            self.logger.error(f"Error saving order: {e}")
            return False

    async def get_open_orders(self, exchange: str) -> list:
        """Get all open orders for an exchange"""
        try:
            cursor = self.db.open_orders.find({
                'exchange': exchange,
                'status': 'PENDING'
            })
            return await cursor.to_list(length=None)
        except Exception as e:
            self.logger.error(f"Error getting open orders: {e}")
            return []

    async def update_order_status(self, order_id: str, status: str):
        """Update order status"""
        try:
            result = await self.db.open_orders.update_one(
                {'order_id': order_id},
                {'$set': {
                    'status': status,
                    'updated_at': datetime.now(timezone.utc)
                }}
            )
            return result.modified_count > 0
        except Exception as e:
            self.logger.error(f"Error updating order status: {e}")
            return False

    async def cancel_order(self, order_id: str):
        """Mark order as cancelled and remove from open orders"""
        try:
            result = await self.db.open_orders.delete_one({'order_id': order_id})
            return result.deleted_count > 0
        except Exception as e:
            self.logger.error(f"Error cancelling order: {e}")
            return False

    async def mark_order_cancelled(self, order_id: str):
        """Handle cancelled order"""
        try:
            async with await self.client.start_session() as session:
                async with session.start_transaction():
                    # Get order before deletion
                    order = await self.db.open_orders.find_one({'order_id': order_id})
                    if not order:
                        return False

                    # Update trade status
                    await self.db.trades.update_one(
                        {'trade_id': order['trade_id']},
                        {
                            '$set': {
                                'status': 'CANCELLED',
                                'cancel_time': datetime.now(timezone.utc)
                            }
                        }
                    )

                    # Remove from open_orders
                    await self.db.open_orders.delete_one({'order_id': order_id})

            return True
        except Exception as e:
            self.logger.error(f"Error marking order cancelled: {e}")
            return False
