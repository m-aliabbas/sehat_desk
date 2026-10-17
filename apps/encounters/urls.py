from django.urls import path

from . import views

app_name = "encounters"

urlpatterns = [
    path("opd/", views.workspace, name="workspace"),
    path("encounters/start/", views.start, name="start"),
    path("encounters/<int:pk>/", views.detail, name="detail"),
    path("encounters/<int:pk>/autosave/", views.autosave, name="autosave"),
    path("encounters/<int:pk>/complete/", views.complete, name="complete"),
    path("encounters/<int:pk>/reopen/", views.reopen, name="reopen"),
    path("encounters/<int:pk>/versions/", views.history, name="history"),
    path("encounters/<int:pk>/diagnoses/search/", views.diagnosis_search, name="diagnosis-search"),
    path("settings/diagnosis-codes/", views.diagnosis_code_list, name="diagnosis-code-list"),
    path(
        "settings/diagnosis-codes/<int:pk>/status/",
        views.diagnosis_code_toggle,
        name="diagnosis-code-toggle",
    ),
    path("encounters/<int:pk>/diagnoses/add/", views.diagnosis_add, name="diagnosis-add"),
    path(
        "encounters/<int:pk>/diagnoses/<int:dx_pk>/update/",
        views.diagnosis_update,
        name="diagnosis-update",
    ),
    path(
        "encounters/<int:pk>/diagnoses/<int:dx_pk>/remove/",
        views.diagnosis_remove,
        name="diagnosis-remove",
    ),
]
