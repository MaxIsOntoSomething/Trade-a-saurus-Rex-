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
    'MONTHLY': timedelta(days=30)  # approximation
}

FUTURES_TIMEFRAME_INTERVALS = {
    'DAILY': timedelta(days=1),
    'WEEKLY': timedelta(weeks=1),
    'MONTHLY': timedelta(days=30)  # Approximation
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

# Futures-specific constants
FUTURES_SETTINGS = {
    'DEFAULT_LEVERAGE': 2,
    'MAX_LEVERAGE': 125,
    'MIN_LEVERAGE': 1,
    'MARGIN_TYPES': ['ISOLATED', 'CROSSED'],
    'POSITION_MODES': ['ONE_WAY', 'HEDGE']
}

FUTURES_MAINTENANCE_MARGINS = {
    'BTCUSDT': 0.0075,  # 0.75%
    'ETHUSDT': 0.01,    # 1%
    'DEFAULT': 0.02     # 2%
}

FUTURES_MIN_NOTIONAL = {
    'BTCUSDT': 5,
    'ETHUSDT': 5,
    'DEFAULT': 5
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
