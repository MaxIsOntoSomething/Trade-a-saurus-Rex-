import motor.motor_asyncio
from datetime import datetime
from typing import List, Optional
from ..types.models import Order, OrderStatus
from ..types.constants import TAX_RATE, PRICE_PRECISION
from decimal import Decimal, ROUND_DOWN

class MongoClient:
    def __init__(self, uri: str, database: str):
        self.client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self.client[database]
        self.orders = self.db.orders

    async def init_indexes(self):
        await self.orders.create_index("order_id", unique=True)
        await self.orders.create_index("status")
        await self.orders.create_index("symbol")

    async def insert_order(self, order: Order) -> str:
        order_dict = {
            "symbol": order.symbol,
            "status": order.status.value,
            "price": str(order.price),
            "quantity": str(order.quantity),
            "threshold": order.threshold,
            "timeframe": order.timeframe.value,
            "order_id": order.order_id,
            "created_at": order.created_at,
            "updated_at": order.updated_at,
            "fees": str(order.fees),
            "fee_asset": order.fee_asset,
            "is_manual": order.is_manual
        }
        result = await self.orders.insert_one(order_dict)
        return str(result.inserted_id)

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
        cursor = self.orders.find({"status": OrderStatus.PENDING.value})
        orders = []
        async for doc in cursor:
            orders.append(self._document_to_order(doc))
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

    def _document_to_order(self, doc: dict) -> Order:
        return Order(
            symbol=doc["symbol"],
            status=OrderStatus(doc["status"]),
            price=float(doc["price"]),
            quantity=float(doc["quantity"]),
            threshold=doc["threshold"],
            timeframe=doc["timeframe"],
            order_id=doc["order_id"],
            created_at=doc["created_at"],
            updated_at=doc["updated_at"],
            filled_at=doc.get("filled_at"),
            cancelled_at=doc.get("cancelled_at")
        )
