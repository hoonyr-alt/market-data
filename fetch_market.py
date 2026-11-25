#!/usr/bin/env python3
"""공개 시장 지표 스냅숏 수집기 — GitHub Actions가 미국 장 마감 직후 돌려 이 저장소 latest.json에 올린다(공개 저장소: 인증 없이 읽힘).

왜: 예약 실행 환경(Cowork 원격)은 야후·네이버 같은 시세 사이트를 막고, PlayMCP 미국 도구도 자주 안 붙는다.
    Actions 러너는 인터넷이 열려 있으니 여기서 모아 두고, 예약 작업은 git clone으로 읽기만 한다(MCP·토큰 0).

사용: python3 fetch_market.py [출력.json [이전.json]]   (기본: 표준출력. 이전.json이 있으면 못 구한 키만 이전 값으로)
  - 표준 라이브러리만 쓴다. 항목마다 출처를 차례로 시도하고, 실패해도 멈추지 않는다(failed에 남김).
  - 값마다 실제 기준일(asof)을 적는다. 신선도 판단(직전 거래일인지)은 읽는 쪽(pm_fetch.py)이 한다.
  - 공개 지표만 다룬다. 보유 종목·수량 같은 개인 정보는 넣지 않는다(저장소 보안 규칙).

출력 키는 portfolio-monitor pm_calc.py 입력 키와 같다:
  ndx_closes, spx_closes, vix, vix_prev, us10y, dxy, cnn_fg, putcall, cape, fwd_pe, fed,
  prices{MU,kospi,kospi200}, kospi_prev, kospi_change_pct, credit_trn, credit_trn_prev,
  forced_liq_eok, foreign_net_buy
"""
import datetime as dt
import html
import json
import re
import sys
import io
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

UA = {"User-Agent": "Mozilla/5.0"}
ET, KST = ZoneInfo("America/New_York"), ZoneInfo("Asia/Seoul")
NOW = dt.datetime.now(dt.timezone.utc)
FG_KO = {"extreme fear": "극단적 공포", "fear": "공포", "neutral": "중립", "greed": "탐욕", "extreme greed": "극단적 탐욕"}
MONTHS = "January February March April May June July August September October November December".split()

out = {"generated_at": NOW.astimezone(KST).strftime("%Y-%m-%d %H:%M KST"), "values": {"prices": {}},
       "asof": {}, "source": {}, "failed": []}
V, A, S = out["values"], out["asof"], out["source"]


# ---------- 공통 ----------
def get(url, raw=False, data=None, headers=None, binary=False):
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
    for i in range(2):                      # 일시 끊김은 한 번 더
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read()
            break
        except urllib.error.HTTPError:
            raise
        except Exception:
            if i: raise
    if binary:
        return body
    return body.decode("utf8", "ignore") if raw else json.loads(body)


def text(page):
    page = re.sub(r"<script[^>]*>.*?</script>|<style[^>]*>.*?</style>", " ", page, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))


def num(x):
    return float(str(x).replace(",", "").replace("+", ""))


def first(name, *fns):
    """출처를 차례로 시도. 성공한 함수 이름을 source에 남긴다."""
    errs = []
    for fn in fns:
        try:
            r = fn()
            if r is not None:
                S[name] = fn.__name__
                return r
            errs.append(f"{fn.__name__}: 값 없음")
        except Exception as e:
            errs.append(f"{fn.__name__}: {e.__class__.__name__}")
    out["failed"].append(f"{name} ({'; '.join(errs)})")
    return None


def us_closed_today():
    n = NOW.astimezone(ET)
    return n.date() if n.time() >= dt.time(16, 15) else n.date() - dt.timedelta(days=1)


US_LAST = us_closed_today()   # 이 날짜 이후 봉(장중 미완성 봉)은 버린다


# ---------- 미국 시세 ----------
def yahoo(symbol, rng, host="query1"):
    r = get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range={rng}&interval=1d")
    r = r["chart"]["result"][0]
    tz = ZoneInfo(r["meta"].get("exchangeTimezoneName") or "America/New_York")
    days = [dt.datetime.fromtimestamp(t, tz).date() for t in r["timestamp"]]
    s = [(d, c) for d, c in zip(days, r["indicators"]["quote"][0]["close"]) if c is not None and d <= US_LAST]
    return s or None


def yahoo_any(symbol, rng):
    def y1(): return yahoo(symbol, rng, "query1")
    def y2(): return yahoo(symbol, rng, "query2")
    y1.__name__, y2.__name__ = "yahoo1", "yahoo2"
    return y1, y2


def nasdaq_hist(sym):
    def f():
        frm = (US_LAST - dt.timedelta(days=90)).isoformat()
        r = get(f"https://api.nasdaq.com/api/quote/{sym}/historical?assetclass=index&limit=70&fromdate={frm}&todate={US_LAST}")
        rows = r["data"]["tradesTable"]["rows"]
        return sorted((dt.datetime.strptime(x["date"], "%m/%d/%Y").date(), num(x["close"])) for x in rows) or None
    f.__name__ = "nasdaq.com"
    return f


def treasury_10y():
    y = US_LAST.year
    csv = get("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
              f"{y}/all?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv", raw=True)
    lines = csv.strip().splitlines()
    i = [c.strip('"') for c in lines[0].split(",")].index("10 Yr")
    rows = [(dt.datetime.strptime(l.split(",")[0], "%m/%d/%Y").date(), float(l.split(",")[i])) for l in lines[1:]]
    return sorted(rows)


treasury_10y.__name__ = "treasury.gov"


def fred(series):
    def f():
        csv = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}", raw=True).strip().splitlines()[1:]
        s = [(dt.date.fromisoformat(l.split(",")[0]), float(l.split(",")[1])) for l in csv if l.split(",")[1] not in (".", "")]
        return [x for x in s if x[0] <= US_LAST][-60:] or None
    f.__name__ = "fred"
    return f


def series(name, fns, keep):
    s = first(name, *fns)
    if s:
        A[name] = s[-1][0].isoformat()
        return [round(c, 2) for _, c in s][-keep:]
    return None


v = series("ndx_closes", (*yahoo_any("^NDX", "1mo"), nasdaq_hist("NDX")), 5)
if v: V["ndx_closes"] = v
v = series("spx_closes", (*yahoo_any("^GSPC", "3mo"), fred("SP500")), 45)
if v: V["spx_closes"] = v
v = series("vix", (*yahoo_any("^VIX", "1mo"), fred("VIXCLS")), 2)
if v:
    V["vix"] = v[-1]
    if len(v) > 1: V["vix_prev"] = v[-2]
v = series("us10y", (*yahoo_any("^TNX", "1mo"), treasury_10y, fred("DGS10")), 1)
if v: V["us10y"] = v[-1]
v = series("dxy", yahoo_any("DX-Y.NYB", "1mo"), 1)
if v: V["dxy"] = v[-1]
v = series("MU", yahoo_any("MU", "1mo"), 1)
if v:
    V["prices"]["MU"] = v[-1]; A["prices.MU"] = A.pop("MU")


# ---------- CNN 공포탐욕·풋콜 ----------
def fg_aistock():
    t = text(get("https://aistockselection.com/en/fear-greed-today", raw=True))
    upd = dt.datetime.strptime(re.search(r"Last updated: ([A-Z][a-z]+ \d{1,2}, \d{4})", t).group(1), "%B %d, %Y").date()
    r = {"cnn_fg": {"now": int(re.search(r"(\d{1,3}) /100", t).group(1)),
                    "prev": int(re.search(r"Previous close (\d{1,3})", t).group(1)),
                    "week": int(re.search(r"1 week ago (\d{1,3})", t).group(1))}, "_date": upd}
    m = re.search(r"Put/call options \d{1,3} (Extreme fear|Fear|Neutral|Greed|Extreme greed)", t, flags=re.I)
    if m: r["putcall"] = FG_KO[m.group(1).lower()]
    return r


def fg_meter():
    page = get("https://feargreedmeter.com/", raw=True)
    mod = dt.date.fromisoformat(re.search(r'"dateModified":"(\d{4}-\d{2}-\d{2})', page).group(1))
    t = text(page)
    g = lambda k: int(re.search(k + r" (?:Extreme Fear|Fear|Neutral|Greed|Extreme Greed) (\d{1,3})", t).group(1))
    return {"cnn_fg": {"now": g("Now"), "prev": g("Yesterday"), "week": g("1 Week Ago")}, "_date": mod}


fg_aistock.__name__, fg_meter.__name__ = "aistockselection", "feargreedmeter"
r = first("cnn_fg", fg_aistock, fg_meter)
if r:
    d = r.pop("_date").isoformat()
    V.update(r); A["cnn_fg"] = d
    if "putcall" in r: A["putcall"] = d; S["putcall"] = S["cnn_fg"]


# ---------- 밸류에이션 ----------
def cape_tt():
    t = text(get("https://www.thetrading.tools/shiller-cape", raw=True))
    m = re.search(r"Ratio Today: (\d+(?:\.\d+)?)× \(([A-Z][a-z]+) (\d{4})\)", t)
    A["cape"] = f"{m.group(3)}-{MONTHS.index(m.group(2)) + 1:02d}"
    return float(m.group(1))


def cape_multpl():
    t = text(get("https://www.multpl.com/shiller-pe", raw=True))
    m = re.search(r"Current Shiller PE Ratio:? (\d+\.\d+)", t)
    A["cape"] = US_LAST.strftime("%Y-%m")
    return float(m.group(1))


cape_tt.__name__, cape_multpl.__name__ = "thetrading.tools", "multpl"
r = first("cape", cape_tt, cape_multpl)
if r is not None: V["cape"] = r


def factset_pdf():
    """FactSet 주간 Earnings Insight PDF(매주 금요일)에서 'P/E ratio for the S&P 500 is N'. pypdf 필요."""
    import pypdf
    d = US_LAST
    for _ in range(5):
        d -= dt.timedelta(days=(d.weekday() - 4) % 7)          # 직전 금요일(당일 포함)
        url = ("https://advantage.factset.com/hubfs/Website/Resources%20Section/Research%20Desk/"
               f"Earnings%20Insight/EarningsInsight_{d.strftime('%m%d%y')}.pdf")
        try:
            pdf = get(url, binary=True)
        except urllib.error.HTTPError:
            d -= dt.timedelta(days=1); continue
        t = re.sub(r"\s+", " ", " ".join((p.extract_text() or "") for p in pypdf.PdfReader(io.BytesIO(pdf)).pages[:4]))
        m = re.search(r"P/E ratio for the S&P 500 is (\d+\.\d)", t)
        if m:
            A["fwd_pe"] = d.isoformat()
            return float(m.group(1))
        d -= dt.timedelta(days=1)
    return None


def factset_pe():
    """(예비) FactSet Earnings Insight 블로그 글에서 'forward 12-month P/E ratio for the S&P 500 was/is N'."""
    idx = get("https://insight.factset.com/topic/earnings", raw=True)
    links = list(dict.fromkeys(re.findall(r'href="(https://insight\.factset\.com/[a-z0-9/._-]+)"', idx)))
    links = [l for l in links if "/topic/" not in l and not l.rstrip("/").endswith("factset.com")]
    for l in links[:10]:
        t = text(get(l, raw=True))
        m = re.search(r"(?:On ([A-Z][a-z]+ \d{1,2}), )?the forward 12-month P/E ratio for the S&P 500 (?:is|was) (\d+\.\d)", t)
        if m:
            when = m.group(1)
            if when:
                d = dt.datetime.strptime(f"{when} {US_LAST.year}", "%B %d %Y").date()
                if d > US_LAST: d = d.replace(year=d.year - 1)
                A["fwd_pe"] = d.isoformat()
            return float(m.group(2))
    return None


factset_pdf.__name__, factset_pe.__name__ = "factset_pdf", "factset_blog"
r = first("fwd_pe", factset_pdf, factset_pe)
if r is not None: V["fwd_pe"] = r


# ---------- 미국 기준금리·다음 FOMC·선물 내재 변화 ----------
def fed_info():
    e = get("https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json")["refRates"][0]
    f = {"target": f"{e['targetRateFrom']:.2f}~{e['targetRateTo']:.2f}", "effr": e["percentRate"]}
    A["fed"] = e["effectiveDate"]
    t = text(get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", raw=True))
    today = NOW.astimezone(ET).date()
    meets = []
    for y in (today.year, today.year + 1):
        seg = t.split(f"{y} FOMC Meetings", 1)[1].split("FOMC Meetings", 1)[0] if f"{y} FOMC Meetings" in t else ""
        for m in re.finditer(r"\b(" + "|".join(MONTHS) + r")(?:/[A-Z][a-z]+)? (\d{1,2})-(\d{1,2})", seg):
            mo = MONTHS.index(m.group(1)) + 1
            end = int(m.group(3))
            try:
                d_end = dt.date(y, mo, end) if end >= int(m.group(2)) else dt.date(y + (mo == 12), mo % 12 + 1, end)
            except ValueError:
                continue
            meets.append(d_end)
    nxt = min((d for d in meets if d >= today), default=None)
    if nxt:
        f["next_fomc"] = nxt.isoformat()
        # 결정 다음 달 30일 연방기금 선물(ZQ): 내재 금리 = 100 - 가격. 현재 EFFR과의 차이를 bp로(추정치)
        mo = nxt.month % 12 + 1; yy = nxt.year + (nxt.month == 12)
        sym = f"ZQ{'FGHJKMNQUVXZ'[mo - 1]}{yy % 100:02d}.CBT"
        try:
            px = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d")["chart"]["result"][0]["meta"]["regularMarketPrice"]
            f["implied_bp"] = round((100 - px - e["percentRate"]) * 100)
        except Exception:
            pass
    return f


fed_info.__name__ = "newyorkfed+federalreserve"
r = first("fed", fed_info)
if r: V["fed"] = r


# ---------- 국내 지수·수급·신용 ----------
def naver_daily(code):
    """네이버 일별 지수(최신→과거). 장 마감(15:40 KST) 전이면 오늘 줄은 버린다(개장 전엔 '오늘·변동 0'으로 나옴)."""
    rows = get(f"https://m.stock.naver.com/api/index/{code}/price?pageSize=5&page=1")
    now = NOW.astimezone(KST)
    if now.time() < dt.time(15, 40):
        rows = [r for r in rows if r["localTradedAt"][:10] < now.date().isoformat()]
    return [(r["localTradedAt"][:10], num(r["closePrice"]), num(r["fluctuationsRatio"])) for r in rows]


try:
    (day, close, pct), (_, prev, _) = naver_daily("KOSPI")[:2]
    V["prices"]["kospi"] = close; V["kospi_prev"] = prev; V["kospi_change_pct"] = pct
    A["prices.kospi"] = A["kospi_prev"] = A["kospi_change_pct"] = day; S["kospi"] = "naver"
    t = get(f"https://m.stock.naver.com/api/index/KOSPI/trend?bizdate={day.replace('-', '')}")
    V["foreign_net_buy"] = num(t["foreignValue"]); A["foreign_net_buy"] = day   # 억원, +순매수
except Exception as e:
    out["failed"].append(f"kospi (naver: {e.__class__.__name__})")
try:
    day, close, _ = naver_daily("KPI200")[0]
    V["prices"]["kospi200"] = close; A["prices.kospi200"] = day
except Exception as e:
    out["failed"].append(f"kospi200 (naver: {e.__class__.__name__})")


def freesis(obj):
    end = NOW.astimezone(KST).date()
    body = json.dumps({"dmSearch": {"tmpV40": "1000000", "tmpV41": "1", "tmpV1": "D",
                                    "tmpV45": (end - dt.timedelta(days=20)).strftime("%Y%m%d"),
                                    "tmpV46": end.strftime("%Y%m%d"), "OBJ_NM": obj}}).encode()
    rows = get("https://freesis.kofia.or.kr/meta/getMetaDataList.do", data=body,
               headers={"Content-Type": "application/json"})["ds1"]
    return sorted(rows, key=lambda x: x["TMPV1"], reverse=True)


try:   # 금융투자협회 신용공여 잔고(백만원): TMPV2 = 신용거래융자 합계
    rows = freesis("STATSCU0100000070BO")
    V["credit_trn"] = round(rows[0]["TMPV2"] / 1e6, 2); V["credit_trn_prev"] = round(rows[1]["TMPV2"] / 1e6, 2)
    A["credit_trn"] = f"{rows[0]['TMPV1'][:4]}-{rows[0]['TMPV1'][4:6]}-{rows[0]['TMPV1'][6:]}"; S["credit_trn"] = "kofia"
except Exception as e:
    out["failed"].append(f"credit_trn (kofia: {e.__class__.__name__})")
try:   # 투자자예탁금·미수금·반대매매(백만원): TMPV6 = 위탁매매 미수금 대비 실제 반대매매금액
    rows = freesis("STATSCU0100000060BO")
    V["forced_liq_eok"] = round(rows[0]["TMPV6"] / 100)
    A["forced_liq_eok"] = f"{rows[0]['TMPV1'][:4]}-{rows[0]['TMPV1'][4:6]}-{rows[0]['TMPV1'][6:]}"; S["forced_liq_eok"] = "kofia"
except Exception as e:
    out["failed"].append(f"forced_liq_eok (kofia: {e.__class__.__name__})")

# 이전 스냅숏 병합(재실행 때 일시 실패로 좋은 값을 잃지 않게): 이번에 못 구한 키만 이전 값·날짜로 채운다
if len(sys.argv) > 2:
    try:
        old = json.load(open(sys.argv[2], encoding="utf8"))
        for k, val in old.get("values", {}).items():
            if k == "prices":
                for pk, pv in val.items():
                    if pk not in V["prices"] and f"prices.{pk}" in old.get("asof", {}):
                        V["prices"][pk] = pv; A[f"prices.{pk}"] = old["asof"][f"prices.{pk}"]; S[pk] = "이전 실행"
            elif k not in V and k in old.get("asof", {}):
                V[k] = val; A[k] = old["asof"][k]; S[k] = "이전 실행"
                for kk in ("vix_prev", "credit_trn_prev", "kospi_prev"):
                    if kk.startswith(k.split("_")[0]) and kk in old["values"] and kk not in V:
                        V[kk] = old["values"][kk]
    except Exception:
        pass
if not V["prices"]:
    del V["prices"]
js = json.dumps(out, ensure_ascii=False, indent=1)
if len(sys.argv) > 1:
    open(sys.argv[1], "w", encoding="utf8").write(js + "\n")
else:
    print(js)
