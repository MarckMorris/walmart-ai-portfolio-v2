"""Product catalogue normalisation.

Supplier feeds arrive with the same product described five different ways:
"Coca-Cola 2 LTR", "coca cola 2l", "COCA COLA 2000ML". Downstream matching,
pricing and replenishment all break on that. This module reduces a raw record
to a canonical form and reports what it could not resolve, rather than guessing
silently.

No machine learning. Deterministic rules, because a catalogue pipeline that
cannot explain why it changed a value is one nobody will run in production.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

# Canonical unit -> (aliases, factor to the canonical base unit)
VOLUME_UNITS: dict[str, tuple[tuple[str, ...], Decimal]] = {
    "ml": (("ml", "millilitre", "milliliter", "mls"), Decimal("1")),
    "l": (("l", "lt", "ltr", "ltrs", "litre", "liter", "litres", "liters"), Decimal("1000")),
    "fl_oz": (("floz", "fl oz", "fluid ounce", "fluid ounces"), Decimal("29.5735")),
    "gal": (("gal", "gallon", "gallons"), Decimal("3785.41")),
}

WEIGHT_UNITS: dict[str, tuple[tuple[str, ...], Decimal]] = {
    "g": (("g", "gr", "gram", "grams", "gramme", "grammes"), Decimal("1")),
    "kg": (("kg", "kgs", "kilo", "kilos", "kilogram", "kilograms"), Decimal("1000")),
    "mg": (("mg", "milligram", "milligrams"), Decimal("0.001")),
    "oz": (("oz", "ounce", "ounces"), Decimal("28.3495")),
    "lb": (("lb", "lbs", "pound", "pounds"), Decimal("453.592")),
}

# Longest aliases first, so "fl oz" is matched before "oz".
_VOLUME_LOOKUP = {
    alias: (canonical, factor)
    for canonical, (aliases, factor) in VOLUME_UNITS.items()
    for alias in aliases
}
_WEIGHT_LOOKUP = {
    alias: (canonical, factor)
    for canonical, (aliases, factor) in WEIGHT_UNITS.items()
    for alias in aliases
}
_ALL_ALIASES = sorted(
    set(_VOLUME_LOOKUP) | set(_WEIGHT_LOOKUP), key=len, reverse=True
)

_SIZE_PATTERN = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>" + "|".join(re.escape(a) for a in _ALL_ALIASES) + r")\b",
    re.IGNORECASE,
)

_MULTIPACK_PATTERN = re.compile(
    r"(?:(?P<count>\d+)\s*(?:x|pack|pk|ct|count)\b|\bpack\s*of\s*(?P<count2>\d+))",
    re.IGNORECASE,
)

# Hyphens and slashes become spaces so "Coca-Cola" and "Coca Cola" collide.
_SEPARATORS = re.compile(r"[-/_]+")
_NOISE = re.compile(r"[^\w\s&']+")
_WHITESPACE = re.compile(r"\s+")

# Words that carry no identity and only defeat exact matching.
STOP_WORDS = frozenset(
    {"the", "a", "an", "of", "and", "new", "brand", "item", "product", "assorted"}
)


@dataclass
class NormalisedProduct:
    """A canonical product record plus an audit trail of what changed."""

    raw_name: str
    name: str
    brand: str | None = None
    size_value: Decimal | None = None
    size_unit: str | None = None
    size_dimension: str | None = None  # "volume" or "weight"
    pack_count: int = 1
    gtin: str | None = None
    match_key: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_name": self.raw_name,
            "name": self.name,
            "brand": self.brand,
            "size_value": float(self.size_value) if self.size_value is not None else None,
            "size_unit": self.size_unit,
            "size_dimension": self.size_dimension,
            "pack_count": self.pack_count,
            "gtin": self.gtin,
            "match_key": self.match_key,
            "warnings": list(self.warnings),
        }


def strip_accents(text: str) -> str:
    """Fold accents so "Nestle" with an acute e and without it collide."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def check_digit(digits: str) -> int:
    """GS1 mod-10 check digit over the payload (without the final digit).

    Weights alternate 3, 1, 3, 1 starting from the rightmost payload digit,
    which is correct for GTIN-8, UPC-12, EAN-13 and GTIN-14 alike.
    """
    total = 0
    for position, char in enumerate(reversed(digits)):
        weight = 3 if position % 2 == 0 else 1
        total += int(char) * weight
    return (10 - (total % 10)) % 10


def normalise_gtin(raw: str | None) -> tuple[str | None, str | None]:
    """Return (gtin14, warning).

    UPC-12, EAN-13 and GTIN-14 are the same number at different widths, so they
    are zero-padded to 14 and the check digit is verified. A barcode that fails
    its own checksum is almost always a transcription error, and silently
    accepting it is how two different products end up merged.
    """
    if raw is None:
        return None, None

    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None, "gtin contained no digits"
    if len(digits) not in (8, 12, 13, 14):
        return None, f"gtin has {len(digits)} digits, expected 8, 12, 13 or 14"

    if check_digit(digits[:-1]) != int(digits[-1]):
        return None, "gtin failed its check digit"

    return digits.zfill(14), None


def parse_size(text: str) -> tuple[Decimal | None, str | None, str | None]:
    """Extract a size and convert it to the canonical unit for its dimension.

    Returns (value_in_canonical_unit, canonical_unit, dimension).
    """
    match = _SIZE_PATTERN.search(text)
    if not match:
        return None, None, None

    raw_value = match.group("value").replace(",", ".")
    try:
        value = Decimal(raw_value)
    except InvalidOperation:
        return None, None, None

    alias = match.group("unit").lower()

    if alias in _VOLUME_LOOKUP:
        _, factor = _VOLUME_LOOKUP[alias]
        return (value * factor).normalize(), "ml", "volume"

    _, factor = _WEIGHT_LOOKUP[alias]
    return (value * factor).normalize(), "g", "weight"


def parse_pack_count(text: str) -> int:
    """Multipacks: "6 x 330ml", "pack of 12", "24ct"."""
    match = _MULTIPACK_PATTERN.search(text)
    if not match:
        return 1
    count = match.group("count") or match.group("count2")
    parsed = int(count)
    # A "pack of 1" is not a multipack, and a four-digit count is a year or a
    # size that leaked into the pattern.
    return parsed if 1 < parsed < 1000 else 1


def clean_name(text: str) -> str:
    """Lowercase, de-accent, drop punctuation and stop words, collapse space."""
    folded = strip_accents(str(text)).lower()
    folded = _SEPARATORS.sub(" ", folded)
    folded = _NOISE.sub(" ", folded)
    tokens = [t for t in _WHITESPACE.split(folded) if t and t not in STOP_WORDS]
    return " ".join(tokens)


def format_size(value: Decimal) -> str:
    """Plain decimal text. Decimal.normalize() emits 2E+3 for 2000, which is
    correct arithmetic and useless as a key."""
    quantised = value.quantize(Decimal("0.001")).normalize()
    text = format(quantised, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def build_match_key(name: str, brand: str | None, size_value, size_unit, pack_count: int) -> str:
    """A deterministic key for exact-match deduplication.

    Two records that mean the same product produce the same key regardless of
    how the supplier spelled them.
    """
    parts = [brand or "", name]
    if size_value is not None:
        parts.append(f"{format_size(size_value)}{size_unit}")
    if pack_count > 1:
        parts.append(f"x{pack_count}")
    return "|".join(p for p in parts if p)


def normalise(record: dict[str, Any]) -> NormalisedProduct:
    """Reduce one raw supplier record to canonical form.

    Recognised input keys: ``name`` (or ``title``/``description``), ``brand``,
    ``gtin`` (or ``upc``/``ean``). Unknown keys are ignored rather than being an
    error, because supplier feeds always carry extra columns.
    """
    raw_name = str(
        record.get("name") or record.get("title") or record.get("description") or ""
    ).strip()

    product = NormalisedProduct(raw_name=raw_name, name="")

    if not raw_name:
        product.warnings.append("record has no name")

    size_value, size_unit, dimension = parse_size(raw_name)
    pack_count = parse_pack_count(raw_name)

    brand_raw = record.get("brand")
    brand = clean_name(brand_raw) if brand_raw else None
    if brand == "":
        brand = None

    # The size and pack count become their own fields, so leaving them in the
    # name would stop "2 LTR" and "2000ML" from ever producing the same key.
    name_without_size = _SIZE_PATTERN.sub(" ", raw_name)
    name_without_size = _MULTIPACK_PATTERN.sub(" ", name_without_size)
    name = clean_name(name_without_size)

    # Suppliers routinely repeat the brand in the name field.
    if brand:
        if name == brand:
            name = ""
        elif name.startswith(brand + " "):
            name = name[len(brand) + 1 :]

    gtin, gtin_warning = normalise_gtin(
        record.get("gtin") or record.get("upc") or record.get("ean")
    )
    if gtin_warning:
        product.warnings.append(gtin_warning)

    if size_value is None and raw_name:
        product.warnings.append("no size found in name")

    product.name = name
    product.brand = brand
    product.size_value = size_value
    product.size_unit = size_unit
    product.size_dimension = dimension
    product.pack_count = pack_count
    product.gtin = gtin
    product.match_key = build_match_key(name, brand, size_value, size_unit, pack_count)

    return product


def normalise_batch(records: list[dict[str, Any]]) -> list[NormalisedProduct]:
    return [normalise(r) for r in records]


def group_duplicates(products: list[NormalisedProduct]) -> dict[str, list[int]]:
    """Positions of records that share a match key, for records that repeat."""
    groups: dict[str, list[int]] = {}
    for index, product in enumerate(products):
        groups.setdefault(product.match_key, []).append(index)
    return {key: idx for key, idx in groups.items() if len(idx) > 1 and key}
