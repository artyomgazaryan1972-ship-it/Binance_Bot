import logging
import time
import functools
from logging.handlers import RotatingFileHandler
from config import LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT


def setup_logger() -> logging.Logger:
    log = logging.getLogger("binance_bot")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    log.addHandler(ch)

    fh = RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)

    return log


logger = setup_logger()


def retry(max_attempts: int = 3, delay: float = 2.0, backoff: float = 2.0):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 0
            wait = delay
            while attempt < max_attempts:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    attempt += 1
                    if attempt >= max_attempts:
                        logger.error("%s провалилась после %d попыток: %s", func.__name__, max_attempts, e)
                        raise
                    logger.warning("Попытка %d/%d для %s: %s. Жду %.1fс...",
                                   attempt, max_attempts, func.__name__, e, wait)
                    time.sleep(wait)
                    wait *= backoff
        return wrapper
    return decorator
