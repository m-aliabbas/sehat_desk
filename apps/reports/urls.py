from django.urls import path

from . import views, views_dashboards

app_name = "reports"

urlpatterns = [
    path("reports/", views.hub, name="hub"),
    path("reports/<slug:key>/", views.report_view, name="report"),
    path("dashboards/lab/", views_dashboards.lab_dashboard, name="lab-dashboard"),
    path("dashboards/pharmacy/", views_dashboards.pharmacy_dashboard, name="pharmacy-dashboard"),
]
