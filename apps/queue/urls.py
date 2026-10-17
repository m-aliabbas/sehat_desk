from django.urls import path

from . import views

app_name = "queue"

urlpatterns = [
    path("reception/", views.desk, name="desk"),
    path("reception/walk-in/", views.walk_in, name="walk-in"),
    path("reception/counters/", views.counters, name="counters"),
    path("reception/counters/<int:pk>/toggle/", views.counter_toggle, name="counter-toggle"),
    path("reception/display-key/", views.display_key_regenerate, name="display-key"),
    path("queue/doctor/", views.doctor_queue, name="doctor"),
    path("queue/call-next/", views.call_next, name="call-next"),
    path("queue/tokens/<int:pk>/slip/", views.token_slip, name="slip"),
    path("queue/tokens/<int:pk>/call/", views.call, name="call"),
    path("queue/tokens/<int:pk>/recall/", views.recall, name="recall"),
    path("queue/tokens/<int:pk>/skip/", views.skip, name="skip"),
    path("queue/tokens/<int:pk>/requeue/", views.requeue, name="requeue"),
    path("queue/tokens/<int:pk>/start/", views.start, name="start"),
    path("queue/tokens/<int:pk>/complete/", views.complete, name="complete"),
    path("queue/tokens/<int:pk>/cancel/", views.cancel, name="cancel"),
    path("display/<str:key>/", views.display, name="display"),
]
