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
SYMBOL = "XRP/KRW"
GRID_COUNT = 5         # 그리드 수
GRID_RANGE = 0.10      # 현재가 기준 ± 10%
GRID_AMOUNT_KRW = 20000  # 그리드당 금액 (원)
FEE = 0.0025           # 빗썸 수수료 0.25%

LOOP_SLEEP_SEC = 10    # 10초마다 체크

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

    grids = []
    for i in range(GRID_COUNT + 1):
        price = lower + step * i
        grids.append(round(price, 2))

    return grids

# ==========================================
# 그리드 상태 저장/로드
# ==========================================
GRID_STATE_PATH = "/root/tfbot/grid_state.json"

def save_grid_state(state: dict):
    with open(GRID_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

def load_grid_state() -> dict:
    if not os.path.exists(GRID_STATE_PATH):
        return {}
    with open(GRID_STATE_PATH, "r") as f:
        return json.load(f)

# ==========================================
# 현재가 조회
# ==========================================
def get_price() -> float:
    ticker = exchange.fetch_ticker(SYMBOL)
    return float(ticker['last'])

# ==========================================
# 매수/매도
# ==========================================
def place_buy(price: float, grid_idx: int):
    amount_coin = GRID_AMOUNT_KRW / price
    if DRY_RUN:
        send_telegram(f"[DRY RUN] 그리드 {grid_idx} 매수\n가격={price:,.0f}원, 수량={amount_coin:.4f}")
        return True
    try:
        exchange.create_market_buy_order(SYMBOL, amount_coin)
        send_telegram(f"✅ 그리드 {grid_idx} 매수 완료\n가격={price:,.0f}원, 수량={amount_coin:.4f}")
        return True
    except Exception as e:
        send_telegram(f"❌ 매수 오류: {e}")
        return False

def place_sell(price: float, grid_idx: int, amount_coin: float):
    if DRY_RUN:
        send_telegram(f"[DRY RUN] 그리드 {grid_idx} 매도\n가격={price:,.0f}원, 수량={amount_coin:.4f}")
        return True
    try:
        exchange.create_market_sell_order(SYMBOL, amount_coin)
        send_telegram(f"🔻 그리드 {grid_idx} 매도 완료\n가격={price:,.0f}원, 수량={amount_coin:.4f}")
        return True
    except Exception as e:
        send_telegram(f"❌ 매도 오류: {e}")
        return False

# ==========================================
# 메인 루프
# ==========================================
def main():
    send_telegram(f"🤖 그리드 봇 시작! (DRY_RUN={DRY_RUN})\n종목={SYMBOL}, 그리드={GRID_COUNT}개, 범위=±{GRID_RANGE*100:.0f}%")

    # 기존 상태 삭제 후 초기화
    if os.path.exists(GRID_STATE_PATH):
        os.remove(GRID_STATE_PATH)

    # 초기 가격으로 그리드 생성
    current_price = get_price()
    grids = create_grids(current_price)

    send_telegram(
        f"📊 그리드 생성 완료\n"
        f"상단={grids[-1]:,.0f}원\n"
        f"하단={grids[0]:,.0f}원\n"
        f"간격={grids[1]-grids[0]:,.0f}원"
    )

    state = {
        "grids": grids,
        "positions": {},
        "last_price": current_price,
        "total_profit": 0.0,
        "trade_count": 0,
    }
    save_grid_state(state)

    last_price = current_price
    send_telegram(f"현재가: {current_price:,.0f}원")

    while True:
        try:
            price = get_price()
            positions = state["positions"]
            grids = state["grids"]

            for i in range(GRID_COUNT):
                lower_grid = grids[i]
                upper_grid = grids[i + 1]
                grid_key = str(i)

                # 가격이 그리드 하단 아래로 내려오면 매수
                if last_price > lower_grid >= price:
                    if grid_key not in positions:
                        amount = GRID_AMOUNT_KRW / lower_grid
                        if place_buy(lower_grid, i):
                            positions[grid_key] = amount
                            state["trade_count"] += 1

                # 가격이 그리드 상단 위로 올라가면 매도 (포지션 있을 때만)
                elif last_price < upper_grid <= price:
                    if grid_key in positions:
                        amount = positions[grid_key]
                        buy_price = lower_grid
                        sell_price = upper_grid
                        profit = (sell_price - buy_price) * amount
                        profit -= (buy_price * amount * FEE)
                        profit -= (sell_price * amount * FEE)
                        if place_sell(upper_grid, i, amount):
                            del positions[grid_key]
                            state["total_profit"] += profit
                            state["trade_count"] += 1
                            send_telegram(
                                f"💰 수익 실현!\n"
                                f"그리드 {i}: +{profit:,.0f}원\n"
                                f"누적수익: {state['total_profit']:,.0f}원\n"
                                f"거래횟수: {state['trade_count']}건"
                            )

            state["last_price"] = price
            state["positions"] = positions
            save_grid_state(state)

            last_price = price

	    # 범위 이탈 감지 → 자동 재설정
            if price > grids[-1] or price < grids[0]:
                send_telegram(
                    f"⚠️ 그리드 범위 이탈!\n"
                    f"현재가={price:,.0f}원\n"
                    f"범위={grids[0]:,.0f}원 ~ {grids[-1]:,.0f}원\n"
                    f"그리드 자동 재설정 중..."
                )
                # 기존 포지션 초기화
                new_grids = create_grids(price)
                state["grids"] = new_grids
                state["positions"] = {}
                state["last_price"] = price
                save_grid_state(state)
                last_price = price
                grids = new_grids
                send_telegram(
                    f"✅ 그리드 재설정 완료!\n"
                    f"새 상단={new_grids[-1]:,.0f}원\n"
                    f"새 하단={new_grids[0]:,.0f}원\n"
                    f"간격={new_grids[1]-new_grids[0]:,.0f}원"
                )

            time.sleep(LOOP_SLEEP_SEC)

        except Exception as e:
            send_telegram(f"❗ 오류: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
