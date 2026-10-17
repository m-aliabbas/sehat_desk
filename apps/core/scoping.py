"""Organization and branch scoping (CLAUDE.md: every view enforces scoping on the backend).

Business tables inherit OrgScopedModel or BranchScopedModel. Views never query `Model.objects`
directly for business data; they go through `scoped(Model, request)` or `get_scoped_or_404`,
which filter by the signed-in user's organization (and current branch for branch data).
A record from another organization is a 404, never a 403, so its existence is not revealed.
"""

from django.db import models
from django.shortcuts import get_object_or_404

from .models import TimeStampedModel


class OrgScopedModel(TimeStampedModel):
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        abstract = True


class BranchScopedModel(OrgScopedModel):
    branch = models.ForeignKey("organizations.Branch", on_delete=models.PROTECT, related_name="+")

    class Meta:
        abstract = True


def _is_branch_scoped(model) -> bool:
    return any(f.name == "branch" for f in model._meta.get_fields())


def scoped(model_or_qs, request, *, all_branches: bool = False):
    """Queryset limited to the request's organization, and to its branch for branch data.

    all_branches=True keeps the organization filter but drops the branch filter, for
    org-wide screens (reports across branches) that have checked a suitable permission.
    """
    qs = (
        model_or_qs.all()
        if isinstance(model_or_qs, models.QuerySet)
        else model_or_qs._default_manager.all()
    )
    if request.organization is None:
        return qs.none()
    qs = qs.filter(organization=request.organization)
    if not all_branches and _is_branch_scoped(qs.model):
        if request.branch is None:
            return qs.none()
        qs = qs.filter(branch=request.branch)
    return qs


def get_scoped_or_404(model_or_qs, request, *, all_branches: bool = False, **lookup):
    return get_object_or_404(scoped(model_or_qs, request, all_branches=all_branches), **lookup)


def paginate(request, queryset, per_page: int = 50):
    """Page of a list for `components/pagination.html` (?page=N, filters kept)."""
    from django.core.paginator import Paginator

    return Paginator(queryset, per_page).get_page(request.GET.get("page"))
