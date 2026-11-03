"""
台股 ETF / 個股儀表板
流程:輸入代號(ETF 或個股) -> 成分股與權重(個股 = 100%) -> 選日期 -> 選面向(成交量 / 技術面 / 籌碼面)

安裝:  pip install -r requirements.txt
執行:  streamlit run stock_comp.py --server.address 0.0.0.0
雲端:  見 DEPLOY.md(packages.txt 會自動安裝 chromium)
手機/平板:  開啟後看左側欄(手機點左上角 >)的 QR code,用手機相機掃描即可
需要:  本機已安裝 Chrome(Selenium 4 會自動管理 chromedriver)

資料來源:
  成分股權重  元大投信 ETF 持股比重頁(Selenium 抓取,僅適用元大發行的 ETF,如 0050、0056)
  成交量/價格 證交所 STOCK_DAY(上市)、櫃買中心 tradingStock(上櫃)
  籌碼面      證交所 T86(上市)、櫃買中心 insti/dailyTrade(上櫃)三大法人買賣超
  股票名稱    證交所 / 櫃買中心 OpenAPI 公司基本資料
"""
import re
import shutil
import socket
import time
from datetime import date, datetime, timedelta
from io import BytesIO, StringIO

import pandas as pd
import requests
import streamlit as st
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

try:
    import segno  # QR code(純 Python,不需要 PIL)
except ImportError:
    segno = None

import sys
from streamlit.runtime import exists as _st_running

if not _st_running():
    print("這是 Streamlit 程式,請在終端機(命令提示字元)用下列指令執行,不要直接用 python 或 Jupyter 跑:\n"
          "    streamlit run stock_comp.py --server.address 0.0.0.0")
    sys.exit(0)

HEADERS = {"User-Agent": "Mozilla/5.0"}
YUANTA = "https://www.yuantaetfs.com/product/detail/{code}/ratio"


# ---------------------------------------------------------------- 1. 成分股
def normalize_code(text: str) -> str:
    """'50' -> '0050';'00981a' -> '00981A'"""
    t = text.strip().upper().replace(".TW", "").replace(".TWO", "")
    return t.zfill(4) if t.isdigit() and len(t) < 4 else t


def make_driver():
    """本機用 Chrome(Selenium 自動管理 driver);Linux/雲端用 apt 安裝的 Chromium。"""
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")  # 雲端 /dev/shm 很小,避免 Chrome 崩潰
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1400,1000")
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})  # 攔截頁面的 XHR/API 回應
    browser = shutil.which("chromium") or shutil.which("chromium-browser")
    driver_bin = shutil.which("chromedriver")
    if browser and driver_bin:
        opts.binary_location = browser
        return webdriver.Chrome(service=Service(driver_bin), options=opts)
    return webdriver.Chrome(options=opts)


def _btn_xpath(*kws: str) -> str:
    """找「看更多 / 載入更多 / 展開 / 下一頁」這類短文字按鈕(排除會跳到別頁的一般連結)。"""
    parts = []
    for kw in kws:
        short = f"string-length(normalize-space(.))<=8 and contains(normalize-space(.),'{kw}')"
        parts.append(f"//button[{short}]")
        parts.append(f"//a[{short}][not(@href) or starts-with(@href,'#') or starts-with(@href,'javascript')]")
        parts.append(f"//*[self::div or self::span or self::li or self::p]"
                     f"[not(*)][{short}]")  # [not(*)] = 最內層文字節點,避免點到整個容器
    return "|".join(parts)


def _click_first(driver, xpath: str) -> bool:
    """點第一個可見的符合元素;點得到回傳 True。"""
    try:
        for el in driver.find_elements(By.XPATH, xpath):
            if el.is_displayed() and el.is_enabled():
                driver.execute_script("arguments[0].click();", el)
                return True
    except Exception:
        pass
    return False


# 口袋證券持股頁是前端渲染(Nuxt),要等表格出現;「代號 名稱 權重 持有數 單位」
_ROW_RE = re.compile(r"(?<![\w.])(\d{4,6}[A-Z]?)\s+(\S+?)\s+(\d+(?:\.\d+)?)\s*%")
_CODE_RE = re.compile(r"^\d{4,6}[A-Z]?$")  # 排除 CASH / RP / MARGIN 等非股票列
_TABLE_JS = ("return Array.from(document.querySelectorAll('table tr')).map("
             "tr => Array.from(tr.querySelectorAll('th,td')).map(c => c.innerText.trim()));")
_MORE_TEXTS = ("查看更多", "顯示更多", "載入更多", "更多", "展開全部", "展開",
               "顯示全部", "查看全部", "全部顯示", "下一頁")


def _more_xpath() -> str:
    cond = " or ".join(f"normalize-space(.)='{t}'" for t in _MORE_TEXTS)
    # <a> 只點不會換頁的(無 href、# 或 javascript),避免誤跳到別的頁面
    return (f"//button[{cond}]|//span[{cond}]|//div[{cond}]|//li[{cond}]"
            f"|//a[({cond}) and (not(@href) or starts-with(@href,'#') or starts-with(@href,'javascript'))]")


def _extract_rows(driver) -> dict:
    """從目前頁面抓出 {代號: {名稱, 權重}}。先讀 <table>,讀不到再用文字比對備援。"""
    out = {}
    try:
        rows = driver.execute_script(_TABLE_JS) or []
    except Exception:
        rows = []
    idx = None
    for r in rows:
        c_code = next((i for i, c in enumerate(r) if "代號" in c), None)
        c_w = next((i for i, c in enumerate(r) if "權重" in c), None)
        if c_code is not None and c_w is not None:  # 表頭列
            c_name = next((i for i, c in enumerate(r) if "名稱" in c), None)
            idx = (c_code, c_name, c_w)
            continue
        if not idx or len(r) <= max(i for i in idx if i is not None):
            continue
        code = r[idx[0]].strip().upper()
        m = re.search(r"\d+(?:\.\d+)?", r[idx[2]].replace(",", ""))
        if not _CODE_RE.match(code) or not m:
            continue
        name = r[idx[1]].strip() if idx[1] is not None else code
        out.setdefault(code, {"代號": code, "名稱": name.replace("*", "").strip(),
                              "權重(%)": float(m.group())})
    if out:
        return out
    # 備援:表格不是 <table> 時,直接比對整頁文字
    try:
        text = driver.execute_script("return document.body.innerText;") or ""
    except Exception:
        text = ""
    for code, name, w in _ROW_RE.findall(text):
        if _CODE_RE.match(code):
            out.setdefault(code, {"代號": code, "名稱": name.replace("*", "").strip(),
                                  "權重(%)": float(w)})
    return out


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_holdings(etf: str) -> pd.DataFrame:
    """口袋證券 ETF 持股明細:捲到底、點「更多/下一頁」直到不再增加,確保拿到全部持股。"""
    driver = make_driver()
    try:
        # finally 裡的程式不管有沒有出錯都會執行,確保 Chrome 一定被關掉,不會留下殭屍程序。
        driver.get(POCKET.format(code=etf))
        WebDriverWait(driver, 25).until(lambda d: bool(_extract_rows(d)))  # 等前端渲染出持股
        seen = dict(_extract_rows(driver))
        stale = 0
        for _ in range(60):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.8)
            clicked = _click_first(driver, _more_xpath())
            if clicked:
                time.sleep(1.2)
            new = _extract_rows(driver)
            grew = any(k not in seen for k in new)
            for k, v in new.items():
                seen.setdefault(k, v)
            if grew:
                stale = 0
            else:
                stale += 1
                if not clicked or stale >= 2:
                    break
    finally:
        driver.quit()

    if not seen:
        return pd.DataFrame()
    out = pd.DataFrame(list(seen.values())).dropna()
    return out.sort_values("權重(%)", ascending=False).reset_index(drop=True)


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_name_map() -> dict:
    """上市 + 上櫃 公司代號 -> 簡稱。抓不到就回傳空 dict(不影響主流程)。"""
    urls = ["https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
            "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"]
    mp = {}
    for u in urls:
        try:
            for r in requests.get(u, headers=HEADERS, timeout=15).json():
                code = str(r.get("公司代號", "")).strip()
                if code:
                    mp[code] = str(r.get("公司簡稱", code)).strip()
        except Exception:
            pass
    return mp


# ---------------------------------------------------------------- 2. 價量
def _num(s):
    return pd.to_numeric(str(s).replace(",", "").replace("--", "").replace("---", ""),
                         errors="coerce")


def _roc(s: str) -> date:
    """民國年 '115/10/29' -> 西元 date(去掉可能夾帶的 * 等符號)"""
    y, m, d = re.sub(r"[^\d/]", "", s).split("/")
    return date(int(y) + 1911, int(m), int(d))


def _month_starts(end: date, months: int):
    cur = end.replace(day=1)
    for _ in range(months):
        yield cur
        cur = (cur - timedelta(days=1)).replace(day=1)


def _fetch_prices_twse(stock: str, end: date, months: int) -> pd.DataFrame:
    frames = []
    for cur in _month_starts(end, months):
        url = ("https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
               f"?date={cur:%Y%m%d}&stockNo={stock}&response=json")
        try:
            j = requests.get(url, headers=HEADERS, timeout=15).json()
        except Exception:
            j = {}
        if j.get("stat") == "OK" and j.get("data"):
            frames.append(pd.DataFrame(j["data"], columns=j["fields"]))
        time.sleep(0.4)  # 避免被證交所限流
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    return pd.DataFrame({
        "日期": df["日期"].map(_roc),
        "open": df["開盤價"].map(_num), "high": df["最高價"].map(_num),
        "low": df["最低價"].map(_num), "close": df["收盤價"].map(_num),
        "volume": df["成交股數"].map(_num) / 1000,  # 張
    })


def _fetch_prices_tpex(stock: str, end: date, months: int) -> pd.DataFrame:
    """上櫃股票日線(櫃買中心)。以欄位關鍵字比對,降低欄位名稱小改版的影響。"""
    frames = []
    for cur in _month_starts(end, months):
        url = ("https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"
               f"?code={stock}&date={cur:%Y/%m/%d}&response=json")
        try:
            j = requests.get(url, headers=HEADERS, timeout=15).json()
        except Exception:
            j = {}
        tb = (j.get("tables") or [{}])[0]
        if tb.get("data"):
            frames.append(pd.DataFrame(tb["data"], columns=tb["fields"]))
        time.sleep(0.4)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    pick = lambda kw: next((c for c in df.columns if kw in c), None)
    c_date, c_o, c_h, c_l, c_c = (pick(k) for k in ("日期", "開盤", "最高", "最低", "收盤"))
    c_v = pick("成交張數") or pick("成交仟股") or pick("成交股數")
    if not all([c_date, c_o, c_h, c_l, c_c, c_v]):
        return pd.DataFrame()
    vol = df[c_v].map(_num)
    if "股數" in c_v and "仟" not in c_v:  # 單位是股 -> 換成張
        vol = vol / 1000
    return pd.DataFrame({
        "日期": df[c_date].map(_roc),
        "open": df[c_o].map(_num), "high": df[c_h].map(_num),
        "low": df[c_l].map(_num), "close": df[c_c].map(_num),
        "volume": vol,
    })


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_prices(stock: str, end: date, months: int = 4) -> pd.DataFrame:
    """抓到 end 為止約 months 個月的日線。先試上市(證交所),沒有再試上櫃(櫃買)。"""
    out = _fetch_prices_twse(stock, end, months)
    if out.empty:
        out = _fetch_prices_tpex(stock, end, months)
    if out.empty:
        return pd.DataFrame()
    out = out.dropna().sort_values("日期").drop_duplicates("日期")
    return out[out["日期"] <= end].reset_index(drop=True)


# ---------------------------------------------------------------- 3. 技術面
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    for n in (5, 20, 60):
        d[f"MA{n}"] = d["close"].rolling(n).mean()
    # RSI(14)
    diff = d["close"].diff()
    gain = diff.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-diff.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    d["RSI14"] = 100 - 100 / (1 + gain / loss.replace(0, float("nan")))
    # KD(9,3,3)
    low9, high9 = d["low"].rolling(9).min(), d["high"].rolling(9).max()
    rsv = (d["close"] - low9) / (high9 - low9) * 100
    d["K"] = rsv.ewm(com=2, adjust=False).mean()
    d["D"] = d["K"].ewm(com=2, adjust=False).mean()
    # MACD(12,26,9)
    ema12 = d["close"].ewm(span=12, adjust=False).mean()
    ema26 = d["close"].ewm(span=26, adjust=False).mean()
    d["DIF"] = ema12 - ema26
    d["MACD"] = d["DIF"].ewm(span=9, adjust=False).mean()
    d["OSC"] = d["DIF"] - d["MACD"]
    return d


def tech_summary(row: pd.Series) -> str:
    tags = []
    if pd.notna(row.get("MA20")):
        tags.append("站上月線" if row["close"] > row["MA20"] else "跌破月線")
    if pd.notna(row.get("MA5")) and pd.notna(row.get("MA20")):
        tags.append("短多排列" if row["MA5"] > row["MA20"] else "短空排列")
    if pd.notna(row.get("RSI14")):
        tags.append("RSI超買" if row["RSI14"] > 70 else "RSI超賣" if row["RSI14"] < 30 else "RSI中性")
    if pd.notna(row.get("OSC")):
        tags.append("MACD偏多" if row["OSC"] > 0 else "MACD偏空")
    return "、".join(tags)


# ---------------------------------------------------------------- 4. 籌碼面
def _fetch_t86_twse(day: date) -> pd.DataFrame:
    url = ("https://www.twse.com.tw/rwd/zh/fund/T86"
           f"?date={day:%Y%m%d}&selectType=ALLBUT0999&response=json")
    try:
        j = requests.get(url, headers=HEADERS, timeout=15).json()
    except Exception:
        return pd.DataFrame()
    if j.get("stat") != "OK":
        return pd.DataFrame()
    df = pd.DataFrame(j["data"], columns=j["fields"])
    pick = lambda kw, ex=None: next(
        (c for c in df.columns if kw in c and (ex is None or ex not in c)), None)
    cols = {
        "代號": pick("證券代號"),
        "外資": pick("外陸資買賣超股數", "自營"),
        "投信": pick("投信買賣超股數"),
        "自營商": pick("自營商買賣超股數"),
        "三大法人": pick("三大法人買賣超股數"),
    }
    out = pd.DataFrame({"代號": df[cols["代號"]].str.strip()})
    nm = pick("證券名稱")
    out["名稱"] = df[nm].str.strip() if nm else ""
    for k in ("外資", "投信", "自營商", "三大法人"):
        out[k + "(張)"] = (df[cols[k]].map(_num) / 1000).round(0) if cols[k] else None
    return out


def _fetch_t86_tpex(day: date) -> pd.DataFrame:
    """上櫃三大法人買賣超(櫃買中心)。以欄位關鍵字比對。"""
    url = ("https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"
           f"?type=Daily&sect=AL&date={day:%Y/%m/%d}&id=&response=json")
    try:
        j = requests.get(url, headers=HEADERS, timeout=15).json()
    except Exception:
        return pd.DataFrame()
    tb = (j.get("tables") or [{}])[0]
    if not tb.get("data"):
        return pd.DataFrame()
    df = pd.DataFrame(tb["data"], columns=tb["fields"])

    def find(*must, no=()):
        return next((c for c in df.columns
                     if all(m in c for m in must) and not any(n in c for n in no)), None)

    cols = {
        "代號": find("代號"),
        "外資": find("外資及陸資", "買賣超", "不含") or find("外資及陸資", "買賣超"),
        "投信": find("投信", "買賣超"),
        "自營商": find("自營商", "買賣超", "合計") or find("自營商", "買賣超", no=("外資",)),
        "三大法人": find("三大法人", "買賣超"),
    }
    if not cols["代號"]:
        return pd.DataFrame()
    out = pd.DataFrame({"代號": df[cols["代號"]].astype(str).str.strip()})
    nm = find("名稱")
    out["名稱"] = df[nm].astype(str).str.strip() if nm else ""
    for k in ("外資", "投信", "自營商", "三大法人"):
        out[k + "(張)"] = (df[cols[k]].map(_num) / 1000).round(0) if cols[k] else None
    return out


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_t86(day: date) -> pd.DataFrame:
    """三大法人買賣超(上市 + 上櫃),單位換算成張。非交易日回傳空表。"""
    parts = [p for p in (_fetch_t86_twse(day), _fetch_t86_tpex(day)) if not p.empty]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def latest_trading_day(start: date) -> date:
    d = start
    for _ in range(10):
        # 只用上市資料判斷是否為交易日(上市、上櫃休市日相同,可少打一個 API)
        if d.weekday() < 5 and not _fetch_t86_twse(d).empty:
            return d
        d -= timedelta(days=1)
    return start


# ---------------------------------------------------------------- UI
ASPECTS = ["成交量", "技術面", "籌碼面"]


def build_row(view, base, px, last, prev, t86):
    if view == "成交量":
        vol5 = px["volume"].tail(5).mean()
        base.update({
            "收盤": last["close"],
            "漲跌幅(%)": round((last["close"] / prev["close"] - 1) * 100, 2),
            "成交量(張)": int(last["volume"]),
            "5日均量(張)": int(vol5),
            "量比": round(last["volume"] / vol5, 2) if vol5 else None,
        })
    elif view == "技術面":
        base.update({
            "收盤": last["close"],
            "MA5": round(last["MA5"], 2), "MA20": round(last["MA20"], 2),
            "MA60": round(last["MA60"], 2) if pd.notna(last["MA60"]) else None,
            "RSI14": round(last["RSI14"], 1),
            "K": round(last["K"], 1), "D": round(last["D"], 1),
            "MACD柱": round(last["OSC"], 2),
            "訊號": tech_summary(last),
        })
    else:  # 籌碼面
        chip = t86[t86["代號"] == base["代號"]] if not t86.empty else t86
        if not chip.empty:
            base.update(chip.drop(columns=["代號", "名稱"]).iloc[0].to_dict())
    return base


def parse_codes(raw: str) -> list:
    """'0050, 2330  6488' -> ['0050', '2330', '6488'](去重、保留順序)"""
    out = []
    for t in re.split(r"[,，、;；\s]+", raw):
        c = normalize_code(t) if t.strip() else ""
        if c and c not in out:
            out.append(c)
    return out


MAX_COMPARE = 6


def run_compare(codes: list):
    """多個代碼(ETF 或個股皆可)並排比較。ETF 以其本身的成交價量/技術/籌碼資料比較。"""
    if len(codes) > MAX_COMPARE:
        st.warning(f"一次最多比較 {MAX_COMPARE} 個代碼,只取前 {MAX_COMPARE} 個。")
        codes = codes[:MAX_COMPARE]
    st.subheader("比較:" + "、".join(codes))

    c1, c2 = st.columns([1, 3])
    picked = c1.date_input("選擇日期", value=date.today(),
                           max_value=date.today(), key="cmp_date")
    views = c2.multiselect("選擇要比較的面向(可複選)", ASPECTS,
                           default=[], key="cmp_views")
    if not views:
        st.info("請在上方選擇要比較的面向:成交量、技術面、籌碼面。")
        return

    day = latest_trading_day(picked)
    if day != picked:
        st.info(f"{picked} 無交易資料,改用最近交易日 {day}")
    t86 = fetch_t86(day)
    name_map = fetch_name_map()

    def name_of(c):
        if c in name_map:
            return name_map[c]
        if not t86.empty:
            nm = t86.loc[t86["代號"] == c, "名稱"]
            if not nm.empty:
                return nm.iloc[0]
        return c

    rows = {v: [] for v in views}
    detail = {}
    bar = st.progress(0.0, text="抓取資料…")
    for i, c in enumerate(codes):
        bar.progress((i + 1) / len(codes), text=c)
        px = fetch_prices(c, day)
        if px.empty:
            st.warning(f"{c} 抓不到價量資料,已略過(請確認代碼是否正確)。")
            continue
        p = add_indicators(px)
        detail[c] = p
        prev = p.iloc[-2] if len(p) > 1 else p.iloc[-1]
        base = {"代號": c, "名稱": name_of(c),
                "類型": "ETF" if c.startswith("00") else "個股"}
        for v in views:
            rows[v].append(build_row(v, dict(base), p, p.iloc[-1], prev, t86))
    bar.empty()

    if not detail:
        st.error("所有代碼都抓不到資料。")
        return

    label = {c: f"{c} {rows[views[0]][i]['名稱']}" for i, c in enumerate(detail)}

    tabs = st.tabs(views)
    for tab, v in zip(tabs, views):
        with tab:
            st.subheader(f"{v}比較｜{day}")
            tbl = pd.DataFrame(rows[v])
            st.dataframe(tbl, use_container_width=True, hide_index=True)

            if v == "成交量":
                st.caption("成交量走勢(張)")
                st.line_chart(pd.DataFrame(
                    {label[c]: d.set_index("日期")["volume"] for c, d in detail.items()}))
            elif v == "技術面":
                st.caption("股價走勢(以共同起始日 = 100 標準化,方便比較漲跌幅)")
                closes = pd.DataFrame(
                    {label[c]: d.set_index("日期")["close"] for c, d in detail.items()}).dropna()
                if not closes.empty:
                    st.line_chart(closes / closes.iloc[0] * 100)
                st.caption("RSI14")
                st.line_chart(pd.DataFrame(
                    {label[c]: d.set_index("日期")["RSI14"] for c, d in detail.items()}))
            else:
                if t86.empty:
                    st.warning("當日三大法人資料尚未公布(通常收盤後約 16:00 後才有)。")
                cols = [c for c in ("外資(張)", "投信(張)", "自營商(張)") if c in tbl.columns]
                if cols:
                    st.caption("三大法人買賣超(張)")
                    chart = tbl.assign(標籤=tbl["代號"] + " " + tbl["名稱"]).set_index("標籤")[cols]
                    st.bar_chart(chart)

    # 兩檔以上 ETF 時,可看成分股重疊
    etfs = [c for c in detail if c.startswith("00")]
    if len(etfs) >= 2:
        with st.expander("ETF 成分股重疊"):
            if st.checkbox("計算重疊(需用 Selenium 抓取各 ETF 持股,較慢)", key="cmp_overlap"):
                merged = None
                for e in etfs:
                    try:
                        h = fetch_holdings(e)
                    except Exception as ex:
                        st.error(f"{e} 成分股抓取失敗:{ex}")
                        continue
                    if h.empty:
                        st.warning(f"{e} 找不到成分股。")
                        continue
                    h = h.rename(columns={"權重(%)": f"{e} 權重(%)"})
                    merged = h if merged is None else merged.merge(
                        h, on=["代號", "名稱"], how="outer")
                if merged is not None:
                    wcols = [c for c in merged.columns if c.endswith("權重(%)")]
                    both = merged.dropna(subset=wcols).sort_values(wcols[0], ascending=False)
                    st.caption(f"{len(both)} 檔為所選 ETF 共同持有")
                    st.dataframe(both.reset_index(drop=True),
                                 use_container_width=True, hide_index=True)


st.set_page_config(page_title="台股代碼查詢", page_icon="📈", layout="wide")


# ---------------------------------------------------------------- 手機 / 平板
def lan_url() -> str:
    """取得這台電腦在區域網路的位址(不會真的送出封包)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return f"http://{ip}:{st.get_option('server.port') or 8501}"


def default_url() -> str:
    """用「目前瀏覽器連進來的網址」當 QR 內容:雲端是 xxx.streamlit.app,
    區網是 http://192.168.x.x:8501;從 localhost 開的才退回區網 IP。"""
    try:
        h = st.context.headers
        host, proto = h.get("Host", ""), h.get("X-Forwarded-Proto", "http")
    except Exception:
        host, proto = "", "http"
    if host and not host.startswith(("localhost", "127.0.0.1")):
        return f"{proto}://{host}"
    return lan_url()


def qr_png(text: str) -> bytes:
    buf = BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=8, border=2)
    return buf.getvalue()


with st.sidebar:
    st.header("📱 手機 / 平板開啟")
    share_url = st.text_input("連線網址", value=default_url(),
                              help="預設為目前瀏覽器連進來的網址(雲端部署即公開網址)。若用 Tailscale、ngrok,請改成對應網址。")
    if segno is None:
        st.warning("尚未安裝 QR code 套件,請執行:pip install segno")
    elif share_url.strip():
        st.image(qr_png(share_url.strip()), width=240)
        st.caption("用手機或平板的相機掃描即可開啟。")
        if "127.0.0.1" in share_url or "localhost" in share_url:
            st.warning("目前是本機位址,手機打不開。請確認電腦已連上 Wi-Fi,或手動填入電腦的區網 IP。")
    st.caption("連線成功")

# 觸控裝置友善:按鈕/頁籤加大、窄螢幕縮小邊距、表格可橫向捲動
st.markdown("""
<style>
button, [data-baseweb="tab"], [data-baseweb="select"] { min-height: 44px; }
[data-baseweb="tab"] { font-size: 1.05rem; padding: 0 1rem; }
[data-testid="stDataFrame"] { overflow-x: auto; }
@media (max-width: 640px) {
  .block-container { padding: 1rem 0.6rem 3rem 0.6rem; }
  h1 { font-size: 1.6rem !important; }
  h2, h3 { font-size: 1.2rem !important; }
}
</style>
""", unsafe_allow_html=True)

st.title("台股代碼查詢")
st.caption("可輸入 ETF(如 0050、00878、00981A)或任何上市櫃個股(如 2330、6488)")

compare = st.checkbox("比較多個代碼(2 個以上)", key="compare_mode")

# 步驟 1:輸入代碼,按下查詢才開始抓資料
with st.form("search"):
    c_in, c_btn = st.columns([4, 1], vertical_alignment="bottom")
    code_in = c_in.text_input(
        "股市代碼(多個請用逗號或空白分隔,如 0050 0056 2330)" if compare else "股市代碼",
        value=st.session_state.get("raw", "0050"))
    if c_btn.form_submit_button("查詢", use_container_width=True):
        st.session_state["raw"] = code_in

codes = parse_codes(st.session_state.get("raw", "0050")) or ["0050"]

if compare:
    if len(codes) < 2:
        st.info("比較模式請輸入至少兩個代碼,例如:0050 0056 2330")
    else:
        run_compare(codes)
    st.stop()

code = codes[0]
if len(codes) > 1:
    st.caption(f"目前是單一查詢,只使用第一個代碼 {code};勾選「比較多個代碼」可同時比較。")

# 00 開頭視為 ETF,其餘視為個股
is_etf = code.startswith("00")
if is_etf:
    with st.spinner(f"Selenium 從元大投信抓取 {code} 成分股…"):
        try:
            holdings = fetch_holdings(code)
        except Exception as e:
            holdings = pd.DataFrame()
            st.error(f"抓取失敗:{e}")
    if holdings.empty:
        st.error("找不到成分股。此來源只支援元大投信發行的 ETF(如 0050、0056),"
             "其他投信的 ETF 請改用其他來源;若確定是元大 ETF,可能是頁面結構改版,需調整 fetch_holdings。")
        st.stop()
    st.subheader(f"{code} 成分股(共 {len(holdings)} 檔台股)")
    st.caption("前五大:" + "、".join(
        f"{r['名稱']} ({r['代號']}):{r['權重(%)']:.2f}%"
        for _, r in holdings.head(5).iterrows())
        + f"｜台股權重合計 {holdings['權重(%)'].sum():.2f}%(其餘可能為現金、期貨或海外標的)")
    show = holdings.assign(排名=range(1, len(holdings) + 1))[["排名", "代號", "名稱", "權重(%)"]]
    st.dataframe(
        show, use_container_width=True, hide_index=True,
        height=min(38 + 35 * len(show), 2000),  # 依筆數撐高,不用捲動就能看到全部
        column_config={"權重(%)": st.column_config.ProgressColumn(
            "權重(%)", format="%.2f%%", min_value=0.0,
            max_value=float(holdings["權重(%)"].max()))})
else:
    # 個股:成分股就是它自己,權重 100%
    name_map = fetch_name_map()
    if name_map and code not in name_map:
        st.error(f"找不到代號 {code}(不在上市櫃公司清單中)。請確認後重新輸入。")
        st.stop()
    holdings = pd.DataFrame([{"代號": code, "名稱": name_map.get(code, code), "權重(%)": 100.0}])
    st.subheader(f"個股 {code} {holdings.loc[0, '名稱']}")
    st.dataframe(holdings, use_container_width=True, hide_index=True)

# 步驟 2、3:選日期、自行選擇面向
c1, c2, c3 = st.columns([1, 2, 1])
picked = c1.date_input("選擇日期", value=date.today(), max_value=date.today())
views = c2.multiselect("選擇要看的面向(可複選)", ASPECTS, default=[])
if is_etf and len(holdings) > 3:
    top_n = c3.slider("分析前 N 大持股(越多越慢)", 3, len(holdings), min(10, len(holdings)))
else:
    top_n = len(holdings)

if not views:
    st.info("請在上方選擇要看的面向:成交量、技術面、籌碼面。")
    st.stop()

day = latest_trading_day(picked)
if day != picked:
    st.info(f"{picked} 無交易資料,改用最近交易日 {day}")

t86 = fetch_t86(day)

targets = holdings.head(top_n)
rows = {v: [] for v in views}
detail = {}
bar = st.progress(0.0, text="抓取個股資料…")
for i, (_, h) in enumerate(targets.iterrows()):
    bar.progress((i + 1) / len(targets), text=f"{h['名稱']} ({h['代號']})")
    px = fetch_prices(h["代號"], day)
    for v in views:
        base = {"代號": h["代號"], "名稱": h["名稱"], "權重(%)": h["權重(%)"]}
        if px.empty:
            rows[v].append(base)
            continue
        p = add_indicators(px)
        detail[h["代號"]] = p
        prev = p.iloc[-2] if len(p) > 1 else p.iloc[-1]
        rows[v].append(build_row(v, base, p, p.iloc[-1], prev, t86))
bar.empty()

if not is_etf and not detail:
    st.error(f"抓不到 {code} 在 {day} 前的價量資料(可能是剛上市櫃、已下市,或資料來源暫時無回應)。")
    st.stop()

# 每個選到的面向各一個分頁
tabs = st.tabs(views)
for tab, v in zip(tabs, views):
    with tab:
        st.subheader(f"{v}｜{day}")
        st.dataframe(pd.DataFrame(rows[v]), use_container_width=True, hide_index=True)
        if v == "籌碼面" and t86.empty:
            st.warning("當日三大法人資料尚未公布(通常收盤後約 16:00 後才有)。")
        if detail:
            sel = st.selectbox(
                "查看個股走勢", list(detail.keys()), key=f"sel_{v}",
                format_func=lambda c: f"{c} {holdings.set_index('代號').loc[c, '名稱']}")
            d = detail[sel].set_index("日期")
            if v == "成交量":
                st.bar_chart(d["volume"])
            elif v == "技術面":
                st.line_chart(d[["close", "MA5", "MA20", "MA60"]])
                st.line_chart(d[["K", "D", "RSI14"]])
            else:
                st.line_chart(d["close"])

st.caption("僅供研究參考,不構成投資建議。資料以證交所、櫃買中心與元大投信公開資訊為準。")
