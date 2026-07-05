import pandas as pd
from config import SYMBOL
from exchange import get_ohlcv, get_fear_greed
from utils import logger


def calculate_adx(df: pd.DataFrame, period: int = 14) -> float:
    high, low, close = df["high"], df["low"], df["close"]
    plus_dm  = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    mask       = plus_dm >= minus_dm
    plus_dm_f  = plus_dm.where(mask, 0.0)
    minus_dm_f = minus_dm.where(~mask, 0.0)
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    alpha    = 1 / period
    atr      = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di  = 100 * plus_dm_f.ewm(alpha=alpha, adjust=False).mean()  / atr
    minus_di = 100 * minus_dm_f.ewm(alpha=alpha, adjust=False).mean() / atr
    dx  = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1)) * 100
    adx = dx.ewm(alpha=alpha, adjust=False).mean()
    return float(adx.iloc[-1])


def calculate_indicators(symbol: str = SYMBOL, timeframe: str = "15m", limit: int = 260) -> dict:
    ohlcv = get_ohlcv(symbol, timeframe, limit)
    df    = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "vol"])
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    df["ema50"]  = df["close"].ewm(span=50,  adjust=False).mean()
    delta  = df["close"].diff()
    gain   = delta.clip(lower=0)
    loss   = (-delta).clip(lower=0)
    avg_g  = gain.ewm(com=13, min_periods=14).mean()
    avg_l  = loss.ewm(com=13, min_periods=14).mean()
    rs     = avg_g / avg_l.replace(0, 1e-9)
    df["rsi"]      = 100 - (100 / (1 + rs))
    df["vol_ma20"] = df["vol"].rolling(20).mean()
    adx  = calculate_adx(df)
    last = df.iloc[-1]
    return {
        "price":  float(last["close"]),
        "ema200": float(last["ema200"]),
        "ema50":  float(last["ema50"]),
        "rsi":    float(last["rsi"]),
        "adx":    adx,
        "volume": float(last["vol"]),
        "vol_ma": float(last["vol_ma20"]),
    }


def fetch_fear_greed() -> tuple:
    return get_fear_greed()
