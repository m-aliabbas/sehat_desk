from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("health/live/", views.health_live, name="health-live"),
    path("health/ready/", views.health_ready, name="health-ready"),
    path("health/metrics/", views.health_metrics, name="health-metrics"),
    path("telemetry/client-error/", views.client_error, name="client-error"),
    path("settings/system/", views.system_health, name="system"),
    path("settings/system/backup/", views.backup_now, name="backup-now"),
    path("modules/<slug:module>/", views.module_placeholder, name="module-placeholder"),
    path("design-system/", views.design_system, name="design-system"),
    path("design-system/table/", views.design_system_table, name="design-system-table"),
    path("design-system/modal/", views.design_system_modal, name="design-system-modal"),
    path("design-system/action/", views.design_system_action, name="design-system-action"),
]
