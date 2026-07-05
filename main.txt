import threading
from flask import Flask

from config import ADMIN_ID, BINANCE_KEY, BINANCE_SECRET, TELEGRAM_TOKEN
from utils import logger
from storage import load_state, apply_state
from monitor import monitor_loop, set_bot
from telegram_bot import bot
from exchange import check_connection

app = Flask(__name__)


@app.route("/")
def health():
    return "OK", 200


def run_flask():
    app.run(host="0.0.0.0", port=6000)


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан!")
    if not BINANCE_KEY or not BINANCE_SECRET:
        raise RuntimeError("BINANCE_KEY / BINANCE_SECRET не заданы!")
    if ADMIN_ID == 0:
        raise RuntimeError("ADMIN_ID не задан!")

    logger.info("Проверка подключения к Binance...")
    conn_status = check_connection()
    logger.info(conn_status.replace("*", "").replace("\n", " | "))

    logger.info("Восстановление состояния из файла...")
    saved = load_state()
    if saved:
        apply_state(saved)
        logger.info("Состояние восстановлено")

    set_bot(bot)

    threading.Thread(target=run_flask,    daemon=True).start()
    logger.info("Flask keep-alive запущен на порту 6000")

    threading.Thread(target=monitor_loop, daemon=True).start()
    logger.info("Монитор рынка запущен (первая проверка немедленно)")

    logger.info("Бот запущен. ADMIN_ID=%d | Режим: ДЕМО (по умолчанию)", ADMIN_ID)
    bot.infinity_polling(timeout=30, long_polling_timeout=20)


if __name__ == "__main__":
    main()
