import json
import aiofiles
import asyncio
from collections import deque
import os
from datetime import datetime
import shutil
import portalocker  # Cross-platform file locking
import platform

class FileConnectionPool:
    def __init__(self, max_connections=5):
        self.max_connections = max_connections
        self.connections = deque()
        self.lock = asyncio.Lock()
        self.in_use = set()

    async def get_connection(self, filepath):
        async with self.lock:
            while (len(self.in_use) >= self.max_connections):
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
        self.backup_dir = os.path.join('data', 'backups')
        os.makedirs(self.backup_dir, exist_ok=True)
        self.is_windows = platform.system() == 'Windows'

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
        """Load JSON with Windows-compatible file locking"""
        try:
            if not os.path.exists(filepath):
                return {}

            async with aiofiles.open(filepath, 'r') as f:
                with portalocker.Lock(filepath, 'r'):
                    content = await f.read()
                    return json.loads(content) if content else {}

        except json.JSONDecodeError as e:
            # Try to recover from backup
            backup_file = f"{filepath}.bak"
            if os.path.exists(backup_file):
                async with aiofiles.open(backup_file, 'r') as f:
                    content = await f.read()
                    return json.loads(content)
            raise e

    async def append_json(self, filepath, data):
        """Append to JSON file atomically"""
        try:
            existing_data = await self.load_json(filepath)
            existing_data.update(data)
            await self.save_json_atomic(filepath, existing_data)
        except Exception as e:
            raise e

    async def save_json_atomic(self, filepath, data):
        """Save JSON data atomically with cross-platform file locking"""
        temp_file = f"{filepath}.tmp"
        backup_file = f"{filepath}.bak"
        
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(filepath), exist_ok=True)

            # First write to temporary file
            async with aiofiles.open(temp_file, 'w') as f:
                # Use cross-platform file locking
                with portalocker.Lock(temp_file, 'w'):
                    await f.write(json.dumps(data, indent=4))
                    await f.flush()
                    os.fsync(f.fileno())

            # Platform-specific atomic file operations
            try:
                if os.path.exists(filepath):
                    shutil.copy2(filepath, backup_file)
                os.replace(temp_file, filepath)
            except OSError:
                # Fallback for platforms that don't support atomic replace
                if os.path.exists(filepath):
                    os.remove(filepath)
                shutil.move(temp_file, filepath)

            # Create timestamped backup
            self._create_backup(filepath)

        except Exception as e:
            # Restore from backup if something went wrong
            if os.path.exists(backup_file):
                try:
                    os.replace(backup_file, filepath)
                except OSError:
                    shutil.copy2(backup_file, filepath)
            raise e
        finally:
            # Cleanup
            self._cleanup_files(temp_file, backup_file)

    def _cleanup_files(self, *files):
        """Safely remove files"""
        for file in files:
            try:
                if os.path.exists(file):
                    os.remove(file)
            except OSError:
                pass

    def _create_backup(self, filepath):
        """Create and manage timestamped backups"""
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_name = f"{os.path.basename(filepath)}.{timestamp}"
            backup_path = os.path.join(self.backup_dir, backup_name)
            
            shutil.copy2(filepath, backup_path)
            
            # Keep only last 5 backups
            backups = sorted([
                f for f in os.listdir(self.backup_dir) 
                if f.startswith(os.path.basename(filepath))
            ])
            if len(backups) > 5:
                for old_backup in backups[:-5]:
                    try:
                        os.remove(os.path.join(self.backup_dir, old_backup))
                    except OSError:
                        pass
        except Exception as e:
            print(f"Warning: Backup creation failed: {e}")

    def validate_json_structure(self, data):
        """Validate the JSON structure matches our trade format"""
        for trade_id, trade_data in data.items():
            if not isinstance(trade_data, dict):
                return False
            if 'trade_info' not in trade_data or 'order_metadata' not in trade_data:
                return False
            # Add more validation as needed
        return True
