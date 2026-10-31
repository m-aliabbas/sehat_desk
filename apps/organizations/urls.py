from django.urls import path

from . import views

app_name = "organizations"

urlpatterns = [
    path("print-layouts/", views.print_layouts, name="print-layouts"),
    path("print-layouts/preview/", views.print_layout_preview, name="print-layout-preview"),
    path("", views.settings_home, name="settings"),
    path("hospital/", views.hospital_settings, name="hospital"),
    path("preferences/", views.preferences, name="preferences"),
    path("fields/", views.field_rules_page, name="field-rules"),
    path("modules/", views.modules, name="modules"),
    path("branches/", views.branch_list, name="branch-list"),
    path("branches/new/", views.branch_create, name="branch-create"),
    path("branches/<int:pk>/", views.branch_edit, name="branch-edit"),
    path("branches/<int:pk>/status/", views.branch_toggle_active, name="branch-toggle-active"),
]
