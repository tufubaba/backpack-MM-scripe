#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# --- robust dotenv loader ---
import os
from pathlib import Path
from dotenv import load_dotenv

def load_env():
    here = Path(__file__).resolve().parent
    candidates = []

    # 1) ENV_FILE 优先
    env_file = os.getenv("ENV_FILE")
    if env_file:
        candidates.append(Path(env_file))

    # 2) 常见放置位置
    candidates += [
        here / ".env",
        here / "backpack.env",  # 新的配置文件名
        here / "backpack-BTC_USDC_PERP.env",  # 向后兼容
        here / "env" / "backpack-BTC_USDC_PERP.env",
    ]

    # 逐个尝试
    for p in candidates:
        if p.is_file():
            load_dotenv(p.as_posix(), override=True)
            return p.as_posix()

    # 3) 兜底：按默认规则（从当前目录向上）寻找 .env
    load_dotenv(override=True)
    return None

ENV_USED = load_env()

# 可选：快速验证（不要打印具体密钥）
sk = os.getenv("SECRET_KEY", "")
if not sk:
    raise RuntimeError("缺少 SECRET_KEY（支持 hex/base64）")

import os, json, time, signal, threading, base64
from decimal import Decimal, ROUND_DOWN, ROUND_UP, InvalidOperation, getcontext
from dataclasses import dataclass
from typing import Optional

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
        # 对冲状态
        self.last_hedge_ms: int = 0
        self.hedge_step: int = 0
        self.hedge_trigger_since: Optional[int] = None

    def start(self):
        import ssl
        stream_pub = f"bookTicker.{self.symbol}"

        def on_open(ws):
            print("[WS] open")
            # 订阅公共流
            ws.send(json.dumps({"method": "SUBSCRIBE", "params": [stream_pub], "id": 1}, ensure_ascii=True))
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
    def maybe_hedge(self):
        if HEDGE_TRIGGER <= 0:
            return
        now = int(time.time() * 1000)
        with self.lock:
            pos = self.pos_net
            b = self.best_bid
            a = self.best_ask

        if b is None or a is None:
            return

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

        if HEDGE_MODE == "market":
            post_order_taker(side=side, qty=qty, mode="market", tif="IOC", reduce_only=True)
        else:
            cross_bps = HEDGE_START_BPS + Decimal(self.hedge_step) * HEDGE_STEP_BPS
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
        if b is None or a is None or self.stale():
            return

        # 点差检查
        spread_ticks = int(((a - b) / FILTERS.tick).to_integral_value(rounding=ROUND_DOWN))
        if spread_ticks < MIN_SPREAD_TICKS:
            return

        # 基础目标价（贴 inside + 固定偏移）
        tick = FILTERS.tick
        bid_px = q_price(b, "Bid")
        ask_px = q_price(a, "Ask")
        if BID_OFFSET_TICKS > 0:
            bid_px = q_price(bid_px - tick * BID_OFFSET_TICKS, "Bid")
        if ASK_OFFSET_TICKS > 0:
            ask_px = q_price(ask_px + tick * ASK_OFFSET_TICKS, "Ask")

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
        min_ask_px = q_price(max(a_now, b_now + tick), "Ask")  # 卖 ≥ max(best_ask, best_bid+tick)
        if ask_px < min_ask_px:
            ask_px = min_ask_px
        max_bid_px = q_price(min(b_now, a_now - tick), "Bid")  # 买 ≤ min(best_bid, best_ask-tick)
        if bid_px > max_bid_px:
            bid_px = max_bid_px

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
        print(f"[INV] pos={pos} eb={eb} ea={ea} pause(bid={pause_bid},ask={pause_ask}) -> bid={bid_px}, ask={ask_px}")

        # 主动 reduceOnly 对冲（放在最后执行）
        self.maybe_hedge()

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
