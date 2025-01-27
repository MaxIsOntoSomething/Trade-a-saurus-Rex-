import json
import aiofiles
import asyncio
from collections import deque
import os
from datetime import datetime
import shutil
import portalocker  # Cross-platform file locking
import platform
import stat
import logging

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
        self.logger = logging.getLogger('FileHandler')  # Add logger initialization

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
        """Save JSON data atomically with cross-platform support"""
        temp_file = f"{filepath}.tmp"
        backup_file = f"{filepath}.bak"
        
        try:
            # Ensure directory exists with cross-platform permissions
            directory = os.path.dirname(filepath)
            if not os.path.exists(directory):
                try:
                    # Use more permissive mode for Linux
                    os.makedirs(directory, mode=0o775, exist_ok=True)
                except Exception as e:
                    self.logger.error(f"Directory creation failed: {e}")
                    # Fallback to basic creation
                    os.makedirs(directory, exist_ok=True)
                    
            # Set file permissions based on platform
            default_mode = 0o664 if os.name != 'nt' else 0o666
            
            # Write to temporary file with proper permissions
            async with aiofiles.open(temp_file, 'w') as f:
                with portalocker.Lock(temp_file, 'w', flags=portalocker.LOCK_EX):
                    await f.write(json.dumps(data, indent=4))
                    await f.flush()
                    if hasattr(os, 'fsync'):
                        os.fsync(f.fileno())
            
            try:
                os.chmod(temp_file, default_mode)
            except Exception as e:
                self.logger.warning(f"Could not set temp file permissions: {e}")

            # Create backup if original exists
            if os.path.exists(filepath):
                try:
                    shutil.copy2(filepath, backup_file)
                    os.chmod(backup_file, default_mode)
                except Exception as e:
                    self.logger.warning(f"Backup creation failed: {e}")

            # Perform atomic replace
            try:
                if os.name == 'nt':
                    # Windows needs special handling
                    if os.path.exists(filepath):
                        os.replace(filepath, backup_file)
                    os.replace(temp_file, filepath)
                else:
                    # Unix systems can do atomic rename
                    os.rename(temp_file, filepath)
                
                # Set permissions on final file
                os.chmod(filepath, default_mode)
                
            except Exception as e:
                self.logger.error(f"Atomic replace failed: {e}")
                # Fallback to non-atomic copy
                shutil.copy2(temp_file, filepath)
                os.chmod(filepath, default_mode)

        except Exception as e:
            self.logger.error(f"Error saving file {filepath}: {e}")
            # Try to restore from backup
            if os.path.exists(backup_file):
                try:
                    shutil.copy2(backup_file, filepath)
                    os.chmod(filepath, default_mode)
                except Exception as restore_error:
                    self.logger.error(f"Backup restoration failed: {restore_error}")
            raise
        finally:
            # Cleanup temporary files with error handling
            for file_path in [temp_file, backup_file]:
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                except Exception as e:
                    self.logger.warning(f"Cleanup failed for {file_path}: {e}")

    def _ensure_directory_permissions(self, directory):
        """Set directory permissions with platform awareness"""
        try:
            if not os.path.exists(directory):
                if os.name == 'nt':
                    os.makedirs(directory, exist_ok=True)
                else:
                    # More restrictive permissions for Linux
                    os.makedirs(directory, mode=0o775, exist_ok=True)
            elif os.name != 'nt':
                # Set proper group permissions on Linux
                os.chmod(directory, 0o775)
        except Exception as e:
            self.logger.warning(f"Directory permission setup failed: {e}")

    def _ensure_file_permissions(self, filepath):
        """Ensure file has correct permissions"""
        try:
            if os.path.exists(filepath):
                current_mode = stat.S_IMODE(os.stat(filepath).st_mode)
                if not current_mode & stat.S_IWRITE:
                    os.chmod(filepath, 0o644)
        except Exception as e:
            self.logger.warning(f"Could not check/fix permissions for {filepath}: {e}")

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
