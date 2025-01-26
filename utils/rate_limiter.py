from collections import deque
import time
import asyncio

class RateLimiter:
    def __init__(self, max_requests: int, time_window: int = 60):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = deque()
        self.lock = asyncio.Lock()  # Add lock for thread safety

    async def acquire(self):
        """Wait if necessary to stay under rate limit with visual feedback"""
        async with self.lock:  # Ensure thread safety
            current_time = time.time()
            
            # Remove old requests outside the time window
            while self.requests and self.requests[0] < current_time - self.time_window:
                self.requests.popleft()
            
            # If at limit, show waiting animation
            if len(self.requests) >= self.max_requests:
                wait_time = self.requests[0] + self.time_window - current_time
                if wait_time > 0:
                    spinner = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
                    start_wait = time.time()
                    while time.time() - start_wait < wait_time:
                        spin = spinner[int((time.time() - start_wait) * 10) % len(spinner)]
                        remaining = wait_time - (time.time() - start_wait)
                        print(f"\r{spin} Rate limit reached. Waiting {remaining:.1f}s... ", end='', flush=True)
                        await asyncio.sleep(0.1)
                    print("\r⏱️  Rate limit reset                    ")
            
            # Add current request
            self.requests.append(current_time)
