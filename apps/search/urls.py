from django.urls import path

from . import views

app_name = "search"

urlpatterns = [
    path("search/", views.search_page, name="page"),
    path("search/suggest/", views.suggest, name="suggest"),
    path("search/recent/clear/", views.clear_recent, name="recent-clear"),
]
