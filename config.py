# config.py - v8.6.5 - TUNED FOR MICRO-SCALPING & FOCUSED SCANNING

import os
from dotenv import load_dotenv

# A curated list of pairs. The bot will only trade pairs from this list if it's not empty.
# If empty, the bot will scan all USDT pairs on the exchange.
WATCHLIST = [
    "BTC-USDT", "ETH-USDT", "BNB-USDT", "SOL-USDT", "XRP-USDT", "DOGE-USDT", "ADA-USDT",
    "DOT-USDT", "LINK-USDT", "MATIC-USDT", "ATOM-USDT", "LTC-USDT", "AAVE-USDT",
    "UNI-USDT", "INJ-USDT", "OP-USDT", "NEAR-USDT", "SUI-USDT", "ARB-USDT",
    "GMX-USDT", "LDO-USDT", "YFI-USDT", "MKR-USDT",
    "COMP-USDT", "BAL-USDT", "CRV-USDT", "FET-USDT", "PYTH-USDT",
    "AGIX-USDT", "GRT-USDT", "PEPE-USDT",
    "SHIB-USDT", "FLOKI-USDT", "JUP-USDT",
    "STRK-USDT", "ZK-USDT",
    "XLM-USDT", "TRX-USDT", "BCH-USDT", "ETC-USDT",
    "SAND-USDT", "IMX-USDT", "YGG-USDT",
    "ICP-USDT", "APT-USDT", "MINA-USDT", "RAY-USDT",
    "ID-USDT",
]

# Set to "1" for paper trading, "0" for live trading.
DEMO_TRADING = "1"
# Enable detailed logging for debugging.
DEBUG_MODE = True # Set to False for cleaner logs during active trading

# Load API credentials from the correct .env file
env_file = "demo.env" if DEMO_TRADING == "1" else "live.env"
load_dotenv(dotenv_path=env_file)

API_KEY = os.getenv("OKX_API_KEY", "")
API_SECRET = os.getenv("OKX_API_SECRET", "")
API_PASSPHRASE = os.getenv("OKX_API_PASSPHRASE", "")

# ---------- Trading parameters ----------
QUOTE_CCY = "USDT"
INSTRUMENT_TYPE = "SPOT"
TAG_PREFIX = "MS86"

# ---------- Market scanning (Lean Profile) ----------
# How often (in seconds) to rescan all pairs for volatility.
SCAN_ALL_PAIRS_INTERVAL = 90 # Less frequent REST API calls
# How many of the most volatile pairs to actively monitor via WebSocket.
TOP_N_VOLATILE_PAIRS = 20   # REDUCED: Focus on the top movers
# Minimum 24h trading volume in USDT to consider a pair.
MIN_ABSOLUTE_24H_VOL_USDT = 500_000 # Keep a reasonable volume floor
# Minimum 24h price swing ((high-low)/low) to consider a pair.
MIN_24H_VOLATILITY_PCT = 1.5       # Keep a reasonable volatility floor

# ---------- Bar aggregation ----------
BAR_SAMPLING_SEC = 1.0
BAR_HISTORY_MAX = 400

# ---------- Entry strategy (Micro-Scalper Tuning) ----------
MIN_CANDLES_FOR_ENTRY = 12
LOOKBACK_CANDLES = 8
MIN_BREAKOUT_STRENGTH_BPS = 1.5
BREAKOUT_DEAD_ZONE_BPS = 0.0 # DISABLED for high frequency

# ATR (volatility) filter
ATR_PERIOD = 14
MIN_ATR_BPS = 5

# EMA trend filter (optional)
REQUIRE_EMA_CROSS = False
EMA_FAST = 5
EMA_SLOW = 13

# --- Stage 2: Execution confirmations ---
# Set to 0 for most aggressive scalping, 1 to require at least one confirmation.
MIN_CONFIRMATIONS_NEEDED = 1
ARMED_CANDIDATE_TIMEOUT_SEC = 20

# Z-score (short-term momentum)
ZSCORE_WINDOW = 15
ZSCORE_THRESHOLD = 0.10 # LOWERED: More sensitive to small moves

# Volume spike
VOLUME_AVG_WINDOW = 30
VOLUME_SPIKE_WINDOW = 3
VOLUME_SPIKE_MULTIPLIER = 1.1

# Order book imbalance
MIN_IMBALANCE = 1.05
MAX_IMBALANCE = 20.0
MIN_BID_DEPTH_USDT = 400

# ---------- Spread / execution ----------
MAX_SPREAD_PCT = 0.35
FEE_TAKER_BPS = 10
FEE_MAKER_BPS = 8
MAX_SLIPPAGE_BPS = 5
ORDER_PENDING_TIMEOUT = 30

# ---------- TP / SL / Trailing (Micro-Scalper Tuning) ----------
TP_ATR_MULTIPLIER = 4.0
SL_ATR_MULTIPLIER = 2.0
MIN_TP_BPS = 20
MAX_TP_BPS = 80
MIN_SL_BPS = 15
MAX_SL_BPS = 60
MIN_RISK_REWARD_RATIO = 0.2

# Trailing Stop-Loss
ENABLE_TRAILING_STOP = True
TRAILING_ACTIVATION_BPS = 15
TRAILING_DISTANCE_ATR_MULTIPLIER = 1.0
TRAILING_DISTANCE_MIN_BPS = 10

# Maximum time (in seconds) to hold a position.
MAX_POSITION_HOLD_SECONDS = 120

# ---------- Position sizing ----------
BASE_ORDER_NOTIONAL_USDT = 20.0

# ---------- Risk Management (Micro-Scalper Tuning) ----------
MAX_CONCURRENT_TRADES = 1
MAX_CONSECUTIVE_LOSSES = 5
MAX_DAILY_LOSS = 30.0
COOL_DOWN_AFTER_EXIT_SEC = 2
COOL_DOWN_AFTER_LOSS_SEC = 8
MAX_TRADES_PER_PAIR_PER_HOUR = 8

# ---------- Logging & Persistence ----------
CSV_LOG_TRADES = True
CSV_LOG_PATH = "momentum_trades_v86.csv"
ERROR_LOG_PATH = "momentum_errors_v86.log"
LOG_ERRORS_TO_FILE = True
LOG_REJECTION_REASONS = True
REJECTION_LOG_PATH = "rejections_v86.csv"

AUTO_EXCLUDE_FAILED_PAIRS = True
MAX_FAILURES_PER_PAIR = 2
EXCLUDED_PAIRS_FILE = "excluded_pairs_v86.json"
ERRORS_TO_IGNORE_FOR_COOLDOWN = []

# ---------- Display ----------
DISPLAY_MODE = "TUI"

# ---------- WebSocket endpoints ----------
WS_URL_DEMO_PUBLIC = "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999"
WS_URL_DEMO_PRIVATE = "wss://wspap.okx.com:8443/ws/v5/private?brokerId=9999"
WS_URL_LIVE_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
WS_URL_LIVE_PRIVATE = "wss://ws.okx.com:8443/ws/v5/private"