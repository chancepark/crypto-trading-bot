# 🤖 Crypto Trading Bot

Algorithmic trading bot for cryptocurrency markets, implemented in Python using the Bithumb exchange API.

---

## 📌 Strategies

### 1. Grid Trading Bot (`grid_bot.py`)
A grid trading strategy that automatically buys and sells at predefined price intervals.

**Key Features:**
- Automatic grid creation based on current price ± 10%
- 5 grid levels with 20,000 KRW per grid
- Auto-reset when price moves outside grid range
- Telegram notifications for all trades
- DRY_RUN mode for paper trading

**Parameters:**
- Symbol: XRP/KRW
- Grid Count: 5
- Range: ±10%
- Fee: 0.25% (Bithumb)

### 2. Donchian Breakout Bot (`bot.py`)
A trend-following strategy using Donchian Channel breakouts with ATR-based position sizing.

**Key Features:**
- 4H timeframe Donchian Channel (20 periods)
- ATR-based trailing stop (3x ATR)
- Partial take profit at 1R and 2R
- 1D trend filter (MA60)
- RSI swing entry (semi-automatic via Telegram)
- 6 coins: BTC, ETH, XRP, SOL, ADA, AVAX

---

## 📊 Backtest Results (Donchian Strategy)

Backtested on BTC/USDT (Nov 2025 ~ Apr 2026):
- Total trades: 16
- Win rate: 0% (trending sideways market)
- Return: -8.2%
- MDD: -10.2%

**Conclusion:** Donchian breakout underperforms in sideway markets. Strategy is market-regime dependent.

---

## 🛠 Tech Stack

- **Python**
- **ccxt** — exchange API
- **pandas** — data processing
- **python-dotenv** — environment variables
- **Telegram Bot API** — notifications

---

## ⚙️ Setup

```bash
pip install ccxt pandas python-dotenv requests
```

Create `.env` file:
BITHUMB_API_KEY=your_api_key
BITHUMB_API_SECRET=your_api_secret
TELEGRAM_TOKEN=your_telegram_token
TELEGRAM_CHAT_ID=your_chat_id
DRY_RUN=True
Run:
```bash
python3 grid_bot.py
```

---

## 👤 Author

Chan Soo Park  
Fund Manager @ Samsung Asset Management  
M.S. Management Science & Engineering, Stanford University  
🔗 [LinkedIn](https://www.linkedin.com/in/chan-soo-park-7022272a)
🔗 [Portfolio Analysis](https://github.com/chancepark/portfolio-analytics)