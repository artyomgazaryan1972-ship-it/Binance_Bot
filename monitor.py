import time
import datetime
from config import (SYMBOL, MONITOR_INTERVAL, MAX_DAILY_TRADES, MAX_CONSECUTIVE_LOSSES,
                    MAX_DAILY_LOSS, STOP_LOSS_PCT, TAKE_PROFIT_PCT)
from indicators import calculate_indicators, fetch_fear_greed
from strategy import check_buy_signal, check_sell_signal
from orders import real_buy, real_sell_position, demo_buy, demo_sell_position
import app_state as S
from storage import persist
from utils import logger

_bot_ref = None


def set_bot(bot) -> None:
    global _bot_ref
    _bot_ref = bot


def _send(msg: str) -> None:
    if _bot_ref:
        try:
            _bot_ref.send_message(S.state_lock and _get_admin_id(), msg, parse_mode="Markdown")
        except Exception as e:
            logger.error("Ошибка отправки Telegram: %s", e)


def _get_admin_id() -> int:
    from config import ADMIN_ID
    return ADMIN_ID


def _send_msg(msg: str) -> None:
    from config import ADMIN_ID
    if _bot_ref:
        try:
            _bot_ref.send_message(ADMIN_ID, msg, parse_mode="Markdown")
        except Exception as e:
            logger.error("Ошибка отправки Telegram: %s", e)


def _reset_daily_if_needed() -> None:
    today = datetime.date.today()
    with S.state_lock:
        if today != S.real_daily_reset:
            S.real_daily_reset  = today
            S.real_daily_trades = 0
            S.real_daily_loss   = 0.0
            S.consecutive_losses = 0
        if today != S.demo_daily_reset:
            S.demo_daily_reset  = today
            S.demo_daily_trades = 0
            S.demo_daily_loss   = 0.0


def _do_cycle() -> None:
    _reset_daily_if_needed()

    with S.state_lock:
        at_on = S.auto_trade_enabled
        sg_on = S.signals_enabled
        mode  = S.trading_mode

    if not at_on and not sg_on:
        return

    try:
        ind15 = calculate_indicators(SYMBOL, "15m", 260)
        price = ind15["price"]
        above = price > ind15["ema200"]

        logger.info("Мониторинг: цена=%.2f EMA200=%.2f RSI=%.1f ADX=%.1f [%s]",
                    price, ind15["ema200"], ind15["rsi"], ind15["adx"], mode.upper())

        with S.state_lock:
            prev  = S.prev_above_ema
            cross = (prev is not None and above != prev)
            S.prev_above_ema = above

        if sg_on and cross:
            fng_val, fng_lbl = fetch_fear_greed()
            if above:
                msg = (f"🚀 *Пробой EMA200 вверх!*\n"
                       f"Цена: *{price:,.2f}* | EMA200: *{ind15['ema200']:,.2f}*\n"
                       f"RSI: *{ind15['rsi']:.1f}* | ADX: *{ind15['adx']:.1f}*\n"
                       f"Fear & Greed: *{fng_val}* ({fng_lbl})")
            else:
                msg = (f"⚠️ *Цена упала ниже EMA200!*\n"
                       f"Цена: *{price:,.2f}* | EMA200: *{ind15['ema200']:,.2f}*\n"
                       f"RSI: *{ind15['rsi']:.1f}* | ADX: *{ind15['adx']:.1f}*\n"
                       f"Fear & Greed: *{fng_val}* ({fng_lbl})")
            _send_msg(msg)

        if not at_on:
            return

        label = "🧪 ДЕМО" if mode == "demo" else "💰 РЕАЛ"

        with S.state_lock:
            cur_pos  = S.demo_position  if mode == "demo" else S.real_position
            d_trades = S.demo_daily_trades if mode == "demo" else S.real_daily_trades
            d_loss   = S.demo_daily_loss   if mode == "demo" else S.real_daily_loss
            consec   = S.consecutive_losses

        if cur_pos is None:
            if d_trades >= MAX_DAILY_TRADES:
                logger.info("Автопилот [%s]: дневной лимит сделок (%d)", mode, MAX_DAILY_TRADES)
                return
            if d_loss >= MAX_DAILY_LOSS:
                logger.info("Автопилот [%s]: дневной лимит убытков %.2f USDT", mode, d_loss)
                return
            if consec >= MAX_CONSECUTIVE_LOSSES:
                logger.info("Автопилот [%s]: %d убытков подряд — пауза", mode, consec)
                return

            ind1h = calculate_indicators(SYMBOL, "1h", 220)
            fng_val, fng_lbl = fetch_fear_greed()
            ok, reason = check_buy_signal(ind15, ind1h, fng_val)

            if ok:
                result = demo_buy(price) if mode == "demo" else real_buy(price)
                if result:
                    with S.state_lock:
                        if mode == "demo":
                            S.demo_position     = result
                            S.demo_daily_trades += 1
                        else:
                            S.real_position     = result
                            S.real_daily_trades += 1
                    persist()
                    _send_msg(
                        f"🤖 *Автопилот {label}: КУПЛЕНО*\n\n{reason}\n\n"
                        f"BTC: *{result['amount_btc']:.8f}*\n"
                        f"Цена входа: *{result['entry_price']:,.2f} USDT*\n"
                        f"Потрачено: *{result['usdt_spent']:.2f} USDT* (вкл. комиссию)\n\n"
                        f"🛑 SL: *{result['entry_price']*(1-STOP_LOSS_PCT):,.2f}*"
                        f" (-{STOP_LOSS_PCT*100:.0f}%)\n"
                        f"🎯 TP: *{result['entry_price']*(1+TAKE_PROFIT_PCT):,.2f}*"
                        f" (+{TAKE_PROFIT_PCT*100:.0f}%)"
                    )
            else:
                logger.info("Автопилот [%s]: вход не открыт — %s", mode, reason)

        else:
            ok, reason = check_sell_signal(ind15, cur_pos)
            if ok:
                entry = cur_pos["entry_price"]
                result = (demo_sell_position(cur_pos, price)
                          if mode == "demo"
                          else real_sell_position(cur_pos))
                if result:
                    pnl     = result["usdt_received"] - cur_pos["usdt_spent"]
                    pnl_pct = pnl / cur_pos["usdt_spent"] * 100
                    sign    = "+" if pnl >= 0 else ""
                    held    = int(
                        (datetime.datetime.now() - cur_pos["entry_time"]).total_seconds() / 60
                    )
                    with S.state_lock:
                        if mode == "demo":
                            S.demo_trades.append({
                                "time":  datetime.datetime.now().strftime("%d.%m %H:%M"),
                                "entry": entry,
                                "exit":  result["sell_price"],
                                "pnl":   pnl,
                                "fee":   result.get("fee_paid", 0),
                            })
                            if len(S.demo_trades) > 20:
                                S.demo_trades.pop(0)
                            S.demo_position     = None
                            S.demo_daily_trades += 1
                            if pnl < 0:
                                S.demo_daily_loss += abs(pnl)
                        else:
                            S.real_position     = None
                            S.real_daily_trades += 1
                            if pnl < 0:
                                S.real_daily_loss += abs(pnl)
                        if pnl < 0:
                            S.consecutive_losses += 1
                        else:
                            S.consecutive_losses = 0
                        consec_now = S.consecutive_losses
                    persist()

                    emoji    = "✅" if pnl >= 0 else "📉"
                    fee_info = f"\n💸 Комиссии: *{result.get('fee_paid', 0):.4f} USDT*"
                    loss_warn = (
                        f"\n⚠️ *Убытков подряд: {consec_now}/{MAX_CONSECUTIVE_LOSSES}*"
                        if consec_now > 0 else ""
                    )
                    _send_msg(
                        f"🤖 *Автопилот {label}: ПРОДАНО*\n\n{reason}\n\n"
                        f"Вход: *{entry:,.2f}* → Выход: *{result['sell_price']:,.2f}*\n"
                        f"{emoji} P&L: *{sign}{pnl:.2f} USDT ({sign}{pnl_pct:.1f}%)*\n"
                        f"Время в позиции: {held} мин.{fee_info}{loss_warn}"
                    )

    except Exception as e:
        logger.error("Ошибка мониторинга: %s", e)


def monitor_loop() -> None:
    logger.info("Поток мониторинга запущен. Интервал: %d сек.", MONITOR_INTERVAL)
    _do_cycle()
    while True:
        time.sleep(MONITOR_INTERVAL)
        _do_cycle()
