#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, time, signal, threading, base64
from collections import deque
from decimal import Decimal, ROUND_DOWN, ROUND_UP, InvalidOperation, getcontext
from dataclasses import dataclass
from typing import Optional, Deque

import requests
from websocket import WebSocketApp
from nacl import signing
from nacl.encoding import RawEncoder
from dotenv import load_dotenv

# ---------------------- 基本设置 ----------------------
getcontext().prec = 28
API_HOST = "https://api.backpack.exchange"
WS_URL   = "wss://ws.backpack.exchange"

# ---------------------- 读取 .env ----------------------
load_dotenv()

# 必填/重要
SECRET_KEY         = os.getenv("SECRET_KEY", "").strip()        # ED25519 私钥（hex 或 base64），至少32字节
API_KEY_B64        = os.getenv("API_KEY_B64", "").strip()       # 可留空，留空将由私钥推导
SYMBOL             = os.getenv("SYMBOL", "SOL_USDC_PERP").strip()

# 交易行为
QTY                = Decimal(os.getenv("QTY", "0.10"))
POST_ONLY          = os.getenv("POST_ONLY", "true").lower() == "true"
SELF_TRADE_MODE    = os.getenv("SELF_TRADE_PREVENTION", "RejectTaker")

# maker 报价参数
MIN_SPREAD_TICKS   = int(os.getenv("MIN_SPREAD_TICKS", "1"))    # 小于该tick数不挂
BID_OFFSET_TICKS   = int(os.getenv("BID_OFFSET_TICKS", "0"))    # 0=贴最优买，1=退一档
ASK_OFFSET_TICKS   = int(os.getenv("ASK_OFFSET_TICKS", "0"))    # 0=贴最优卖，1=抬一档
REFRESH_MS         = int(os.getenv("REFRESH_MS", "1500"))       # 最快重挂频率
WS_STALE_MS        = int(os.getenv("WS_STALE_MS", "2000"))      # 超过该毫秒未更新视为过期
HTTP_TIMEOUT       = float(os.getenv("HTTP_TIMEOUT", "5"))
WINDOW_MS          = int(os.getenv("WINDOW_MS", "5000"))
DRY_RUN            = os.getenv("DRY_RUN", "false").lower() == "true"

# 库存偏移 & 硬阈暂停（需私有流才能生效）
USE_PRIVATE_WS         = os.getenv("USE_PRIVATE_WS", "0") == "1"
SOFT_POS               = Decimal(os.getenv("SOFT_POS", "0"))
HARD_POS               = Decimal(os.getenv("HARD_POS", "0"))
SKEW_TICKS_PER_UNIT    = int(os.getenv("SKEW_TICKS_PER_UNIT", "0"))

# 主动 reduceOnly 对冲
HEDGE_TRIGGER   = Decimal(os.getenv("HEDGE_TRIGGER", "0"))      # 绝对净仓位 ≥ 触发
HEDGE_CHUNK     = Decimal(os.getenv("HEDGE_CHUNK",   "0"))      # 每次对冲数量
HEDGE_MODE      = os.getenv("HEDGE_MODE", "market").lower()     # market / ioc_limit
HEDGE_START_BPS = Decimal(os.getenv("HEDGE_START_BPS", "5"))
HEDGE_STEP_BPS  = Decimal(os.getenv("HEDGE_STEP_BPS",  "5"))
HEDGE_MAX_STEPS = int(os.getenv("HEDGE_MAX_STEPS", "4"))
HEDGE_COOLDOWN_MS = int(os.getenv("HEDGE_COOLDOWN_MS", "1500"))
HEDGE_GRACE_MS    = int(os.getenv("HEDGE_GRACE_MS",    "2000"))

# 自适应 & 深度数据参数
ADAPTIVE_SPREAD          = os.getenv("ADAPTIVE_SPREAD", "false").lower() == "true"
ADAPTIVE_HEDGE           = os.getenv("ADAPTIVE_HEDGE", "false").lower() == "true"
DEPTH_LEVELS             = int(os.getenv("DEPTH_LEVELS", "5"))
DEPTH_STALE_MS           = int(os.getenv("DEPTH_STALE_MS", "3000"))
TRADE_WINDOW_SEC         = int(os.getenv("TRADE_WINDOW_SEC", "5"))
TRADE_STALE_MS           = int(os.getenv("TRADE_STALE_MS", "4000"))
LIQUIDITY_TARGET_VOLUME  = Decimal(os.getenv("LIQUIDITY_TARGET_VOLUME", "500"))
LIQUIDITY_VOLUME_STEP    = Decimal(os.getenv("LIQUIDITY_VOLUME_STEP", "100"))
IMBALANCE_THRESHOLD      = Decimal(os.getenv("IMBALANCE_THRESHOLD", "0.25"))
IMBALANCE_TICK_IMPACT    = int(os.getenv("IMBALANCE_TICK_IMPACT", "1"))
DEPTH_STREAM_PREFIX      = os.getenv("DEPTH_STREAM_PREFIX", "orderbook")
TRADE_STREAM_PREFIX      = os.getenv("TRADE_STREAM_PREFIX", "trade")

# ---------------------- 私钥 & 会话 ----------------------
def _load_signing_key():
    if not SECRET_KEY:
        raise RuntimeError("缺少 SECRET_KEY（支持 hex/base64）")
    sk_bytes = None
    # 尝试 hex
    try:
        sk_clean = SECRET_KEY.replace("0x", "").replace(" ", "")
        sk_bytes = bytes.fromhex(sk_clean)
    except Exception:
        pass
    # 尝试 base64
    if sk_bytes is None:
        try:
            sk_bytes = base64.b64decode(SECRET_KEY)
        except Exception:
            pass
    if sk_bytes is None or len(sk_bytes) < 32:
        raise RuntimeError("SECRET_KEY 非法：无法解析为 >=32 字节的 seed")
    seed = sk_bytes[:32]
    return signing.SigningKey(seed, encoder=RawEncoder)

SIGNING_KEY = _load_signing_key()
if not API_KEY_B64:
    API_KEY_B64 = base64.b64encode(bytes(SIGNING_KEY.verify_key)).decode()

SESSION = requests.Session()
SESSION.headers.update({"Content-Type": "application/json"})

def now_ms() -> int:
    return int(time.time() * 1000)

def _fmt_for_sig(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Decimal):
        return format(v, "f")
    return str(v)

def sign_headers(instruction: str, body: dict | None = None, params: dict | None = None):
    parts = [f"instruction={instruction}"]
    if body:
        for k in sorted(body.keys()):
            parts.append(f"{k}={_fmt_for_sig(body[k])}")
    if params:
        for k in sorted(params.keys()):
            parts.append(f"{k}={_fmt_for_sig(params[k])}")
    ts = now_ms()
    parts.append(f"timestamp={ts}")
    parts.append(f"window={WINDOW_MS}")
    msg = "&".join(parts).encode("utf-8")
    sig = SIGNING_KEY.sign(msg).signature
    return {
        "X-API-KEY": API_KEY_B64,
        "X-SIGNATURE": base64.b64encode(sig).decode(),
        "X-TIMESTAMP": str(ts),
        "X-WINDOW": str(WINDOW_MS),
    }

def ws_signature_tuple() -> list[str]:
    ts = str(now_ms())
    msg = f"instruction=subscribe&timestamp={ts}&window={WINDOW_MS}".encode()
    sig = SIGNING_KEY.sign(msg).signature
    return [API_KEY_B64, base64.b64encode(sig).decode(), ts, str(WINDOW_MS)]

# ---------------------- 市场过滤 ----------------------
@dataclass
class MarketFilters:
    tick: Decimal
    step: Decimal
    min_qty: Decimal
    max_qty: Optional[Decimal]


@dataclass
class DepthMetrics:
    ts_ms: int
    total_bid: Decimal
    total_ask: Decimal
    best_bid: Decimal
    best_ask: Decimal


@dataclass
class TradeEvent:
    ts_ms: int
    qty: Decimal
    side: str  # "buy" or "sell"


@dataclass
class TradeStats:
    ts_ms: int
    buy_volume: Decimal
    sell_volume: Decimal

    @property
    def total_volume(self) -> Decimal:
        return self.buy_volume + self.sell_volume

    @property
    def imbalance(self) -> Decimal:
        denom = self.total_volume
        if denom == 0:
            return Decimal("0")
        return (self.buy_volume - self.sell_volume) / denom


def _calc_liquidity_steps(total_bid: Decimal, total_ask: Decimal) -> int:
    if LIQUIDITY_TARGET_VOLUME <= 0 or LIQUIDITY_VOLUME_STEP <= 0:
        return 0
    min_depth = min(total_bid, total_ask)
    if min_depth >= LIQUIDITY_TARGET_VOLUME:
        return 0
    deficit = LIQUIDITY_TARGET_VOLUME - min_depth
    steps = int((deficit / LIQUIDITY_VOLUME_STEP).to_integral_value(rounding=ROUND_UP))
    return max(0, steps)


def adaptive_offsets(bid_offset: int, ask_offset: int,
                     depth: Optional[DepthMetrics],
                     trades: Optional[TradeStats]) -> tuple[int, int]:
    out_bid = bid_offset
    out_ask = ask_offset

    if depth is not None:
        extra = _calc_liquidity_steps(depth.total_bid, depth.total_ask)
        if extra > 0:
            out_bid += extra
            out_ask += extra

    if trades is not None and trades.total_volume > 0 and IMBALANCE_TICK_IMPACT > 0:
        imb = trades.imbalance
        threshold = IMBALANCE_THRESHOLD
        if threshold < 0:
            threshold = Decimal("0")
        if imb >= threshold:
            out_bid = max(0, out_bid - IMBALANCE_TICK_IMPACT)
            out_ask += IMBALANCE_TICK_IMPACT
        elif imb <= -threshold:
            out_ask = max(0, out_ask - IMBALANCE_TICK_IMPACT)
            out_bid += IMBALANCE_TICK_IMPACT

    return out_bid, out_ask


def adaptive_hedge_params(base_start: Decimal, base_step: Decimal, pos: Decimal,
                          depth: Optional[DepthMetrics], trades: Optional[TradeStats]) -> tuple[Decimal, Decimal]:
    start = base_start
    step = base_step

    if depth is not None and depth.total_bid >= 0 and depth.total_ask >= 0:
        extra = _calc_liquidity_steps(depth.total_bid, depth.total_ask)
        if extra > 0:
            # Liquidity thin → hedge sooner and increase follow-up aggressiveness
            adj = Decimal(extra)
            start = max(Decimal("1"), start - adj * base_step)
            step = max(Decimal("1"), step + adj)

    if trades is not None and trades.total_volume > 0:
        imb = trades.imbalance
        if pos > 0:
            if imb < Decimal("0"):
                start = max(Decimal("1"), start - base_step)
            elif imb > Decimal("0"):
                start = start + base_step
        elif pos < 0:
            if imb > Decimal("0"):
                start = max(Decimal("1"), start - base_step)
            elif imb < Decimal("0"):
                start = start + base_step

    return start, step

def _to_decimal_safe(v, default=None):
    if v is None:
        return default
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return default

def get_market_filters(symbol: str) -> MarketFilters:
    r = SESSION.get(f"{API_HOST}/api/v1/market", params={"symbol": symbol}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    d = r.json()
    price = d.get("filters", {}).get("price", {})
    qty   = d.get("filters", {}).get("quantity", {})

    tick  = _to_decimal_safe(price.get("tickSize"), Decimal("0.01"))
    step  = _to_decimal_safe(qty.get("stepSize") or qty.get("step") or qty.get("step_size"), Decimal("0.01"))
    min_q = _to_decimal_safe(qty.get("minQuantity") or qty.get("minQty") or qty.get("min"), Decimal("0"))
    max_q = _to_decimal_safe(qty.get("maxQuantity") or qty.get("maxQty") or qty.get("max"), None)

    print(f"[市场过滤] tickSize={tick} stepSize={step} minQty={min_q} maxQty={max_q if max_q is not None else '∞'}")
    return MarketFilters(tick=tick, step=step, min_qty=min_q, max_qty=max_q)

FILTERS = get_market_filters(SYMBOL)

def q_price(p: Decimal, side: str) -> Decimal:
    tick = FILTERS.tick
    rounding = ROUND_DOWN if side == "Bid" else ROUND_UP
    return (p / tick).to_integral_value(rounding=rounding) * tick

# 用于 IOC/吃单的激进对齐：买向上、卖向下
def q_price_aggr(p: Decimal, side: str) -> Decimal:
    tick = FILTERS.tick
    rounding = ROUND_UP if side == "Bid" else ROUND_DOWN
    return (p / tick).to_integral_value(rounding=rounding) * tick

def q_qty(q: Decimal) -> Decimal:
    step = FILTERS.step
    out = (q / step).to_integral_value(rounding=ROUND_DOWN) * step
    if out < FILTERS.min_qty:
        out = FILTERS.min_qty
    if FILTERS.max_qty is not None and out > FILTERS.max_qty:
        out = FILTERS.max_qty
    return out

# ---------------------- 下单/撤单 ----------------------
def post_order(side: str, price: Decimal, qty: Decimal) -> dict:
    body = {
        "symbol": SYMBOL,
        "side": side,                      # "Bid" / "Ask"
        "orderType": "Limit",
        "timeInForce": "GTC",
        "postOnly": True if POST_ONLY else False,
        "price": format(price, "f"),
        "quantity": format(qty, "f"),
        "selfTradePrevention": SELF_TRADE_MODE,
    }
    if DRY_RUN:
        print(f"[DRY] 下{ '买' if side=='Bid' else '卖' }单 {price} x {qty}")
        return {"id": "dry-run"}
    headers = sign_headers("orderExecute", body=body)
    r = SESSION.post(f"{API_HOST}/api/v1/order", headers=headers, data=json.dumps(body), timeout=HTTP_TIMEOUT)
    try:
        r.raise_for_status()
        out = r.json()
        print(f"[挂{ '买' if side=='Bid' else '卖' }单] price={price} qty={qty} id={out.get('id')}")
        return out
    except requests.HTTPError as e:
        print(f"[下{ '买' if side=='Bid' else '卖' }单失败] {e}")
        try:
            print(f"[POST ERROR] {r.status_code} {r.text}")
        except Exception:
            pass
        return {}

def post_order_taker(side: str, qty: Decimal, mode: str = "market",
                     price: Optional[Decimal] = None, tif: str = "IOC",
                     reduce_only: bool = True) -> dict:
    body = {
        "symbol": SYMBOL,
        "side": side,                              # "Bid"/"Ask"
        "orderType": "Market" if mode == "market" else "Limit",
        "timeInForce": tif,                        # IOC/FOK
        "postOnly": False,
        "quantity": format(qty, "f"),
        "selfTradePrevention": SELF_TRADE_MODE,
        "reduceOnly": True if reduce_only else False,
    }
    if price is not None:
        body["price"] = format(price, "f")

    if DRY_RUN:
        print(f"[DRY HEDGE] {mode} {side} qty={qty}" + (f" px={price}" if price is not None else ""))
        return {"id": "dry-run"}

    headers = sign_headers("orderExecute", body=body)
    r = SESSION.post(f"{API_HOST}/api/v1/order", headers=headers, data=json.dumps(body), timeout=HTTP_TIMEOUT)
    try:
        r.raise_for_status()
        out = r.json()
        print(f"[对冲下单] {mode} {side} qty={qty}" + (f" px={price}" if price is not None else "") + f" id={out.get('id')}")
        return out
    except requests.HTTPError as e:
        print(f"[对冲失败] {e}")
        try:
            print(f"[POST ERROR] {r.status_code} {r.text}")
        except Exception:
            pass
        return {}

def cancel_order(order_id: str):
    body = {"symbol": SYMBOL, "orderId": str(order_id)}
    headers = sign_headers("orderCancel", body=body)
    r = SESSION.delete(f"{API_HOST}/api/v1/order", headers=headers, data=json.dumps(body), timeout=HTTP_TIMEOUT)
    try:
        r.raise_for_status()
        print(f"[已撤单] {order_id}")
    except requests.HTTPError as e:
        print(f"[撤单失败] {e}")
        try:
            print(f"[DELETE ERROR] {r.status_code} {r.text}")
        except Exception:
            pass

def cancel_all():
    body = {"symbol": SYMBOL}
    headers = sign_headers("orderCancelAll", body=body)
    r = SESSION.delete(f"{API_HOST}/api/v1/orders", headers=headers, data=json.dumps(body), timeout=HTTP_TIMEOUT)
    if r.status_code in (200, 202):
        print("已发送全撤。")
    else:
        print(f"[全撤失败] {r.status_code} {r.text}")

# ---------------------- WS bookTicker + 私有流 ----------------------
class WSBookTicker:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.best_bid: Optional[Decimal] = None
        self.best_ask: Optional[Decimal] = None
        self.best_bid_qty: Optional[Decimal] = None
        self.best_ask_qty: Optional[Decimal] = None
        self.last_update_us: int = 0
        self.ws: Optional[WebSocketApp] = None
        self.lock = threading.Lock()
        self.bid_order_id: Optional[str] = None
        self.ask_order_id: Optional[str] = None
        self.last_quote_ms: int = 0
        # 仓位（来自私有流；未启用时保持0）
        self.pos_net: Decimal = Decimal("0")
        # 深度 / 成交统计
        self.depth_metrics: Optional[DepthMetrics] = None
        self.depth_levels: int = DEPTH_LEVELS
        self.recent_trades: Deque[TradeEvent] = deque()
        self.trade_stats: Optional[TradeStats] = None
        # 对冲状态
        self.last_hedge_ms: int = 0
        self.hedge_step: int = 0
        self.hedge_trigger_since: Optional[int] = None

    def start(self):
        import ssl
        stream_pub = f"bookTicker.{self.symbol}"
        stream_depth = f"{DEPTH_STREAM_PREFIX}.{self.symbol}"
        stream_trade = f"{TRADE_STREAM_PREFIX}.{self.symbol}"

        def on_open(ws):
            print("[WS] open")
            # 订阅公共流
            public_streams = [stream_pub]
            if self.depth_levels > 0:
                public_streams.append(stream_depth)
            if TRADE_WINDOW_SEC > 0:
                public_streams.append(stream_trade)
            ws.send(json.dumps({"method": "SUBSCRIBE", "params": public_streams, "id": 1}, ensure_ascii=True))
            # 订阅私有流（可选）
            if USE_PRIVATE_WS:
                sig = ws_signature_tuple()
                ws.send(json.dumps({
                    "method": "SUBSCRIBE",
                    "params": [f"account.positionUpdate.{self.symbol}", f"account.orderUpdate.{self.symbol}"],
                    "signature": sig
                }, ensure_ascii=True))

        def on_message(ws, message: str):
            try:
                msg = json.loads(message)
            except Exception:
                return

            stream = msg.get("stream")
            if isinstance(stream, str):
                data = msg.get("data", {})
                # 公共 bookTicker
                if stream.startswith("bookTicker."):
                    try:
                        a = Decimal(str(data["a"]))
                        b = Decimal(str(data["b"]))
                        A = Decimal(str(data.get("A", "0")))
                        B = Decimal(str(data.get("B", "0")))
                    except Exception:
                        return
                    ts_us = int(data.get("T") or data.get("E") or 0)
                    if a <= 0 or b <= 0 or a <= b:
                        return
                    with self.lock:
                        self.best_ask = a; self.best_ask_qty = A
                        self.best_bid = b; self.best_bid_qty = B
                        self.last_update_us = ts_us
                    self.maybe_quote()
                    return

                # order book depth
                if stream.startswith(DEPTH_STREAM_PREFIX):
                    self._handle_depth_message(data)
                    return

                # recent trades
                if stream.startswith(TRADE_STREAM_PREFIX):
                    self._handle_trade_message(data)
                    return

                # 私有：仓位更新
                if stream.startswith("account.positionUpdate"):
                    q = data.get("q")
                    if q is not None:
                        try:
                            with self.lock:
                                self.pos_net = Decimal(str(q))
                        except Exception:
                            pass
                    return

                # 私有：订单更新（此处可扩展为统计拒单/已成）
                if stream.startswith("account.orderUpdate"):
                    return

            # 兼容扁平（极少见）
            payload = msg
            if payload.get("e") == "bookTicker" and payload.get("s") == self.symbol:
                try:
                    a = Decimal(str(payload["a"])); b = Decimal(str(payload["b"]))
                except Exception:
                    return
                ts_us = int(payload.get("T") or payload.get("E") or 0)
                if a <= 0 or b <= 0 or a <= b:
                    return
                with self.lock:
                    self.best_ask = a; self.best_bid = b; self.last_update_us = ts_us
                self.maybe_quote()

        def on_error(ws, err):
            print(f"[WS ERROR] {repr(err)}")
            if isinstance(err, Exception) and "latin-1" in str(err):
                print("[HINT] unset http_proxy/https_proxy 等代理变量试试")

        def on_close(ws, code, reason):
            print(f"[WS] closed code={code} reason={reason}")

        self.ws = WebSocketApp(
            WS_URL,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
            header=[],
            subprotocols=None,
        )
        t = threading.Thread(
            target=self.ws.run_forever,
            kwargs={
                "sslopt": {"cert_reqs": ssl.CERT_NONE},
                "ping_interval": 50,
                "ping_timeout": 20,
                "http_proxy_host": None,
                "http_proxy_port": None,
                "http_no_proxy": "*",
            },
            daemon=True,
        )
        t.start()

    def stale(self) -> bool:
        if self.last_update_us == 0:
            return True
        return (time.time() * 1000 - self.last_update_us / 1000.0) > WS_STALE_MS

    # ---- 公共流处理 ----
    def _handle_depth_message(self, data):
        bids_raw = data.get("bids") or data.get("b") or []
        asks_raw = data.get("asks") or data.get("a") or []
        if not isinstance(bids_raw, list) or not isinstance(asks_raw, list):
            return

        def _sum_levels(levels):
            total = Decimal("0")
            best_price = None
            count = 0
            for entry in levels:
                if count >= self.depth_levels:
                    break
                price = None
                qty = None
                if isinstance(entry, dict):
                    price = entry.get("price") or entry.get("p")
                    qty = entry.get("quantity") or entry.get("q") or entry.get("size")
                elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    price, qty = entry[0], entry[1]
                if price is None or qty is None:
                    continue
                try:
                    price_dec = Decimal(str(price))
                    qty_dec = Decimal(str(qty))
                except Exception:
                    continue
                if qty_dec <= 0:
                    continue
                if best_price is None:
                    best_price = price_dec
                total += qty_dec
                count += 1
            return total, best_price

        total_bid, best_bid = _sum_levels(bids_raw)
        total_ask, best_ask = _sum_levels(asks_raw)
        if total_bid is None or total_ask is None:
            return
        ts_ms = int(data.get("t") or data.get("T") or now_ms())
        with self.lock:
            self.depth_metrics = DepthMetrics(
                ts_ms=ts_ms,
                total_bid=total_bid,
                total_ask=total_ask,
                best_bid=best_bid if best_bid is not None else (self.best_bid or Decimal("0")),
                best_ask=best_ask if best_ask is not None else (self.best_ask or Decimal("0")),
            )
        # 深度变化可能影响自适应偏移
        self.maybe_quote()

    def _handle_trade_message(self, data):
        now = now_ms()
        events = data if isinstance(data, list) else [data]
        updated = False

        for evt in events:
            qty_raw = evt.get("quantity") or evt.get("q") or evt.get("size") or evt.get("Q")
            if qty_raw is None:
                continue
            try:
                qty_dec = Decimal(str(qty_raw))
            except Exception:
                continue
            if qty_dec <= 0:
                continue
            side_raw = evt.get("side") or evt.get("S")
            if side_raw is None and "m" in evt:
                # maker flag: True 表示卖单打到买单（taker 卖）
                side_raw = "sell" if evt.get("m") else "buy"
            if side_raw is None:
                continue
            side = str(side_raw).lower()
            if side in ("buy", "bid", "true", "1"):
                side = "buy"
            elif side in ("sell", "ask", "false", "0"):
                side = "sell"
            else:
                continue
            ts_ms = int(evt.get("t") or evt.get("T") or now)
            trade_event = TradeEvent(ts_ms=ts_ms, qty=qty_dec, side=side)
            with self.lock:
                self.recent_trades.append(trade_event)
                self._prune_trades_locked(now)
                self._recalc_trade_stats_locked()
            updated = True

        if updated:
            self.maybe_quote()

    def _prune_trades_locked(self, now_ms_value: int):
        cutoff = now_ms_value - TRADE_WINDOW_SEC * 1000
        while self.recent_trades and self.recent_trades[0].ts_ms < cutoff:
            self.recent_trades.popleft()

    def _recalc_trade_stats_locked(self):
        buy_volume = Decimal("0")
        sell_volume = Decimal("0")
        latest_ts = 0
        for evt in self.recent_trades:
            if evt.side == "buy":
                buy_volume += evt.qty
            else:
                sell_volume += evt.qty
            if evt.ts_ms > latest_ts:
                latest_ts = evt.ts_ms
        if latest_ts == 0:
            latest_ts = now_ms()
        self.trade_stats = TradeStats(ts_ms=latest_ts, buy_volume=buy_volume, sell_volume=sell_volume)

    # ---- 库存偏移 & 硬阈暂停 ----
    def compute_skew_and_pause(self):
        """
        返回 (extra_bid_ticks, extra_ask_ticks, pause_bid, pause_ask)
        净多(>0): bid 更保守(+ticks), ask 更激进(-ticks)
        净空(<0): 反之
        """
        pos = self.pos_net
        pause_bid = pause_ask = False
        extra_bid = 0
        extra_ask = 0

        if HARD_POS > 0 and abs(pos) >= HARD_POS:
            if pos > 0:
                pause_bid = True
            elif pos < 0:
                pause_ask = True

        if SOFT_POS > 0 and SKEW_TICKS_PER_UNIT > 0 and abs(pos) > SOFT_POS:
            over = abs(pos) - SOFT_POS
            units = (over / SOFT_POS)
            levels = int(units.to_integral_value(rounding=ROUND_UP))
            extra = levels * SKEW_TICKS_PER_UNIT
            if pos > 0:
                extra_bid =  extra
                extra_ask = -extra
            else:
                extra_bid = -extra
                extra_ask =  extra
        return extra_bid, extra_ask, pause_bid, pause_ask

    # ---- 主动 reduceOnly 对冲 ----
    def maybe_hedge(self, depth_snapshot: Optional[DepthMetrics] = None,
                    trade_snapshot: Optional[TradeStats] = None):
        if HEDGE_TRIGGER <= 0:
            return
        now = int(time.time() * 1000)
        with self.lock:
            pos = self.pos_net
            b = self.best_bid
            a = self.best_ask
            if depth_snapshot is None:
                depth_snapshot = self.depth_metrics
            if trade_snapshot is None:
                trade_snapshot = self.trade_stats

        if b is None or a is None:
            return

        depth_fresh = None
        if depth_snapshot is not None and now - depth_snapshot.ts_ms <= DEPTH_STALE_MS:
            depth_fresh = depth_snapshot
        trade_fresh = None
        if trade_snapshot is not None and now - trade_snapshot.ts_ms <= TRADE_STALE_MS:
            trade_fresh = trade_snapshot

        # 触发：仓位达到阈值
        if abs(pos) >= HEDGE_TRIGGER:
            if self.hedge_trigger_since is None:
                self.hedge_trigger_since = now
            # 宽限期（给被动成交一点时间）
            if now - self.hedge_trigger_since < HEDGE_GRACE_MS:
                return
        else:
            self.hedge_trigger_since = None
            self.hedge_step = 0
            return

        # 冷却
        if now - self.last_hedge_ms < HEDGE_COOLDOWN_MS:
            return

        qty = min(abs(pos), HEDGE_CHUNK if HEDGE_CHUNK > 0 else abs(pos))
        qty = q_qty(qty)
        if qty <= 0:
            return

        side = "Ask" if pos > 0 else "Bid"  # 净多→卖，净空→买

        hedge_start = HEDGE_START_BPS
        hedge_step = HEDGE_STEP_BPS
        if ADAPTIVE_HEDGE:
            hedge_start, hedge_step = adaptive_hedge_params(hedge_start, hedge_step, pos, depth_fresh, trade_fresh)
        if hedge_start <= 0:
            hedge_start = Decimal("1")
        if hedge_step <= 0:
            hedge_step = Decimal("1")

        if HEDGE_MODE == "market":
            post_order_taker(side=side, qty=qty, mode="market", tif="IOC", reduce_only=True)
        else:
            cross_bps = hedge_start + Decimal(self.hedge_step) * hedge_step
            if side == "Ask":
                px = b * (Decimal(1) - cross_bps/Decimal(10000))
                px = q_price_aggr(px, "Ask")  # 卖向下取整
            else:
                px = a * (Decimal(1) + cross_bps/Decimal(10000))
                px = q_price_aggr(px, "Bid")  # 买向上取整
            post_order_taker(side=side, qty=qty, mode="limit", price=px, tif="IOC", reduce_only=True)

        self.last_hedge_ms = now
        if self.hedge_step < HEDGE_MAX_STEPS:
            self.hedge_step += 1

    def maybe_quote(self):
        now = int(time.time() * 1000)
        if now - self.last_quote_ms < REFRESH_MS:
            return
        with self.lock:
            b = self.best_bid
            a = self.best_ask
            pos = self.pos_net
            depth_snapshot = self.depth_metrics
            trade_snapshot = self.trade_stats
        if b is None or a is None or self.stale():
            return

        depth_fresh = None
        if depth_snapshot is not None and now - depth_snapshot.ts_ms <= DEPTH_STALE_MS:
            depth_fresh = depth_snapshot
        trade_fresh = None
        if trade_snapshot is not None and now - trade_snapshot.ts_ms <= TRADE_STALE_MS:
            trade_fresh = trade_snapshot

        # 点差检查
        spread_ticks = int(((a - b) / FILTERS.tick).to_integral_value(rounding=ROUND_DOWN))
        if spread_ticks < MIN_SPREAD_TICKS:
            return

        # 基础目标价（贴 inside + 固定偏移）
        tick = FILTERS.tick
        bid_px = q_price(b, "Bid")
        ask_px = q_price(a, "Ask")
        bid_offset_ticks = BID_OFFSET_TICKS
        ask_offset_ticks = ASK_OFFSET_TICKS

        if ADAPTIVE_SPREAD:
            bid_offset_ticks, ask_offset_ticks = adaptive_offsets(bid_offset_ticks, ask_offset_ticks, depth_fresh, trade_fresh)

        if bid_offset_ticks > 0:
            bid_px = q_price(bid_px - tick * Decimal(bid_offset_ticks), "Bid")
        if ask_offset_ticks > 0:
            ask_px = q_price(ask_px + tick * Decimal(ask_offset_ticks), "Ask")

        # 库存偏移 & 硬阈暂停
        eb, ea, pause_bid, pause_ask = self.compute_skew_and_pause()
        if eb != 0:
            bid_px = q_price(bid_px - tick * Decimal(eb), "Bid")
        if ea != 0:
            ask_px = q_price(ask_px + tick * Decimal(ea), "Ask")

        # ---- Maker-safe clamp：确保 postOnly 不撞价 ----
        with self.lock:
            b_now = self.best_bid
            a_now = self.best_ask
        min_ask_px = q_price(max(a_now, b_now + tick), "Ask")   # 卖 ≥ max(best_ask, best_bid+tick)
        if ask_px < min_ask_px:
            ask_px = min_ask_px
        max_bid_px = q_price(min(b_now, a_now - tick), "Bid")   # 买 ≤ min(best_bid, best_ask-tick)
        if bid_px > max_bid_px:
            bid_px = max_bid_px

        if bid_px >= ask_px:
            return

        qty = q_qty(QTY)
        if qty <= 0:
            print("[WARN] 数量为0，检查 QTY / stepSize / minQty")
            return

        # 先撤旧
        try:
            if self.bid_order_id:
                cancel_order(self.bid_order_id); self.bid_order_id = None
            if self.ask_order_id:
                cancel_order(self.ask_order_id); self.ask_order_id = None
        except Exception:
            pass

        # 按暂停标志挂单
        if not pause_bid:
            res_bid = post_order("Bid", bid_px, qty)
            self.bid_order_id = res_bid.get("id")
        else:
            self.bid_order_id = None

        if not pause_ask:
            res_ask = post_order("Ask", ask_px, qty)
            self.ask_order_id = res_ask.get("id")
        else:
            self.ask_order_id = None

        self.last_quote_ms = now
        depth_tag = "-"
        if depth_fresh is not None:
            depth_tag = f"{format(depth_fresh.total_bid, 'f')}/{format(depth_fresh.total_ask, 'f')}"
        trade_tag = "-"
        if trade_fresh is not None:
            trade_tag = f"imb={trade_fresh.imbalance:.2f} vol={format(trade_fresh.total_volume, 'f')}"
        print(
            f"[INV] pos={pos} eb={eb} ea={ea} pause(bid={pause_bid},ask={pause_ask}) "
            f"depth={depth_tag} trades={trade_tag} -> bid={bid_px}, ask={ask_px}"
        )

        # 主动 reduceOnly 对冲（放在最后执行）
        self.maybe_hedge(depth_snapshot=depth_fresh, trade_snapshot=trade_fresh)

# ---------------------- 主流程 ----------------------
BOT = WSBookTicker(SYMBOL)

def _graceful_exit(sig, frame):
    print("退出中，撤销挂单...")
    try:
        cancel_all()
    finally:
        os._exit(0)

def main():
    print(f"Backpack scalper (WS + inv + hedge) 启动：symbol={SYMBOL}, qty={QTY}, postOnly={POST_ONLY}, dryRun={DRY_RUN}, usePrivateWS={USE_PRIVATE_WS}")
    signal.signal(signal.SIGINT, _graceful_exit)
    signal.signal(signal.SIGTERM, _graceful_exit)
    BOT.start()
    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()

