"""What a scanner typed: one reader for every screen that takes a barcode or QR code.

A scanner is a keyboard, so a code arrives as text. It may be a plain EAN/UPC/GTIN, a shop or lab
code, a GS1 string (DataMatrix or QR, with or without parentheses), a GS1 Digital Link URL, or a
QR code with labelled text such as "Batch: A12". `parse` turns all of them into a `Scan`, and
`find_products` is the one way to go from a code to products. It keeps what identifies a pack:
the code, batch, dates and serial. Names and prices on a label are ignored on purpose: the
hospital's own catalogue decides those. No patient data passes through here.
"""

import calendar
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qsl, unquote, urlsplit

from django.db.models import Q
from django.utils import timezone

GS = "\x1d"
_GS_SPELLINGS = re.compile(r"<GS>|\{GS\}|\[GS\]|\N{SYMBOL FOR GROUP SEPARATOR}", re.IGNORECASE)
_SYMBOLOGY = re.compile(r"^\][A-Za-z]\d")  # a prefix some scanners add: ]d2 DataMatrix, ]Q3 QR
_GTIN_LENGTHS = (8, 12, 13, 14)

# GS1 application identifiers: how long each value is, or the longest it may be.
_FIXED = {
    "00": 18,
    "01": 14,
    "02": 14,
    "11": 6,
    "12": 6,
    "13": 6,
    "15": 6,
    "16": 6,
    "17": 6,
    "20": 2,
}
_FIXED |= {str(n): 6 for n in range(3100, 3700)}
_FIXED["7003"] = 10
_VARIABLE = {
    "10": 20,
    "21": 20,
    "22": 20,
    "30": 8,
    "37": 8,
    "90": 30,
    "240": 90,
    "241": 30,
    "400": 30,
}
_VARIABLE |= {str(n): 90 for n in range(91, 100)} | {str(n): 20 for n in range(710, 716)}
_LABELS = {
    "gtin": ("gtin", "gtin 14", "gtin 13", "ean", "ean 13", "upc", "barcode", "bar code"),
    "batch": ("batch", "batch no", "batch number", "lot", "lot no", "lot number", "b no"),
    "expiry": (
        "exp", "exp date", "expiry", "expiry date", "expiration", "expiration date", "expires",
        "use by", "best before",
    ),
    "made": (
        "mfg", "mfg date", "mfd", "manufactured", "manufacture date", "manufacturing date",
        "manufactured on",
    ),
    "serial": ("serial", "serial no", "serial number", "sn", "s/n", "sr no"),
    # Never read. Known only so that a value above stops where one of these starts.
    "ignored": (
        "name", "product", "product name", "brand", "brand name", "medicine", "drug",
        "manufacturer", "mfr", "mfd by", "mfg by", "manufactured by", "company",
        "reg", "reg no", "registration", "registration no", "drap", "drap reg no",
        "mrp", "price", "retail price",
    ),
}  # fmt: skip
_FIELD_FOR_LABEL = {
    re.sub(r"[\s/]", "", label): name for name, labels in _LABELS.items() for label in labels
}
_LABEL_WORDS = sorted((x for v in _LABELS.values() for x in v), key=len, reverse=True)
_LABEL_PATTERN = "|".join(re.escape(x).replace(r"\ ", r"[\s_.\-]*") for x in _LABEL_WORDS)
_LABELLED = re.compile(
    rf"(?:^|(?<=[\s;|,{{(\"'\x1d]))({_LABEL_PATTERN})[\"']?\s*\.?\s*[:=#]\s*", re.IGNORECASE
)
_IDENTIFYING = ("gtin", "batch", "expiry", "made", "serial")
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


@dataclass(frozen=True)
class Scan:
    raw: str
    code: str = ""  # what to look a product up by: the GTIN, or the plain code as scanned
    gtin: str = ""
    batch: str = ""
    expiry: date | None = None
    made: date | None = None
    serial: str = ""
    # No separators arrived, so where the batch ends is a guess: show it, never sell by it.
    batch_is_guess: bool = False

    @property
    def structured(self) -> bool:
        """True when the text was a coded label, so it must never be searched as words."""
        return bool(self.gtin or self.batch or self.expiry or self.made or self.serial)

    @property
    def expired(self) -> bool:
        return bool(self.expiry and self.expiry < timezone.localdate())

    @property
    def summary(self) -> str:
        parts = [f"GTIN {self.gtin}" if self.gtin else ""]
        parts.append(f"batch {self.batch}" if self.batch else "")
        parts.append(f"expiry {self.expiry:%m/%y}" if self.expiry else "")
        parts.append(f"serial {self.serial}" if self.serial else "")
        return ", ".join(p for p in parts if p)

    def as_dict(self) -> dict:
        """For the screens' scripts. Expiry goes in the MM/YY the entry grid takes."""
        return {
            "code": self.code,
            "gtin": self.gtin,
            "batch": self.batch,
            "expiry": self.expiry.isoformat() if self.expiry else "",
            "expiry_short": f"{self.expiry:%m/%y}" if self.expiry else "",
            "expired": self.expired,
            "made": self.made.isoformat() if self.made else "",
            "serial": self.serial,
            "batch_is_guess": self.batch_is_guess,
            "summary": self.summary,
        }


def gtin_is_valid(value: str) -> bool:
    if not value.isdigit() or len(value) not in _GTIN_LENGTHS:
        return False
    total = sum(int(d) * (1 if i % 2 else 3) for i, d in enumerate(reversed(value[:-1])))
    return (10 - total % 10) % 10 == int(value[-1])


def _gtin_shaped(value: str) -> bool:
    return value.isdigit() and len(value) in _GTIN_LENGTHS


def code_variants(code: str) -> list[str]:
    """The same GTIN written at every length: a pack's EAN-13 is its GTIN-14 with a leading zero."""
    code = code.strip()
    if not code:
        return []
    if not _gtin_shaped(code):
        return [code]
    core = code.lstrip("0")
    return [core.zfill(n) for n in _GTIN_LENGTHS if len(core) <= n]


def code_key(text: str) -> str:
    """One spelling per product code, to spot the same code twice."""
    code = parse(text).code
    return min(code_variants(code), key=len) if _gtin_shaped(code) else code.lower()


def _expiry_from_gs1(text: str) -> date | None:
    """YYMMDD. A day of 00 means the end of that month."""
    if not re.fullmatch(r"\d{6}", text):
        return None
    year, month, day = 2000 + int(text[:2]), int(text[2:4]), int(text[4:])
    if not 1 <= month <= 12:
        return None
    last = calendar.monthrange(year, month)[1]
    if day > last:
        return None
    return date(year, month, day or last)


def _expiry_field(text: str) -> date | None:
    """Expiry as coded. Some Pakistani packs write MMYYYY (122028) where GS1 says YYMMDD."""
    if found := _expiry_from_gs1(text):
        return found
    if re.fullmatch(r"(0[1-9]|1[0-2])20\d{2}", text):
        year, month = int(text[2:]), int(text[:2])
        return date(year, month, calendar.monthrange(year, month)[1])
    return _date_from_text(text)  # "03/28" or "MAR-28" where digits belong


def _date_from_text(text: str) -> date | None:
    """Dates as printed on a label: 03/27, 03/2027, MAR-27, 2027-03-31, 31/03/2027, 270331."""
    text = text.strip()
    if found := _expiry_from_gs1(text):
        return found
    year = month = day = None
    if m := re.fullmatch(r"(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})", text):
        year, month, day = (int(x) for x in m.groups())
    elif m := re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})", text):
        day, month, year = (int(x) for x in m.groups())
    elif m := re.fullmatch(r"(\d{1,2})[/.\-](\d{2}|\d{4})", text):
        month, year = (int(x) for x in m.groups())
    elif m := re.fullmatch(r"([A-Za-z]{3})[a-z]*[\s/.\-]*(\d{2}|\d{4})", text):
        month, year = _MONTHS.get(m[1].lower()), int(m[2])
    if not month or not 1 <= month <= 12:
        return None
    year = year + 2000 if year < 100 else year
    last = calendar.monthrange(year, month)[1]
    if day and day > last:
        return None
    return date(year, month, day or last)


def _next_identifier(text: str, at: int) -> str | None:
    for size in (4, 3, 2):
        ai = text[at : at + size]
        if ai in _FIXED or ai in _VARIABLE:
            return ai
    return None


def _split_unmarked(value: str, ai: str) -> tuple[str, str, bool]:
    """A variable value with no separator after it: find where the next element starts.
    A valid date is safe to split on. A serial or batch marker is only a guess."""
    if ai == "240":
        return value, "", False  # name and price text, usually last on Pakistani packs: never read
    other = {"10": "21", "21": "10"}.get(ai)
    cuts = [m.start() for m in re.finditer(r"240(?=[A-Za-z])", value) if m.start()]
    cuts += [
        m.start()
        for m in re.finditer(r"(?:11|15|17)(\d{6})", value)
        if m.start() and _expiry_from_gs1(m[1])
    ]
    if cuts:
        cut = min(cuts)
        unsure = bool(other and value.find(other, 1, cut) > 0)
        return value[:cut], value[cut:], unsure
    at = value.find(other, 1) if other else -1
    if at > 0:
        return value[:at], value[at:], True
    return value, "", False


def _elements(text: str) -> tuple[list[tuple[str, str]], bool] | None:
    """A GS1 element string as (identifier, value) pairs, and whether a split was guessed. Reading
    stops at anything unknown and keeps what came before. None when it does not start like one."""
    pairs, at, guessed = [], 0, False
    has_separators = GS in text
    while at < len(text):
        if text[at] == GS:
            at += 1
            continue
        ai = _next_identifier(text, at)
        if ai is None:
            break
        start = at + len(ai)
        if ai in _FIXED:
            value = text[start : start + _FIXED[ai]]
            if len(value) != _FIXED[ai] or not value.isdigit():
                break
            at = start + len(value)
        else:
            end = text.find(GS, start)
            value = text[start : end if end >= 0 else len(text)]
            at = start + len(value)
            if not has_separators:
                value, rest, unsure = _split_unmarked(value, ai)
                at -= len(rest)
                guessed = guessed or unsure
            value = value[: _VARIABLE[ai]]
        pairs.append((ai, value))
    return (pairs, guessed) if pairs else None


def _from_pairs(raw, pairs, *, guessed=False) -> Scan | None:
    found = dict(reversed(pairs))  # the first of a repeated identifier wins
    gtin = found.get("01") or found.get("02") or ""
    if not _gtin_shaped(gtin):
        return (
            None  # not a GS1 code after all. A wrong check digit is the label's: it still looks up
        )
    return Scan(
        raw=raw,
        code=gtin,
        gtin=gtin,
        batch=found.get("10", "").strip(),
        expiry=next(
            filter(None, map(_expiry_field, (found.get(a, "") for a in ("17", "15", "16")))),
            None,
        ),
        made=_expiry_field(found.get("11", "")),
        serial=found.get("21", "").strip(),
        batch_is_guess=guessed,
    )


def _from_url(raw: str) -> Scan | None:
    """GS1 Digital Link: https://any.host/01/<gtin>/10/<batch>?17=<yymmdd>. Other links only count
    when their query names what it holds (?gtin=...&batch=...)."""
    url = urlsplit(raw)
    parts = [unquote(p) for p in url.path.split("/") if p]
    pairs = [
        (parts[i], parts[i + 1])
        for i in range(len(parts) - 1)
        if parts[i] in ("01", "02", "10", "21", "17", "11", "15", "22", "240", "241", "400")
    ]
    query = parse_qsl(url.query)
    pairs += [(k, v) for k, v in query if k.isdigit()]
    if scan := _from_pairs(raw, pairs):
        return scan
    return _from_labels(raw, "\n".join(f"{k}: {v}" for k, v in query if not k.isdigit()))


def _from_labels(raw: str, text: str) -> Scan | None:
    """Text QR codes: "GTIN: 0896...; Batch: A12; Exp: 03/27", on lines or as JSON."""
    hits = list(_LABELLED.finditer(text))
    values: dict[str, str] = {}
    for hit, following in zip(hits, [*hits[1:], None], strict=False):
        value = text[hit.end() : following.start() if following else len(text)]
        value = re.split(r"[\n;|\x1d]", value, maxsplit=1)[0].strip(" \t,\"'}])")
        key = _FIELD_FOR_LABEL[re.sub(r"[\s_.\-/]", "", hit[1].lower())]
        if value and key not in values:
            values[key] = value
    if not any(k in values for k in _IDENTIFYING):
        return None
    gtin = re.sub(r"\D", "", values.get("gtin", ""))
    return Scan(
        raw=raw,
        code=gtin if gtin_is_valid(gtin) else values.get("gtin", ""),
        gtin=gtin if gtin_is_valid(gtin) else "",
        batch=values.get("batch", ""),
        expiry=_date_from_text(values.get("expiry", "")),
        made=_date_from_text(values.get("made", "")),
        serial=values.get("serial", ""),
    )


def parse(raw: str) -> Scan:
    """Read whatever was scanned or typed. Anything not coded comes back as a plain code, so a
    strange label can never stop a screen."""
    try:
        return _read((raw or "").strip(" \t\r\n"))
    except (ValueError, IndexError, OverflowError):
        return Scan(raw=raw, code=raw.strip().replace(GS, ""))


def _read(raw: str) -> Scan:
    text = _GS_SPELLINGS.sub(GS, raw).replace("\xe8", GS)
    text = _SYMBOLOGY.sub("", text).strip(GS)
    if not text:
        return Scan(raw=raw)

    if gtin_is_valid(text):
        return Scan(raw=raw, code=text, gtin=text)
    if re.match(r"https?://", text, re.IGNORECASE):
        return _from_url(text) or Scan(raw=raw, code=text)
    if text.startswith("("):
        pairs = re.findall(r"\((\d{2,4})\)([^(]*)", text)
        pairs = [(ai, value.strip(GS + " ")) for ai, value in pairs]
        if scan := _from_pairs(raw, pairs):
            return scan
    if parsed := _elements(text):
        pairs, guessed = parsed
        if scan := _from_pairs(raw, pairs, guessed=guessed):
            return scan
    if scan := _from_labels(raw, text):
        return scan
    return Scan(raw=raw, code=text.replace(GS, ""))


def as_scan(value) -> Scan:
    return value if isinstance(value, Scan) else parse(value)


def find_products(products, value) -> list:
    """The products this code belongs to, from a scoped queryset: barcode first, then SKU."""
    scan = as_scan(value)
    if not scan.code:
        return []
    variants = code_variants(scan.code)
    exact = list(products.filter(barcode__in=variants))
    return (
        exact
        or list(products.filter(barcode__iexact=scan.code))
        or list(products.filter(sku__iexact=scan.code))
    )


def search_products(products, q: str, *, field: str = "search_text"):
    """A list screen's search box: the scanned product alone, else the words typed."""
    scan = parse(q)
    if hits := find_products(products, scan):
        return products.filter(pk__in=[p.pk for p in hits])
    if scan.structured:
        return products.none()
    condition = Q()
    for word in q.lower().split():
        condition &= Q(**{f"{field}__contains": word})
    return products.filter(condition)
