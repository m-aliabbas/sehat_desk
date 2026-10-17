from django.urls import path

from . import views

app_name = "procedures"

urlpatterns = [
    path("procedures/", views.procedure_list, name="procedure-list"),
    path("procedures/new/", views.procedure_create, name="procedure-create"),
    path("procedures/<int:pk>/", views.procedure_detail, name="procedure-detail"),
    path("procedures/<int:pk>/complete/", views.procedure_complete, name="procedure-complete"),
    path("procedures/<int:pk>/cancel/", views.procedure_cancel, name="procedure-cancel"),
    path(
        "encounters/<int:pk>/procedures/",
        views.encounter_order,
        name="procedure-encounter-order",
    ),
]
