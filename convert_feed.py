#!/usr/bin/env python3
"""Convert Webareal Google Shopping XML to OpenAI discovery CSV.

No external Python dependencies. Doesn't mutate the source feed or shop.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

SOURCE_URL = "https://www.kupteraz.sk/fotky3776/xml/google_nakupy.xml"
GOOGLE = "{http://base.google.com/ns/1.0}"
FIELDNAMES = [
    "item_id", "title", "description", "url", "brand", "seller_name",
    "image_url", "availability", "price", "sale_price", "target_countries",
    "store_country", "gtin", "mpn", "condition", "product_category",
    "seller_url", "is_eligible_search", "is_ads_eligible",
]
ALLOWED_STOCK = {"in_stock", "out_of_stock", "pre_order", "backorder", "unknown"}
ALLOWED_CONDITIONS = {"new", "used", "refurbished"}
STOP_MARKERS = (
    "hlavné vlastnosti", "bezpečnosť produktu", "dôležité upozornenia",
    "výrobca a eú zodpovedná", "ďalšie informácie", "obsah balenia",
    "technické parametre", "technické údaje", "gpsr", "podobný tovar",
)

class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"br", "p", "div", "li"}:
            self.parts.append("\n")


def collapse(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text).replace("\xa0", " ")).strip()


def description(raw: str) -> str:
    parser = PlainText()
    parser.feed(unescape(raw or ""))
    lines = [collapse(line) for line in "".join(parser.parts).splitlines()]
    fragments: list[str] = []
    for line in lines:
        if not line:
            continue
        low = line.lower().strip().strip(":")
        if any(low.startswith(stopper) for stopper in STOP_MARKERS):
            if fragments:
                break
            continue
        if low in {"popis produktu", "podrobnejší opis produktu", "výhody", "prečo si vybrať"}:
            continue
        if len(line) < 35 and not fragments:
            continue
        fragments.append(line)
        if len(" ".join(fragments)) > 1250 or len(fragments) >= 3:
            break
    cleaned = collapse(" ".join(fragments))
    if len(cleaned) > 2200:
        prefix = cleaned[:2200]
        stop = max(prefix.rfind(". "), prefix.rfind("! "), prefix.rfind("? "))
        cleaned = prefix[:stop + 1] if stop > 500 else prefix.rsplit(" ", 1)[0]
    return cleaned


def validate_gtin(value: str) -> bool:
    if len(value) not in {8, 12, 13, 14} or not value.isdigit():
        return False
    digits = [int(d) for d in value]
    check = digits.pop()
    weighted = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return (10 - weighted % 10) % 10 == check


def get(item: ET.Element, tag: str) -> str:
    child = item.find(tag)
    return (child.text or "").strip() if child is not None else ""


def price(value: str) -> str:
    value = value.strip().replace(",", ".")
    number, currency = value.rsplit(None, 1)
    if currency != "EUR":
        raise ValueError("unexpected currency " + currency)
    amount = Decimal(number)
    if amount <= 0 or amount.as_tuple().exponent < -2:
        raise ValueError("non-positive or invalid precision")
    return f"{amount:.2f} EUR"


def is_public_url(value: str) -> bool:
    url = urlparse(value)
    return url.scheme in ("http", "https") and bool(url.netloc)


def convert(xml_bytes: bytes, min_products: int = 100) -> tuple[list[dict[str, str]], dict]:
    if len(xml_bytes) < 4000:
        raise RuntimeError("Source XML suspiciously short; keeping previous catalog")
    root = ET.fromstring(xml_bytes)
    items = root.findall("./channel/item")
    if not items:
        raise RuntimeError("Source is not a Google Shopping RSS catalog")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    excluded: list[dict[str, str]] = []
    invalid_gtins: list[dict[str, str]] = []
    stats = Counter()
    for item in items:
        pid = get(item, GOOGLE + "id")
        title = collapse(get(item, "title"))
        brand = collapse(get(item, GOOGLE + "brand"))
        url = get(item, "link")
        image = get(item, GOOGLE + "image_link")
        desc = description(get(item, "description"))
        stock = get(item, GOOGLE + "availability")
        reasons = []
        if not pid or pid in seen: reasons.append("missing/duplicate ID")
        if not title: reasons.append("missing title")
        if not brand: reasons.append("missing brand")
        if not desc: reasons.append("missing meaningful description")
        if not is_public_url(url): reasons.append("invalid product URL")
        if not is_public_url(image): reasons.append("invalid image URL")
        if stock not in ALLOWED_STOCK: reasons.append("unsupported stock status")
        try:
            regular = price(get(item, GOOGLE + "price"))
        except (ValueError, InvalidOperation):
            regular = ""
            reasons.append("invalid regular price")
        if reasons:
            excluded.append({"id":pid,"title":title,"reasons":", ".join(reasons)})
            continue
        seen.add(pid)
        sale = ""
        raw_sale = get(item, GOOGLE + "sale_price")
        if raw_sale:
            try:
                candidate = price(raw_sale)
                if Decimal(candidate.split()[0]) < Decimal(regular.split()[0]):
                    sale = candidate
                    stats["sale_prices"] += 1
                else:
                    stats["unusable_sale_prices"] += 1
            except (ValueError, InvalidOperation):
                stats["unusable_sale_prices"] += 1
        gtin = get(item, GOOGLE + "gtin")
        if gtin and not validate_gtin(gtin):
            invalid_gtins.append({"id":pid,"gtin":gtin})
            gtin = ""
        condition = get(item, GOOGLE + "condition")
        if condition not in ALLOWED_CONDITIONS:
            condition = ""
        rows.append({
            "item_id": pid,
            "title": title[:150],
            "description": desc[:5000],
            "url": url,
            "brand": brand,
            "seller_name": "Varga Trade s.r.o.",
            "image_url": image,
            "availability": stock,
            "price": regular,
            "sale_price": sale,
            "target_countries": '["SK"]',
            "store_country": "SK",
            "gtin": gtin,
            "mpn": get(item, GOOGLE + "mpn"),
            "condition": condition,
            "product_category": collapse(get(item, GOOGLE + "product_type")).replace(" > ", " > "),
            "seller_url": "https://www.kupteraz.sk/",
            "is_eligible_search": "true",
            "is_ads_eligible": "false",
        })
        stats["stock_" + stock] += 1
    if len(rows) < min_products:
        raise RuntimeError(f"Only {len(rows)} valid rows (min {min_products}); keeping previous catalog")
    return rows, {
        "source_products": len(items),
        "output_products": len(rows),
        "excluded": excluded,
        "invalid_gtins_omitted": invalid_gtins,
        "stats": dict(stats),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=SOURCE_URL, help="HTTPS source URL or local XML path")
    ap.add_argument("--output", default="feeds/kupteraz-chatgpt.csv")
    ap.add_argument("--report", default="feeds/audit.json")
    args = ap.parse_args()
    if args.source.startswith("https://"):
        request = Request(args.source, headers={"User-Agent": "KupTeraz-ChatGPT-FeedConverter/1.0"})
        with urlopen(request, timeout=40) as response:
            xml_bytes = response.read(16 * 1024 * 1024 + 1)
        if len(xml_bytes) > 16 * 1024 * 1024:
            raise RuntimeError("XML exceeds the 16 MiB safety limit")
    elif args.source.startswith("http://"):
        raise RuntimeError("HTTP source is not allowed; use HTTPS")
    else:
        xml_bytes = Path(args.source).read_bytes()
    rows, report = convert(xml_bytes)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Fail closed on unexpectedly large catalog shrinkage to avoid overwriting a healthy feed.
    if output.exists():
        with output.open(encoding="utf-8-sig", newline="") as f:
            old_count = max(0, sum(1 for _ in csv.reader(f)) - 1)
        if old_count >= 100 and len(rows) < old_count * 0.70:
            raise RuntimeError(f"Unexpected catalog shrinkage ({old_count} -> {len(rows)}); old CSV preserved")
    temp = output.with_suffix(".csv.tmp")
    with temp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(output)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Exported {len(rows)} / {report['source_products']} products; invalid GTIN omitted: {len(report['invalid_gtins_omitted'])}; excluded: {len(report['excluded'])}")
    if report["excluded"]:
        print("Excluded:", ", ".join(x["id"] for x in report["excluded"]))

if __name__ == "__main__":
    main()
