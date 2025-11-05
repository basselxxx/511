# utils.py

import asyncio
import csv
import json
import logging
import os
import sys
import time
import traceback
from collections import deque, defaultdict
from datetime import datetime
from decimal import Decimal

import aiohttp
from aiohttp import TCPConnector

import config

class CleanFormatter(logging.Formatter):
    FORMATS = {
        logging.DEBUG: "   %(message)s",
        logging.INFO: "%(message)s",
        logging.WARNING: "⚠️ %(message)s",
        logging.ERROR: "❌ %(message)s",
        logging.CRITICAL: "🚨 %(message)s",
    }
    def format(self, record):
        return logging.Formatter(self.FORMATS.get(record.levelno)).format(record)

def setup_logger():
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG if config.DEBUG_MODE else logging.INFO)
    for h in logger.handlers[:]:
        logger.removeHandler(h)
    console = logging.StreamHandler()
    console.setFormatter(CleanFormatter())
    logger.addHandler(console)
    try:
        file_handler = logging.FileHandler('bot_runtime.log', mode='a', encoding='utf-8')
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(file_handler)
        logger.info("✓ File logger initialized.")
    except Exception as e:
        logger.error(f"❌ Failed to initialize file logger: {e}")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    return logger

logger = setup_logger()

class ErrorLogger:
    def __init__(self):
        self.recent_errors = deque(maxlen=20)
        self.error_count = 0
        self.last_error_time = 0
        self._ensure_log_file()

    def _ensure_log_file(self):
        try:
            with open(config.ERROR_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*70}\nBot started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'='*70}\n")
            logger.info(f"✓ Error log: {config.ERROR_LOG_PATH}")
        except Exception as e:
            logger.warning(f"Cannot create error log: {e}")

    def log_error(self, error_type, message, details=""):
        self.error_count += 1
        self.last_error_time = time.time()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.recent_errors.append({"time": ts, "type": error_type, "message": message})
        if config.LOG_ERRORS_TO_FILE:
            try:
                with open(config.ERROR_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(f"{ts} | {error_type} | {message}\n")
                    if details:
                        f.write(f"   Details: {details}\n")
            except Exception:
                pass
        if error_type not in getattr(config, "ERRORS_TO_IGNORE_FOR_COOLDOWN", []):
            logger.error(f"{error_type}: {message}")
            if details and config.DEBUG_MODE:
                logger.debug(f"   {details}")

error_logger = ErrorLogger()

class DisplayManager:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.start_time = time.time()
        self.session_pnl = Decimal("0")
        self.total_trades, self.wins, self.losses = 0, 0, 0
        self.recent_trades = deque(maxlen=20)
        self.active_position = None
        self.hot_pairs = []
        self.excluded_pairs = []
        self.usdt_balance = Decimal("0")
        self.current_status = "Initializing..."
        self.signals_detected, self.signals_rejected = 0, 0
        self.sniper_ref = None

    def format_runtime(self):
        elapsed = int(time.time() - self.start_time)
        return f"{elapsed//3600}:{(elapsed%3600)//60:02d}:{elapsed%60:02d}"

    def update_trade(self, inst_id, pnl, entry_px, exit_px, hold_sec):
        self.total_trades += 1
        self.session_pnl += Decimal(pnl)
        if pnl >= 0: self.wins += 1
        else: self.losses += 1
        self.recent_trades.append({
            "time": datetime.now().strftime("%H:%M:%S"), "pair": inst_id, "pnl": pnl,
            "entry": entry_px, "exit": exit_px, "hold": hold_sec, "win": pnl >= 0,
        })

    async def render(self):
        async with self.lock:
            os.system("cls" if os.name == "nt" else "clear")
            win_rate = (self.wins / self.total_trades * 100) if self.total_trades > 0 else 0
            avg_pnl = (self.session_pnl / self.total_trades) if self.total_trades > 0 else Decimal("0")
            print("╔" + "═" * 78 + "╗")
            print(f"║{'⚡ MOMENTUM SNIPER v8.7.2 (STABLE) ⚡':^78}║")
            print("╠" + "═" * 78 + "╣")
            print(f"║ {self.current_status:<76} ║")
            print("╠" + "─" * 78 + "╣")
            print(f"║ Balance: ${float(self.usdt_balance):>10.2f} │ Runtime: {self.format_runtime():<10} │ Trades: {self.total_trades:<4} ║")
            print(f"║ P&L: {self._fmt_pnl(self.session_pnl):>12} │ Win Rate: {win_rate:>5.1f}% │ Avg: {self._fmt_pnl(avg_pnl):>10} │ Errors: {error_logger.error_count:<4} ║")
            print(f"║ Signals: Detected {self.signals_detected:<4} │ Rejected {self.signals_rejected:<4} {'':<28}║")
            print("╠" + "═" * 78 + "╣")
            if self.active_position:
                pos = self.active_position
                age = int(time.time() - pos.get("time", 0))
                entry = pos.get("entry_price", 0)
                current = pos.get("current", entry)
                pnl_pct = ((current - entry) / entry * 100) if entry and entry > 0 else 0
                pos_state = pos.get('state', 'N/A')
                state_color = "\033[91m" if "FAILED" in pos_state else "\033[92m"
                state_reset = "\033[0m"
                print(f"║{'ACTIVE POSITION':^78}║")
                print("╠" + "─" * 78 + "╣")
                print(f"║ {pos.get('inst_id', 'N/A'):12} │ {state_color}State: {pos_state:<12}{state_reset} │ Age: {age}s {'':<23}║")
                print(f"║ Entry: ${float(entry):.6f} │ Curr: ${float(current):.6f} │ P&L: {pnl_pct:+.2f}% {'':<11}║")
                print(f"║ TP: ${float(pos.get('tp_price', 0)):.6f} │ SL: ${float(pos.get('sl_price', 0)):.6f} │ R:R: {pos.get('rr', 0):.2f} {'':<5}║")
                print(f"║ Trail: {'ON' if pos.get('trailing_active') else 'OFF'} │ Peak: ${float(pos.get('peak_price', 0)):.6f} {'':<30}║")
            else:
                print(f"║{'NO ACTIVE POSITION - Scanning for setups...':^78}║")
                print("╠" + "═" * 78 + "╣")
                print(f"║{'RECENT TRADES':^78}║")
                print("╠" + "─" * 78 + "╣")
                if self.recent_trades:
                    for t in list(self.recent_trades)[-6:]:
                        sym = "✅" if t["win"] else "❌"
                        print(f"║ {t['time']} {sym} {t['pair']:12} {self._fmt_pnl(t['pnl']):>10} [{t['hold']}s] {'':<27}║")
                else:
                    print(f"║{'No trades yet':^78}║")
                print("╠" + "═" * 78 + "╣")
                print(f"║ 🔥 Hot ({len(self.hot_pairs)}): {', '.join(self.hot_pairs[:6]):<60} ║")
                if hasattr(self, 'sniper_ref') and self.sniper_ref:
                    armed_count = len(self.sniper_ref.armed_candidates)
                    if armed_count > 0:
                        armed_pairs = list(self.sniper_ref.armed_candidates.keys())[:4]
                        armed_str = ', '.join(armed_pairs)
                        print(f"║ 🎯 ARMED ({armed_count}): {armed_str:<65} ║")
                if self.excluded_pairs:
                    print(f"║ 🚫 Excluded ({len(self.excluded_pairs)}): {', '.join(self.excluded_pairs[:4]):<54} ║")
            print("╚" + "═" * 78 + "╝")
            sys.stdout.flush()

    def _fmt_pnl(self, pnl):
        return f"{'+' if pnl >= 0 else ''}${float(pnl):.2f}"

    async def render_loop(self):
        while True:
            await self.render()
            await asyncio.sleep(1)

display = DisplayManager()

class DiagnosticMonitor:
    def __init__(self):
        self.ws_messages_received = 0
        self.orderbook_updates = defaultdict(int)
        self.trade_updates = defaultdict(int)
        self.last_update = defaultdict(float)
    def record_message(self, channel, inst_id):
        self.ws_messages_received += 1
        if channel == "bbo-tbt": self.orderbook_updates[inst_id] += 1
        elif channel == "trades": self.trade_updates[inst_id] += 1
        self.last_update[inst_id] = time.time()
    def get_status(self):
        now = time.time()
        active_pairs = [inst_id for inst_id, last_time in self.last_update.items() if now - last_time < 5]
        return {"total_messages": self.ws_messages_received, "active_pairs": len(active_pairs), "active_pair_names": active_pairs[:5]}

diagnostics = DiagnosticMonitor()

def safe_decimal(x, default=Decimal("0")):
    try: return Decimal(str(x))
    except: return default

class TradeLogger:
    def __init__(self, path):
        self.path = path
        self._ensure_file()
    def _ensure_file(self):
        if not config.CSV_LOG_TRADES: return
        try:
            is_new_file = not os.path.exists(self.path)
            with open(self.path, "a", newline="", encoding="utf-8") as f:
                if is_new_file:
                    csv.writer(f).writerow(["timestamp", "pair", "entry_px", "exit_px", "size", "pnl_usdt", "pnl_pct", "hold_sec", "exit_type", "tp_bps", "sl_bps", "atr", "imbalance", "breakout_strength"])
            if is_new_file: logger.info(f"✓ Trade log: {config.CSV_LOG_PATH}")
        except Exception as e:
            logger.warning(f"Cannot create trade log: {e}")
    def log_trade(self, **kwargs):
        if not config.CSV_LOG_TRADES: return
        try:
            with open(self.path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    kwargs.get("pair", ""), f"{kwargs.get('entry_px', 0):.8f}", f"{kwargs.get('exit_px', 0):.8f}",
                    f"{kwargs.get('size', 0):.8f}", f"{kwargs.get('pnl_usdt', 0):.6f}", f"{kwargs.get('pnl_pct', 0):.4f}",
                    kwargs.get("hold_sec", 0), kwargs.get("exit_type", ""), kwargs.get("tp_bps", 0),
                    kwargs.get("sl_bps", 0), kwargs.get("atr", 0), kwargs.get("imbalance", 0),
                    kwargs.get("breakout_strength", 0),
                ])
        except Exception as e: error_logger.log_error("TRADE_LOG", "Failed to log", str(e))

trade_logger = TradeLogger(config.CSV_LOG_PATH)

class FailedPairTracker:
    def __init__(self):
        self.failure_counts = defaultdict(int)
        self.excluded_pairs_timestamps = self._load_from_file()
        self.refresh_excluded_list()
    def _load_from_file(self):
        if os.path.exists(config.EXCLUDED_PAIRS_FILE):
            try:
                with open(config.EXCLUDED_PAIRS_FILE, "r", encoding="utf-8") as f: return json.load(f).get("excluded_pairs", {})
            except: return {}
        return {}
    def _save_to_file(self):
        try:
            tmp = config.EXCLUDED_PAIRS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f: json.dump({"excluded_pairs": self.excluded_pairs_timestamps}, f, indent=2)
            os.replace(tmp, config.EXCLUDED_PAIRS_FILE)
        except Exception as e: error_logger.log_error("FILE_SAVE", "Cannot save exclusions", str(e))
    def refresh_excluded_list(self):
        if not isinstance(self.excluded_pairs_timestamps, dict):
            logger.warning(f"⚠️ Found old or corrupt format in {config.EXCLUDED_PAIRS_FILE}. Resetting exclusions.")
            self.excluded_pairs_timestamps = {}
        now = time.time()
        cool_off_sec = getattr(config, 'AUTO_EXCLUDE_COOL_OFF_HOURS', 1.0) * 3600
        pairs_to_remove = [p for p, ts in self.excluded_pairs_timestamps.items() if now - ts > cool_off_sec]
        if pairs_to_remove:
            for pair in pairs_to_remove:
                del self.excluded_pairs_timestamps[pair]
                if pair in self.failure_counts: del self.failure_counts[pair]
                logger.info(f"✅ Re-enabled pair after cool-off: {pair}")
            self._save_to_file()
        display.excluded_pairs = sorted(list(self.excluded_pairs_timestamps.keys()))
    def record_failure(self, inst_id, error_code, error_msg):
        self.refresh_excluded_list()
        if not config.AUTO_EXCLUDE_FAILED_PAIRS or self.is_excluded(inst_id): return
        format_error_codes = {"51020", "51111", "51000", "51008", "51024"}
        if str(error_code) in format_error_codes:
            logger.warning(f"⚠️ Order format error for {inst_id} (not excluding): {error_msg}")
            return
        permanent_codes = {"51155", "51077", "51001", "51002"}
        if str(error_code) in permanent_codes:
            self.excluded_pairs_timestamps[inst_id] = time.time()
            self.refresh_excluded_list()
            logger.warning(f"🚫 Excluded {inst_id} (will cool-off): {error_msg}")
            self._save_to_file()
            return
        if str(error_code) in {"-1", "50011", "51014"}: return
        self.failure_counts[inst_id] += 1
        if self.failure_counts[inst_id] >= config.MAX_FAILURES_PER_PAIR:
            self.excluded_pairs_timestamps[inst_id] = time.time()
            self.refresh_excluded_list()
            logger.warning(f"🚫 Excluded {inst_id} after {self.failure_counts[inst_id]} failures (will cool-off)")
            self._save_to_file()
    def is_excluded(self, inst_id):
        self.refresh_excluded_list()
        return inst_id in self.excluded_pairs_timestamps

failed_pair_tracker = FailedPairTracker()

class ClientOrderIDGenerator:
    def __init__(self):
        self.counter = int(time.time() * 10) % 1000
        self.prefix = f"{config.TAG_PREFIX}{int(time.time()) % 10000:04d}"
    def generate(self, inst_id: str) -> str:
        self.counter += 1
        ms_timestamp = int(time.time() * 1000)
        pair_code = inst_id.split('-')[0][:4]
        return f"{self.prefix}{pair_code}{ms_timestamp}{self.counter:03d}"[:32]

order_id_gen = ClientOrderIDGenerator()

class VolatilePairScanner:
    def __init__(self, order_manager):
        self.order_manager = order_manager
        self.last_scan = 0
        self.movers = []
    async def scan_for_volatile_pairs(self):
        if time.time() - self.last_scan < config.SCAN_ALL_PAIRS_INTERVAL: return self.movers
        try:
            result = await self.order_manager._make_request("GET", "/api/v5/market/tickers", {"instType": config.INSTRUMENT_TYPE})
            if not result or result.get("code") != "0":
                error_logger.log_error("SCANNER_ERROR", "Failed to get tickers", str(result.get("msg")))
                return []
            movers = []
            watchlist = {w.upper() for w in config.WATCHLIST} if hasattr(config, "WATCHLIST") and config.WATCHLIST else None
            for t in result.get("data", []):
                try:
                    inst_id = t["instId"]
                    if not inst_id.upper().endswith(f"-{config.QUOTE_CCY}"): continue
                    if watchlist and inst_id.upper() not in watchlist: continue
                    if inst_id.upper() not in self.order_manager.meta or failed_pair_tracker.is_excluded(inst_id): continue
                    vol_24h = float(t.get("volCcy24h", 0) or 0)
                    if vol_24h < config.MIN_ABSOLUTE_24H_VOL_USDT: continue
                    high, low = float(t.get("high24h", 0) or 0), float(t.get("low24h", 0) or 0)
                    if high > 0 and low > 0 and ((high - low) / low) * 100 >= config.MIN_24H_VOLATILITY_PCT:
                        movers.append({"instId": inst_id, "volatility": ((high - low) / low) * 100, "volume": vol_24h})
                except: continue
            movers.sort(key=lambda x: (x["volatility"], x["volume"]), reverse=True)
            self.movers = movers[: config.TOP_N_VOLATILE_PAIRS]
            self.last_scan = time.time()
            if len(self.movers) > 0: logger.info(f"📡 Scan found {len(movers)} volatile pairs (top {len(self.movers)} selected)")
            return self.movers
        except Exception as e:
            error_logger.log_error("SCANNER_ERROR", "Scan failed", str(e))
            return []

class RiskManager:
    def __init__(self):
        self.daily_pnl = Decimal("0")
        self.consecutive_losses = 0
        self.last_trade_time = 0
        self.last_loss_time = 0
        self.pair_trade_times = defaultdict(list)
    def can_open_position(self, inst_id=None, open_positions=0):
        now = time.time()
        if now - self.last_trade_time < config.COOL_DOWN_AFTER_EXIT_SEC: return False, f"Cooldown ({int(config.COOL_DOWN_AFTER_EXIT_SEC - (now - self.last_trade_time))}s)"
        if self.consecutive_losses > 0 and now - self.last_loss_time < config.COOL_DOWN_AFTER_LOSS_SEC: return False, f"Loss cooldown ({int(config.COOL_DOWN_AFTER_LOSS_SEC - (now - self.last_loss_time))}s)"
        if self.daily_pnl <= -Decimal(str(config.MAX_DAILY_LOSS)): return False, "Daily loss limit hit"
        if self.consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES: return False, f"Max consecutive losses ({self.consecutive_losses})"
        if open_positions >= int(config.MAX_CONCURRENT_TRADES): return False, f"Max concurrent trades ({open_positions})"
        if inst_id and hasattr(config, "MAX_TRADES_PER_PAIR_PER_HOUR"):
            recent = [t for t in self.pair_trade_times[inst_id] if now - t < 3600]
            self.pair_trade_times[inst_id] = recent
            if len(recent) >= config.MAX_TRADES_PER_PAIR_PER_HOUR: return False, f"{inst_id} rate limit"
        return True, ""
    def record_trade(self, inst_id, pnl):
        self.daily_pnl += Decimal(pnl)
        self.last_trade_time = time.time()
        self.pair_trade_times[inst_id].append(time.time())
        if pnl < 0:
            self.consecutive_losses += 1
            self.last_loss_time = time.time()
        else: self.consecutive_losses = 0

rm_global = RiskManager()
rejection_log_file = None
rejection_log_writer = None

def setup_rejection_logger():
    global rejection_log_file, rejection_log_writer
    if not config.LOG_REJECTION_REASONS:
        return
    try:
        is_new_file = not os.path.exists(config.REJECTION_LOG_PATH)
        rejection_log_file = open(config.REJECTION_LOG_PATH, "a", encoding="utf-8", newline="")
        rejection_log_writer = csv.writer(rejection_log_file)
        if is_new_file:
            rejection_log_writer.writerow(["Timestamp", "Pair", "Stage", "Reason", "Details"])
        logger.info(f"✓ Rejection log: {config.REJECTION_LOG_PATH}")
    except Exception as e:
        logger.warning(f"Could not create rejection log: {e}")

setup_rejection_logger()