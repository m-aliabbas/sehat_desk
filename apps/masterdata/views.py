"""Settings → Departments, Doctors, Services (permission: admin.masterdata.manage)."""

from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.decorators import permission_required
from apps.audit import services as audit
from apps.core.scoping import get_scoped_or_404, scoped
from apps.organizations.models import Branch

from .forms import (
    DepartmentForm,
    DoctorLeaveForm,
    DoctorProfileForm,
    ScheduleFormSet,
    ServiceCategoryForm,
    ServiceForm,
    SpecialtyForm,
)
from .models import (
    WEEKDAYS,
    Department,
    DoctorLeave,
    DoctorProfile,
    DoctorSchedule,
    Service,
    ServiceBranchPrice,
    ServiceCategory,
    Specialty,
)

MANAGE = "admin.masterdata.manage"


def _crumbs(section, url, *extra):
    return [{"label": "Settings", "url": "/settings/"}, {"label": section, "url": url}, *extra]


def _toggle(request, obj, label):
    obj.is_active = not obj.is_active
    obj.save(update_fields=["is_active", "updated_at"])
    audit.record(request, f"{label}.{'activate' if obj.is_active else 'deactivate'}", obj)
    messages.success(request, f"“{obj}” is now {'active' if obj.is_active else 'inactive'}.")


# --- Departments and specialties -------------------------------------------------------


@permission_required(MANAGE)
def department_list(request):
    departments = scoped(Department, request)
    return render(
        request,
        "masterdata/department_list.html",
        {
            "departments": departments,
            "specialties": scoped(Specialty, request),
            "specialty_form": SpecialtyForm(organization=request.organization),
            "settings_tab": "departments",
        },
    )


@permission_required(MANAGE)
def department_form(request, pk=None):
    dept = (
        get_scoped_or_404(Department, request, pk=pk)
        if pk
        else Department(organization=request.organization)
    )
    creating = pk is None
    form = DepartmentForm(request.POST or None, instance=dept, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        dept = form.save()
        audit.record(request, "department.create" if creating else "department.update", dept)
        messages.success(request, f"Department “{dept.name}” saved.")
        return redirect("masterdata:department-list")
    return render(
        request,
        "masterdata/simple_form.html",
        {
            "form": form,
            "title": "New department" if creating else f"Edit {dept.name}",
            "cancel_url": "/settings/departments/",
            "settings_tab": "departments",
            "breadcrumbs": _crumbs("Departments", "/settings/departments/", {"label": "Edit"}),
        },
    )


@require_POST
@permission_required(MANAGE)
def department_toggle(request, pk):
    _toggle(request, get_scoped_or_404(Department, request, pk=pk), "department")
    return redirect("masterdata:department-list")


@require_POST
@permission_required(MANAGE)
def specialty_create(request):
    form = SpecialtyForm(request.POST, organization=request.organization)
    if form.is_valid():
        spec = form.save(commit=False)
        spec.organization = request.organization
        spec.save()
        audit.record(request, "specialty.create", spec)
        messages.success(request, f"Specialty “{spec.name}” added.")
    else:
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
    return redirect("masterdata:department-list")


# --- Doctors -------------------------------------------------------------------------------


@permission_required(MANAGE)
def doctor_list(request):
    doctors = scoped(DoctorProfile, request).select_related("user", "department", "specialty")
    return render(
        request, "masterdata/doctor_list.html", {"doctors": doctors, "settings_tab": "doctors"}
    )


def _new_doctor(organization):
    """New doctors start from Settings → Preferences (slot length, follow-up window)."""
    from apps.organizations.models import preferences_for

    prefs = preferences_for(organization)
    return DoctorProfile(
        organization=organization,
        slot_minutes=prefs.default_slot_minutes,
        follow_up_days=prefs.default_follow_up_days,
    )


@permission_required(MANAGE)
def doctor_form(request, pk=None):
    doctor = (
        get_scoped_or_404(DoctorProfile, request, pk=pk)
        if pk
        else _new_doctor(request.organization)
    )
    creating = pk is None
    form = DoctorProfileForm(
        request.POST or None, instance=doctor, organization=request.organization
    )
    if request.method == "POST" and form.is_valid():
        doctor = form.save()
        audit.record(request, "doctor.create" if creating else "doctor.update", doctor)
        messages.success(request, f"{doctor} saved.")
        return redirect("masterdata:doctor-detail", pk=doctor.pk)
    return render(
        request,
        "masterdata/doctor_form.html",
        {
            "form": form,
            "doctor": doctor,
            "creating": creating,
            "settings_tab": "doctors",
            "breadcrumbs": _crumbs(
                "Doctors", "/settings/doctors/", {"label": "Add doctor" if creating else "Edit"}
            ),
        },
    )


@permission_required(MANAGE)
def doctor_detail(request, pk):
    """Profile summary, weekly schedule for a chosen branch, and leave."""
    doctor = get_scoped_or_404(DoctorProfile.objects.select_related("user"), request, pk=pk)
    branches = list(Branch.objects.filter(organization=request.organization, is_active=True))
    branch = next(
        (b for b in branches if str(b.pk) == request.GET.get("branch", "")), request.branch
    )
    existing = {s.weekday: s for s in doctor.schedules.filter(branch=branch)}

    if request.method == "POST" and "save_schedule" in request.POST:
        formset = ScheduleFormSet(request.POST, prefix="days")
        if formset.is_valid():
            with transaction.atomic():
                for f in formset:
                    c = f.cleaned_data
                    sched = existing.get(c["weekday"])
                    if not c["enabled"]:
                        if sched and sched.is_active:
                            sched.is_active = False
                            sched.save(update_fields=["is_active", "updated_at"])
                        continue
                    sched = sched or DoctorSchedule(
                        doctor=doctor, branch=branch, weekday=c["weekday"]
                    )
                    for field in ("start_time", "end_time", "break_start", "break_end", "room"):
                        setattr(sched, field, c[field])
                    sched.is_active = True
                    sched.save()
            audit.record(request, "doctor.schedule_update", doctor, summary=f"branch={branch.code}")
            messages.success(request, f"Schedule for {branch.name} saved.")
            return redirect(f"{request.path}?branch={branch.pk}")
    else:
        initial = []
        for day, _ in WEEKDAYS:
            s = existing.get(day)
            initial.append(
                {
                    "weekday": day,
                    "enabled": bool(s and s.is_active),
                    "start_time": s.start_time if s else None,
                    "end_time": s.end_time if s else None,
                    "break_start": s.break_start if s else None,
                    "break_end": s.break_end if s else None,
                    "room": s.room if s else "",
                }
            )
        formset = ScheduleFormSet(initial=initial, prefix="days")

    rows = [(dict(WEEKDAYS)[int(f["weekday"].value())], f) for f in formset]
    return render(
        request,
        "masterdata/doctor_detail.html",
        {
            "doctor": doctor,
            "branches": branches,
            "schedule_branch": branch,
            "formset": formset,
            "rows": rows,
            "leaves": doctor.leaves.all()[:20],
            "leave_form": DoctorLeaveForm(),
            "settings_tab": "doctors",
            "breadcrumbs": _crumbs("Doctors", "/settings/doctors/", {"label": str(doctor)}),
        },
    )


@require_POST
@permission_required(MANAGE)
def doctor_toggle(request, pk):
    doctor = get_scoped_or_404(DoctorProfile, request, pk=pk)
    _toggle(request, doctor, "doctor")
    return redirect("masterdata:doctor-detail", pk=pk)


@require_POST
@permission_required(MANAGE)
def leave_add(request, pk):
    doctor = get_scoped_or_404(DoctorProfile, request, pk=pk)
    form = DoctorLeaveForm(request.POST)
    if form.is_valid():
        leave = form.save(commit=False)
        leave.doctor = doctor
        leave.created_by = request.user
        leave.save()
        audit.record(
            request, "doctor.leave_add", doctor, summary=f"{leave.start_date}..{leave.end_date}"
        )
        # Existing bookings are not cancelled automatically; reception reschedules them.
        from apps.appointments.models import Appointment

        affected = (
            Appointment.objects.active()
            .filter(doctor=doctor, date__range=(leave.start_date, leave.end_date))
            .count()
        )
        msg = "Leave added."
        if affected:
            msg += f" {affected} existing appointment(s) fall on these dates and need rescheduling."
            messages.warning(request, msg)
        else:
            messages.success(request, msg)
    else:
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
    return redirect("masterdata:doctor-detail", pk=pk)


@require_POST
@permission_required(MANAGE)
def leave_remove(request, pk, leave_pk):
    doctor = get_scoped_or_404(DoctorProfile, request, pk=pk)
    leave = DoctorLeave.objects.filter(doctor=doctor, pk=leave_pk).first()
    if leave:
        audit.record(
            request, "doctor.leave_remove", doctor, summary=f"{leave.start_date}..{leave.end_date}"
        )
        leave.delete()
        messages.success(request, "Leave removed.")
    return redirect("masterdata:doctor-detail", pk=pk)


# --- Services ------------------------------------------------------------------------------


@permission_required(MANAGE)
def service_list(request):
    services = scoped(Service, request).select_related("category", "department")
    q = request.GET.get("q", "").strip()
    if q:
        services = services.filter(name__icontains=q) | services.filter(code__icontains=q.upper())
    kind = request.GET.get("kind", "")
    if kind == "procedure":
        services = services.filter(is_procedure=True)
    elif kind == "service":
        services = services.filter(is_procedure=False)
    return render(
        request,
        "masterdata/service_list.html",
        {
            "services": services,
            "q": q,
            "kind": kind,
            "categories": scoped(ServiceCategory, request),
            "category_form": ServiceCategoryForm(organization=request.organization),
            "settings_tab": "services",
        },
    )


@permission_required(MANAGE)
def service_form(request, pk=None):
    service = (
        get_scoped_or_404(Service, request, pk=pk)
        if pk
        else Service(organization=request.organization)
    )
    creating = pk is None
    branches = Branch.objects.filter(organization=request.organization, is_active=True)
    form = ServiceForm(
        request.POST or None,
        instance=service,
        organization=request.organization,
        branches=branches,
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            service = form.save()
            for b in form.branches:
                price = form.cleaned_data.get(f"branch_price_{b.pk}")
                if price is None:
                    ServiceBranchPrice.objects.filter(service=service, branch=b).delete()
                else:
                    ServiceBranchPrice.objects.update_or_create(
                        service=service, branch=b, defaults={"price": price}
                    )
            audit.record(
                request,
                "service.create" if creating else "service.update",
                service,
                summary=f"price={service.price}",
            )
        messages.success(request, f"Service “{service.name}” saved.")
        return redirect("masterdata:service-list")
    return render(
        request,
        "masterdata/service_form.html",
        {
            "form": form,
            "service": service,
            "creating": creating,
            "settings_tab": "services",
            "breadcrumbs": _crumbs(
                "Services", "/settings/services/", {"label": "New service" if creating else "Edit"}
            ),
        },
    )


@require_POST
@permission_required(MANAGE)
def service_toggle(request, pk):
    _toggle(request, get_scoped_or_404(Service, request, pk=pk), "service")
    return redirect("masterdata:service-list")


@require_POST
@permission_required(MANAGE)
def category_create(request):
    form = ServiceCategoryForm(request.POST, organization=request.organization)
    if form.is_valid():
        cat = form.save(commit=False)
        cat.organization = request.organization
        cat.save()
        audit.record(request, "service_category.create", cat)
        messages.success(request, f"Category “{cat.name}” added.")
    else:
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
    return redirect("masterdata:service-list")
