from collections import deque
import time
import asyncio

class RateLimiter:
    def __init__(self, max_requests: int, time_window: int = 60):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = deque()

    async def acquire(self):
        """Wait if necessary to stay under rate limit"""
        current_time = time.time()
        
        # Remove old requests
        while self.requests and self.requests[0] < current_time - self.time_window:
            self.requests.popleft()
        
        # If at limit, wait until oldest request expires
        if len(self.requests) >= self.max_requests:
            wait_time = self.requests[0] + self.time_window - current_time
            if wait_time > 0:
                await asyncio.sleep(wait_time)
        
        # Add current request
        self.requests.append(current_time)
