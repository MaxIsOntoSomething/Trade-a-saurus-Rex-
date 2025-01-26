# Binance Trading Bot

A Self Made Cryptocurrency Bot that buys for me to safe Money on the side , this is a hobby project.

## Core Features 🚀

- ✨ Multi-timeframe monitoring (daily, weekly, monthly)
- 📊 Advanced visualization and analytics
- 💰 Dynamic drop threshold system
- 🔒 Balance protection with USDT reserve
- 📈 Real-time price tracking and trend indicators
- 🤖 Interactive Telegram commands
- 📉 Advanced performance graphs and statistics
- ⚡ Limit and market order support
- 🛡️ Smart symbol validation and error handling
- 💹 Portfolio analysis and tracking
- 📝 Manual trade entry system
- 🕒 24h trading pause on low balance
- 📊 Data visualization suite
- ⏱️ 8-hour limit order auto-cancellation
- 🔍 Real-time order status monitoring
- 💼 Complete portfolio management
- 🧮 Automatic tax calculations (28%)
- 🎯 Enhanced Visual Monitoring
- ⚡ Real-time API Status Indicators
- 🔄 Interactive Progress Spinners
- ⏱️ Rate Limit Visualizations
- 📊 Live Price Updates Display

## Portfolio Management Features

- 📈 Real-time portfolio tracking
- 💰 Automated profit/loss calculations
- 📊 Tax-adjusted performance metrics
- 🔄 Manual trade entry system
- 📑 Trade history management
- 💹 Symbol-specific analytics
- 📌 Position tracking
- 💸 Cost basis tracking

## Analytics & Visualization Features

- 📊 Entry price distribution histograms
- 📈 Position building visualization
- ⏱️ Trade timing analysis
- 💼 Portfolio value evolution
- 🥧 Asset allocation charts
- 📉 Price drop monitoring
- 💰 Profit/loss tracking
- 📊 Tax impact analysis

## Real-time Monitoring Features

### Visual Feedback System
- Live spinner animations for active monitoring
- API call status indicators with timing
- Rate limit countdown displays
- Price update progress indicators
- Clear success/failure icons

### Price Monitoring Display
- Symbol-by-symbol price updates
- Real-time percentage changes
- Clean, organized output format
- Color-coded trend indicators
- Instant error notifications

### Rate Limiting Visualization
- Real-time rate limit tracking
- Visual countdown timers
- Dynamic spinner animations
- Clear reset notifications
- Queue position indicators

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

## Portfolio Management

- Real-time value tracking
- Automated P/L calculations
- Tax considerations (28% rate)
- Position size monitoring
- Cost basis tracking
- Trade history management

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

### Market Analysis
- `/positions` - Show current prices and trading opportunities
- `/orders` - Show open limit orders with cancel times

### Portfolio & Trading
- `/balance` - Show current balance for all assets
- `/trades` - List all trades with P/L after tax
- `/addtrade` - Interactive manual trade entry
- `/symbol <SYMBOL>` - Show detailed symbol stats with tax
- `/summary` - Show complete portfolio summary with tax
- `/profits` - Show current profits for all positions
- `/portfolio` - Show portfolio value evolution
- `/allocation` - Show current asset distribution

### Analytics
- `/distribution` - Show entry price distribution
- `/stacking` - Show position building patterns
- `/buytimes` - Show time between purchases

### System
- `/stats` - Show system stats and bot information

## Manual Trade Entry System

The bot supports manual trade entry through an interactive Telegram conversation:

1. Start with `/addtrade`
2. Enter trading pair (e.g., BTCUSDT)
3. Enter entry price
4. Enter quantity
5. Review and confirm trade details
6. Trade is added to portfolio tracking

Features:
- Input validation at each step
- Clear error messages
- Preview before confirmation
- Automatic tax calculations
- Integration with portfolio summary

## Docker Setup

### Prerequisites
- Docker installed on your system
- Docker Compose installed on your system

### Running with Docker

1. **Build and start the container**
   ```sh
   docker-compose up -d
   ```

2. **View logs**
   ```sh
   docker-compose logs -f
   ```

3. **Stop the container**
   ```sh
   docker-compose down
   ```

### Docker Configuration

1. **Environment Variables**
   - Copy `.env.example` to `.env`
   - Update the variables in `.env`:
     ```properties
     # Bot Configuration
     USE_TESTNET=true
     USE_TELEGRAM=true
     ORDER_TYPE=limit
     USE_PERCENTAGE=false
     TRADE_AMOUNT=10
     RESERVE_BALANCE=20000

     # API Keys
     BINANCE_API_KEY=your_api_key
     BINANCE_API_SECRET=your_api_secret
     TESTNET_API_KEY=your_testnet_key
     TESTNET_API_SECRET=your_testnet_secret
     TELEGRAM_TOKEN=your_telegram_token
     TELEGRAM_CHAT_ID=your_chat_id

     # Trading Symbols
     TRADING_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT

     # Timeframes Configuration
     DAILY_CONFIG=true:1,2,3
     WEEKLY_CONFIG=true:3,6,10
     MONTHLY_CONFIG=true:5,10
     ```

2. **Volume Mounts**
   - `/app/logs`: Bot log files
   - `/app/data`: Bot data files
   - `/app/config`: Configuration files

3. **Dockerfile Configuration**
   - Base image: Python 3.9-slim
   - Automatically installs dependencies
   - Creates necessary directories
   - Sets timezone to UTC

4. **Docker Compose Features**
   - Automatic restart policy
   - Volume persistence
   - Environment variable support
   - UTC timezone setting

### Docker Management Commands

```sh
# Build the container
docker-compose build

# Start the container
docker-compose up -d

# View logs
docker-compose logs -f

# Stop the container
docker-compose down

# Remove volumes (caution: removes data)
docker-compose down -v

# Rebuild and restart
docker-compose up -d --build
```

## Example Calculations

### Price Drop Calculation

Assume the following configuration for daily timeframe:
- Enabled: true
- Thresholds: [0.01, 0.02, 0.03]

If the reference price (open price) for BTCUSDT is $50,000 and the current price drops to $49,000:
- Drop percentage = (50,000 - 49,000) / 50,000 = 0.02 (2%)

The bot will trigger a buy order at the 2% drop threshold.

### Profit Calculation

Assume a trade with the following details:
- Entry price: $49,000
- Quantity: 0.1 BTC
- Current price: $51,000

Gross profit:
- Gross profit = (51,000 - 49,000) * 0.1 = $200

Tax (28%):
- Tax = 200 * 0.28 = $56

Net profit:
- Net profit = 200 - 56 = $144

### Portfolio Summary

Assume the following trades:
- BTCUSDT: 0.1 BTC at $49,000
- ETHUSDT: 1 ETH at $3,000

Current prices:
- BTCUSDT: $51,000
- ETHUSDT: $3,200

Portfolio value:
- BTC value = 0.1 * 51,000 = $5,100
- ETH value = 1 * 3,200 = $3,200
- Total value = $5,100 + $3,200 = $8,300

## 🚧 TODO & Future Features

### Take-Profit Implementation
- [ ] Add overall symbol take-profit functionality
  - Trigger sell when total symbol position profit reaches target
  - Example: Sell 100% when BTC position is +25% in profit
  - Configurable TP levels per symbol
  - Optional partial sells at different levels

### Performance Improvements
- [ ] Implement connection pooling for file operations
  - Faster data handling
  - Better resource management
  - Reduced disk I/O
  - Improved scalability

### WebSocket Enhancements
- [ ] Implement advanced WebSocket reconnection logic
  - Exponential backoff with jitter
  - Multiple fallback endpoints
  - Connection health monitoring
  - Automatic recovery
  - Better error handling

### Risk Management
- [ ] Add position size limits per symbol
- [ ] Implement portfolio exposure limits
- [ ] Add volatility-based position sizing
- [ ] Create risk score system

### Analytics Expansion
- [ ] Add real-time performance metrics
- [ ] Create detailed trade reports
- [ ] Implement win/loss ratio tracking
- [ ] Add ROI calculations per timeframe

### User Experience
- [ ] Add more interactive Telegram commands
- [ ] Create custom keyboard shortcuts
- [ ] Add chart generation
- [ ] Implement customizable alerts

### Monitoring
- [ ] Add system health monitoring
- [ ] Implement error tracking analytics
- [ ] Add performance bottleneck detection
- [ ] Create detailed logging analytics

## Support & Contact

For support or questions:
- Discord: **maskiplays**

## Disclaimer

This bot is for educational purposes only. Trading cryptocurrencies carries significant risks. Use at your own discretion.

## License

MIT License - Feel free to use and modify as needed.