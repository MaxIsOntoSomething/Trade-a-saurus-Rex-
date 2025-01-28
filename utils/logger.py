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

    # Replace the old API formatter section with this improved version
    class APIFormatter(logging.Formatter):
        def format(self, record):
            # Add default attributes if not present
            defaults = {
                'api_type': 'API_CALL',
                'request_data': 'N/A',
                'response_data': 'N/A',
                'duration': 0.0,
                'details': ''
            }
            
            for key, default in defaults.items():
                if not hasattr(record, key):
                    setattr(record, key, default)
                    
            return super().format(record)

    # Create API formatter instance
    api_formatter = APIFormatter(
        '%(asctime)s - [%(levelname)s] - %(message)s\n'
        'Type: %(api_type)s\n'
        'Request: %(request_data)s\n'
        'Response: %(response_data)s\n'
        'Duration: %(duration).3fms\n'
        'Details: %(details)s\n'
        '----------------------------------------',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Update API handler to use new formatter
    api_handler = RotatingFileHandler(
        'logs/api.log',
        maxBytes=10*1024*1024,
        backupCount=5,
        encoding='utf-8'
    )
    api_handler.setLevel(logging.DEBUG)
    api_handler.setFormatter(api_formatter)

    # Remove the old duplicate section and replace with:
    api_logger = logging.getLogger('API')
    api_logger.setLevel(logging.DEBUG)
    api_logger.addHandler(api_handler)
    api_logger.propagate = False

    # Add telegram-specific formatter and handler
    telegram_formatter = logging.Formatter(
        '%(asctime)s - [%(levelname)s] - %(message)s\n'
        'Details: %(details)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        defaults={
            'details': ''
        }
    )
    
    telegram_handler = RotatingFileHandler(
        'logs/telegram.log',
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    telegram_handler.setLevel(logging.DEBUG)
    telegram_handler.setFormatter(telegram_formatter)

    # Create Telegram logger
    telegram_logger = logging.getLogger('Telegram')
    telegram_logger.setLevel(logging.DEBUG)
    telegram_logger.addHandler(telegram_handler)
    telegram_logger.propagate = False

    # Add MongoDB logger configuration
    mongodb_formatter = logging.Formatter(
        '%(asctime)s - [%(levelname)s] - %(message)s\n'
        'Operation: %(operation)s\n'
        'Collection: %(collection)s\n'
        'Query: %(query)s\n'
        'Duration: %(duration).3fms\n'
        'Result: %(result)s\n'
        '----------------------------------------',
        datefmt='%Y-%m-%d %H:%M:%S',
        defaults={
            'operation': 'N/A',
            'collection': 'N/A',
            'query': 'N/A',
            'duration': 0.0,
            'result': 'N/A'
        }
    )

    # Create MongoDB log handler
    mongodb_file = f'logs/mongodb_{datetime.now().strftime("%Y%m%d")}.log'
    mongodb_handler = RotatingFileHandler(
        mongodb_file,
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    mongodb_handler.setLevel(logging.DEBUG)
    mongodb_handler.setFormatter(mongodb_formatter)

    # Create MongoDB logger
    mongodb_logger = logging.getLogger('MongoDB')
    mongodb_logger.setLevel(logging.DEBUG)
    mongodb_logger.addHandler(mongodb_handler)
    mongodb_logger.propagate = False
    
    return logger, api_logger, api_logger, telegram_logger, mongodb_logger