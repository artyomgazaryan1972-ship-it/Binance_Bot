import os
import logging
import threading
import time
import datetime
import requests
import ccxt
import pandas as pd
import telebot
from telebot import types
from flask import Flask

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Secrets ───────────────────────────────────────────────────────────────────
BINANCE_KEY    = os.getenv("BINANCE_KEY", "")
BINANCE_SECRET = os.getenv("BINANCE_SECRET", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ADMIN_ID       = int(os.getenv("ADMIN_ID", "0"))

# ── Торговые параметры ────────────────────────────────────────────────────────
BUY_MIN_USDT       = 5.0
BUY_MAX_USDT       = 15.0
AUTO_TRADE_AMOUNT  = 15.0
STOP_LOSS_PCT      = 0.03
TAKE_PROFIT_PCT    = 0.06
TRAIL_ACTIVATE_PCT = 0.03
TRAIL_PCT          = 0.015
MAX_DAILY_TRADES   = 5
SYMBOL             = "BTC/USDT"
MONITOR_INTERVAL   = 30 * 60

RSI_MIN    = 40
RSI_MAX    = 65
ADX_MIN    = 20
FNG_MAX_BUY= 80

DEMO_START_USDT = 1000.0   # стартовый демо-баланс

# ── Состояние ─────────────────────────────────────────────────────────────────
state_lock = threading.Lock()

# Режим торговли: "demo" или "real"
trading_mode    = "demo"   # по умолчанию демо (безопасно)

signals_enabled    = True
auto_trade_enabled = False
prev_above_ema     = None

# Реальная позиция
real_position = None   # {entry_price, amount_btc, usdt_spent, entry_time, highest_price}
real_daily_trades    = 0
real_daily_reset     = datetime.date.today()

# Демо-состояние
demo_usdt          = DEMO_START_USDT
demo_btc           = 0.0
demo_position      = None   # {entry_price, amount_btc, usdt_spent, entry_time, highest_price}
demo_daily_trades  = 0
demo_daily_reset   = datetime.date.today()
demo_trades        = []     # история: список dict

# ── Binance ───────────────────────────────────────────────────────────────────
exchange = ccxt.binance({
    "apiKey": BINANCE_KEY,
    "secret": BINANCE_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "spot"},
})

# ── Telegram ──────────────────────────────────────────────────────────────────
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# ── Flask keep-alive ──────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def health():
    return "OK", 200

def run_flask():
    app.run(host="0.0.0.0", port=6000)


# ── Технический анализ ────────────────────────────────────────────────────────

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


def calculate_indicators(symbol: str, timeframe: str = "15m", limit: int = 260) -> dict:
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
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


def fetch_fear_greed() -> tuple[int, str]:
    try:
        resp  = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        data  = resp.json()["data"][0]
        return int(data["value"]), data["value_classification"]
    except Exception as e:
        logger.warning("Fear & Greed недоступен: %s", e)
        return 50, "Neutral"


def check_buy_signal(ind15: dict, ind1h: dict, fng: int) -> tuple[bool, str]:
    p, e200, e50, rsi, adx, vol, vol_ma = (
        ind15["price"], ind15["ema200"], ind15["ema50"],
        ind15["rsi"],   ind15["adx"],
        ind15["volume"], ind15["vol_ma"],
    )
    fails = []
    if p <= e200:
        fails.append(f"цена {p:,.0f} < EMA200(15м) {e200:,.0f}")
    if e50 <= e200:
        fails.append(f"EMA50 ≤ EMA200")
    if ind1h["price"] <= ind1h["ema200"]:
        fails.append(f"ниже EMA200(1ч)")
    if not (RSI_MIN <= rsi <= RSI_MAX):
        fails.append(f"RSI {rsi:.1f} вне [{RSI_MIN}–{RSI_MAX}]")
    if adx < ADX_MIN:
        fails.append(f"ADX {adx:.1f} < {ADX_MIN}")
    if vol < vol_ma * 1.2:
        fails.append("объём слабый")
    if fng > FNG_MAX_BUY:
        fails.append(f"F&G={fng} экстремальная жадность")
    if fails:
        return False, " | ".join(fails)
    return True, (
        f"Цена: {p:,.2f} > EMA200: {e200:,.2f} (15м+1ч)\n"
        f"EMA50: {e50:,.2f} | RSI: {rsi:.1f} | ADX: {adx:.1f} | F&G: {fng}"
    )


def check_sell_signal(ind: dict, pos: dict) -> tuple[bool, str]:
    price      = ind["price"]
    entry      = pos["entry_price"]
    highest    = pos["highest_price"]
    stop_price = entry * (1 - STOP_LOSS_PCT)
    tp_price   = entry * (1 + TAKE_PROFIT_PCT)
    profit_pct = (price - entry) / entry

    if price > highest:
        pos["highest_price"] = price
        highest = price

    trail_stop = highest * (1 - TRAIL_PCT)
    if profit_pct >= TRAIL_ACTIVATE_PCT and price <= trail_stop:
        pct = profit_pct * 100
        return True, f"📐 Трейлинг-стоп: {price:,.2f} ({pct:+.1f}%)"
    if price <= stop_price:
        pct = profit_pct * 100
        return True, f"🛑 Стоп-лосс: {price:,.2f} ({pct:+.1f}%)"
    if price >= tp_price:
        pct = profit_pct * 100
        return True, f"🎯 Тейк-профит: {price:,.2f} (+{pct:.1f}%)"
    if price < ind["ema200"]:
        return True, f"📉 Ниже EMA200 ({ind['ema200']:,.2f})"
    return False, ""


# ── Реальная торговля ─────────────────────────────────────────────────────────

def real_buy(price: float) -> dict | None:
    try:
        amount = AUTO_TRADE_AMOUNT / price
        order  = exchange.create_market_buy_order(SYMBOL, amount)
        filled = float(order.get("filled", amount))
        cost   = float(order.get("cost",   AUTO_TRADE_AMOUNT))
        avg_px = cost / filled if filled > 0 else price
        logger.info("[REAL] Куплено: %.8f BTC за %.2f USDT по %.2f", filled, cost, avg_px)
        return {"entry_price": avg_px, "amount_btc": filled,
                "usdt_spent": cost, "entry_time": datetime.datetime.now(),
                "highest_price": avg_px}
    except Exception as e:
        logger.error("[REAL] Ошибка покупки: %s", e)
        return None


def real_sell_position(pos: dict) -> dict | None:
    try:
        balance  = exchange.fetch_balance()
        btc_free = balance["free"].get("BTC", 0.0)
        markets  = exchange.load_markets()
        min_qty  = markets[SYMBOL]["limits"]["amount"]["min"] or 0.00001
        if btc_free < min_qty:
            logger.warning("[REAL] Недостаточно BTC: %.8f", btc_free)
            return None
        btc_sell = float(exchange.amount_to_precision(SYMBOL, btc_free))
        order    = exchange.create_market_sell_order(SYMBOL, btc_sell)
        cost     = float(order.get("cost",   0.0))
        filled   = float(order.get("filled", btc_sell))
        avg_px   = cost / filled if filled > 0 else 0
        logger.info("[REAL] Продано: %.8f BTC, получено %.2f USDT", filled, cost)
        return {"sell_price": avg_px, "amount_btc": filled, "usdt_received": cost}
    except Exception as e:
        logger.error("[REAL] Ошибка продажи: %s", e)
        return None


# ── Демо торговля ─────────────────────────────────────────────────────────────

def demo_buy(price: float) -> dict | None:
    global demo_usdt, demo_btc
    with state_lock:
        available = demo_usdt
    amount_usdt = min(AUTO_TRADE_AMOUNT, available)
    if amount_usdt < 1.0:
        logger.info("[DEMO] Недостаточно виртуального USDT: %.2f", available)
        return None
    amount_btc = amount_usdt / price
    with state_lock:
        demo_usdt -= amount_usdt
        demo_btc  += amount_btc
    logger.info("[DEMO] Куплено: %.8f BTC за %.2f USDT по %.2f", amount_btc, amount_usdt, price)
    return {"entry_price": price, "amount_btc": amount_btc,
            "usdt_spent": amount_usdt, "entry_time": datetime.datetime.now(),
            "highest_price": price}


def demo_sell_position(pos: dict, price: float) -> dict:
    global demo_usdt, demo_btc
    amount_btc   = pos["amount_btc"]
    usdt_received = amount_btc * price
    with state_lock:
        demo_usdt += usdt_received
        demo_btc  -= amount_btc
        if demo_btc < 0:
            demo_btc = 0.0
    logger.info("[DEMO] Продано: %.8f BTC по %.2f, получено %.2f USDT",
                amount_btc, price, usdt_received)
    return {"sell_price": price, "amount_btc": amount_btc, "usdt_received": usdt_received}


# ── Мониторинг ────────────────────────────────────────────────────────────────

def monitor_loop() -> None:
    global prev_above_ema, signals_enabled, auto_trade_enabled, trading_mode
    global real_position, real_daily_trades, real_daily_reset
    global demo_position, demo_daily_trades, demo_daily_reset, demo_trades

    logger.info("Поток мониторинга запущен. Интервал: %d мин.", MONITOR_INTERVAL // 60)

    while True:
        time.sleep(MONITOR_INTERVAL)

        today = datetime.date.today()
        with state_lock:
            if today != real_daily_reset:
                real_daily_reset  = today
                real_daily_trades = 0
            if today != demo_daily_reset:
                demo_daily_reset  = today
                demo_daily_trades = 0

        with state_lock:
            at_on = auto_trade_enabled
            sg_on = signals_enabled
            mode  = trading_mode

        if not at_on and not sg_on:
            continue

        try:
            ind15 = calculate_indicators(SYMBOL, "15m", 260)
            price = ind15["price"]
            above = price > ind15["ema200"]

            logger.info(
                "Мониторинг: цена=%.2f EMA200=%.2f RSI=%.1f ADX=%.1f [%s]",
                price, ind15["ema200"], ind15["rsi"], ind15["adx"], mode.upper(),
            )

            # ── Сигнал EMA200 ──────────────────────────────────────────────
            with state_lock:
                cross = (prev_above_ema is not None and above != prev_above_ema)
                prev_above_ema = above

            if sg_on and cross:
                fng_val, fng_lbl = fetch_fear_greed()
                if above:
                    msg = (
                        f"🚀 *Пробой EMA200 вверх!*\n"
                        f"Цена: *{price:,.2f}* | EMA200: *{ind15['ema200']:,.2f}*\n"
                        f"RSI: *{ind15['rsi']:.1f}* | ADX: *{ind15['adx']:.1f}*\n"
                        f"Fear & Greed: *{fng_val}* ({fng_lbl})"
                    )
                else:
                    msg = (
                        f"⚠️ *Цена упала ниже EMA200!*\n"
                        f"Цена: *{price:,.2f}* | EMA200: *{ind15['ema200']:,.2f}*\n"
                        f"RSI: *{ind15['rsi']:.1f}* | ADX: *{ind15['adx']:.1f}*\n"
                        f"Fear & Greed: *{fng_val}* ({fng_lbl})"
                    )
                try:
                    bot.send_message(ADMIN_ID, msg, parse_mode="Markdown")
                except Exception as e:
                    logger.error("Ошибка сигнала: %s", e)

            if not at_on:
                continue

            # ── Автоторговля ───────────────────────────────────────────────
            label = "🧪 ДЕМО" if mode == "demo" else "💰 РЕАЛ"

            if mode == "demo":
                with state_lock:
                    cur_pos  = demo_position
                    d_trades = demo_daily_trades
            else:
                with state_lock:
                    cur_pos  = real_position
                    d_trades = real_daily_trades

            if cur_pos is None:
                if d_trades >= MAX_DAILY_TRADES:
                    continue

                ind1h    = calculate_indicators(SYMBOL, "1h", 220)
                fng_val, fng_lbl = fetch_fear_greed()
                ok, reason = check_buy_signal(ind15, ind1h, fng_val)

                if ok:
                    if mode == "demo":
                        result = demo_buy(price)
                        if result:
                            with state_lock:
                                demo_position     = result
                                demo_daily_trades += 1
                    else:
                        result = real_buy(price)
                        if result:
                            with state_lock:
                                real_position     = result
                                real_daily_trades += 1

                    if result:
                        msg = (
                            f"🤖 *Автопилот {label}: КУПЛЕНО*\n\n"
                            f"{reason}\n\n"
                            f"BTC: *{result['amount_btc']:.8f}*\n"
                            f"Цена входа: *{result['entry_price']:,.2f} USDT*\n"
                            f"Потрачено: *{result['usdt_spent']:.2f} USDT*\n\n"
                            f"🛑 SL: *{result['entry_price']*(1-STOP_LOSS_PCT):,.2f}*"
                            f" (-{STOP_LOSS_PCT*100:.0f}%)\n"
                            f"🎯 TP: *{result['entry_price']*(1+TAKE_PROFIT_PCT):,.2f}*"
                            f" (+{TAKE_PROFIT_PCT*100:.0f}%)"
                        )
                        bot.send_message(ADMIN_ID, msg, parse_mode="Markdown")
                else:
                    logger.info("Автопилот [%s]: вход не открыт — %s", mode, reason)

            else:
                ok, reason = check_sell_signal(ind15, cur_pos)
                if ok:
                    entry = cur_pos["entry_price"]
                    if mode == "demo":
                        result = demo_sell_position(cur_pos, price)
                        with state_lock:
                            pnl = result["usdt_received"] - cur_pos["usdt_spent"]
                            demo_trades.append({
                                "time":  datetime.datetime.now().strftime("%d.%m %H:%M"),
                                "entry": entry,
                                "exit":  result["sell_price"],
                                "pnl":   pnl,
                            })
                            if len(demo_trades) > 20:
                                demo_trades.pop(0)
                            demo_position     = None
                            demo_daily_trades += 1
                    else:
                        result = real_sell_position(cur_pos)
                        if result:
                            with state_lock:
                                real_position     = None
                                real_daily_trades += 1

                    if result:
                        pnl     = result["usdt_received"] - cur_pos["usdt_spent"]
                        pnl_pct = pnl / cur_pos["usdt_spent"] * 100
                        sign    = "+" if pnl >= 0 else ""
                        held    = int(
                            (datetime.datetime.now() - cur_pos["entry_time"]).total_seconds() / 60
                        )
                        emoji = "✅" if pnl >= 0 else "📉"
                        msg = (
                            f"🤖 *Автопилот {label}: ПРОДАНО*\n\n"
                            f"{reason}\n\n"
                            f"Вход: *{entry:,.2f}* → Выход: *{result['sell_price']:,.2f}*\n"
                            f"{emoji} P&L: *{sign}{pnl:.2f} USDT ({sign}{pnl_pct:.1f}%)*\n"
                            f"Время в позиции: {held} мин."
                        )
                        bot.send_message(ADMIN_ID, msg, parse_mode="Markdown")

        except Exception as e:
            logger.error("Ошибка мониторинга: %s", e)


# ── Keyboards ─────────────────────────────────────────────────────────────────

def main_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        types.KeyboardButton("📊 Статус рынка"),
        types.KeyboardButton("💰 Мой баланс"),
        types.KeyboardButton("⚡️ Купить 5 USDT"),
        types.KeyboardButton("⚡️ Купить 15 USDT"),
        types.KeyboardButton("🔻 Продать всё"),
        types.KeyboardButton("💳 Пополнить"),
        types.KeyboardButton("🎮 Торговля"),
        types.KeyboardButton("🤖 Автопилот"),
        types.KeyboardButton("🔔 Настройка сигналов"),
        types.KeyboardButton("🛡 Проверка связи"),
    )
    return kb


def trading_mode_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(
            "🧪 Демо счёт — тестирование без риска",
            callback_data="mode_demo",
        ),
        types.InlineKeyboardButton(
            "💰 Реальный счёт — реальные деньги",
            callback_data="mode_real",
        ),
    )
    return kb


def signals_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ Включить",  callback_data="signals_on"),
        types.InlineKeyboardButton("🔕 Выключить", callback_data="signals_off"),
    )
    return kb


def autopilot_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🟢 Включить автопилот",  callback_data="auto_on"),
        types.InlineKeyboardButton("🔴 Выключить автопилот", callback_data="auto_off"),
        types.InlineKeyboardButton("📋 Текущая позиция",     callback_data="auto_position"),
    )
    return kb


def deposit_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("💵 USDT — TRC20 (Tron)",     callback_data="deposit_USDT_TRX"),
        types.InlineKeyboardButton("💵 USDT — BEP20 (BSC)",      callback_data="deposit_USDT_BSC"),
        types.InlineKeyboardButton("💵 USDT — ERC20 (Ethereum)", callback_data="deposit_USDT_ETH"),
        types.InlineKeyboardButton("₿  BTC  — Bitcoin",          callback_data="deposit_BTC_BTC"),
        types.InlineKeyboardButton("❌ ОТМЕНА",                   callback_data="cancel"),
    )
    return kb


def confirm_keyboard(action: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ ДА, ПОДТВЕРЖДАЮ", callback_data=f"confirm_{action}"),
        types.InlineKeyboardButton("❌ ОТМЕНА",           callback_data="cancel"),
    )
    return kb


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_admin(message) -> bool:
    return message.from_user.id == ADMIN_ID


def trading_status_text() -> str:
    with state_lock:
        mode    = trading_mode
        d_usdt  = demo_usdt
        d_btc   = demo_btc
        d_pos   = demo_position
        d_trades= list(demo_trades)

    if mode == "demo":
        active = "🧪 *Демо счёт* (активен)"
        other  = "💰 Реальный счёт"
    else:
        active = "💰 *Реальный счёт* (активен)"
        other  = "🧪 Демо счёт"

    lines = [
        f"🎮 *Торговля*",
        f"Режим: {active}",
        f"",
    ]

    if mode == "demo":
        # Считаем общий P&L демо
        total_pnl = sum(t["pnl"] for t in d_trades)
        sign = "+" if total_pnl >= 0 else ""
        lines += [
            f"💼 *Демо баланс:*",
            f"USDT: *{d_usdt:,.2f}* (старт: {DEMO_START_USDT:.0f})",
            f"BTC:  *{d_btc:.8f}*",
            f"P&L всего: *{sign}{total_pnl:.2f} USDT*",
            f"",
        ]
        if d_pos:
            try:
                ticker  = exchange.fetch_ticker(SYMBOL)
                cur     = ticker["last"]
                pnl_pct = (cur - d_pos["entry_price"]) / d_pos["entry_price"] * 100
                sign2   = "+" if pnl_pct >= 0 else ""
                lines += [
                    f"📌 *Открытая позиция:*",
                    f"BTC: *{d_pos['amount_btc']:.8f}*",
                    f"Вход: *{d_pos['entry_price']:,.2f}* | Сейчас: *{cur:,.2f}*",
                    f"P&L: *{sign2}{pnl_pct:.2f}%*",
                    f"",
                ]
            except Exception:
                lines += [f"📌 Позиция открыта: *{d_pos['entry_price']:,.2f}*", ""]

        if d_trades:
            lines.append(f"📋 *История сделок (последние {min(5, len(d_trades))}):'*")
            for t in reversed(d_trades[-5:]):
                sign3 = "+" if t["pnl"] >= 0 else ""
                emoji = "✅" if t["pnl"] >= 0 else "❌"
                lines.append(
                    f"{emoji} {t['time']}  "
                    f"{t['entry']:,.0f}→{t['exit']:,.0f}  "
                    f"*{sign3}{t['pnl']:.2f} USDT*"
                )
        else:
            lines.append("📋 Сделок ещё не было — включи автопилот.")

    else:
        try:
            balance = exchange.fetch_balance()
            usdt    = balance["free"].get("USDT", 0.0)
            btc     = balance["free"].get("BTC",  0.0)
            lines += [
                f"💼 *Реальный баланс (Spot):*",
                f"USDT: *{usdt:,.2f}*",
                f"BTC:  *{btc:.8f}*",
            ]
        except Exception as e:
            lines += [f"Ошибка баланса: {e}"]

    lines += ["", f"Переключить режим:"]
    return "\n".join(lines)


def fetch_market_status() -> str:
    try:
        ind      = calculate_indicators(SYMBOL, "15m", 260)
        ind1h    = calculate_indicators(SYMBOL, "1h",  220)
        fng, lbl = fetch_fear_greed()
        price    = ind["price"]
        trend15  = (
            f"📈 Выше EMA200 ({ind['ema200']:,.0f}) — бычий"
            if price > ind["ema200"]
            else f"📉 Ниже EMA200 ({ind['ema200']:,.0f}) — медвежий"
        )
        trend1h  = (
            "📈 Выше EMA200(1ч)"
            if ind1h["price"] > ind1h["ema200"]
            else "📉 Ниже EMA200(1ч)"
        )
        rsi_lbl  = (
            "🔴 Перекуплен" if ind["rsi"] > 65
            else "🟢 Перепродан" if ind["rsi"] < 40
            else "🟡 Зона входа"
        )
        adx_lbl  = (
            "💪 Сильный" if ind["adx"] >= 25
            else "⚡ Умеренный" if ind["adx"] >= ADX_MIN
            else "😴 Флэт"
        )
        fng_emoji = (
            "😱" if fng < 25 else "😨" if fng < 40
            else "😐" if fng < 60 else "😏" if fng < 80 else "🤑"
        )
        return (
            f"📊 *Статус рынка BTC/USDT*\n"
            f"Цена: *{price:,.2f} USDT*\n\n"
            f"15м: {trend15}\n"
            f"1ч:  {trend1h}\n\n"
            f"EMA50: *{ind['ema50']:,.0f}*\n"
            f"RSI(14): *{ind['rsi']:.1f}* — {rsi_lbl}\n"
            f"ADX(14): *{ind['adx']:.1f}* — {adx_lbl}\n"
            f"Объём: {'↑ выше среднего' if ind['volume'] > ind['vol_ma']*1.2 else '→ обычный'}\n\n"
            f"Fear & Greed: {fng_emoji} *{fng}* — {lbl}"
        )
    except Exception as e:
        logger.error("Ошибка статуса рынка: %s", e)
        return f"Ошибка API: {e}"


def fetch_balance() -> str:
    try:
        balance = exchange.fetch_balance()
        usdt    = balance["free"].get("USDT", 0.0)
        btc     = balance["free"].get("BTC",  0.0)
        with state_lock:
            d_usdt = demo_usdt
            d_btc  = demo_btc
        return (
            f"💰 *Баланс Spot (реальный):*\n"
            f"USDT: *{usdt:,.2f}*\n"
            f"BTC:  *{btc:.8f}*\n\n"
            f"🧪 *Баланс Демо:*\n"
            f"USDT: *{d_usdt:,.2f}*\n"
            f"BTC:  *{d_btc:.8f}*"
        )
    except Exception as e:
        logger.error("Ошибка баланса: %s", e)
        return f"Ошибка API: {e}"


def execute_buy(usdt_amount: float) -> str:
    try:
        ticker = exchange.fetch_ticker(SYMBOL)
        price  = ticker["last"]
        amount = usdt_amount / price
        order  = exchange.create_market_buy_order(SYMBOL, amount)
        filled = order.get("filled", amount)
        cost   = order.get("cost",   usdt_amount)
        return (
            f"✅ *Покупка исполнена*\n"
            f"Куплено: *{filled:.8f} BTC*\n"
            f"Потрачено: *{cost:.2f} USDT*\n"
            f"Цена: *{price:,.2f} USDT*"
        )
    except Exception as e:
        logger.error("Ошибка покупки: %s", e)
        return f"Ошибка API: {e}"


def execute_sell_all() -> str:
    global real_position
    try:
        balance  = exchange.fetch_balance()
        btc_free = balance["free"].get("BTC", 0.0)
        markets  = exchange.load_markets()
        min_qty  = markets[SYMBOL]["limits"]["amount"]["min"] or 0.00001
        if btc_free < min_qty:
            return f"⚠️ Недостаточно BTC (доступно: {btc_free:.8f})"
        btc_sell = exchange.amount_to_precision(SYMBOL, btc_free)
        order    = exchange.create_market_sell_order(SYMBOL, float(btc_sell))
        cost     = order.get("cost", 0.0)
        with state_lock:
            real_position = None
        return (
            f"✅ *Продажа исполнена*\n"
            f"Продано: *{btc_free:.8f} BTC*\n"
            f"Получено: *{cost:.2f} USDT*"
        )
    except Exception as e:
        logger.error("Ошибка продажи: %s", e)
        return f"Ошибка API: {e}"


def fetch_deposit_address(currency: str, network: str) -> str:
    try:
        data    = exchange.fetch_deposit_address(currency, params={"network": network})
        address = data.get("address", "")
        tag     = data.get("tag") or data.get("memo")
        labels  = {"TRX": "TRC20 (Tron)", "BSC": "BEP20 (BSC)",
                   "ETH": "ERC20 (Ethereum)", "BTC": "Bitcoin"}
        net_lbl = labels.get(network, network)
        text    = f"💳 *Пополнение {currency} — {net_lbl}*\n\nАдрес:\n`{address}`\n"
        if tag:
            text += f"\n⚠️ *Memo / Tag:* `{tag}`\n_(обязательно при переводе)_\n"
        text += f"\n⚠️ Только {currency} по сети *{net_lbl}*."
        return text
    except Exception as e:
        logger.error("Ошибка адреса: %s", e)
        return f"Ошибка API: {e}"


def check_connectivity() -> str:
    try:
        status   = exchange.fetch_status()
        server   = status.get("status", "unknown")
        ticker   = exchange.fetch_ticker(SYMBOL)
        price    = ticker["last"]
        ping_ms  = exchange.last_response_headers.get("x-mbx-used-weight", "—")
        fng, lbl = fetch_fear_greed()
        with state_lock:
            mode = trading_mode
        return (
            f"🛡 *Проверка связи*\n"
            f"Binance API: ✅ Онлайн\n"
            f"Статус биржи: *{server}*\n"
            f"BTC/USDT: *{price:,.2f} USDT*\n"
            f"API Weight: *{ping_ms}*\n"
            f"Fear & Greed: *{fng}* — {lbl}\n"
            f"Режим торговли: *{'🧪 Демо' if mode == 'demo' else '💰 Реальный'}*"
        )
    except Exception as e:
        logger.error("Ошибка связи: %s", e)
        return f"Ошибка: {e}"


def autopilot_status_text() -> str:
    with state_lock:
        at   = auto_trade_enabled
        mode = trading_mode
        pos  = demo_position if mode == "demo" else real_position
        dt   = demo_daily_trades if mode == "demo" else real_daily_trades

    status     = "🟢 Включён" if at else "🔴 Выключен"
    mode_label = "🧪 Демо" if mode == "demo" else "💰 Реальный"
    lines = [
        f"🤖 *Автопилот*",
        f"Статус: *{status}*",
        f"Режим: *{mode_label}*",
        f"",
        f"⚙️ Сумма: *{AUTO_TRADE_AMOUNT:.0f} USDT* | SL: *-{STOP_LOSS_PCT*100:.0f}%*"
        f" | TP: *+{TAKE_PROFIT_PCT*100:.0f}%*",
        f"Трейлинг: +{TRAIL_ACTIVATE_PCT*100:.0f}% → стоп {TRAIL_PCT*100:.1f}% от макс",
        f"Сделок сегодня: *{dt}/{MAX_DAILY_TRADES}*",
        f"",
        f"📋 EMA200(15м+1ч) + EMA50 + RSI[{RSI_MIN}–{RSI_MAX}] + ADX>{ADX_MIN} + F&G<{FNG_MAX_BUY}",
    ]
    if pos:
        entry = pos["entry_price"]
        try:
            cur     = exchange.fetch_ticker(SYMBOL)["last"]
            pnl_pct = (cur - entry) / entry * 100
            sign    = "+" if pnl_pct >= 0 else ""
            held    = int(
                (datetime.datetime.now() - pos["entry_time"]).total_seconds() / 60
            )
            lines += [
                f"",
                f"📌 *Открытая позиция:*",
                f"Вход: *{entry:,.2f}* | Сейчас: *{cur:,.2f}*",
                f"P&L: *{sign}{pnl_pct:.2f}%* | В позиции: {held} мин.",
                f"SL: *{entry*(1-STOP_LOSS_PCT):,.2f}* | TP: *{entry*(1+TAKE_PROFIT_PCT):,.2f}*",
            ]
        except Exception:
            lines += ["", f"📌 Позиция: вход *{entry:,.2f}*"]
    else:
        lines += ["", "📌 Позиция: *нет*"]
    return "\n".join(lines)


# ── Handlers ──────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def cmd_start(message):
    if not is_admin(message):
        return
    logger.info("Команда /start")
    bot.send_message(
        message.chat.id,
        "👋 Добро пожаловать в *Binance Terminal*!\n\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(func=lambda m: True)
def handle_buttons(message):
    if not is_admin(message):
        return
    text = message.text
    logger.info("Кнопка: %s", text)

    if text == "📊 Статус рынка":
        bot.send_message(message.chat.id, "⏳ Анализирую рынок...")
        bot.send_message(message.chat.id, fetch_market_status(), parse_mode="Markdown")

    elif text == "💰 Мой баланс":
        bot.send_message(message.chat.id, "⏳ Запрашиваю баланс...")
        bot.send_message(message.chat.id, fetch_balance(), parse_mode="Markdown")

    elif text == "⚡️ Купить 5 USDT":
        bot.send_message(
            message.chat.id,
            f"❓ Купить BTC на *{BUY_MIN_USDT:.0f} USDT*?\n⚠️ Реальная сделка!",
            parse_mode="Markdown",
            reply_markup=confirm_keyboard("buy_5"),
        )

    elif text == "⚡️ Купить 15 USDT":
        bot.send_message(
            message.chat.id,
            f"❓ Купить BTC на *{BUY_MAX_USDT:.0f} USDT*?\n⚠️ Реальная сделка!",
            parse_mode="Markdown",
            reply_markup=confirm_keyboard("buy_15"),
        )

    elif text == "🔻 Продать всё":
        bot.send_message(
            message.chat.id,
            "❓ Продать *весь* BTC?\n⚠️ Реальная сделка!",
            parse_mode="Markdown",
            reply_markup=confirm_keyboard("sell"),
        )

    elif text == "🎮 Торговля":
        bot.send_message(message.chat.id, "⏳ Загружаю...")
        bot.send_message(
            message.chat.id,
            trading_status_text(),
            parse_mode="Markdown",
            reply_markup=trading_mode_keyboard(),
        )

    elif text == "🤖 Автопилот":
        bot.send_message(message.chat.id, "⏳ Загружаю...")
        bot.send_message(
            message.chat.id,
            autopilot_status_text(),
            parse_mode="Markdown",
            reply_markup=autopilot_keyboard(),
        )

    elif text == "🔔 Настройка сигналов":
        with state_lock:
            st = signals_enabled
        bot.send_message(
            message.chat.id,
            f"🔔 *Сигналы EMA200*\nСтатус: *{'✅ включены' if st else '🔕 выключены'}*\n\n"
            f"Проверка каждые 30 мин. Сигнал при пересечении EMA200.",
            parse_mode="Markdown",
            reply_markup=signals_keyboard(),
        )

    elif text == "💳 Пополнить":
        bot.send_message(message.chat.id, "💳 Выберите валюту и сеть:",
                         reply_markup=deposit_keyboard())

    elif text == "🛡 Проверка связи":
        bot.send_message(message.chat.id, "⏳ Проверяю...")
        bot.send_message(message.chat.id, check_connectivity(), parse_mode="Markdown")

    else:
        bot.send_message(message.chat.id, "Используйте кнопки меню.",
                         reply_markup=main_keyboard())


@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    global signals_enabled, auto_trade_enabled, trading_mode

    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id)
        return

    logger.info("Callback: %s", call.data)
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_reply_markup(
            call.message.chat.id, call.message.message_id, reply_markup=None
        )
    except Exception:
        pass

    if call.data == "mode_demo":
        with state_lock:
            trading_mode = "demo"
        logger.info("Режим: ДЕМО")
        bot.send_message(
            call.message.chat.id,
            f"🧪 *Демо счёт активирован!*\n\n"
            f"Виртуальный баланс: *{DEMO_START_USDT:.0f} USDT*\n"
            f"Все сделки симулируются по реальным ценам — деньги не тратятся.\n\n"
            f"Включи 🤖 Автопилот чтобы начать тестирование.",
            parse_mode="Markdown",
        )

    elif call.data == "mode_real":
        with state_lock:
            trading_mode = "real"
        logger.info("Режим: РЕАЛЬНЫЙ")
        bot.send_message(
            call.message.chat.id,
            f"💰 *Реальный счёт активирован!*\n\n"
            f"⚠️ Теперь автопилот будет совершать *настоящие сделки* на твоём Binance аккаунте.\n"
            f"Убедись что на балансе есть USDT.",
            parse_mode="Markdown",
        )

    elif call.data == "confirm_buy_5":
        bot.send_message(call.message.chat.id, "⏳ Покупка...")
        bot.send_message(call.message.chat.id, execute_buy(BUY_MIN_USDT), parse_mode="Markdown")

    elif call.data == "confirm_buy_15":
        bot.send_message(call.message.chat.id, "⏳ Покупка...")
        bot.send_message(call.message.chat.id, execute_buy(BUY_MAX_USDT), parse_mode="Markdown")

    elif call.data == "confirm_sell":
        bot.send_message(call.message.chat.id, "⏳ Продажа...")
        bot.send_message(call.message.chat.id, execute_sell_all(), parse_mode="Markdown")

    elif call.data == "signals_on":
        with state_lock:
            signals_enabled = True
        bot.send_message(call.message.chat.id,
                         "✅ *Сигналы включены.*", parse_mode="Markdown")

    elif call.data == "signals_off":
        with state_lock:
            signals_enabled = False
        bot.send_message(call.message.chat.id,
                         "🔕 *Сигналы выключены.*", parse_mode="Markdown")

    elif call.data == "auto_on":
        with state_lock:
            auto_trade_enabled = True
            mode = trading_mode
        label = "🧪 Демо" if mode == "demo" else "💰 Реальный"
        bot.send_message(
            call.message.chat.id,
            f"🟢 *Автопилот включён!* [{label}]\n\n"
            f"Стратегия: EMA200(15м+1ч) + EMA50 + RSI + ADX + F&G\n"
            f"Сумма: *{AUTO_TRADE_AMOUNT:.0f} USDT* | SL: *-{STOP_LOSS_PCT*100:.0f}%*"
            f" | TP: *+{TAKE_PROFIT_PCT*100:.0f}%*\n\n"
            f"О каждой сделке напишу сюда.",
            parse_mode="Markdown",
        )

    elif call.data == "auto_off":
        with state_lock:
            auto_trade_enabled = False
        bot.send_message(
            call.message.chat.id,
            "🔴 *Автопилот выключен.*",
            parse_mode="Markdown",
        )

    elif call.data == "auto_position":
        bot.send_message(
            call.message.chat.id,
            autopilot_status_text(),
            parse_mode="Markdown",
            reply_markup=autopilot_keyboard(),
        )

    elif call.data.startswith("deposit_"):
        parts    = call.data.split("_", 2)
        currency = parts[1]
        network  = parts[2]
        bot.send_message(call.message.chat.id, "⏳ Запрашиваю адрес...")
        bot.send_message(call.message.chat.id,
                         fetch_deposit_address(currency, network), parse_mode="Markdown")

    elif call.data == "cancel":
        bot.send_message(call.message.chat.id, "❌ Отменено.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан!")
    if not BINANCE_KEY or not BINANCE_SECRET:
        raise RuntimeError("BINANCE_KEY / BINANCE_SECRET не заданы!")
    if ADMIN_ID == 0:
        raise RuntimeError("ADMIN_ID не задан!")

    threading.Thread(target=run_flask, daemon=True).start()
    logger.info("Flask keep-alive запущен на порту 6000")

    threading.Thread(target=monitor_loop, daemon=True).start()

    logger.info("Бот запущен. ADMIN_ID=%d | Режим: ДЕМО (по умолчанию)", ADMIN_ID)
    bot.infinity_polling(timeout=30, long_polling_timeout=20)
