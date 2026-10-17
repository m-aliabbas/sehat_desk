from django.urls import path

from . import views

app_name = "lab"

urlpatterns = [
    path("lab/", views.worklist, name="worklist"),
    path("lab/orders/new/", views.order_create, name="order-create"),
    path("lab/orders/<int:pk>/", views.order_detail, name="order-detail"),
    path("lab/orders/<int:pk>/cancel/", views.order_cancel, name="order-cancel"),
    path("lab/orders/<int:pk>/start/", views.order_start, name="order-start"),
    path("lab/orders/<int:pk>/results/", views.results, name="results"),
    path("lab/orders/<int:pk>/verify/", views.verify, name="verify"),
    path("lab/orders/<int:pk>/release/", views.release, name="release"),
    path("lab/orders/<int:pk>/report/", views.report, name="report"),
    path("lab/orders/<int:pk>/print/", views.order_print, name="order-print"),
    path("lab/samples/<int:pk>/collect/", views.sample_collect, name="sample-collect"),
    path("lab/samples/<int:pk>/reject/", views.sample_reject, name="sample-reject"),
    path("lab/samples/<int:pk>/label/", views.sample_label, name="sample-label"),
    path("lab/items/<int:pk>/amend/", views.amend, name="amend"),
    path("lab/items/<int:pk>/attachments/", views.attachment_upload, name="attachment-upload"),
    path("lab/attachments/<int:pk>/", views.attachment_download, name="attachment"),
    path("lab/results/<int:pk>/acknowledge/", views.acknowledge, name="acknowledge"),
    path("encounters/<int:pk>/lab-orders/", views.encounter_order, name="encounter-order"),
    path("lab/catalog/", views.catalog, name="catalog"),
    path("lab/catalog/tests/new/", views.test_form, name="test-create"),
    path("lab/catalog/tests/<int:pk>/", views.test_form, name="test-edit"),
    path("lab/catalog/tests/<int:pk>/status/", views.test_toggle, name="test-toggle"),
    path("lab/catalog/panels/new/", views.panel_form, name="panel-create"),
    path("lab/catalog/panels/<int:pk>/", views.panel_form, name="panel-edit"),
    path("lab/catalog/panels/<int:pk>/status/", views.panel_toggle, name="panel-toggle"),
]
