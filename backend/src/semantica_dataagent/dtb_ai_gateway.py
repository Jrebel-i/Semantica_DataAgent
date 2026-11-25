"""AI Gov gateway plugins for DTB query execution and metadata pulling."""

import asyncio
import json
import re
import time
from typing import Any, ClassVar
from urllib.parse import quote

import httpx
import sqlglot
from jsonschema import Draft202012Validator, ValidationError
from sqlglot import exp

from .dtb_executor import (
    DtbAuthenticationError,
    DtbError,
    _borrow,
    _exception_category,
    _poll_error_category,
)
from .dtb_ossie import DTB_PULLER_PARAMETERS_SCHEMA, DtbPluginPuller
from .dtb_puller import (
    MAX_ROW_LIMIT,
    DtbPuller,
    _downloaded_row_count,
    _result_count,
)
from .dtb_puller import DtbAuthenticationError as DtbPullAuthenticationError
from .plugins import PullArtifact, PullContext

DEFAULT_AI_GOV_DTB_URL = "https://ai-gov.pkfare.io/api/v1/gateway/call/ai-dtb"
AUTH_BUSINESS_STATUSES = frozenset({"401", "403", "503"})


def _request_stage(path: str) -> str:
    if path == "/async-execute":
        return "submit"
    if path.startswith("/result/"):
        return "poll"
    if path.startswith("/download/"):
        return "download"
    return "unknown"


def _check_gateway_path(method: str, path: str) -> None:
    allowed = (method == "POST" and path == "/async-execute") or (
        method == "GET"
        and re.fullmatch(r"/(?:result|download)/[A-Za-z0-9_-]+", path) is not None
    )
    if not allowed:
        raise ValueError("AI Gov DTB 请求不在允许的接口白名单中")


def _validate_agent_key(
    token: Any, *, stage: str = "unknown", for_puller: bool = False
) -> str:
    if (
        not isinstance(token, str)
        or not token.strip()
        or not token.isascii()
        or any(char.isspace() for char in token)
    ):
        if for_puller:
            raise DtbPullAuthenticationError("AI Gov Agent Key 格式无效")
        raise DtbAuthenticationError(
            "AI Gov Agent Key 未配置或格式无效，请在个人设置中更新",
            stage=stage,
        )
    return token


def _unwrap_outer_data(payload: Any, *, stage: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise DtbError("AI Gov DTB 响应格式错误", stage=stage, category="unknown")
    return payload["data"]


def _extract_submit_key(payload: Any) -> str:
    outer = _unwrap_outer_data(payload, stage="submit")
    if "status" in outer:
        status = str(outer.get("status", ""))
        if status in AUTH_BUSINESS_STATUSES:
            raise DtbAuthenticationError(
                "AI Gov 网关认证或托管 SK 授权失败，请检查个人 Dev Key 及托管 SK",
                stage="submit",
            )
        if status != "200":
            raise DtbError("AI Gov DTB 提交失败", stage="submit", category="unknown")
        data = outer.get("data") if isinstance(outer.get("data"), dict) else outer
    else:
        data = outer
    key = data.get("queryKey") or data.get("queryId")
    if not isinstance(key, str) or not key:
        raise DtbError("AI Gov DTB 未返回查询标识", stage="submit", category="result")
    return key


def _extract_poll_data(payload: Any) -> dict[str, Any]:
    outer = _unwrap_outer_data(payload, stage="poll")
    if "status" not in outer:
        raise DtbError("AI Gov DTB 响应格式错误", stage="poll", category="unknown")
    status = str(outer.get("status", ""))
    if status in AUTH_BUSINESS_STATUSES:
        raise DtbAuthenticationError(
            "AI Gov 网关认证或托管 SK 授权失败，请检查个人 Dev Key 及托管 SK",
            stage="poll",
        )
    if status != "200" or not isinstance(outer.get("data"), dict):
        raise DtbError("AI Gov DTB 响应失败", stage="poll", category="unknown")
    inner = outer["data"]
    if inner.get("errorMessage"):
        raise DtbError(
            "DTB 查询执行失败",
            stage="poll",
            category=_poll_error_category(inner.get("errorMessage")),
        )
    return inner


def _inspect_error_json(obj: Any, *, stage: str) -> None:
    if not isinstance(obj, dict):
        return
    candidate = None
    if "status" in obj:
        candidate = obj
    elif isinstance(obj.get("data"), dict) and "status" in obj["data"]:
        candidate = obj["data"]
    if candidate is not None:
        status = str(candidate.get("status", ""))
        if status in AUTH_BUSINESS_STATUSES:
            raise DtbAuthenticationError(
                "AI Gov 网关认证或托管 SK 授权失败，请检查个人 Dev Key 及托管 SK",
                stage=stage,
            )
        if status != "200":
            raise DtbError("AI Gov DTB 下载失败", stage=stage, category="result")


def _validate_download_response(response: httpx.Response) -> tuple[bytes, str | None]:
    if response.status_code in (401, 403, 503):
        raise DtbAuthenticationError(
            "AI Gov 网关认证或托管 SK 授权失败，请检查个人 Dev Key 及托管 SK",
            stage="download",
        )
    response.raise_for_status()
    content = response.content
    if not response.headers.get("content-disposition"):
        try:
            parsed = json.loads(content)
        except (ValueError, UnicodeError):
            parsed = None
        _inspect_error_json(parsed, stage="download")
        raise DtbError("AI Gov DTB 下载失败", stage="download", category="result")
    if not content:
        raise DtbError("DTB 下载结果为空", stage="download", category="result")
    if len(content) <= 8192:
        try:
            parsed = json.loads(content)
        except (ValueError, UnicodeError):
            parsed = None
        _inspect_error_json(parsed, stage="download")
    return content, response.headers.get("content-type")


class DtbAiGatewayExecutor:
    """Query executor talking to AI Gov's DTB gateway via per-user X-Agent-Key."""

    id = "dtb_ai_gateway"
    version = "1.0"
    display_name = "DTB (AI Gov 网关)"
    parameters_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "base_url": {
                "type": "string",
                "format": "uri",
                "default": DEFAULT_AI_GOV_DTB_URL,
                "title": "AI Gov DTB 网关地址",
            },
            "env": {
                "type": "integer",
                "minimum": 1,
                "default": 2,
                "title": "环境 ID",
            },
            "data_source": {
                "type": "string",
                "enum": ["doris_ai", "presto"],
                "default": "doris_ai",
                "title": "查询引擎",
            },
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
        base_url: str = DEFAULT_AI_GOV_DTB_URL,
        token: str = "",
        *,
        env: int = 2,
        data_source: str = "doris_ai",
        client=None,
        retry_delays=(5, 15, 45),
        poll_interval: float = 2,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.env = env
        self.data_source = data_source
        self.client = client
        self.retry_delays = retry_delays
        self.poll_interval = poll_interval

    def bind(self, parameters: dict, token: str):
        return DtbAiGatewayExecutor(
            parameters["base_url"],
            token,
            env=int(parameters.get("env", 2)),
            data_source=str(parameters.get("data_source", "doris_ai")),
        )

    async def _request(self, method: str, path: str, *, raw: bool = False, **kwargs):
        _check_gateway_path(method, path)
        stage = _request_stage(path)
        key = _validate_agent_key(self.token, stage=stage)
        headers = {"X-Agent-Key": key}
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
                if response.status_code in (401, 403, 503):
                    raise DtbAuthenticationError(
                        "AI Gov 网关认证或托管 SK 授权失败，请检查个人 Dev Key 及托管 SK",
                        stage=stage,
                    )
                if raw:
                    return _validate_download_response(response)
                response.raise_for_status()
                return response.json()
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
                        "AI Gov DTB 请求失败", stage=stage, category=category
                    ) from None
                await asyncio.sleep(self.retry_delays[attempt])
        raise AssertionError("unreachable")

    async def submit(self, sql: str, limit: int) -> str:
        payload = await self._request(
            "POST",
            "/async-execute",
            json={
                "env": self.env,
                "dataSource": self.data_source,
                "sql": sql,
                "defaultLimit": limit,
            },
        )
        return _extract_submit_key(payload)

    async def poll(self, key: str) -> dict:
        payload = await self._request("GET", "/result/" + quote(key, safe=""))
        return _extract_poll_data(payload)

    async def wait(self, key: str) -> dict:
        while True:
            data = await self.poll(key)
            if float(data.get("progress") or 0) >= 1.0:
                return data
            await asyncio.sleep(self.poll_interval)

    async def download(self, key: str, format: str) -> tuple[bytes, str | None]:
        return await self._request(
            "GET",
            "/download/" + quote(key, safe=""),
            params={"format": format},
            raw=True,
        )

    async def estimate(self, sql: str) -> dict:
        return {"partitions": None, "rows": None}


class DtbAiGatewayFetcher(DtbPuller):
    """DTB puller fetcher talking to AI Gov's DTB gateway endpoints."""

    def __init__(
        self,
        *,
        env: int = 2,
        data_source: str = "doris_ai",
        metadata_row_limit: int = 0,
    ) -> None:
        self.env = env
        self.data_source = data_source
        self.metadata_row_limit = metadata_row_limit

    @staticmethod
    def _validate_readonly_sql(sql: Any) -> None:
        try:
            statements = sqlglot.parse(sql, read="presto")
        except (sqlglot.errors.ParseError, TypeError) as exc:
            raise ValueError("DTB query is not permitted") from exc
        if (
            len(statements) != 1
            or not isinstance(statements[0], exp.Query)
            or any(
                isinstance(
                    node,
                    (
                        exp.Insert,
                        exp.Update,
                        exp.Delete,
                        exp.Command,
                        exp.Create,
                        exp.Drop,
                    ),
                )
                for node in statements[0].walk()
            )
        ):
            raise ValueError("DTB query is not permitted")

    def query_download(
        self,
        client,
        base_url,
        token,
        sql,
        format,
        poll_interval=2,
        max_polls=30,
        data_source="presto-emr",
        row_limit=0,
        allow_truncation=False,
    ):
        del (
            data_source
        )  # AI Gov gateway always uses the configured gateway data_source.
        if format not in {"json", "xlsx", "csv"}:
            raise ValueError("Invalid DTB query format")
        if (
            isinstance(row_limit, bool)
            or not isinstance(row_limit, int)
            or not 0 <= row_limit <= MAX_ROW_LIMIT
        ):
            raise ValueError("Invalid DTB query row limit")
        self._validate_readonly_sql(sql)
        key_header = _validate_agent_key(token, stage="submit", for_puller=True)
        headers = {"X-Agent-Key": key_header}
        effective_limit = self.metadata_row_limit if row_limit == 0 else row_limit
        root = base_url.rstrip("/")

        try:
            _check_gateway_path("POST", "/async-execute")
            response = client.request(
                "POST",
                root + "/async-execute",
                headers=headers,
                json={
                    "env": self.env,
                    "dataSource": self.data_source,
                    "sql": sql,
                    "defaultLimit": effective_limit,
                },
            )
            if response.status_code in (401, 403, 503):
                raise DtbPullAuthenticationError(
                    f"AI Gov DTB authentication failed ({response.status_code})"
                )
            response.raise_for_status()
            key = quote(_extract_submit_key(response.json()), safe="")

            total = None
            result_path = "/result/" + key
            _check_gateway_path("GET", result_path)
            for attempt in range(max_polls):
                poll_resp = client.request("GET", root + result_path, headers=headers)
                if poll_resp.status_code in (401, 403, 503):
                    raise DtbPullAuthenticationError(
                        f"AI Gov DTB authentication failed ({poll_resp.status_code})"
                    )
                poll_resp.raise_for_status()
                result = _extract_poll_data(poll_resp.json())
                if float(result.get("progress") or 0) >= 1.0:
                    total = _result_count(result, "totalCount")
                    break
                if attempt + 1 < max_polls:
                    time.sleep(poll_interval)
            else:
                raise ValueError("AI Gov DTB query timed out")

            if (
                row_limit
                and not allow_truncation
                and total is not None
                and total >= row_limit
            ):
                raise ValueError("DTB result truncated")

            download_path = "/download/" + key
            _check_gateway_path("GET", download_path)
            dl_resp = client.request(
                "GET",
                root + download_path,
                headers=headers,
                params={"format": format},
            )
            content, _ = _validate_download_response(dl_resp)
            if row_limit and not allow_truncation and total is None:
                total = _downloaded_row_count(content, format)
                if total >= row_limit:
                    raise ValueError("DTB result truncated")
            return content
        except DtbAuthenticationError as exc:
            raise DtbPullAuthenticationError(str(exc)) from exc
        except DtbError as exc:
            raise ValueError(str(exc)) from exc
        except DtbPullAuthenticationError:
            raise
        except httpx.TimeoutException as exc:
            raise ValueError("AI Gov DTB query timeout") from exc
        except (httpx.HTTPError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError("AI Gov DTB query request failed") from exc


_GATEWAY_PULL_ONLY_KEYS = ("env", "data_source", "metadata_row_limit")


class DtbAiGatewayPuller:
    """PullerPlugin using AI Gov's DTB gateway endpoints with a service Dev Key."""

    id = "dtb_ai_gateway"
    version = "1"
    display_name = "DTB (AI Gov 网关)"
    parameters_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            **DTB_PULLER_PARAMETERS_SCHEMA["properties"],
            "url": {
                "type": "string",
                "format": "uri",
                "default": DEFAULT_AI_GOV_DTB_URL,
                "title": "AI Gov DTB 网关地址",
            },
            "token": {
                "type": "string",
                "format": "password",
                "title": "AI Gov Agent Key (Dev Key)",
            },
            "env": {
                "type": "integer",
                "minimum": 1,
                "default": 2,
                "title": "环境 ID",
            },
            "data_source": {
                "type": "string",
                "enum": ["doris_ai", "presto"],
                "default": "doris_ai",
                "title": "查询引擎",
            },
            "metadata_row_limit": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_ROW_LIMIT,
                "default": 0,
                "title": "元数据查询行数上限（0 表示不限）",
            },
        },
        "additionalProperties": False,
    }

    def __init__(self, fetcher: Any | None = None) -> None:
        self._fetcher = fetcher

    def pull(self, context: PullContext) -> PullArtifact:
        params = dict(context.parameters or {})
        try:
            Draft202012Validator(self.parameters_schema).validate(params)
        except ValidationError as exc:
            raise ValueError(exc.message) from exc

        fetcher = self._fetcher or DtbAiGatewayFetcher(
            env=int(params.get("env", 2)),
            data_source=str(params.get("data_source", "doris_ai")),
            metadata_row_limit=int(params.get("metadata_row_limit", 0)),
        )
        base_params = {
            key: value
            for key, value in params.items()
            if key not in _GATEWAY_PULL_ONLY_KEYS
        }
        base_context = PullContext(
            project_id=context.project_id,
            instance_id=context.instance_id,
            parameters=base_params,
            secrets=dict(context.secrets),
            catalog_prefix=context.catalog_prefix,
            task_namespace=context.task_namespace,
        )
        return DtbPluginPuller(fetcher=fetcher).pull(base_context)
