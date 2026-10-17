from django.urls import path

from . import views

app_name = "hr"

urlpatterns = [
    path("hr/", views.home, name="home"),
    path("hr/check/", views.attendance_check, name="attendance-check"),
    path("hr/earnings/", views.earnings, name="earnings"),
    path("hr/attendance/", views.attendance_register, name="attendance-register"),
    path("hr/attendance/month/", views.attendance_month, name="attendance-month"),
    path("hr/pay/", views.pay_list, name="pay-list"),
    path("hr/pay/<int:user_id>/", views.pay_profile, name="pay-profile"),
    path("hr/pay/<int:user_id>/rules/", views.pay_rule_add, name="pay-rule-add"),
    path(
        "hr/pay/<int:user_id>/rules/<int:rule_pk>/remove/",
        views.pay_rule_remove,
        name="pay-rule-remove",
    ),
    path("hr/pay/<int:user_id>/ledger/", views.staff_ledger, name="staff-ledger"),
    path("hr/pay/<int:user_id>/ledger/pay/", views.staff_pay, name="staff-pay"),
    path("hr/pay/<int:user_id>/ledger/opening/", views.staff_opening, name="staff-opening"),
    path("hr/payroll/", views.payroll_list, name="payroll-list"),
    path("hr/payroll/<int:pk>/", views.payroll_detail, name="payroll-detail"),
    path(
        "hr/payroll/<int:pk>/recalculate/",
        views.payroll_recalculate,
        name="payroll-recalculate",
    ),
    path("hr/payroll/<int:pk>/finalize/", views.payroll_finalize, name="payroll-finalize"),
    path("hr/payslips/<int:pk>/adjust/", views.payslip_adjust, name="payslip-adjust"),
    path("hr/payslips/<int:pk>/print/", views.payslip_print, name="payslip-print"),
]
