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
        '%(asctime)s - %(name)s - %(levellevel)s - %(filename)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    simple_formatter = logging.Formatter(
        '[%(asctime)s] %(levelname)s: %(message)s',
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

    # Create console handler with UTF-8 encoding and special handling for Windows
    if os.name == 'nt':  # Windows
        import sys
        import codecs

        class SafeStreamHandler(logging.StreamHandler):
            def emit(self, record):
                try:
                    msg = self.format(record)
                    stream = self.stream.buffer if hasattr(self.stream, 'buffer') else self.stream
                    stream.write(msg.encode('utf-8', errors='replace'))
                    stream.write(b'\n')
                    self.flush()
                except Exception:
                    self.handleError(record)
        
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'replace')
        console_handler = SafeStreamHandler(sys.stdout)
    else:
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
    
    return logger