from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Amount = Annotated[int, Field(strict=True, ge=0, le=10**15)]
Name = Annotated[str, Field(min_length=1, max_length=100)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Models = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=128)]], Field(min_length=1, max_length=30)
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Strict):
    username: str = Field(max_length=100)
    password: str = Field(max_length=1024)


class StudentIdentity(Strict):
    student_number: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    name: Name | None = None


def aware_expiry(value):
    if value is not None and value.tzinfo is None:
        raise ValueError("timezone required")
    return value


class StudentImport(Strict):
    operation_id: str = Field(min_length=16, max_length=100)
    team_id: str = Field(max_length=36)
    students: list[StudentIdentity] = Field(min_length=1, max_length=100)
    models: Models
    budget: Amount = 0
    period: Literal["day", "month"] = "month"
    initial_password_mode: Literal["student_number", "random"] = "student_number"
    expires_at: datetime | None = None

    _aware = field_validator("expires_at")(aware_expiry)

    @field_validator("students")
    @classmethod
    def unique_students(cls, value):
        if len({s.student_number for s in value}) != len(value):
            raise ValueError("duplicate student numbers")
        return value


class PasswordChange(Strict):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=8, max_length=128)


class StudentKeyIssue(Strict):
    operation_id: str = Field(min_length=16, max_length=100)
    membership_id: str = Field(max_length=36)
    rotate: bool = False


class PasswordReset(Strict):
    reason: Reason


class TestUsageClear(Strict):
    confirm_name: Name


class TeamCreate(Strict):
    name: Name
    code: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    upstream_key: str = Field(min_length=8, max_length=512)
    models: Models
    budget: Amount = 0
    period: Literal["day", "month"] = "month"
    rpm: int = Field(default=60, strict=True, ge=1, le=100000)
    concurrency: int = Field(default=10, strict=True, ge=1, le=1000)
    max_output: int = Field(default=2048, strict=True, ge=1, le=65536)


class TeamPatch(Strict):
    active: bool | None = None
    name: Name | None = None
    models: Models | None = None
    rpm: int | None = Field(default=None, strict=True, ge=1, le=100000)
    concurrency: int | None = Field(default=None, strict=True, ge=1, le=1000)
    max_output: int | None = Field(default=None, strict=True, ge=1, le=65536)


class Enroll(Strict):
    operation_id: str = Field(min_length=16, max_length=100)
    name: Name
    student_number: str = Field(min_length=1, max_length=64)
    kind: Literal["student", "service"] = "student"
    initial_password_mode: Literal["student_number", "random"] | None = None
    owner: Name | None = None
    team_id: str = Field(max_length=36)
    budget: Amount = 0
    period: Literal["day", "month"] = "month"
    models: Models
    expires_at: datetime | None = None
    rpm: int = Field(default=20, strict=True, ge=1, le=100000)
    concurrency: int = Field(default=2, strict=True, ge=1, le=1000)

    @field_validator("name", "student_number", "owner")
    @classmethod
    def normalize_identity(cls, value):
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("identity cannot be blank")
        return value

    _aware = field_validator("expires_at")(aware_expiry)


class CredentialRotate(Strict):
    upstream_key: str = Field(min_length=8, max_length=512)


class KeyIssue(Strict):
    operation_id: str = Field(min_length=16, max_length=100)
    models: Models
    expires_at: datetime | None = None
    revoke_key_id: str | None = Field(default=None, max_length=36)

    _aware = field_validator("expires_at")(aware_expiry)


class BudgetUpdate(Strict):
    amount: Amount
    reason: Reason


class Reconcile(Strict):
    cost: Amount
    reason: Reason


class EventResolve(Strict):
    reason: Reason = "已检查"


class Message(Strict):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict] | None = None
    name: str | None = Field(default=None, max_length=100)
    tool_call_id: str | None = Field(default=None, max_length=128)
    tool_calls: list[dict] | None = None
    reasoning_content: str | None = None

    @field_validator("role", mode="before")
    @classmethod
    def instruction_role(cls, value):
        # OpenAI clients use developer for their system instructions. DeepSeek
        # Chat Completions represents the same instructions with system.
        return "system" if value == "developer" else value

    @field_validator("content")
    @classmethod
    def text_only(cls, value):
        if isinstance(value, list):
            if not value or any(
                set(x) != {"type", "text"} or x.get("type") != "text" or not isinstance(x.get("text"), str)
                for x in value
            ):
                raise ValueError("only text content blocks are enabled")
        return value

    @field_validator("tool_calls")
    @classmethod
    def function_calls_only(cls, value):
        if value is not None:
            for item in value:
                if set(item) - {"id", "type", "function"} or item.get("type") != "function":
                    raise ValueError("only client function calls are enabled")
                f = item.get("function")
                if (
                    not isinstance(f, dict)
                    or set(f) != {"name", "arguments"}
                    or not all(isinstance(v, str) for v in f.values())
                ):
                    raise ValueError("invalid function call")
        return value


class Chat(Strict):
    model: str = Field(min_length=1, max_length=128)
    messages: list[Message] = Field(min_length=1, max_length=500)
    stream: bool = False
    stream_options: dict | None = None
    # Admission clamps either client ceiling to the group's max_output before
    # reserving budget. Accept clients' generous defaults within SQL int bounds.
    max_tokens: int | None = Field(default=None, strict=True, ge=1, le=2147483647)
    max_completion_tokens: int | None = Field(default=None, strict=True, ge=1, le=2147483647)
    store: Literal[False] | None = Field(default=None, exclude=True)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    stop: str | list[str] | None = None
    tools: list[dict] | None = Field(default=None, max_length=100)
    tool_choice: str | dict | None = None
    response_format: dict | None = None
    thinking: dict | None = None
    reasoning_effort: Literal["low", "high", "max", "none"] | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = Field(default=None, ge=0, le=20)

    @field_validator("store", mode="before")
    @classmethod
    def no_storage(cls, value):
        if value is not None and value is not False:
            raise ValueError("only store false is supported")
        return value

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def official_reasoning_aliases(cls, value):
        if isinstance(value, str):
            return {"minimal": "low", "medium": "high", "xhigh": "high"}.get(value, value)
        return value

    @field_validator("tools")
    @classmethod
    def client_functions(cls, value):
        for tool in value or []:
            if set(tool) != {"type", "function"} or tool.get("type") != "function":
                raise ValueError("only client-defined function tools are enabled")
            f = tool["function"]
            if (
                not isinstance(f, dict)
                or not isinstance(f.get("name"), str)
                or set(f) - {"name", "description", "parameters", "strict"}
            ):
                raise ValueError("invalid function tool")
        return value

    @field_validator("tool_choice")
    @classmethod
    def client_choice(cls, value):
        if value is None or value in ("none", "auto", "required"):
            return value
        if isinstance(value, dict) and set(value) == {"type", "function"} and value.get("type") == "function":
            f = value["function"]
            if isinstance(f, dict) and set(f) == {"name"} and isinstance(f["name"], str):
                return value
        raise ValueError("invalid tool choice")

    @field_validator("thinking")
    @classmethod
    def thinking_mode(cls, value):
        if value is not None and value not in ({"type": "enabled"}, {"type": "disabled"}):
            raise ValueError("unsupported thinking options")
        return value

    @field_validator("response_format")
    @classmethod
    def response_type(cls, value):
        if value is not None and value not in ({"type": "text"}, {"type": "json_object"}):
            raise ValueError("unsupported response format")
        return value

    @field_validator("stream_options")
    @classmethod
    def usage_option(cls, value):
        if value is not None and (
            set(value) != {"include_usage"} or type(value["include_usage"]) is not bool
        ):
            raise ValueError("unsupported stream options")
        return value


def chat_validation_details(errors):
    """Return schema paths and fixed codes, never values or validator messages."""
    allowed = set(Chat.model_fields) | set(Message.model_fields) | {"body"}
    codes = {
        "extra_forbidden": "unsupported_field",
        "literal_error": "unsupported_value",
        "missing": "required",
        "less_than_equal": "out_of_range",
        "greater_than_equal": "out_of_range",
        "too_long": "too_long",
    }
    details = []
    for item in errors[:10]:
        parts = [
            str(part) if isinstance(part, int) or part in allowed else "unsupported_field"
            for part in item["loc"]
            if part != "body"
        ]
        details.append(
            {"parameter": ".".join(parts) or "body", "code": codes.get(item["type"], "invalid_value")}
        )
    return details


class PriceCreate(Strict):
    model: str
    upstream_model: str
    version: str
    input_miss: int
    input_hit: int
    output: int
    estimated: bool = True
    note: str
