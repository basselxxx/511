# ws_manager.py

import asyncio
import json
import hmac
import base64
import time
import traceback
import websockets

import config
from utils import logger, error_logger

class WSManager:
    def __init__(self, sniper, is_private: bool):
        self.sniper = sniper
        self.is_private = is_private
        self.ws = None
        self.should_run = True
        self.subscriptions = []
        self.initial_pairs = []

        if config.DEMO_TRADING == "1":
            self.url = config.WS_URL_DEMO_PRIVATE if is_private else config.WS_URL_DEMO_PUBLIC
        else:
            self.url = config.WS_URL_LIVE_PRIVATE if is_private else config.WS_URL_LIVE_PUBLIC

    async def connect(self):
        try:
            self.ws = await websockets.connect(self.url, ping_interval=20, ping_timeout=15)
            
            if self.is_private:
                ts = str(int(time.time() + self.sniper.order_manager.time_offset))
                sign = base64.b64encode(hmac.new(config.API_SECRET.encode(), f"{ts}GET/users/self/verify".encode(), "sha256").digest()).decode()
                login_payload = { "op": "login", "args": [{"apiKey": config.API_KEY, "passphrase": config.API_PASSPHRASE, "timestamp": ts, "sign": sign}] }
                await self.ws.send(json.dumps(login_payload))
                login_data = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=10))
                
                if not (login_data.get("event") == "login" and login_data.get("code") == "0"):
                    logger.error(f"❌ Private WebSocket Login Failed: {login_data.get('msg', 'Unknown')}")
                    return False
                logger.info("✓ Private WebSocket Authenticated")
            
            logger.info(f"✓ {'Private' if self.is_private else 'Public'} WebSocket Connected to {self.url}")
            
            if not self.is_private and self.initial_pairs: await self.subscribe(self.initial_pairs)
            else: await self.resubscribe()
                
            return True
        except Exception:
            error_logger.log_error("WS_CONNECT", f"Failed to connect {'private' if self.is_private else 'public'} WS", traceback.format_exc())
            return False

    async def _send_op(self, op, args):
        if not self.ws: return False
        try:
            await self.ws.send(json.dumps({"op": op, "args": args}))
            return True
        except websockets.exceptions.ConnectionClosed: return False

    async def subscribe(self, inst_ids):
        if self.is_private:
            args = [
                {"channel": "account", "ccy": "USDT"},
                {"channel": "orders", "instType": config.INSTRUMENT_TYPE},
            ]
            if config.INSTRUMENT_TYPE != "SPOT":
                args.append({"channel": "orders-algo", "instType": config.INSTRUMENT_TYPE})

            if await self._send_op("subscribe", args):
                self.subscriptions = args
                subs_str = ', '.join([a['channel'] for a in args])
                logger.info(f"✓ Subscribed to private channels: {subs_str}")
            return

        new_subs_ids = [i for i in inst_ids if i not in {s.get("instId") for s in self.subscriptions}]
        if not new_subs_ids: return
        
        for i in range(0, len(new_subs_ids), 20):
            batch = new_subs_ids[i:i+20]
            args = ([{"channel": "trades", "instId": inst} for inst in batch] +
                    [{"channel": "bbo-tbt", "instId": inst} for inst in batch])
            if await self._send_op("subscribe", args):
                self.subscriptions.extend(args)
                logger.info(f"✓ Subscribed to trades & bbo-tbt for {len(batch)} pairs")
                await asyncio.sleep(0.1)

    async def unsubscribe(self, inst_ids):
        if self.is_private or not inst_ids: return
        args_to_unsub = ([{"channel": "trades", "instId": i} for i in inst_ids] +
                         [{"channel": "bbo-tbt", "instId": i} for i in inst_ids])
        if await self._send_op("unsubscribe", args_to_unsub):
            self.subscriptions = [s for s in self.subscriptions if s.get("instId") not in set(inst_ids)]
            logger.info(f"✓ Unsubscribed from {len(inst_ids)} pairs.")

    async def resubscribe(self):
        if self.is_private: await self.subscribe([])
        elif self.subscriptions: await self._send_op("subscribe", self.subscriptions)

    async def run(self):
        while self.should_run:
            if not await self.connect():
                logger.info("Reconnecting in 15 seconds...")
                await asyncio.sleep(15)
                continue
            
            try:
                while self.should_run:
                    msg = await self.ws.recv()
                    if msg == 'pong': continue
                    data = json.loads(msg)

                    if "event" in data:
                        event = data.get("event")
                        if event == "error":
                            error_logger.log_error("WS_ERROR", data.get("msg", "Unknown WS Error"), str(data))
                        elif event in ["subscribe", "unsubscribe", "login"] and config.DEBUG_MODE:
                            logger.debug(f"WS Event: {data}")
                        continue

                    if "arg" in data and "data" in data:
                        arg, payload = data["arg"], data["data"]
                        channel = arg.get("channel")
                        
                        if channel == "trades": await self.sniper.handle_trade_data(arg.get("instId"), payload)
                        elif channel == "bbo-tbt": await self.sniper.handle_bbo_data(arg.get("instId"), payload)
                        elif channel in ["orders", "account", "orders-algo"]: await self.sniper.handle_account_update(payload)
                        continue
                        
            except (websockets.exceptions.ConnectionClosed, asyncio.TimeoutError) as e:
                logger.warning(f"WS {'Private' if self.is_private else 'Public'} disconnected: {type(e).__name__}.")
            except Exception:
                error_logger.log_error("WS_LOOP", "Unhandled error", traceback.format_exc())
            finally:
                if self.ws:
                    try: await self.ws.close()
                    except: pass
                logger.info("Reconnecting in 15 seconds...")
                await asyncio.sleep(15)

    async def stop(self):
        self.should_run = False
        if self.ws: await self.ws.close()
        logger.info(f"WebSocket {'Private' if self.is_private else 'Public'} stopped.")