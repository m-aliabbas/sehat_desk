from django.urls import path

from . import views

app_name = "approvals"

urlpatterns = [
    path("approvals/", views.approval_list, name="list"),
    path("approvals/bulk/", views.approval_bulk, name="bulk"),
    path("approvals/<int:pk>/approve/", views.approval_approve, name="approve"),
    path("approvals/<int:pk>/reject/", views.approval_reject, name="reject"),
]
