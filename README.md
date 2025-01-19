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

4. **Configure the bot**
    ```sh
    cp config/config_template.json config/config.json
    ```
    Edit config.json with your credentials:
    ```json
    {
        "BINANCE_API_KEY": "your_api_key",
        "BINANCE_API_SECRET": "your_api_secret",
        "TESTNET_API_KEY": "your_testnet_key",
        "TESTNET_API_SECRET": "your_testnet_secret",
        "TRADING_SYMBOLS": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        "TELEGRAM_TOKEN": "your_telegram_token",
        "TELEGRAM_CHAT_ID": "your_chat_id"
    }
    ```

## Usage

1. **Start the bot**
    ```sh
    python main.py
    ```

2. **Initial Configuration**
    - Choose between Testnet/Live trading
    - Set up Telegram notifications
    - Configure USDT reserve amount
    - Set up timeframes and thresholds
    - Choose order type (limit/market)
    - Configure trade amounts

3. **Telegram Commands**
    - `/start` - Show available commands
    - `/positions` - Show trading opportunities
    - `/balance` - Current balance
    - `/trades` - Trade count
    - `/profits` - Current profits
    - `/stats` - System statistics
    - `/distribution` - Price entry distribution
    - `/stacking` - Position building analysis
    - `/buytimes` - Trade timing analysis
    - `/portfolio` - Portfolio evolution
    - `/allocation` - Asset allocation

## Trading Strategy

The bot implements a multi-timeframe drop-based buying strategy:
- Monitors price drops across daily, weekly, and monthly timeframes
- Executes trades when price drops reach configured thresholds
- Maintains separate thresholds for each timeframe
- Implements smart order management and position building

## Analytics Features

1. **Entry Price Analysis**
   - Distribution of entry prices
   - Entry timing patterns
   - Price level analysis

2. **Position Building**
   - Cumulative position growth
   - Entry point visualization
   - Position building patterns

3. **Portfolio Analytics**
   - Value evolution over time
   - Asset allocation
   - Performance metrics

4. **Trading Patterns**
   - Time between trades
   - Trade frequency analysis
   - Market timing visualization

## Completed Features ✅

- [x] Multi-timeframe support
- [x] Enhanced Telegram integration
- [x] Advanced graphing capabilities
- [x] Portfolio analytics
- [x] Position tracking
- [x] Trade timing analysis
- [x] Asset allocation visualization
- [x] Error handling and recovery
- [x] Invalid symbol management
- [x] Balance protection system

## TODO 📋

- [ ] Implement sell strategies
- [ ] Add more technical indicators
- [ ] Develop backtesting module
- [ ] Create web dashboard
- [ ] Add more exchanges
- [ ] Implement machine learning predictions
- [ ] Add trade journal export
- [ ] Create mobile app integration
- [ ] Add email notifications
- [ ] Implement stop-loss features

## Safety Features

- Real-time balance monitoring
- USDT reserve protection
- Invalid symbol detection
- Error recovery system
- Automatic trade pausing
- Order timeout protection

## Support & Contact

For support or questions:

[![Discord](https://img.shields.io/badge/Discord-7289DA?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/)

Discord: **maskiplays**

## Disclaimer

This bot is provided for educational purposes. Trading cryptocurrencies carries significant risks. Use at your own discretion and risk.

## License

MIT License - Feel free to use and modify as needed