from django.urls import path

from . import views

app_name = "patients"

urlpatterns = [
    path("", views.patient_list, name="list"),
    path("register/", views.patient_register, name="register"),
    path("lookup/", views.patient_lookup, name="lookup"),
    path("duplicates/", views.duplicate_check, name="duplicates"),
    path("<int:pk>/", views.patient_detail, name="detail"),
    path("<int:pk>/edit/", views.patient_edit, name="edit"),
    path("<int:pk>/photo/", views.patient_photo, name="photo"),
    path("<int:pk>/slip/", views.registration_slip, name="slip"),
    path("<int:pk>/allergies/", views.allergy_add, name="allergy-add"),
    path(
        "<int:pk>/allergies/<int:allergy_pk>/error/", views.allergy_mark_error, name="allergy-error"
    ),
    path("<int:pk>/alerts/", views.alert_add, name="alert-add"),
    path("<int:pk>/alerts/<int:alert_pk>/remove/", views.alert_remove, name="alert-remove"),
    path("<int:pk>/identifiers/", views.identifier_add, name="identifier-add"),
    path(
        "<int:pk>/identifiers/<int:identifier_pk>/remove/",
        views.identifier_remove,
        name="identifier-remove",
    ),
]
