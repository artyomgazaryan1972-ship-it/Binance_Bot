import json
import datetime
import threading
from config import STATE_FILE
from utils import logger

_save_lock = threading.Lock()


def _serialize(obj):
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    if isinstance(obj, datetime.date):
        return obj.isoformat()
    raise TypeError(f"Не сериализуется: {type(obj)}")


def _fix_position(pos):
    if pos is None:
        return None
    if "entry_time" in pos and isinstance(pos["entry_time"], str):
        pos["entry_time"] = datetime.datetime.fromisoformat(pos["entry_time"])
    return pos


def save_state(state: dict) -> None:
    with _save_lock:
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, default=_serialize, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("Ошибка сохранения состояния: %s", e)


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["real_position"] = _fix_position(data.get("real_position"))
        data["demo_position"] = _fix_position(data.get("demo_position"))
        for key in ("real_daily_reset", "demo_daily_reset"):
            if key in data and isinstance(data[key], str):
                data[key] = datetime.date.fromisoformat(data[key])
        logger.info("Состояние восстановлено из %s", STATE_FILE)
        return data
    except FileNotFoundError:
        logger.info("Файл состояния не найден — начинаем с нуля")
        return {}
    except Exception as e:
        logger.error("Ошибка загрузки состояния: %s", e)
        return {}


def apply_state(data: dict) -> None:
    import app_state as S
    with S.state_lock:
        S.trading_mode       = data.get("trading_mode",       S.trading_mode)
        S.signals_enabled    = data.get("signals_enabled",    S.signals_enabled)
        S.auto_trade_enabled = data.get("auto_trade_enabled", S.auto_trade_enabled)
        S.consecutive_losses = data.get("consecutive_losses", S.consecutive_losses)
        S.real_position      = data.get("real_position",      S.real_position)
        S.real_daily_trades  = data.get("real_daily_trades",  S.real_daily_trades)
        S.real_daily_reset   = data.get("real_daily_reset",   S.real_daily_reset)
        S.real_daily_loss    = data.get("real_daily_loss",    S.real_daily_loss)
        S.demo_usdt          = data.get("demo_usdt",          S.demo_usdt)
        S.demo_btc           = data.get("demo_btc",           S.demo_btc)
        S.demo_position      = data.get("demo_position",      S.demo_position)
        S.demo_daily_trades  = data.get("demo_daily_trades",  S.demo_daily_trades)
        S.demo_daily_reset   = data.get("demo_daily_reset",   S.demo_daily_reset)
        S.demo_daily_loss    = data.get("demo_daily_loss",    S.demo_daily_loss)
        S.demo_trades        = data.get("demo_trades",        S.demo_trades)


def snapshot_state() -> dict:
    import app_state as S
    with S.state_lock:
        return {
            "trading_mode":       S.trading_mode,
            "signals_enabled":    S.signals_enabled,
            "auto_trade_enabled": S.auto_trade_enabled,
            "consecutive_losses": S.consecutive_losses,
            "real_position":      S.real_position,
            "real_daily_trades":  S.real_daily_trades,
            "real_daily_reset":   S.real_daily_reset,
            "real_daily_loss":    S.real_daily_loss,
            "demo_usdt":          S.demo_usdt,
            "demo_btc":           S.demo_btc,
            "demo_position":      S.demo_position,
            "demo_daily_trades":  S.demo_daily_trades,
            "demo_daily_reset":   S.demo_daily_reset,
            "demo_daily_loss":    S.demo_daily_loss,
            "demo_trades":        S.demo_trades,
        }


def persist() -> None:
    save_state(snapshot_state())
