from django.urls import path

from . import views

app_name = "recyclebin"

urlpatterns = [
    path("recycle-bin/", views.bin_list, name="list"),
    path("recycle-bin/restore/", views.restore_selected, name="restore-selected"),
    path("recycle-bin/<slug:kind>/<int:pk>/restore/", views.restore, name="restore"),
    path("recycle-bin/<slug:kind>/<int:pk>/erase/", views.erase, name="erase"),
    path("delete/<slug:kind>/", views.delete_selected, name="delete-selected"),
    path("delete/<slug:kind>/<int:pk>/", views.delete_one, name="delete"),
]
