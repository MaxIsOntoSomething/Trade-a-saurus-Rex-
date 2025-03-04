import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import codecs
from datetime import datetime
import shutil
from datetime import datetime, timedelta
import glob

# Create custom logger levels
BALANCE_CHECK = 25
RESERVE_CHECK = 26
CONFIG_CHECK = 27  # Add new level for config logging

logging.addLevelName(BALANCE_CHECK, 'BALANCE_CHECK')
logging.addLevelName(RESERVE_CHECK, 'RESERVE_CHECK')
logging.addLevelName(CONFIG_CHECK, 'CONFIG')  # Add config level

class ConfigLogger:
    """Custom logger for configuration events"""
    def __init__(self, logger):
        self.logger = logger

    def log_config(self, message: str):
        self.logger.log(CONFIG_CHECK, f"[CONFIG] {message}")

class WindowsConsoleHandler(logging.StreamHandler):
    """Custom handler for Windows console that handles encoding properly"""
    def __init__(self):
        if sys.platform == 'win32':
            # Configure Windows console to use utf-8
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            stream = codecs.getwriter('utf-8')(sys.stdout.buffer)
        else:
            stream = sys.stdout
        super().__init__(stream)

class CleanupRotatingFileHandler(RotatingFileHandler):
    """Enhanced RotatingFileHandler that cleans up old log files"""
    def __init__(self, filename, mode='a', maxBytes=0, backupCount=0, 
                 encoding=None, delay=False, errors=None, max_age_days=None):
        super().__init__(filename, mode, maxBytes, backupCount, encoding, delay, errors)
        self.max_age_days = max_age_days
        self.cleanup_old_logs()
        
    def doRollover(self):
        """Override doRollover to clean up old logs after rotation"""
        super().doRollover()
        self.cleanup_old_logs()
        
    def cleanup_old_logs(self):
        """Remove log files that exceed the maximum count or are older than max_age_days"""
        if not self.max_age_days:
            return
            
        try:
            # Get the base path and pattern for the log files
            base_path = Path(self.baseFilename)
            log_dir = base_path.parent
            base_name = base_path.stem
            extension = base_path.suffix
            pattern = f"{base_name}.*{extension}"
            
            # Find all log files matching the pattern
            log_files = list(log_dir.glob(pattern))
            
            # Find files older than max_age_days
            cutoff_date = datetime.now() - timedelta(days=self.max_age_days)
            
            # Check each file's modification time
            for log_file in log_files:
                try:
                    # Get file modification time
                    mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
                    if mtime < cutoff_date:
                        os.remove(log_file)
                        logging.getLogger().info(f"Removed old log file: {log_file}")
                except Exception as e:
                    logging.getLogger().error(f"Error checking/removing log file {log_file}: {e}")
        except Exception as e:
            logging.getLogger().error(f"Error in log file cleanup: {e}")

def cleanup_log_directory(log_dir=None, max_size_mb=500, min_free_space_mb=1000):
    """Clean up the entire log directory if it gets too large or disk space is low"""
    if log_dir is None:
        log_dir = Path('logs')
    
    try:
        # Check if directory exists
        if not log_dir.exists():
            return
            
        # Calculate total size of log directory
        total_size_bytes = sum(f.stat().st_size for f in log_dir.glob('**/*') if f.is_file())
        total_size_mb = total_size_bytes / (1024 * 1024)
        
        # Check free space on the disk
        if sys.platform == 'win32':
            import ctypes
            free_bytes = ctypes.c_ulonglong(0)
            ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(str(log_dir)), None, None, ctypes.pointer(free_bytes))
            free_space_mb = free_bytes.value / (1024 * 1024)
        else:
            import shutil
            free_space_mb = shutil.disk_usage(str(log_dir)).free / (1024 * 1024)
        
        # Log current space usage
        logger = logging.getLogger()
        logger.info(f"Log directory size: {total_size_mb:.2f} MB, Free disk space: {free_space_mb:.2f} MB")
        
        # If either condition is met, clean up old logs
        if total_size_mb > max_size_mb or free_space_mb < min_free_space_mb:
            logger.warning(f"Log directory cleanup triggered: size={total_size_mb:.2f}MB, free space={free_space_mb:.2f}MB")
            
            # Get all log files sorted by modification time (oldest first)
            log_files = sorted(
                [f for f in log_dir.glob('**/*') if f.is_file()],
                key=lambda f: f.stat().st_mtime
            )
            
            # Remove oldest files until we're below the threshold or have removed half the files
            files_to_remove = max(len(log_files) // 2, 1)
            for i, log_file in enumerate(log_files):
                if i >= files_to_remove:
                    break
                    
                try:
                    os.remove(log_file)
                    logger.info(f"Removed old log file during cleanup: {log_file}")
                except Exception as e:
                    logger.error(f"Failed to remove log file {log_file}: {e}")
            
            # Calculate new total size
            new_total_bytes = sum(f.stat().st_size for f in log_dir.glob('**/*') if f.is_file())
            new_total_mb = new_total_bytes / (1024 * 1024)
            logger.info(f"Log directory cleanup complete. New size: {new_total_mb:.2f} MB")
    except Exception as e:
        logging.getLogger().error(f"Error in log directory cleanup: {e}")

def setup_logging() -> ConfigLogger:
    """Configure logging with enhanced tracking and cleanup"""
    # Create logs directory if it doesn't exist
    log_dir = Path('logs')
    log_dir.mkdir(exist_ok=True)

    # Create formatters
    console_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    )
    
    file_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
    )

    # Create handlers with UTF-8 encoding
    console_handler = logging.StreamHandler(
        codecs.getwriter('utf-8')(sys.stdout.buffer) if sys.platform == 'win32' else sys.stdout
    )
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(logging.INFO)

    # Debug log file with enhanced cleanup
    debug_handler = CleanupRotatingFileHandler(
        log_dir / 'debug.log',
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding='utf-8',
        max_age_days=7  # Auto-delete logs older than a week
    )
    debug_handler.setFormatter(file_formatter)
    debug_handler.setLevel(logging.DEBUG)

    # Error log file with enhanced cleanup
    error_handler = CleanupRotatingFileHandler(
        log_dir / 'error.log',
        maxBytes=5*1024*1024,  # 5MB
        backupCount=5,
        encoding='utf-8',
        max_age_days=14  # Keep errors longer - 2 weeks
    )
    error_handler.setFormatter(file_formatter)
    error_handler.setLevel(logging.ERROR)

    # Balance specific log file with custom filter
    balance_handler = CleanupRotatingFileHandler(
        log_dir / 'balance.log',
        maxBytes=5*1024*1024,  # 5MB
        backupCount=3,
        encoding='utf-8',
        max_age_days=30  # Keep balance logs for a month
    )
    balance_handler.setFormatter(file_formatter)
    balance_handler.setLevel(logging.INFO)

    # Config specific log file
    config_handler = CleanupRotatingFileHandler(
        log_dir / 'config.log',
        maxBytes=5*1024*1024,  # 5MB
        backupCount=2,
        encoding='utf-8',
        max_age_days=14  # Keep config for 2 weeks
    )
    config_handler.setFormatter(file_formatter)
    config_handler.setLevel(CONFIG_CHECK)

    # Create balance filter
    class BalanceFilter(logging.Filter):
        def filter(self, record):
            return any(term in record.getMessage().lower() 
                     for term in ['balance', 'reserve', 'usdt'])

    # Add filter to balance handler
    balance_handler.addFilter(BalanceFilter())

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # Remove any existing handlers
    root_logger.handlers = []

    # Add all handlers
    root_logger.addHandler(console_handler)
    root_logger.addHandler(debug_handler)
    root_logger.addHandler(error_handler)
    root_logger.addHandler(balance_handler)
    root_logger.addHandler(config_handler)

    # Create balance logger
    balance_logger = logging.getLogger('balance')
    balance_logger.setLevel(logging.INFO)
    
    # Add custom logging methods
    def balance_check(self, msg, *args, **kwargs):
        self.log(BALANCE_CHECK, msg, *args, **kwargs)

    def reserve_check(self, msg, *args, **kwargs):
        self.log(RESERVE_CHECK, msg, *args, **kwargs)

    logging.Logger.balance_check = balance_check
    logging.Logger.reserve_check = reserve_check

    # Create config logger instance
    config_logger = ConfigLogger(root_logger)
    
    # Run initial log directory cleanup
    cleanup_log_directory()
    
    # Log startup
    root_logger.info("Logging system initialized")
    balance_logger.info("Balance tracking initialized")
    config_logger.log_config("Logger configuration completed")

    return config_logger
