"""Lookup module tables.

A generic four-table core (type registry -> value -> translation, plus alias) so the
module scales to hundreds of types and large value sets without a table per type.
Design: Teamy decision record 0002 (lookups design) §3-4.
"""

from datetime import date, datetime
from enum import Enum
from typing import Optional

from sqlalchemy import JSON, Column, Index, UniqueConstraint, text
from sqlmodel import Field, Relationship, SQLModel

# Deleting a type removes its values; deleting a value removes its translations/aliases.
_CASCADE = {"cascade": "all, delete-orphan"}


class LookupStatus(str, Enum):
    active = "active"
    deprecated = "deprecated"  # hidden from new-entry pickers; still resolves for history


class SortMode(str, Enum):
    label = "label"  # alphabetical, per requested language
    sort_order = "sort_order"  # curated order via LookupValue.sort_order


class TypeScope(str, Enum):
    """Who owns a type's values (issue #35; DR 0001 D2 as decided in #24).

    Explicit and declared at registration — never inferred from ``code_system``
    or ``is_open``:

    - ``platform``: values are platform-owned reference data, immutable to
      orgs — no edits, no additions, no tombstones. Platform updates reach
      every tenant instantly. Never an open list.
    - ``org``: values live wholly at org level. The type's platform-held rows
      are a starter TEMPLATE, copied per org by ``seed_org_lookups`` at org
      creation and never served to org reads; after seeding the org owns its
      list outright (template drift is accepted by design).
    """

    platform = "platform"
    org = "org"


class LookupType(SQLModel, table=True):
    """Registry of lookup types (gender, nationality, university, skill, ...)."""

    __tablename__ = "lookup_type"

    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)  # machine name, e.g. "nationality"
    name: str
    description: Optional[str] = None
    is_open: bool = False  # can non-admins add values? Only legal on org-scoped types.
    is_hierarchical: bool = False  # values may have a parent (country -> city)
    code_system: Optional[str] = None  # "ISO3166-1A2", "ISO639-1", "internal", ...
    # Who owns the values (issue #35). Explicit — never inferred. Existing
    # types backfill to platform in migration 0002 (all library seeds are
    # reference data); hosts declare scope="org" for their vocabularies.
    scope: TypeScope = TypeScope.platform
    default_sort: SortMode = SortMode.label
    version: int = 1  # bumped on any change under this type -> cache invalidation
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    values: list["LookupValue"] = Relationship(
        back_populates="type", sa_relationship_kwargs=_CASCADE
    )


class LookupValue(SQLModel, table=True):
    """A single value within a type. ``code`` is the stable, immutable identifier that
    consuming records store; labels live in LookupTranslation and may change freely."""

    __tablename__ = "lookup_value"
    __table_args__ = (
        # Per-org override pattern (WXL-241): NULL org_id = platform seed, an org row
        # shadows it. Two partial uniques because a composite unique treats NULLs as
        # distinct on Postgres (duplicate global codes would slip through).
        Index(
            "uq_value_type_code_global",
            "type_id",
            "code",
            unique=True,
            sqlite_where=text("org_id IS NULL"),
            postgresql_where=text("org_id IS NULL"),
        ),
        Index(
            "uq_value_type_code_org",
            "type_id",
            "code",
            "org_id",
            unique=True,
            sqlite_where=text("org_id IS NOT NULL"),
            postgresql_where=text("org_id IS NOT NULL"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    type_id: int = Field(foreign_key="lookup_type.id", index=True)
    # Owning org (WXL-241): NULL = platform default visible to every org; a non-NULL
    # row is one org's own value (open types) or label override. Plain int, no FK —
    # the package stays self-contained; the org resolver is configured by app wiring.
    org_id: Optional[int] = Field(default=None, index=True)
    code: str = Field(index=True)  # stable; unique within (type, org-or-global)
    parent_id: Optional[int] = Field(default=None, foreign_key="lookup_value.id")
    status: LookupStatus = LookupStatus.active
    is_default: bool = False
    sort_order: int = 0
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None  # set when retired; references still resolve
    superseded_by_id: Optional[int] = Field(default=None, foreign_key="lookup_value.id")
    # 'metadata' is reserved by SQLAlchemy declarative; store extras under 'meta'.
    meta: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    type: Optional[LookupType] = Relationship(back_populates="values")
    translations: list["LookupTranslation"] = Relationship(
        back_populates="value", sa_relationship_kwargs=_CASCADE
    )
    aliases: list["LookupAlias"] = Relationship(
        back_populates="value", sa_relationship_kwargs=_CASCADE
    )


class LookupTranslation(SQLModel, table=True):
    """One display label per (value, language). Adding a language is data, not a migration."""

    __tablename__ = "lookup_translation"
    __table_args__ = (UniqueConstraint("value_id", "lang", name="uq_translation_value_lang"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    value_id: int = Field(foreign_key="lookup_value.id", index=True)
    lang: str = Field(index=True)  # BCP-47 / ISO-639: "en", "ar", ...
    label: str
    short_label: Optional[str] = None

    value: Optional[LookupValue] = Relationship(back_populates="translations")


class LookupAlias(SQLModel, table=True):
    """Synonyms and former names, so e.g. "Turkey" still resolves to code TR (Türkiye)."""

    __tablename__ = "lookup_alias"

    id: Optional[int] = Field(default=None, primary_key=True)
    value_id: int = Field(foreign_key="lookup_value.id", index=True)
    lang: Optional[str] = None  # null = language-agnostic
    alias: str = Field(index=True)

    value: Optional[LookupValue] = Relationship(back_populates="aliases")
