"""Master data: departments, specialties, doctors (with schedules and leave) and services.

Departments, specialties, doctors and services are organization-wide. Doctor schedules and
service prices can differ per branch.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models

from apps.core.models import TimeStampedModel
from apps.core.scoping import OrgScopedModel

_code_validator = RegexValidator(r"^[A-Z0-9-]{1,12}$", "Use capital letters, digits or -.")
money = {"max_digits": 12, "decimal_places": 2, "validators": [MinValueValidator(Decimal("0"))]}


class Department(OrgScopedModel):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=12, validators=[_code_validator])
    description = models.CharField(max_length=200, blank=True)
    token_prefix = models.CharField(
        max_length=3,
        blank=True,
        validators=[RegexValidator(r"^[A-Z]{1,3}$", "Use 1–3 capital letters.")],
        help_text="Token letters, e.g. A for A-024. Default: the code's first letter.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_department_code"),
            models.UniqueConstraint(fields=["organization", "name"], name="uniq_department_name"),
        ]

    def __str__(self):
        return self.name

    @property
    def token_letters(self) -> str:
        return self.token_prefix or self.code[:1]


class Specialty(OrgScopedModel):
    name = models.CharField(max_length=100)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "specialties"
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="uniq_specialty_name")
        ]

    def __str__(self):
        return self.name


class DoctorProfile(OrgScopedModel):
    """Clinical profile for a staff account. Access still comes from the user's roles."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="doctor_profile"
    )
    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="doctors")
    specialty = models.ForeignKey(
        Specialty, on_delete=models.PROTECT, null=True, blank=True, related_name="doctors"
    )
    registration_number = models.CharField(
        max_length=40, blank=True, help_text="PMDC or other professional registration number."
    )
    qualifications = models.CharField(max_length=200, blank=True, help_text="e.g. MBBS, FCPS")
    consultation_fee = models.DecimalField(**money, default=Decimal("0"))
    follow_up_fee = models.DecimalField(**money, default=Decimal("0"))
    follow_up_days = models.PositiveSmallIntegerField(
        default=7, help_text="A visit within this many days of the last one counts as a follow-up."
    )
    slot_minutes = models.PositiveSmallIntegerField(
        default=15, validators=[MinValueValidator(5)], help_text="Length of one appointment slot."
    )
    allow_overbooking = models.BooleanField(
        default=False, help_text="Allow more than one appointment in the same slot."
    )
    is_active = models.BooleanField(default=True, help_text="Accepting appointments.")

    class Meta:
        ordering = ["user__first_name", "user__last_name"]

    def __str__(self):
        return self.display_name

    @property
    def display_name(self) -> str:
        return f"Dr. {self.user.get_full_name() or self.user.username}"


WEEKDAYS = [
    (0, "Monday"),
    (1, "Tuesday"),
    (2, "Wednesday"),
    (3, "Thursday"),
    (4, "Friday"),
    (5, "Saturday"),
    (6, "Sunday"),
]


class DoctorSchedule(TimeStampedModel):
    """Weekly clinic hours for one doctor, in one branch, on one weekday."""

    doctor = models.ForeignKey(DoctorProfile, on_delete=models.CASCADE, related_name="schedules")
    branch = models.ForeignKey(
        "organizations.Branch", on_delete=models.PROTECT, related_name="doctor_schedules"
    )
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAYS)
    start_time = models.TimeField()
    end_time = models.TimeField()
    break_start = models.TimeField(null=True, blank=True)
    break_end = models.TimeField(null=True, blank=True)
    room = models.CharField(max_length=40, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["weekday", "start_time"]
        constraints = [
            models.UniqueConstraint(
                fields=["doctor", "branch", "weekday"], name="uniq_schedule_per_day"
            )
        ]

    def clean(self):
        if self.start_time and self.end_time and self.start_time >= self.end_time:
            raise ValidationError("Clinic end time must be after start time.")
        if bool(self.break_start) != bool(self.break_end):
            raise ValidationError("Give both break start and break end, or neither.")
        if self.break_start and self.break_end:
            if not (self.start_time <= self.break_start < self.break_end <= self.end_time):
                raise ValidationError("The break must fall inside clinic hours.")

    def slot_starts(self, slot_minutes: int, on_date):
        """Start times of every slot on a date, skipping the break."""
        step = timedelta(minutes=slot_minutes)
        cursor = datetime.combine(on_date, self.start_time)
        end = datetime.combine(on_date, self.end_time)
        brk = (
            (datetime.combine(on_date, self.break_start), datetime.combine(on_date, self.break_end))
            if self.break_start
            else None
        )
        while cursor + step <= end:
            if brk and cursor < brk[1] and cursor + step > brk[0]:
                cursor = brk[1]
                continue
            yield cursor.time()
            cursor += step


class DoctorLeave(TimeStampedModel):
    doctor = models.ForeignKey(DoctorProfile, on_delete=models.CASCADE, related_name="leaves")
    start_date = models.DateField()
    end_date = models.DateField()
    reason = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-start_date"]

    def clean(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError("Leave must end on or after its start date.")


class ServiceCategory(OrgScopedModel):
    name = models.CharField(max_length=80)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "service categories"
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="uniq_service_category")
        ]

    def __str__(self):
        return self.name


class Service(OrgScopedModel):
    """A billable item (consultation, procedure, dressing...). Billing uses it from Phase 7.
    Services marked is_procedure are ordered per patient from apps.procedures (Phase 33)."""

    category = models.ForeignKey(ServiceCategory, on_delete=models.PROTECT, related_name="services")
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, null=True, blank=True, related_name="services"
    )
    code = models.CharField(max_length=12, validators=[_code_validator])
    name = models.CharField(max_length=120)
    price = models.DecimalField(**money, help_text="Default price for every branch.")
    is_procedure = models.BooleanField(
        default=False,
        help_text="A procedure added to a patient's visit, e.g. ECG, ECHO, ultrasound, an "
        "operation or dental work.",
    )
    price_editable = models.BooleanField(
        "Price can be changed per patient",
        default=True,
        help_text="Staff with the price-change permission may charge a different price "
        "(with a reason). The list price is kept on record.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["category__name", "name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_service_code")
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"

    def price_for(self, branch) -> Decimal:
        """Branch price when one is set, otherwise the default price."""
        override = self.branch_prices.filter(branch=branch).first() if branch else None
        return override.price if override else self.price


class ServiceBranchPrice(TimeStampedModel):
    service = models.ForeignKey(Service, on_delete=models.CASCADE, related_name="branch_prices")
    branch = models.ForeignKey("organizations.Branch", on_delete=models.CASCADE, related_name="+")
    price = models.DecimalField(**money)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["service", "branch"], name="uniq_service_branch_price")
        ]
