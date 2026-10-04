import hmac
from datetime import UTC, datetime, timedelta

import jwt

from app.config import Settings
from app.domain import DomainError


class TokenService:
    """Admin-only token provisioning replaces a full user registration system."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def require_admin(self, token: str) -> None:
        if not hmac.compare_digest(token.encode(), self.settings.admin_token.encode()):
            raise DomainError(403, "admin_required", "Admin credentials required")

    def issue(self, user_id: str) -> str:
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "sub": user_id,
                "role": "user",
                "iat": now,
                "exp": now + timedelta(seconds=self.settings.token_ttl_seconds),
                "iss": "seat-reservation",
                "aud": "seat-reservation-api",
            },
            self.settings.auth_secret,
            algorithm="HS256",
        )

    def user_id(self, token: str) -> str:
        try:
            claims = jwt.decode(
                token,
                self.settings.auth_secret,
                algorithms=["HS256"],
                issuer="seat-reservation",
                audience="seat-reservation-api",
                options={"require": ["sub", "role", "iat", "exp", "iss", "aud"]},
            )
            if claims["role"] != "user" or not isinstance(claims["sub"], str):
                raise ValueError("Invalid principal")
            if not 1 <= len(claims["sub"]) <= 128:
                raise ValueError("Invalid subject")
            return claims["sub"]
        except (jwt.InvalidTokenError, ValueError) as exc:
            raise DomainError(401, "invalid_token", "Invalid or expired user token") from exc
