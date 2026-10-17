from django.urls import path

from . import views

app_name = "audit"

urlpatterns = [
    path("settings/audit/", views.log, name="log"),
    path("settings/audit/<int:pk>/", views.entry, name="entry"),
]
