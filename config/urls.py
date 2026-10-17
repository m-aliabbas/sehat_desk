from django.conf import settings
from django.urls import include, path, re_path
from django.views.static import serve

urlpatterns = [
    path("", include("apps.accounts.urls")),
    path("settings/", include("apps.organizations.urls")),
    path("settings/", include("apps.masterdata.urls")),
    path("patients/", include("apps.patients.urls")),
    path("appointments/", include("apps.appointments.urls")),
    path("", include("apps.queue.urls")),
    path("", include("apps.billing.urls")),
    path("", include("apps.encounters.urls")),
    path("", include("apps.vitals.urls")),
    path("", include("apps.prescriptions.urls")),
    path("", include("apps.lab.urls")),
    path("", include("apps.pharmacy.urls")),
    path("", include("apps.ipd.urls")),
    path("", include("apps.nursing.urls")),
    path("", include("apps.documents.urls")),
    path("", include("apps.notifications.urls")),
    path("", include("apps.reports.urls")),
    path("", include("apps.search.urls")),
    path("", include("apps.procedures.urls")),
    path("", include("apps.hr.urls")),
    path("", include("apps.finance.urls")),
    path("", include("apps.audit.urls")),
    path("", include("apps.core.urls")),
    # Uploaded files (logos now, documents later). There is no separate web server on the
    # hospital machine, so Django serves them, and LoginRequiredMiddleware keeps them private.
    # Only logos are served here. Patient photos and documents go through permission-checked views.
    re_path(
        r"^media/(?P<path>organization-logos/.*)$", serve, {"document_root": settings.MEDIA_ROOT}
    ),
]

handler500 = "apps.core.views.server_error"
