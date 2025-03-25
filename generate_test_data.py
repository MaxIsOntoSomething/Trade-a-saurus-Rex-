import asyncio
import random
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Dict
from src.types.models import TakeProfit, StopLoss, TPSLStatus
from src.database.mongo_client import MongoClient
from src.types.models import Order, OrderStatus, TimeFrame, OrderType, TakeProfit, StopLoss, TPSLStatus
from src.trading.binance_client import BinanceClient

class TestDataGenerator:
    def __init__(self, mongo_client: MongoClient, config: dict):
        self.mongo_client = mongo_client
        self.config = config
        self.base_currency = config['trading'].get('base_currency', 'USDT')
        self.symbols = config['trading']['pairs']
        
    async def generate_all_test_data(self):
        """Generate all test data"""
        print("Generating test data...")
        
        # Generate orders for the last 30 days
        await self.generate_orders(days=30)
        
        # Generate balance history
        await self.generate_balance_history(days=30)
        
        # Generate some triggered thresholds
        await self.generate_triggered_thresholds()
        
        print("Test data generation complete!")
        
    async def generate_orders(self, days: int = 30):
        """Generate sample orders with more realistic BTC trading data"""
        print("Generating sample orders...")
        
        # Base prices for symbols (approximate real values)
        base_prices = {
            'BTCUSDT': 35000,  # Starting point for BTC
            'ETHUSDT': 2200,
            'BNBUSDT': 300,
            'SOLUSDT': 100,
            'ADAUSDT': 0.5,
            'XRPUSDT': 0.5,
            'DOGEUSDT': 0.1,
        }
        
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=days)
        
        order_count = 0
        btc_trades = 0  # Counter for BTC trades
        
        # Generate price trend for BTC
        btc_price_trend = []
        current_price = base_prices['BTCUSDT']
        for _ in range(days * 24):  # Hourly prices
            # Add some randomness to price movement
            change = random.uniform(-0.015, 0.015)  # 1.5% max change per hour
            current_price *= (1 + change)
            # Keep price within desired range
            current_price = max(20000, min(40000, current_price))
            btc_price_trend.append(current_price)
        
        for symbol in self.symbols:
            # Determine number of orders based on symbol
            if symbol == 'BTCUSDT':
                num_orders = 40  # More BTC trades
            else:
                num_orders = random.randint(5, 15)  # Fewer trades for other pairs
            
            base_price = base_prices.get(symbol, random.uniform(1, 100))
            
            for _ in range(num_orders):
                # Generate random date between start and end
                order_date = start_date + timedelta(
                    seconds=random.randint(0, int((end_date - start_date).total_seconds()))
                )
                
                # Calculate price based on symbol
                if symbol == 'BTCUSDT':
                    # Use the price trend for BTC
                    days_passed = (order_date - start_date).days
                    hours_passed = int((order_date - start_date).total_seconds() / 3600)
                    if hours_passed >= len(btc_price_trend):
                        hours_passed = len(btc_price_trend) - 1
                    price = Decimal(str(btc_price_trend[hours_passed]))
                    btc_trades += 1
                else:
                    # Random variation for other symbols
                    price = Decimal(str(base_price * random.uniform(0.8, 1.2)))
                
                # Generate quantity (adjusted for BTC)
                if symbol == 'BTCUSDT':
                    # Smaller quantities for BTC due to higher price
                    quantity = Decimal(str(random.uniform(0.001, 0.01)))
                else:
                    quantity = Decimal(str(random.uniform(0.1, 1.0) * (100 / float(price))))
                
                # Create order with TP/SL
                order = Order(
                    symbol=symbol,
                    status=OrderStatus.FILLED,
                    order_type=OrderType.SPOT,
                    price=price,
                    quantity=quantity,
                    timeframe=random.choice(list(TimeFrame)),
                    order_id=f"TEST_{order_date.strftime('%Y%m%d_%H%M%S')}_{random.randint(1000, 9999)}",
                    created_at=order_date,
                    updated_at=order_date,
                    filled_at=order_date,
                    fees=price * quantity * Decimal('0.001'),  # 0.1% fee
                    fee_asset=self.base_currency,
                    threshold=random.choice([1, 2, 5, 10]),
                    is_manual=False
                )
                
                # Add take profit and stop loss for some orders
                if random.random() < 0.7:  # 70% of orders have TP/SL
                    tp_price = price * Decimal(str(1 + random.uniform(0.02, 0.05)))  # 2-5% TP
                    sl_price = price * Decimal(str(1 - random.uniform(0.01, 0.03)))  # 1-3% SL
                    
                    order.take_profit = TakeProfit(
                        price=tp_price,
                        percentage=float((tp_price/price - 1) * 100),
                        status=TPSLStatus.TRIGGERED if random.random() < 0.4 else TPSLStatus.PENDING,
                        triggered_at=order_date + timedelta(hours=random.randint(1, 24)) if random.random() < 0.4 else None
                    )
                    
                    order.stop_loss = StopLoss(
                        price=sl_price,
                        percentage=float((1 - sl_price/price) * 100),
                        status=TPSLStatus.TRIGGERED if random.random() < 0.2 else TPSLStatus.PENDING,
                        triggered_at=order_date + timedelta(hours=random.randint(1, 24)) if random.random() < 0.2 else None
                    )
                
                # Save to database
                await self.mongo_client.insert_order(order)
                order_count += 1
                
                # Add small delay to prevent database overload
                await asyncio.sleep(0.1)
        
        print(f"Generated {order_count} sample orders (including {btc_trades} BTC trades)")
        
    async def generate_balance_history(self, days: int = 30):
        """Generate more realistic balance history data"""
        print("Generating balance history...")
        
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=days)
        
        # More realistic starting values
        balance = Decimal('25000')    # Starting balance
        invested = Decimal('15000')   # Starting investment
        
        current_date = start_date
        entry_count = 0
        
        # Create a trend for balance changes
        trend = random.choice(['upward', 'downward', 'sideways'])
        trend_strength = random.uniform(0.001, 0.003)  # 0.1-0.3% daily trend
        
        while current_date <= end_date:
            # Apply trend to balance changes
            if trend == 'upward':
                balance_change = Decimal(str(random.uniform(-0.5, 1.5) * trend_strength * float(balance)))
            elif trend == 'downward':
                balance_change = Decimal(str(random.uniform(-1.5, 0.5) * trend_strength * float(balance)))
            else:  # sideways
                balance_change = Decimal(str(random.uniform(-1.0, 1.0) * trend_strength * float(balance)))
            
            balance += balance_change
            
            # Sometimes adjust investment amount
            if random.random() < 0.2:  # 20% chance
                investment_change = Decimal(str(random.uniform(-0.02, 0.05) * float(invested)))
                invested += investment_change
            
            # Ensure balance stays reasonable
            balance = max(balance, Decimal('5000'))  # Don't go below 5000
            invested = max(invested, Decimal('0'))   # Don't go negative
            
            # Record balance
            await self.mongo_client.record_balance(
                timestamp=current_date,
                balance=balance,
                invested=invested
            )
            
            # Random time increment between 2-6 hours
            current_date += timedelta(hours=random.randint(2, 6))
            entry_count += 1
            
            # Add small delay to prevent database overload
            await asyncio.sleep(0.1)
        
        print(f"Generated {entry_count} balance history entries")
        
    async def generate_triggered_thresholds(self):
        """Generate some triggered thresholds"""
        print("Generating triggered thresholds...")
        
        threshold_count = 0
        for symbol in self.symbols:
            for timeframe in TimeFrame:
                # 50% chance to have triggered thresholds for each symbol/timeframe
                if random.random() < 0.5:
                    # Generate 1-3 triggered thresholds
                    thresholds = random.sample([1, 2, 5, 10, 15, 20], random.randint(1, 3))
                    
                    # Save to database
                    await self.mongo_client.save_triggered_threshold(
                        symbol=symbol,
                        timeframe=timeframe.value,
                        thresholds=thresholds
                    )
                    threshold_count += len(thresholds)
                    
        print(f"Generated {threshold_count} triggered thresholds")

async def main():
    # Load configuration
    from main import load_and_merge_config
    config = load_and_merge_config()
    
    # Initialize MongoDB client
    mongo_client = MongoClient(
        uri=config['mongodb']['uri'],
        database=config['mongodb']['database'],
        driver=config['mongodb'].get('driver', 'motor')
    )
    
    # Create test data generator
    generator = TestDataGenerator(mongo_client, config)
    
    # Generate test data
    await generator.generate_all_test_data()

if __name__ == "__main__":
    asyncio.run(main()) 