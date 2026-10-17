"""Transaction helpers for money and stock writes (CLAUDE.md: financial and stock writes must
be transactional).

    from apps.core.db import critical_write, locked

    @critical_write
    def receive_payment(invoice, ...):
        invoice = locked(invoice)          # re-read with a row lock
        ...                                # every write here commits or rolls back together
        after_commit(lambda: notify(...))  # side effects only once the money is saved

On PostgreSQL `locked()` takes a row lock (SELECT ... FOR UPDATE). On SQLite the connection
uses BEGIN IMMEDIATE (config/settings.py), so the whole transaction already holds the write
lock and `locked()` is a plain re-read.
"""

import functools

from django.db import transaction


def critical_write(func):
    """Run the function in one transaction. Nested calls join the outer transaction, so a
    checkout that issues an invoice and moves stock commits once, or not at all."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with transaction.atomic():
            return func(*args, **kwargs)

    return wrapper


def locked(instance_or_model, pk=None):
    """Fresh copy of a row, locked until the surrounding transaction ends.

    locked(invoice) or locked(Invoice, invoice_id). Must be called inside a transaction."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("locked() must be called inside critical_write / transaction.atomic")
    if pk is None:
        model, pk = type(instance_or_model), instance_or_model.pk
    else:
        model = instance_or_model
    return model._default_manager.select_for_update(of=("self",)).get(pk=pk)


def after_commit(func):
    """Run func once the current transaction commits (never if it rolls back)."""
    transaction.on_commit(func)
