from django.urls import path

from . import views

app_name = "organizations"

urlpatterns = [
    path("", views.settings_home, name="settings"),
    path("hospital/", views.hospital_settings, name="hospital"),
    path("preferences/", views.preferences, name="preferences"),
    path("branches/", views.branch_list, name="branch-list"),
    path("branches/new/", views.branch_create, name="branch-create"),
    path("branches/<int:pk>/", views.branch_edit, name="branch-edit"),
    path("branches/<int:pk>/status/", views.branch_toggle_active, name="branch-toggle-active"),
]
