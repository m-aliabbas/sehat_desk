"""Reception desk, walk-ins, token actions, doctor queue, counters and the public display."""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import has_perm, permission_required
from apps.appointments import services as appt_services
from apps.appointments.models import Appointment
from apps.audit import services as audit
from apps.billing import services as billing_services
from apps.core.forms import StyledFormMixin
from apps.core.scoping import get_scoped_or_404, scoped
from apps.masterdata.models import Department, DoctorProfile, DoctorSchedule
from apps.organizations.models import Branch
from apps.patients.models import Patient

from . import services
from .models import Counter, QueueToken

Status = QueueToken.Status
DESK = "queue.manage"


def _doctors(request):
    return (
        scoped(DoctorProfile, request).filter(is_active=True).select_related("user", "department")
    )


def _tokens(request):
    return scoped(QueueToken, request).filter(date=timezone.localdate())


def _back(request, default):
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, {request.get_host()}):
        return redirect(nxt)
    return redirect(default)


# --- Reception desk ----------------------------------------------------------------------------


@require_GET
@permission_required(DESK)
def desk(request):
    """Reception & token desk (design screen 02). Live parts re-render every few seconds."""
    clinic = request.GET.get("clinic", "")
    tokens = _tokens(request).select_related("patient", "doctor__user", "department")
    doctor = None
    if clinic.startswith("doc-") and clinic[4:].isdigit():
        doctor = _doctors(request).filter(pk=clinic[4:]).first()
        if doctor:
            tokens = services.queue_for(request.branch, doctor=doctor)
    elif clinic.startswith("dept-") and clinic[5:].isdigit():
        tokens = tokens.filter(department_id=clinic[5:])

    open_tokens = tokens.filter(status__in=QueueToken.OPEN_STATUSES).order_by(
        "priority_rank", "queued_at"
    )
    closed_tokens = tokens.exclude(status__in=QueueToken.OPEN_STATUSES).order_by("-updated_at")[:10]
    today = timezone.localdate()
    working_ids = DoctorSchedule.objects.filter(
        branch=request.branch, weekday=today.weekday(), is_active=True
    ).values_list("doctor_id", flat=True)
    doctors_today = []
    for d in _doctors(request).filter(pk__in=working_ids):
        mine = services.queue_for(request.branch, doctor=d)
        doctors_today.append(
            {
                "doctor": d,
                "current": services.current_for(request.branch, d),
                "waiting": mine.filter(status=Status.WAITING).count(),
                "on_leave": appt_services.on_leave(d, today),
            }
        )
    ctx = {
        "clinic": clinic,
        "selected_doctor": doctor,
        "current": services.current_for(request.branch, doctor) if doctor else None,
        "open_tokens": open_tokens,
        "closed_tokens": closed_tokens,
        "stats": services.stats(request.branch),
        "doctors_today": doctors_today,
        "upcoming": scoped(Appointment, request)
        .filter(date=today, status__in=[Appointment.Status.SCHEDULED, Appointment.Status.CONFIRMED])
        .select_related("patient", "doctor__user")
        .order_by("start_time")[:8],
        "doctor_options": [(f"doc-{d.pk}", f"{d} · {d.department}") for d in _doctors(request)],
        "department_options": [
            (f"dept-{d.pk}", d.name) for d in scoped(Department, request).filter(is_active=True)
        ],
        "counters": scoped(Counter, request).filter(is_active=True),
        "can_book": has_perm(request, "appointment.manage"),
        "collection": billing_services.day_totals(request.branch)
        if has_perm(request, "billing.view")
        else None,
    }
    if request.htmx and request.htmx.target == "desk-live":
        return render(request, "queue/_desk_live.html", ctx)
    return render(request, "queue/desk.html", ctx)


class WalkInForm(StyledFormMixin, forms.Form):
    patient = forms.ModelChoiceField(queryset=Patient.objects.none(), widget=forms.HiddenInput)
    department = forms.ModelChoiceField(queryset=Department.objects.none())
    doctor = forms.ModelChoiceField(
        queryset=DoctorProfile.objects.none(),
        required=False,
        empty_label="Any doctor (department queue)",
    )
    priority = forms.ChoiceField(choices=QueueToken.Priority.choices)
    reason = forms.CharField(max_length=200, required=False, label="Reason for visit")

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["patient"].queryset = scoped(Patient, request).filter(is_active=True)
        self.fields["department"].queryset = scoped(Department, request).filter(is_active=True)
        self.fields["doctor"].queryset = _doctors(request)
        if "priority.special" not in request.sd_perms:
            self.fields["priority"].choices = [
                c for c in QueueToken.Priority.choices if c[0] != QueueToken.Priority.SPECIAL
            ]

    def clean(self):
        c = super().clean()
        if (
            c.get("doctor")
            and c.get("department")
            and c["doctor"].department_id != c["department"].id
        ):
            self.add_error("doctor", "This doctor is not in the chosen department.")
        return c


@permission_required(DESK)
def walk_in(request):
    """Walk-in: issue a token now. With a doctor, a walk-in visit is recorded on the patient too."""
    initial = {"priority": "normal"}
    if request.GET.get("patient"):
        initial["patient"] = request.GET["patient"]
    form = WalkInForm(request.POST or None, initial=initial, request=request)
    if request.method == "POST" and form.is_valid():
        c = form.cleaned_data
        try:
            with transaction.atomic():
                appt = None
                if c["doctor"]:
                    now = timezone.localtime()
                    appt = appt_services.book(
                        request=request,
                        patient=c["patient"],
                        doctor=c["doctor"],
                        on_date=now.date(),
                        start=now.time().replace(second=0, microsecond=0),
                        visit_type=Appointment.VisitType.EMERGENCY
                        if c["priority"] == QueueToken.Priority.EMERGENCY
                        else Appointment.VisitType.WALK_IN,
                        source=Appointment.Source.COUNTER,
                        reason=c["reason"],
                    )
                token = services.issue_token(
                    request=request,
                    patient=c["patient"],
                    department=c["department"],
                    doctor=c["doctor"],
                    appointment=appt,
                    priority=c["priority"],
                )
        except (services.QueueError, appt_services.BookingError) as err:
            form.add_error(None, err.message)
        else:
            audit.record(
                request, "queue.issue", token, summary=f"{token.label} walk-in {token.priority}"
            )
            messages.success(request, f"Token {token.label} issued to {token.patient.full_name}.")
            return redirect(f"{reverse('queue:desk')}?issued={token.pk}")
    patient = None
    pid = form["patient"].value()
    if pid and str(pid).isdigit():
        patient = scoped(Patient, request).filter(pk=pid).first()
    return render(request, "queue/walk_in.html", {"form": form, "patient": patient})


@require_GET
@permission_required(DESK, "consultation.view", any_of=True)
def token_slip(request, pk):
    token = get_scoped_or_404(
        QueueToken.objects.select_related("patient", "department", "doctor__user"), request, pk=pk
    )
    ahead = (
        services.waiting(
            services.queue_for(request.branch, doctor=token.doctor)
            if token.doctor
            else _tokens(request).filter(department=token.department)
        )
        .filter(priority_rank__lte=token.priority_rank, queued_at__lt=token.queued_at)
        .count()
    )
    return render(request, "queue/slip.html", {"token": token, "ahead": ahead})


# --- Token actions -----------------------------------------------------------------------------


def _operator_doctor(request, doctor_id=None):
    """The doctor whose queue this user is operating.

    Reception (queue.manage) may operate any doctor's queue. A doctor (consultation.create) may only
    operate their own.
    """
    own = getattr(request.user, "doctor_profile", None)
    if doctor_id:
        doctor = get_object_or_404(_doctors(request), pk=doctor_id)
    else:
        doctor = own
    if doctor is None:
        raise Http404
    if has_perm(request, DESK):
        return doctor
    if has_perm(request, "consultation.create") and own is not None and own.pk == doctor.pk:
        return doctor
    raise PermissionDenied


def _token_for_operator(request, pk):
    token = get_scoped_or_404(QueueToken, request, pk=pk)
    if token.date != timezone.localdate():
        raise Http404
    if has_perm(request, DESK):
        return token
    own = getattr(request.user, "doctor_profile", None)
    in_my_queue = own is not None and (
        token.doctor_id == own.pk
        or (token.doctor_id is None and token.department_id == own.department_id)
    )
    if has_perm(request, "consultation.create") and in_my_queue:
        return token
    raise PermissionDenied


def _run(request, label, fn, token, default_url, **kwargs):
    before = token.status
    try:
        token = fn(token, **kwargs)
    except services.QueueError as err:
        messages.error(request, err.message)
    else:
        audit.record(request, f"queue.{label}", token, changes={"status": [before, token.status]})
        messages.success(request, f"{token.label}: {token.get_status_display().lower()}.")
    return _back(request, default_url)


@require_POST
def call_next(request):
    doctor = _operator_doctor(request, request.POST.get("doctor"))
    try:
        token = services.call_next(request=request, doctor=doctor)
    except services.QueueError as err:
        messages.error(request, err.message)
    else:
        audit.record(request, "queue.call", token, changes={"status": ["waiting", "called"]})
        messages.success(request, f"Calling {token.label} to {token.room or doctor}.")
    return _back(request, reverse("queue:doctor"))


@require_POST
def call(request, pk):
    token = _token_for_operator(request, pk)
    doctor = _operator_doctor(request, request.POST.get("doctor") or token.doctor_id)
    return _run(
        request, "call", services.call, token, reverse("queue:desk"), request=request, doctor=doctor
    )


@require_POST
def recall(request, pk):
    token = _token_for_operator(request, pk)
    return _run(request, "recall", services.recall, token, reverse("queue:desk"), request=request)


@require_POST
def skip(request, pk):
    return _run(
        request, "skip", services.skip, _token_for_operator(request, pk), reverse("queue:desk")
    )


@require_POST
def requeue(request, pk):
    return _run(
        request,
        "requeue",
        services.requeue,
        _token_for_operator(request, pk),
        reverse("queue:desk"),
    )


@require_POST
def start(request, pk):
    return _run(
        request, "start", services.start, _token_for_operator(request, pk), reverse("queue:doctor")
    )


@require_POST
def complete(request, pk):
    return _run(
        request,
        "complete",
        services.complete,
        _token_for_operator(request, pk),
        reverse("queue:doctor"),
    )


@require_POST
@permission_required(DESK)
def cancel(request, pk):
    token = get_scoped_or_404(QueueToken, request, pk=pk)
    return _run(
        request,
        "cancel",
        services.cancel,
        token,
        reverse("queue:desk"),
        reason=request.POST.get("reason", ""),
    )


# --- Doctor queue ------------------------------------------------------------------------------


@require_GET
@permission_required(DESK, "consultation.view", any_of=True)
def doctor_queue(request):
    """Doctor patient queue (design screen 94): current patient, next, waiting list, controls."""
    own = getattr(request.user, "doctor_profile", None)
    doctor = None
    if request.GET.get("doctor") and has_perm(request, DESK):
        doctor = _doctors(request).filter(pk=request.GET["doctor"]).first()
    elif own is not None and own.organization_id == request.organization.id:
        doctor = own
    ctx = {"doctor": doctor, "all_doctors": _doctors(request) if has_perm(request, DESK) else []}
    if doctor is not None:
        qs = services.queue_for(request.branch, doctor=doctor)
        ctx.update(
            {
                "current": services.current_for(request.branch, doctor),
                "waiting": services.waiting(qs),
                "skipped": qs.filter(status=Status.SKIPPED).order_by("skipped_at"),
                "done": qs.filter(status__in=[Status.COMPLETED, Status.CANCELLED]).order_by(
                    "-updated_at"
                )[:15],
                "is_own_queue": has_perm(request, "consultation.create")
                and own is not None
                and own.pk == doctor.pk,
                "can_operate": has_perm(request, DESK)
                or (
                    has_perm(request, "consultation.create")
                    and own is not None
                    and own.pk == doctor.pk
                ),
            }
        )
    template = (
        "queue/_doctor_live.html"
        if request.htmx and request.htmx.target == "doctor-live"
        else "queue/doctor.html"
    )
    return render(request, template, ctx)


# --- Counters ----------------------------------------------------------------------------------


class CounterForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Counter
        fields = ["code", "name", "assigned_staff"]

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.accounts.models import User

        self.fields["assigned_staff"].queryset = User.objects.filter(
            organization=request.organization, is_active=True
        )
        self.request = request

    def clean_code(self):
        code = self.cleaned_data["code"].strip()
        qs = Counter.objects.filter(branch=self.request.branch, code=code)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("Another counter in this branch uses this ID.")
        return code


@permission_required(DESK)
def counters(request):
    can_configure = has_perm(request, "admin.masterdata.manage")
    form = CounterForm(request.POST or None, request=request) if can_configure else None
    if request.method == "POST":
        if not can_configure:
            raise PermissionDenied
        if form.is_valid():
            counter = form.save(commit=False)
            counter.organization, counter.branch = request.organization, request.branch
            counter.save()
            audit.record(request, "counter.create", counter)
            messages.success(request, f"Counter {counter} added.")
            return redirect("queue:counters")
    branch = request.branch
    tokens = _tokens(request)
    clinics = []
    for dept in scoped(Department, request).filter(is_active=True):
        dt = tokens.filter(department=dept)
        if not dt.exists():
            continue
        current = (
            dt.filter(status__in=[Status.CALLED, Status.IN_CONSULTATION])
            .order_by("-called_at")
            .first()
        )
        clinics.append(
            {
                "department": dept,
                "current": current,
                "waiting": dt.filter(status=Status.WAITING).count(),
            }
        )
    return render(
        request,
        "queue/counters.html",
        {
            "counters": scoped(Counter, request).select_related("assigned_staff"),
            "open_count": scoped(Counter, request).filter(is_open=True, is_active=True).count(),
            "form": form,
            "clinics": clinics,
            "stats": services.stats(branch),
            "display_url": request.build_absolute_uri(
                reverse("queue:display", args=[branch.display_key])
            )
            if branch.display_key
            else "",
            "can_manage_display": has_perm(request, "admin.branches.manage"),
        },
    )


@require_POST
@permission_required(DESK)
def counter_toggle(request, pk):
    counter = get_scoped_or_404(Counter, request, pk=pk)
    counter.is_open = not counter.is_open
    if counter.is_open and counter.assigned_staff is None:
        counter.assigned_staff = request.user
    counter.save(update_fields=["is_open", "assigned_staff", "updated_at"])
    audit.record(request, "counter.open" if counter.is_open else "counter.close", counter)
    messages.success(
        request, f"Counter {counter.code} is now {'open' if counter.is_open else 'closed'}."
    )
    return redirect("queue:counters")


@require_POST
@permission_required("admin.branches.manage")
def display_key_regenerate(request):
    request.branch.regenerate_display_key()
    audit.record(request, "branch.display_key_regenerate", request.branch)
    messages.success(
        request,
        "New display link created. Update the waiting-room screens; the old link stopped working.",
    )
    return redirect("queue:counters")


# --- Public waiting-room display ---------------------------------------------------------------


def _display_branch(key):
    if not key or len(key) < 20:
        raise Http404
    branch = Branch.objects.filter(
        display_key=key, is_active=True, organization__is_active=True
    ).first()
    if branch is None:
        raise Http404
    return branch


def _display_context(branch):
    """Now serving per doctor, plus the next token. Token numbers and rooms only, never names."""
    today = timezone.localdate()
    tokens = QueueToken.objects.filter(branch=branch, date=today).select_related(
        "department", "doctor__user"
    )
    boards = []
    doctor_ids = tokens.exclude(doctor=None).values_list("doctor_id", flat=True).distinct()
    for doctor in DoctorProfile.objects.filter(pk__in=doctor_ids).select_related(
        "department", "user"
    ):
        current = (
            tokens.filter(doctor=doctor, status__in=[Status.CALLED, Status.IN_CONSULTATION])
            .order_by("-called_at")
            .first()
        )
        nxt = services.waiting(services.queue_for(branch, doctor=doctor)).first()
        if current or nxt:
            boards.append(
                {
                    "department": doctor.department.name,
                    "doctor": doctor.display_name,
                    "current": current,
                    "next": nxt,
                    "fresh": bool(
                        current
                        and current.called_at
                        and (timezone.now() - current.called_at).seconds < 20
                    ),
                }
            )
    from apps.organizations.models import preferences_for

    prefs = preferences_for(branch.organization)
    return {
        "branch": branch,
        "boards": boards,
        "now": timezone.localtime(),
        "today": today,
        "refresh_seconds": prefs.display_refresh_seconds,
        "show_doctor": prefs.display_show_doctor,
    }


@login_not_required
@require_GET
def display(request, key):
    branch = _display_branch(key)
    timezone.activate(branch.organization.timezone)
    ctx = _display_context(branch)
    template = (
        "queue/_display_live.html" if request.headers.get("HX-Request") else "queue/display.html"
    )
    response = render(request, template, ctx)
    response["Cache-Control"] = "no-store"
    response["X-Robots-Tag"] = "noindex"
    return response
