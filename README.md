```
          ___                                      .-~. /_"-._
        `-._~-.                                  / /_ "~o\  :Y
              \  \                                / : \~x.  ` ')
              ]  Y                              /  |  Y< ~-.__j
             /   !                        _.--~T : l  l<  /.-~
            /   /                 ____.--~ .   ` l /~\ \<|Y
           /   /             .-~~"        /| .    ',-~\ \L|
          /   /             /     .^   \ Y~Y \.^>/l_   "--'
         /   Y           .-"(  .  l__  j_j l_/ /~_.-~    .
        Y    l          /    \  )    ~~~." / `/"~ / \.__/l_
        |     \     _.-"      ~-{__     l  :  l._Z~-.___.--~
        |      ~---~           /   ~~"---\_  ' __[>
        l  .                _.^   ___     _>-y~
         \  \     .      .-~   .-~   ~>--"  /
          \  ~---"            /     ./  _.-'
           "-.,_____.,_  _.--~\     _.-~
                       ~~     (   _}       
                              `. ~(
                                )  \
                          /,`--'~\--'~\
                          ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                         Trade-a-saurus Rex 🦖📈
```

# Trade-a-saurus Rex

A personal hobby project - An automated cryptocurrency trading bot for Binance that monitors price drops across multiple timeframes.

## 🦖 What is Trade-a-saurus Rex?

### Spot Trading Features
- 🎯 Automatic buy order placement based on price thresholds
- 📉 Lower entry price protection
- 💼 Portfolio management and tracking
- 🔍 Order history and performance tracking

### Futures Trading Features (New!)
- 📊 Dedicated Futures trading mode with separate commands
- 🔧 Adjustable leverage (1x to 5x)
- 💫 Support for both Isolated and Cross margin modes
- 📈 Long and Short position support
- 🎯 Take Profit and Stop Loss automation
- ⚠️ Margin and liquidation warnings
- 💰 PnL tracking and reporting
- 🔄 Position monitoring and automatic closure
- 🏦 Separate Futures wallet management

### Risk Management Features
- ⚖️ Reserve balance protection
- 🎯 Automated Take Profit and Stop Loss (TP/SL)
- ⚠️ Margin level monitoring
- 🚨 Liquidation price alerts
- 💹 Real-time PnL tracking
- 📊 Risk exposure monitoring

## Prerequisites

### Testing and Development
- 🧪 Support for both Spot and Futures Testnet
- 🔑 Separate API keys for Spot and Futures testing
- 🔄 Easy switching between test and production environments
- 📝 Comprehensive logging and error handling

## Configuration

### Environment Variables
Create a `.env` file based on `.env.example` with your configuration:

```env
# Binance Configuration
BINANCE_SPOT_TESTNET_API_KEY=your_testnet_api_key_here
BINANCE_SPOT_TESTNET_API_SECRET=your_testnet_api_secret_here
BINANCE_FUTURES_TESTNET_API_KEY=your_futures_testnet_api_key_here
BINANCE_FUTURES_TESTNET_API_SECRET=your_futures_testnet_api_secret_here
BINANCE_MAINNET_API_KEY=your_mainnet_api_key_here
BINANCE_MAINNET_API_SECRET=your_mainnet_api_secret_here
BINANCE_USE_TESTNET=true

# Other configurations...
```

- `/start` - Start the bot and show welcome message
- `/power` - Toggle trading on/off
- `/balance` - Check current balance
- `/stats` - Trading statistics
- `/profits` - P/L analysis with tax calculations 
- `/history` - View recent order history
- `/thresholds` - Show threshold status and resets
- `/add` - Add manual trade (interactive)
- `/resetthresholds` - Reset all thresholds across timeframes
- `/viz` - Show data visualizations (volume, profit, balance charts)
- `/menu` - Show all available commands

```json
{
    "binance": {
        "spot_testnet": {
            "api_key": "your_testnet_api_key",
            "api_secret": "your_testnet_api_secret"
        },
        "futures_testnet": {
            "api_key": "your_futures_testnet_api_key",
            "api_secret": "your_futures_testnet_api_secret"
        },
        "mainnet": {
            "api_key": "your_mainnet_api_key",
            "api_secret": "your_mainnet_api_secret"
        },
        "use_testnet": true
    }
    // Other configurations...
}
```

- **Enhanced Data Persistence**: All thresholds and reference prices are now stored in MongoDB for reliable recovery after restarts
- **Threshold Restoration**: Bot now properly restores triggered thresholds after a restart
- **Improved Visualization Tools**: Added balance history charts and improved trade visualizations
- **Better Error Handling**: Comprehensive error handling throughout the system
- **Fixed Format Strings**: Resolved formatting issues in notifications
- **Optimized MongoDB Queries**: More efficient and robust database operations
- **Reserve Balance Protection**: Enhanced reserve balance protection to prevent over-trading
- **Command Improvements**: Added `/resetthresholds` command for manual reset

## Portfolio Analysis

The bot provides detailed portfolio analysis including:
- Entry/exit points
- Tax-adjusted profits
- Fee calculations
- Multi-timeframe performance
- Balance history tracking

## Docker Support

The bot can run in Docker for improved stability and easier deployment on servers:

```bash
docker-compose up -d
```

## Note on Drop Analysis

At the bottom of this repository, you'll find historical price drop analyses for BTC. These analyses can help you optimize the threshold settings in your config file for better trading results.

## Disclaimer

This is a hobby project and should not be used for serious trading without thorough testing. Trade at your own risk!

## License

MIT License


## ✅ TODO  

### 🚀 Work in Progress  
- ⚙️ **Futures Integration** ⏳ (In Progress)  
- 🔗 **Bybit Integration** ⏳ (In Progress)  
- 📈 **Binance Futures Trading** ⏳ (In Progress)  
  - 🔧 Update config to support three Binance API keys (Mainnet Spot & Futures, Testnet Spot, Testnet Futures).  
  - 📝 Implement Binance Futures trading in a separate Python file.  
  - 🔄 Adjust Telegram commands to support Futures mode.  
  - 📊 Modify database to store Binance Futures trades.  
  - 📉 Update chart generation for Futures trades.  
  - 🎯 Introduce percentage-based Take Profit (TP) and Stop Loss (SL) system.  
  - 📉 Add "Only Lower Entries" setting to prevent increasing average entry price.  
  - ⚙️ Allow leverage and margin mode configuration via config/env variables.  
  - 🛠️ Implement three new Telegram commands:  
    - 📌 **Leverage Command** (Max 5x)  
    - 📌 **Margin Mode Command** (Switch between Isolated & Cross)  
    - 📌 **Order Amount Command** (Min 10 USDT per order)  

### ✅ Finished  
- ✅ **Telegram - Open Orders Command**  
- ✅ **Reserve Balance**  
- ✅ **Chart Generation**  
- ✅ **Reset Updates Send**  
- ✅ **MongoDB Integration**  
- ✅ **Migrating from SQLite**  
- ✅ **Docker Support**  
- ✅ **Threshold Persistence**  
- ✅ **Balance History Charts**  
- ✅ **Format String Fixes**  

### 📌 Planned  
- 📌 **Weekly & Monthly Summary (Optional)**  
- 📌 **Hyperliquid Integration**  
- 📌 **Backtest Option to Test Performance**  
- 📌 **Report with real Balance 5k+**  
- 📌 **More Detailed Explanation of Bot**

 Readme**

## Analysis

![Drop Analysis Daily Candles](src/img/data1.png)
![Drop Analysis Weekly Candles](src/img/data2.png)
![Drop Analysis Monthly Candles](src/img/data3.png)
