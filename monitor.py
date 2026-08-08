import time
import datetime
import logging
import app_state as S
from config import (SYMBOL, POSITION_CHECK_INTERVAL, SIGNAL_CHECK_INTERVAL,
                    MAX_DAILY_TRADES, MAX_CONSECUTIVE_LOSSES, MAX_DAILY_LOSS,
                    STOP_LOSS_PCT, TAKE_PROFIT_PCT)
from indicators import calculate_indicators, fetch_fear_greed
from strategy import check_buy_signal, check_sell_signal
from orders import real_buy, real_sell_position, demo_buy, demo_sell_position
from storage import persist

logger = logging.getLogger(__name__)
_bot_ref = None

def set_bot(bot_instance):
    global _bot_ref
    _bot_ref = bot_instance

def _send_msg(msg: str):
    from config import ADMIN_ID
    if _bot_ref:
        try:
            _bot_ref.send_message(ADMIN_ID, msg, parse_mode="Markdown")
        except Exception as e:
            logger.error("Ошибка отправки Telegram: %s", e)

def _reset_daily_if_needed():
    today = datetime.date.today().isoformat()
    with S.state_lock:
        if today != S.real_daily_reset:
            S.real_daily_reset = today
            S.real_daily_trades = 0
            S.real_daily_loss = 0.0
            S.consecutive_losses = 0
        if today != S.demo_daily_reset:
            S.demo_daily_reset = today
            S.demo_daily_trades = 0
            S.demo_daily_loss = 0.0

def monitor_loop():
    logger.info("Поток PRO-мониторинга запущен (SL/TP: %dсек, Сигналы: %dсек)", 
                POSITION_CHECK_INTERVAL, SIGNAL_CHECK_INTERVAL)
    last_signal_scan = 0

    while True:
        try:
            now = time.time()
            _reset_daily_if_needed()

            with S.state_lock:
                at_on = S.auto_trade_enabled
                sg_on = S.signals_enabled
                mode  = S.trading_mode
                cur_pos = S.demo_position if mode == "demo" else S.real_position

            if not at_on and not sg_on:
                time.sleep(POSITION_CHECK_INTERVAL)
                continue

            ind15 = calculate_indicators(SYMBOL, "15m", 260)
            price = ind15["price"]

            # 1. Быстрый контроль открытой позиции
            if at_on and cur_pos is not None:
                should_sell, reason = check_sell_signal(ind15, cur_pos)
                if should_sell:
                    entry = cur_pos["entry_price"]
                    res = demo_sell_position(cur_pos, price) if mode == "demo" else real_sell_position(cur_pos)
                    if res:
                        pnl = res["usdt_received"] - cur_pos["usdt_spent"]
                        pnl_pct = (pnl / cur_pos["usdt_spent"]) * 100
                        sign = "+" if pnl >= 0 else ""
                        
                        with S.state_lock:
                            if mode == "demo":
                                S.demo_trades.append({
                                    "time": datetime.datetime.now().strftime("%d.%m %H:%M"),
                                    "entry": entry, "exit": res["sell_price"], "pnl": pnl
                                })
                                S.demo_position = None
                                S.demo_daily_trades += 1
                                if pnl < 0: S.demo_daily_loss += abs(pnl)
                            else:
                                S.real_position = None
                                S.real_daily_trades += 1
                                if pnl < 0: S.real_daily_loss += abs(pnl)

                            if pnl < 0: S.consecutive_losses += 1
                            else: S.consecutive_losses = 0

                        persist()
                        emoji = "✅" if pnl >= 0 else "📉"
                        _send_msg(f"🤖 *Автопилот ({mode.upper()}): ПРОДАНО*\n\n{reason}\n"
                                  f"Вход: *{entry:,.2f}* → Выход: *{res['sell_price']:,.2f}*\n"
                                  f"{emoji} P&L: *{sign}{pnl:.2f} USDT ({sign}{pnl_pct:.1f}%)*")

            # 2. Поиск новых входов и сигналов (раз в 5 минут)
            elif (now - last_signal_scan) >= SIGNAL_CHECK_INTERVAL:
                last_signal_scan = now
                
                # Проверка пробоя EMA200
                above = price > ind15["ema200"]
                with S.state_lock:
                    cross = (S.prev_above_ema is not None and above != S.prev_above_ema)
                    S.prev_above_ema = above

                if sg_on and cross:
                    fng_v, fng_l = fetch_fear_greed()
                    direction = "🚀 Пробой EMA200 вверх!" if above else "⚠️ Уход ниже EMA200!"
                    _send_msg(f"*{direction}*\nЦена: *{price:,.2f}* | F&G: {fng_v} ({fng_l})")

                # Вход в новую сделку
                if at_on and cur_pos is None:
                    with S.state_lock:
                        d_trades = S.demo_daily_trades if mode == "demo" else S.real_daily_trades
                        d_loss   = S.demo_daily_loss if mode == "demo" else S.real_daily_loss
                        consec   = S.consecutive_losses

                    if d_trades < MAX_DAILY_TRADES and d_loss < MAX_DAILY_LOSS and consec < MAX_CONSECUTIVE_LOSSES:
                        ind1h = calculate_indicators(SYMBOL, "1h", 220)
                        fng_v, fng_l = fetch_fear_greed()
                        ok, reason = check_buy_signal(ind15, ind1h, fng_v)
                        
                        if ok:
                            res = demo_buy(price) if mode == "demo" else real_buy(price)
                            if res:
                                with S.state_lock:
                                    if mode == "demo":
                                        S.demo_position = res
                                        S.demo_daily_trades += 1
                                    else:
                                        S.real_position = res
                                        S.real_daily_trades += 1
                                persist()
                                _send_msg(f"🤖 *Автопилот ({mode.upper()}): КУПЛЕНО*\n\n{reason}\n"
                                          f"Цена: *{res['entry_price']:,.2f} USDT*")

        except Exception as e:
            logger.error("Ошибка в цикле мониторинга: %s", e)

        time.sleep(POSITION_CHECK_INTERVAL)
