import logging
from datetime import datetime
import sys
import os

def setup_logger():
    """Setup logger with enhanced configuration"""
    # Create logs directory if it doesn't exist
    os.makedirs('logs', exist_ok=True)

    # Create logger
    logger = logging.getLogger('BinanceBot')
    logger.setLevel(logging.DEBUG)

    # Create formatters
    detailed_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    simple_formatter = logging.Formatter(
        '[%(asctime)s] %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Create rotating file handler with UTF-8 encoding
    debug_handler = logging.FileHandler(
        f'logs/debug_{datetime.now().strftime("%Y%m%d")}.log',
        encoding='utf-8'  # Add UTF-8 encoding
    )
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(detailed_formatter)

    # Create file handler with UTF-8 encoding
    file_handler = logging.FileHandler(
        f'logs/trades_{datetime.now().strftime("%Y%m%d")}.log',
        encoding='utf-8'  # Add UTF-8 encoding
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(simple_formatter)

    # Create UTF-8 console handler
    if sys.stdout.encoding != 'utf-8':
        console_handler = logging.StreamHandler(stream=open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1))
    else:
        console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(simple_formatter)

    # Remove any existing handlers
    logger.handlers = []

    # Add handlers to logger
    logger.addHandler(debug_handler)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    # Prevent logs from being sent to root logger
    logger.propagate = False

    # Set levels for other loggers
    logging.getLogger('telegram').setLevel(logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.WARNING)

    return logger