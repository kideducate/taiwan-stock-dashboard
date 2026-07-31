"""
vix_fetcher.py
--------------
抓取美股VIX (^VIX，CBOE) 跟台指VIX (VIXTWN，台灣期交所編製)兩個恐慌指數，
各自本地快取 (vix_cache.json / twvix_cache.json)，避免每次 run_all.py 執行都重新打API。

用法：
    from vix_fetcher import get_vix, get_twvix

    vix_data = get_vix()      # 美股VIX，來源 yfinance ^VIX
    twvix_data = get_twvix()  # 台指VIX，來源 FinMind TaiwanOptionVix 資料集
    # 兩者格式一致：
    # {'value': 16.42, 'change': -0.35, 'change_pct': -2.09,
    #  'level': '正常', 'date': '2026-07-29', 'source': 'live'}

整合建議：
    - run_all.py: 在 scan 階段結束後呼叫，把結果塞進你現有的 dashboard.json context 裡。
    - server.py (Flask): 加 /api/vix、/api/twvix 路由，直接回傳。
"""

import json
import os
from datetime import date, datetime, timedelta

_DIR = os.path.dirname(__file__)
VIX_CACHE_FILE = os.path.join(_DIR, "vix_cache.json")
TWVIX_CACHE_FILE = os.path.join(_DIR, "twvix_cache.json")
CACHE_TTL_MINUTES = 30  # 非交易時間內沒必要頻繁刷新


def _load_cache(cache_file):
    if not os.path.exists(cache_file):
        return None
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            cache = json.load(f)
        cached_time = datetime.fromisoformat(cache["fetched_at"])
        if datetime.now() - cached_time < timedelta(minutes=CACHE_TTL_MINUTES):
            return cache["data"]
    except (json.JSONDecodeError, KeyError, ValueError):
        pass
    return None


def _save_cache(cache_file, data):
    cache = {"fetched_at": datetime.now().isoformat(), "data": data}
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def _classify(v: float) -> str:
    if v < 15:
        return "平靜"
    elif v < 20:
        return "正常"
    elif v < 30:
        return "緊張"
    else:
        return "恐慌"


def _unavailable(error: str) -> dict:
    return {
        "value": None, "change": None, "change_pct": None,
        "level": "無法取得", "date": None, "source": "unavailable",
        "error": error,
    }


# ── 美股VIX（CBOE ^VIX，經yfinance）──────────────────────────
def get_vix(force_refresh: bool = False) -> dict:
    """回傳最新美股VIX指數資料。優先讀快取，過期或強制刷新才打API。"""
    if not force_refresh:
        cached = _load_cache(VIX_CACHE_FILE)
        if cached is not None:
            cached = dict(cached, source="cache")
            return cached

    try:
        import yfinance as yf

        ticker = yf.Ticker("^VIX")
        hist = ticker.history(period="5d")
        if hist.empty:
            raise ValueError("yfinance 回傳空資料")

        latest = hist["Close"].iloc[-1]
        prev = hist["Close"].iloc[-2] if len(hist) > 1 else latest
        change = latest - prev
        change_pct = (change / prev * 100) if prev else 0.0

        data = {
            "value": round(float(latest), 2),
            "change": round(float(change), 2),
            "change_pct": round(float(change_pct), 2),
            "level": _classify(latest),
            "date": hist.index[-1].strftime("%Y-%m-%d"),
            "source": "live",
        }
        _save_cache(VIX_CACHE_FILE, data)
        return data

    except Exception as e:
        stale = _load_cache(VIX_CACHE_FILE)
        if stale is not None:
            return dict(stale, source="stale")
        return _unavailable(str(e))


# ── 台指VIX（VIXTWN，台灣期交所編製）──────────────────────
def get_twvix(force_refresh: bool = False) -> dict:
    """回傳最新台指選擇權波動率指數(VIXTWN)資料。優先讀快取，過期或強制刷新才打API。
    資料來源：玩股網背後的即時報價API（https://www.wantgoo.com/investrue/all-quote-info），
    免費、不需帳號，回傳一個JSON陣列，裡面找 id=="VIXTWN" 那筆。
    """
    if not force_refresh:
        cached = _load_cache(TWVIX_CACHE_FILE)
        if cached is not None:
            cached = dict(cached, source="cache")
            return cached

    try:
        import requests

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.wantgoo.com/index/vixtwn",
            "Accept": "application/json, text/plain, */*",
        }
        resp = requests.get(
            "https://www.wantgoo.com/investrue/all-quote-info",
            headers=headers, timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()

        target = None
        for row in rows:
            if row.get("id") == "VIXTWN":
                target = row
                break
        if target is None:
            raise ValueError("回傳資料裡找不到 id=='VIXTWN' 這筆")

        latest_val = float(target["close"])
        prev_val = float(target.get("previousClose") or latest_val)
        change = latest_val - prev_val
        change_pct = (change / prev_val * 100) if prev_val else 0.0

        trade_date_ms = target.get("tradeDate")
        if trade_date_ms:
            trade_date = datetime.fromtimestamp(trade_date_ms / 1000).strftime("%Y-%m-%d")
        else:
            trade_date = date.today().isoformat()

        data = {
            "value": round(latest_val, 2),
            "change": round(change, 2),
            "change_pct": round(change_pct, 2),
            "level": _classify(latest_val),
            "date": trade_date,
            "source": "live",
        }
        _save_cache(TWVIX_CACHE_FILE, data)
        return data

    except Exception as e:
        stale = _load_cache(TWVIX_CACHE_FILE)
        if stale is not None:
            return dict(stale, source="stale")
        return _unavailable(str(e))


if __name__ == "__main__":
    print("美股VIX:", json.dumps(get_vix(force_refresh=True), ensure_ascii=False, indent=2))
    print("台指VIX:", json.dumps(get_twvix(force_refresh=True), ensure_ascii=False, indent=2))
