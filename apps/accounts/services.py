"""RBAC rules: who can do what, in which branch."""

from django.db import transaction
from django.db.models import Q

from apps.organizations.models import Branch, Organization

from .models import Permission, Role, RoleAssignment, User
from .permissions import ALL_CODES, DEFAULT_ROLES, PERMISSIONS, SUPER_ADMIN_KEY


def sync_permissions() -> None:
    """Copy the code catalogue into the Permission table and keep Super admin complete.

    A code that is new to the database is also granted to existing system roles whose default
    definition includes it, so adding a module's permissions reaches existing hospitals.
    Codes that already existed are never re-granted, so an admin's edits to roles stay.
    """
    defaults_by_key = {d.key: d.permissions for d in DEFAULT_ROLES}
    for order, p in enumerate(PERMISSIONS):
        perm, created = Permission.objects.update_or_create(
            code=p.code, defaults={"module": p.module, "label": p.label, "sort_order": order}
        )
        if created:
            keys = [k for k, codes in defaults_by_key.items() if p.code in codes]
            for role in Role.objects.filter(system_key__in=keys):
                role.permissions.add(perm)
    Permission.objects.exclude(code__in=ALL_CODES).delete()
    all_perms = list(Permission.objects.all())
    for role in Role.objects.filter(system_key=SUPER_ADMIN_KEY):
        role.permissions.set(all_perms)


def seed_default_roles(organization: Organization) -> dict[str, Role]:
    """Create the system roles for a new organization. Existing roles are left as edited."""
    perms = {p.code: p for p in Permission.objects.all()}
    roles = {}
    for definition in DEFAULT_ROLES:
        role, created = Role.objects.get_or_create(
            organization=organization,
            system_key=definition.key,
            defaults={"name": definition.name, "description": definition.description},
        )
        if created:
            role.permissions.set([perms[c] for c in definition.permissions if c in perms])
        roles[definition.key] = role
    return roles


@transaction.atomic
def create_organization_with_admin(*, org_name, branch_name, branch_code, city, admin_data):
    """First-run setup: organization, main branch, default roles, and a Super admin account."""
    sync_permissions()
    org = Organization.objects.create(name=org_name)
    branch = Branch.objects.create(
        organization=org, name=branch_name, code=branch_code, city=city, is_main=True
    )
    roles = seed_default_roles(org)
    password = admin_data.pop("password")
    user = User(organization=org, **admin_data)
    user.set_password(password)
    user.save()
    RoleAssignment.objects.create(user=user, role=roles[SUPER_ADMIN_KEY], branch=None)
    return org, branch, user


def _active_assignments(user):
    return RoleAssignment.objects.filter(
        user=user,
        role__organization_id=user.organization_id,
        role__is_active=True,
    ).filter(Q(branch__isnull=True) | Q(branch__is_active=True))


def accessible_branches(user):
    """Branches the user may work in: all active ones for an all-branch assignment."""
    if not user.is_authenticated or user.organization_id is None:
        return Branch.objects.none()
    org_branches = Branch.objects.filter(organization_id=user.organization_id, is_active=True)
    assignments = _active_assignments(user)
    if assignments.filter(branch__isnull=True).exists():
        return org_branches
    return org_branches.filter(id__in=assignments.values("branch_id"))


def effective_permissions(user, branch) -> frozenset:
    """Permission codes from roles assigned for all branches or for this branch."""
    if not user.is_authenticated or not user.is_active or branch is None:
        return frozenset()
    role_ids = (
        _active_assignments(user)
        .filter(Q(branch__isnull=True) | Q(branch=branch))
        .values("role_id")
    )
    return frozenset(
        Permission.objects.filter(roles__in=role_ids).values_list("code", flat=True).distinct()
    )


def all_permissions_anywhere(user) -> frozenset:
    """Union of a user's permissions across every branch (used for escalation checks)."""
    role_ids = _active_assignments(user).values("role_id")
    return frozenset(
        Permission.objects.filter(roles__in=role_ids).values_list("code", flat=True).distinct()
    )


def role_codes(role) -> frozenset:
    return frozenset(role.permissions.values_list("code", flat=True))


def can_grant_role(actor_perms: frozenset, role: Role) -> bool:
    """No privilege escalation: you can only hand out permissions you hold yourself."""
    return role_codes(role) <= actor_perms


def super_admin_count(organization) -> int:
    return (
        User.objects.filter(
            organization=organization,
            is_active=True,
            assignments__role__system_key=SUPER_ADMIN_KEY,
            assignments__branch__isnull=True,
        )
        .distinct()
        .count()
    )


def is_super_admin(user) -> bool:
    return RoleAssignment.objects.filter(
        user=user, role__system_key=SUPER_ADMIN_KEY, branch__isnull=True
    ).exists()
