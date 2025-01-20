# Binance Trading Bot

A sophisticated cryptocurrency trading bot that implements a drop-based buying strategy across multiple timeframes with advanced visualization capabilities.

## Core Features 🚀

- ✨ Multi-timeframe monitoring (daily, weekly, monthly)
- 📊 Advanced visualization and analytics
- 💰 Dynamic drop threshold system
- 🔒 Balance protection with USDT reserve
- 📈 Real-time price tracking and trend indicators
- 🤖 Comprehensive Telegram integration
- 📉 Advanced performance graphs and statistics
- ⚡ Limit and market order support
- 🛡️ Smart symbol validation and error handling
- 💹 Portfolio analysis and tracking
- 📝 Automatic invalid symbol management
- 🕒 24h trading pause on low balance
- 📊 Data visualization suite
- ⏱️ 8-hour limit order auto-cancellation
- 🔍 Real-time order status monitoring

## Analytics & Visualization Features

- 📊 Entry price distribution histograms
- 📈 Position building visualization
- ⏱️ Trade timing analysis
- 💼 Portfolio value evolution
- 🥧 Asset allocation charts

## Prerequisites

- Python 3.7 or higher
- Binance account
- Telegram bot (optional but recommended)

## Installation

1. **Clone the repository**
    ```sh
    git clone https://github.com/your-username/binance-bot.git
    cd binance-bot
    ```

2. **Set up virtual environment**
    ```sh
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```

3. **Install dependencies**
    ```sh
    pip install -r requirements.txt
    ```

## Configuration

1. **Initial Setup**
   - Copy `config_template.json` to `config.json`
   - Add your API keys and preferences
   - Configure trading symbols

2. **Trading Parameters**
   - Choose between testnet/live trading
   - Set USDT reserve amount
   - Configure trading amount
   - Select order type (limit/market)

3. **Timeframe Settings**
   - Enable/disable timeframes (daily, weekly, monthly)
   - Set drop thresholds for each timeframe
   - Configure order limits

## Order Management

- Limit orders auto-cancel after 8 hours
- Real-time order status monitoring
- Automatic order validation
- Dynamic quantity adjustment
- Price precision handling

## Safety Features

- Real-time balance monitoring
- USDT reserve protection
- Invalid symbol detection
- Error recovery system
- Automatic trade pausing
- Order timeout protection
- Scientific notation handling for small-cap tokens

## Logging System

- Clean, structured trade logs
- Important event tracking
- Error and warning management
- Performance monitoring
- Trade execution details
- Balance updates

## Trading Strategy

The bot implements a sophisticated multi-timeframe drop-based buying strategy:
- Monitors daily, weekly, and monthly timeframes
- Executes trades at configured drop thresholds
- Implements smart position building
- Manages order timeouts and cancellations

## Telegram Commands

- `/start` - Show available commands and bot status
- `/positions` - Show available trading opportunities
- `/balance` - Show current balance
- `/trades` - Show total number of trades
- `/profits` - Show current profits
- `/stats` - Show system stats and bot information
- `/distribution` - Show entry price distribution
- `/stacking` - Show position building over time
- `/buytimes` - Show time between buys
- `/portfolio` - Show portfolio value evolution
- `/allocation` - Show asset allocation
- `/orders` - Show open limit orders with cancellation times

## Support & Contact

For support or questions:
- Discord: **maskiplays**

## Disclaimer

This bot is for educational purposes only. Trading cryptocurrencies carries significant risks. Use at your own discretion.

## License

MIT License - Feel free to use and modify as needed.