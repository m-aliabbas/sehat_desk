"""Load test on a throwaway database (Phase 28).

    SEHATDESK_DATA_DIR=/tmp/sd-load python manage.py migrate
    SEHATDESK_DATA_DIR=/tmp/sd-load python manage.py loadtest --patients 20000

Builds a hospital with many patients and a stocked pharmacy, then times the hot paths through
the real views: reception patient search, POS checkout, and the admin dashboard. Refuses to
run on a database that already has patients, so it can never touch real data.
"""

import random
import statistics
import time
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

FIRST = [
    "Ayesha",
    "Ahmed",
    "Fatima",
    "Bilal",
    "Sana",
    "Usman",
    "Hira",
    "Zain",
    "Maryam",
    "Omar",
    "Nadia",
    "Imran",
    "Rabia",
    "Kamran",
    "Sadia",
    "Tariq",
    "Amna",
    "Faisal",
    "Iqra",
    "Hamza",
]
LAST = [
    "Khan",
    "Raza",
    "Noor",
    "Hussain",
    "Javed",
    "Tariq",
    "Aslam",
    "Ali",
    "Iqbal",
    "Farooq",
    "Sheikh",
    "Butt",
    "Malik",
    "Qureshi",
    "Chaudhry",
    "Abbasi",
    "Siddiqui",
    "Mirza",
]


class Command(BaseCommand):
    help = "Seed a throwaway database and time patient search, POS checkout and the dashboard."

    def add_arguments(self, parser):
        parser.add_argument("--patients", type=int, default=20000)
        parser.add_argument("--rounds", type=int, default=30)

    def handle(self, *args, patients, rounds, **options):
        from apps.patients.models import Patient

        if Patient.objects.exists():
            raise CommandError("This database has patients. Run loadtest on an empty data folder.")
        random.seed(7)
        admin, org, branch = self._hospital()
        self._patients(org, branch, admin, patients)
        product = self._pharmacy(org, branch, admin)
        client = Client(HTTP_HOST="localhost")
        client.force_login(admin)

        results = {
            "Patient search (name)": self._time(
                client, rounds, lambda: (reverse("patients:list"), {"q": random.choice(FIRST)})
            ),
            "Patient search (phone)": self._time(
                client,
                rounds,
                lambda: (reverse("patients:list"), {"q": f"0300{random.randint(0, patients):07d}"}),
            ),
            "Global search suggestions": self._time(
                client, rounds, lambda: (reverse("search:suggest"), {"q": random.choice(LAST)})
            ),
            "Admin dashboard": self._time(client, rounds, lambda: (reverse("core:dashboard"), {})),
            "POS checkout (2 lines)": self._checkout(admin, org, branch, product, rounds),
        }
        self.stdout.write(f"\n{patients} patients, {rounds} rounds each (milliseconds)\n")
        self.stdout.write(f"{'Operation':32} {'median':>8} {'p95':>8} {'max':>8}")
        for name, samples in results.items():
            p95 = sorted(samples)[max(0, int(len(samples) * 0.95) - 1)]
            self.stdout.write(
                f"{name:32} {statistics.median(samples):8.1f} {p95:8.1f} {max(samples):8.1f}"
            )

    # --- data ---------------------------------------------------------------------------

    def _hospital(self):
        from apps.accounts.services import create_organization_with_admin

        result = create_organization_with_admin(
            org_name="Load Test Hospital",
            branch_name="Main",
            branch_code="MAIN",
            city="Lahore",
            admin_data={
                "username": "loadadmin",
                "password": "Load-test-pass-2026!",
                "first_name": "Load",
            },
        )
        org, branch, admin = result
        return admin, org, branch

    def _patients(self, org, branch, admin, n):
        from apps.core.models import Sequence
        from apps.patients.models import Patient

        started = time.perf_counter()
        batch = []
        for i in range(n):
            name = f"{random.choice(FIRST)} {random.choice(LAST)}"
            batch.append(
                Patient(
                    organization=org,
                    registered_branch=branch,
                    created_by=admin,
                    mrn=f"{org.patient_id_prefix}-{i + 1:06d}",
                    full_name=name,
                    name_search=name.lower(),
                    gender=random.choice(["female", "male"]),
                    phone=f"0300{i:07d}",
                )  # fmt: skip
            )
            if len(batch) == 2000:
                Patient.objects.bulk_create(batch)
                batch = []
        Patient.objects.bulk_create(batch)
        Sequence.objects.update_or_create(
            organization=org, key="patient", defaults={"last_value": n}
        )
        self.stdout.write(f"Seeded {n} patients in {time.perf_counter() - started:.1f}s")

    def _pharmacy(self, org, branch, admin):
        from apps.accounts.services import effective_permissions
        from apps.billing.services import ensure_payment_methods
        from apps.pharmacy import stock
        from apps.pharmacy.models import Product

        ensure_payment_methods(org)
        product = Product.objects.create(
            organization=org, sku="P-1", brand_name="Panadol", strength="500 mg",
            generic_name="Paracetamol", sale_price=Decimal("3"),
        )  # fmt: skip
        req = _Request(admin, org, branch, effective_permissions(admin, branch))
        stock.receive(
            request=req, product=product, batch_number="LOAD-1",
            expiry_date=timezone.localdate() + timedelta(days=365), quantity=100000,
            purchase_cost=Decimal("1"),
        )  # fmt: skip
        return product

    # --- timing ---------------------------------------------------------------------------

    def _time(self, client, rounds, make):
        url, params = make()
        client.get(url, params)  # warm up
        samples = []
        for _ in range(rounds):
            url, params = make()
            started = time.perf_counter()
            response = client.get(url, params)
            samples.append((time.perf_counter() - started) * 1000)
            if response.status_code != 200:
                raise CommandError(f"{url} returned {response.status_code}")
        return samples

    def _checkout(self, admin, org, branch, product, rounds):
        from apps.accounts.services import effective_permissions
        from apps.billing.models import PaymentMethod
        from apps.pharmacy import pos

        req = _Request(admin, org, branch, effective_permissions(admin, branch))
        cash = PaymentMethod.objects.get(organization=org, code="cash")
        samples = []
        for _ in range(rounds):
            started = time.perf_counter()
            with transaction.atomic():
                cart = pos.open_cart(req)
                pos.attach_patient(cart, request=req, customer_name="Walk-in")
                pos.add_product(cart, request=req, product=product, quantity=10)
                pos.checkout(cart, request=req, payments=[{"method": cash, "amount": "30"}])
            samples.append((time.perf_counter() - started) * 1000)
        return samples


class _Request:
    """The parts of a request the POS and stock services read."""

    def __init__(self, user, org, branch, perms):
        self.user, self.organization, self.branch, self.sd_perms = user, org, branch, perms
        self.META = {"REMOTE_ADDR": "127.0.0.1"}
        self.headers = {}
        self.session = {}
        self.path = "/loadtest/"
        self.method = "POST"
