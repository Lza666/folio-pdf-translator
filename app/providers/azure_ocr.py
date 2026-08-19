from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx

from app.providers.base import OCRItem, ProviderError, ProviderNotConfigured


class AzureLayoutOCR:
    def __init__(self, *, endpoint: str, api_key: str | None, api_version: str) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.api_version = api_version
        if not self.endpoint or not self.api_key:
            raise ProviderNotConfigured("扫描页需要先配置 Azure Document Intelligence")
        parsed = urlparse(self.endpoint)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ProviderError("Azure endpoint 必须是有效 HTTPS 地址")

    def analyze(self, content: bytes, content_type: str = "image/png") -> list[OCRItem]:
        url = (
            f"{self.endpoint}/documentintelligence/documentModels/prebuilt-layout:analyze"
            f"?api-version={self.api_version}"
        )
        headers = {"Ocp-Apim-Subscription-Key": self.api_key, "Content-Type": content_type}
        response = httpx.post(url, headers=headers, content=content, timeout=120)
        if response.status_code not in {200, 202}:
            raise ProviderError(f"Azure OCR 请求失败：HTTP {response.status_code}")
        if response.status_code == 200:
            payload = response.json()
        else:
            operation = response.headers.get("operation-location")
            if not operation:
                raise ProviderError("Azure OCR 未返回 operation-location")
            payload = None
            for _ in range(90):
                poll = httpx.get(operation, headers={"Ocp-Apim-Subscription-Key": self.api_key}, timeout=30)
                poll.raise_for_status()
                data = poll.json()
                status = data.get("status")
                if status == "succeeded":
                    payload = data
                    break
                if status == "failed":
                    raise ProviderError("Azure OCR 分析失败")
                time.sleep(1)
            if payload is None:
                raise ProviderError("Azure OCR 轮询超时")
        result = payload.get("analyzeResult", payload)
        items: list[OCRItem] = []
        for paragraph in result.get("paragraphs", []):
            regions = paragraph.get("boundingRegions") or []
            polygon = regions[0].get("polygon", []) if regions else []
            text = (paragraph.get("content") or "").strip()
            if text and len(polygon) >= 8:
                items.append(OCRItem(text=text, polygon=[float(value) for value in polygon]))
        return items

    def test(self) -> tuple[bool, str, int]:
        started = time.perf_counter()
        url = f"{self.endpoint}/documentintelligence/info?api-version={self.api_version}"
        try:
            response = httpx.get(
                url, headers={"Ocp-Apim-Subscription-Key": self.api_key}, timeout=20
            )
            ok = response.status_code < 400
            message = "Azure OCR 连接测试通过" if ok else f"Azure 返回 HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            ok, message = False, f"Azure 连接失败：{exc}"
        return ok, message, round((time.perf_counter() - started) * 1000)
