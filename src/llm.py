"""Model access through Gravity's LLM endpoint only (never a key of our own). Small prompts, typed outputs,
a per-call timeout, and a bounded retry. Every attempt is charged to the run's call budget, so retries
can never push a run past the model-call limit in the spec."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import BaseModel


class GatewayLLM:
    def __init__(self, base_url: str, token: str, model: str = "openai/gpt-6-luna", timeout: float = 60.0,
                 retries: int = 1, backoff: float = 1.5):
        self.base_url, self.token, self.model = base_url, token, model
        self.timeout, self.retries, self.backoff = timeout, retries, backoff
        self._m = None

    def _model(self):
        if self._m is None:
            from langchain_openai import ChatOpenAI
            self._m = ChatOpenAI(model=self.model, base_url=self.base_url, api_key=self.token, timeout=self.timeout, max_retries=0, temperature=0)
        return self._m

    async def structured(self, schema: type[BaseModel], system: str, payload: dict[str, Any], budget=None) -> BaseModel:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            if budget is not None:
                budget.spend()  # raises BudgetExceeded: a retry is a call too
            try:
                res = await asyncio.wait_for(
                    self._model().with_structured_output(schema).ainvoke(
                        [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, default=str)}]),
                    timeout=self.timeout + 5)
                return res if isinstance(res, schema) else schema.model_validate(res)
            except Exception as e:  # noqa: BLE001 - retried once, then surfaced to the caller, which falls back to rules
                last = e
                if attempt < self.retries:
                    await asyncio.sleep(self.backoff * (attempt + 1))
        raise RuntimeError(f"model call failed after {self.retries + 1} attempts: {str(last)[:160]}")
