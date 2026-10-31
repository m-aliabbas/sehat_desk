"""One plain phrase for how a product is packed: "Strip of 10 tablets", "Box of 10 strips of 10
tablets", "Bottle". Staff think in these words; the model keeps the separate numbers."""

import re

PRESETS = [
    "Strip of 10 tablets",
    "Box of 10 strips of 10 tablets",
    "Strip of 14 tablets",
    "Strip of 10 capsules",
    "Box of 3 strips of 10 capsules",
    "Bottle",
    "Tube",
    "Inhaler",
    "Piece",
    "Box of 5 vials",
    "Box of 10 ampoules",
    "Box of 20 sachets",
]
HINT = 'Write it like "Strip of 10 tablets", "Box of 10 strips of 10 tablets" or "Bottle".'
_THREE = re.compile(r"^(\w+) of (\d+) (\w+) of (\d+) (\w+)$")
_TWO = re.compile(r"^(\w+) of (\d+) (\w+)$")


def _one(word: str) -> str:
    word = word.lower()
    if word.endswith("xes"):
        return word[:-2]
    return word[:-1] if word.endswith("s") and not word.endswith("ss") else word


def parse_packing(text: str) -> dict:
    """The model fields for a phrase. Raises ValueError with what to type instead."""
    text = " ".join(text.split())
    if match := _THREE.match(text.lower()):
        box, packs, pack, size, unit = match.groups()
        numbers = {"packs_per_box": int(packs), "pack_size": int(size)}
        names = {"box_unit": _one(box), "pack_unit": _one(pack), "unit": _one(unit)}
    elif match := _TWO.match(text.lower()):
        pack, size, unit = match.groups()
        numbers = {"packs_per_box": 1, "pack_size": int(size)}
        names = {"box_unit": "box", "pack_unit": _one(pack), "unit": _one(unit)}
    elif re.fullmatch(r"[A-Za-z]+", text):
        unit = _one(text)
        numbers = {"packs_per_box": 1, "pack_size": 1}
        names = {"box_unit": "box", "pack_unit": unit, "unit": unit}
    else:
        raise ValueError(HINT)
    if min(numbers.values()) < 1:
        raise ValueError(HINT)
    return {**names, **numbers}


def _many(word: str, count: int) -> str:
    if count == 1:
        return word
    return word + ("es" if word.endswith(("s", "x", "ch", "sh")) else "s")


def packing_text(product) -> str:
    unit = product.unit.lower()
    if not product.has_packs:
        return unit.capitalize()
    text = f"{product.pack_unit.lower()} of {product.pack_size} {_many(unit, product.pack_size)}"
    if product.has_boxes:
        pack = _many(product.pack_unit.lower(), product.packs_per_box)
        text = (
            f"{product.box_unit.lower()} of {product.packs_per_box} {pack} "
            f"of {product.pack_size} {_many(unit, product.pack_size)}"
        )
    return text.capitalize()
