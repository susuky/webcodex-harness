from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import get_type_hints

from mcp import types
from pydantic import ConfigDict, create_model


class OutcomeUnknown(ValueError):
    """An external side effect may have happened without a confirmed result."""


def tool(permission: str = "read"):
    def decorate(fn):
        fn.permission = permission
        return fn

    return decorate


@dataclass
class Definition:
    name: str
    permission: str
    model: type
    description: str

    def spec(self) -> types.Tool:
        return types.Tool(
            name=self.name,
            description=self.description,
            inputSchema=self.model.model_json_schema(),
            annotations=types.ToolAnnotations(
                readOnlyHint=self.permission in {"read", "computer_observe"},
                destructiveHint=self.permission not in {"read", "computer_observe"},
                idempotentHint=self.permission in {"read", "computer_observe"},
                openWorldHint=self.permission
                in {"execute", "git_write", "extensions", "computer_observe", "computer_control"},
            ),
        )


def definitions(cls, *, remote: bool) -> dict[str, Definition]:
    result = {}
    for name, fn in inspect.getmembers(cls, inspect.isfunction):
        if not hasattr(fn, "permission"):
            continue
        hints = get_type_hints(fn, include_extras=True)
        fields = {}
        for key, p in inspect.signature(fn).parameters.items():
            if key == "self":
                continue
            fields[key] = (hints[key], ... if p.default is p.empty else p.default)
        if remote:
            fields["task_id"] = (str, ...)
            fields["request_id"] = (str | None, None)
        model = create_model(name + "Arguments", __config__=ConfigDict(extra="forbid"), **fields)
        description = inspect.getdoc(fn) or name
        if remote:
            description += (
                " Requires an explicit task_id from task_create/task_list. Returns a durable job_id; "
                "follow next_action in this turn to observe queued/running work and remaining output. "
                "Only pending_approval needs an operator decision; show the returned details once, "
                "then resume that job after the decision. Check outcome/exit_code: job succeeded "
                "does not mean the command passed. "
                "Supply a stable request_id when retrying the same call to avoid duplicate effects. "
                "An unknown result must be investigated before retrying with a new request_id."
            )
        result[name] = Definition(name, fn.permission, model, description)
    return result
