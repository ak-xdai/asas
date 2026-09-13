"""Getting a typed object out of a model without it returning broken JSON.

Three layers, each cheap, together the pattern that held up in production:

1. **Ask strictly.** :func:`strict_json_schema` turns a pydantic model into the
   schema shape OpenAI-compatible providers enforce under ``strict: true``
   (every property required, ``additionalProperties: false``, no ``$ref`` with
   siblings). :func:`response_format` wraps it as the ``response_format``
   argument. Under strict decoding the model *cannot* emit a key that is not in
   the schema.
2. **Repair before parsing.** Providers that ignore or do not support strict
   mode still return near-JSON: a trailing comma, a code fence, a truncated
   string. :func:`parse_structured` strips fences and runs ``json_repair``
   before validating, which turns most of those into a parse instead of a
   retry.
3. **Fail with the evidence.** When it still does not validate the failure is
   a :class:`~asas_llm.errors.StructuredOutputError` carrying the raw text and
   pydantic's error list, so the bad output can be read from the exception
   rather than re-produced.

The strict-schema walker is a port of the one the OpenAI SDK ships privately,
kept here so the package does not import ``openai`` for a schema transform.
"""

from __future__ import annotations

import inspect
import re
from typing import Any, TypeVar

import pydantic
from json_repair import repair_json

from .errors import StructuredOutputError

T = TypeVar("T", bound=pydantic.BaseModel)

_MISSING = object()
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n(.*?)\n\s*```\s*$", re.S)


def strict_json_schema(model: type[pydantic.BaseModel] | pydantic.TypeAdapter) -> dict[str, Any]:
    """The model's JSON schema, rewritten to the ``strict`` dialect."""
    if inspect.isclass(model) and issubclass(model, pydantic.BaseModel):
        schema = model.model_json_schema()
    elif isinstance(model, pydantic.TypeAdapter):
        schema = model.json_schema()
    else:
        raise TypeError(f"expected a pydantic BaseModel class or TypeAdapter, got {model!r}")
    return _ensure_strict(schema, path=(), root=schema)


def response_format(model: type[pydantic.BaseModel], *, name: str | None = None) -> dict[str, Any]:
    """The ``response_format`` argument for an OpenAI-compatible chat call."""
    schema = strict_json_schema(model)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name or schema.get("title") or model.__name__,
            "strict": True,
            "schema": schema,
        },
    }


def strip_code_fence(text: str) -> str:
    """``\\`\\`\\`json ... \\`\\`\\``` around an answer is the single most common way a
    model breaks "return only JSON"."""
    match = _FENCE.match(text)
    return match.group(1) if match else text


def parse_structured(
    text: str,
    model: type[T],
    *,
    repair: bool = True,
    trace_id: str | None = None,
    prompt: str | None = None,
) -> T:
    """Turn model output into ``model``, repairing the JSON first when asked."""
    candidate = strip_code_fence(text or "")
    if not candidate.strip():
        raise StructuredOutputError(
            "model returned no JSON to parse", raw=text or "", trace_id=trace_id, prompt=prompt
        )
    if repair:
        try:
            candidate = repair_json(candidate)
        except Exception:  # pragma: no cover - repair_json is very forgiving
            pass
    try:
        return model.model_validate_json(candidate)
    except pydantic.ValidationError as exc:
        raise StructuredOutputError(
            f"model output does not validate as {model.__name__}",
            raw=text,
            errors=exc.errors(),
            trace_id=trace_id,
            prompt=prompt,
        ) from exc


# --- strict schema walker -----------------------------------------------------


def _ensure_strict(schema: object, *, path: tuple[str, ...], root: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(schema, dict):
        raise TypeError(f"expected a dict at {'/'.join(path) or '<root>'}, got {schema!r}")

    for key in ("$defs", "definitions"):
        defs = schema.get(key)
        if isinstance(defs, dict):
            for name, sub in defs.items():
                _ensure_strict(sub, path=(*path, key, name), root=root)

    if schema.get("type") == "object" and "additionalProperties" not in schema:
        schema["additionalProperties"] = False

    properties = schema.get("properties")
    if isinstance(properties, dict):
        schema["required"] = list(properties.keys())
        schema["properties"] = {
            k: _ensure_strict(v, path=(*path, "properties", k), root=root) for k, v in properties.items()
        }

    items = schema.get("items")
    if isinstance(items, dict):
        schema["items"] = _ensure_strict(items, path=(*path, "items"), root=root)

    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        schema["anyOf"] = [
            _ensure_strict(v, path=(*path, "anyOf", str(i)), root=root) for i, v in enumerate(any_of)
        ]

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        if len(all_of) == 1:
            schema.update(_ensure_strict(all_of[0], path=(*path, "allOf", "0"), root=root))
            schema.pop("allOf")
        else:
            schema["allOf"] = [
                _ensure_strict(v, path=(*path, "allOf", str(i)), root=root) for i, v in enumerate(all_of)
            ]

    # A ``None`` default carries no information under strict mode (the field is
    # nullable either way) and some providers reject it.
    if schema.get("default", _MISSING) is None:
        schema.pop("default")

    ref = schema.get("$ref")
    if ref and len(schema) > 1:
        if not isinstance(ref, str):
            raise ValueError(f"non-string $ref at {'/'.join(path)}: {ref!r}")
        resolved = _resolve_ref(root, ref)
        schema.update({**resolved, **schema})
        schema.pop("$ref")
        return _ensure_strict(schema, path=path, root=root)

    return schema


def _resolve_ref(root: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported $ref {ref!r}: only local '#/' references are handled")
    node: Any = root
    for key in ref[2:].split("/"):
        if not isinstance(node, dict) or key not in node:
            raise ValueError(f"$ref {ref!r} does not resolve")
        node = node[key]
    if not isinstance(node, dict):
        raise ValueError(f"$ref {ref!r} resolved to a non-object")
    return node
