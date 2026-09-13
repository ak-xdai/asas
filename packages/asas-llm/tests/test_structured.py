from typing import Literal

import pytest
from pydantic import BaseModel, Field

from asas_llm.errors import StructuredOutputError
from asas_llm.structured import parse_structured, response_format, strict_json_schema, strip_code_fence


class Skill(BaseModel):
    name: str
    level: Literal["junior", "senior"] = "junior"


class Profile(BaseModel):
    """Candidate profile."""

    full_name: str
    years: int | None = None
    skills: list[Skill] = Field(default_factory=list)
    manager: "Skill | None" = None


def test_strict_schema_requires_everything_and_closes_objects():
    schema = strict_json_schema(Profile)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["full_name", "years", "skills", "manager"]
    skill = schema["$defs"]["Skill"]
    assert skill["additionalProperties"] is False and skill["required"] == ["name", "level"]


def test_strict_schema_strips_none_defaults_and_keeps_others():
    schema = strict_json_schema(Profile)
    assert "default" not in schema["properties"]["years"]
    assert schema["$defs"]["Skill"]["properties"]["level"]["default"] == "junior"


def test_strict_schema_unravels_ref_with_siblings():
    class Wrapper(BaseModel):
        inner: Skill = Field(description="the skill")

    schema = strict_json_schema(Wrapper)
    inner = schema["properties"]["inner"]
    assert "$ref" not in inner and inner["description"] == "the skill" and inner["additionalProperties"] is False


def test_strict_schema_rejects_non_models():
    with pytest.raises(TypeError):
        strict_json_schema(dict)  # type: ignore[arg-type]


def test_response_format_shape():
    rf = response_format(Profile)
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"] == "Profile"
    assert rf["json_schema"]["schema"]["additionalProperties"] is False
    assert response_format(Profile, name="custom")["json_schema"]["name"] == "custom"


def test_strip_code_fence():
    assert strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_code_fence('{"a": 1}') == '{"a": 1}'


def test_parse_repairs_fenced_and_broken_json():
    text = '```json\n{"full_name": "Ana", "years": 7, "skills": [{"name": "SQL",},],}\n```'
    profile = parse_structured(text, Profile)
    assert profile.full_name == "Ana" and profile.skills[0].name == "SQL"


def test_parse_failure_carries_raw_and_errors():
    with pytest.raises(StructuredOutputError) as info:
        parse_structured('{"years": "seven"}', Profile, trace_id="t1", prompt="parse-cv")
    err = info.value
    assert err.trace_id == "t1" and err.prompt == "parse-cv"
    assert err.raw == '{"years": "seven"}'
    assert any(e["loc"] == ("full_name",) for e in err.errors)


def test_parse_empty_is_structured_error():
    with pytest.raises(StructuredOutputError):
        parse_structured("   ", Profile)


def test_parse_without_repair_fails_on_broken_json():
    with pytest.raises(StructuredOutputError):
        parse_structured('{"full_name": "Ana",}', Profile, repair=False)
    assert parse_structured('{"full_name": "Ana",}', Profile).full_name == "Ana"


def test_raw_is_truncated():
    with pytest.raises(StructuredOutputError) as info:
        parse_structured("x" * 10_000, Profile)
    assert len(info.value.raw) == StructuredOutputError.RAW_LIMIT
