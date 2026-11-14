"""Thin mini-harness client used by the translation pipeline.

This mirrors the harness HTTP contract rather than embedding any harness code:
the platform remains deployable with mini-harness running elsewhere.  The
client is sync because translation workers are plain threads.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .config import Settings


class HarnessError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.details = details or {}


def _error(response: httpx.Response) -> HarnessError:
    message = f"mini-harness HTTP {response.status_code}"
    code = None
    details: dict[str, Any] = {}
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or message)
            code = error.get("code")
            details = error.get("details") or {}
        elif data.get("detail"):
            message = str(data["detail"])
    return HarnessError(message, status=response.status_code, code=code, details=details)


class HarnessClient:
    def __init__(self, settings: Settings, *, base_url: str | None = None) -> None:
        if not settings.harness_api_key:
            raise HarnessError(
                "未配置 HARNESS_API_KEY，无法调用 mini-harness。请在 .env 中设置平台专用 Key。"
            )
        self.settings = settings
        self._client = httpx.Client(
            base_url=(base_url or settings.harness_base_url).rstrip("/"),
            timeout=httpx.Timeout(settings.harness_timeout_s, connect=15.0),
            headers={"Authorization": f"Bearer {settings.harness_api_key}"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HarnessClient:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def health(self) -> dict[str, Any]:
        response = self._client.get("/healthz", timeout=5)
        if response.status_code >= 400:
            raise _error(response)
        return response.json()

    def models(self) -> dict[str, Any]:
        response = self._client.get("/v1/models")
        if response.status_code >= 400:
            raise _error(response)
        return response.json()

    def run(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        max_steps: int = 1,
        temperature: float = 0.2,
        max_tokens: int = 16_000,
        images: list[str | dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "prompt": prompt,
            "max_steps": max_steps,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if system:
            payload["system"] = system
        if model:
            payload["model"] = model
        if images:
            payload["images"] = [
                {"url": image} if isinstance(image, str) else image for image in images
            ]
        response = self._client.post("/v1/agent/run", json=payload)
        if response.status_code >= 400:
            raise _error(response)
        return response.json()

    def run_json(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 16_000,
        retries: int = 2,
        images: list[str | dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
        """Run once and parse a JSON object from the final answer.

        Returns ``(payload, raw_response, usage)``.  Retries append a short repair
        instruction while keeping the temperature at zero for deterministic JSON.
        """
        last_error = ""
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        for attempt in range(retries + 1):
            suffix = ""
            if attempt:
                suffix = (
                    "\n\n上一次输出无法被 JSON 解析。请只输出一个合法 JSON 对象，"
                    "不要 Markdown 代码块、不要解释。"
                )
            raw = self.run(
                prompt + suffix,
                system=system,
                model=model,
                temperature=0.0 if attempt else 0.15,
                max_tokens=max_tokens,
                images=images,
            )
            usage = raw.get("usage") or {}
            for key in total_usage:
                total_usage[key] += int(usage.get(key) or 0)
            try:
                parsed = parse_json_object(str(raw.get("output") or ""))
                return parsed, raw, total_usage
            except ValueError as exc:
                last_error = str(exc)
                last_raw = raw
        raise HarnessError(f"AI 返回内容无法解析为 JSON：{last_error}", details={"raw": last_raw})


def parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("empty response")
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object found") from None
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc.msg}") from None
    if not isinstance(value, dict):
        raise ValueError("top-level JSON is not an object")
    return value
