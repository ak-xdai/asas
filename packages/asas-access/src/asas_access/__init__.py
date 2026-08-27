"""Asas access control — configuration-driven permissions over one principal layer.

Extracted from Teamy (epic WXL-112, actions TEAMY-452, groups TEAMY-453;
extraction epic TEAMY-466, design record 0017). Three concerns:

* **Field permissions** — which *principals* may view/edit each field of an entity
  type, stored as data in ``field_permission``.
* **Action permissions** — which principals may perform org-wide action verbs
  (``team.create``, ``audit.view``, …), stored as data in ``action_permission``.
* **Record visibility** — public/private records with registered private viewers.

A **principal** is a role tier (``admin``/``member``/``viewer``), a group key
(org-wide functional bundle; membership resolved query-time via a host-registered
global source), or a relationship resolved per (user, record) (``self``,
``supervisor``, ``team_*``, …). Hosts register resolvers/sources/fields/verbs at
boot — the package never imports host models.

Semantics are safe-by-default: a field with no policy rows keeps the caller's
baseline rule (this engine returns "allowed"); an unconfigured verb is admin-only;
``admin`` is always allowed (the implicit floor); only an *actual* change to a
restricted field is rejected.

Host contract (table-owning variant): :func:`migrate` (package Alembic chain,
adopt-or-create), the ``register_*``/``reserve_principals`` hooks, and
:func:`seed_field_permissions` / :func:`seed_action_permissions` taking the
host's policy data (grant defaults live host-side, like validation rule
catalogs). Enforcement on/off is the caller's concern throughout.
"""

from .actions import (
    action_allowed,
    action_allowed_for,
    granted_principals,
    invalidate_action_cache,
    known_actions,
    record_scoped_actions,
    register_actions,
)
from .catalog import known_fields, register_fields
from .groups import (
    ensure_system_groups,
    group_by_key,
    reserve_principals,
    reserved_principals,
    validate_group_key,
)
from .mac import (
    classified_entities,
    ensure_clearance_levels,
    invalidate_mac_cache,
    invalidate_record_markings,
    mac_allows,
    record_markings,
    register_classified_entity,
    register_subject_source,
    subject_access_profile,
)
from .migrate import migrate
from .models import (
    AccessGroup,
    AccessGroupMembership,
    ActionPermission,
    ClearanceLevel,
    FieldPermission,
    Marking,
    RecordMarking,
    SubjectClearance,
    SubjectMarking,
)
from .policy import (
    can_edit_field,
    can_view_field,
    forbidden_edits,
    invalidate_cache,
    redact_view,
    view_restricted_fields,
)
from .principals import (
    CHANGE_APPROVER,
    PROJECT_LEAD,
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_VIEWER,
    SELF,
    SUPERVISOR,
    TEAM_LEAD,
    TEAM_MEMBER,
    held_principals,
    register_global_source,
    register_record_source,
    register_resolver,
)
from .seeding import seed_action_permissions, seed_field_permissions
from .visibility import (
    PRIVATE,
    PUBLIC,
    VIEW_PRIVATE,
    can_view_record,
    register_private_viewers,
)

__version__ = "0.15.0"

__all__ = [
    "AccessGroup",
    "AccessGroupMembership",
    "ActionPermission",
    "CHANGE_APPROVER",
    "ClearanceLevel",
    "FieldPermission",
    "Marking",
    "PRIVATE",
    "RecordMarking",
    "SubjectClearance",
    "SubjectMarking",
    "PROJECT_LEAD",
    "PUBLIC",
    "ROLE_ADMIN",
    "ROLE_MEMBER",
    "ROLE_VIEWER",
    "SELF",
    "SUPERVISOR",
    "TEAM_LEAD",
    "TEAM_MEMBER",
    "VIEW_PRIVATE",
    "action_allowed",
    "action_allowed_for",
    "can_edit_field",
    "can_view_field",
    "can_view_record",
    "classified_entities",
    "ensure_clearance_levels",
    "ensure_system_groups",
    "forbidden_edits",
    "group_by_key",
    "granted_principals",
    "held_principals",
    "invalidate_action_cache",
    "invalidate_cache",
    "invalidate_mac_cache",
    "invalidate_record_markings",
    "known_actions",
    "known_fields",
    "mac_allows",
    "migrate",
    "record_markings",
    "record_scoped_actions",
    "redact_view",
    "register_actions",
    "register_classified_entity",
    "register_fields",
    "register_global_source",
    "register_private_viewers",
    "register_record_source",
    "register_resolver",
    "register_subject_source",
    "subject_access_profile",
    "reserve_principals",
    "reserved_principals",
    "seed_action_permissions",
    "seed_field_permissions",
    "validate_group_key",
    "view_restricted_fields",
    "__version__",
]
