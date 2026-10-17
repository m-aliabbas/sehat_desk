from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("notifications/", views.center, name="center"),
    path("notifications/read-all/", views.mark_all_read, name="read-all"),
    path("notifications/<int:pk>/open/", views.open_notification, name="open"),
    path("notifications/<int:pk>/read/", views.mark_read, name="read"),
    path("settings/messages/", views.message_settings, name="messages"),
]
