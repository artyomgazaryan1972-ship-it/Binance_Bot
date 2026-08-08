import json
import os
import app_state as S
from config import STATE_FILE
import logging

logger = logging.getLogger(__name__)

def persist() -> None:
    """Атомарная запись состояния во временный файл с переименованием."""
    with S.state_lock:
        data = {
            "trading_mode": S.trading_mode,
            "signals_enabled": S.signals_enabled,
            "auto_trade_enabled": S.auto_trade_enabled,
            "prev_above_ema": S.prev_above_ema,
            "consecutive_losses": S.consecutive_losses,
            "real_position": S.real_position,
            "real_daily_trades": S.real_daily_trades,
            "real_daily_loss": S.real_daily_loss,
            "real_daily_reset": str(S.real_daily_reset),
            "demo_usdt": S.demo_usdt,
            "demo_btc": S.demo_btc,
            "demo_position": S.demo_position,
            "demo_daily_trades": S.demo_daily_trades,
            "demo_daily_loss": S.demo_daily_loss,
            "demo_daily_reset": str(S.demo_daily_reset),
            "demo_trades": S.demo_trades,
        }
    
    tmp_file = f"{STATE_FILE}.tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp_file, STATE_FILE)
    except Exception as e:
        logger.error("Ошибка сохранения состояния: %s", e)

def load_state() -> bool:
    if not os.path.exists(STATE_FILE):
        return False
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        with S.state_lock:
            S.trading_mode       = data.get("trading_mode", "demo")
            S.signals_enabled    = data.get("signals_enabled", True)
            S.auto_trade_enabled = data.get("auto_trade_enabled", False)
            S.prev_above_ema     = data.get("prev_above_ema")
            S.consecutive_losses = data.get("consecutive_losses", 0)
            S.real_position      = data.get("real_position")
            S.real_daily_trades  = data.get("real_daily_trades", 0)
            S.real_daily_loss    = data.get("real_daily_loss", 0.0)
            S.real_daily_reset   = data.get("real_daily_reset")
            S.demo_usdt          = data.get("demo_usdt", 1000.0)
            S.demo_btc           = data.get("demo_btc", 0.0)
            S.demo_position     = data.get("demo_position")
            S.demo_daily_trades  = data.get("demo_daily_trades", 0)
            S.demo_daily_loss    = data.get("demo_daily_loss", 0.0)
            S.demo_daily_reset   = data.get("demo_daily_reset")
            S.demo_trades        = data.get("demo_trades", [])
        return True
    except Exception as e:
        logger.error("Ошибка загрузки состояния: %s", e)
        return False
