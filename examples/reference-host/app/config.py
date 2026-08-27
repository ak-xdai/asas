"""Settings, and the tier boundary.

Demonstrates: **zero-config boot** (design record 0030 §5c). Everything the core
tier needs has a working default, so ``uvicorn app.main:app`` runs against an
empty environment. Everything that cannot have one is optional and reports
itself as off.

The tiering is the lesson. Graceful degradation is a property Asas claims
everywhere and demonstrates nowhere: a package whose configuration is absent
must reduce the feature set, never break the boot. Read ``Settings.tier_report``
as the executable statement of which half each package is in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- Core tier: defaults that work with nothing set -------------------
    database_url: str = field(
        default_factory=lambda: os.environ.get("DATABASE_URL", "sqlite:///./helpdesk.db")
    )
    uploads_dir: str = field(
        default_factory=lambda: os.environ.get("UPLOADS_DIR", "./uploads")
    )

    # --- Optional tier: off unless configured -----------------------------
    # Deep search needs Postgres. On SQLite the base provider still answers, so
    # /search works either way and only the ranking tier differs.
    deep_search: bool = field(
        default_factory=lambda: os.environ.get("DATABASE_URL", "").startswith("postgresql")
    )
    # MCP needs a token to be worth mounting; without one there is nobody to
    # authenticate, so the endpoint stays absent rather than open.
    mcp_token: str | None = field(
        default_factory=lambda: os.environ.get("MCP_TOKEN") or None
    )
    # See fake_auth.py. Refuses to arm without this, on purpose.
    enable_fake_auth: bool = field(default_factory=lambda: _flag("ENABLE_FAKE_AUTH"))

    @property
    def mcp_enabled(self) -> bool:
        return self.mcp_token is not None

    def tier_report(self) -> dict[str, str]:
        """What is on, for the boot log and the ``/health`` route.

        A host that degrades silently is indistinguishable from one that is
        broken, so the reduced configuration has to announce itself.
        """
        return {
            "database": self.database_url.split("://", 1)[0],
            "search": "deep (postgres)" if self.deep_search else "base (portable)",
            "mcp": "on" if self.mcp_enabled else "off (set MCP_TOKEN)",
            "auth": "fake auth ARMED" if self.enable_fake_auth else "off (anonymous)",
            "storage": f"local ({self.uploads_dir})",
        }


settings = Settings()
