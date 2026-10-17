from django.urls import path

from . import views_auth, views_roles, views_staff

app_name = "accounts"

urlpatterns = [
    # Sign-in and setup
    path("setup/", views_auth.setup, name="setup"),
    path("accounts/login/", views_auth.LoginView.as_view(), name="login"),
    path("accounts/logout/", views_auth.logout_view, name="logout"),
    path("accounts/password/change/", views_auth.password_change, name="password-change"),
    path("accounts/password/forgot/", views_auth.password_reset, name="password-reset"),
    path(
        "accounts/password/forgot/sent/", views_auth.password_reset_done, name="password-reset-done"
    ),
    path(
        "accounts/password/reset/<uidb64>/<token>/",
        views_auth.password_reset_confirm,
        name="password-reset-confirm",
    ),
    path(
        "accounts/password/reset/done/",
        views_auth.password_reset_complete,
        name="password-reset-complete",
    ),
    # Workspace
    path("workspace/", views_auth.workspace, name="workspace"),
    path("workspace/switch/", views_auth.switch_branch, name="switch-branch"),
    path("workspace/no-access/", views_auth.no_access, name="no-access"),
    # Staff
    path("staff/", views_staff.staff_list, name="staff-list"),
    path("staff/new/", views_staff.staff_create, name="staff-create"),
    path("staff/<int:pk>/", views_staff.staff_detail, name="staff-detail"),
    path("staff/<int:pk>/edit/", views_staff.staff_edit, name="staff-edit"),
    path("staff/<int:pk>/status/", views_staff.staff_toggle_active, name="staff-toggle-active"),
    path(
        "staff/<int:pk>/reset-password/",
        views_staff.staff_reset_password,
        name="staff-reset-password",
    ),
    path("staff/<int:pk>/unlock/", views_staff.staff_unlock, name="staff-unlock"),
    path("staff/<int:pk>/roles/", views_staff.assignment_add, name="assignment-add"),
    path(
        "staff/<int:pk>/roles/<int:assignment_pk>/remove/",
        views_staff.assignment_remove,
        name="assignment-remove",
    ),
    # Roles
    path("settings/roles/", views_roles.role_list, name="role-list"),
    path("settings/roles/new/", views_roles.role_create, name="role-create"),
    path("settings/roles/matrix/", views_roles.permission_matrix, name="permission-matrix"),
    path("settings/roles/<int:pk>/", views_roles.role_edit, name="role-edit"),
    path(
        "settings/roles/<int:pk>/status/", views_roles.role_toggle_active, name="role-toggle-active"
    ),
]
