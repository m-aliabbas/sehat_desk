"""Laboratory screens: worklist, orders, sample collection, result entry, verification,
reports and the test catalogue."""

import mimetypes

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Prefetch, Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import has_perm, permission_required
from apps.audit import services as audit
from apps.core.pdf import weasyprint_html
from apps.core.scoping import get_scoped_or_404, scoped
from apps.documents.services import panel_context as document_panel
from apps.encounters import services as encounter_services
from apps.encounters.models import Encounter
from apps.patients.models import Patient

from . import services
from .barcode import code39_svg
from .forms import (
    AttachmentForm,
    LabOrderForm,
    LabPanelForm,
    LabTestForm,
    RangeFormSet,
    ReasonForm,
)
from .models import (
    LabAttachment,
    LabOrder,
    LabOrderItem,
    LabPanel,
    LabResult,
    LabSample,
    LabTest,
    LabTestCategory,
)

LAB_ANY = (
    "lab.order.create",
    "lab.sample.collect",
    "lab.result.enter",
    "lab.result.verify",
    "lab.catalog.manage",
)
ItemStatus = LabOrderItem.Status


def _can_view_results(request) -> bool:
    return has_perm(request, "consultation.view") or any(has_perm(request, c) for c in LAB_ANY)


def _next(request, fallback):
    nxt = request.POST.get("next", "")
    return nxt if url_has_allowed_host_and_scheme(nxt, {request.get_host()}) else fallback


def _crumbs(*extra):
    return [{"label": "Laboratory", "url": reverse("lab:worklist")}, *extra]


def _orders(request, *, all_branches=False):
    return scoped(LabOrder, request, all_branches=all_branches).select_related(
        "patient", "doctor__user", "invoice"
    )


def _get_order(request, pk, *, all_branches=False):
    return get_scoped_or_404(_orders(request, all_branches=all_branches), request, pk=pk)


def _items_with_results(order):
    return order.items.select_related("test", "sample", "panel").prefetch_related(
        Prefetch(
            "results",
            queryset=LabResult.objects.select_related(
                "entered_by", "verified_by", "critical_acknowledged_by"
            ),
        ),
        "attachments",
    )


# --- Worklist ----------------------------------------------------------------------------------

TABS = [
    ("collect", "Pending samples"),
    ("processing", "Processing"),
    ("results", "Result pending"),
    ("verify", "Verification pending"),
    ("released", "Released today"),
]


@require_GET
@permission_required(*LAB_ANY, any_of=True)
def worklist(request):
    tab = request.GET.get("tab", "collect")
    if tab not in dict(TABS):
        tab = "collect"
    q = request.GET.get("q", "").strip()
    priority = request.GET.get("priority", "")
    category = request.GET.get("category", "")
    today = timezone.localdate()

    items = LabOrderItem.objects.filter(
        order__organization=request.organization, order__branch=request.branch
    )
    counts = {
        "collect": scoped(LabSample, request)
        .filter(status=LabSample.Status.PENDING, order__cancelled_at__isnull=True)
        .count(),
        "processing": items.filter(status=ItemStatus.COLLECTED).count(),
        "results": items.filter(status=ItemStatus.PROCESSING).count(),
        "verify": items.filter(status=ItemStatus.RESULT_ENTERED).count(),
        "released": scoped(LabOrder, request).filter(released_at__date=today).count(),
    }

    def order_filter(qs, prefix=""):
        if q:
            qs = qs.filter(
                Q(**{f"{prefix}number__icontains": q})
                | Q(**{f"{prefix}patient__mrn__icontains": q})
                | Q(**{f"{prefix}patient__name_search__contains": q.lower()})
            )
        if priority:
            qs = qs.filter(**{f"{prefix}priority": priority})
        return qs

    ctx = {
        "tab": tab,
        "tabs": [(k, label, counts[k]) for k, label in TABS],
        "q": q,
        "priority": priority,
        "category": category,
        "priorities": LabOrder.Priority.choices,
        "categories": scoped(LabTestCategory, request),
        "critical": services.unacknowledged_critical(request.organization, branch=request.branch)[
            :10
        ],
        "can_order": has_perm(request, "lab.order.create"),
    }
    if tab == "collect":
        samples = (
            scoped(LabSample, request)
            .filter(status=LabSample.Status.PENDING, order__cancelled_at__isnull=True)
            .select_related("order__patient", "order__invoice", "recollection_of")
            .prefetch_related("items")
            .order_by("order__created_at")
        )
        samples = order_filter(samples, "order__")
        if category:
            samples = samples.filter(items__test__category_id=category).distinct()
        ctx["samples"] = samples[:200]
    elif tab == "released":
        ctx["orders"] = order_filter(_orders(request).filter(released_at__date=today))[:200]
    else:
        status = {
            "processing": ItemStatus.COLLECTED,
            "results": ItemStatus.PROCESSING,
            "verify": ItemStatus.RESULT_ENTERED,
        }[tab]
        orders = (
            _orders(request)
            .filter(items__status=status)
            .annotate(n=Count("items", filter=Q(items__status=status)))
            .order_by("-priority", "created_at")
        )
        if category:
            orders = orders.filter(items__test__category_id=category)
        ctx["orders"] = order_filter(orders).distinct()[:200]
        ctx["item_status"] = status
    return render(request, "lab/worklist.html", ctx)


# --- Orders ------------------------------------------------------------------------------------


def _catalogue(request):
    categories = scoped(LabTestCategory, request).prefetch_related(
        Prefetch("tests", queryset=LabTest.objects.filter(is_active=True))
    )
    return {
        "categories": [c for c in categories if c.tests.all()],
        "panels": scoped(LabPanel, request).filter(is_active=True).prefetch_related("tests"),
    }


@permission_required("lab.order.create")
def order_create(request):
    """Lab desk order (design screens 76–78): patient, tests, review."""
    initial = {}
    if request.GET.get("patient", "").isdigit():
        initial["patient"] = request.GET["patient"]
    form = LabOrderForm(request.POST or None, initial=initial, request=request)
    if request.method == "POST" and form.is_valid():
        c = form.cleaned_data
        try:
            order = services.create_order(
                request=request,
                patient=c["patient"],
                tests=c["tests"],
                panels=c["panels"],
                priority=c["priority"],
                clinical_notes=c["clinical_notes"],
                doctor=c["doctor"],
                source=LabOrder.Source.DESK,
            )
        except services.LabError as err:
            form.add_error(None, err.message)
        else:
            audit.record(
                request, "lab.order.create", order, summary=f"{order.number} {order.patient.mrn}"
            )
            messages.success(request, f"{order.number} created." + (
                f" Invoice {order.invoice.number} is ready for payment." if order.invoice else ""
            ))  # fmt: skip
            return redirect("lab:order-detail", pk=order.pk)
    patient = None
    pid = form["patient"].value()
    if pid and str(pid).isdigit():
        patient = scoped(Patient, request).filter(pk=pid).first()
    return render(
        request,
        "lab/order_create.html",
        {
            "form": form,
            "patient": patient,
            **_catalogue(request),
            "selected_tests": [str(v) for v in (form["tests"].value() or [])],
            "selected_panels": [str(v) for v in (form["panels"].value() or [])],
            "breadcrumbs": _crumbs({"label": "New lab order"}),
        },
    )


@require_POST
@permission_required("lab.order.create")
def encounter_order(request, pk):
    """Doctor orders tests from the consultation workspace."""
    encounter = get_scoped_or_404(Encounter, request, pk=pk)
    back = f"{reverse('encounters:detail', args=[pk])}#sec-lab"
    if not (encounter.is_draft and encounter_services.can_write(request, encounter)):
        messages.error(request, "Only the treating doctor can order tests on an open consultation.")
        return redirect(back)
    form = LabOrderForm(request.POST, request=request, with_patient=False)
    if not form.is_valid():
        messages.error(request, " ".join(form.non_field_errors()) or "Check the lab order.")
        return redirect(back)
    c = form.cleaned_data
    try:
        order = services.create_order(
            request=request,
            patient=encounter.patient,
            tests=c["tests"],
            panels=c["panels"],
            priority=c["priority"],
            clinical_notes=c["clinical_notes"],
            encounter=encounter,
            source=LabOrder.Source.DOCTOR,
        )
    except services.LabError as err:
        messages.error(request, err.message)
    else:
        audit.record(
            request, "lab.order.create", order, summary=f"{order.number} from {encounter.number}"
        )
        messages.success(request, f"{order.number} sent to the lab ({order.items.count()} tests).")
    return redirect(back)


@require_GET
def order_detail(request, pk):
    if not _can_view_results(request):
        raise PermissionDenied
    order = _get_order(request, pk, all_branches=has_perm(request, "consultation.view"))
    audit.record(request, "lab.order.view", order, summary=order.number)
    items = list(_items_with_results(order))
    samples = order.samples.select_related("collected_by", "rejected_by").prefetch_related("items")
    return render(
        request,
        "lab/order_detail.html",
        {
            "order": order,
            "items": items,
            "samples": samples,
            "can_collect": has_perm(request, "lab.sample.collect") and order.is_open,
            "can_enter": has_perm(request, "lab.result.enter") and order.is_open,
            "can_verify": has_perm(request, "lab.result.verify"),
            "can_cancel": has_perm(request, "lab.order.create") and order.is_open,
            "is_lab_branch": order.branch_id == request.branch.pk,
            "breadcrumbs": _crumbs({"label": order.number}),
            **document_panel(request, order.patient, lab_order=order),
        },
    )


@require_POST
@permission_required("lab.order.create")
def order_cancel(request, pk):
    order = _get_order(request, pk)
    form = ReasonForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give the reason for cancelling.")
        return redirect("lab:order-detail", pk=pk)
    try:
        services.cancel_order(order, request=request, reason=form.cleaned_data["reason"])
    except services.LabError as err:
        messages.error(request, err.message)
    else:
        audit.record(request, "lab.order.cancel", order, summary=form.cleaned_data["reason"])
        messages.success(request, f"{order.number} cancelled.")
    return redirect("lab:order-detail", pk=pk)


# --- Samples -----------------------------------------------------------------------------------


@require_POST
@permission_required("lab.sample.collect")
def sample_collect(request, pk):
    sample = get_scoped_or_404(LabSample, request, pk=pk)
    reason = request.POST.get("unpaid_reason", "")
    try:
        services.collect_sample(sample, request=request, unpaid_reason=reason)
    except services.LabError as err:
        messages.error(request, err.message)
    else:
        audit.record(
            request,
            "lab.sample.collect",
            sample,
            summary=sample.accession + (f" before payment: {reason[:120]}" if reason else ""),
        )
        messages.success(request, f"Sample {sample.accession} collected. Print the label.")
    return redirect(_next(request, reverse("lab:order-detail", args=[sample.order_id])))


@require_POST
@permission_required("lab.sample.collect")
def sample_reject(request, pk):
    sample = get_scoped_or_404(LabSample, request, pk=pk)
    form = ReasonForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give the rejection reason.")
    else:
        try:
            new = services.reject_sample(
                sample, request=request, reason=form.cleaned_data["reason"]
            )
        except services.LabError as err:
            messages.error(request, err.message)
        else:
            audit.record(request, "lab.sample.reject", sample, summary=form.cleaned_data["reason"])
            messages.warning(request, f"{sample.accession} rejected. Recollect as {new.accession}.")
    return redirect("lab:order-detail", pk=sample.order_id)


@require_GET
def order_print(request, pk):
    """A5 lab order (requisition) form: tests, samples, priority and payment (Phase 24)."""
    if not (_can_view_results(request) or has_perm(request, "lab.order.create")):
        raise PermissionDenied
    order = _get_order(request, pk, all_branches=has_perm(request, "consultation.view"))
    items = order.items.select_related("test", "sample", "panel").order_by("sample_id", "pk")
    return render(request, "lab/order_print.html", {"order": order, "items": items})


@require_GET
@permission_required(*LAB_ANY, any_of=True)
def sample_label(request, pk):
    sample = get_scoped_or_404(LabSample.objects.select_related("order__patient"), request, pk=pk)
    return render(
        request,
        "lab/sample_label.html",
        {
            "sample": sample,
            "barcode": code39_svg(sample.accession),
            "tests": sample.items.exclude(status=ItemStatus.CANCELLED),
        },
    )


# --- Results -----------------------------------------------------------------------------------


@require_POST
@permission_required("lab.result.enter")
def order_start(request, pk):
    order = _get_order(request, pk)
    n = services.start_processing(order, request=request)
    messages.success(request, f"{n} test{'s' if n != 1 else ''} moved to processing.")
    return redirect("lab:results", pk=pk)


@permission_required("lab.result.enter")
def results(request, pk):
    """Result entry (design screens 83–84). Blank rows are left as they are."""
    order = _get_order(request, pk)
    items = [i for i in _items_with_results(order) if i.status in services.ENTERABLE]
    errors = {}
    if request.method == "POST":
        saved = 0
        for item in items:
            value = request.POST.get(f"value_{item.pk}", "").strip()
            if not value:
                continue
            try:
                result = services.enter_result(
                    item,
                    request=request,
                    value=value,
                    interpretation=request.POST.get(f"interp_{item.pk}", ""),
                )
            except services.LabError as err:
                errors[item.pk] = err.message
                continue
            saved += 1
            audit.record(
                request,
                "lab.result.enter",
                item,
                summary=f"{order.number} {item.test.code} v{result.version} {result.flag or '-'}",
            )
        if not errors:
            order = services.refresh_status(order)
            if order.has_critical:
                messages.warning(
                    request,
                    "Critical result recorded. Inform the ordering doctor now.",
                )
            messages.success(request, f"{saved} result{'s' if saved != 1 else ''} saved.")
            return redirect("lab:order-detail", pk=pk)
        messages.error(request, "Some results were not saved. Check the highlighted rows.")
        items = [i for i in _items_with_results(order) if i.status in services.ENTERABLE]
    rows = []
    for item in items:
        current = item.current_result
        rng = item.test.range_for(order.patient)
        rows.append(
            {
                "item": item,
                "current": current,
                "range": rng.label if rng else item.test.normal_value,
                "value": request.POST.get(f"value_{item.pk}")
                if request.method == "POST"
                else (current.display_value if current else ""),
                "interp": request.POST.get(f"interp_{item.pk}")
                if request.method == "POST"
                else (current.interpretation if current else ""),
                "error": errors.get(item.pk, ""),
            }
        )
    return render(
        request,
        "lab/results.html",
        {
            "order": order,
            "rows": rows,
            "attachment_form": AttachmentForm(),
            "breadcrumbs": _crumbs(
                {"label": order.number, "url": reverse("lab:order-detail", args=[pk])},
                {"label": "Results"},
            ),
        },
    )


@require_POST
@permission_required("lab.result.enter")
def attachment_upload(request, pk):
    item = _get_item(request, pk)
    form = AttachmentForm(request.POST, request.FILES)
    if form.is_valid():
        upload = form.cleaned_data["file"]
        from apps.documents.services import DocumentError, validate_upload

        try:
            validate_upload(upload, limit=10 * 1024 * 1024)
        except DocumentError as err:
            messages.error(request, err.message)
            return redirect(_next(request, reverse("lab:order-detail", args=[item.order_id])))
        att = LabAttachment(
            item=item, file=upload, original_name=upload.name[:200], uploaded_by=request.user
        )
        try:
            att.full_clean()
        except Exception as err:  # noqa: BLE001 - shown to the user
            messages.error(request, " ".join(getattr(err, "messages", [str(err)])))
        else:
            att.save()
            audit.record(request, "lab.attachment.add", item, summary=att.original_name)
            messages.success(request, f"Attached {att.original_name}.")
    else:
        messages.error(request, "Choose a file to attach.")
    return redirect(_next(request, reverse("lab:order-detail", args=[item.order_id])))


def _get_item(request, pk):
    item = (
        LabOrderItem.objects.select_related("order", "test")
        .filter(pk=pk, order__organization=request.organization, order__branch=request.branch)
        .first()
    )
    if item is None:
        raise Http404
    return item


@require_GET
def attachment_download(request, pk):
    if not _can_view_results(request):
        raise PermissionDenied
    att = (
        LabAttachment.objects.select_related("item__order")
        .filter(pk=pk, item__order__organization=request.organization)
        .first()
    )
    if att is None:
        raise Http404
    audit.record(request, "lab.attachment.view", att.item, summary=att.original_name)
    content_type = mimetypes.guess_type(att.original_name)[0] or "application/octet-stream"
    return FileResponse(att.file.open("rb"), content_type=content_type, filename=att.original_name)


# --- Verification and release -----------------------------------------------------------------


@require_POST
@permission_required("lab.result.verify")
def verify(request, pk):
    order = _get_order(request, pk)
    ids = [int(i) for i in request.POST.getlist("items") if i.isdigit()]
    items = (
        order.items.filter(pk__in=ids)
        if ids
        else order.items.filter(status=ItemStatus.RESULT_ENTERED)
    )
    n = services.verify(items, request=request)
    if n:
        audit.record(request, "lab.result.verify", order, summary=f"{order.number}: {n} results")
        messages.success(request, f"{n} result{'s' if n != 1 else ''} verified.")
    else:
        messages.error(request, "No entered results to verify.")
    order = services.refresh_status(order)
    if request.POST.get("release") and order.status == LabOrder.Status.VERIFIED:
        return release(request, pk)
    return redirect("lab:order-detail", pk=pk)


@require_POST
@permission_required("lab.result.verify")
def release(request, pk):
    order = _get_order(request, pk)
    try:
        services.release(order, request=request)
    except services.LabError as err:
        messages.error(request, err.message)
    else:
        audit.record(request, "lab.report.release", order, summary=order.number)
        messages.success(request, f"Report {order.number} released.")
    return redirect("lab:order-detail", pk=pk)


@permission_required("lab.result.verify")
def amend(request, pk):
    """Correct a verified result (design screen 89). Keeps every earlier version."""
    item = _get_item(request, pk)
    order = item.order
    current = item.results.filter(is_current=True).first()
    error = ""
    if request.method == "POST":
        try:
            result = services.amend(
                item,
                request=request,
                value=request.POST.get("value", ""),
                interpretation=request.POST.get("interpretation", ""),
                reason=request.POST.get("reason", ""),
            )
        except services.LabError as err:
            error = err.message
        else:
            audit.record(
                request,
                "lab.result.amend",
                item,
                changes={"version": [result.version - 1, result.version]},
                summary=f"{order.number} {item.test.code}: {result.amendment_reason[:150]}",
            )
            messages.success(request, "Amendment saved. Verify and release the report again.")
            return redirect("lab:order-detail", pk=order.pk)
    return render(
        request,
        "lab/amend.html",
        {
            "item": item,
            "order": order,
            "current": current,
            "history": item.results.select_related("entered_by", "verified_by"),
            "error": error,
            "breadcrumbs": _crumbs(
                {"label": order.number, "url": reverse("lab:order-detail", args=[order.pk])},
                {"label": f"Amend {item.test_name}"},
            ),
        },
    )


@require_POST
def acknowledge(request, pk):
    result = (
        LabResult.objects.select_related("item__order")
        .filter(pk=pk, item__order__organization=request.organization)
        .first()
    )
    if result is None:
        raise Http404
    try:
        services.acknowledge_critical(result, request=request)
    except services.LabError as err:
        messages.error(request, err.message)
    else:
        audit.record(
            request, "lab.critical.acknowledge", result.item, summary=result.item.order.number
        )
        messages.success(request, "Critical result acknowledged.")
    return redirect(_next(request, reverse("lab:order-detail", args=[result.item.order_id])))


# --- Report ------------------------------------------------------------------------------------


@require_GET
def report(request, pk):
    if not _can_view_results(request):
        raise PermissionDenied
    order = _get_order(request, pk, all_branches=True)
    released = order.status == LabOrder.Status.RELEASED
    lab_staff = ("lab.sample.collect", "lab.result.enter", "lab.result.verify")
    if not released and not any(has_perm(request, c) for c in lab_staff):
        raise Http404  # clinicians see released reports only; lab staff can preview
    items = [i for i in _items_with_results(order) if i.status != ItemStatus.CANCELLED]
    collected = [s.collected_at for s in order.samples.all() if s.collected_at]
    ctx = {
        "order": order,
        "patient": order.patient,
        "items": items,
        "released": released,
        "sample_date": min(collected) if collected else None,
        "technicians": sorted({str(r.entered_by) for i in items if (r := i.current_result)}),
        "verifiers": sorted(
            {str(r.verified_by) for i in items if (r := i.current_result) and r.verified_by}
        ),
        "organization": request.organization,
    }
    audit.record(request, "lab.report.view", order, summary=order.number)
    if request.GET.get("format") == "pdf":
        html = render_to_string("lab/report.html", {**ctx, "pdf": True}, request=request)
        HTML = weasyprint_html()
        if HTML is None:
            messages.error(
                request,
                "PDF export needs the WeasyPrint system libraries on this computer. "
                "Use Print → Save as PDF instead.",
            )
            return redirect("lab:report", pk=pk)
        pdf = HTML(string=html, base_url=request.build_absolute_uri("/")).write_pdf()
        resp = HttpResponse(pdf, content_type="application/pdf")
        resp["Content-Disposition"] = f'inline; filename="{order.number}.pdf"'
        return resp
    return render(request, "lab/report.html", ctx)


# --- Catalogue ---------------------------------------------------------------------------------

CATALOG = "lab.catalog.manage"


@permission_required(CATALOG)
def catalog(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()[:80]
        if name and not scoped(LabTestCategory, request).filter(name__iexact=name).exists():
            cat = LabTestCategory.objects.create(organization=request.organization, name=name)
            audit.record(request, "lab.category.create", cat)
            messages.success(request, f"Category “{name}” added.")
        else:
            messages.error(request, "Enter a new category name.")
        return redirect("lab:catalog")
    q = request.GET.get("q", "").strip()
    tests = scoped(LabTest, request).select_related("category").prefetch_related("ranges")
    if q:
        tests = tests.filter(Q(code__icontains=q) | Q(name__icontains=q))
    return render(
        request,
        "lab/catalog.html",
        {
            "tests": tests,
            "q": q,
            "categories": scoped(LabTestCategory, request).annotate(n=Count("tests")),
            "panels": scoped(LabPanel, request).prefetch_related("tests"),
            "breadcrumbs": _crumbs({"label": "Test catalogue"}),
        },
    )


@permission_required(CATALOG)
def test_form(request, pk=None):
    test = (
        get_scoped_or_404(LabTest, request, pk=pk)
        if pk
        else LabTest(organization=request.organization)
    )
    creating = pk is None
    form = LabTestForm(request.POST or None, instance=test, request=request)
    ranges = RangeFormSet(request.POST or None, instance=test, prefix="ranges")
    if request.method == "POST" and form.is_valid() and ranges.is_valid():
        test = form.save()
        ranges.instance = test
        ranges.save()
        audit.record(request, "lab.test.create" if creating else "lab.test.update", test)
        messages.success(request, f"{test} saved.")
        return redirect("lab:catalog")
    if not scoped(LabTestCategory, request).exists():
        messages.info(request, "Add a test category first (for example Haematology).")
    return render(
        request,
        "lab/test_form.html",
        {
            "form": form,
            "ranges": ranges,
            "test": test,
            "creating": creating,
            "breadcrumbs": _crumbs(
                {"label": "Test catalogue", "url": reverse("lab:catalog")},
                {"label": "New test" if creating else test.code},
            ),
        },
    )


@require_POST
@permission_required(CATALOG)
def test_toggle(request, pk):
    test = get_scoped_or_404(LabTest, request, pk=pk)
    test.is_active = not test.is_active
    test.save(update_fields=["is_active", "updated_at"])
    audit.record(request, f"lab.test.{'activate' if test.is_active else 'deactivate'}", test)
    return redirect("lab:catalog")


@permission_required(CATALOG)
def panel_form(request, pk=None):
    panel = (
        get_scoped_or_404(LabPanel, request, pk=pk)
        if pk
        else LabPanel(organization=request.organization)
    )
    creating = pk is None
    form = LabPanelForm(request.POST or None, instance=panel, request=request)
    if request.method == "POST" and form.is_valid():
        panel = form.save()
        audit.record(request, "lab.panel.create" if creating else "lab.panel.update", panel)
        messages.success(request, f"{panel} saved.")
        return redirect("lab:catalog")
    return render(
        request,
        "lab/panel_form.html",
        {
            "form": form,
            "creating": creating,
            "breadcrumbs": _crumbs(
                {"label": "Test catalogue", "url": reverse("lab:catalog")},
                {"label": "New panel" if creating else panel.code},
            ),
        },
    )


@require_POST
@permission_required(CATALOG)
def panel_toggle(request, pk):
    panel = get_scoped_or_404(LabPanel, request, pk=pk)
    panel.is_active = not panel.is_active
    panel.save(update_fields=["is_active", "updated_at"])
    audit.record(request, f"lab.panel.{'activate' if panel.is_active else 'deactivate'}", panel)
    return redirect("lab:catalog")
