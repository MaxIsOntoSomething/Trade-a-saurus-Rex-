import logging
import sys
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
import codecs

def setup_logger(name='BinanceBot'):
    """Setup logger with enhanced configuration"""
    # Create logs directory if it doesn't exist
    os.makedirs('logs', exist_ok=True)

    # Create logger
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # Create formatters
    detailed_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    simple_formatter = logging.Formatter(
        '[%(asctime)s] %(levellevel)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Create debug file handler with UTF-8 encoding
    debug_file = f'logs/debug_{datetime.now().strftime("%Y%m%d")}.log'
    debug_handler = logging.FileHandler(debug_file, encoding='utf-8', mode='a')
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(detailed_formatter)
    
    # Create trades file handler with UTF-8 encoding
    trades_file = f'logs/trades_{datetime.now().strftime("%Y%m%d")}.log'
    file_handler = logging.FileHandler(trades_file, encoding='utf-8', mode='a')
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(simple_formatter)

    # Create console handler with UTF-8 encoding
    try:
        if sys.stdout.encoding != 'utf-8':
            # For Windows, create a special UTF-8 stream
            sys.stdout.reconfigure(encoding='utf-8')
            console_handler = logging.StreamHandler(sys.stdout)
        else:
            console_handler = logging.StreamHandler()
    except AttributeError:
        # Fallback for older Python versions or if reconfigure is not available
        console_handler = logging.StreamHandler()
        
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(simple_formatter)
    
    # Remove existing handlers
    logger.handlers.clear()
    
    # Add handlers
    logger.addHandler(debug_handler)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    # Prevent logs from being sent to root logger
    logger.propagate = False
    
    # Configure other loggers
    logging.getLogger('telegram').setLevel(logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    
    return logger