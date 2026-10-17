from django.urls import path

from . import views

app_name = "finance"

urlpatterns = [
    path("finance/", views.dashboard, name="dashboard"),
    path("finance/pnl/", views.pnl, name="pnl"),
    path("finance/ledger/", views.ledger, name="ledger"),
    path("finance/ledger/doctors/", views.ledger_doctors, name="ledger-doctors"),
    path("finance/expenses/", views.expense_list, name="expense-list"),
    path("finance/expenses/new/", views.expense_create, name="expense-create"),
    path("finance/expenses/<int:pk>/void/", views.expense_void, name="expense-void"),
    path("finance/categories/", views.category_list, name="category-list"),
    path("finance/categories/<int:pk>/status/", views.category_toggle, name="category-toggle"),
    path("finance/suppliers/pay/", views.supplier_pay, name="supplier-pay"),
]
