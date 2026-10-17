from django.urls import path

from . import views

app_name = "appointments"

urlpatterns = [
    path("", views.calendar, name="calendar"),
    path("list/", views.appointment_list, name="list"),
    path("book/", views.book, name="book"),
    path("slots/", views.slots, name="slots"),
    path("doctor-options/", views.doctor_options, name="doctor-options"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/slip/", views.slip, name="slip"),
    path("<int:pk>/confirm/", views.confirm, name="confirm"),
    path("<int:pk>/check-in/", views.check_in, name="check-in"),
    path("<int:pk>/no-show/", views.no_show, name="no-show"),
    path("<int:pk>/cancel/", views.cancel, name="cancel"),
    path("<int:pk>/reschedule/", views.reschedule, name="reschedule"),
]
