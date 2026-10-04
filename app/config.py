"""Validated runtime configuration; credentials have no production defaults."""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    auth_secret: str = field(repr=False)
    admin_token: str = field(repr=False)
    db_pool_size: int = 10
    db_pool_timeout: float = 180
    token_ttl_seconds: int = 86400

    @classmethod
    def from_env(cls) -> "Settings":
        result = cls(
            database_url=os.environ["DATABASE_URL"],
            auth_secret=os.environ["AUTH_SECRET"],
            admin_token=os.environ["ADMIN_TOKEN"],
            db_pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
            db_pool_timeout=float(os.getenv("DB_POOL_TIMEOUT", "180")),
            token_ttl_seconds=int(os.getenv("TOKEN_TTL_SECONDS", "86400")),
        )
        if len(result.auth_secret) < 32 or len(result.admin_token) < 24:
            raise ValueError("AUTH_SECRET needs 32+ characters and ADMIN_TOKEN needs 24+")
        if not 1 <= result.db_pool_size <= 100 or result.db_pool_timeout <= 0:
            raise ValueError("Invalid connection pool configuration")
        if not 60 <= result.token_ttl_seconds <= 604800:
            raise ValueError("TOKEN_TTL_SECONDS must be between 60 and 604800")
        return result
