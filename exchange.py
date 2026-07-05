import requests as req
import ccxt
from config import BINANCE_KEY, BINANCE_SECRET, SYMBOL, MAX_SLIPPAGE_PCT
from utils import logger, retry

exchange = ccxt.binance({
    "apiKey": BINANCE_KEY,
    "secret": BINANCE_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "spot"},
})


@retry(max_attempts=3, delay=2.0, backoff=2.0)
def get_ticker(symbol: str = SYMBOL) -> dict:
    return exchange.fetch_ticker(symbol)


@retry(max_attempts=3, delay=2.0, backoff=2.0)
def get_ohlcv(symbol: str, timeframe: str, limit: int) -> list:
    return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)


@retry(max_attempts=3, delay=2.0, backoff=2.0)
def get_balance() -> dict:
    return exchange.fetch_balance()


@retry(max_attempts=3, delay=5.0, backoff=2.0)
def get_fear_greed() -> tuple:
    try:
        resp = req.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        data = resp.json()["data"][0]
        return int(data["value"]), data["value_classification"]
    except Exception as e:
        logger.warning("Fear & Greed недоступен: %s", e)
        return 50, "Neutral"


def get_symbol_filters(symbol: str = SYMBOL) -> dict:
    try:
        markets = exchange.load_markets()
        market  = markets[symbol]
        limits    = market.get("limits", {})
        precision = market.get("precision", {})
        return {
            "min_qty":        limits.get("amount", {}).get("min") or 0.00001,
            "min_notional":   limits.get("cost",   {}).get("min") or 1.0,
            "step_size":      precision.get("amount") or 8,
            "price_precision":precision.get("price")  or 2,
        }
    except Exception as e:
        logger.error("Ошибка получения фильтров: %s", e)
        return {"min_qty": 0.00001, "min_notional": 1.0, "step_size": 8, "price_precision": 2}


def check_liquidity(symbol: str = SYMBOL, usdt_amount: float = 15.0) -> bool:
    try:
        ob = exchange.fetch_order_book(symbol, limit=5)
        ask_vol_usdt = sum(p * v for p, v in ob["asks"][:5])
        if ask_vol_usdt < usdt_amount * 3:
            logger.warning("Ликвидность низкая: %.0f USDT в стакане", ask_vol_usdt)
            return False
        return True
    except Exception as e:
        logger.warning("Ошибка проверки ликвидности: %s", e)
        return True


def check_slippage(expected: float, filled: float) -> bool:
    if expected <= 0:
        return True
    slip = abs(filled - expected) / expected
    if slip > MAX_SLIPPAGE_PCT:
        logger.warning("Проскальзывание %.4f%% > макс %.4f%%",
                       slip * 100, MAX_SLIPPAGE_PCT * 100)
        return False
    return True


def check_connection() -> str:
    try:
        status  = exchange.fetch_status()
        server  = status.get("status", "unknown")
        ticker  = get_ticker(SYMBOL)
        price   = ticker["last"]
        weight  = exchange.last_response_headers.get("x-mbx-used-weight", "—")
        fng, lbl = get_fear_greed()
        return (
            f"🛡 *Проверка связи*\n"
            f"Binance API: ✅ Онлайн\n"
            f"Статус биржи: *{server}*\n"
            f"BTC/USDT: *{price:,.2f} USDT*\n"
            f"API Weight: *{weight}*\n"
            f"Fear & Greed: *{fng}* — {lbl}"
        )
    except Exception as e:
        logger.error("Ошибка связи: %s", e)
        return f"❌ Ошибка подключения: {e}"
