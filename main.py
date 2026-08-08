import signal
import sys
import threading
from flask import Flask
from config import ADMIN_ID, BINANCE_KEY, BINANCE_SECRET, TELEGRAM_TOKEN
from storage import load_state, persist
from monitor import monitor_loop, set_bot
from telegram_bot import bot
from exchange import check_connection
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route("/")
def health():
    return "OK", 200

def shutdown_handler(signum, frame):
    logger.info("Получен сигнал остановки. Сохраняю состояние...")
    persist()
    sys.exit(0)

def main():
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    if not TELEGRAM_TOKEN or not BINANCE_KEY or not BINANCE_SECRET or ADMIN_ID == 0:
        raise RuntimeError("Ошибка: Переменные окружения не настроены!")

    logger.info(check_connection())

    if load_state():
        logger.info("Состояние загружено из файла.")

    set_bot(bot)

    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=6000), daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()

    logger.info("Бот запущен!")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)

if __name__ == "__main__":
    main()
