from django.urls import path

from . import views

app_name = "billing"

urlpatterns = [
    path("billing/", views.invoice_list, name="invoice-list"),
    path("billing/new/", views.invoice_create, name="invoice-create"),
    path("billing/<int:pk>/", views.invoice_detail, name="invoice-detail"),
    path("billing/<int:pk>/items/", views.item_add, name="invoice-item-add"),
    path(
        "billing/<int:pk>/items/<int:item_pk>/remove/",
        views.item_remove,
        name="invoice-item-remove",
    ),
    path("billing/<int:pk>/discount/", views.discount, name="invoice-discount"),
    path("billing/<int:pk>/issue/", views.issue, name="invoice-issue"),
    path("billing/<int:pk>/cancel/", views.cancel, name="invoice-cancel"),
    path("billing/<int:pk>/pay/", views.pay, name="invoice-pay"),
    path("billing/<int:pk>/refund/", views.refund, name="invoice-refund"),
    path("billing/<int:pk>/print/", views.invoice_print, name="invoice-print"),
    path("payments/", views.payment_list, name="payment-list"),
    path("payments/<int:pk>/receipt/", views.receipt_print, name="receipt"),
    path("settings/billing/", views.billing_settings, name="settings"),
    path("settings/billing/methods/<int:pk>/status/", views.method_toggle, name="method-toggle"),
]
