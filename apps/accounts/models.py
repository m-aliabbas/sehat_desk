from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Q

from apps.core.models import TimeStampedModel


class User(AbstractUser):
    """A staff account. Belongs to one organization; access comes from RoleAssignments.

    Django's is_staff / is_superuser flags are not used for authorization in SehatDesk.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="users",
        null=True,
        blank=True,
    )
    phone = models.CharField(max_length=30, blank=True)
    employee_code = models.CharField(max_length=30, blank=True)
    designation = models.CharField(max_length=80, blank=True, help_text="e.g. Consultant physician")
    must_change_password = models.BooleanField(
        default=False, help_text="Set after an admin resets the password."
    )

    class Meta:
        ordering = ["first_name", "last_name", "username"]

    def __str__(self):
        return self.get_full_name() or self.username


class Permission(models.Model):
    """One action, e.g. "patient.create". Rows are synced from accounts/permissions.py."""

    code = models.CharField(max_length=60, unique=True)
    module = models.CharField(max_length=40)
    label = models.CharField(max_length=120)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order"]

    def __str__(self):
        return self.code


class Role(TimeStampedModel):
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="roles"
    )
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=200, blank=True)
    # Set for roles seeded from DEFAULT_ROLES; empty for custom roles.
    system_key = models.CharField(max_length=40, blank=True)
    permissions = models.ManyToManyField(Permission, related_name="roles", blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="uniq_role_name_per_org"),
            models.UniqueConstraint(
                fields=["organization", "system_key"],
                condition=~Q(system_key=""),
                name="uniq_system_role_per_org",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def is_system(self) -> bool:
        return bool(self.system_key)

    @property
    def is_locked(self) -> bool:
        from .permissions import SUPER_ADMIN_KEY

        return self.system_key == SUPER_ADMIN_KEY


class RoleAssignment(TimeStampedModel):
    """Gives a user a role in one branch, or in every branch when branch is empty."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="assignments")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="assignments")
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        related_name="assignments",
        null=True,
        blank=True,
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["role__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "role", "branch"], name="uniq_assignment_per_branch"
            ),
            models.UniqueConstraint(
                fields=["user", "role"],
                condition=Q(branch__isnull=True),
                name="uniq_assignment_all_branches",
            ),
        ]

    def __str__(self):
        return f"{self.user} · {self.role} · {self.branch or 'All branches'}"
