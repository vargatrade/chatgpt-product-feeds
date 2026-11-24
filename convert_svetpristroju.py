#!/usr/bin/env python3
"""CZ/CZK adapter for the existing shared Webareal converter.

This intentionally does not change convert_feed.py or the KupTeraz workflow.
The adapter runs in its own Python process for the SP workflow and reuses the
existing validation, GTIN handling, CSV atomics and catalog-shrinkage guard.
"""

from __future__ import annotations

import sys
from html import unescape
from decimal import Decimal
from urllib.parse import urlparse

import convert_feed as shared

SOURCE_URL = "https://www.svetpristroju.cz/fotky16797/xml/google_nakupy.xml"
SELLER_URL = "https://www.svetpristroju.cz/"
DOMAIN = "svetpristroju.cz"
CZECH_STOP_MARKERS = (
    "hlavní vlastnosti", "bezpečnost produktu", "bezpečnostní informace",
    "bezpečnostní upozornění", "důležité upozornění", "důležitá upozornění",
    "výrobce a eu odpovědná", "další informace", "obsah balení",
    "technické parametry", "technické údaje", "podobné zboží",
)


def czk_price(value: str) -> str:
    # No FX conversion: source prices are already the actual Czech prices.
    number, currency = value.strip().replace(",", ".").rsplit(None, 1)
    if currency != "CZK":
        raise ValueError(f"Unexpected source currency: {currency!r} (expected CZK)")
    amount = Decimal(number)
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
        raise ValueError("Non-positive, non-finite or invalid CZK precision")
    return f"{amount:.2f} CZK"


def belongs_to_sp(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in {
        DOMAIN, "www." + DOMAIN
    }


_original_convert = shared.convert
_original_description = shared.description


def description_sp(raw: str) -> str:
    """Keep short, factual CZ bullet lists when the KT intro heuristic returns empty."""
    result = _original_description(raw)
    if result:
        return result
    parser = shared.PlainText()
    parser.feed(unescape(raw or ""))
    lines = [shared.collapse(line) for line in "".join(parser.parts).splitlines()]
    fragments = []
    for line in lines:
        low = line.lower().strip().strip(":")
        if any(low.startswith(marker) for marker in shared.STOP_MARKERS):
            break
        if low in {"popis produktu", "podrobnější popis produktu", "specifikace"}:
            continue
        if line:
            fragments.append(line)
        if len(fragments) >= 12:
            break
    result = shared.collapse("; ".join(fragments))
    return result[:2200] if len(result) >= 25 else ""



def convert_sp(xml_bytes: bytes, min_products: int = 100):
    rows, report = _original_convert(xml_bytes, min_products=min_products)
    for row in rows:
        if not belongs_to_sp(row["url"]):
            raise RuntimeError(f"Product {row['item_id']} points outside SvetPristroju.cz")
        if not belongs_to_sp(row["image_url"]):
            raise RuntimeError(f"Image {row['item_id']} points outside SvetPristroju.cz")
        if not row["price"].endswith(" CZK"):
            raise RuntimeError(f"Invalid Czech price for product {row['item_id']}")
        if row["sale_price"] and not row["sale_price"].endswith(" CZK"):
            raise RuntimeError(f"Invalid Czech sale price for product {row['item_id']}")
        row["target_countries"] = '["CZ"]'
        row["store_country"] = "CZ"
        row["seller_url"] = SELLER_URL

    report["market"] = "CZ"
    report["currency"] = "CZK"
    return rows, report


def main():
    # Changes here are in-memory only, within this separate SP process.
    # Calling python convert_feed.py for KT is fully unchanged.
    shared.price = czk_price
    shared.description = description_sp
    shared.STOP_MARKERS = shared.STOP_MARKERS + CZECH_STOP_MARKERS
    shared.convert = convert_sp
    sys.argv = [
        sys.argv[0],
        "--source", SOURCE_URL,
        "--output", "feeds/svetpristroju-chatgpt.csv",
        "--report", "feeds/svetpristroju-audit.json",
        *sys.argv[1:],  # Optional overrides for local/offline validation.
    ]
    shared.main()


if __name__ == "__main__":
    main()
