import logging
from datetime import datetime

def setup_logger():
    logging.basicConfig(
        filename=f'logs/trades_{datetime.now().strftime("%Y%m%d")}.log',
        format='%(asctime)s - %(message)s',
        level=logging.INFO
    )
    return logging.getLogger()