from django.urls import path

from . import views

app_name = "nursing"

urlpatterns = [
    path("nursing/", views.workspace, name="workspace"),
    path("nursing/handover/", views.handover, name="handover"),
    path("nursing/handover/<int:pk>/acknowledge/", views.handover_ack, name="handover-ack"),
    path("nursing/admissions/<int:pk>/notes/", views.note_add, name="note-add"),
    path("nursing/admissions/<int:pk>/intake-output/", views.io_add, name="io-add"),
    path("nursing/admissions/<int:pk>/schedule/", views.schedule_set, name="schedule"),
    path("nursing/admissions/<int:pk>/assign/", views.assign, name="assign"),
    path("nursing/assignments/<int:pk>/end/", views.unassign, name="unassign"),
    path("nursing/medication/<int:pk>/dose/", views.dose_chart, name="dose"),
    path("nursing/<slug:kind>/<int:pk>/error/", views.mark_error, name="error"),
]
