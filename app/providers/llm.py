from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.language import target_language_name
from app.providers.base import ProviderError, ProviderNotConfigured, TranslationItem


class OpenAICompatibleTranslator:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        extra: dict[str, Any] | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.extra = extra or {}
        self.timeout = timeout
        if not self.base_url or not self.api_key or not self.model:
            raise ProviderNotConfigured("请先在服务设置中配置模型地址、API Key 和模型名称")

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/chat/completions"
        return f"{self.base_url}/v1/chat/completions"

    @staticmethod
    def _parse_response(
        text: str,
        expected_ids: set[str],
        max_characters: dict[str, int] | None = None,
    ) -> dict[str, str]:
        value = text.strip()
        if value.startswith("```"):
            value = value.split("\n", 1)[-1]
            value = value.rsplit("```", 1)[0]
        start, end = value.find("{"), value.rfind("}")
        if start < 0 or end <= start:
            raise ProviderError("模型未返回 JSON 对象")
        try:
            payload = json.loads(value[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ProviderError(f"模型返回了无效 JSON：{exc.msg}") from exc
        rows = payload.get("translations")
        if not isinstance(rows, list):
            raise ProviderError("模型响应缺少 translations 数组")
        result: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                raise ProviderError("模型响应包含无效片段")
            item_id = row["id"]
            translated = row.get("text")
            if item_id in result or item_id not in expected_ids:
                raise ProviderError(f"模型响应包含重复或未知 ID：{item_id}")
            if not isinstance(translated, str) or not translated.strip():
                raise ProviderError(f"模型响应中的译文为空：{item_id}")
            translated = translated.strip()
            limit = (max_characters or {}).get(item_id)
            if limit is not None and len(translated) > limit:
                raise ProviderError(
                    f"模型响应超过字符预算：{item_id}（{len(translated)} > {limit}）"
                )
            result[item_id] = translated
        missing = expected_ids - result.keys()
        if missing:
            raise ProviderError(f"模型漏掉片段：{', '.join(sorted(missing))}")
        return result

    def translate(
        self, items: list[TranslationItem], target_language: str, terms: dict[str, str]
    ) -> dict[str, str]:
        language_name = target_language_name(target_language)
        term_lines = "\n".join(f"- {source} => {target}" for source, target in terms.items())
        system = (
            "You are a precise professional document translator. Translate every input item into "
            f"{language_name}. Preserve meaning, numbers, names, inline formatting and tone. "
            "Do not add explanations. Return only one JSON object with this exact shape: "
            '{"translations":[{"id":"unchanged id","text":"translation"}]}. '
            "Every input id must occur exactly once."
        )
        if term_lines:
            system += f"\nUse these mandatory terms when applicable:\n{term_lines}"
        if any(item.text.lstrip().startswith("<folio") for item in items):
            system += (
                "\nSome items contain FOLIO XML. Translate only the text inside each <run> element. "
                "Return the complete XML as that item's text value. Preserve every element name, id, "
                "attribute, nesting level and element order exactly; do not add, remove, merge or split "
                "paragraph, list-item or run elements. XML-escape reserved characters in translated text."
            )
        limits = {
            item.id: item.max_characters
            for item in items
            if item.max_characters is not None
        }
        if limits:
            system += (
                "\nSome input items include max_characters. For those items, rewrite the translation "
                "as a concise but complete professional translation within that hard Unicode-character "
                "limit, counting spaces and punctuation. Preserve every number, name, negation, legal or "
                "modal meaning, and mandatory term. Remove redundancy and use standard abbreviations where "
                "safe. Never use an ellipsis and never mention the limit."
            )
        user_payload = {
            "target_language": target_language,
            "segments": [
                {
                    "id": item.id,
                    "source_language": item.source_language,
                    "context": item.context,
                    "text": item.text,
                    **(
                        {"max_characters": item.max_characters}
                        if item.max_characters is not None
                        else {}
                    ),
                }
                for item in items
            ],
        }
        request_payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            **self.extra,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = httpx.post(
                    self.endpoint,
                    headers=headers,
                    json=request_payload,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                return self._parse_response(
                    content,
                    {item.id for item in items},
                    limits,
                )
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, ProviderError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2**attempt)
        raise ProviderError(f"翻译服务在三次尝试后仍失败：{last_error}")

    def test(self) -> tuple[bool, str, int]:
        started = time.perf_counter()
        try:
            result = self.translate(
                [
                    TranslationItem(id="en", text="A clear page is easier to review.", source_language="en"),
                    TranslationItem(id="fr", text="La précision compte.", source_language="fr"),
                ],
                "zh-Hans",
                {},
            )
            ok = set(result) == {"en", "fr"}
            message = "模型连接和片段映射测试通过" if ok else "模型响应片段不完整"
        except ProviderError as exc:
            ok, message = False, str(exc)
        return ok, message, round((time.perf_counter() - started) * 1000)
