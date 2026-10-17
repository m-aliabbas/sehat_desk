from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm, SetPasswordForm

from apps.core.forms import StyledFormMixin
from apps.organizations.models import Branch

from .models import Permission, Role, RoleAssignment, User


class LoginForm(AuthenticationForm):
    error_messages = {
        "invalid_login": (
            "The username or password is not correct. Try again, or ask your administrator "
            "to reset your password."
        ),
        "inactive": "This staff account is inactive. Ask your administrator to review its status.",
    }

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if user.organization_id and not user.organization.is_active:
            raise forms.ValidationError(self.error_messages["inactive"], code="inactive")


class StyledPasswordChangeForm(StyledFormMixin, PasswordChangeForm):
    pass


class StyledSetPasswordForm(StyledFormMixin, SetPasswordForm):
    pass


class _PasswordPairMixin:
    """Two password fields validated against Django's password validators."""

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get("password1"), cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "The two passwords do not match.")
        elif p1:
            try:
                password_validation.validate_password(p1, self._password_user())
            except forms.ValidationError as err:
                self.add_error("password1", err)
        return cleaned


class SetupForm(StyledFormMixin, _PasswordPairMixin, forms.Form):
    """First-run setup: hospital, main branch and the first Super admin."""

    org_name = forms.CharField(label="Hospital name", max_length=150)
    branch_name = forms.CharField(label="Main branch name", max_length=120, initial="Main branch")
    branch_code = forms.RegexField(
        label="Branch code",
        regex=r"^[A-Za-z0-9-]{1,10}$",
        initial="MAIN",
        help_text="Short code for tokens and receipts.",
    )
    city = forms.CharField(max_length=80, required=False)
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150, required=False)
    username = forms.CharField(max_length=150)
    email = forms.EmailField(required=False)
    password1 = forms.CharField(
        label="Password", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"})
    )
    password2 = forms.CharField(
        label="Confirm password", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"})
    )

    def _password_user(self):
        return User(
            username=self.cleaned_data.get("username", ""),
            first_name=self.cleaned_data.get("first_name", ""),
        )

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("This username is taken.")
        return username

    def clean_branch_code(self):
        return self.cleaned_data["branch_code"].upper()


class StaffForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = User
        fields = [
            "first_name",
            "last_name",
            "username",
            "email",
            "phone",
            "employee_code",
            "designation",
        ]

    def __init__(self, *args, organization, **kwargs):
        self.organization = organization
        super().__init__(*args, **kwargs)
        self.fields["first_name"].required = True

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        qs = User.objects.filter(username__iexact=username)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("This username is taken.")
        return username


class StaffCreateForm(_PasswordPairMixin, StaffForm):
    """New staff account with a first role. The user must change the password on first sign-in."""

    password1 = forms.CharField(
        label="Temporary password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label="Confirm temporary password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    role = forms.ModelChoiceField(queryset=Role.objects.none())
    branch = forms.ModelChoiceField(
        queryset=Branch.objects.none(),
        required=False,
        empty_label="All branches",
        help_text="Limit this role to one branch, or leave as All branches.",
    )

    def __init__(self, *args, assignable_roles, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].queryset = assignable_roles
        self.fields["branch"].queryset = Branch.objects.filter(
            organization=self.organization, is_active=True
        )

    def _password_user(self):
        return User(
            username=self.cleaned_data.get("username", ""),
            first_name=self.cleaned_data.get("first_name", ""),
            email=self.cleaned_data.get("email", ""),
        )


class AssignmentForm(StyledFormMixin, forms.Form):
    role = forms.ModelChoiceField(queryset=Role.objects.none())
    branch = forms.ModelChoiceField(
        queryset=Branch.objects.none(), required=False, empty_label="All branches"
    )

    def __init__(self, *args, user, organization, assignable_roles, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self.fields["role"].queryset = assignable_roles
        self.fields["branch"].queryset = Branch.objects.filter(
            organization=organization, is_active=True
        )

    def clean(self):
        cleaned = super().clean()
        role, branch = cleaned.get("role"), cleaned.get("branch")
        if (
            role
            and RoleAssignment.objects.filter(user=self.user, role=role, branch=branch).exists()
        ):
            raise forms.ValidationError("This staff member already has that role there.")
        return cleaned


class ResetPasswordForm(StyledFormMixin, _PasswordPairMixin, forms.Form):
    password1 = forms.CharField(
        label="New temporary password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label="Confirm password", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"})
    )

    def __init__(self, *args, user, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def _password_user(self):
        return self.user


class RoleForm(StyledFormMixin, forms.ModelForm):
    permissions = forms.ModelMultipleChoiceField(
        queryset=Permission.objects.all(), required=False, widget=forms.CheckboxSelectMultiple
    )

    class Meta:
        model = Role
        fields = ["name", "description", "permissions"]

    def __init__(self, *args, organization, grantable_codes, **kwargs):
        self.organization = organization
        self.grantable_codes = grantable_codes
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        qs = Role.objects.filter(organization=self.organization, name__iexact=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("Another role already has this name.")
        return name

    def clean_permissions(self):
        perms = self.cleaned_data["permissions"]
        original = (
            set(self.instance.permissions.values_list("code", flat=True))
            if self.instance.pk
            else set()
        )
        changed = {p.code for p in perms} ^ original
        not_allowed = sorted(changed - self.grantable_codes)
        if not_allowed:
            raise forms.ValidationError(
                "You can only grant or remove permissions you hold yourself: "
                + ", ".join(not_allowed)
            )
        return perms
