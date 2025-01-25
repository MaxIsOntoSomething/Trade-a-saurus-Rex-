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

    # Create formatters - Fix the typo in levelname
    detailed_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    simple_formatter = logging.Formatter(
        '[%(asctime)s] %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Create debug file handler
    debug_file = f'logs/debug_{datetime.now().strftime("%Y%m%d")}.log'
    debug_handler = RotatingFileHandler(
        debug_file,
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(detailed_formatter)
    
    # Create trades file handler
    trades_file = f'logs/trades_{datetime.now().strftime("%Y%m%d")}.log'
    file_handler = RotatingFileHandler(
        trades_file,
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(simple_formatter)

    # Fixed SafeStreamHandler implementation
    class SafeStreamHandler(logging.StreamHandler):
        def emit(self, record):
            try:
                msg = self.format(record)
                stream = self.stream
                # Write directly as string, not bytes
                stream.write(msg + self.terminator)
                self.flush()
            except Exception:
                self.handleError(record)

    # Configure console handler
    console_handler = SafeStreamHandler(sys.stdout)
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

    # Add API request logger
    api_handler = RotatingFileHandler(
        'logs/api_requests.log',
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    api_handler.setLevel(logging.DEBUG)
    api_handler.setFormatter(
        logging.Formatter('%(asctime)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    )

    # Create API logger
    api_logger = logging.getLogger('API_Requests')
    api_logger.setLevel(logging.DEBUG)
    api_logger.addHandler(api_handler)
    api_logger.propagate = False
    
    return logger, api_logger