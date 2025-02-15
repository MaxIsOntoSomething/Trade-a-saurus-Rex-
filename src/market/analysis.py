import asyncio
from datetime import datetime, timedelta
from pytrends.request import TrendReq
from google_play_scraper import app
import numpy as np
from typing import Dict, List, Tuple
import logging
import httpx
import json

logger = logging.getLogger(__name__)

class MarketAnalyzer:
    def __init__(self):
        self.pytrends = TrendReq(hl='en-US', tz=360)
        self.crypto_apps = {
            'Binance': {
                'ios_id': '1436799971',
                'android': 'com.binance.dev'
            },
            'Coinbase': {
                'ios_id': '886427730',
                'android': 'com.coinbase.android'
            },
            'KuCoin': {
                'ios_id': '1378956601',
                'android': 'com.kubi.kucoin'
            },
            'Phantom': {
                'ios_id': '1598432977',
                'android': 'app.phantom.mobile'
            },
            'OKX': {
                'ios_id': '1327268470',
                'android': 'com.okex.android'
            },
            'Bitget': {
                'ios_id': '1442778704',
                'android': 'com.bitget.global'
            }
        }
        self.session = httpx.AsyncClient(timeout=30.0)
        self.itunes_base_url = "https://itunes.apple.com/lookup"

    async def get_google_trends(self, symbol: str) -> Dict:
        """Get Google Trends data for crypto"""
        try:
            # Build payload
            kw_list = [f"{symbol} crypto", "bitcoin", "cryptocurrency"]
            self.pytrends.build_payload(kw_list, timeframe='now 7-d')
            
            # Get interest over time
            interest_df = self.pytrends.interest_over_time()
            
            if interest_df.empty:
                return {
                    "trend_score": 0,
                    "relative_interest": "Low",
                    "trend_direction": "Neutral"
                }

            # Calculate metrics
            current = interest_df[f"{symbol} crypto"].iloc[-1]
            avg = interest_df[f"{symbol} crypto"].mean()
            direction = "Up" if current > avg else "Down"
            
            # Calculate relative interest
            relative_score = current / interest_df["bitcoin"].iloc[-1] * 100
            interest_level = "High" if relative_score > 50 else "Medium" if relative_score > 25 else "Low"

            return {
                "trend_score": int(current),
                "relative_interest": interest_level,
                "trend_direction": direction
            }

        except Exception as e:
            logger.error(f"Error getting Google Trends: {e}")
            return {
                "trend_score": 0,
                "relative_interest": "Error",
                "trend_direction": "Unknown"
            }

    async def get_app_store_info(self, app_id: str) -> dict:
        """Get iOS app information using iTunes Search API"""
        try:
            params = {
                'id': app_id,
                'country': 'us',
                'entity': 'software'
            }
            response = await self.session.get(self.itunes_base_url, params=params)
            response.raise_for_status()
            data = response.json()
            
            if data['resultCount'] > 0:
                app_data = data['results'][0]
                return {
                    'rating': app_data.get('averageUserRating', 0),
                    'reviews': app_data.get('userRatingCount', 0),
                    'last_updated': app_data.get('currentVersionReleaseDate', 'Unknown')
                }
            return {'rating': 0, 'reviews': 0, 'last_updated': 'Unknown'}
            
        except Exception as e:
            logger.error(f"Failed to get App Store data: {e}")
            return {'rating': 0, 'reviews': 0, 'last_updated': 'Error'}

    async def get_app_rankings(self) -> Dict:
        """Get app rankings from both stores"""
        try:
            rankings = {}
            
            # Process each app
            for app_name, app_ids in self.crypto_apps.items():
                try:
                    # Get iOS data using iTunes API
                    ios_data = await self.get_app_store_info(app_ids['ios_id'])
                    
                    # Get Android data using existing method
                    android_details = app(
                        app_ids['android'],
                        lang='en',
                        country='us'
                    )
                    
                    rankings[app_name] = {
                        'ios_rating': ios_data['rating'],
                        'ios_reviews': ios_data['reviews'],
                        'android_rating': android_details['score'],
                        'android_installs': android_details['installs'],
                        'last_updated': datetime.now().strftime('%Y-%m-%d')
                    }
                    
                except Exception as app_error:
                    logger.error(f"Error fetching {app_name} data: {app_error}")
                    continue
                    
                # Add small delay between requests
                await asyncio.sleep(0.5)
                
            return rankings
            
        except Exception as e:
            logger.error(f"Error getting app rankings: {e}")
            return {}

    async def cleanup(self):
        """Cleanup resources"""
        await self.session.aclose()

    def calculate_ichimoku(self, prices: List[float]) -> Dict:
        """Calculate Ichimoku Cloud indicators"""
        try:
            # Convert prices to numpy array
            prices = np.array(prices)
            
            # Calculate Ichimoku components
            tenkan_sen = self._calculate_ichimoku_line(prices, 9)
            kijun_sen = self._calculate_ichimoku_line(prices, 26)
            
            # Calculate Cloud spans
            senkou_span_a = (tenkan_sen + kijun_sen) / 2
            senkou_span_b = self._calculate_ichimoku_line(prices, 52)
            
            # Get current price
            current_price = prices[-1]
            
            # Determine position relative to cloud
            above_cloud = current_price > max(senkou_span_a[-1], senkou_span_b[-1])
            below_cloud = current_price < min(senkou_span_a[-1], senkou_span_b[-1])
            
            # Determine trend strength
            trend_strength = "Strong" if abs(senkou_span_a[-1] - senkou_span_b[-1]) > (current_price * 0.02) else "Weak"
            
            return {
                "position": "Above Cloud" if above_cloud else "Below Cloud" if below_cloud else "In Cloud",
                "trend": "Bullish" if above_cloud else "Bearish" if below_cloud else "Neutral",
                "strength": trend_strength,
                "tenkan_sen": float(tenkan_sen[-1]),
                "kijun_sen": float(kijun_sen[-1])
            }
            
        except Exception as e:
            logger.error(f"Error calculating Ichimoku: {e}")
            return {
                "position": "Error",
                "trend": "Unknown",
                "strength": "Unknown",
                "tenkan_sen": 0,
                "kijun_sen": 0
            }

    def _calculate_ichimoku_line(self, prices: np.ndarray, period: int) -> np.ndarray:
        """Helper method to calculate Ichimoku lines"""
        highs = np.array([max(prices[i:i+period]) for i in range(len(prices)-period+1)])
        lows = np.array([min(prices[i:i+period]) for i in range(len(prices)-period+1)])
        return (highs + lows) / 2
