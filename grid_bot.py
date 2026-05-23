import os
import time
import json
import requests
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

# ==========================================
# 그리드 설정
# ==========================================
SYMBOLS = ["XRP/KRW", "ADA/KRW", "SOL/KRW"]
GRID_COUNT = 5
GRID_RANGE = 0.10
GRID_AMOUNT_KRW = 20000
FEE = 0.0025

LOOP_SLEEP_SEC = 10

# ==========================================
# 텔레그램
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
# 빗썸 객체
# ==========================================
exchange = ccxt.bithumb({
    "apiKey": BITHUMB_API_KEY,
    "secret": BITHUMB_API_SECRET,
    "enableRateLimit": True,
})

# ==========================================
# 그리드 생성
# ==========================================
def create_grids(current_price: float) -> list:
    lower = current_price * (1 - GRID_RANGE)
    upper = current_price * (1 + GRID_RANGE)
    step = (upper - lower) / GRID_COUNT
    return [round(lower + step * i, 2) for i in range(GRID_COUNT + 1)]

# ==========================================
# 상태 저장/로드
# ==========================================
def state_path(symbol: str) -> str:
    safe = symbol.replace("/", "-")
    return f"/root/tfbot/grid_state_{safe}.json"

def save_state(symbol: str, state: dict):
    with open(state_path(symbol), "w") as f:
        json.dump(state, f, indent=2)

def load_state(symbol: str) -> dict:
    path = state_path(symbol)
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)

# ==========================================
# 현재가 조회
# ==========================================
def get_price(symbol: str) -> float:
    ticker = exchange.fetch_ticker(symbol)
    price = ticker.get('last') or ticker.get('close')
    if price is None:
        raise ValueError(f"{symbol} 가격 조회 실패")
    return float(price)

# ==========================================
# 매수/매도
# ==========================================
def place_buy(symbol: str, price: float, grid_idx: int) -> bool:
    amount_coin = GRID_AMOUNT_KRW / price
    if DRY_RUN:
        send_telegram(f"[DRY RUN] [{symbol}] 그리드 {grid_idx} 매수\n가격={price:,.0f}원, 수량={amount_coin:.4f}")
        return True
    try:
        exchange.create_market_buy_order(symbol, amount_coin)
        send_telegram(f"✅ [{symbol}] 그리드 {grid_idx} 매수 완료\n가격={price:,.0f}원, 수량={amount_coin:.4f}")
        return True
    except Exception as e:
        send_telegram(f"❌ [{symbol}] 매수 오류: {e}")
        return False

def place_sell(symbol: str, price: float, grid_idx: int, amount_coin: float) -> bool:
    if DRY_RUN:
        send_telegram(f"[DRY RUN] [{symbol}] 그리드 {grid_idx} 매도\n가격={price:,.0f}원, 수량={amount_coin:.4f}")
        return True
    try:
        exchange.create_market_sell_order(symbol, amount_coin)
        send_telegram(f"🔻 [{symbol}] 그리드 {grid_idx} 매도 완료\n가격={price:,.0f}원, 수량={amount_coin:.4f}")
        return True
    except Exception as e:
        send_telegram(f"❌ [{symbol}] 매도 오류: {e}")
        return False

# ==========================================
# 코인 하나 처리
# ==========================================
def process_symbol(symbol: str):
    try:
        price = get_price(symbol)
    except Exception as e:
        send_telegram(f"❗ [{symbol}] 가격 조회 오류: {e}")
        return

    state = load_state(symbol)

    # 초기화
    if not state:
        grids = create_grids(price)
        state = {
            "grids": grids,
            "positions": {},
            "last_price": price,
            "total_profit": 0.0,
            "trade_count": 0,
        }
        save_state(symbol, state)
        send_telegram(
            f"📊 [{symbol}] 그리드 생성 완료\n"
            f"상단={grids[-1]:,.0f}원\n"
            f"하단={grids[0]:,.0f}원\n"
            f"간격={grids[1]-grids[0]:,.0f}원\n"
            f"현재가={price:,.0f}원"
        )
        return

    grids = state["grids"]
    last_price = float(state["last_price"])
    positions = state["positions"]

    # 범위 이탈 감지 → 자동 재설정
    if price > grids[-1] or price < grids[0]:
        send_telegram(
            f"⚠️ [{symbol}] 그리드 범위 이탈!\n"
            f"현재가={price:,.0f}원\n"
            f"범위={grids[0]:,.0f}원 ~ {grids[-1]:,.0f}원\n"
            f"그리드 자동 재설정 중..."
        )
        new_grids = create_grids(price)
        state["grids"] = new_grids
        state["positions"] = {}
        state["last_price"] = price
        save_state(symbol, state)
        send_telegram(
            f"✅ [{symbol}] 그리드 재설정 완료!\n"
            f"새 상단={new_grids[-1]:,.0f}원\n"
            f"새 하단={new_grids[0]:,.0f}원\n"
            f"간격={new_grids[1]-new_grids[0]:,.0f}원"
        )
        return

    # 그리드 체크
    for i in range(GRID_COUNT):
        lower_grid = grids[i]
        upper_grid = grids[i + 1]
        grid_key = str(i)

        # 하락 시 매수
        if last_price >= upper_grid > price >= lower_grid:
            if grid_key not in positions:
                amount = GRID_AMOUNT_KRW / price
                if place_buy(symbol, price, i):
                    positions[grid_key] = amount
                    state["trade_count"] += 1

        # 상승 시 매도
        elif last_price <= lower_grid < price <= upper_grid:
            if grid_key in positions:
                amount = positions[grid_key]
                profit = (price - lower_grid) * amount
                profit -= (lower_grid * amount * FEE)
                profit -= (price * amount * FEE)
                if place_sell(symbol, price, i, amount):
                    del positions[grid_key]
                    state["total_profit"] += profit
                    state["trade_count"] += 1
                    send_telegram(
                        f"💰 [{symbol}] 수익 실현!\n"
                        f"그리드 {i}: +{profit:,.0f}원\n"
                        f"누적수익: {state['total_profit']:,.0f}원\n"
                        f"거래횟수: {state['trade_count']}건"
                    )

    state["last_price"] = price
    state["positions"] = positions
    save_state(symbol, state)

# ==========================================
# 메인 루프
# ==========================================
def main():
    send_telegram(f"🤖 멀티코인 그리드 봇 시작! (DRY_RUN={DRY_RUN})\n종목={', '.join(SYMBOLS)}, 그리드={GRID_COUNT}개, 범위=±{GRID_RANGE*100:.0f}%")

    while True:
        try:
            for symbol in SYMBOLS:
                process_symbol(symbol)
            time.sleep(LOOP_SLEEP_SEC)

        except Exception as e:
            send_telegram(f"❗ MAIN LOOP ERROR: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()