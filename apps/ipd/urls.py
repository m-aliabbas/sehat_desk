from django.urls import path

from . import views

app_name = "ipd"

urlpatterns = [
    path("ipd/", views.board, name="board"),
    path("ipd/admissions/", views.admission_list, name="admission-list"),
    path("ipd/admit/", views.admit, name="admit"),
    path("ipd/admissions/<int:pk>/", views.admission_detail, name="admission-detail"),
    path("ipd/admissions/<int:pk>/print/", views.admission_print, name="admission-print"),
    path("ipd/admissions/<int:pk>/cancel/", views.admission_cancel, name="admission-cancel"),
    path("ipd/admissions/<int:pk>/transfer/", views.transfer, name="admission-transfer"),
    path("ipd/admissions/<int:pk>/rounds/", views.round_add, name="admission-round"),
    path("ipd/rounds/<int:pk>/error/", views.round_error, name="round-error"),
    path("ipd/admissions/<int:pk>/medication/", views.medication_add, name="admission-medication"),
    path("ipd/medication/<int:pk>/stop/", views.medication_stop, name="medication-stop"),
    path("ipd/admissions/<int:pk>/charges/", views.charge_add, name="admission-charge"),
    path("ipd/admissions/<int:pk>/deposits/", views.deposit_add, name="admission-deposit"),
    path("ipd/admissions/<int:pk>/discharge/", views.discharge, name="discharge"),
    path("ipd/admissions/<int:pk>/summary/", views.summary, name="summary"),
    path("ipd/admissions/<int:pk>/summary/amend/", views.amend, name="summary-amend"),
    path("ipd/beds/<int:pk>/", views.bed_detail, name="bed-detail"),
    path("ipd/beds/<int:pk>/status/", views.bed_status, name="bed-status"),
    path("ipd/beds/<int:pk>/toggle/", views.bed_toggle, name="bed-toggle"),
    path("ipd/setup/", views.setup, name="setup"),
    path("ipd/setup/wards/<int:pk>/", views.ward_edit, name="setup-ward"),
    path("ipd/setup/rooms/<int:pk>/", views.room_edit, name="setup-room"),
]
