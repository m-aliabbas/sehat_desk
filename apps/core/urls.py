from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("health/live/", views.health_live, name="health-live"),
    path("health/ready/", views.health_ready, name="health-ready"),
    path("health/metrics/", views.health_metrics, name="health-metrics"),
    path("telemetry/client-error/", views.client_error, name="client-error"),
    path("menu/", views.menu, name="menu"),
    path("settings/system/", views.system_health, name="system"),
    path("settings/system/backup/", views.backup_now, name="backup-now"),
    path("settings/backups/", views.backup_settings, name="backups"),
    path("settings/system/gdrive/", views.gdrive_panel, name="gdrive-panel"),
    path("settings/system/gdrive/connect/", views.gdrive_connect, name="gdrive-connect"),
    path("settings/system/gdrive/disconnect/", views.gdrive_disconnect, name="gdrive-disconnect"),
    path("settings/system/gdrive/backup/", views.gdrive_backup_now, name="gdrive-backup"),
    path("settings/system/gdrive/list/", views.gdrive_restore_list, name="gdrive-restore-list"),
    path("settings/system/restore/upload/", views.restore_upload, name="restore-upload"),
    path("settings/system/restore/local/", views.restore_local, name="restore-local"),
    path("settings/system/restore/drive/", views.restore_drive, name="restore-drive"),
    path("modules/<slug:module>/", views.module_placeholder, name="module-placeholder"),
    path("design-system/", views.design_system, name="design-system"),
    path("design-system/table/", views.design_system_table, name="design-system-table"),
    path("design-system/modal/", views.design_system_modal, name="design-system-modal"),
    path("design-system/action/", views.design_system_action, name="design-system-action"),
]
