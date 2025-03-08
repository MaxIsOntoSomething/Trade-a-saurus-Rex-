import aiohttp
import logging
import json
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

class YahooFinanceSP500Scraper:
    """A scraper class to fetch S&P 500 historical data from Yahoo Finance"""
    
    def __init__(self):
        self.base_url = "https://query1.finance.yahoo.com/v8/finance/chart"
        self.symbol = "%5EGSPC"  # ^GSPC (S&P 500 index)
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    
    async def get_sp500_data(self, days: int = 90) -> Dict[str, float]:
        """Fetch S&P 500 data for the specified number of days and return as a date->ROI dictionary
        
        Args:
            days: Number of days of historical data to fetch
            
        Returns:
            Dictionary with dates as keys and ROI percentage values
        """
        try:
            # Calculate start and end dates
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days+5)  # Add buffer days for non-trading days
            
            # Convert to Unix timestamps (seconds)
            period1 = int(start_date.timestamp())
            period2 = int(end_date.timestamp())
            
            # Build URL with parameters
            params = {
                "symbol": self.symbol,
                "period1": period1,
                "period2": period2,
                "interval": "1d",  # Daily data
                "includePrePost": "false",
                "events": "history"
            }
            
            # Headers to mimic a browser request
            headers = {
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1"
            }
            
            # Make async request to Yahoo Finance API
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/{self.symbol}", 
                    params=params, 
                    headers=headers
                ) as response:
                    if response.status != 200:
                        logger.error(f"Yahoo Finance API error: {response.status}")
                        return {}
                    
                    data = await response.text()
                    json_data = json.loads(data)
                    
                    # Extract price data
                    result = self._process_yahoo_data(json_data, days)
                    logger.info(f"Successfully fetched {len(result)} days of S&P 500 data from Yahoo Finance")
                    return result
                    
        except Exception as e:
            logger.error(f"Error fetching S&P 500 data: {e}", exc_info=True)
            return {}
    
    def _process_yahoo_data(self, json_data: dict, days: int) -> Dict[str, float]:
        """Process the raw Yahoo Finance data into the required format
        
        Args:
            json_data: Raw JSON data from Yahoo Finance
            days: Number of days to include in the result
            
        Returns:
            Dictionary with dates as keys and ROI percentage as values
        """
        try:
            # Extract required data from nested JSON
            chart_data = json_data.get('chart', {}).get('result', [])
            if not chart_data:
                logger.error("No chart data found in Yahoo Finance response")
                return {}
            
            # Get timestamps and close prices
            timestamps = chart_data[0].get('timestamp', [])
            close_prices = chart_data[0].get('indicators', {}).get('quote', [{}])[0].get('close', [])
            
            if not timestamps or not close_prices:
                logger.error("Missing timestamp or price data in Yahoo Finance response")
                return {}
                
            # Create DataFrame for easier date handling
            df = pd.DataFrame({
                'timestamp': timestamps,
                'close': close_prices
            })
            
            # Convert timestamps to dates
            df['date'] = pd.to_datetime(df['timestamp'], unit='s').dt.strftime('%Y-%m-%d')
            
            # Remove rows with NaN values
            df = df.dropna()
            
            # Limit to the most recent 'days' number of days
            if len(df) > days:
                df = df.tail(days)
                
            # Calculate ROI based on the first day in the dataset
            base_price = df.iloc[0]['close']
            result = {}
            
            for _, row in df.iterrows():
                if pd.isna(row['close']):
                    continue
                    
                date = row['date']
                price = row['close']
                roi = ((price - base_price) / base_price) * 100
                result[date] = float(roi)
                
            return result
            
        except Exception as e:
            logger.error(f"Error processing Yahoo Finance data: {e}", exc_info=True)
            return {}
