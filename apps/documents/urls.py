from django.urls import path

from . import views

app_name = "documents"

urlpatterns = [
    path("patients/<int:pk>/documents/", views.upload, name="upload"),
    path("documents/<int:pk>/file/", views.file, name="file"),
    path("documents/<int:pk>/archive/", views.archive, name="archive"),
    path("documents/<int:pk>/restore/", views.restore, name="restore"),
]
