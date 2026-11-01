"""The lists every hospital starts with, kept in defaults.json. Edit that file to change them.

`load_defaults` only adds what is missing, by name or code, so a hospital's changes stay as they
are. It runs when a hospital is created and, once per `version` in the file, after each migrate.
Raise `version` when adding entries, or existing hospitals will not get them.
"""

import json
import logging
from functools import lru_cache
from pathlib import Path

from django.apps import apps
from django.db import DatabaseError, transaction

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _data() -> dict:
    path = Path(__file__).with_name("defaults.json")
    return json.loads(path.read_text(encoding="utf-8"))


def rows(key: str) -> list:
    return _data()[key]


def suggestions(source: str) -> list[str]:
    """Starting suggestions for a remembered box. source: "app.Model.field"."""
    return _data()["suggestions"].get(source, [])


def _add(organization, label, entries, unique=("name",), numbered=False) -> None:
    model = apps.get_model(label)
    existing = model.objects.filter(organization=organization)
    for position, entry in enumerate(entries):
        row = {"name": entry} if isinstance(entry, str) else dict(entry)
        if any(existing.filter(**{f"{f}__iexact": row[f]}).exists() for f in unique):
            continue
        if numbered:
            row["sort_order"] = position
        model.objects.create(organization=organization, **row)


@transaction.atomic
def load_defaults(organization) -> None:
    _add(organization, "masterdata.Department", rows("departments"), unique=("name", "code"))
    _add(organization, "masterdata.Specialty", rows("specialties"))
    _add(organization, "masterdata.ServiceCategory", rows("service_categories"))
    _add(organization, "lab.LabTestCategory", rows("lab_test_categories"), numbered=True)
    _add(organization, "pharmacy.ProductCategory", rows("product_categories"))
    _add(organization, "finance.ExpenseCategory", rows("expense_categories"))
    _add(
        organization,
        "billing.PaymentMethod",
        rows("payment_methods"),
        unique=("code",),
        numbered=True,
    )
    mark_loaded(organization)


def mark_loaded(organization) -> None:
    from apps.organizations.models import preferences_for

    prefs = preferences_for(organization)
    prefs.lists_version = _data()["version"]
    prefs.save(update_fields=["lists_version", "updated_at"])


def load_for_all() -> None:
    from apps.organizations.models import Organization

    try:
        behind = list(
            Organization.objects.exclude(preferences__lists_version__gte=_data()["version"])
        )
        for organization in behind:
            load_defaults(organization)
    except DatabaseError:  # a partial `migrate <app>`: the next full migrate loads them
        logger.warning("Standard lists not loaded: the database is not fully migrated.")
