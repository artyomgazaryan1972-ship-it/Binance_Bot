from config import (RSI_MIN, RSI_MAX, ADX_MIN, FNG_MAX_BUY, MIN_VOLUME_MULT,
                    STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAIL_ACTIVATE_PCT, TRAIL_PCT)


def check_buy_signal(ind15: dict, ind1h: dict, fng: int) -> tuple:
    p, e200, e50, rsi, adx, vol, vol_ma = (
        ind15["price"], ind15["ema200"], ind15["ema50"],
        ind15["rsi"],   ind15["adx"],
        ind15["volume"], ind15["vol_ma"],
    )
    fails = []
    if p <= e200:
        fails.append(f"цена {p:,.0f} < EMA200(15м) {e200:,.0f}")
    if e50 <= e200:
        fails.append("EMA50 ≤ EMA200")
    if ind1h["price"] <= ind1h["ema200"]:
        fails.append("ниже EMA200(1ч)")
    ema_diff = abs(e50 - e200) / e200
    if ema_diff < 0.0025:
        fails.append("SIDEWAYS MARKET: EMA compression (no trend)")
    price_range = abs(p - e200) / e200
    if price_range < 0.003:
        fails.append("LOW VOLATILITY: price too close to EMA200")
    if not (RSI_MIN <= rsi <= RSI_MAX):
        fails.append(f"RSI {rsi:.1f} вне [{RSI_MIN}–{RSI_MAX}]")
    if adx < ADX_MIN:
        fails.append(f"ADX {adx:.1f} < {ADX_MIN}")
    if vol < vol_ma * MIN_VOLUME_MULT:
        fails.append("объём слабый")
    if fng > FNG_MAX_BUY:
        fails.append(f"F&G={fng} экстремальная жадность")
    if fails:
        return False, " | ".join(fails)
    return True, (
        f"Цена: {p:,.2f} > EMA200: {e200:,.2f} (15м+1ч)\n"
        f"EMA50: {e50:,.2f} | RSI: {rsi:.1f} | ADX: {adx:.1f} | F&G: {fng}"
    )


def check_sell_signal(ind: dict, pos: dict) -> tuple:
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
        return True, f"📐 Трейлинг-стоп: {price:,.2f} ({profit_pct*100:+.1f}%)"
    if price <= stop_price:
        return True, f"🛑 Стоп-лосс: {price:,.2f} ({profit_pct*100:+.1f}%)"
    if price >= tp_price:
        return True, f"🎯 Тейк-профит: {price:,.2f} (+{profit_pct*100:.1f}%)"
    if price < ind["ema200"]:
        return True, f"📉 Ниже EMA200 ({ind['ema200']:,.2f})"
    return False, ""
