"""Official TWSE/TPEx/MOPS adapters; latest financials are forward-only snapshots."""
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import json
import html
import math
import re
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .storage import digest, read, write
from .network import tls_context

TAIPEI = timezone(timedelta(hours=8))

# Confirmed exceptional closures absent from the annual holiday schedule.
# Keep evidence with the date; missing quotes alone never establish a closure.
KNOWN_EXTRA_CLOSURES = {
    "2026-07-10": "https://www.emega.com.tw/emegaTran/bulletin.do?id=20260709085742643431",
}


def extra_closures(root):
    return {**KNOWN_EXTRA_CLOSURES, **read(root / "extra_closures.json", {})}


def today():
    return datetime.now(TAIPEI).date()


def number(value):
    try:
        n = float(str(value).replace(",", "").strip())
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


class SourceError(RuntimeError):
    pass


class Client:
    def __init__(self, root):
        self.root = root
        self.audit = []

    def get(self, url, html=False):
        for attempt in range(3):
            try:
                with urlopen(Request(url, headers={"User-Agent": "TStockNews/0.1"}), timeout=20, context=tls_context()) as r:
                    raw = r.read()
                if html:
                    try:
                        text = raw.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        text = raw.decode("cp950", errors="strict")
                else:
                    text = raw.decode("utf-8-sig")
                value = text if html else json.loads(text)
                self.audit.append({"url": url, "fetched_at": datetime.now(TAIPEI).isoformat(),
                                   "hash": digest(value)})
                time.sleep(.3)  # modest rate, bounded retries; never bypass a blocked source
                return value
            except Exception as error:
                if attempt == 2:
                    raise SourceError(f"Official source failed: {url} ({type(error).__name__})") from error
                time.sleep(1 + attempt)

    def json(self, base, **params):
        return self.get(base + ("?" + urlencode(params) if params else ""))


def tables(value):
    return value.get("tables", [value])


def _signed_price_change(direction, amount):
    """Read an exchange-provided price change without inferring its sign."""
    if direction is None:
        # TPEx dailyQuotes exposes a single signed 漲跌 value.
        text = str(amount).strip() if amount is not None else ""
        text = text.translate(str.maketrans({"＋": "+", "－": "-", "−": "-"}))
        if text in {"", "--", "X", "x"}:
            return None
        parsed = number(text)
        if parsed is None:
            return None
        if parsed == 0 or text.startswith(("+", "-", "＋", "－")):
            return parsed
        return None

    parsed = number(amount)
    if parsed is None:
        return None
    # TWSE returns its sign cell as an HTML fragment such as
    # '<p style= color:red>+</p>' instead of plain text.
    marker = html.unescape(re.sub(r"<[^>]*>", "", str(direction))).strip()
    marker = marker.translate(str.maketrans({"＋": "+", "－": "-", "−": "-"})).upper()
    if marker in {"X", "--"}:
        return None
    if marker in {"+", "^", "＋"}:
        return abs(parsed)
    if marker in {"-", "V", "－", "−"}:
        return -abs(parsed)
    if marker == "" and parsed == 0:
        return 0.0
    return None


def _price_change_pct(close, change):
    # Official change is measured from that session's reference price, which
    # can differ from the prior close on ex-right/ex-dividend sessions.
    close_value = number(close)
    if close_value is None or change is None:
        return None
    reference_price = close_value - change
    if reference_price <= 0:
        return None
    return change / reference_price


def quote_rows(value, market, day):
    if str(value.get("date")) != day.replace("-", ""):
        raise SourceError(f"{market} quote date mismatch: {day}")
    out = []
    for table in tables(value):
        fields = table.get("fields", [])
        code = "證券代號" if market == "twse" else "代號"
        if code not in fields:
            continue
        for values in table.get("data", []):
            row = dict(zip(fields, values))
            maps = ("開盤價", "最高價", "最低價", "收盤價") if market == "twse" else ("開盤", "最高", "最低", "收盤")
            volume = number(row.get("成交股數"))
            if volume is None:
                raise SourceError("Missing volume field")
            if market == "twse":
                price_change = _signed_price_change(
                    row.get("漲跌(+/-)", row.get("漲跌註記")),
                    row.get("漲跌價差", row.get("漲跌價")),
                )
            else:
                price_change = _signed_price_change(None, row.get("漲跌"))
            close = number(row.get(maps[3]))
            out.append({"date": day, "market": market, "symbol": row[code].strip(),
                        "name": row.get("證券名稱", row.get("名稱", "")).strip(),
                        **dict(zip(("open", "high", "low", "close"), (number(row.get(k)) for k in maps))),
                        "price_change": price_change,
                        "price_change_pct": _price_change_pct(close, price_change),
                        "volume_shares": int(volume)})
    if not out:
        raise SourceError(f"{market} has no quotes for expected session {day}")
    return out


def inst_rows(value, market, day):
    response_day = str(value.get("date", ""))
    if not response_day and market == "twse":
        match = re.search(r"(\d+)年(\d+)月(\d+)日", value.get("title", ""))
        if match:
            y, m, d = (int(part) for part in match.groups())
            response_day = f"{y + 1911:04d}{m:02d}{d:02d}"
    if response_day != day.replace("-", ""):
        raise SourceError(f"{market} institutional date mismatch: {day}")
    out = []
    for table in tables(value):
        fields = table.get("fields") or []
        total = "三大法人買賣超股數" if market == "twse" else "三大法人買賣超股數合計"
        code = "證券代號" if market == "twse" else "代號"
        if total not in fields or code not in fields:
            continue
        ci, ti = fields.index(code), fields.index(total)
        for row in table.get("data", []):
            net = number(row[ti])
            if net is None:
                raise SourceError("Missing institutional total")
            out.append({"date": day, "market": market, "symbol": row[ci].strip(), "net_buy_shares": int(net)})
    if not out:
        raise SourceError(f"{market} has no institutional totals for {day}")
    return out


def holiday_days(client, year):
    value = client.json("https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule",
                        response="json", queryYear=year)
    if not value.get("data"):
        raise SourceError("Official calendar unavailable")
    result = set()
    for row in value["data"]:
        # Explicit trading-day markers are not holidays.
        if "交易日" in row[1] or "開始交易" in row[1]:
            continue
        result.add(date.fromisoformat(str(row[0]).replace("/", "-")).isoformat())
    return result


def dates_between(start, end):
    current = date.fromisoformat(start)
    while current <= date.fromisoformat(end):
        yield current
        current += timedelta(days=1)


def collect_day(client, day, allowed=None):
    compact, slash = day.replace("-", ""), day.replace("-", "/")
    prices, institutions = [], []
    for market, base, query, inst_base, inst_query in (
        ("twse", "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
         {"date": compact, "type": "ALLBUT0999", "response": "json"},
         "https://www.twse.com.tw/rwd/zh/fund/T86",
         {"date": compact, "selectType": "ALLBUT0999", "response": "json"}),
        ("tpex", "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes",
         {"date": slash, "response": "json"},
         "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade",
         {"date": slash, "type": "Daily", "sect": "EW", "response": "json"}),
    ):
        prices.extend(quote_rows(client.json(base, **query), market, day))
        institutions.extend(inst_rows(client.json(inst_base, **inst_query), market, day))
    if allowed is not None:
        prices = [r for r in prices if (r["market"], r["symbol"]) in allowed]
        institutions = [r for r in institutions if (r["market"], r["symbol"]) in allowed]
    return {"date": day, "prices": prices, "institutional": institutions,
            "source_audit": list(client.audit)}


class RevenueTable(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.cells, self.cell, self.in_cell = [], [], [], False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.cells = []
        elif tag in ("td", "th"):
            self.cell, self.in_cell = [], True

    def handle_data(self, text):
        if self.in_cell:
            self.cell.append(text)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.in_cell:
            self.cells.append("".join(self.cell).strip())
            self.in_cell = False
        elif tag == "tr" and self.cells:
            self.rows.append(self.cells)


def ordinary_universe(client):
    if hasattr(client, "_universe"):
        return client._universe
    universe = []
    for market, url, mode in (
        ("twse", "https://openapi.twse.com.tw/v1/opendata/t187ap03_L", "2"),
        ("tpex", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O", "4"),
    ):
        companies = client.get(url)
        if not isinstance(companies, list) or not companies:
            raise SourceError("Official company universe missing")
        isin = RevenueTable()
        isin.feed(client.get("https://isin.twse.com.tw/isin/C_public.jsp?strMode=" + mode, html=True))
        codes = {r[0].split()[0] for r in isin.rows if len(r) > 5 and r[5] == "ESVUFR"}
        if len(codes) < 100:
            raise SourceError("Official ordinary-share classification unavailable")
        for row in companies:
            code = str(row.get("公司代號", row.get("SecuritiesCompanyCode", ""))).strip()
            name = row.get("公司簡稱", row.get("CompanyAbbreviation", ""))
            if not code or not name:
                raise SourceError("Company schema changed")
            if code in codes:
                universe.append({"symbol": code, "name": name, "market": market})
    client._universe = universe
    return universe


def month_offset(day, offset):
    n = day.year * 12 + day.month - 1 + offset
    return n // 12, n % 12 + 1


def universe_and_financials(client, as_of):
    # Snapshot is available only from the day we first observed it, not its fiscal period.
    observed = today().isoformat()
    if as_of != observed:
        raise SourceError("Latest financial snapshots cannot be used to generate historical recommendations")
    universe, eps_map, revenue = ordinary_universe(client), {}, {}
    for market, prefix, company_endpoint, sector_prefix in (
        ("twse", "https://openapi.twse.com.tw/v1/opendata/", "t187ap03_L", "t187ap06_L_"),
        ("tpex", "https://www.tpex.org.tw/openapi/v1/", "mopsfin_t187ap03_O", "mopsfin_t187ap06_O_"),
    ):
        codes = {c["symbol"] for c in universe if c["market"] == market}
        for sector in ("ci", "basi", "bd", "fh", "ins", "mim"):
            rows = client.get(prefix + sector_prefix + sector)
            if not isinstance(rows, list):
                raise SourceError("Income-statement schema changed")
            for row in rows:
                symbol = str(row.get("公司代號", row.get("SecuritiesCompanyCode", ""))).strip()
                year, season = row.get("年度", row.get("Year")), row.get("季別", row.get("Season"))
                if symbol not in codes:
                    continue
                if not year or not season or "基本每股盈餘（元）" not in row:
                    raise SourceError("EPS schema changed")
                period = f"{int(year) + 1911}-Q{int(season)}"
                if int(season) not in (1, 2, 3, 4):
                    raise SourceError("Invalid fiscal quarter")
                existing = eps_map.get((market, symbol))
                if existing and existing["fiscal_period"] > period:
                    continue
                value = {"eps": number(row["基本每股盈餘（元）"]),
                                           "fiscal_period": period,
                                           "operating_margin": (number(row.get("營業利益（損失）")) / number(row.get("營業收入")))
                                           if number(row.get("營業收入")) and number(row.get("營業利益（損失）")) is not None else None}
                if existing and existing["fiscal_period"] == period and existing["eps"] != value["eps"]:
                    raise SourceError("Conflicting EPS for the same fiscal period")
                eps_map[(market, symbol)] = value
        # Four completed months allow late reporters to use their latest complete 3-month window.
        for offset in (-1, -2, -3, -4):
            year, month = month_offset(today(), offset)
            for category in (0, 1):
                url = f"https://mopsov.twse.com.tw/nas/t21/{'sii' if market == 'twse' else 'otc'}/t21sc03_{year - 1911}_{month}_{category}.html"
                # The immediately previous month may not be published yet. It is unavailable,
                # not zero; an older complete window can still be used transparently.
                try:
                    text = client.get(url, html=True)
                except SourceError:
                    if offset == -1:
                        continue
                    raise
                parser = RevenueTable()
                parser.feed(text)
                matched = 0
                for cells in parser.rows:
                    if len(cells) < 5 or cells[0] not in codes:
                        continue
                    current, previous = number(cells[2]), number(cells[4])
                    if current is None or previous is None:
                        continue
                    revenue[(market, cells[0], year, month)] = (current, previous)
                    matched += 1
                if not matched and offset != -1 and category == 0:
                    raise SourceError("Historical revenue table missing or schema changed")
    fundamentals = []
    for company in universe:
        key = (company["market"], company["symbol"])
        ratio, end_month = None, None
        for latest in (-1, -2):
            months = [month_offset(today(), latest - delta) for delta in range(3)]
            values = [revenue.get((*key, y, m)) for y, m in months]
            if all(v is not None for v in values):
                prior = sum(v[1] for v in values)
                ratio = sum(v[0] for v in values) / prior - 1 if prior > 0 else None
                end_month = f"{months[0][0]}-{months[0][1]:02d}"
                break
        fundamentals.append({**company, **eps_map.get(key, {"eps": None, "fiscal_period": ""}),
                             "revenue_yoy_3m": ratio, "revenue_end_month": end_month,
                             "available_date": observed, "operating_cash_flow": None,
                             "cash_flow_status": "SOURCE_NOT_IMPLEMENTED"})
    return {"universe": universe, "fundamentals": fundamentals, "observed_date": observed,
            "source_audit": list(client.audit)}


def sync(root, start, end, progress=None):
    client = Client(root)
    closures = extra_closures(root)  # ISO day -> evidence URL
    calendars = {year: holiday_days(client, year) for year in
                 range(date.fromisoformat(start).year, date.fromisoformat(end).year + 1)}
    allowed = {(r["market"], r["symbol"]) for r in ordinary_universe(client)}
    for path in (root / "recommendations").glob("*.json"):
        allowed.update((r["market"], r["symbol"]) for r in read(path).get("signals", []))
    for current in dates_between(start, end):
        day = current.isoformat()
        if current.weekday() >= 5 or day in calendars[current.year] or day in closures:
            continue
        path = root / "days" / (day + ".json.gz")
        if path.exists():
            continue
        client.audit = []
        daily = collect_day(client, day, allowed)
        write(path, daily)
        if progress:
            progress(day)
    return client
