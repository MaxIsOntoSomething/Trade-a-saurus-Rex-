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

- Take-profit functionality
- Enhanced risk management
- Advanced analytics
- Performance optimizations
- More Exchanges
- Leverage Perp Optional 
- SQL Database integration using SQLLite

## Contact

Discord: **maskiplays**

## Disclaimer

For educational purposes only. Trade at your own risk.

## License

MIT License
