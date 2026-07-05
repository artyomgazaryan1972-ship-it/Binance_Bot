import datetime
import telebot
from telebot import types

from config import (ADMIN_ID, TELEGRAM_TOKEN, DEMO_START_USDT, AUTO_TRADE_AMOUNT,
                    STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAIL_ACTIVATE_PCT, TRAIL_PCT,
                    RSI_MIN, RSI_MAX, ADX_MIN, FNG_MAX_BUY, BUY_MIN_USDT, BUY_MAX_USDT,
                    MONITOR_INTERVAL, MAX_DAILY_TRADES, MAX_CONSECUTIVE_LOSSES)
from exchange import exchange, check_connection
from indicators import calculate_indicators, fetch_fear_greed
import app_state as S
from storage import persist
from utils import logger

bot = telebot.TeleBot(TELEGRAM_TOKEN)


# ── Keyboards ──────────────────────────────────────────────────────────────────

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
        types.InlineKeyboardButton("🧪 Демо счёт — тестирование без риска", callback_data="mode_demo"),
        types.InlineKeyboardButton("💰 Реальный счёт — реальные деньги",    callback_data="mode_real"),
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


# ── Helpers ────────────────────────────────────────────────────────────────────

def is_admin(message) -> bool:
    return message.from_user.id == ADMIN_ID


def fetch_market_status() -> str:
    try:
        ind      = calculate_indicators(timeframe="15m", limit=260)
        ind1h    = calculate_indicators(timeframe="1h",  limit=220)
        fng, lbl = fetch_fear_greed()
        price    = ind["price"]
        trend15  = (f"📈 Выше EMA200 ({ind['ema200']:,.0f}) — бычий"
                    if price > ind["ema200"]
                    else f"📉 Ниже EMA200 ({ind['ema200']:,.0f}) — медвежий")
        trend1h  = ("📈 Выше EMA200(1ч)" if ind1h["price"] > ind1h["ema200"]
                    else "📉 Ниже EMA200(1ч)")
        rsi_lbl  = ("🔴 Перекуплен" if ind["rsi"] > 65
                    else "🟢 Перепродан" if ind["rsi"] < 40
                    else "🟡 Зона входа")
        adx_lbl  = ("💪 Сильный" if ind["adx"] >= 25
                    else "⚡ Умеренный" if ind["adx"] >= ADX_MIN
                    else "😴 Флэт")
        fng_emoji = ("😱" if fng < 25 else "😨" if fng < 40
                     else "😐" if fng < 60 else "😏" if fng < 80 else "🤑")
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


def fetch_balance_text() -> str:
    try:
        balance = exchange.fetch_balance()
        usdt    = balance["free"].get("USDT", 0.0)
        btc     = balance["free"].get("BTC",  0.0)
        with S.state_lock:
            d_usdt = S.demo_usdt
            d_btc  = S.demo_btc
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
        ticker = exchange.fetch_ticker("BTC/USDT")
        price  = ticker["last"]
        amount = usdt_amount / price
        order  = exchange.create_market_buy_order("BTC/USDT", amount)
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
    try:
        balance  = exchange.fetch_balance()
        btc_free = balance["free"].get("BTC", 0.0)
        markets  = exchange.load_markets()
        min_qty  = markets["BTC/USDT"]["limits"]["amount"]["min"] or 0.00001
        if btc_free < min_qty:
            return f"⚠️ Недостаточно BTC (доступно: {btc_free:.8f})"
        btc_sell = exchange.amount_to_precision("BTC/USDT", btc_free)
        order    = exchange.create_market_sell_order("BTC/USDT", float(btc_sell))
        cost     = order.get("cost", 0.0)
        with S.state_lock:
            S.real_position = None
        persist()
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


def trading_status_text() -> str:
    with S.state_lock:
        mode     = S.trading_mode
        d_usdt   = S.demo_usdt
        d_btc    = S.demo_btc
        d_pos    = S.demo_position
        d_trades = list(S.demo_trades)

    if mode == "demo":
        active = "🧪 *Демо счёт* (активен)"
    else:
        active = "💰 *Реальный счёт* (активен)"

    lines = [f"🎮 *Торговля*", f"Режим: {active}", ""]

    if mode == "demo":
        total_pnl = sum(t["pnl"] for t in d_trades)
        sign = "+" if total_pnl >= 0 else ""
        lines += [
            f"💼 *Демо баланс:*",
            f"USDT: *{d_usdt:,.2f}* (старт: {DEMO_START_USDT:.0f})",
            f"BTC:  *{d_btc:.8f}*",
            f"P&L всего: *{sign}{total_pnl:.2f} USDT*",
            "",
        ]
        if d_pos:
            try:
                cur     = exchange.fetch_ticker("BTC/USDT")["last"]
                pnl_pct = (cur - d_pos["entry_price"]) / d_pos["entry_price"] * 100
                sign2   = "+" if pnl_pct >= 0 else ""
                lines += [
                    f"📌 *Открытая позиция:*",
                    f"BTC: *{d_pos['amount_btc']:.8f}*",
                    f"Вход: *{d_pos['entry_price']:,.2f}* | Сейчас: *{cur:,.2f}*",
                    f"P&L: *{sign2}{pnl_pct:.2f}%*",
                    "",
                ]
            except Exception:
                lines += [f"📌 Позиция открыта: *{d_pos['entry_price']:,.2f}*", ""]

        if d_trades:
            lines.append(f"📋 *История сделок (последние {min(5, len(d_trades))}):*")
            for t in reversed(d_trades[-5:]):
                sign3 = "+" if t["pnl"] >= 0 else ""
                emoji = "✅" if t["pnl"] >= 0 else "❌"
                fee_str = f" | комиссия {t.get('fee', 0):.4f}" if t.get("fee") else ""
                lines.append(
                    f"{emoji} {t['time']}  "
                    f"{t['entry']:,.0f}→{t['exit']:,.0f}  "
                    f"*{sign3}{t['pnl']:.2f} USDT*{fee_str}"
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

    lines += ["", "Переключить режим:"]
    return "\n".join(lines)


def autopilot_status_text() -> str:
    with S.state_lock:
        at   = S.auto_trade_enabled
        mode = S.trading_mode
        pos  = S.demo_position if mode == "demo" else S.real_position
        dt   = S.demo_daily_trades if mode == "demo" else S.real_daily_trades
        dl   = S.demo_daily_loss   if mode == "demo" else S.real_daily_loss
        con  = S.consecutive_losses

    status     = "🟢 Включён" if at else "🔴 Выключен"
    mode_label = "🧪 Демо" if mode == "demo" else "💰 Реальный"
    lines = [
        f"🤖 *Автопилот*",
        f"Статус: *{status}*",
        f"Режим: *{mode_label}*",
        "",
        f"⚙️ Сумма: *{AUTO_TRADE_AMOUNT:.0f} USDT* | SL: *-{STOP_LOSS_PCT*100:.0f}%*"
        f" | TP: *+{TAKE_PROFIT_PCT*100:.0f}%*",
        f"Трейлинг: +{TRAIL_ACTIVATE_PCT*100:.0f}% → стоп {TRAIL_PCT*100:.1f}% от макс",
        f"Сделок сегодня: *{dt}/{MAX_DAILY_TRADES}*",
        f"Убыток сегодня: *{dl:.2f} USDT*",
        f"Убытков подряд: *{con}/{MAX_CONSECUTIVE_LOSSES}*",
        "",
        f"📋 EMA200(15м+1ч) + EMA50 + RSI[{RSI_MIN}–{RSI_MAX}] + ADX>{ADX_MIN} + F&G<{FNG_MAX_BUY}",
    ]
    if pos:
        entry = pos["entry_price"]
        try:
            cur     = exchange.fetch_ticker("BTC/USDT")["last"]
            pnl_pct = (cur - entry) / entry * 100
            sign    = "+" if pnl_pct >= 0 else ""
            held    = int(
                (datetime.datetime.now() - pos["entry_time"]).total_seconds() / 60
            )
            lines += [
                "",
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


# ── Handlers ───────────────────────────────────────────────────────────────────

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
        bot.send_message(message.chat.id, fetch_balance_text(), parse_mode="Markdown")

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
        with S.state_lock:
            st = S.signals_enabled
        bot.send_message(
            message.chat.id,
            f"🔔 *Сигналы EMA200*\nСтатус: *{'✅ включены' if st else '🔕 выключены'}*\n\n"
            f"Проверка каждые {MONITOR_INTERVAL//60} мин. Сигнал при пересечении EMA200.",
            parse_mode="Markdown",
            reply_markup=signals_keyboard(),
        )

    elif text == "💳 Пополнить":
        bot.send_message(message.chat.id, "💳 Выберите валюту и сеть:",
                         reply_markup=deposit_keyboard())

    elif text == "🛡 Проверка связи":
        bot.send_message(message.chat.id, "⏳ Проверяю...")
        bot.send_message(message.chat.id, check_connection(), parse_mode="Markdown")

    else:
        bot.send_message(message.chat.id, "Используйте кнопки меню.",
                         reply_markup=main_keyboard())


@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
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
        with S.state_lock:
            S.trading_mode = "demo"
        persist()
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
        with S.state_lock:
            S.trading_mode = "real"
        persist()
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
        with S.state_lock:
            S.signals_enabled = True
        persist()
        bot.send_message(call.message.chat.id, "✅ *Сигналы включены.*", parse_mode="Markdown")

    elif call.data == "signals_off":
        with S.state_lock:
            S.signals_enabled = False
        persist()
        bot.send_message(call.message.chat.id, "🔕 *Сигналы выключены.*", parse_mode="Markdown")

    elif call.data == "auto_on":
        with S.state_lock:
            S.auto_trade_enabled = True
            mode = S.trading_mode
        persist()
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
        with S.state_lock:
            S.auto_trade_enabled = False
        persist()
        bot.send_message(call.message.chat.id, "🔴 *Автопилот выключен.*", parse_mode="Markdown")

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
