# strategy.py
# Corrected: Added missing 'datetime' import.

import asyncio
import time
import traceback
from collections import deque, defaultdict
from decimal import Decimal
from math import log as math_log
from datetime import datetime # <--- FIX: ADDED MISSING IMPORT

import config
from utils import logger, error_logger, display, rm_global, trade_logger, safe_decimal, diagnostics

class BarAggregator:
    """Aggregates real-time ticks into time-based bars (e.g., 1-second bars)."""
    def __init__(self, order_manager, bar_period_sec=1.0, max_bars=400):
        self.order_manager = order_manager
        self.bar_period = bar_period_sec
        self.bars = deque(maxlen=max_bars)
        self.current_bar = None
        self.current_bar_start = 0
        self.trade_volumes = []

    def add_tick(self, price, volume=0):
        """Adds a new price tick to the current bar or creates a new one."""
        server_now = time.time() + self.order_manager.time_offset
        bar_bucket = int(server_now / self.bar_period) * self.bar_period

        if self.current_bar is None or bar_bucket > self.current_bar_start:
            if self.current_bar:
                total_vol = sum(self.trade_volumes) if self.trade_volumes else 0
                self.bars.append((*self.current_bar, total_vol))
            
            self.current_bar_start = bar_bucket
            self.current_bar = (bar_bucket, price, price, price, price)
            self.trade_volumes = []

        _, o, h, l, _ = self.current_bar
        self.current_bar = (bar_bucket, o, max(h, price), min(l, price), price)
        if volume > 0:
            self.trade_volumes.append(volume)

    def get_closes(self, n=None):
        closes = [bar[4] for bar in self.bars]
        return closes[-n:] if n else closes

    def get_bars(self, n=None): return list(self.bars)[-n:] if n else list(self.bars)
    def __len__(self): return len(self.bars)


class MomentumSniper:
    def __init__(self, scanner, order_manager, risk_manager):
        self.scanner = scanner
        self.order_manager = order_manager
        self.risk_manager = risk_manager
        self.order_manager._sniper_ref = self
        self.pairs_data = {}
        self.active_position = None
        self.position_lock = asyncio.Lock()
        self.public_ws = None
        self.private_ws = None
        self.hot_pairs = set()
        self.armed_candidates = {}
        self.last_setup_log = defaultdict(float)
        self.bar_aggregators = defaultdict(lambda: BarAggregator(self.order_manager, config.BAR_SAMPLING_SEC, config.BAR_HISTORY_MAX))

    def set_ws_managers(self, public_ws, private_ws):
        self.public_ws = public_ws
        self.private_ws = private_ws

    def _log_rejection(self, inst_id, stage, reason, details=""):
        from utils import rejection_log_writer, rejection_log_file
        log_key = f"{inst_id}-{stage}-{reason}"
        if time.time() - self.last_setup_log[log_key] < 5: return
        self.last_setup_log[log_key] = time.time()
        
        display.signals_rejected += 1
        if not config.LOG_REJECTION_REASONS or not rejection_log_writer: return
        
        ts = datetime.now().strftime("%H:%M:%S")
        try:
            rejection_log_writer.writerow([ts, inst_id, stage, reason, details])
            if rejection_log_file: rejection_log_file.flush()
        except: pass

    def compute_ema(self, values, period):
        if len(values) < period: return None
        k = 2.0 / (period + 1.0)
        ema = values[0]
        for v in values[1:]: ema = (v * k) + (ema * (1.0 - k))
        return Decimal(str(ema))

    def compute_zscore(self, returns):
        if len(returns) < 5: return 0.0
        try:
            arr = [float(r) for r in returns]
            mean, var = sum(arr) / len(arr), sum((x - mean) ** 2 for x in arr) / len(arr)
            std = var**0.5 if var > 0 else 1e-9
            return (arr[-1] - mean) / std
        except: return 0.0

    async def check_setup_conditions(self, inst_id):
        try:
            aggregator = self.bar_aggregators.get(inst_id)
            if not aggregator or len(aggregator) < config.MIN_CANDLES_FOR_ENTRY: return

            if inst_id in self.armed_candidates: return

            closes = aggregator.get_closes()
            if len(closes) < config.MIN_CANDLES_FOR_ENTRY: return

            if config.REQUIRE_EMA_CROSS:
                ema_fast, ema_slow = self.compute_ema(closes, config.EMA_FAST), self.compute_ema(closes, config.EMA_SLOW)
                if not ema_fast or not ema_slow: return
                if ema_fast <= ema_slow: return

            current_price = Decimal(str(closes[-1]))
            lookback_closes = closes[-config.LOOKBACK_CANDLES:]
            if len(lookback_closes) < 2: return
            
            recent_high = Decimal(str(max(lookback_closes[:-1])))

            if config.BREAKOUT_DEAD_ZONE_BPS > 0:
                pd = self.pairs_data.get(inst_id, {})
                last_armed_high = pd.get('last_armed_high', Decimal(0))
                if recent_high == last_armed_high:
                    dead_zone_price = recent_high * (Decimal(1) - Decimal(str(config.BREAKOUT_DEAD_ZONE_BPS)) / Decimal(10000))
                    if current_price > dead_zone_price:
                        self._log_rejection(inst_id, "Setup", "In Dead Zone", f"C:{float(current_price):.6f} > DZ:{float(dead_zone_price):.6f}")
                        return

            if current_price <= recent_high:
                self._log_rejection(inst_id, "Setup", "No Breakout", f"C:{float(current_price):.6f}<=H{float(recent_high):.6f}")
                return

            breakout_bps = (current_price - recent_high) / recent_high * 10000
            if breakout_bps < Decimal(str(config.MIN_BREAKOUT_STRENGTH_BPS)):
                self._log_rejection(inst_id, "Setup", "Weak Breakout", f"{float(breakout_bps):.2f}bps")
                return
            
            if config.BREAKOUT_DEAD_ZONE_BPS > 0:
                if inst_id not in self.pairs_data: self.pairs_data[inst_id] = {}
                self.pairs_data[inst_id]['last_armed_high'] = recent_high
            
            bars = aggregator.get_bars(config.ATR_PERIOD + 1)
            if len(bars) < config.ATR_PERIOD + 1: return
            
            trs = [max(curr[2] - curr[3], abs(curr[2] - prev[4]), abs(curr[3] - prev[4])) for prev, curr in zip(bars, bars[1:])]
            if not trs: return

            atr = Decimal(str(sum(trs) / len(trs)))
            atr_bps = (atr / current_price) * 10000
            if atr_bps < Decimal(str(config.MIN_ATR_BPS)):
                self._log_rejection(inst_id, "Setup", "Low ATR", f"{float(atr_bps):.2f}bps")
                return

            tp_dist, sl_dist = atr * Decimal(str(config.TP_ATR_MULTIPLIER)), atr * Decimal(str(config.SL_ATR_MULTIPLIER))
            raw_tp_bps, raw_sl_bps = float((tp_dist / current_price) * 10000), float((sl_dist / current_price) * 10000)
            
            expected_cost_bps = config.FEE_TAKER_BPS + config.MAX_SLIPPAGE_BPS
            if (raw_tp_bps - expected_cost_bps) <= 0 or raw_sl_bps <= 0: return

            rr = (raw_tp_bps - expected_cost_bps) / (raw_sl_bps + expected_cost_bps)
            if rr < config.MIN_RISK_REWARD_RATIO:
                self._log_rejection(inst_id, "Setup", "Low R:R", f"rr={rr:.2f}")
                return

            tp_bps_adj = max(config.MIN_TP_BPS, min(config.MAX_TP_BPS, raw_tp_bps))
            sl_bps_adj = max(config.MIN_SL_BPS, min(config.MAX_SL_BPS, raw_sl_bps))
            
            self.armed_candidates[inst_id] = {
                "entry_price": current_price,
                "tp": current_price * (Decimal("1") + Decimal(str(tp_bps_adj)) / Decimal("10000")),
                "sl": current_price * (Decimal("1") - Decimal(str(sl_bps_adj)) / Decimal("10000")),
                "tp_bps": tp_bps_adj, "sl_bps": sl_bps_adj, "atr": float(atr),
                "breakout_strength": float(breakout_bps), "rr": rr, "time": time.time(),
            }
            logger.info(f"🔎 SETUP FOUND: {inst_id} armed. Awaiting execution trigger...")
        except Exception as e:
            if inst_id in self.pairs_data: self.pairs_data[inst_id].pop('last_armed_high', None)
            error_logger.log_error("SETUP_SCAN", f"Error for {inst_id}", traceback.format_exc(e))

    async def check_execution_conditions(self, inst_id):
        try:
            if inst_id not in self.armed_candidates: return
            pd = self.pairs_data.get(inst_id)
            if not pd or not pd.get("order_book"): return
            async with self.position_lock:
                if self.active_position: return
                candidate = self.armed_candidates.get(inst_id)
                if not candidate: return
                now = time.time()
                if now - candidate.get("time", 0) > config.ARMED_CANDIDATE_TIMEOUT_SEC:
                    del self.armed_candidates[inst_id]
                    self._log_rejection(inst_id, "Execution", "Stale Candidate")
                    return
                can, reason = self.risk_manager.can_open_position(inst_id, 0)
                if not can:
                    self._log_rejection(inst_id, "Execution", "Risk", reason)
                    return
                spread_pct = (pd["best_ask"] - pd["best_bid"]) / pd["best_ask"] * 100
                if spread_pct > Decimal(str(config.MAX_SPREAD_PCT)):
                    self._log_rejection(inst_id, "Execution", "Wide Spread", f"{float(spread_pct):.3f}%")
                    return
                confirmations = 0
                reasons = []
                aggregator = self.bar_aggregators.get(inst_id)
                if aggregator and len(aggregator) >= config.ZSCORE_WINDOW:
                    closes = aggregator.get_closes(config.ZSCORE_WINDOW)
                    if len(closes) > 1:
                        rets = [math_log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
                        if rets:
                            z_score = self.compute_zscore(rets)
                            if abs(z_score) >= config.ZSCORE_THRESHOLD:
                                confirmations += 1
                                reasons.append(f"ZOK({z_score:.2f})")
                
                if aggregator and len(aggregator) >= config.VOLUME_AVG_WINDOW:
                    volumes = [b[5] for b in aggregator.get_bars(config.VOLUME_AVG_WINDOW)]
                    if len(volumes) >= config.VOLUME_AVG_WINDOW:
                        recent_vol = sum(volumes[-config.VOLUME_SPIKE_WINDOW:])
                        base_vols = volumes[:-config.VOLUME_SPIKE_WINDOW]
                        if base_vols and sum(base_vols) > 0:
                            avg_base_vol = sum(base_vols) / len(base_vols)
                            if avg_base_vol > 0:
                                mult = recent_vol / (avg_base_vol * config.VOLUME_SPIKE_WINDOW)
                                if mult >= config.VOLUME_SPIKE_MULTIPLIER:
                                    confirmations += 1
                                    reasons.append(f"VolOK(x{mult:.1f})")

                bids, asks = pd["order_book"]["bids"], pd["order_book"]["asks"]
                if bids and asks:
                    bid_vol = sum(Decimal(b[1])*Decimal(b[0]) for b in bids)
                    ask_vol = sum(Decimal(a[1])*Decimal(a[0]) for a in asks)
                    if ask_vol > 0 and bid_vol >= Decimal(str(config.MIN_BID_DEPTH_USDT)):
                        imb = float(bid_vol / ask_vol)
                        if config.MIN_IMBALANCE <= imb <= config.MAX_IMBALANCE:
                            confirmations += 1
                            reasons.append(f"ImbOK({imb:.2f})")

                if confirmations < config.MIN_CONFIRMATIONS_NEEDED:
                    self._log_rejection(inst_id, "Execution", "Confirmations Failed", f"{confirmations}/{config.MIN_CONFIRMATIONS_NEEDED} {','.join(reasons)}")
                    return
                
                signal = self.armed_candidates.pop(inst_id, None)
                if not signal: return
                
                signal["imbalance"] = float(imb) if 'imb' in locals() else 0
                display.signals_detected += 1
                logger.info(f"🎯 EXECUTION {inst_id} @ {float(pd['mid']):.6f} | {','.join(reasons)}")
                asyncio.create_task(self.enter_position(inst_id, signal))
        except Exception as e:
            if inst_id in self.armed_candidates: del self.armed_candidates[inst_id]
            error_logger.log_error("EXEC_TRIGGER", f"Error for {inst_id}", traceback.format_exc())

    async def handle_bbo_data(self, inst_id, bbo_data_list):
        diagnostics.record_message("bbo-tbt", inst_id)
        if not bbo_data_list: return
            
        try:
            bbo_data = bbo_data_list[0]
            bids, asks = bbo_data.get('bids'), bbo_data.get('asks')
            if not bids or not asks or not bids[0] or not asks[0]: return

            if inst_id not in self.pairs_data: self.pairs_data[inst_id] = {}

            best_bid_px, best_ask_px = Decimal(bids[0][0]), Decimal(asks[0][0])
            self.pairs_data[inst_id].update({
                "best_bid": best_bid_px, "best_ask": best_ask_px, "mid": (best_bid_px + best_ask_px) / 2,
                "order_book": {"bids": bids, "asks": asks}
            })

            mid_price = float((best_bid_px + best_ask_px) / 2)
            if mid_price > 0:
                self.bar_aggregators[inst_id].add_tick(mid_price)
                await asyncio.sleep(0)
                asyncio.create_task(self.check_setup_conditions(inst_id))
                asyncio.create_task(self.check_execution_conditions(inst_id))
        except (KeyError, IndexError, ValueError, TypeError) as e:
            if config.DEBUG_MODE: logger.debug(f"Error parsing BBO for {inst_id}: {e} | Data: {bbo_data_list}")

    async def handle_trade_data(self, inst_id, trades):
        diagnostics.record_message("trades", inst_id)
        aggregator = self.bar_aggregators[inst_id]
        for tr in trades:
            try:
                price = float(tr["px"])
                if price > 0: aggregator.add_tick(price, float(tr["sz"]))
            except: continue

    async def handle_account_update(self, data):
        for update in data:
            if "details" in update:
                for detail in update.get("details", []):
                    if detail.get("ccy") == "USDT":
                        new_bal = Decimal(detail.get("availBal", "0"))
                        if new_bal != self.order_manager.usdt_balance:
                            self.order_manager.usdt_balance = new_bal
                            display.usdt_balance = new_bal
                continue
            
            async with self.position_lock:
                if not self.active_position: continue
                pos = self.active_position
                if update.get("instId") != pos["inst_id"]: continue

                is_entry_fill = update.get("ordId") == pos.get("entry_order_id") and update.get("state") == "filled"
                is_tpsl_fill = pos.get("attach_algo_id") and update.get("algoId") == pos.get("attach_algo_id")
                is_manual_exit_fill = pos.get("exit_order_id") and update.get("ordId") == pos.get("exit_order_id") and update.get("state") == "filled"

                if is_entry_fill and pos.get("state") == "PENDING_ENTRY":
                    pos.update({
                        "entry_price": safe_decimal(update["avgPx"]), "entry_size": safe_decimal(update["accFillSz"]),
                        "entry_fee": abs(safe_decimal(update.get("fee", "0"))), "state": "OPEN", "time": time.time(),
                        "peak_price": safe_decimal(update["avgPx"])
                    })
                    logger.info(f"✅ ENTRY FILLED: {pos['inst_id']} | Size: {pos['entry_size']} @ ${float(pos['entry_price']):.6f}")
                    display.active_position = pos

                elif (is_tpsl_fill or is_manual_exit_fill) and pos.get("state") == "OPEN":
                    if is_tpsl_fill:
                        exit_price = safe_decimal(update.get("lastPx", update.get("avgPx")))
                        exit_fee = abs(safe_decimal(update.get("fee", "0")))
                    else:
                        exit_price = safe_decimal(update["avgPx"])
                        exit_fee = abs(safe_decimal(update.get("fee", "0")))

                    pos.update({
                        "exit_price": exit_price,
                        "exit_fee": pos.get("exit_fee", 0) + exit_fee,
                        "exit_type": pos.get("exit_type", "TP/SL_HIT")
                    })
                    logger.info(f"✅ EXIT FILLED for {pos['inst_id']} via {pos['exit_type']}")
                    await self.finalize_trade()

    async def enter_position(self, inst_id, signal):
        async with self.position_lock:
            if self.active_position: return
            
            await self.order_manager.update_balance()
            if self.order_manager.usdt_balance < Decimal(str(config.BASE_ORDER_NOTIONAL_USDT)):
                logger.warning(f"⚠️ Insufficient balance: ${self.order_manager.usdt_balance}")
                return
            
            size_base = Decimal(str(config.BASE_ORDER_NOTIONAL_USDT)) / signal["entry_price"]
            entry_cl_ord_id = self.order_manager.order_id_gen.generate(inst_id)
            
            entry_ord_id, attach_algo_id, error = await self.order_manager.place_order_with_tpsl(
                inst_id=inst_id, side="buy", size=size_base, tp_price=signal["tp"],
                sl_price=signal["sl"], cl_ord_id=entry_cl_ord_id
            )
            
            if error:
                logger.error(f"❌ Order placement failed: {error}")
                return
            
            self.active_position = {
                "inst_id": inst_id, "entry_order_id": entry_ord_id, "clOrdId": entry_cl_ord_id,
                "attach_algo_id": attach_algo_id, "tp_price": signal["tp"], "sl_price": signal["sl"],
                "tp_bps": signal["tp_bps"], "sl_bps": signal["sl_bps"], "atr": signal["atr"],
                "rr": signal.get("rr", 0), "time": time.time(), "state": "PENDING_ENTRY"
            }
            display.active_position = self.active_position
            logger.info(f"✅ Position pending for {inst_id} with attached TP/SL.")

    async def monitor_position(self):
        while True:
            await asyncio.sleep(1.0)
            if not self.active_position: continue

            async with self.position_lock:
                if not self.active_position: continue
                pos, inst_id, now = self.active_position, self.active_position["inst_id"], time.time()
                
                if pos.get("state") in ["CLOSING_FAILED"]: continue

                if pos.get("state") == "PENDING_ENTRY" and now - pos.get("time", now) > config.ORDER_PENDING_TIMEOUT:
                    res = await self.order_manager._make_request("GET", "/api/v5/trade/order", params={"instId": inst_id, "ordId": pos["entry_order_id"]})
                    if res and res.get("code") == "0" and res.get("data"):
                        if res["data"][0].get("state") == "filled":
                            await self.handle_account_update(res["data"])
                        else:
                            logger.warning(f"Entry order {pos['entry_order_id']} for {inst_id} timed out. Cancelling.")
                            if pos.get("attach_algo_id"): await self.order_manager.cancel_algo_order(inst_id, pos['attach_algo_id'])
                            await self.order_manager._make_request("POST", "/api/v5/trade/cancel-order", body={"instId": inst_id, "ordId": pos["entry_order_id"]})
                            self.active_position = None
                            display.active_position = None
                    continue
                
                if pos.get("state") == "OPEN":
                    current_price = self.pairs_data.get(inst_id, {}).get("mid")
                    if not current_price: continue

                    pos["current"] = current_price
                    
                    if now - pos.get("time", now) > config.MAX_POSITION_HOLD_SECONDS:
                        await self.force_close_position("MAX_HOLD_TIME")
                        continue

                    if config.ENABLE_TRAILING_STOP and pos.get("entry_price"):
                        if "peak_price" not in pos or not pos["peak_price"]: pos["peak_price"] = pos["entry_price"]
                        if current_price > pos["peak_price"]: pos["peak_price"] = current_price
                        
                        activation_price = pos["entry_price"] * (Decimal("1") + Decimal(str(config.TRAILING_ACTIVATION_BPS)) / Decimal("10000"))
                        if not pos.get("trailing_active") and current_price >= activation_price:
                            pos["trailing_active"] = True
                            logger.info(f"📈 Trailing stop ACTIVATED for {inst_id}")
                            if pos.get("attach_algo_id"):
                                logger.info(f"Cancelling attached algo order {pos['attach_algo_id']} to enable trailing stop.")
                                await self.order_manager.cancel_algo_order(inst_id, pos['attach_algo_id'])
                                pos['attach_algo_id'] = None
                        
                        if pos.get("trailing_active"):
                            trail_dist = max(Decimal(str(pos.get("atr", 0))) * Decimal(str(config.TRAILING_DISTANCE_ATR_MULTIPLIER)), pos["entry_price"] * Decimal(str(config.TRAILING_DISTANCE_MIN_BPS)) / Decimal("10000"))
                            if current_price <= pos["peak_price"] - trail_dist:
                                await self.force_close_position("TRAILING_STOP")
                                continue

    async def force_close_position(self, reason):
        if not self.active_position or self.active_position.get("state") != "OPEN": return
            
        pos, inst_id = self.active_position, self.active_position["inst_id"]
        pos["state"] = "CLOSING"
        
        logger.info(f"🚨 Force closing {inst_id} - Reason: {reason}")
        if pos.get("attach_algo_id"):
            await self.order_manager.cancel_algo_order(inst_id, pos["attach_algo_id"])
            
        await asyncio.sleep(0.2)

        size_to_sell = pos.get("entry_size")
        if not size_to_sell:
            self.active_position = None
            display.active_position = None
            return
            
        exit_ord_id, error = await self.order_manager.place_market_order(inst_id, "sell", size_to_sell)
        if not exit_ord_id:
            logger.error(f"❌ CRITICAL: Market sell failed on force close: {error}")
            pos["state"] = "CLOSING_FAILED"
            return
            
        pos.update({"exit_order_id": exit_ord_id, "exit_type": reason})
        logger.info(f"✅ Manual exit order placed for {inst_id}: {exit_ord_id}")

    async def finalize_trade(self):
        pos = self.active_position
        if not pos or not pos.get("entry_price") or not pos.get("exit_price"):
             self.active_position, display.active_position = None, None
             return

        entry_notional = pos["entry_price"] * pos["entry_size"]
        pnl_usdt = (pos["exit_price"] - pos["entry_price"]) * pos["entry_size"] - (pos.get("entry_fee", 0) + pos.get("exit_fee", 0))
        pnl_pct = (pnl_usdt / entry_notional) * 100 if entry_notional > 0 else 0
        hold_sec = int(time.time() - pos["time"])
        
        log_data = {**pos, "pnl_usdt": pnl_usdt, "pnl_pct": pnl_pct, "hold_sec": hold_sec, "pair": pos["inst_id"]}
        trade_logger.log_trade(**log_data)
        
        self.risk_manager.record_trade(pos["inst_id"], pnl_usdt)
        display.update_trade(pos["inst_id"], pnl_usdt, pos["entry_price"], pos["exit_price"], hold_sec)
        logger.info(f"TRADE COMPLETE: {pos['inst_id']} | P&L: ${float(pnl_usdt):.2f} ({pnl_pct:+.2f}%) | Exit: {pos['exit_type']} | Hold: {hold_sec}s")
        
        self.active_position, display.active_position = None, None
        if pos["inst_id"] in self.pairs_data:
            self.pairs_data[pos["inst_id"]].pop('last_armed_high', None)

    async def run_tasks(self):
        await asyncio.gather(self.monitor_position(), self.periodic_scanner_update())

    async def periodic_scanner_update(self):
        while True:
            await asyncio.sleep(config.SCAN_ALL_PAIRS_INTERVAL)
            try:
                movers = await self.scanner.scan_for_volatile_pairs()
                hot_pairs = {m["instId"] for m in movers}
                newly_hot = hot_pairs - self.hot_pairs
                newly_cold = self.hot_pairs - hot_pairs
                if newly_hot and self.public_ws: await self.public_ws.subscribe(list(newly_hot))
                if newly_cold and self.public_ws: await self.public_ws.unsubscribe(list(newly_cold))
                self.hot_pairs = hot_pairs
                display.hot_pairs = sorted(list(self.hot_pairs))
            except Exception as e:
                error_logger.log_error("SCANNER_LOOP", "Error in periodic scan", str(e))