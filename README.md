# Binance Trading Bot

An automated cryptocurrency trading bot for Binance with portfolio management and analytics.

## Key Features 🚀

- ✨ Multi-timeframe monitoring (daily, weekly, monthly)
- 📊 Real-time analytics and visualization
- 💰 Dynamic price drop detection
- 🔒 USDT reserve protection
- 🤖 Telegram integration
- 📈 Portfolio tracking and tax calculations (28%)

## Prerequisites

- Python 3.7+
- Binance account
- Telegram bot (optional)

## Quick Start

1. **Clone and setup**
   ```sh
   git clone https://github.com/your-username/binance-bot.git
   cd binance-bot
   python -m venv venv
   source venv/bin/activate  # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Configure**
   - Copy `config_template.json` to `config.json`
   - Add API keys and trading preferences

## Main Features

### Trading
- Automated multi-timeframe buying strategy
- Limit and market order support
- 8-hour order auto-cancellation
- Position building and tracking

### Portfolio Management
- Real-time value tracking
- P/L calculations with tax
- Trade history and cost basis tracking

### Telegram Commands
- `/positions` - View prices and opportunities
- `/balance` - Show current balances
- `/trades` - List trades with P/L
- `/portfolio` - Portfolio overview
- `/stats` - System information

## Docker Support

```sh
# Start
docker-compose up -d

# Stop
docker-compose down
```

## Future Plans

### API Integration
- [x] Basic Spot Trading
- [x] Basic Futures Trading
- [ ] WebSocket support for real-time price updates
- [ ] Futures-specific order types (stop-loss, take-profit)
- [ ] OCO (One-Cancels-Other) orders
- [ ] Trailing stops for both spot and futures

### Risk Management
- [ ] Position sizing based on account risk percentage
- [ ] Dynamic leverage adjustment based on volatility
- [ ] Auto-hedging in futures mode
- [ ] Liquidation price monitoring
- [ ] Multi-level stop losses

### Trading Features
- [ ] Grid trading support
- [ ] DCA (Dollar Cost Averaging) strategies
- [ ] Combined spot-futures arbitrage
- [ ] Cross-exchange arbitrage
- [ ] Multi-timeframe trading strategies

### Market Analysis
- [ ] Technical indicators (RSI, MACD, etc.)
- [ ] Order book depth analysis
- [ ] Volume profile analysis
- [ ] Funding rate monitoring for futures
- [ ] Sentiment analysis integration

### Portfolio Management
- [ ] Auto-rebalancing
- [ ] Cross-margin collateral management
- [ ] PnL tracking per strategy
- [ ] Risk-adjusted performance metrics
- [ ] Tax-efficient trading strategies

### System Improvements
- [ ] Multi-account support
- [ ] API failover and load balancing
- [ ] Enhanced error recovery
- [ ] Performance optimization
- [ ] Distributed system support
- [x] SQLite Database integration

### User Interface
- [ ] Web dashboard
- [x] Telegram integration
- [ ] Mobile notifications
- [ ] Real-time performance graphs
- [ ] Position builder interface
- [ ] Strategy backtesting UI

### Advanced Features
- [ ] Machine learning predictions
- [ ] Custom strategy builder
- [ ] Social trading integration
- [ ] API marketplace
- [ ] Automated strategy optimization

## Contact

Discord: **maskiplays**

## Disclaimer

For educational purposes only. Trade at your own risk.

## License

MIT License
