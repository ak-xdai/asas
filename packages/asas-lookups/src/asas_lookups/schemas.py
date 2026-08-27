"""Request/response models for the lookup module."""

from datetime import date
from typing import Optional

from pydantic import BaseModel

from .models import TypeScope

# ---------- Read ----------


class LookupTypeRead(BaseModel):
    key: str
    name: str
    description: Optional[str] = None
    is_open: bool
    is_hierarchical: bool
    code_system: Optional[str] = None
    scope: TypeScope
    default_sort: str
    version: int


class LookupValueRead(BaseModel):
    code: str
    label: str  # resolved for the requested language (may be a fallback)
    short_label: Optional[str] = None
    lang: str  # the language actually returned (differs from requested on fallback)
    is_default: bool = False
    sort_order: int = 0
    status: str = "active"
    parent_code: Optional[str] = None
    meta: dict = {}
    aliases: list[str] = []  # search synonyms / former names (all languages)


class LookupValueList(BaseModel):
    type: str
    lang: str
    version: int
    total: int
    page: int
    page_size: int
    items: list[LookupValueRead]


class ResolveRequest(BaseModel):
    codes: list[str]
    lang: str = "en"


class ResolveResponse(BaseModel):
    type: str
    lang: str
    labels: dict[str, Optional[str]]  # code -> label (None if unknown)


# ---------- Admin / write ----------


class TranslationIn(BaseModel):
    lang: str
    label: str
    short_label: Optional[str] = None


class LookupTypeCreate(BaseModel):
    key: str
    name: str
    description: Optional[str] = None
    is_open: bool = False
    is_hierarchical: bool = False
    code_system: Optional[str] = None
    # Who owns the values (issue #35): "platform" (org-read-only reference
    # data, the default) or "org" (org-owned vocabulary, seeded per org).
    scope: TypeScope = TypeScope.platform
    default_sort: str = "label"


class LookupValueCreate(BaseModel):
    # For open lists ``code`` may be omitted and is derived from the English label.
    code: Optional[str] = None
    translations: list[TranslationIn]
    is_default: bool = False
    sort_order: int = 0
    parent_code: Optional[str] = None
    meta: dict = {}
    aliases: list[str] = []


class LookupValueUpdate(BaseModel):
    translations: Optional[list[TranslationIn]] = None  # upserted by lang
    is_default: Optional[bool] = None
    sort_order: Optional[int] = None
    meta: Optional[dict] = None


class DeprecateRequest(BaseModel):
    valid_to: Optional[date] = None
    superseded_by: Optional[str] = None  # code of the replacement value


class AliasIn(BaseModel):
    alias: str
    lang: Optional[str] = None


class MergeRequest(BaseModel):
    into: str  # target code; the source is deprecated and superseded by the target
