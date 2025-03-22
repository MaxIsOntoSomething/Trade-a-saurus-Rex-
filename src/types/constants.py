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

# Notification formatting
NOTIFICATION_EMOJI = {
    'DAILY': '📅',
    'WEEKLY': '📆',
    'MONTHLY': '📊',
    'THRESHOLD': '🎯',
    'RESET': '🔄',
    'SUCCESS': '✅',
    'ERROR': '❌',
    'WARNING': '⚠️'
}

# Update trading fees based on provided rates
TRADING_FEES = {
    'DEFAULT': 0.001,  # 0.10% default for spot
    'SPOT': 0.001,     # 0.10% for spot trading
    'FUTURES': 0.002  # 0.02% for futures trading (updated to correct rate)
}

# Add order type specific fees
ORDER_TYPE_FEES = {
    'spot': 0.001,     # 0.10% for spot
    'futures': 0.002  # 0.02% for futures (updated to correct rate)
}
