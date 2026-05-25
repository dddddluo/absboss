import logging

import httpx
import respx
from httpx import Response

import pytest

from absbot.abs_client import AudiobookshelfAuthError, AudiobookshelfClient, AudiobookshelfError, AudiobookshelfNotFoundError


@respx.mock
async def test_abs_client_create_reset_disable_delete_requests():
    route_create = respx.post("https://abs.example.com/api/users").mock(
        return_value=Response(200, json={"id": "usr_1", "username": "alice"})
    )
    route_reset = respx.patch("https://abs.example.com/api/users/usr_1").mock(
        return_value=Response(200, json={"id": "usr_1", "username": "alice"})
    )
    route_disable = respx.patch("https://abs.example.com/api/users/usr_1").mock(
        return_value=Response(200, json={"id": "usr_1", "isActive": False})
    )
    route_delete = respx.delete("https://abs.example.com/api/users/usr_1").mock(
        return_value=Response(200, json={})
    )

    async with AudiobookshelfClient("https://abs.example.com/", "token") as client:
        await client.create_user("alice", "secret")
        await client.reset_password("usr_1", "new-secret")
        await client.disable_user("usr_1")
        await client.delete_user("usr_1")

    patch_payloads = [
        call.request.content
        for call in respx.calls
        if call.request.method == "PATCH"
    ]
    assert route_create.calls.last.request.headers["Authorization"] == "Bearer token"
    assert route_create.calls.last.request.content == (
        b'{"username":"alice","password":"secret","type":"user","isActive":true,"isLocked":false,'
        b'"permissions":{"download":false,"update":false,"delete":false,"upload":false,'
        b'"accessAllLibraries":true,"accessAllTags":true,"accessExplicitContent":true}}'
    )
    assert b'{"password":"new-secret"}' in patch_payloads
    assert b'{"isActive":false}' in patch_payloads
    assert route_reset.called
    assert route_disable.called
    assert route_delete.called


@respx.mock
async def test_abs_client_create_user_accepts_nested_user_response():
    respx.post("https://abs.example.com/api/users").mock(
        return_value=Response(200, json={"success": True, "user": {"id": "usr_1", "username": "alice"}})
    )

    async with AudiobookshelfClient("https://abs.example.com", "token") as client:
        user = await client.create_user("alice", "secret")

    assert user["id"] == "usr_1"
    assert user["username"] == "alice"


@respx.mock
async def test_abs_client_create_user_rejects_missing_user_id():
    respx.post("https://abs.example.com/api/users").mock(
        return_value=Response(200, json={"success": True})
    )

    async with AudiobookshelfClient("https://abs.example.com", "token") as client:
        with pytest.raises(AudiobookshelfError, match="创建用户响应缺少用户 ID"):
            await client.create_user("alice", "secret")


@respx.mock
async def test_abs_client_logs_api_error_response(caplog):
    caplog.set_level(logging.WARNING)
    respx.post("https://abs.example.com/api/users").mock(
        return_value=Response(400, text="username already exists")
    )

    async with AudiobookshelfClient("https://abs.example.com", "token") as client:
        with pytest.raises(AudiobookshelfError, match="用户名已存在"):
            await client.create_user("alice", "secret")

    assert "Audiobookshelf API POST /api/users 请求异常：" in caplog.text
    assert "username already exists" in caplog.text


@respx.mock
async def test_abs_client_maps_duplicate_username_to_user_message(caplog):
    caplog.set_level(logging.WARNING)
    respx.post("https://abs.example.com/api/users").mock(
        return_value=Response(400, text="Username already taken")
    )

    async with AudiobookshelfClient("https://abs.example.com", "token") as client:
        with pytest.raises(AudiobookshelfError, match="用户名已存在"):
            await client.create_user("alice", "secret")

    assert "Username already taken" in caplog.text


@respx.mock
async def test_abs_client_logs_connection_error(caplog):
    caplog.set_level(logging.ERROR)
    respx.post("https://abs.example.com/api/users").mock(
        side_effect=httpx.ConnectError("connect failed")
    )

    async with AudiobookshelfClient("https://abs.example.com", "token") as client:
        with pytest.raises(AudiobookshelfError, match="连接服务器失败"):
            await client.create_user("alice", "secret")

    assert "Audiobookshelf API POST /api/users 请求失败：" in caplog.text
    assert "connect failed" in caplog.text


@respx.mock
async def test_abs_client_maps_connection_error_to_user_message(caplog):
    caplog.set_level(logging.ERROR)
    respx.post("https://abs.example.com/api/users").mock(
        side_effect=httpx.ConnectError("connect failed")
    )

    async with AudiobookshelfClient("https://abs.example.com", "token") as client:
        with pytest.raises(AudiobookshelfError, match="连接服务器失败"):
            await client.create_user("alice", "secret")

    assert "connect failed" in caplog.text


@respx.mock
async def test_abs_client_maps_timeout_to_user_message(caplog):
    caplog.set_level(logging.ERROR)
    respx.post("https://abs.example.com/api/users").mock(
        side_effect=httpx.ReadTimeout("read timeout")
    )

    async with AudiobookshelfClient("https://abs.example.com", "token") as client:
        with pytest.raises(AudiobookshelfError, match="连接服务器超时"):
            await client.create_user("alice", "secret")

    assert "read timeout" in caplog.text


@respx.mock
async def test_abs_client_reads_latest_listening_session():
    respx.get("https://abs.example.com/api/users/usr_1/listening-sessions").mock(
        return_value=Response(
            200,
            json={
                "sessions": [
                    {"id": "play_1", "updatedAt": 1770000000000},
                ]
            },
        )
    )

    async with AudiobookshelfClient("https://abs.example.com", "token") as client:
        session = await client.get_latest_listening_session("usr_1")

    assert session["id"] == "play_1"


@respx.mock
async def test_abs_client_authenticates_existing_user_without_admin_token():
    route = respx.post("https://abs.example.com/login").mock(
        return_value=Response(200, json={"user": {"id": "usr_1", "username": "alice", "token": "u"}})
    )

    async with AudiobookshelfClient("https://abs.example.com", "admin-token") as client:
        user = await client.authenticate_user("alice", "secret")

    assert user["id"] == "usr_1"
    assert route.calls.last.request.content == b'{"username":"alice","password":"secret"}'
    assert "Authorization" not in route.calls.last.request.headers


@respx.mock
async def test_abs_client_authentication_rejects_bad_credentials():
    respx.post("https://abs.example.com/login").mock(return_value=Response(401, json={}))

    async with AudiobookshelfClient("https://abs.example.com", "token") as client:
        with pytest.raises(AudiobookshelfAuthError):
            await client.authenticate_user("alice", "bad")


@respx.mock
async def test_abs_client_authentication_raises_for_server_error():
    respx.post("https://abs.example.com/login").mock(return_value=Response(500, text="boom"))

    async with AudiobookshelfClient("https://abs.example.com", "token") as client:
        with pytest.raises(AudiobookshelfError, match="服务器请求失败"):
            await client.authenticate_user("alice", "secret")


@respx.mock
async def test_abs_client_get_user_raises_not_found_on_404():
    respx.get("https://abs.example.com/api/users/usr_999").mock(
        return_value=Response(404, json={"error": "User not found"})
    )

    async with AudiobookshelfClient("https://abs.example.com", "token") as client:
        with pytest.raises(AudiobookshelfNotFoundError):
            await client.get_user("usr_999")


@respx.mock
async def test_get_libraries_returns_list():
    respx.get("https://abs.example.com/api/libraries").mock(
        return_value=Response(
            200,
            json={
                "libraries": [
                    {"id": "lib_1", "name": "Audiobooks", "stats": {"totalItems": 150}},
                    {"id": "lib_2", "name": "Podcasts",   "stats": {"totalItems": 30}},
                ]
            },
        )
    )

    async with AudiobookshelfClient("https://abs.example.com", "token") as client:
        libs = await client.get_libraries()

    assert len(libs) == 2
    assert libs[0]["id"] == "lib_1"
    assert libs[1]["stats"]["totalItems"] == 30


@respx.mock
async def test_get_libraries_returns_empty_list_when_key_missing():
    respx.get("https://abs.example.com/api/libraries").mock(
        return_value=Response(200, json={})
    )

    async with AudiobookshelfClient("https://abs.example.com", "token") as client:
        libs = await client.get_libraries()

    assert libs == []


@respx.mock
async def test_get_libraries_raises_on_http_error():
    respx.get("https://abs.example.com/api/libraries").mock(
        return_value=Response(500, text="internal error")
    )

    async with AudiobookshelfClient("https://abs.example.com", "token") as client:
        with pytest.raises(AudiobookshelfError):
            await client.get_libraries()


@respx.mock
async def test_get_library_stats_returns_stats():
    respx.get("https://abs.example.com/api/libraries/lib_123/stats").mock(
        return_value=Response(
            200,
            json={
                "totalItems": 42,
                "totalAuthors": 2,
                "totalDuration": 1234.5,
            },
        )
    )

    async with AudiobookshelfClient("https://abs.example.com", "token") as client:
        stats = await client.get_library_stats("lib_123")

    assert stats["totalItems"] == 42
    assert stats["totalAuthors"] == 2
    assert stats["totalDuration"] == 1234.5


@respx.mock
async def test_get_listening_sessions_page_returns_sessions():
    respx.get("https://abs.example.com/api/users/u1/listening-sessions").mock(
        return_value=Response(200, json={"sessions": [{"id": "s1", "startedAt": 1000}]})
    )
    async with AudiobookshelfClient("https://abs.example.com", "token") as client:
        result = await client.get_listening_sessions_page("u1", page=0, items_per_page=1)
    assert result["sessions"][0]["id"] == "s1"


@respx.mock
async def test_get_listening_sessions_page_passes_params():
    route = respx.get("https://abs.example.com/api/users/u2/listening-sessions").mock(
        return_value=Response(200, json={"sessions": []})
    )
    async with AudiobookshelfClient("https://abs.example.com", "token") as client:
        await client.get_listening_sessions_page("u2", page=3, items_per_page=25)
    assert route.calls.last.request.url.params["page"] == "3"
    assert route.calls.last.request.url.params["itemsPerPage"] == "25"
