"""Claude calls: one structured call, and a bounded tool-using agent loop.

Every call goes through the same ledger so a run knows what it cost.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from dowel.config import PRICES
from dowel.schemas import Usage

T = TypeVar("T", bound=BaseModel)

# Server-side fallback: if a request is declined by a safety classifier (security-flavoured
# review prompts can trip one), the API re-runs it on a fallback model inside the same call.
_BETAS = ["server-side-fallback-2026-07-01"]

ToolFn = Callable[[dict[str, Any]], Awaitable[str]]


class LLMError(RuntimeError):
    pass


class CostCapReached(LLMError):
    pass


def _first_call_usd(model: str) -> float:
    """What to hold for a model's first call in a run, before it has an average of its own."""
    return 0.01 if "haiku" in model else 0.10 if "opus" in model else 0.04


class Ledger:
    def __init__(self, cap_usd: float) -> None:
        self.usage = Usage()
        self._cap = cap_usd
        self._held = 0.0
        self._calls = 0
        self._by_model: dict[str, tuple[float, int]] = {}  # model -> (dollars, calls)

    def add(self, model: str, usage: Any) -> None:
        inp = getattr(usage, "input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
        read = getattr(usage, "cache_read_input_tokens", 0) or 0
        write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        self._calls += 1
        p_in, p_out = PRICES.get(model, (4.0, 20.0))
        self.usage.input_tokens += inp
        self.usage.output_tokens += out
        self.usage.cache_read_tokens += read
        self.usage.cache_write_tokens += write
        cost = (inp * p_in + read * p_in * 0.1 + write * p_in * 1.25 + out * p_out) / 1e6
        self.usage.cost_usd += cost
        self.usage.by_model[model] = round(self.usage.by_model.get(model, 0.0) + cost, 5)
        spent, calls = self._by_model.get(model, (0.0, 0))
        self._by_model[model] = (spent + cost, calls + 1)

    def reserve(self, model: str = "") -> float:
        """Set money aside for a call before it starts, so calls running side by side cannot overshoot.

        The amount held is what a call to this model has cost on average so far in this run. It is
        kept per model because one expensive planning call says nothing about twenty cheap checks:
        holding the run-wide average for each of them stopped reviews long before their limit.
        """
        spent, calls = self._by_model.get(model, (0.0, 0))
        hold = spent / calls if calls else _first_call_usd(model)
        if self.usage.cost_usd + self._held + hold > self._cap:
            raise CostCapReached(f"cost cap of ${self._cap:.2f} reached")
        self._held += hold
        return hold

    def release(self, hold: float) -> None:
        self._held = max(0.0, self._held - hold)


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    return {"type": "json_schema", "schema": model.model_json_schema()}


def _text(response: Any) -> str:
    return "".join(b.text for b in response.content if b.type == "text")


def _parse(model: type[T], response: Any) -> T:  # noqa: UP047 - T is shared with the methods below
    if response.stop_reason == "refusal":
        raise LLMError("the model declined this request")
    if response.stop_reason == "max_tokens":
        raise LLMError("the model ran out of output room before finishing")
    try:
        return model.model_validate_json(_text(response))
    except ValidationError as exc:
        raise LLMError(f"reply did not match {model.__name__}: {exc}") from exc


class Claude:
    def __init__(self, ledger: Ledger) -> None:
        self._client = anthropic.AsyncAnthropic()
        self._ledger = ledger

    async def _create(self, **kwargs: Any) -> Any:
        hold = self._ledger.reserve(kwargs["model"])
        try:
            if kwargs["model"].startswith("claude-haiku"):  # no server-side refusal fallback on Haiku
                response = await self._client.messages.create(**kwargs)
            else:
                response = await self._client.beta.messages.create(betas=_BETAS, fallbacks="default", **kwargs)
        except anthropic.RateLimitError as exc:
            raise LLMError("rate limited by the Claude API") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("could not reach the Claude API") from exc
        finally:
            self._ledger.release(hold)
        self._ledger.add(kwargs["model"], response.usage)
        return response

    async def structured(
        self,
        *,
        model: str,
        system: str | list[dict[str, Any]],
        user: str,
        output: type[T],
        effort: str = "medium",
        max_tokens: int = 16000,
    ) -> T:
        response = await self._create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": _schema(output)},
        )
        return _parse(output, response)

    async def agent(
        self,
        *,
        model: str,
        system: str | list[dict[str, Any]],
        user: str,
        output: type[T],
        tools: list[dict[str, Any]],
        run_tool: Callable[[str, dict[str, Any]], Awaitable[str]],
        max_tool_calls: int,
        effort: str = "low",
        max_tokens: int = 8000,
        on_tool: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> T:
        """Let the model use read-only tools up to a budget, then return a structured answer."""
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        used = 0
        for _ in range(max_tool_calls + 4):
            response = await self._create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                tools=tools,
                messages=messages,
                output_config={"effort": effort, "format": _schema(output)},
            )
            if response.stop_reason != "tool_use":
                return _parse(output, response)

            messages.append({"role": "assistant", "content": response.content})
            results: list[dict[str, Any]] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                if used >= max_tool_calls:
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "Tool budget used up. Give your answer now from what you have.",
                            "is_error": True,
                        }
                    )
                    continue
                used += 1
                args = block.input if isinstance(block.input, dict) else {}
                if on_tool:
                    on_tool(block.name, args)
                try:
                    content = await run_tool(block.name, args)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
                except Exception as exc:  # noqa: BLE001 - a failed or malformed tool call is told to the model, never a crash
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Error: {exc}",
                            "is_error": True,
                        }
                    )
            messages.append({"role": "user", "content": results})
        raise LLMError("the model kept calling tools past its budget")


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
