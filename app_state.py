import datetime
import threading
from config import DEMO_START_USDT

state_lock = threading.Lock()

trading_mode       = "demo"
signals_enabled    = True
auto_trade_enabled = False
prev_above_ema     = None
consecutive_losses = 0

real_position     = None
real_daily_trades = 0
real_daily_reset  = datetime.date.today()
real_daily_loss   = 0.0

demo_usdt         = DEMO_START_USDT
demo_btc          = 0.0
demo_position     = None
demo_daily_trades = 0
demo_daily_reset  = datetime.date.today()
demo_daily_loss   = 0.0
demo_trades       = []
