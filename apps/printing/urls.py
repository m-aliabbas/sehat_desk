from django.urls import path

from . import views

app_name = "printing"

urlpatterns = [
    path("print/jobs/", views.job_create, name="job-create"),
    path("print/jobs/<int:pk>/", views.job_status, name="job-status"),
    path("print/agent/join/", views.agent_join, name="agent-join"),
    path("print/agent/hello/", views.agent_hello, name="agent-hello"),
    path("print/agent/next/", views.agent_next, name="agent-next"),
    path("print/agent/jobs/<int:pk>/", views.agent_finish, name="agent-finish"),
    path("settings/printers/", views.stations, name="stations"),
    path("settings/printers/<int:pk>/routes/", views.station_routes, name="station-routes"),
    path("settings/printers/<int:pk>/delete/", views.station_delete, name="station-delete"),
]
