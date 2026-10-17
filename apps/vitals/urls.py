from django.urls import path

from . import views

app_name = "vitals"

urlpatterns = [
    path("vitals/", views.station, name="station"),
    path("vitals/record/", views.record, name="record"),
    path("vitals/<int:pk>/error/", views.mark_error, name="error"),
]
