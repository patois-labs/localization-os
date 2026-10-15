"""Per-user credentials for positive connector API fixtures in throwaway suites."""


def user_api_token():
    from app.models import Role
    from app.services import apikeys, security

    email = "connector-fixture@example.test"
    user = security.get_by_email(email)
    if user is None:
        password = "offline-fixture-password-123"  # pregate: allow-secret offline fixture
        user = security.create_user(email, "Connector fixture", Role.ADMIN, password)
    return apikeys.create_key(user.id, "connector fixture", scopes="api")[1]
