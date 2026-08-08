import ccxt
import logging
from config import BINANCE_KEY, BINANCE_SECRET, SYMBOL

logger = logging.getLogger(__name__)

exchange = ccxt.binance({
    "apiKey": BINANCE_KEY,
    "secret": BINANCE_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "spot"},
})

def format_amount(amount: float) -> float:
    """Приведение количества BTC к правильной точности биржи."""
    try:
        exchange.load_markets()
        return float(exchange.amount_to_precision(SYMBOL, amount))
    except Exception as e:
        logger.error("Ошибка форматирования точности: %s", e)
        return amount

def check_connection() -> str:
    try:
        status = exchange.fetch_status()
        ticker = exchange.fetch_ticker(SYMBOL)
        return f"Binance API: ✅ {status.get('status')} | {SYMBOL}: {ticker['last']} USDT"
    except Exception as e:
        return f"Binance API: ❌ Ошибка подключения ({e})"
