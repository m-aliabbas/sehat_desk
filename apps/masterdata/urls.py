from django.urls import path

from . import views

app_name = "masterdata"

urlpatterns = [
    path("departments/", views.department_list, name="department-list"),
    path("departments/new/", views.department_form, name="department-create"),
    path("departments/<int:pk>/", views.department_form, name="department-edit"),
    path("departments/<int:pk>/status/", views.department_toggle, name="department-toggle"),
    path("specialties/new/", views.specialty_create, name="specialty-create"),
    path("doctors/", views.doctor_list, name="doctor-list"),
    path("doctors/new/", views.doctor_form, name="doctor-create"),
    path("doctors/<int:pk>/", views.doctor_detail, name="doctor-detail"),
    path("doctors/<int:pk>/edit/", views.doctor_form, name="doctor-edit"),
    path("doctors/<int:pk>/status/", views.doctor_toggle, name="doctor-toggle"),
    path("doctors/<int:pk>/leave/", views.leave_add, name="leave-add"),
    path("doctors/<int:pk>/leave/<int:leave_pk>/remove/", views.leave_remove, name="leave-remove"),
    path("services/", views.service_list, name="service-list"),
    path("services/new/", views.service_form, name="service-create"),
    path("services/<int:pk>/", views.service_form, name="service-edit"),
    path("services/<int:pk>/status/", views.service_toggle, name="service-toggle"),
    path("service-categories/new/", views.category_create, name="category-create"),
]
