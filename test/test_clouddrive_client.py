"""
util/clouddrive —— CloudDrive2 的 gRPC-Web 客户端。

传输层是自己实现的（5 字节帧 + HTTP/1.1），所以这里把「发出去什么、收回来怎么解」
逐条钉住：CD2 那边改一步不会在这里静默变成另一个行为。

线上实测过的三件事都留了对应的用例：
    · 正常响应 = 数据帧 + 0x80 的 trailer 帧
    · 返回 Empty 的方法只有 trailer 帧，没有数据帧
    · 凭据被拒时 CD2 回一个空 body，连 trailer 都没有（错误密码调 GetToken 见到）
"""

import httpx
import pytest

from util.clouddrive import (
    CloudDriveAuthError, CloudDriveClient, CloudDriveError, status_label
)
from util.clouddrive import client as cd2
from util.clouddrive import clouddrive_pb2 as pb


def grpc_web_body(payload: bytes = b"", status: str = "0", message: str = "") -> bytes:
    """按 gRPC-Web 的线上格式拼响应体：数据帧 + 0x80 的 trailer 帧"""
    body = b""

    if payload:
        body += bytes([cd2.FRAME_MESSAGE]) + len(payload).to_bytes(4, "big") + payload

    trailer = "grpc-status:%s\r\n" % status

    if message:
        trailer += "grpc-message:%s\r\n" % message

    encoded = trailer.encode("utf-8")

    return body + bytes([cd2.FRAME_TRAILER]) + len(encoded).to_bytes(4, "big") + encoded


class Cd2Stub:
    """假的 CD2 服务端：按方法名回一个预先摆好的响应，并记下收到的请求"""

    def __init__(self):
        self.calls = []
        self.replies = {}

    def reply(self, method, payload: bytes = b"", status: str = "0", message: str = ""):
        self.replies[method] = (payload, status, message)

    def reply_with_empty_body(self, method):
        """凭据不被接受时 CD2 就是这么回的 —— 特意留一条能复现它的路"""
        self.replies[method] = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        payload, _ = cd2._unframe(request.content)

        self.calls.append({
            "method": method,
            "payload": payload,
            "auth": request.headers.get("authorization"),
            "content_type": request.headers.get("content-type"),
            "path": request.url.path,
        })

        reply = self.replies.get(method)

        if reply is None:
            return httpx.Response(
                200, content = b"", headers = {"content-type": "application/grpc-web+proto"},
            )

        data, status, message = reply

        return httpx.Response(
            200, content = grpc_web_body(data, status, message),
            headers = {"content-type": "application/grpc-web+proto"},
        )

    def client(self, host: str = "cd2.test") -> CloudDriveClient:
        instance = CloudDriveClient(host)
        instance._client = httpx.Client(transport = httpx.MockTransport(self.handle))

        return instance


@pytest.fixture
def stub():
    return Cd2Stub()


@pytest.fixture
def client(stub):
    instance = stub.client()
    instance.set_device_token("device-token")

    return instance


# -- 帧编解码 ---------------------------------------------------------------

def test_frames_round_trip():
    payload = pb.StringValue(value = "hello").SerializeToString()
    data, trailers = cd2._unframe(grpc_web_body(payload))

    assert data == payload
    assert trailers["grpc-status"] == "0"


def test_a_trailer_only_body_has_no_payload():
    """返回 Empty 的方法就是这样：只有 trailer，没有数据帧"""
    data, trailers = cd2._unframe(grpc_web_body())

    assert data is None
    assert trailers["grpc-status"] == "0"


def test_an_empty_body_yields_nothing_at_all():
    data, trailers = cd2._unframe(b"")

    assert data is None
    assert trailers == {}


# -- 传输与鉴权 -------------------------------------------------------------

def test_calls_go_out_as_grpc_web_with_a_bearer(stub, client):
    stub.reply("GetSystemInfo", pb.CloudDriveSystemInfo().SerializeToString())
    client.system_info()

    call = stub.calls[-1]

    assert call["content_type"] == "application/grpc-web+proto"
    assert call["auth"] == "Bearer device-token"
    assert call["path"] == "/clouddrive.CloudDriveFileSrv/GetSystemInfo"


def test_an_empty_body_is_surfaced_as_an_auth_problem(stub, client):
    """裸空 body 说明这次调用根本没成立，不能说成"成功但没内容" """
    stub.reply_with_empty_body("BackupGetAll")

    with pytest.raises(CloudDriveAuthError):
        client.list_backups()


def test_permission_denied_maps_to_an_auth_error(stub, client):
    stub.reply("BackupGetAll", status = "7", message = "no%20access")

    with pytest.raises(CloudDriveAuthError, match = "no access"):
        client.list_backups()


def test_other_grpc_failures_stay_plain_errors(stub, client):
    stub.reply("BackupGetAll", status = "13", message = "boom")

    with pytest.raises(CloudDriveError) as info:
        client.list_backups()

    assert not isinstance(info.value, CloudDriveAuthError)


def test_no_credentials_is_refused_before_any_request(stub):
    """凭据都没有就别发请求了 —— 否则拿到的是 CD2 的空 body，错误更难读"""
    with pytest.raises(CloudDriveAuthError, match = "凭据"):
        stub.client().list_backups()

    assert stub.calls == []


def test_a_transport_failure_is_wrapped(stub):
    instance = stub.client()
    instance.set_device_token("device-token")
    instance._client = httpx.Client(
        transport = httpx.MockTransport(lambda request: (_ for _ in ()).throw(
            httpx.ConnectError("connection refused")))
    )

    with pytest.raises(CloudDriveError, match = "连不上"):
        instance.list_backups()


# -- 凭据 -------------------------------------------------------------------

def test_login_swaps_the_account_password_for_a_jwt(stub):
    stub.reply("GetToken", pb.JWTToken(success = True, token = "jwt-1").SerializeToString())
    stub.reply("BackupGetAll", pb.BackupList().SerializeToString())

    instance = stub.client()
    instance.set_credentials("me@example.com", "pw")
    instance.list_backups()

    sent = pb.GetTokenRequest()
    sent.ParseFromString(stub.calls[0]["payload"])

    assert stub.calls[0]["method"] == "GetToken"
    assert stub.calls[0]["auth"] is None            # 登录本身不带令牌
    assert sent.userName == "me@example.com"
    assert sent.password == "pw"

    assert stub.calls[-1]["auth"] == "Bearer jwt-1"


def test_the_jwt_is_cached_across_calls(stub):
    stub.reply("GetToken", pb.JWTToken(success = True, token = "jwt-1").SerializeToString())
    stub.reply("BackupGetAll", pb.BackupList().SerializeToString())

    instance = stub.client()
    instance.set_credentials("me@example.com", "pw")
    instance.list_backups()
    instance.list_backups()

    logins = [c for c in stub.calls if c["method"] == "GetToken"]

    assert len(logins) == 1


def test_login_failure_is_an_auth_error(stub):
    stub.reply("GetToken", pb.JWTToken(
        success = False, errorMessage = "用户名或密码错误").SerializeToString())

    instance = stub.client()
    instance.set_credentials("me@example.com", "bad")

    with pytest.raises(CloudDriveAuthError, match = "用户名或密码错误"):
        instance.list_backups()


def test_an_expired_jwt_is_renewed():
    """过期时间落在续签余量里就该重登，而不是把过期令牌发出去"""
    import time

    instance = CloudDriveClient("cd2.test")

    with instance._lock:
        instance._token = "stale"
        instance._token_expiry = time.time() + 5      # 少于 TOKEN_RENEW_MARGIN
        instance._credentials = ("u", "p")

    calls = []

    def fake_login(username, password, totp = ""):
        calls.append(username)

        return "fresh"

    instance.login = fake_login

    assert instance._resolve_token() == "fresh"
    assert calls == ["u"]


# -- 备份 -------------------------------------------------------------------

def _backup_list(*sources):
    listing = pb.BackupList()

    for source in sources:
        entry = listing.backups.add()
        entry.backup.sourcePath = source
        entry.status = pb.BackupStatus.Scanned

    return listing.SerializeToString()


def test_find_backup_matches_the_source_path_exactly(stub, client):
    stub.reply("BackupGetAll", _backup_list("/Storage/ISO", "/Storage/哔哩哔哩"))

    found = client.find_backup("/Storage/哔哩哔哩")

    assert found is not None
    assert found.backup.sourcePath == "/Storage/哔哩哔哩"
    assert client.find_backup("/Storage/没有这条") is None


def test_backup_status_sends_the_source_path(stub, client):
    status = pb.BackupStatus()
    status.status = pb.BackupStatus.WalkingThrough
    stub.reply("BackupGetStatus", status.SerializeToString())

    reply = client.backup_status("/Storage/哔哩哔哩")

    sent = pb.StringValue()
    sent.ParseFromString(stub.calls[-1]["payload"])

    assert sent.value == "/Storage/哔哩哔哩"
    assert reply.status == pb.BackupStatus.WalkingThrough


def test_restart_sends_the_source_path(stub, client):
    stub.reply("BackupRestartWalkingThrough")

    client.restart_walking_through("/Storage/哔哩哔哩")

    sent = pb.StringValue()
    sent.ParseFromString(stub.calls[-1]["payload"])

    assert stub.calls[-1]["method"] == "BackupRestartWalkingThrough"
    assert sent.value == "/Storage/哔哩哔哩"


def test_backup_destinations_are_readable(stub, client):
    status = pb.BackupStatus()
    status.backup.sourcePath = "/Storage/哔哩哔哩"
    status.status = pb.BackupStatus.Scanned
    status.statusMessage = "Backup scanned"

    slot = status.backup.destinations.add()
    slot.destinationPath = "/115open/云下载/哔哩哔哩"
    slot.isEnabled = True
    slot.lastFinishTime.seconds = 1790945459

    stub.reply("BackupGetStatus", status.SerializeToString())

    reply = client.backup_status("/Storage/哔哩哔哩")
    destination = reply.backup.destinations[0]

    assert destination.destinationPath == "/115open/云下载/哔哩哔哩"
    assert destination.isEnabled is True
    assert destination.lastFinishTime.seconds == 1790945459


# -- 状态标签 ---------------------------------------------------------------

@pytest.mark.parametrize("code, word", [
    (pb.BackupStatus.Idle, "空闲"),
    (pb.BackupStatus.WalkingThrough, "扫描中"),
    (pb.BackupStatus.Scanned, "已扫描"),
    (pb.BackupStatus.Finished, "已完成"),
    (pb.BackupStatus.Waiting, "排队中"),
])
def test_status_labels_are_translated(code, word):
    assert status_label(code) == word


def test_an_unknown_status_still_renders_something():
    assert "99" in status_label(99)
