import logging
from datetime import datetime

def setup_logger():
    # Create a formatter that includes timestamp
    formatter = logging.Formatter(
        '[%(asctime)s] %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Set up file handler
    file_handler = logging.FileHandler(
        f'logs/trades_{datetime.now().strftime("%Y%m%d")}.log'
    )
    file_handler.setFormatter(formatter)

    # Configure logger
    logger = logging.getLogger('BinanceBot')
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)

    # Prevent logs from being sent to root logger
    logger.propagate = False

    # Only log important events
    logging.getLogger('telegram').setLevel(logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.WARNING)

    return logger