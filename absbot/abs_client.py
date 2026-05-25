from __future__ import annotations

import logging
from typing import Any

import httpx


logger = logging.getLogger(__name__)


class AudiobookshelfError(RuntimeError):
    pass


class AudiobookshelfAuthError(AudiobookshelfError):
    pass


class AudiobookshelfNotFoundError(AudiobookshelfError):
    """HTTP 404 — 目标资源不存在。"""
    pass


def _request_error_message(exc: httpx.RequestError) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "连接服务器超时"
    return "连接服务器失败"


def _response_error_message(method: str, path: str, response: httpx.Response) -> str:
    text = response.text.lower()
    if response.status_code == 404:
        return "用户不存在"
    if method == "POST" and path == "/api/users" and "username" in text:
        if "taken" in text or "already" in text or "exists" in text:
            return "用户名已存在"
    return "服务器请求失败，请稍后重试"


class AudiobookshelfClient:
    def __init__(self, base_url: str, api_token: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "AudiobookshelfClient":
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={"Authorization": f"Bearer {self.api_token}"},
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.api_token}"},
            )
        return self._client

    async def create_user(self, username: str, password: str) -> dict[str, Any]:
        payload = {
            "username": username,
            "password": password,
            "type": "user",
            "isActive": True,
            "isLocked": False,
            "permissions": {
                "download": False,
                "update": False,
                "delete": False,
                "upload": False,
                "accessAllLibraries": True,
                "accessAllTags": True,
                "accessExplicitContent": True,
            },
        }
        data = await self._request("POST", "/api/users", json=payload)
        user = data.get("user") or data
        if not user.get("id"):
            raise AudiobookshelfError("Audiobookshelf 创建用户响应缺少用户 ID")
        return user

    async def list_users(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/users")
        return data.get("users", [])

    async def authenticate_user(self, username: str, password: str) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            try:
                response = await client.post(
                    "/login",
                    json={"username": username, "password": password},
                )
            except httpx.RequestError as exc:
                logger.error("Audiobookshelf API POST /login 请求失败：%s", exc)
                raise AudiobookshelfError(_request_error_message(exc)) from exc
        if response.status_code == 401:
            raise AudiobookshelfAuthError("账号或密码错误")
        if response.status_code >= 400:
            logger.warning(
                "Audiobookshelf API POST /login 请求异常：%s %s",
                response.status_code,
                response.text,
            )
            raise AudiobookshelfError(_response_error_message("POST", "/login", response))
        data = response.json()
        user = data.get("user") or {}
        if not user.get("id"):
            raise AudiobookshelfError("Audiobookshelf 登录响应缺少用户 ID")
        return user

    async def get_user(self, abs_user_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/users/{abs_user_id}")

    async def reset_password(self, abs_user_id: str, password: str) -> dict[str, Any]:
        return await self._request("PATCH", f"/api/users/{abs_user_id}", json={"password": password})

    async def disable_user(self, abs_user_id: str) -> dict[str, Any]:
        return await self._request("PATCH", f"/api/users/{abs_user_id}", json={"isActive": False})

    async def restore_user(self, abs_user_id: str) -> dict[str, Any]:
        return await self._request("PATCH", f"/api/users/{abs_user_id}", json={"isActive": True})

    async def delete_user(self, abs_user_id: str) -> None:
        await self._request("DELETE", f"/api/users/{abs_user_id}")

    async def ping(self) -> bool:
        """轻量探活：可达返回 True，不可达返回 False。"""
        try:
            response = await self.client.get("/ping", timeout=5.0)
            return response.status_code < 400
        except httpx.RequestError:
            return False

    async def get_libraries(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/libraries")
        return data.get("libraries", [])

    async def get_library_stats(self, library_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/libraries/{library_id}/stats")

    async def get_latest_listening_session(self, abs_user_id: str) -> dict[str, Any] | None:
        data = await self._request(
            "GET",
            f"/api/users/{abs_user_id}/listening-sessions",
            params={"itemsPerPage": 1, "page": 0},
        )
        sessions = data.get("sessions") or []
        return sessions[0] if sessions else None

    async def get_listening_sessions_page(
        self,
        abs_user_id: str,
        *,
        page: int = 0,
        items_per_page: int = 50,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/users/{abs_user_id}/listening-sessions",
            params={"itemsPerPage": items_per_page, "page": page},
        )

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            logger.error("Audiobookshelf API %s %s 请求失败：%s", method, path, exc)
            raise AudiobookshelfError(_request_error_message(exc)) from exc
        if response.status_code >= 400:
            logger.warning(
                "Audiobookshelf API %s %s 请求异常：%s %s",
                method,
                path,
                response.status_code,
                response.text,
            )
            if response.status_code == 404:
                raise AudiobookshelfNotFoundError(_response_error_message(method, path, response))
            raise AudiobookshelfError(_response_error_message(method, path, response))
        if not response.content:
            return {}
        return response.json()
