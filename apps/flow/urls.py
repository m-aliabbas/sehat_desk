from django.urls import path

from . import views

app_name = "flow"

urlpatterns = [
    path("flow/", views.board, name="board"),
    path("flow/pull/", views.pull, name="pull"),
    path("flow/token/", views.token, name="token"),
    path("flow/<int:pk>/bill/", views.bill, name="bill"),
    path("flow/<int:pk>/send-on/", views.send_on, name="send-on"),
    path("flow/<int:pk>/charge/", views.charge, name="charge"),
    path("settings/patient-flow/", views.flow_settings, name="settings"),
]
