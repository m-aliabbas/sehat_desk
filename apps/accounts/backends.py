from django.contrib.auth.backends import ModelBackend


class SehatDeskBackend(ModelBackend):
    """Lets the login form tell inactive staff why they cannot sign in, without letting them in.

    authenticate() accepts inactive users so AuthenticationForm.confirm_login_allowed can show the
    "account inactive" message. get_user() rejects them, so an existing session ends the moment
    an admin deactivates the account or its organization.
    """

    def user_can_authenticate(self, user):
        return True

    def get_user(self, user_id):
        user = super().get_user(user_id)
        if user is None or not user.is_active:
            return None
        if user.organization_id is not None and not user.organization.is_active:
            return None
        return user
