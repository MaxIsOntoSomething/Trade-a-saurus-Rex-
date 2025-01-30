import asyncio
import time
from collections import deque
from ..types.constants import MAX_REQUESTS_PER_MINUTE, REQUEST_WEIGHT_DEFAULT

class RateLimiter:
    def __init__(self, max_requests: int = MAX_REQUESTS_PER_MINUTE):
        self.max_requests = max_requests
        self.requests = deque()
        
    async def acquire(self, weight: int = REQUEST_WEIGHT_DEFAULT):
        """Acquire rate limit permission"""
        now = time.time()
        
        # Remove requests older than 1 minute
        while self.requests and now - self.requests[0] > 60:
            self.requests.popleft()
            
        # If we're at the limit, wait until we can make another request
        if len(self.requests) >= self.max_requests:
            wait_time = 60 - (now - self.requests[0])
            if wait_time > 0:
                await asyncio.sleep(wait_time)
                
        self.requests.append(now)
