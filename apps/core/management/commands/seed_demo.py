"""Demo hospital for training, sales demos and staging (Phase 31).

    SEHATDESK_DATA_DIR=/tmp/sd-demo python manage.py migrate
    SEHATDESK_DATA_DIR=/tmp/sd-demo python manage.py seed_demo

Builds one hospital with two branches, staff for every role, a lab catalogue, a stocked pharmacy
and IPD beds, then plays the last few weeks of hospital work through the real services:
registration, booking, tokens, vitals, consultations, prescriptions, lab orders and results,
billing, pharmacy dispensing and admissions. Each step runs with the clock set to its demo
time, so dashboards and reports show history, not one burst of records "now".

Refuses to run where another hospital already exists unless --alongside is given (development
only), so it never mixes demo patients into a customer's records. Logins go to
<data folder>/DEMO_LOGIN.txt, never to the console.
"""

import random
import secrets
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.apps import apps as django_apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test import RequestFactory
from django.utils import timezone

from apps.core import demo_data as data

HISTORY_DAYS = 14  # working days of OPD history before today


class _Clock:
    """Stands in for timezone.now() while seeding. Each call moves on a millisecond, so records
    made in one step keep their order."""

    def __init__(self):
        self.real_now = timezone.now()
        self.today = timezone.localdate(self.real_now)
        self._at = self.real_now

    def set(self, day: date, hh: int, mm: int = 0):
        at = timezone.make_aware(datetime.combine(day, time(hh, mm)))
        if day == self.today:  # today's work never runs ahead of the real time
            start = timezone.make_aware(datetime.combine(day, time(0, 5)))
            at = max(start, min(at, self.real_now - timedelta(minutes=1)))
        self._at = at

    def plus(self, minutes: int):
        self._at += timedelta(minutes=minutes)

    def now(self):
        self._at += timedelta(milliseconds=1)
        return self._at


@contextmanager
def _clock_patched(clock):
    """Point timezone.now (and model fields defaulting to it) at the demo clock."""
    original = timezone.now
    fields = [
        f
        for model in django_apps.get_models()
        for f in model._meta.concrete_fields
        if f.default is original
    ]
    for f in fields:
        f.default = clock.now
        f.__dict__.pop("_get_default", None)  # Django caches the default callable
    try:
        with mock.patch.object(timezone, "now", clock.now):
            yield
    finally:
        for f in fields:
            f.default = original
            f.__dict__.pop("_get_default", None)


class Command(BaseCommand):
    help = "Load a demo hospital with staff, patients and a few weeks of activity."

    def add_arguments(self, parser):
        parser.add_argument(
            "--alongside",
            action="store_true",
            help="Add the demo hospital next to existing hospitals (development databases only).",
        )
        parser.add_argument("--password", help="Password for every demo login (default: random).")

    def handle(self, *args, alongside, password, **options):
        from apps.organizations.models import Organization

        if Organization.objects.filter(name=data.HOSPITAL["name"]).exists():
            raise CommandError("The demo hospital is already loaded.")
        if Organization.objects.exists() and not alongside:
            raise CommandError(
                "This database already has a hospital. Load demo data into an empty data folder "
                "(SEHATDESK_DATA_DIR), or pass --alongside on a development database."
            )
        self.password = password or f"Demo-{secrets.token_urlsafe(9)}"
        self.rng = random.Random(31)
        self.clock = _Clock()
        self.today = self.clock.today
        self.rf = RequestFactory()
        self._perms = {}
        with _clock_patched(self.clock), transaction.atomic():
            self._build()
        self._write_logins()
        self._report()

    # --- helpers -------------------------------------------------------------------------------

    def req(self, user, branch):
        """A request carrying what the services read: user, organization, branch, permissions."""
        from apps.accounts.services import effective_permissions

        key = (user.pk, branch.pk)
        if key not in self._perms:
            self._perms[key] = effective_permissions(user, branch)
        request = self.rf.post("/", HTTP_USER_AGENT="seed_demo")
        request.user, request.organization, request.branch = user, self.org, branch
        request.sd_perms = self._perms[key]
        return request

    def at(self, day, hh, mm=0):
        self.clock.set(day, hh, mm)

    def working_days_back(self, n):
        """The n most recent days before today, skipping Sundays (newest first)."""
        days, day = [], self.today
        while len(days) < n:
            day -= timedelta(days=1)
            if day.weekday() != 6:
                days.append(day)
        return days

    def has_clinic(self, doctor, branch, day):
        from apps.appointments.services import schedule_for

        return schedule_for(doctor, branch, day) is not None

    def clinic_day(self, doctor, branch, start_idx):
        """The first past working day at or before past_days[start_idx] with this clinic."""
        for day in self.past_days[start_idx:]:
            if self.has_clinic(doctor, branch, day):
                return day
        raise CommandError(f"No clinic day for {doctor} in {branch}.")

    def next_clinic_day(self, doctor, branch, after_days):
        day = self.today + timedelta(days=after_days)
        while not self.has_clinic(doctor, branch, day):
            day += timedelta(days=1)
        return day

    def money(self, value):
        return Decimal(value).quantize(Decimal("0.01"))

    def method(self, code):
        return self.methods[code]

    def reference(self, code):
        return "" if code == "cash" else f"{code[:2].upper()}{self.rng.randint(10**7, 10**8 - 1)}"

    # --- build ---------------------------------------------------------------------------------

    def _build(self):
        self.past_days = self.working_days_back(HISTORY_DAYS + 6)
        setup_day = self.today - timedelta(days=45)
        self.at(setup_day, 9)
        self._hospital()
        self._staff()
        self._clinical_masters()
        self._lab_catalogue()
        self._pharmacy_catalogue()
        self._wards()
        self._stock()
        self._patients()
        self._opd_history()
        self._desk_lab_orders()
        self._walk_in_sales()
        self._stock_corrections()
        self._admissions()
        self._today()
        self._future_appointments()

    def _hospital(self):
        from apps.accounts.services import create_organization_with_admin
        from apps.billing.models import PaymentMethod
        from apps.billing.services import ensure_payment_methods
        from apps.organizations.models import Branch

        main = next(b for b in data.BRANCHES if b[0] == "main")
        staff = data.STAFF[0]
        self.org, main_branch, self.admin = create_organization_with_admin(
            org_name=data.HOSPITAL["name"],
            branch_name=main[1],
            branch_code=main[2],
            city=main[3],
            admin_data={
                "username": f"demo.{staff[0]}",
                "password": self.password,
                "first_name": staff[1],
                "last_name": staff[2],
                "designation": staff[5],
            },
        )
        for field, value in data.HOSPITAL.items():
            if field != "name":
                setattr(self.org, field, value)
        self.org.save()
        main_branch.address = main[4]
        main_branch.phone = data.HOSPITAL["phone"]
        main_branch.save()
        self.branches = {"main": main_branch}
        for key, name, code, city, address, is_main in data.BRANCHES:
            if key not in self.branches:
                self.branches[key] = Branch.objects.create(
                    organization=self.org, name=name, code=code, city=city, address=address,
                    is_main=is_main, phone="042-35700000",
                )  # fmt: skip
        self.main, self.gulberg = self.branches["main"], self.branches["gulberg"]
        ensure_payment_methods(self.org)
        self.methods = {m.code: m for m in PaymentMethod.objects.filter(organization=self.org)}

    def _staff(self):
        from apps.accounts.models import Role, RoleAssignment

        roles = {r.system_key: r for r in Role.objects.filter(organization=self.org)}
        self.logins = [(self.admin.username, "Super admin", "All branches")]
        self.users = {"admin": self.admin}
        for n, (key, first, last, role, branch_key, designation) in enumerate(data.STAFF[1:], 2):
            user = self._user(f"demo.{key}", first, last, designation, n)
            branch = self.branches[branch_key] if branch_key else None
            RoleAssignment.objects.create(
                user=user, role=roles[role], branch=branch, created_by=self.admin
            )
            self.users[key] = user
            self.logins.append((user.username, roles[role].name, branch.name if branch else "All"))
        self.doctor_role = roles["doctor"]

    def _user(self, username, first, last, designation, n):
        from apps.accounts.models import User

        user = User(
            organization=self.org, username=username, first_name=first, last_name=last,
            designation=designation, employee_code=f"EMP-{n:03d}",
            phone=f"0300{self.rng.randint(1000000, 9999999)}",
        )  # fmt: skip
        user.set_password(self.password)
        user.save()
        return user

    def _clinical_masters(self):
        from apps.accounts.models import RoleAssignment
        from apps.encounters.services import ensure_diagnosis_codes
        from apps.masterdata.models import (
            Department,
            DoctorLeave,
            DoctorProfile,
            DoctorSchedule,
            Service,
            ServiceCategory,
            Specialty,
        )

        self.departments = {
            key: Department.objects.create(
                organization=self.org,
                name=name,
                code=code,
                token_prefix=prefix,
                description=f"{name} outpatient clinic",
            )  # fmt: skip
            for key, name, code, prefix in data.DEPARTMENTS
        }
        specialties = {
            name: Specialty.objects.create(organization=self.org, name=name)
            for name in data.SPECIALTIES
        }
        self.doctors = {}
        for n, row in enumerate(data.DOCTORS, 1):
            key, first, last, dept, specialty, quals, fee, fu_fee, slot, evenings = row
            user = self._user(f"demo.dr.{key}", first, last, "Consultant", 100 + n)
            RoleAssignment.objects.create(
                user=user, role=self.doctor_role, branch=None, created_by=self.admin
            )
            doctor = DoctorProfile.objects.create(
                organization=self.org, user=user, department=self.departments[dept],
                specialty=specialties[specialty], registration_number=f"PMDC-{10000 + n * 731}-P",
                qualifications=quals, consultation_fee=fee, follow_up_fee=fu_fee,
                follow_up_days=14, slot_minutes=slot,
            )  # fmt: skip
            for weekday in range(6):  # Monday to Saturday mornings at the main campus
                DoctorSchedule.objects.create(
                    doctor=doctor, branch=self.main, weekday=weekday, start_time=time(9),
                    end_time=time(14), break_start=time(12), break_end=time(12, 30),
                    room=f"OPD-{100 + n}",
                )  # fmt: skip
            for weekday in evenings:
                DoctorSchedule.objects.create(
                    doctor=doctor, branch=self.gulberg, weekday=weekday, start_time=time(17),
                    end_time=time(20), room=f"Clinic {n}",
                )  # fmt: skip
            self.doctors[key] = doctor
            self.logins.append((user.username, "Doctor", "All"))
        DoctorLeave.objects.create(
            doctor=self.doctors["kashif"], start_date=self.today + timedelta(days=12),
            end_date=self.today + timedelta(days=16), reason="Orthopaedic conference",
            created_by=self.admin,
        )  # fmt: skip

        categories = {}
        self.services = {}
        for code, name, category, price, is_procedure, dept in data.SERVICES:
            if category not in categories:
                categories[category] = ServiceCategory.objects.create(
                    organization=self.org, name=category
                )
            self.services[code] = Service.objects.create(
                organization=self.org, category=categories[category], code=code, name=name,
                price=price, is_procedure=is_procedure,
                department=self.departments[dept] if dept else None,
            )  # fmt: skip
        ensure_diagnosis_codes(self.org)

    def _lab_catalogue(self):
        from apps.lab.models import LabPanel, LabReferenceRange, LabTest, LabTestCategory

        categories = {
            name: LabTestCategory.objects.create(organization=self.org, name=name, sort_order=i)
            for i, name in enumerate(data.LAB_CATEGORIES)
        }
        self.lab_tests, self.lab_normals = {}, {}
        for row in data.LAB_TESTS:
            code, name, cat, sample, price, rtype, unit, decimals, normal, ranges, typical = row
            test = LabTest.objects.create(
                organization=self.org, code=code, name=name, category=categories[cat],
                sample_type=sample, price=price, result_type=rtype, unit=unit,
                decimals=decimals, normal_value=normal,
                turnaround_hours=4 if cat == "Haematology" else 24,
            )  # fmt: skip
            for gender, low, high, crit_low, crit_high in ranges:
                LabReferenceRange.objects.create(
                    test=test, gender=gender, low=low, high=high, critical_low=crit_low,
                    critical_high=crit_high,
                )  # fmt: skip
            self.lab_tests[code] = test
            self.lab_normals[code] = typical
        self.lab_panels = {}
        for code, name, price, tests in data.LAB_PANELS:
            panel = LabPanel.objects.create(
                organization=self.org, code=code, name=name, price=price
            )
            panel.tests.set([self.lab_tests[t] for t in tests])
            self.lab_panels[code] = panel

    def _pharmacy_catalogue(self):
        from apps.pharmacy.models import Product, ProductCategory, Vendor
        from apps.prescriptions.models import Medicine

        categories = {
            name: ProductCategory.objects.create(organization=self.org, name=name)
            for name in data.PRODUCT_CATEGORIES
        }
        self.products, self.medicines = {}, {}
        for row in data.PRODUCTS:
            sku, brand, generic, strength, form, route, unit, pack, price, rx, cat, reorder = row
            medicine = None
            if form:
                medicine = Medicine.objects.create(
                    organization=self.org, brand_name=brand, generic_name=generic,
                    strength=strength, form=form, route=route,
                )  # fmt: skip
                self.medicines[sku] = medicine
            self.products[sku] = Product.objects.create(
                organization=self.org, sku=sku, brand_name=brand, generic_name=generic,
                medicine=medicine, strength=strength, category=categories[cat], unit=unit,
                pack_size=pack, sale_price=price, purchase_price=self.cost(price),
                requires_prescription=rx, reorder_level=reorder,
                barcode=f"8964{self.rng.randint(10**8, 10**9 - 1)}",
            )  # fmt: skip
        self.vendors = [
            Vendor.objects.create(
                organization=self.org,
                name=name,
                contact_person=person,
                phone=phone,
                payment_terms_days=terms,
                address="Lahore",
            )  # fmt: skip
            for name, person, phone, terms in data.VENDORS
        ]

    def cost(self, price):
        return self.money(Decimal(price) * Decimal("0.78"))

    def _wards(self):
        from apps.ipd import services as ipd
        from apps.ipd.models import Room, Ward

        def ward(name, code, kind, dept, floor):
            return Ward.objects.create(
                organization=self.org, branch=self.main, name=name, code=code, kind=kind,
                department=self.departments[dept] if dept else None, floor=floor,
            )  # fmt: skip

        def room(w, name, kind, rate):
            return Room.objects.create(
                organization=self.org, branch=self.main, ward=w, name=name, kind=kind,
                daily_rate=rate,
            )  # fmt: skip

        general = ward("General Ward", "GW", Ward.Kind.GENERAL, "gm", "Ground floor")
        private = ward("Private Rooms", "PVT", Ward.Kind.PRIVATE, None, "First floor")
        icu = ward("ICU", "ICU", Ward.Kind.ICU, "cardio", "First floor")
        ipd.add_beds(room(general, "Ward 1", Room.Kind.WARD, 3000), prefix="G", count=6)
        ipd.add_beds(room(private, "201", Room.Kind.PRIVATE, 9000), prefix="P", count=1)
        ipd.add_beds(room(private, "202", Room.Kind.PRIVATE, 9000), prefix="P", count=1, start=2)
        ipd.add_beds(room(icu, "ICU Bay", Room.Kind.ICU, 18000), prefix="I", count=2)

    # --- pharmacy stock ---------------------------------------------------------------------

    def _stock(self):
        """Three supplier deliveries and a top-up at the main campus; opening stock at Gulberg."""
        from apps.pharmacy import purchasing, stock

        skus = [p[0] for p in data.PRODUCTS]
        chunks = [skus[:20], skus[20:38], skus[38:]]
        pharmacist = self.users["pharmacy1"]
        for n, (vendor, chunk) in enumerate(zip(self.vendors, chunks, strict=True)):
            self.at(self.today - timedelta(days=40 - n * 4), 11)
            self._purchase(vendor, chunk, pharmacist, near_expiry=(n == 2))
        self.at(self.today - timedelta(days=9), 12)
        top_up = ["PAN500", "BRU400", "AUG625", "CIP500", "RIS20", "GLU500", "NOR5", "LIP20",
                  "LOP75", "CEF400", "ORS", "FEFOL", "CAL600", "MOT10", "SOF10"]  # fmt: skip
        self._purchase(self.vendors[0], top_up, pharmacist, invoice="TOPUP")

        self.at(self.today - timedelta(days=38), 10)
        request = self.req(self.users["pharmacy2"], self.gulberg)
        for sku in data.GULBERG_STOCK:
            product = self.products[sku]
            stock.receive(
                request=request, product=product, batch_number=self.batch_no(sku),
                expiry_date=self.today + timedelta(days=self.rng.randint(240, 700)),
                quantity=self.order_quantity(product), purchase_cost=product.purchase_price,
                reference="Opening stock", opening=True,
            )  # fmt: skip
        # Low stock and a supplier balance to show on the dashboards.
        request = self.req(pharmacist, self.main)
        purchasing.post_opening_balance(self.vendors[1], request=request, amount=Decimal("45000"))

    def _purchase(self, vendor, skus, user, *, near_expiry=False, invoice=""):
        from apps.pharmacy import purchasing

        request = self.req(user, self.main)
        number = f"{invoice or vendor.name[:3].upper()}-{self.rng.randint(1000, 9999)}"
        purchase = purchasing.create_purchase(
            request=request, vendor=vendor, supplier_invoice_number=number
        )
        for i, sku in enumerate(skus):
            product = self.products[sku]
            days = 50 if near_expiry and i < 2 else self.rng.randint(300, 720)
            quantity = self.order_quantity(product)
            if sku in ("AZO500", "FUC"):  # left below the reorder level
                quantity = product.reorder_level // 2
            purchasing.add_item(
                purchase, request=request, product=product, quantity=quantity,
                unit_cost=product.purchase_price, batch_number=self.batch_no(sku),
                expiry_date=self.today + timedelta(days=days),
            )  # fmt: skip
        purchasing.mark_ordered(purchase, request=request)
        self.clock.plus(90)
        purchasing.receive(purchase, request=request)

    def order_quantity(self, product):
        packs = 30 if product.pack_size > 1 else 24
        return max(product.pack_size * packs // (3 if product.pack_size >= 30 else 1), 12)

    def batch_no(self, sku):
        return f"{sku[:3]}{self.rng.randint(10000, 99999)}"

    # --- patients ---------------------------------------------------------------------------

    def _patients(self):
        from apps.patients.services import register_patient

        self.patients = []
        today_count = 2
        n = len(data.PATIENTS)
        for i, (name, gender, age, area, blood) in enumerate(data.PATIENTS):
            if i >= n - today_count:
                day, branch, desk = self.today, self.main, self.users["reception1"]
                self.at(day, 9, 10 + i)
            else:
                day = self.today - timedelta(days=42 - (i * 26 // (n - today_count)))
                branch = self.gulberg if i % 5 == 4 else self.main
                desk = self.users["reception2" if branch == self.gulberg else "reception1"]
                self.at(day, 9 + i % 6, (i * 7) % 60)
            dob = self.today - timedelta(days=age * 365 + self.rng.randint(10, 340))
            adult = age >= 18
            relation = self.rng.choice(data.RELATIONS if adult else ["Father", "Mother"])
            patient = register_patient(
                organization=self.org, branch=branch, user=desk,
                data={
                    "full_name": name, "gender": gender, "date_of_birth": dob,
                    "dob_is_estimated": age > 60 and i % 2 == 0,
                    "phone": f"03{self.rng.choice(['00', '01', '21', '33', '45'])}"
                             f"{self.rng.randint(1000000, 9999999)}",
                    "cnic": f"35202{self.rng.randint(10**6, 10**7 - 1)}"
                            f"{1 if gender == 'male' else 2}" if adult else "",
                    "city": "Lahore", "address": f"House {self.rng.randint(1, 400)}, {area}",
                    "emergency_contact_name": f"{self.rng.choice(data.CONTACT_NAMES)} "
                                              f"{name.split()[-1]}",
                    "emergency_contact_phone": f"0300{self.rng.randint(1000000, 9999999)}",
                    "emergency_contact_relation": relation,
                    "blood_group": blood,
                    "allergy_status": "none" if i % 3 else "unknown",
                    "consent_recorded": True,
                },
            )  # fmt: skip
            self.patients.append(patient)

    # --- OPD history ------------------------------------------------------------------------

    # (past working-day index, doctor, case, patient, branch, prescription, lab stage, results)
    # prescription: dispense | pending (sent, not yet dispensed) | not_sent | none
    VISITS = [
        (11, "ayesha", "dengue", 2, "main", "dispense", "released", None),
        (11, "farhan", "angina", 0, "main", "dispense", "released", None),
        (10, "sana", "tonsillitis", 3, "main", "dispense", None, None),
        (10, "ayesha", "diabetes", 16, "main", "dispense", "released", None),
        (9, "kashif", "back_pain", 6, "main", "dispense", None, None),
        (9, "nadia", "anaemia_pregnancy", 5, "main", "dispense", "released", None),
        (8, "imran", "typhoid", 14, "main", "dispense", "released", None),
        (8, "imran", "gastritis", 23, "gulberg", "dispense", None, None),
        (7, "zainab", "lipids", 34, "main", "not_sent", "released", None),
        (7, "sana", "gastroenteritis", 11, "main", "dispense", None, None),
        (6, "ayesha", "uti", 9, "main", "dispense", "released", None),
        (6, "kashif", "knee_oa", 13, "main", "none", None, None),
        (5, "hamid", "asthma", 18, "gulberg", "dispense", None, None),
        (5, "nadia", "dysmenorrhoea", 25, "main", "none", None, None),
        (4, "farhan", "htn", 10, "main", "dispense", "released", None),
        (3, "imran", "urti", 12, "gulberg", "dispense", None, None),
        (2, "ayesha", "dengue", 35, "main", "pending", "verified", {"PLT": "18", "WBC": "2.4"}),
        (1, "zainab", "angina", 45, "main", "none", "result_entered", None),
        (1, "hamid", "gastroenteritis", 22, "main", "none", None, None),
        (0, "ayesha", "diabetes", 30, "main", "none", "processing", None),
    ]

    def _opd_history(self):
        from apps.appointments import services as appts

        planned = []
        for row in self.VISITS:
            idx, doc, case, patient_idx, branch_key, rx, lab, results = row
            doctor, branch = self.doctors[doc], self.branches[branch_key]
            day = self.clinic_day(doctor, branch, idx)
            planned.append(
                (day, doctor, branch, case, self.patients[patient_idx], rx, lab, results)
            )
        planned.sort(key=lambda v: (v[0], v[1].pk))
        slot_index = {}
        for day, doctor, branch, case, patient, rx, lab, results in planned:
            k = slot_index[(day, doctor.pk)] = slot_index.get((day, doctor.pk), -1) + 1
            appt = self._book(
                patient,
                doctor,
                branch,
                day,
                slot=1 + k * 3,
                booked_days_before=k,
                reason=data.CASES[case]["complaint"],
            )
            self._visit(appt, case, rx, lab, results)

        # Cancelled and missed appointments.
        for idx, doc, patient_idx, reason in [
            (4, "nadia", 15, "Patient is travelling; will call to rebook."),
            (2, "kashif", 17, "Pain settled; patient cancelled by phone."),
        ]:
            doctor = self.doctors[doc]
            day = self.clinic_day(doctor, self.main, idx)
            appt = self._book(self.patients[patient_idx], doctor, self.main, day, slot=6)
            self.at(day, 8, 30)
            appts.cancel(appt, user=self.users["reception1"], reason=reason)
        doctor = self.doctors["sana"]
        day = self.clinic_day(doctor, self.main, 1)
        appt = self._book(self.patients[28], doctor, self.main, day, slot=8)
        self.at(day, 14, 30)
        appts.mark_no_show(appt)

    def _book(
        self,
        patient,
        doctor,
        branch,
        day,
        *,
        slot,
        booked_days_before=1,
        visit_type=None,
        reason="",
    ):
        """Book the slot-th free slot of the doctor's clinic, as reception did beforehand."""
        from apps.appointments import services as appts
        from apps.appointments.models import Appointment

        desk = self.users["reception2" if branch == self.gulberg else "reception1"]
        self.at(day - timedelta(days=max(booked_days_before, 1)), 16, 15)
        free = [s for s in appts.available_slots(doctor, branch, day) if s.available]
        if not free:
            visit_type, start = Appointment.VisitType.WALK_IN, time(10)
        else:
            start = free[min(slot, len(free) - 1)].start
        visit_type = visit_type or appts.suggest_visit_type(doctor, patient, day)
        source = self.rng.choice(
            [Appointment.Source.COUNTER, Appointment.Source.PHONE, Appointment.Source.PHONE]
        )
        return appts.book(
            request=self.req(desk, branch), patient=patient, doctor=doctor, on_date=day,
            start=start, visit_type=visit_type, source=source, reason=reason,
        )  # fmt: skip

    def _visit(self, appt, case_key, rx_mode, lab_stage, results):
        """Arrival, payment, vitals, consultation, prescription, lab order and dispensing."""
        from apps.billing import services as billing
        from apps.encounters import services as encounters
        from apps.prescriptions import services as rx_services
        from apps.queue import services as queue

        case = data.CASES[case_key]
        branch, doctor, patient, day = appt.branch, appt.doctor, appt.patient, appt.date
        gulberg = branch == self.gulberg
        desk = self.users["reception2" if gulberg else "reception1"]
        slot = datetime.combine(day, appt.start_time)

        def at_slot(minutes):
            moment = slot + timedelta(minutes=minutes)
            self.at(day, moment.hour, moment.minute)

        at_slot(-25)
        desk_req = self.req(desk, branch)
        token = queue.issue_token(
            request=desk_req, patient=patient, department=appt.department, doctor=doctor,
            appointment=appt,
        )  # fmt: skip
        invoice = billing.create_invoice(request=desk_req, patient=patient, appointment=appt)
        method = "cash" if self.rng.random() < 0.7 else self.rng.choice(["easypaisa", "card"])
        billing.receive_payment(
            invoice, request=desk_req, method=self.method(method), amount=invoice.grand_total,
            reference=self.reference(method),
        )  # fmt: skip
        at_slot(-15)
        vitals_user = doctor.user if gulberg else self.users["nurse1"]
        self._vitals(patient, branch, vitals_user, token=token, case_key=case_key)

        at_slot(0)
        doc_req = self.req(doctor.user, branch)
        queue.call(token, request=doc_req, doctor=doctor)
        encounter, _ = encounters.start(request=doc_req, patient=patient, token=token)
        self.clock.plus(4)
        self._write_note(encounter, doc_req, case, day)
        if rx_mode != "none":
            for sku, dose, freq, days, food, instructions in case["rx"]:
                rx_services.add_item(
                    encounter, request=doc_req, medicine=self.medicines[sku],
                    data={"dose": dose, "frequency": freq, "duration_value": days,
                          "duration_unit": "days", "food": food, "instructions": instructions},
                )  # fmt: skip
            rx_services.set_advice(encounter, request=doc_req, advice=case["advice"])
        order = None
        if lab_stage and case.get("labs"):
            order = self._lab_order(patient, branch, doc_req, case["labs"], encounter=encounter)
        self.clock.plus(8)
        encounters.complete(encounter, request=doc_req)
        rx = rx_services.prescription_for(encounter)
        if rx and rx_mode in ("dispense", "pending"):
            rx_services.send_to_pharmacy(rx, request=doc_req)
        if order is not None:
            at_slot(35)
            self._run_lab(order, lab_stage, {**case.get("results", {}), **(results or {})})
        if rx and rx_mode == "dispense":
            at_slot(45)
            self._dispense(rx, branch)

    def _write_note(self, encounter, request, case, day):
        from apps.encounters import services as encounters
        from apps.encounters.models import DiagnosisCode

        code = DiagnosisCode.objects.get(organization=self.org, code=case["dx"])
        follow_up = day + timedelta(days=case["follow_up"]) if case["follow_up"] else None
        encounters.save_note(
            encounter, request=request,
            data={
                "chief_complaint": case["complaint"],
                "history_present_illness": case["hpi"],
                "past_medical_history": "Nil significant.",
                "allergy_notes": "No known drug allergies.",
                "examination_notes": case["exam"],
                "provisional_diagnosis": code.title,
                "final_diagnosis": code.title,
                "treatment_plan": case["plan"],
                "follow_up_date": follow_up,
                "follow_up_instructions": "Review with reports." if follow_up else "",
            },
        )  # fmt: skip
        encounters.add_diagnosis(encounter, request=request, code=code, kind="final")

    VITALS_BY_CASE = {
        "dengue": {"temperature_c": Decimal("38.9"), "pulse": 104},
        "typhoid": {"temperature_c": Decimal("38.6"), "pulse": 98},
        "tonsillitis": {"temperature_c": Decimal("38.4"), "pulse": 112},
        "htn": {"bp_systolic": 158, "bp_diastolic": 96},
        "angina": {"bp_systolic": 146, "bp_diastolic": 90, "pulse": 88},
        "diabetes": {"blood_glucose": 212, "glucose_context": "random"},
        "asthma": {"spo2": 96, "respiratory_rate": 24},
    }

    def _vitals(self, patient, branch, user, *, token=None, case_key=""):
        from apps.vitals import services as vitals

        child = (patient.age or 30) < 14
        readings = {
            "temperature_c": Decimal("36.8"),
            "pulse": self.rng.randint(72, 90) + (20 if child else 0),
            "respiratory_rate": 22 if child else 16,
            "spo2": self.rng.randint(97, 99),
            "weight_kg": Decimal(str(max(10, (patient.age or 30) * 3) if child else
                                     self.rng.randint(55, 92))),
            "height_cm": Decimal(str(100 + (patient.age or 5) * 5 if child else
                                     self.rng.randint(152, 182))),
        }  # fmt: skip
        if not child:
            readings |= {"bp_systolic": self.rng.randint(112, 128),
                         "bp_diastolic": self.rng.randint(72, 84)}  # fmt: skip
        readings |= self.VITALS_BY_CASE.get(case_key, {})
        vitals.record(request=self.req(user, branch), patient=patient, data=readings, token=token)

    # --- laboratory -------------------------------------------------------------------------

    def _lab_order(self, patient, branch, request, labs, *, encounter=None, priority="routine"):
        from apps.lab import services as lab

        return lab.create_order(
            request=request, patient=patient,
            tests=[self.lab_tests[c] for c in labs.get("tests", [])],
            panels=[self.lab_panels[c] for c in labs.get("panels", [])],
            encounter=encounter, priority=priority,
            source="doctor" if encounter else "desk",
            clinical_notes="Please process today." if priority != "routine" else "",
        )  # fmt: skip

    STAGES = ["payment_pending", "sample_pending", "sample_collected", "processing",
              "result_entered", "verified", "released"]  # fmt: skip

    def _run_lab(self, order, stage, results, *, pay=True):
        """Walk a lab order through the desk and the lab up to `stage`."""
        from apps.billing import services as billing
        from apps.lab import services as lab

        reach = self.STAGES.index(stage)
        main_req = self.req(self.users["lab1"], order.branch)
        verifier = self.req(self.users["lab1"], order.branch)
        tech = self.req(self.users["lab2"], order.branch)
        order.refresh_from_db()
        if pay and reach >= 1 and order.invoice and order.invoice.balance_due > 0:
            cashier = self.req(self.users["cashier1"], order.branch)
            method = "cash" if self.rng.random() < 0.75 else "card"
            billing.receive_payment(
                order.invoice, request=cashier, method=self.method(method),
                amount=order.invoice.balance_due, reference=self.reference(method),
            )  # fmt: skip
        if reach < 2:
            return
        self.clock.plus(10)
        for sample in order.samples.all():
            lab.collect_sample(sample, request=tech)
        if reach < 3:
            return
        self.clock.plus(20)
        lab.start_processing(order, request=tech)
        if reach < 4:
            return
        self.clock.plus(90)
        items = list(order.items.select_related("test"))
        for item in items:
            value = results.get(item.test.code, self.lab_normals[item.test.code])
            lab.enter_result(item, request=tech, value=value)
        if reach < 5:
            return
        self.clock.plus(40)
        lab.verify(items, request=verifier)
        if reach < 6:
            return
        self.clock.plus(5)
        lab.release(order, request=main_req)

    def _desk_lab_orders(self):
        """Walk-in patients sent to the lab from outside (no consultation here)."""
        tech = self.users["lab2"]
        orders = [
            (9, 7, {"panels": ["CBC"]}, "released", {}),
            (8, 19, {"tests": ["FBS", "HBA1C"]}, "released", {"FBS": "131", "HBA1C": "6.4"}),
            (6, 24, {"panels": ["RFT", "LFT"]}, "released", {"ALT": "88"}),
            (3, 29, {"tests": ["TSH"]}, "released", {"TSH": "6.8"}),
            (0, 33, {"tests": ["HCV", "HBSAG"]}, "processing", {}),
        ]
        for idx, patient_idx, labs, stage, results in orders:
            day = self.past_days[idx]
            self.at(day, 10 + idx % 3, 15)
            request = self.req(tech, self.main)
            order = self._lab_order(self.patients[patient_idx], self.main, request, labs)
            self._run_lab(order, stage, results)

    # --- pharmacy ---------------------------------------------------------------------------

    def _dispense(self, rx, branch):
        from apps.pharmacy import pos

        user = self.users["pharmacy2" if branch == self.gulberg else "pharmacy1"]
        request = self.req(user, branch)
        cart = pos.open_cart(request)
        lines = []
        for line in pos.prescription_lines(rx, branch):
            product = (line["exact"] or line["substitutes"] or [None])[0]
            if product is None or not line["remaining"]:
                continue
            lines.append({"rx_item": line["item"], "product": product,
                          "quantity": min(line["remaining"], product.stock),
                          "reason": "" if line["exact"] else "Same generic in stock"})  # fmt: skip
        pos.fill_from_prescription(cart, request=request, prescription=rx, lines=lines)
        self._pay_cart(cart, request)

    def _pay_cart(self, cart, request):
        from apps.pharmacy import pos

        total = pos.totals(cart)["grand_total"]
        method = "cash" if self.rng.random() < 0.8 else "easypaisa"
        pos.checkout(
            cart, request=request,
            payments=[{"method": self.method(method), "amount": total,
                       "reference": self.reference(method)}],
        )  # fmt: skip

    def _walk_in_sales(self):
        """Over-the-counter sales without a prescription."""
        from apps.pharmacy import pos

        sales = [
            (9, "main", None, [("PAN500", 20), ("ORS", 4)]),
            (7, "gulberg", None, [("SURZ", 30)]),
            (5, "main", 1, [("BRU400", 10), ("RIS20", 14)]),
            (3, "gulberg", None, [("CALPOL", 1), ("ORS", 2)]),
            (1, "gulberg", 41, [("SOF10", 10), ("PAN500", 10)]),
            (0, "main", None, [("SURZ", 30), ("CAL600", 30), ("POLY", 1)]),
        ]
        for idx, branch_key, patient_idx, items in sales:
            branch = self.branches[branch_key]
            user = self.users["pharmacy2" if branch_key == "gulberg" else "pharmacy1"]
            self.at(self.past_days[idx] if idx else self.today, 17, 20)
            request = self.req(user, branch)
            cart = pos.open_cart(request)
            pos.attach_patient(
                cart, request=request,
                patient=self.patients[patient_idx] if patient_idx is not None else None,
                customer_name="Walk-in customer",
            )  # fmt: skip
            for sku, quantity in items:
                pos.add_product(cart, request=request, product=self.products[sku],
                                quantity=quantity)  # fmt: skip
            self._pay_cart(cart, request)

    def _stock_corrections(self):
        from apps.pharmacy import stock
        from apps.pharmacy.models import InventoryBatch, StockTransaction

        request = self.req(self.users["pharmacy1"], self.main)
        self.at(self.past_days[4], 18)
        batch = InventoryBatch.objects.filter(
            branch=self.main, product=self.products["CALPOL"]
        ).first()
        stock.write_off(batch, request=request, quantity=2, kind=StockTransaction.Kind.DAMAGED,
                        reason="Bottles broken during shelving")  # fmt: skip
        batch = InventoryBatch.objects.filter(
            branch=self.main, product=self.products["GRAV50"]
        ).first()
        stock.adjust_to_count(batch, request=request, counted=batch.quantity_available - 10,
                              reason="Monthly stock count")  # fmt: skip

    # --- inpatients -------------------------------------------------------------------------

    def _admissions(self):
        from apps.ipd import services as ipd
        from apps.ipd.models import Bed

        nurse = self.users["nurse1"]
        cashier = self.users["cashier1"]
        meds = {
            "gm": [("NS1000", "1000 ml", "iv", "BD", "Over 8 hours"),
                   ("PAN500", "1 tablet", "oral", "TDS", "If temperature above 100 °F")],
            "cardio": [("LOP75", "1 tablet", "oral", "OD", ""),
                       ("LIP20", "1 tablet", "oral", "HS", ""),
                       ("CON5", "1 tablet", "oral", "OD", "Hold if pulse below 55")],
            "gyn": [("CEFTRX", "1 g", "iv", "BD", "For 48 hours after surgery"),
                    ("KET30", "30 mg", "im", "Q8H", "For pain")],
        }  # fmt: skip
        rounds = {
            "gm": ("Febrile overnight, tolerating orally. Platelets 64.", "Continue IV fluids, "
                   "repeat CBC in the morning."),
            "cardio": ("No further chest pain. Troponin negative x2. ECG stable.",
                       "Continue antiplatelets. Plan echo and stress test."),
            "gyn": ("Post-op day 1. Wound dry, passing flatus, mobilising.",
                    "Remove catheter, start soft diet."),
        }  # fmt: skip
        for plan in data.ADMISSIONS:
            doctor = self.doctors[plan["doctor"]]
            patient = self.patients[plan["patient"]]
            day = self.today - timedelta(days=plan["days_ago"])
            self.at(day, 11)
            nurse_req = self.req(nurse, self.main)
            bed = Bed.objects.get(branch=self.main, code=plan["bed"])
            admission = ipd.admit(
                request=nurse_req, patient=patient, doctor=doctor,
                department=self.departments[plan["dept"]], bed=bed, reason=plan["reason"],
                diagnosis=plan["diagnosis"],
                attendant={"name": f"{patient.emergency_contact_name}",
                           "relation": patient.emergency_contact_relation,
                           "phone": patient.emergency_contact_phone},
            )  # fmt: skip
            self.clock.plus(20)
            ipd.record_deposit(
                admission, request=self.req(cashier, self.main),
                method=self.method(plan["method"]), amount=plan["deposit"],
                reference=self.reference(plan["method"]),
            )  # fmt: skip
            self._vitals(patient, self.main, nurse, case_key=plan["case"])
            doc_req = self.req(doctor.user, self.main)
            self.clock.plus(40)
            for sku, dose, route, freq, instructions in meds[plan["dept"]]:
                ipd.order_medication(
                    admission, request=doc_req,
                    data={"medicine": self.medicines[sku], "dose": dose, "route": route,
                          "frequency": freq, "instructions": instructions},
                )  # fmt: skip
            if plan["dept"] == "cardio":
                ipd.add_charge(admission, request=nurse_req, service=self.services["ECG"])
            for n in range(plan["days_ago"]):
                self.at(day + timedelta(days=n + 1), 10, 30)
                findings, next_steps = rounds[plan["dept"]]
                ipd.add_round(admission, request=self.req(doctor.user, self.main),
                              findings=findings, plan=next_steps)  # fmt: skip
                ipd.add_charge(admission, request=self.req(nurse, self.main),
                               service=self.services["IPD-VISIT"])  # fmt: skip
            ipd.add_charge(
                admission,
                request=self.req(nurse, self.main),
                service=self.services["NURS-DAY"],
                quantity=max(plan["days_ago"], 1),
            )

        self.at(self.today - timedelta(days=1), 15)
        request = self.req(nurse, self.main)
        ipd.set_bed_status(Bed.objects.get(branch=self.main, code="G-05"), request=request,
                           status=Bed.Status.CLEANING)  # fmt: skip
        ipd.set_bed_status(Bed.objects.get(branch=self.main, code="G-06"), request=request,
                           status=Bed.Status.MAINTENANCE, note="Side rail repair")  # fmt: skip

    # --- today and the coming days ----------------------------------------------------------

    def _today(self):
        """A doctor mid-consultation, a child waiting in the queue, an afternoon booking, and
        lab desk work in progress."""
        from apps.appointments import services as appts
        from apps.appointments.models import Appointment
        from apps.encounters import services as encounters
        from apps.queue import services as queue

        today = self.today
        desk = self.req(self.users["reception1"], self.main)
        # 1. In consultation with Dr Ayesha.
        ayesha = self.doctors["ayesha"]
        appt = self._book(self.patients[31], ayesha, self.main, today, slot=0)
        self.at(today, 9, 5)
        token = queue.issue_token(request=self.req(self.users["reception1"], self.main),
                                  patient=appt.patient, department=appt.department,
                                  doctor=ayesha, appointment=appt)  # fmt: skip
        self._vitals(appt.patient, self.main, self.users["nurse1"], token=token, case_key="urti")
        self.at(today, 9, 20)
        doc_req = self.req(ayesha.user, self.main)
        queue.call(token, request=doc_req, doctor=ayesha)
        encounter, _ = encounters.start(request=doc_req, patient=appt.patient, token=token)
        encounters.save_note(encounter, request=doc_req, data={
            "chief_complaint": data.CASES["urti"]["complaint"],
            "history_present_illness": data.CASES["urti"]["hpi"],
        })  # fmt: skip

        # 2. Walk-in child waiting for Dr Sana.
        sana = self.doctors["sana"]
        self.at(today, 9, 40)
        walk_in = appts.book(
            request=desk, patient=self.patients[42], doctor=sana, on_date=today,
            start=self._now_time(), visit_type=Appointment.VisitType.WALK_IN,
            source=Appointment.Source.COUNTER, reason="Fever since last night",
        )  # fmt: skip
        token = queue.issue_token(request=desk, patient=walk_in.patient,
                                  department=walk_in.department, doctor=sana,
                                  appointment=walk_in)  # fmt: skip
        self._vitals(walk_in.patient, self.main, self.users["nurse2"], token=token,
                     case_key="tonsillitis")  # fmt: skip

        # 3. Booked for later today with Dr Farhan, confirmed by phone.
        farhan = self.doctors["farhan"]
        later = self._book(self.patients[37], farhan, self.main, today, slot=9)
        if later.status == Appointment.Status.SCHEDULED:
            appts.confirm(later)

        # Lab desk: one paid and waiting for the sample, two waiting for payment, one STAT.
        tech = self.req(self.users["lab2"], self.main)
        self.at(today, 9, 30)
        order = self._lab_order(self.patients[36], self.main, tech, {"panels": ["CBC"]})
        self._run_lab(order, "sample_pending", {})
        self.at(today, 10, 0)
        self._lab_order(self.patients[49], self.main, tech, {"tests": ["RBS", "URE"]})
        self._lab_order(self.patients[44], self.main, tech, {"tests": ["MP", "NS1"]})
        self.at(today, 10, 20)
        order = self._lab_order(self.patients[43], self.main, tech, {"tests": ["TROP"]},
                                priority="stat")  # fmt: skip
        self._run_lab(order, "sample_collected", {}, pay=False)  # STAT: sample before payment

    def _now_time(self):
        now = timezone.localtime(self.clock.now())
        return time(now.hour, now.minute)

    def _future_appointments(self):
        from apps.appointments.models import Appointment

        for doc, branch_key, patient_idx, after, visit_type in [
            ("kashif", "main", 39, 1, Appointment.VisitType.NEW),
            ("nadia", "main", 40, 2, Appointment.VisitType.NEW),
            ("zainab", "main", 46, 3, Appointment.VisitType.NEW),
            ("hamid", "gulberg", 47, 1, Appointment.VisitType.NEW),
        ]:
            doctor, branch = self.doctors[doc], self.branches[branch_key]
            day = self.next_clinic_day(doctor, branch, after)
            self._book(
                self.patients[patient_idx],
                doctor,
                branch,
                day,
                slot=2,
                booked_days_before=(day - self.today).days,
                visit_type=visit_type,
            )

    # --- output ---------------------------------------------------------------------------------

    def _write_logins(self):
        path = settings.DATA_DIR / "DEMO_LOGIN.txt"
        lines = [
            "SehatDesk demo hospital logins (demo data only; never use on a customer machine)",
            f"Password for every account: {self.password}",
            "",
            *(f"{u:<24} {role:<16} {branch}" for u, role, branch in self.logins),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.login_file = path

    def _report(self):
        from apps.appointments.models import Appointment
        from apps.billing.models import Invoice, Payment
        from apps.encounters.models import Encounter
        from apps.ipd.models import Admission, Bed
        from apps.lab.models import LabOrder
        from apps.patients.models import Patient
        from apps.pharmacy.models import InventoryBatch, Product, StockTransaction
        from apps.prescriptions.models import Prescription

        org = self.org
        counts = [
            ("Branches", org.branches.count()),
            ("Departments", len(self.departments)),
            ("Doctors", len(self.doctors)),
            ("Staff logins", len(self.logins)),
            ("Patients", Patient.objects.filter(organization=org).count()),
            ("Appointments", Appointment.objects.filter(organization=org).count()),
            ("Completed consultations",
             Encounter.objects.filter(organization=org, status="completed").count()),
            ("Prescriptions", Prescription.objects.filter(organization=org).count()),
            ("Lab orders", LabOrder.objects.filter(organization=org).count()),
            ("Pharmacy products", Product.objects.filter(organization=org).count()),
            ("Stock batches", InventoryBatch.objects.filter(organization=org).count()),
            ("Stock transactions", StockTransaction.objects.filter(organization=org).count()),
            ("Invoices", Invoice.objects.filter(organization=org).count()),
            ("Payments", Payment.objects.filter(organization=org).count()),
            ("IPD beds", Bed.objects.filter(organization=org).count()),
            ("Active admissions", Admission.objects.filter(
                organization=org, status__in=Admission.ACTIVE_STATUSES).count()),
        ]  # fmt: skip
        self.stdout.write(self.style.SUCCESS(f"Loaded {org.name}."))
        for label, n in counts:
            self.stdout.write(f"  {label:<26} {n}")
        self.stdout.write(f"Logins and the demo password: {self.login_file}")
