"""Read the fixed official CNY tariff page; never infer a changed table layout."""

import re
from decimal import Decimal
from html.parser import HTMLParser

import httpx

SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"


class PricingFormatError(ValueError):
    pass


class Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self.table = self.row = self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.table = []
        elif tag == "tr":
            self.row = []
        elif tag in ("th", "td"):
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            if self.row is not None:
                self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.table is not None:
                self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def parse_prices(html):
    parser = Tables()
    parser.feed(html)
    candidates = [t for t in parser.tables if t and t[0] and t[0][0] == "模型"]
    if len(candidates) != 1:
        raise PricingFormatError("model table changed")
    table = candidates[0]
    models = []
    for cell in table[0][1:]:
        match = re.fullmatch(r"(deepseek-[a-z0-9-]+)(?:\s*\(\d+\))?", cell)
        if not match:
            raise PricingFormatError("model identifier changed")
        models.append(match[1])
    if not models or len(models) != len(set(models)):
        raise PricingFormatError("invalid model columns")
    bands = {"peak": {}, "off_peak": {}}
    category = None
    for row in table[1:]:
        label = " ".join(row).lower().replace(" ", "")
        if "缓存未命中" in label:
            category = "input_miss"
        elif "缓存命中" in label:
            category = "input_hit"
        elif "tokens输出" in label:
            category = "output"
        band = "off_peak" if "空闲时段" in row else "peak" if "高峰时段" in row else None
        if band is None:
            continue
        if not category or category in bands[band] or len(row) < len(models) + 1:
            raise PricingFormatError("price rows changed")
        values = []
        for cell in row[-len(models) :]:
            if not re.fullmatch(r"\d+(?:\.\d{1,6})?元", cell):
                raise PricingFormatError("CNY price format changed")
            micro_cny = int(Decimal(cell[:-1]) * 1_000_000)
            if not 0 <= micro_cny <= 10**15:
                raise PricingFormatError("price out of bounds")
            values.append(micro_cny)
        bands[band][category] = values
    if any(set(band) != {"input_miss", "input_hit", "output"} for band in bands.values()):
        raise PricingFormatError("incomplete tariffs")
    result = []
    for i, model in enumerate(models):
        item = {
            "model": model,
            **{band: {k: v[i] for k, v in rates.items()} for band, rates in bands.items()},
        }
        for band in (item["peak"], item["off_peak"]):
            if (
                not 0 <= band["input_hit"] <= band["input_miss"]
                or min(band["input_miss"], band["output"]) <= 0
            ):
                raise PricingFormatError("invalid tariff")
        if any(item["off_peak"][k] > item["peak"][k] for k in item["peak"]):
            raise PricingFormatError("unexpected tariff order")
        result.append(item)
    return result


async def fetch_prices():
    # No user-controlled URL, credentials, redirects, or unbounded response buffer.
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
        async with client.stream("GET", SOURCE) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 1_000_000:
                    raise PricingFormatError("official page too large")
    return parse_prices(body.decode("utf-8"))
