from datetime import timedelta

# Decimal precision for different cryptocurrencies
PRECISION = {
    'BTC': 6,
    'ETH': 5,
    'USDT': 2,
    'DEFAULT': 8
}

# Timeframe reset intervals
TIMEFRAME_INTERVALS = {
    'DAILY': timedelta(days=1),
    'WEEKLY': timedelta(weeks=1),
    'MONTHLY': timedelta(days=30)
}

# Rate limiting
MAX_REQUESTS_PER_MINUTE = 1200
REQUEST_WEIGHT_DEFAULT = 1

# Order related constants
MIN_NOTIONAL = {
    'BTCUSDT': 10,
    'ETHUSDT': 10,
    'DEFAULT': 10
}

# Tax settings
TAX_RATE = 0.28 #Tax Rate

# Profit calculation settings
PRICE_PRECISION = {
    'PRICE': 2,
    'PERCENTAGE': 2,
    'QUANTITY': 8
}

# Fee settings
TRADING_FEES = {
    'MAKER': 0.001,  # 0.1% maker fee
    'TAKER': 0.001,  # 0.1% taker fee
    'DEFAULT': 0.001  # Default fee for testnet
}
