import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import codecs
from datetime import datetime

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

def setup_logging() -> ConfigLogger:
    """Configure logging with enhanced tracking"""
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

    # Debug log file
    debug_handler = RotatingFileHandler(
        log_dir / 'debug.log',
        maxBytes=10*1024*1024,
        backupCount=5,
        encoding='utf-8'
    )
    debug_handler.setFormatter(file_formatter)
    debug_handler.setLevel(logging.DEBUG)

    # Error log file
    error_handler = RotatingFileHandler(
        log_dir / 'error.log',
        maxBytes=10*1024*1024,
        backupCount=5,
        encoding='utf-8'
    )
    error_handler.setFormatter(file_formatter)
    error_handler.setLevel(logging.ERROR)

    # Balance specific log file with custom filter
    balance_handler = RotatingFileHandler(
        log_dir / 'balance.log',
        maxBytes=10*1024*1024,
        backupCount=5,
        encoding='utf-8'
    )
    balance_handler.setFormatter(file_formatter)
    balance_handler.setLevel(logging.INFO)

    # Config specific log file
    config_handler = RotatingFileHandler(
        log_dir / 'config.log',
        maxBytes=10*1024*1024,
        backupCount=5,
        encoding='utf-8'
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
    
    # Log startup
    root_logger.info("Logging system initialized")
    balance_logger.info("Balance tracking initialized")
    config_logger.log_config("Logger configuration completed")

    return config_logger
