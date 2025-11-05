# order_manager.py

import json
import hmac
import base64
import time
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode
from datetime import datetime, timezone
import asyncio

import aiohttp
from aiohttp import TCPConnector

import config
from utils import logger, error_logger, failed_pair_tracker, order_id_gen

try:
    from aiohttp.resolver import AsyncResolver
except ImportError:
    AsyncResolver = None

class OrderManager:
    def __init__(self):
        self.meta = {}
        self.time_offset = 0.0
        self.session = None
        self.usdt_balance = Decimal("0")
        self.order_id_gen = order_id_gen

    async def initialize_session(self):
        connector = TCPConnector(resolver=AsyncResolver(), limit=80, ttl_dns_cache=600) if AsyncResolver else TCPConnector(limit=80, ttl_dns_cache=600)
        self.session = aiohttp.ClientSession(connector=connector)
        await self._sync_server_time()
        await self.load_instrument_meta()
        await self.update_balance()

    async def close_session(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _sync_server_time(self):
        try:
            async with self.session.get("https://www.okx.com/api/v5/public/time", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if data and data.get("code") == "0":
                        ts = float(data["data"][0]["ts"]) / 1000.0
                        self.time_offset = ts - time.time()
                        logger.info(f"✓ Server time synced (offset: {self.time_offset:.3f}s)")
                        return
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            error_logger.log_error("TIME_SYNC", "Failed to sync server time", str(e))
        logger.warning("⚠️ Time sync failed. Using system time.")
        self.time_offset = 0.0
        
    @staticmethod
    def _iso_timestamp(ts=None):
        dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    @staticmethod
    def _okx_sign(ts, method, path, body, secret):
        prehash = f"{ts}{method.upper()}{path}{body}"
        mac = hmac.new(secret.encode(), prehash.encode(), "sha256")
        return base64.b64encode(mac.digest()).decode()

    async def _make_request(self, method, path, params=None, body=None, retries=3):
        full_path = f"{path}?{urlencode(sorted(params.items()), doseq=True)}" if params else path
        url = f"https://www.okx.com{full_path}"
        body_str = json.dumps(body) if body else ""
        for i in range(retries):
            try:
                headers = {"Content-Type": "application/json"}
                if config.DEMO_TRADING == "1": headers["x-simulated-trading"] = "1"
                ts = self._iso_timestamp(time.time() + self.time_offset)
                headers.update({
                    "OK-ACCESS-KEY": config.API_KEY,
                    "OK-ACCESS-SIGN": self._okx_sign(ts, method, full_path, body_str, config.API_SECRET),
                    "OK-ACCESS-TIMESTAMP": ts,
                    "OK-ACCESS-PASSPHRASE": config.API_PASSPHRASE,
                })
                async with self.session.request(
                    method, url, headers=headers, data=body_str.encode("utf-8"),
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status != 200:
                        logger.warning(f"API Error (attempt {i+1}/{retries}): Status {response.status} | {await response.text()}")
                        await asyncio.sleep(0.5)
                        continue
                    return await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                error_logger.log_error("NETWORK_ERROR", f"Request failed: {path}", str(e))
                await asyncio.sleep(0.5)
        return {"code": "-1", "msg": "Max retries exceeded"}

    def _dp_from_str(self, s):
        if "e" in str(s).lower(): s = format(Decimal(s), "f")
        return len(str(s).split(".")[1].rstrip("0")) if "." in str(s) else 0

    async def load_instrument_meta(self):
        logger.info("Loading instrument metadata...")
        res = await self._make_request("GET", "/api/v5/public/instruments", {"instType": config.INSTRUMENT_TYPE})
        if not res or res.get("code") != "0": raise SystemExit("FATAL: Cannot fetch instruments")
        count = 0
        for d in res.get("data", []):
            if d.get("state") == "live" and d["instId"].endswith(f"-{config.QUOTE_CCY}"):
                try:
                    self.meta[d["instId"]] = {
                        "tickDp": self._dp_from_str(d["tickSz"]), "lotDp": self._dp_from_str(d["lotSz"]),
                        "minSz": float(d["minSz"]), "tickSz": Decimal(d["tickSz"]), "lotSz": Decimal(d["lotSz"]),
                    }
                    count += 1
                except: pass
        logger.info(f"✓ Loaded {count} instruments")
        if count == 0: raise SystemExit("FATAL: No instruments loaded")

    async def update_balance(self):
        res = await self._make_request("GET", "/api/v5/account/balance")
        if res and res.get("code") == "0" and res.get("data"):
            for detail in res["data"][0].get("details", []):
                if detail.get("ccy") == "USDT":
                    self.usdt_balance = Decimal(detail.get("availBal", "0"))
                    return self.usdt_balance
        return self.usdt_balance

    def _fmt_amount(self, x, dp):
        return (x.quantize(Decimal(10) ** -dp, ROUND_DOWN).to_eng_string())

    async def place_market_order(self, inst_id, side, amount, cl_ord_id, is_quote_amount=False):
        md = self.meta.get(inst_id)
        if not md: return None, "No metadata"
        
        # FIX: For market buy, 'sz' is the quote currency amount (USDT).
        # For market sell, 'sz' is the base currency amount.
        size_param = self._fmt_amount(amount, md["lotDp"])
        if side == 'buy' and is_quote_amount:
            # OKX expects USDT amount for market buy size
            size_param = str(amount)

        payload = {
            "instId": inst_id, "tdMode": "cash", "side": side, "ordType": "market",
            "sz": size_param, "clOrdId": cl_ord_id, "tag": config.TAG_PREFIX,
        }
        logger.info(f"📤 Placing MARKET order: {side.upper()} {inst_id} | Size Param: {size_param}")
        res = await self._make_request("POST", "/api/v5/trade/order", body=payload)
        
        if not res or res.get("code") != "0": return None, res.get("msg", "API Error")
        data = res.get("data", [{}])[0]
        if data.get("sCode") != "0": return None, data.get("sMsg", "Order Rejected")
        
        return data.get("ordId"), None

    async def place_oco_order(self, inst_id, size, tp_price, sl_price, algo_cl_ord_id_base):
        md = self.meta.get(inst_id)
        if not md: return None, "No metadata"
        
        rounded_size = size.quantize(md["lotSz"], rounding=ROUND_DOWN)

        payload = {
            "instId": inst_id, "tdMode": "cash", "side": "sell", "ordType": "oco",
            "sz": self._fmt_amount(rounded_size, md["lotDp"]),
            "tpTriggerPx": self._fmt_amount(tp_price, md["tickDp"]),
            "tpOrdPx": "-1",
            "slTriggerPx": self._fmt_amount(sl_price, md["tickDp"]),
            "slOrdPx": "-1",
            "algoClOrdId": algo_cl_ord_id_base
        }
        
        logger.info(f"📤 Placing OCO order for {inst_id}: TP @ {tp_price:.6f}, SL @ {sl_price:.6f}")
        res = await self._make_request("POST", "/api/v5/trade/order-algo", body=payload)

        if res and res.get("code") == "0" and res.get("data") and res["data"][0].get("sCode") == "0":
            algo_id = res["data"][0].get("algoId")
            logger.info(f"✓ OCO order placed for {inst_id} with AlgoID {algo_id}")
            return algo_id, None
        
        msg = (res.get('msg') if res else 'No response') or (res.get('data',[{}])[0].get('sMsg') if res and res.get('data') else 'No sMsg')
        logger.warning(f"❌ OCO order failed for {inst_id}: {msg}")
        return None, msg

    async def cancel_algo_order(self, inst_id, algo_id):
        if not algo_id: return None, "Invalid algo_id"
        
        payload = [{"instId": inst_id, "algoId": str(algo_id)}]
        res = await self._make_request("POST", "/api/v5/trade/cancel-algos", body=payload)
        
        if not res or res.get("code") != "0": return None, res.get("msg", "API Error")
        data = res.get("data", [{}])[0]
        if data.get("sCode") != "0" and data.get("sCode") not in ["51300"]:
            logger.warning(f"⚠️ Algo cancel failed for {algo_id}: {data.get('sMsg')}")
            return None, data.get("sMsg", "Cancel failed")
        
        logger.info(f"✓ Algo order {algo_id} cancelled.")
        return data.get("algoId"), None