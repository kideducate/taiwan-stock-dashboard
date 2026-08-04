"""
build_market_pe_history.py
---------------------------
一次性回補「大盤本益比」歷史資料。
證交所沒有現成的「大盤本益比」時間序列API，只能從全部個股的本益比自己算。

做法（用「成交金額」加權，不需要股本資料）：
  對指定的日期範圍，每個月抓一天（月底最後一個交易日）：
    1) BWIBBU_d：當天全部上市股票的本益比、收盤價
    2) MI_INDEX（type=ALLBUT0999）：當天全部上市股票的成交金額
  兩份資料用證券代號合併，用「成交金額」當權重算出加權平均本益比，
  當作那天的「大盤本益比」。用成交金額當權重是因為證交所沒有現成的
  股本/市值清單可以直接抓，而成交金額大致上也能反映公司規模（權值股
  通常成交金額也大），是市面上抓不到市值時常見的替代做法。

用法：
  python build_market_pe_history.py --start 2023-01-01 --end 2026-07-29

參數：
  --start   回補起始日期（預設 2023-01-01）
  --end     回補結束日期（預設今天）
  --sleep   每次呼叫API之間間隔幾秒，預設3秒
"""
import csv
import time
import argparse
from pathlib import Path
from datetime import date, datetime, timedelta
from curl_cffi import requests as cffi_req

OUTPUT_CSV = Path("market_pe_history.csv")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.twse.com.tw/zh/trading/historical/bwibbu-day.html",
    "Accept-Language": "zh-TW,zh;q=0.9",
}


def _session():
    s = cffi_req.Session(impersonate="chrome124")
    s.verify = False
    return s


def fetch_bwibbu_by_date(date_str):
    """date_str: YYYYMMDD。回傳 {證券代號: 本益比} 或 None（非交易日/無資料）"""
    url = "https://www.twse.com.tw/exchangeReport/BWIBBU_d"
    resp = _session().get(url, params={"response": "json", "date": date_str, "selectType": "ALL"},
                           headers=HEADERS, timeout=20)
    resp.raise_for_status()
    try:
        payload = resp.json()
    except Exception:
        print(f"[BWIBBU_d 非JSON回應 HTTP{resp.status_code} 內容前150字: {resp.text[:150]!r}] ", end="")
        return None
    if payload.get("stat") != "OK":
        print(f"[BWIBBU_d stat={payload.get('stat')!r}] ", end="")
        return None
    fields = payload.get("fields", [])
    rows = payload.get("data", [])
    try:
        idx_code = fields.index("證券代號")
        idx_per = fields.index("本益比")
    except ValueError:
        print(f"  ⚠ BWIBBU_d 欄位對不上，實際欄位：{fields}")
        return None
    result = {}
    for row in rows:
        code = str(row[idx_code]).strip()
        try:
            per = float(row[idx_per])
        except (ValueError, TypeError):
            continue
        if per > 0:
            result[code] = per
    return result if result else None


def fetch_trade_value_by_date(date_str):
    """date_str: YYYYMMDD。回傳 {證券代號: 成交金額} 或 None（非交易日/無資料）"""
    url = "https://www.twse.com.tw/exchangeReport/MI_INDEX"
    resp = _session().get(url, params={"response": "json", "date": date_str, "type": "ALLBUT0999"},
                           headers=HEADERS, timeout=20)
    resp.raise_for_status()
    try:
        payload = resp.json()
    except Exception:
        print(f"[MI_INDEX 非JSON回應 HTTP{resp.status_code} 內容前150字: {resp.text[:150]!r}] ", end="")
        return None
    if payload.get("stat") != "OK":
        print(f"[MI_INDEX stat={payload.get('stat')!r}] ", end="")
        return None

    result = {}
    table_report = []

    # 新版格式：payload 裡有一個 "tables" 清單，每個元素是一張表格
    tables = payload.get("tables")
    if tables:
        for i, t in enumerate(tables):
            if not isinstance(t, dict):
                continue
            fields = t.get("fields") or t.get("field") or t.get("title") or []
            rows = t.get("data") or t.get("rows") or []
            field_names = [f.get("title") if isinstance(f, dict) else f for f in fields] if fields else []
            table_report.append(f"tables[{i}](rows={len(rows) if rows else 0}, fields={field_names[:15]})")
            if not field_names or not rows:
                continue
            try:
                idx_code = field_names.index("證券代號")
                idx_val = field_names.index("成交金額")
            except ValueError:
                continue
            for row in rows:
                if isinstance(row, dict):
                    code = str(row.get("證券代號", "")).strip()
                    raw_val = row.get("成交金額")
                else:
                    code = str(row[idx_code]).strip()
                    raw_val = row[idx_val]
                try:
                    val = float(str(raw_val).replace(",", ""))
                except (ValueError, TypeError):
                    continue
                if code and val > 0:
                    result[code] = val
    else:
        for key in payload:
            if not key.startswith("data"):
                continue
            fields_key = "fields" + key[4:]
            fields = payload.get(fields_key) or payload.get("fields")
            rows = payload.get(key)
            table_report.append(f"{key}(rows={len(rows) if rows else 0},fields={fields})")
            if not fields or not rows:
                continue
            try:
                idx_code = fields.index("證券代號")
                idx_val = fields.index("成交金額")
            except ValueError:
                continue
            for row in rows:
                code = str(row[idx_code]).strip()
                try:
                    val = float(str(row[idx_val]).replace(",", ""))
                except (ValueError, TypeError):
                    continue
                if val > 0:
                    result[code] = val

    if not result:
        print(f"[MI_INDEX 解析後為空 payload的keys={list(payload.keys())} 各表狀況={table_report}] ", end="")
        return None
    return result


def compute_weighted_pe(per_map, value_map, max_per=100):
    """回傳 (加權平均本益比, 納入計算的檔數) 或 None（樣本不足）。
    排除本益比 > max_per 的離群值（通常是獲利接近0、本益比被墊高到幾百倍的個股，
    這種股票即使成交金額大，也不該主導整個大盤的本益比估算）。
    """
    total_weight = 0.0
    total_weighted_pe = 0.0
    used = 0
    for code, per in per_map.items():
        if per > max_per:
            continue
        val = value_map.get(code)
        if not val:
            continue
        total_weight += val
        total_weighted_pe += val * per
        used += 1
    if total_weight <= 0 or used < 30:
        return None
    return round(total_weighted_pe / total_weight, 2), used


def month_end_dates(start, end):
    dates = []
    d = date(start.year, start.month, 1)
    while d <= end:
        next_month = date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)
        last_day = min(next_month - timedelta(days=1), end)
        dates.append(last_day)
        d = next_month
    return dates


def load_existing():
    existing = {}
    if OUTPUT_CSV.exists():
        with open(OUTPUT_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing[row["date"]] = row["market_pe"]
    return existing


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2024-11-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--sleep", type=float, default=8.0)
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()

    results = load_existing()
    print(f"已有 {len(results)} 筆歷史資料，重複的日期會自動跳過\n")

    consecutive_fail = 0
    for target in month_end_dates(start, end):
        if consecutive_fail >= 2:
            print(f"\n⚠ 已經連續 {consecutive_fail} 個月都抓不到資料，很可能是被證交所限流了。")
            print("先停在這裡，等個10-20分鐘再重新執行同一行指令即可（已存到的資料不會重抓）。")
            break
        d = target
        tried = 0
        got = False
        while tried < 6:
            date_str = d.strftime("%Y%m%d")
            if date_str in results:
                print(f"{date_str} 已有資料，跳過")
                got = True
                break
            print(f"抓 {date_str} ...", end=" ", flush=True)
            try:
                per_map = fetch_bwibbu_by_date(date_str)
                time.sleep(args.sleep)
                if per_map is None:
                    print("BWIBBU_d 非交易日/無資料，往前一天")
                    d -= timedelta(days=1); tried += 1; time.sleep(args.sleep)
                    continue
                value_map = fetch_trade_value_by_date(date_str)
                time.sleep(args.sleep)
                if value_map is None:
                    print("MI_INDEX 非交易日/無資料，往前一天")
                    d -= timedelta(days=1); tried += 1; time.sleep(args.sleep)
                    continue
                pe_result = compute_weighted_pe(per_map, value_map)
                if pe_result is None:
                    print(f"樣本不足（本益比{len(per_map)}檔／成交金額{len(value_map)}檔），往前一天")
                    d -= timedelta(days=1); tried += 1; time.sleep(args.sleep)
                    continue
                pe, used = pe_result
                results[date_str] = pe
                print(f"大盤本益比 {pe}（{used}檔納入計算）")
                got = True
                break
            except Exception as e:
                print(f"失敗：{e}，往前一天")
                d -= timedelta(days=1); tried += 1; time.sleep(args.sleep)
                continue
        if not got:
            print(f"⚠ {target} 附近找不到可用資料，跳過")
            consecutive_fail += 1
        else:
            consecutive_fail = 0
        time.sleep(args.sleep)

    with open(OUTPUT_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "market_pe"])
        for d in sorted(results.keys()):
            writer.writerow([d, results[d]])

    print(f"\n完成，共 {len(results)} 筆資料存進 {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
