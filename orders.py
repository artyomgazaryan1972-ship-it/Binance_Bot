import datetime
from config import (SYMBOL, AUTO_TRADE_AMOUNT, BINANCE_FEE, RISK_PCT, STOP_LOSS_PCT)
from exchange import exchange, get_balance, get_symbol_filters, check_slippage, check_liquidity
from utils import logger, retry
import app_state as S
from storage import persist


def _calc_position_size(usdt_capital: float) -> float:
    risk_usdt    = usdt_capital * RISK_PCT
    by_risk      = risk_usdt / STOP_LOSS_PCT
    return min(by_risk, AUTO_TRADE_AMOUNT)


@retry(max_attempts=3, delay=2.0)
def real_buy(price: float) -> dict | None:
    try:
        if not check_liquidity(SYMBOL, AUTO_TRADE_AMOUNT):
            logger.warning("[REAL] Ликвидность низкая — пропускаем вход")
            return None

        filters   = get_symbol_filters(SYMBOL)
        balance   = get_balance()
        usdt_free = balance["free"].get("USDT", 0.0)
        pos_usdt  = _calc_position_size(usdt_free)

        if pos_usdt < filters["min_notional"]:
            logger.warning("[REAL] Сумма %.2f < minNotional %.2f", pos_usdt, filters["min_notional"])
            return None

        amount = pos_usdt / price
        if amount < filters["min_qty"]:
            logger.warning("[REAL] Количество %.8f < minQty %.8f", amount, filters["min_qty"])
            return None

        amount_str = exchange.amount_to_precision(SYMBOL, amount)
        order  = exchange.create_market_buy_order(SYMBOL, float(amount_str))
        filled = float(order.get("filled", amount))
        cost   = float(order.get("cost",   pos_usdt))
        avg_px = cost / filled if filled > 0 else price
        fee    = cost * BINANCE_FEE
        total  = cost + fee

        check_slippage(price, avg_px)
        logger.info("[REAL] Куплено: %.8f BTC за %.2f USDT (комиссия %.4f) по %.2f",
                    filled, total, fee, avg_px)
        return {
            "entry_price":  avg_px,
            "amount_btc":   filled,
            "usdt_spent":   total,
            "entry_time":   datetime.datetime.now(),
            "highest_price":avg_px,
            "fee_paid":     fee,
        }
    except Exception as e:
        logger.error("[REAL] Ошибка покупки: %s", e)
        return None


@retry(max_attempts=3, delay=2.0)
def real_sell_position(pos: dict) -> dict | None:
    try:
        filters  = get_symbol_filters(SYMBOL)
        balance  = get_balance()
        btc_free = balance["free"].get("BTC", 0.0)

        if btc_free < filters["min_qty"]:
            logger.warning("[REAL] Недостаточно BTC: %.8f (min %.8f)", btc_free, filters["min_qty"])
            return None

        btc_sell = float(exchange.amount_to_precision(SYMBOL, btc_free))
        order    = exchange.create_market_sell_order(SYMBOL, btc_sell)
        cost     = float(order.get("cost",   0.0))
        filled   = float(order.get("filled", btc_sell))
        avg_px   = cost / filled if filled > 0 else 0
        fee      = cost * BINANCE_FEE
        net      = cost - fee

        check_slippage(pos["entry_price"], avg_px)
        logger.info("[REAL] Продано: %.8f BTC, получено %.2f USDT (комиссия %.4f)", filled, net, fee)
        return {"sell_price": avg_px, "amount_btc": filled, "usdt_received": net, "fee_paid": fee}
    except Exception as e:
        logger.error("[REAL] Ошибка продажи: %s", e)
        return None


def demo_buy(price: float) -> dict | None:
    with S.state_lock:
        available = S.demo_usdt
    pos_usdt = _calc_position_size(available)
    if pos_usdt < 1.0:
        logger.info("[DEMO] Недостаточно USDT: %.2f", available)
        return None
    fee   = pos_usdt * BINANCE_FEE
    total = pos_usdt + fee
    amount_btc = pos_usdt / price
    with S.state_lock:
        S.demo_usdt -= total
        S.demo_btc  += amount_btc
    persist()
    logger.info("[DEMO] Куплено: %.8f BTC за %.2f USDT (комиссия %.4f) по %.2f",
                amount_btc, total, fee, price)
    return {
        "entry_price":  price,
        "amount_btc":   amount_btc,
        "usdt_spent":   total,
        "entry_time":   datetime.datetime.now(),
        "highest_price":price,
        "fee_paid":     fee,
    }


def demo_sell_position(pos: dict, price: float) -> dict:
    amount_btc = pos["amount_btc"]
    gross      = amount_btc * price
    fee        = gross * BINANCE_FEE
    net        = gross - fee
    with S.state_lock:
        S.demo_usdt += net
        S.demo_btc  -= amount_btc
        if S.demo_btc < 0:
            S.demo_btc = 0.0
    persist()
    logger.info("[DEMO] Продано: %.8f BTC по %.2f, получено %.2f USDT (комиссия %.4f)",
                amount_btc, price, net, fee)
    return {"sell_price": price, "amount_btc": amount_btc, "usdt_received": net, "fee_paid": fee}
