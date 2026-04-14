import os
import time
import json
import requests
import pandas as pd
import ccxt
from dotenv import load_dotenv

# ==========================================
# 환경 변수 로드
# ==========================================
load_dotenv()

BITHUMB_API_KEY = os.getenv("BITHUMB_API_KEY")
BITHUMB_API_SECRET = os.getenv("BITHUMB_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DRY_RUN = os.getenv("DRY_RUN", "True").upper() == "TRUE"

INTERVAL = "4h"
DONCHIAN_WINDOW = 20
ATR_WINDOW = 14

ATR_MULT_TRAIL = 3.0
ATR_MULT_INITIAL_STOP = ATR_MULT_TRAIL

RISK_PER_TRADE = 0.01  # 계좌 1% 리스크

TP1_R = 1.0
TP2_R = 2.0
TP1_PCT = 0.30
TP2_PCT = 0.30

MIN_ORDER_KRW = 5000

# ✅ 6개 코인
TICKERS = ["BTC/KRW", "ETH/KRW", "XRP/KRW", "SOL/KRW", "ADA/KRW", "AVAX/KRW"]

LOOP_SLEEP_SEC = 60
STATUS_INTERVAL_SEC = 3600  # 1시간 리포트

# ==========================================
# Telegram 송신
# ==========================================
def send_telegram(msg: str):
    print("[TG]", msg)
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception:
        pass


# ==========================================
# 거래 로그 (CSV)
# ==========================================
TRADE_LOG_PATH = "/root/tfbot/trades.csv"

def log_trade(symbol: str, action: str, price: float, qty: float, note: str = ""):
    ts = pd.Timestamp.now(tz="Asia/Seoul").strftime("%Y-%m-%d %H:%M:%S")
    krw = float(price) * float(qty) if price is not None and qty is not None else 0.0

    row = {
        "time": ts,
        "symbol": symbol,
        "action": action,   # ENTRY_DONCHIAN / ENTRY_SWING_OK / TP1 / TP2 / STOP / EXIT_ALL
        "price": float(price) if price is not None else None,
        "qty": float(qty) if qty is not None else None,
        "krw": krw,
        "note": note,
    }

    # 헤더 포함 생성 / 없으면 append
    df = pd.DataFrame([row])
    if not os.path.exists(TRADE_LOG_PATH):
        df.to_csv(TRADE_LOG_PATH, index=False)
    else:
        df.to_csv(TRADE_LOG_PATH, mode="a", header=False, index=False)


# ==========================================
# Telegram 수신 (OK/PASS 반자동)
# ==========================================
TG_OFFSET_PATH = "/root/tfbot/tg_offset.json"

def load_tg_offset():
    try:
        with open(TG_OFFSET_PATH, "r") as f:
            return json.load(f).get("offset", None)
    except Exception:
        return None

def save_tg_offset(offset: int):
    try:
        with open(TG_OFFSET_PATH, "w") as f:
            json.dump({"offset": offset}, f)
    except Exception:
        pass

def poll_telegram_commands() -> list[str]:
    """
    텔레그램 최신 메시지 읽기. 예: "OK BTC/KRW", "PASS BTC/KRW"
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return []

    offset = load_tg_offset()
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"timeout": 5}
    if offset is not None:
        params["offset"] = offset

    try:
        r = requests.get(url, params=params, timeout=15).json()
        if not r.get("ok"):
            return []

        updates = r.get("result", [])
        cmds = []
        for u in updates:
            update_id = u.get("update_id")
            if update_id is not None:
                save_tg_offset(int(update_id) + 1)

            msg = u.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = (msg.get("text") or "").strip()

            # 본인 chat id만 허용
            if chat_id != str(TELEGRAM_CHAT_ID):
                continue

            if text:
                cmds.append(text)
        return cmds
    except Exception:
        return []


# ==========================================
# 스윙 지표/필터
# ==========================================
SWING_RSI_WINDOW = 14
SWING_RSI_LOWER = 45
SWING_RSI_TRIGGER = 45

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def trend_filter_1d(exchange, symbol: str) -> bool:
    """
    1D 추세필터: close > MA60 AND MA20 > MA60
    """
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=120)
        if not ohlcv or len(ohlcv) < 80:
            return False
        df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
        df["ma20"] = df["close"].rolling(20).mean()
        df["ma60"] = df["close"].rolling(60).mean()
        last = df.iloc[-1]
        if pd.isna(last["ma20"]) or pd.isna(last["ma60"]):
            return False
        return (last["close"] > last["ma60"])
    except Exception:
        return False


# ==========================================
# 상태 저장 / 로드
# ==========================================
def state_path(symbol: str) -> str:
    safe = symbol.replace("/", "-")
    return f"/root/tfbot/state_{safe}.json"

DEFAULT_STATE = {
    "in_position": False,
    "entry_price": None,
    "stop_price": None,
    "size": None,
    "last_bar_time": None,

    "initial_stop": None,
    "tp1_done": False,
    "tp2_done": False,
    "highest_price": None,

    "pending_entry": None,   # {"type":"SWING","price":...,"atr":...,"bar_time":...}
    "pending_expire": None,  # unix ts
}

def load_state(symbol: str) -> dict:
    path = state_path(symbol)
    if not os.path.exists(path):
        return dict(DEFAULT_STATE)

    try:
        with open(path, "r") as f:
            st = json.load(f)

        # 구버전 호환
        for k, v in DEFAULT_STATE.items():
            st.setdefault(k, v)
        return st
    except Exception:
        return dict(DEFAULT_STATE)

def save_state(symbol: str, state: dict):
    try:
        with open(state_path(symbol), "w") as f:
            json.dump(state, f)
    except Exception:
        pass


# ==========================================
# 봇 공용 상태
# ==========================================
BOT_STATE_PATH = "/root/tfbot/bot_state.json"

def load_bot_state() -> dict:
    if not os.path.exists(BOT_STATE_PATH):
        return {"last_status_ts": None}
    try:
        with open(BOT_STATE_PATH, "r") as f:
            st = json.load(f)
        st.setdefault("last_status_ts", None)
        return st
    except Exception:
        return {"last_status_ts": None}

def save_bot_state(st: dict):
    try:
        with open(BOT_STATE_PATH, "w") as f:
            json.dump(st, f)
    except Exception:
        pass

def now_ts() -> int:
    return int(time.time())


# ==========================================
# 빗썸 객체 (ccxt)
# ==========================================
exchange = ccxt.bithumb({
    "apiKey": BITHUMB_API_KEY,
    "secret": BITHUMB_API_SECRET,
    "enableRateLimit": True,
})


# ==========================================
# 데이터 / 지표
# ==========================================
def get_ohlcv(symbol: str):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=INTERVAL, limit=180)
    except Exception as e:
        send_telegram(f"[{symbol}] OHLCV 조회 오류: {e}")
        return None

    if not ohlcv or len(ohlcv) < DONCHIAN_WINDOW + ATR_WINDOW + 5:
        return None

    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df

def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["prev_close"] = df["close"].shift(1)

    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - df["prev_close"]).abs()
    tr3 = (df["low"] - df["prev_close"]).abs()
    df["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    df["atr"] = df["tr"].rolling(ATR_WINDOW).mean()
    df["donchian_high"] = df["high"].rolling(DONCHIAN_WINDOW).max().shift(1)
    df["donchian_low"] = df["low"].rolling(DONCHIAN_WINDOW).min().shift(1)
    return df


# ==========================================
# 잔고
# ==========================================
def get_balances():
    try:
        return exchange.fetch_balance()
    except Exception as e:
        send_telegram(f"❗ 빗썸 잔고 조회 오류: {e}")
        return None

def get_krw(balance) -> float:
    try:
        return float(balance["free"].get("KRW", 0.0))
    except Exception:
        return 0.0

def get_coin(balance, symbol: str) -> float:
    base = symbol.split("/")[0]
    try:
        return float(balance["free"].get(base, 0.0))
    except Exception:
        return 0.0


# ==========================================
# 주문 함수 (시장가)
# ==========================================
def buy(symbol: str, amount_base: float, price_ref: float, note: str):
    if amount_base <= 0:
        return

    if DRY_RUN:
        send_telegram(f"[{symbol}] [DRY RUN] 시장가 매수 (수량={amount_base:.8f})")
        log_trade(symbol, "ENTRY", price=float(price_ref), qty=amount_base, note=note)
        return

    try:
        order = exchange.create_market_buy_order(symbol, amount_base)
        send_telegram(f"[{symbol}] ✅ 시장가 매수 완료 (수량={amount_base:.8f})\norder_id={order.get('id')}")
        log_trade(symbol, "ENTRY", price=float(price_ref), qty=amount_base, note=note)
    except Exception as e:
        send_telegram(f"[{symbol}] ❌ 매수 주문 오류: {e}")

def sell_partial(symbol: str, amount_base: float, price_ref: float, tag: str):
    if amount_base <= 0:
        return

    est_krw = amount_base * price_ref
    if est_krw < MIN_ORDER_KRW:
        send_telegram(f"[{symbol}] {tag} 매도 스킵 (추정금액 {est_krw:,.0f} KRW < {MIN_ORDER_KRW})")
        return

    if DRY_RUN:
        send_telegram(f"[{symbol}] [DRY RUN] {tag} 시장가 매도 (수량={amount_base:.8f})")
        return

    try:
        order = exchange.create_market_sell_order(symbol, amount_base)
        send_telegram(f"[{symbol}] 🔻 {tag} 시장가 매도 (수량={amount_base:.8f})\norder_id={order.get('id')}")
    except Exception as e:
        send_telegram(f"[{symbol}] ❌ {tag} 매도 주문 오류: {e}")

def sell_all(symbol: str, balance):
    vol = get_coin(balance, symbol)
    if vol <= 0:
        send_telegram(f"[{symbol}] 보유량 없음 → 매도 스킵")
        return

    if DRY_RUN:
        send_telegram(f"[{symbol}] [DRY RUN] 전량 시장가 매도 (수량={vol:.8f})")
        return

    try:
        order = exchange.create_market_sell_order(symbol, vol)
        send_telegram(f"[{symbol}] 🔻 시장가 전량 매도 (수량={vol:.8f})\norder_id={order.get('id')}")
    except Exception as e:
        send_telegram(f"[{symbol}] ❌ 매도 주문 오류: {e}")


# ==========================================
# 텔레그램 OK/PASS 처리
# ==========================================
def handle_telegram_ok_pass():
    cmds = poll_telegram_commands()
    if not cmds:
        return

    for c in cmds:
        parts = c.split()
        if len(parts) < 2:
            continue

        action = parts[0].upper()
        sym = parts[1].upper().replace("-", "/")

        if sym not in TICKERS:
            continue

        st = load_state(sym)

        # 만료 처리
        exp = st.get("pending_expire")
        if exp is not None and now_ts() > int(exp):
            st["pending_entry"] = None
            st["pending_expire"] = None
            save_state(sym, st)

        if st.get("pending_entry") is None:
            continue

        if action == "PASS":
            st["pending_entry"] = None
            st["pending_expire"] = None
            save_state(sym, st)
            send_telegram(f"✅ [{sym}] SWING pending 취소(PASS) 완료")
            continue

        if action == "OK":
            pending = st["pending_entry"]
            price_ = float(pending["price"])
            atr_ = float(pending["atr"])

            balance2 = get_balances()
            if balance2 is None:
                send_telegram(f"[{sym}] OK 처리 중 잔고 조회 실패")
                continue

            krw2 = get_krw(balance2)
            risk_krw = krw2 * RISK_PER_TRADE
            per_unit_risk = atr_ * ATR_MULT_INITIAL_STOP
            if per_unit_risk <= 0:
                send_telegram(f"[{sym}] OK 받았으나 ATR/per_unit_risk 오류")
                continue

            amount_base = risk_krw / per_unit_risk
            cost_krw = amount_base * price_
            if cost_krw < MIN_ORDER_KRW:
                send_telegram(f"[{sym}] OK 받았으나 주문금액 너무 작아 스킵({cost_krw:,.0f} KRW)")
                st["pending_entry"] = None
                st["pending_expire"] = None
                save_state(sym, st)
                continue

            send_telegram(f"🟢 [{sym}] OK 승인 → 시장가 진입\n수량≈{amount_base:.8f}, 금액≈{cost_krw:,.0f} KRW")
            buy(sym, amount_base, price_ref=price_, note="ENTRY_SWING_OK")

            initial_stop = price_ - atr_ * ATR_MULT_INITIAL_STOP
            st["in_position"] = True
            st["entry_price"] = price_
            st["initial_stop"] = initial_stop
            st["stop_price"] = initial_stop
            st["size"] = amount_base
            st["tp1_done"] = False
            st["tp2_done"] = False
            st["highest_price"] = price_

            st["pending_entry"] = None
            st["pending_expire"] = None
            save_state(sym, st)


# ==========================================
# 종목 하나 처리
# ==========================================
def process_symbol(symbol: str, balance) -> dict | None:
    df = get_ohlcv(symbol)
    if df is None:
        return None

    df = calc_indicators(df)
    df["rsi"] = calc_rsi(df["close"], SWING_RSI_WINDOW)

    last = df.iloc[-1]
    bar_time = df.index[-1]
    bar_time_str = bar_time.strftime("%Y-%m-%d %H:%M:%S")

    price = float(last["close"])
    atr = float(last["atr"]) if not pd.isna(last["atr"]) else 0.0
    donchian_high = float(last["donchian_high"]) if not pd.isna(last["donchian_high"]) else 0.0
    rsi_now = float(last["rsi"]) if not pd.isna(last["rsi"]) else None

    state = load_state(symbol)

    in_pos = bool(state.get("in_position", False))
    stop_price = state.get("stop_price", None)
    entry_price = state.get("entry_price", None)

    last_bar_old = state.get("last_bar_time", None)
    new_bar = (last_bar_old != bar_time_str)

    # (A) 포지션 관리: 매 루프
    if in_pos and entry_price is not None and atr > 0:
        hp = state.get("highest_price")
        if hp is None:
            hp = float(entry_price)
        hp = max(float(hp), float(price))
        state["highest_price"] = hp

        trail_stop = hp - atr * ATR_MULT_TRAIL
        if stop_price is None:
            stop_price = trail_stop
        else:
            stop_price = max(float(stop_price), float(trail_stop))
        state["stop_price"] = stop_price

        initial_stop = state.get("initial_stop")
        if initial_stop is None:
            initial_stop = float(entry_price) - atr * ATR_MULT_INITIAL_STOP
            state["initial_stop"] = initial_stop

        R = float(entry_price) - float(initial_stop)
        if R > 0:
            tp1 = float(entry_price) + TP1_R * R
            tp2 = float(entry_price) + TP2_R * R
            size = float(state.get("size") or 0.0)

            if (not state.get("tp1_done", False)) and price >= tp1 and size > 0:
                qty = size * TP1_PCT
                sell_partial(symbol, qty, price_ref=price, tag=f"[TP1] {int(TP1_PCT*100)}% 익절")
                state["size"] = max(0.0, size - qty)
                state["tp1_done"] = True

            size = float(state.get("size") or 0.0)
            if (not state.get("tp2_done", False)) and price >= tp2 and size > 0:
                qty = size * TP2_PCT
                sell_partial(symbol, qty, price_ref=price, tag=f"[TP2] {int(TP2_PCT*100)}% 익절")
                log_trade(symbol, "TP2", price=price, qty=qty, note="PARTIAL_TP2")
                state["size"] = max(0.0, size - qty)
                state["tp2_done"] = True

        if stop_price is not None and price < float(stop_price):
            send_telegram(f"🔻 [{symbol}] 스탑아웃!\nprice={price:,.0f} < stop={float(stop_price):,.0f}")
            qty_all = get_coin(balance, symbol)  # 현재 잔고 기준
            log_trade(symbol, "STOP", price=price, qty=qty_all, note="STOP_OUT")
            sell_all(symbol, balance)

            # 리셋
            state.update(dict(DEFAULT_STATE))
            in_pos = False

        save_state(symbol, state)

    # (B) 신규 진입 로직: 새 4H 봉에서만
    if new_bar:
        send_telegram(f"[DEBUG SWING] {symbol} new_bar=True price={price:,.0f} rsi_now={rsi_now}")
        krw = get_krw(balance)

        # (B-0) pending 만료 체크
        exp = state.get("pending_expire")
        if exp is not None and now_ts() > int(exp):
            state["pending_entry"] = None
            state["pending_expire"] = None

        # (B-1) 스윙 후보 생성(반자동) - pending 없을 때만
        if (not state.get("in_position", False)) and (state.get("pending_entry") is None):
            try:
                prev = df.iloc[-2]
                rsi_prev = float(prev["rsi"]) if not pd.isna(prev["rsi"]) else None

                swing_ok = False
                if (rsi_prev is not None) and (rsi_now is not None):
                    if trend_filter_1d(exchange, symbol):
                        if (rsi_prev < SWING_RSI_LOWER) and (rsi_now > SWING_RSI_TRIGGER):
                            swing_ok = True

                if swing_ok and atr > 0 and krw > MIN_ORDER_KRW:
                    state["pending_entry"] = {
                        "type": "SWING",
                        "price": float(price),
                        "atr": float(atr),
                        "bar_time": bar_time_str,
                    }
                    state["pending_expire"] = now_ts() + 6 * 3600  # 6시간 후 만료

                    send_telegram(
                        f"🟡 [{symbol}] SWING 진입 후보(반자동)\n"
                        f"price={price:,.0f}, RSI(prev={rsi_prev:.1f}→now={rsi_now:.1f}), ATR={atr:,.0f}\n"
                        f"승인: OK {symbol}\n"
                        f"거절: PASS {symbol}\n"
                        f"(6시간 후 자동 만료)"
                    )
            except Exception as e:
                send_telegram(f"[{symbol}] 스윙 시그널 계산 오류: {e}")

        # (B-2) 돈치안 자동 진입(기존)
        if (not state.get("in_position", False)) and price > donchian_high and atr > 0 and krw > MIN_ORDER_KRW:
            risk_krw = krw * RISK_PER_TRADE
            per_unit_risk = atr * ATR_MULT_INITIAL_STOP
            if per_unit_risk > 0:
                amount_base = risk_krw / per_unit_risk
                cost_krw = amount_base * price

                if cost_krw >= MIN_ORDER_KRW:
                    send_telegram(
                        f"📈 [{symbol}] 진입 신호 (4H Donchian 상단 돌파)\n"
                        f"price={price:,.0f}, DC_high={donchian_high:,.0f}, ATR={atr:,.0f}\n"
                        f"수량≈{amount_base:.8f}, 예상금액≈{cost_krw:,.0f} KRW"
                    )
                    buy(symbol, amount_base, price_ref=price, note="ENTRY_DONCHIAN")

                    initial_stop = price - atr * ATR_MULT_INITIAL_STOP
                    state["in_position"] = True
                    state["entry_price"] = price
                    state["initial_stop"] = initial_stop
                    state["stop_price"] = initial_stop
                    state["size"] = amount_base
                    state["tp1_done"] = False
                    state["tp2_done"] = False
                    state["highest_price"] = price
                else:
                    send_telegram(f"[{symbol}] 돈치안 신호지만 주문금액 작아 스킵({cost_krw:,.0f} KRW)")

        # 새 봉 처리 완료
        state["last_bar_time"] = bar_time_str
        save_state(symbol, state)

        in_pos = bool(state.get("in_position", False))
        entry_price = state.get("entry_price", None)
        stop_price = state.get("stop_price", None)

    # 리포트용
    return {
        "symbol": symbol,
        "price": price,
        "atr": atr,
        "donchian_high": donchian_high,
        "rsi": rsi_now,
        "in_position": in_pos,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "last_bar_time": bar_time_str,
        "new_bar": new_bar,
        "tp1_done": state.get("tp1_done", False),
        "tp2_done": state.get("tp2_done", False),
        "highest_price": state.get("highest_price", None),
        "pending": state.get("pending_entry") is not None,
    }


# ==========================================
# 리포트 메시지
# ==========================================
def build_status_report(reports: list[dict]) -> str:
    lines = ["📊 [Bithumb] 4H Bot 상태 리포트"]
    for r in reports:
        pos = "보유" if r["in_position"] else "미보유"
        pend = " (PENDING)" if (r.get("pending") and (not r["in_position"])) else ""
        base = f"{r['symbol']}: {pos}{pend}, price={r['price']:,.0f}, ATR={r['atr']:,.0f}"
        if r.get("rsi") is not None:
            base += f", RSI={r['rsi']:.1f}"

        if r["in_position"]:
            extra = ""
            if r["entry_price"] is not None:
                extra += f", entry={float(r['entry_price']):,.0f}"
            if r["stop_price"] is not None:
                extra += f", stop={float(r['stop_price']):,.0f}"
            if r.get("highest_price") is not None:
                extra += f", high={float(r['highest_price']):,.0f}"
            extra += f", TP1={'Y' if r.get('tp1_done') else 'N'}"
            extra += f", TP2={'Y' if r.get('tp2_done') else 'N'}"
            lines.append(base + extra)
        else:
            lines.append(base)
    return "\n".join(lines)


# ==========================================
# 메인 루프
# ==========================================
def main_loop():
    send_telegram(f"🤖 [Bithumb] Multi-Asset 4H Donchian+ATR Bot 시작 (DRY_RUN={DRY_RUN})")
    bot_state = load_bot_state()

    while True:
        try:
            # 반자동 OK/PASS 먼저 처리
            handle_telegram_ok_pass()

            balance = get_balances()
            if balance is None:
                time.sleep(LOOP_SLEEP_SEC)
                continue

            reports = []
            any_new_bar = False

            for sym in TICKERS:
                info = process_symbol(sym, balance)
                if info is None:
                    continue
                reports.append(info)
                if info.get("new_bar"):
                    any_new_bar = True

            if any_new_bar and reports:
                send_telegram(build_status_report(reports))

            last_ts = bot_state.get("last_status_ts")
            now = now_ts()
            if (last_ts is None) or (now - int(last_ts) >= STATUS_INTERVAL_SEC):
                if reports:
                    send_telegram(build_status_report(reports))
                bot_state["last_status_ts"] = now
                save_bot_state(bot_state)

            time.sleep(LOOP_SLEEP_SEC)

        except Exception as e:
            send_telegram(f"❗ MAIN LOOP ERROR: {e}")
            time.sleep(600)


if __name__ == "__main__":
    main_loop()
