import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import codecs

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

def setup_logging():
    """Setup logging configuration"""
    # Create logs directory
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    
    # Clear any existing handlers
    root_logger.handlers.clear()
    
    # Console handler with simple formatting
    console_handler = WindowsConsoleHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)
    
    # Detailed formatter for file handlers
    file_formatter = logging.Formatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Configure file handlers
    handlers_config = {
        'debug': {
            'filename': 'debug.log',
            'level': logging.DEBUG,
        },
        'error': {
            'filename': 'error.log',
            'level': logging.ERROR,
        },
        'trade': {
            'filename': 'trades.log',
            'level': logging.INFO,
        },
        'telegram': {
            'filename': 'telegram.log',
            'level': logging.INFO,
        }
    }
    
    # Create handlers
    for name, config in handlers_config.items():
        handler = RotatingFileHandler(
            log_dir / config['filename'],
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5,
            encoding='utf-8'
        )
        handler.setLevel(config['level'])
        handler.setFormatter(file_formatter)
        
        if name == 'telegram':
            # Special handling for telegram loggers
            for logger_name in ['telegram', 'telegram.ext']:
                logger = logging.getLogger(logger_name)
                logger.addHandler(handler)
                logger.propagate = False
        else:
            root_logger.addHandler(handler)
    
    # Disable propagation for noisy loggers
    for logger_name in ['httpx', 'asyncio']:
        logger = logging.getLogger(logger_name)
        logger.propagate = False
