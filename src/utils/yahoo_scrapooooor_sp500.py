import aiohttp
import logging
import json
from datetime import datetime, timedelta
import time
from typing import Dict, Optional
import asyncio

logger = logging.getLogger(__name__)

class YahooSP500Scraper:
    """Scrapes S&P 500 historical data from Yahoo Finance"""
    
    def __init__(self, rate_limit_delay: int = 2):
        """Initialize the scraper with optional rate limiting"""
        self.rate_limit_delay = rate_limit_delay
        self.user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        self._last_request_time = 0
    
    async def _respect_rate_limit(self):
        """Ensure we don't exceed Yahoo's rate limits"""
        now = time.time()
        elapsed = now - self._last_request_time
        
        if elapsed < self.rate_limit_delay:
            delay = self.rate_limit_delay - elapsed
            logger.debug(f"Rate limiting: sleeping for {delay:.2f} seconds")
            await asyncio.sleep(delay)
            
        self._last_request_time = time.time()
    
    async def get_sp500_data(self, days: int = 90) -> Dict[str, float]:
        """
        Fetch S&P 500 historical data from Yahoo Finance
        Returns a dictionary of dates and ROI percentages relative to first day
        """
        try:
            await self._respect_rate_limit()
            
            # Calculate period parameters
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days+5)  # Add buffer for weekends
            
            # Convert to Unix timestamps (seconds)
            period1 = int(start_date.timestamp())
            period2 = int(end_date.timestamp())
            
            # Yahoo Finance API URL
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"
            
            # Parameters for request (emulating browser request)
            params = {
                "period1": period1,
                "period2": period2,
                "interval": "1d",
                "events": "history"
            }
            
            # Enhanced headers to avoid 406 errors (Not Acceptable)
            headers = {
                "User-Agent": self.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Origin": "https://finance.yahoo.com",
                "Referer": "https://finance.yahoo.com/quote/%5EGSPC",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site",
                "Connection": "keep-alive"
            }
            
            # Try different request methods if first one fails
            async with aiohttp.ClientSession() as session:
                # First attempt with primary URL
                async with session.get(url, params=params, headers=headers) as response:
                    if response.status == 200:
                        # Parse the JSON response
                        data = await response.json()
                        result = self._process_yahoo_data(data, days)
                        
                        if result:
                            logger.info(f"Successfully retrieved {len(result)} days of S&P 500 data from Yahoo Finance")
                            return result
                    
                    # If we get a 406 error, try the alternative API endpoint
                    if response.status == 406:
                        logger.warning("Received 406 error from Yahoo Finance, trying alternative endpoint")
                        
                        # Alternative URL approach - use Yahoo's query2 endpoint
                        alt_url = f"https://query2.finance.yahoo.com/v8/finance/chart/%5EGSPC"
                        
                        # Wait before retry
                        await asyncio.sleep(1)
                        
                        async with session.get(alt_url, params=params, headers=headers) as alt_response:
                            if alt_response.status == 200:
                                data = await alt_response.json()
                                result = self._process_yahoo_data(data, days)
                                
                                if result:
                                    logger.info(f"Successfully retrieved {len(result)} days of S&P 500 data from Yahoo Finance (alternative endpoint)")
                                    return result
                            else:
                                logger.error(f"Alternative Yahoo Finance endpoint also failed with status code {alt_response.status}")
                    else:
                        logger.error(f"Yahoo Finance returned status code {response.status}")
            
            # If we reach here, both attempts failed
            logger.error("All attempts to retrieve S&P 500 data failed")
            return {}
                    
        except Exception as e:
            logger.error(f"Error fetching S&P 500 data from Yahoo Finance: {e}", exc_info=True)
            return {}
    
    def _process_yahoo_data(self, data: Dict, days_needed: int) -> Dict[str, float]:
        """Process Yahoo Finance JSON data into a standardized ROI format"""
        try:
            # Extract time series data
            result_data = data.get('chart', {}).get('result', [])
            
            if not result_data:
                logger.error("No data found in Yahoo Finance response")
                return {}
                
            quotes = result_data[0]
            timestamps = quotes.get('timestamp', [])
            
            # Get adjusted close prices (accounts for splits and dividends)
            indicators = quotes.get('indicators', {})
            adjclose = indicators.get('adjclose', [])
            
            if not adjclose or not timestamps:
                logger.error("Missing price or timestamp data in Yahoo Finance response")
                return {}
                
            prices = adjclose[0].get('adjclose', [])
            
            if not prices or len(prices) != len(timestamps):
                logger.error(f"Data length mismatch: {len(prices)} prices vs {len(timestamps)} timestamps")
                return {}
            
            # Create a dictionary of dates and prices
            price_dict = {}
            for ts, price in zip(timestamps, prices):
                if price is None:  # Skip any null values
                    continue
                dt = datetime.fromtimestamp(ts)
                date_str = dt.strftime('%Y-%m-%d')
                price_dict[date_str] = price
            
            # Sort dates
            dates = sorted(price_dict.keys())
            
            if not dates:
                return {}
            
            # Use first day's price as base for ROI calculation
            base_price = price_dict[dates[0]]
            result = {}
            
            # Calculate ROI for each day relative to first day
            for date in dates:
                current_price = price_dict[date]
                roi = ((current_price - base_price) / base_price) * 100
                result[date] = roi
            
            # Limit to the number of days needed
            if len(result) > days_needed:
                # Keep only the most recent days_needed days
                dates = sorted(result.keys())
                dates_to_keep = dates[-days_needed:]
                result = {date: result[date] for date in dates_to_keep}
            
            return result
            
        except Exception as e:
            logger.error(f"Error processing Yahoo Finance data: {e}", exc_info=True)
            return {}

    async def get_sp500_daily_change(self) -> Optional[float]:
        """Get the current day's S&P 500 percentage change"""
        try:
            await self._respect_rate_limit()
            
            url = "https://query1.finance.yahoo.com/v7/finance/quote"
            params = {"symbols": "^GSPC"}
            
            # Enhanced headers to avoid 406 errors
            headers = {
                "User-Agent": self.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Origin": "https://finance.yahoo.com",
                "Referer": "https://finance.yahoo.com/quote/%5EGSPC"
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers) as response:
                    if response.status != 200:
                        logger.error(f"Failed to get S&P 500 daily change: {response.status}")
                        return None
                    
                    data = await response.json()
                    quotes = data.get('quoteResponse', {}).get('result', [])
                    
                    if not quotes:
                        return None
                    
                    # Get percentage change
                    return quotes[0].get('regularMarketChangePercent')
                    
        except Exception as e:
            logger.error(f"Error getting S&P 500 daily change: {e}")
            return None
