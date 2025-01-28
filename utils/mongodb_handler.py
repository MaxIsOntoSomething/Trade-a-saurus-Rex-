import motor.motor_asyncio
from datetime import datetime, timezone
import logging
from typing import Optional, Dict, List, Any
from pymongo import ASCENDING, DESCENDING, IndexModel
import time

class MongoDBHandler:
    def __init__(self, connection_string: str, database_name: str):
        self.client = motor.motor_asyncio.AsyncIOMotorClient(connection_string)
        self.db = self.client[database_name]
        self.logger = logging.getLogger('MongoDB')
        
        # Add new collections to initialization
        collections = {
            'trades': [
                IndexModel([('symbol', ASCENDING)]),
                IndexModel([('status', ASCENDING)]),
                IndexModel([('exchange', ASCENDING)]),
                IndexModel([('created_at', DESCENDING)])
            ],
            'orders': [
                IndexModel([('symbol', ASCENDING)]),
                IndexModel([('status', ASCENDING)]),
                IndexModel([('order_id', ASCENDING)], unique=True)
            ],
            'symbols': [
                IndexModel([('symbol', ASCENDING)], unique=True),
                IndexModel([('exchange', ASCENDING)])
            ],
            'positions': [
                IndexModel([('symbol', ASCENDING)]),
                IndexModel([('exchange', ASCENDING)])
            ],
            'settings': [
                IndexModel([('exchange', ASCENDING)]),
                IndexModel([('key', ASCENDING)], unique=True)
            ],
            'invalid_symbols': [
                IndexModel([('symbol', ASCENDING)], unique=True),  # New collection
                IndexModel([('added_at', DESCENDING)])
            ],
            'system_settings': [  # For storing various system settings
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
        """Initialize database collections and indexes"""
        try:
            # Create collections if they don't exist
            collections = {
                'trades': [
                    IndexModel([('symbol', ASCENDING)]),
                    IndexModel([('status', ASCENDING)]),
                    IndexModel([('exchange', ASCENDING)]),
                    IndexModel([('created_at', DESCENDING)])
                ],
                'orders': [
                    IndexModel([('symbol', ASCENDING)]),
                    IndexModel([('status', ASCENDING)]),
                    IndexModel([('order_id', ASCENDING)], unique=True)
                ],
                'symbols': [
                    IndexModel([('symbol', ASCENDING)], unique=True),
                    IndexModel([('exchange', ASCENDING)])
                ],
                'positions': [
                    IndexModel([('symbol', ASCENDING)]),
                    IndexModel([('exchange', ASCENDING)])
                ],
                'settings': [
                    IndexModel([('exchange', ASCENDING)]),
                    IndexModel([('key', ASCENDING)], unique=True)
                ],
                'invalid_symbols': [
                    IndexModel([('symbol', ASCENDING)], unique=True),  # New collection
                    IndexModel([('added_at', DESCENDING)])
                ],
                'system_settings': [  # For storing various system settings
                    IndexModel([('key', ASCENDING)], unique=True)
                ]
            }

            # Create indexes for each collection
            for collection_name, indexes in collections.items():
                await self.db[collection_name].create_indexes(indexes)

            self.logger.info("Database initialized successfully")
            return True

        except Exception as e:
            self.logger.error(f"Database initialization failed: {e}")
            return False

    async def save_trade(self, trade_data: Dict):
        """Save trade with proper exchange and environment tags"""
        try:
            trade_data['created_at'] = datetime.now(timezone.utc)
            trade_data['updated_at'] = datetime.now(timezone.utc)
            
            result = await self.db.trades.insert_one(trade_data)
            return result.inserted_id
        except Exception as e:
            self.logger.error(f"Error saving trade: {e}")
            return None

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
        """Get trade history with optional symbol filter"""
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

    async def mark_order_filled(self, order_id: str, fill_data: Dict):
        """Mark order as filled with execution details"""
        try:
            result = await self.db.orders.update_one(
                {'order_id': order_id},
                {
                    '$set': {
                        'status': 'FILLED',
                        'fill_time': datetime.now(timezone.utc),
                        'fill_price': fill_data['price'],
                        'filled_quantity': fill_data['quantity'],
                        'fees': fill_data.get('fees', 0),
                        'fee_asset': fill_data.get('fee_asset', 'USDT'),
                        'updated_at': datetime.now(timezone.utc)
                    }
                }
            )
            return result.modified_count > 0
        except Exception as e:
            self.logger.error(f"Error marking order filled: {e}")
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
