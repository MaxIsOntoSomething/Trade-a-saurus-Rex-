import json
import aiofiles
import asyncio
from collections import deque
import os
from datetime import datetime
import fcntl  # For file locking

class FileConnectionPool:
    def __init__(self, max_connections=5):
        self.max_connections = max_connections
        self.connections = deque()
        self.lock = asyncio.Lock()
        self.in_use = set()

    async def get_connection(self, filepath):
        async with self.lock:
            while len(self.in_use) >= self.max_connections:
                await asyncio.sleep(0.1)
            
            try:
                file = await aiofiles.open(filepath, mode='r+')
                self.in_use.add(file)
                return file
            except FileNotFoundError:
                # Create directory if needed
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                file = await aiofiles.open(filepath, mode='w+')
                self.in_use.add(file)
                return file

    async def release_connection(self, file):
        async with self.lock:
            if file in self.in_use:
                self.in_use.remove(file)
                await file.close()

class AsyncFileHandler:
    def __init__(self, pool_size=5):
        self.pool = FileConnectionPool(pool_size)

    async def save_json(self, filepath, data):
        file = None
        try:
            file = await self.pool.get_connection(filepath)
            await file.seek(0)
            await file.truncate()
            await file.write(json.dumps(data, indent=4))
            await file.flush()
        finally:
            if file:
                await self.pool.release_connection(file)

    async def load_json(self, filepath):
        file = None
        try:
            file = await self.pool.get_connection(filepath)
            content = await file.read()
            return json.loads(content) if content else {}
        finally:
            if file:
                await self.pool.release_connection(file)

    async def append_json(self, filepath, data):
        file = None
        try:
            file = await self.pool.get_connection(filepath)
            content = await file.read()
            existing_data = json.loads(content) if content else {}
            existing_data.update(data)
            await file.seek(0)
            await file.truncate()
            await file.write(json.dumps(existing_data, indent=4))
            await file.flush()
        finally:
            if file:
                await self.pool.release_connection(file)

    async def save_json_atomic(self, filepath, data):
        """Save JSON data atomically with file locking"""
        temp_file = f"{filepath}.tmp"
        backup_file = f"{filepath}.bak"
        
        try:
            # Write to temporary file first
            async with aiofiles.open(temp_file, 'w') as f:
                # Acquire exclusive lock
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                await f.write(json.dumps(data, indent=4))
                await f.flush()
                os.fsync(f.fileno())
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            # Create backup of existing file if it exists
            if os.path.exists(filepath):
                os.replace(filepath, backup_file)

            # Atomic rename of temp file to actual file
            os.replace(temp_file, filepath)

            # Remove backup file if everything succeeded
            if os.path.exists(backup_file):
                os.remove(backup_file)

        except Exception as e:
            # Restore from backup if something went wrong
            if os.path.exists(backup_file):
                os.replace(backup_file, filepath)
            raise e
        finally:
            # Cleanup temp file if it still exists
            if os.path.exists(temp_file):
                os.remove(temp_file)
