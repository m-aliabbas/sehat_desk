from django.urls import path

from . import views

app_name = "prescriptions"

urlpatterns = [
    path("prescriptions/medicines/search/", views.medicine_search, name="medicine-search"),
    path("encounters/<int:pk>/prescription/items/", views.item_add, name="item-add"),
    path(
        "encounters/<int:pk>/prescription/items/<int:item_pk>/remove/",
        views.item_remove,
        name="item-remove",
    ),
    path("encounters/<int:pk>/prescription/advice/", views.advice, name="advice"),
    path("encounters/<int:pk>/prescription/favorites/", views.favorite_save, name="favorite-save"),
    path(
        "encounters/<int:pk>/prescription/favorites/<int:fav_pk>/apply/",
        views.favorite_apply,
        name="favorite-apply",
    ),
    path(
        "prescriptions/favorites/<int:fav_pk>/delete/",
        views.favorite_delete,
        name="favorite-delete",
    ),
    path(
        "encounters/<int:pk>/prescription/copy/<int:rx_pk>/",
        views.copy_previous,
        name="copy-previous",
    ),
    path("prescriptions/<int:pk>/send/", views.send, name="send"),
    path("prescriptions/<int:pk>/print/", views.print_rx, name="print"),
    path("pharmacy/prescriptions/", views.pharmacy_inbox, name="pharmacy-inbox"),
    path("settings/medicines/", views.medicine_list, name="medicine-list"),
    path("settings/medicines/new/", views.medicine_form, name="medicine-create"),
    path("settings/medicines/<int:pk>/", views.medicine_form, name="medicine-edit"),
    path("settings/medicines/<int:pk>/status/", views.medicine_toggle, name="medicine-toggle"),
]
