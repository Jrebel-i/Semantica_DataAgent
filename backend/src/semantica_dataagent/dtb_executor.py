"""DTB query executor; credentials are supplied by the caller for each instance."""

import asyncio
import re
from typing import ClassVar
from urllib.parse import quote

import httpx

from .dtb_auth import authentication_failed
from .failure_diagnostics import (
    safe_dtb_category,
    safe_dtb_stage,
)
from .plugins import ExecutorAuthenticationError, ExecutorError


def column_names(columns):
    """Return result column names, or None when the structure is invalid.

    The DTB result endpoint returns ``{"name": ..., "type": ...}`` objects;
    plain strings are accepted for compatibility with other executors and
    older fixtures.
    """
    if not isinstance(columns, list):
        return None
    names = []
    for column in columns:
        if isinstance(column, str):
            names.append(column)
        elif isinstance(column, dict) and isinstance(column.get("name"), str):
            names.append(column["name"])
        else:
            return None
    return names


class DtbError(ExecutorError):
    """An executor failure whose public message never includes raw platform text."""

    def __init__(self, message, *, stage="unknown", category="unknown"):
        super().__init__(message)
        self.failure_stage = safe_dtb_stage(stage)
        self.failure_category = safe_dtb_category(category)


class DtbAuthenticationError(DtbError, ExecutorAuthenticationError):
    def __init__(self, message, *, stage="unknown"):
        super().__init__(message, stage=stage, category="authentication")


def _request_stage(path):
    if path == "/api/data-query/async-execute":
        return "submit"
    if path.startswith("/api/data-query/result/"):
        return "poll"
    if path.startswith("/api/data-query/download"):
        return "download"
    return "unknown"


def _exception_category(exc):
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.TransportError):
        return "data_source"
    return "unknown"


_MISSING_TABLE = re.compile(
    r"Table\[([A-Za-z0-9_.@#$\-]{1,128})\]"
    r"\s*does not exist\s*in\s*database\[([A-Za-z0-9_.@#$\-]{1,128})\]"
)


def _poll_error_category(message):
    """Map only the known, bounded DTB missing-table shape; everything else is unknown.

    ``errorMessage`` is free-form platform text, so a keyword search would let
    arbitrary SQL, literals or credentials be misclassified.  The one shape
    observed in production is matched as a whole with identifier-only captures.
    """
    if not isinstance(message, str):
        return "unknown"
    return (
        "table_or_catalog" if _MISSING_TABLE.fullmatch(message.strip()) else "unknown"
    )


class DtbExecutor:
    id = "dtb"
    version = "1.0"
    display_name = "DTB"
    parameters_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "base_url": {"type": "string", "format": "uri", "title": "DTB 地址"},
            "row_limit": {
                "type": "integer",
                "minimum": 1,
                "default": 1000,
                "title": "返回行数上限",
            },
            "download_limit": {
                "type": "integer",
                "minimum": 1,
                "default": 100000,
                "title": "下载行数上限",
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "default": 300,
                "title": "超时秒数",
            },
            "confirmation_ttl_seconds": {
                "type": "integer",
                "minimum": 1,
                "default": 1800,
                "title": "查询确认有效期（秒）",
            },
            "interpret_results": {
                "type": "boolean",
                "default": True,
                "title": "允许 agent 解读查询结果",
            },
        },
        "required": ["base_url"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        base_url="",
        token="",
        *,
        client=None,
        retry_delays=(5, 15, 45),
        poll_interval=2,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.client = client
        self.retry_delays = retry_delays
        self.poll_interval = poll_interval

    def bind(self, parameters, token):
        return DtbExecutor(parameters["base_url"], token)

    async def _request(self, method, path, *, raw=False, **kwargs):
        allowed = (method == "POST" and path == "/api/data-query/async-execute") or (
            method == "GET"
            and (
                path == "/api/data-query/download"
                or re.fullmatch(
                    r"/api/data-query/(?:result|download)/[A-Za-z0-9_-]+", path
                )
            )
        )
        if not allowed:
            raise ValueError("DTB 请求不在允许的接口白名单中")
        headers = {
            "token": self.token,
            "x-requested-with": "XMLHttpRequest",
            "cookie": f"satoken={self.token}",
        }
        stage = _request_stage(path)
        for attempt in range(len(self.retry_delays) + 1):
            try:
                async with (
                    httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))
                    if self.client is None
                    else _borrow(self.client) as client
                ):
                    response = await client.request(
                        method, self.base_url + path, headers=headers, **kwargs
                    )
                if authentication_failed(response.status_code, None):
                    raise DtbAuthenticationError(
                        "DTB token 失效，请在个人设置中更新", stage=stage
                    )
                response.raise_for_status()
                if raw:
                    if not response.content:
                        raise DtbError(
                            "DTB 下载结果为空", stage=stage, category="result"
                        )
                    return response.content, response.headers.get("content-type")
                payload = response.json()
                if not isinstance(payload, dict):
                    raise DtbError("DTB 响应格式错误", stage=stage, category="unknown")
                status = str(payload.get("status", ""))
                if authentication_failed(response.status_code, payload):
                    raise DtbAuthenticationError(
                        "DTB token 失效，请在个人设置中更新", stage=stage
                    )
                if status != "200" or not isinstance(payload.get("data"), dict):
                    raise DtbError("DTB 响应失败", stage=stage, category="unknown")
                return payload["data"]
            except DtbAuthenticationError:
                raise
            except DtbError:
                if attempt == len(self.retry_delays):
                    raise
                await asyncio.sleep(self.retry_delays[attempt])
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                if attempt == len(self.retry_delays):
                    category = _exception_category(exc)
                    raise DtbError(
                        "DTB 请求失败", stage=stage, category=category
                    ) from None
                await asyncio.sleep(self.retry_delays[attempt])
        raise AssertionError("unreachable")

    async def submit(self, sql, limit):
        data = await self._request(
            "POST",
            "/api/data-query/async-execute",
            json={
                "env": 2,
                "dataSource": "doris_ai",
                "sql": sql,
                "defaultLimit": limit,
            },
        )
        key = data.get("queryKey") or data.get("queryId")
        if not isinstance(key, str) or not key:
            raise DtbError("DTB 未返回查询标识", stage="submit", category="result")
        return key

    async def poll(self, key):
        data = await self._request(
            "GET", "/api/data-query/result/" + quote(key, safe="")
        )
        if data.get("errorMessage"):
            raise DtbError(
                "DTB 查询执行失败",
                stage="poll",
                category=_poll_error_category(data.get("errorMessage")),
            )
        return data

    async def wait(self, key):
        while True:
            data = await self.poll(key)
            if data.get("progress") == 1.0:
                return data
            await asyncio.sleep(self.poll_interval)

    async def download(self, key, format):
        return await self._request(
            "GET",
            "/api/data-query/download/" + quote(key, safe=""),
            params={"format": format},
            raw=True,
        )

    async def estimate(self, sql):
        return {"partitions": None, "rows": None}


class _borrow:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *args):
        pass
