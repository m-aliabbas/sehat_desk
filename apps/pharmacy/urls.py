from django.urls import path

from . import views, views_pos, views_purchasing

app_name = "pharmacy"

urlpatterns = [
    path("inventory/", views.dashboard, name="dashboard"),
    path("inventory/products/", views.product_list, name="product-list"),
    path("inventory/products/new/", views.product_form, name="product-create"),
    path("inventory/products/<int:pk>/", views.product_detail, name="product-detail"),
    path("inventory/products/<int:pk>/edit/", views.product_form, name="product-edit"),
    path("inventory/categories/new/", views.category_create, name="category-create"),
    path("inventory/receive/", views.receive, name="receive"),
    path("inventory/batches/<int:pk>/action/", views.batch_action, name="batch-action"),
    path("inventory/ledger/", views.ledger, name="ledger"),
    # Point of sale (Phase 13)
    path("pharmacy/pos/", views_pos.till, name="pos"),
    path("pharmacy/pos/search/", views_pos.search, name="pos-search"),
    path("pharmacy/pos/add/", views_pos.add, name="pos-add"),
    path("pharmacy/pos/items/<int:pk>/", views_pos.item, name="pos-item"),
    path("pharmacy/pos/customer/", views_pos.customer, name="pos-customer"),
    path("pharmacy/pos/prescription/", views_pos.prescription, name="pos-prescription"),
    path("pharmacy/pos/discount/", views_pos.discount, name="pos-discount"),
    path("pharmacy/pos/hold/", views_pos.hold, name="pos-hold"),
    path("pharmacy/pos/clear/", views_pos.clear, name="pos-clear"),
    path("pharmacy/pos/pay/", views_pos.pay, name="pos-pay"),
    path("pharmacy/pos/held/", views_pos.held, name="pos-held"),
    path("pharmacy/pos/held/<int:pk>/resume/", views_pos.resume, name="pos-resume"),
    path("pharmacy/pos/held/<int:pk>/clear/", views_pos.held_clear, name="pos-held-clear"),
    path("pharmacy/dispense/", views_pos.rx_queue, name="rx-queue"),
    path("pharmacy/dispense/<int:pk>/", views_pos.rx_detail, name="rx-detail"),
    path("pharmacy/dispense/<int:pk>/close/", views_pos.rx_close, name="rx-close"),
    path("pharmacy/sales/", views_pos.sale_list, name="sale-list"),
    path("pharmacy/sales/<int:pk>/", views_pos.sale_detail, name="sale-detail"),
    path("pharmacy/sales/<int:pk>/receipt/", views_pos.sale_receipt, name="sale-receipt"),
    path("pharmacy/sales/<int:pk>/return/", views_pos.sale_return, name="sale-return"),
    # Purchasing (Phase 14)
    path("inventory/suppliers/", views_purchasing.vendor_list, name="vendor-list"),
    path("inventory/suppliers/new/", views_purchasing.vendor_form, name="vendor-create"),
    path("inventory/suppliers/<int:pk>/", views_purchasing.vendor_detail, name="vendor-detail"),
    path("inventory/suppliers/<int:pk>/edit/", views_purchasing.vendor_form, name="vendor-edit"),
    path(
        "inventory/suppliers/<int:pk>/status/",
        views_purchasing.vendor_toggle,
        name="vendor-toggle",
    ),
    path("inventory/purchases/", views_purchasing.purchase_list, name="purchase-list"),
    path("inventory/purchases/new/", views_purchasing.purchase_create, name="purchase-create"),
    path(
        "inventory/purchases/<int:pk>/",
        views_purchasing.purchase_detail,
        name="purchase-detail",
    ),
    path(
        "inventory/purchases/<int:pk>/items/<int:item_pk>/",
        views_purchasing.purchase_item,
        name="purchase-item",
    ),
    path(
        "inventory/purchases/<int:pk>/details/",
        views_purchasing.purchase_details,
        name="purchase-details",
    ),
    path(
        "inventory/purchases/<int:pk>/order/",
        views_purchasing.purchase_order,
        name="purchase-order",
    ),
    path(
        "inventory/purchases/<int:pk>/receive/",
        views_purchasing.purchase_receive,
        name="purchase-receive",
    ),
    path(
        "inventory/purchases/<int:pk>/cancel/",
        views_purchasing.purchase_cancel,
        name="purchase-cancel",
    ),
    path(
        "inventory/purchases/<int:pk>/return/",
        views_purchasing.purchase_return,
        name="purchase-return",
    ),
    path(
        "inventory/reports/purchases/",
        views_purchasing.purchase_report,
        name="purchase-report",
    ),
]
