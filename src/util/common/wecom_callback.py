"""
企业微信回调 —— URL 验证与消息接收，纯标准库实现

企微「接收消息服务器配置」里的 URL 必须能被企微服务器直接访问，所以这个端点
**不能要求登录**（企微不会带我们的面板令牌）。匿名不等于不设防：安全性由企微
签名保证 —— 只有同时掌握 Token 与 EncodingAESKey 的一方才能算出合法的
msg_signature、才能解出可读的明文。

排查线上用了很久的那套加解密，这里只做三件事，不做任何业务：

  1. 校验签名      sha1(sort(token, timestamp, nonce, encrypt))
  2. 解密消息体    AES-256-CBC，明文 = random(16B) + msg_len(4B) + msg + receiveid
  3. 加密被动回复  把上面两步反过来再来一遍

为什么要自己写 AES：容器 requirements 里没有 cryptography / pycryptodome，
而一次回调只解几十字节，纯 Python 就是微秒级的事。为这一处多引一个包，等于
在构建期多一个失败点 —— 这与 notify.py 只依赖 urllib 是同一条理由。

本模块不碰 Qt、不碰 HTTP、也不读配置：所有入参由调用方传入，方便单测直接
喂官方文档给出的那组样例向量（见 test/test_wecom_callback.py）。
"""

import base64
import binascii
import hashlib
import hmac
import logging
import os
import struct
import time
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)


class CallbackError(ValueError):
    """回调数据不合法（签名不符 / 解不开 / 结构不对）。文案面向日志，不面向用户"""


# 企微一条消息也就几 KB；给个上限，免得匿名端点被塞大包
MAX_MESSAGE_BYTES = 64 * 1024

# AES 分组与企微用的 padding 块大小（官方：PKCS#7 填充至 32 字节的倍数）
BLOCK_SIZE = 16
PAD_SIZE = 32


# ---------------------------------------------------------------------------
# AES-256 本体
#
# 只用到解密与加密各一块的功能，但轮函数要写全 —— 半套 AES 没法验证。
# 表在导入时算一次（S 盒走 GF(2^8) 求逆 + 仿射变换），不硬编码 256 个字节，
# 少一个抄错的入口。

def _rotl8(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (8 - shift))) & 0xFF


def _build_sbox() -> tuple:
    sbox = bytearray(256)
    p = q = 1

    while True:
        # p 按 ×3 递推，正好走遍 GF(2^8) 的全部非零元
        p = (p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)) & 0xFF

        # q 是 p 在 GF(2^8) 里的乘法逆（同样的递推，走的是逆运算）
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF

        if q & 0x80:
            q ^= 0x09

        # 仿射变换
        sbox[p] = (q ^ _rotl8(q, 1) ^ _rotl8(q, 2) ^ _rotl8(q, 3) ^ _rotl8(q, 4) ^ 0x63) & 0xFF

        if p == 1:
            break

    sbox[0] = 0x63

    inverse = bytearray(256)

    for index, value in enumerate(sbox):
        inverse[value] = index

    return bytes(sbox), bytes(inverse)


_SBOX, _INV_SBOX = _build_sbox()


def _xtime(value: int) -> int:
    return ((value << 1) ^ 0x1B) & 0xFF if value & 0x80 else (value << 1) & 0xFF


def _mul(a: int, b: int) -> int:
    """GF(2^8) 乘法。固定用 2 与 3，写成通用循环是为了可读"""
    result = 0

    while b:
        if b & 1:
            result ^= a

        a = _xtime(a)
        b >>= 1

    return result


def _expand_key(key: bytes) -> tuple:
    """AES-256 密钥扩展，返回 (轮密钥字列表, 轮数)"""
    words = len(key) // 4
    rounds = words + 6
    expanded = [list(key[4 * i:4 * i + 4]) for i in range(words)]
    rcon = 1

    for i in range(words, 4 * (rounds + 1)):
        temp = list(expanded[i - 1])

        if i % words == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[byte] for byte in temp]
            temp[0] ^= rcon
            rcon = _xtime(rcon)

        elif words > 6 and i % words == 4:
            temp = [_SBOX[byte] for byte in temp]

        expanded.append([expanded[i - words][j] ^ temp[j] for j in range(4)])

    return expanded, rounds


# 状态按列主序平铺：扁平下标 i 对应 state[i % 4][i // 4]，即与输入字节顺序一致
def _add_round_key(state: list, words: list, round_index: int):
    for i in range(16):
        state[i] ^= words[4 * round_index + i // 4][i % 4]


def _sub_bytes(state: list, table: bytes):
    for i in range(16):
        state[i] = table[state[i]]


def _shift_rows(state: list):
    """第 r 行左移 r 字节"""
    for row in range(1, 4):
        shifted = [state[row + 4 * col] for col in range(4)]
        shifted = shifted[row:] + shifted[:row]

        for col in range(4):
            state[row + 4 * col] = shifted[col]


def _inv_shift_rows(state: list):
    """第 r 行右移 r 字节"""
    for row in range(1, 4):
        shifted = [state[row + 4 * col] for col in range(4)]
        shifted = shifted[-row:] + shifted[:-row]

        for col in range(4):
            state[row + 4 * col] = shifted[col]


def _mix_columns(state: list):
    for col in range(4):
        base = 4 * col
        a = [state[base], state[base + 1], state[base + 2], state[base + 3]]

        state[base] = _mul(a[0], 2) ^ _mul(a[1], 3) ^ a[2] ^ a[3]
        state[base + 1] = a[0] ^ _mul(a[1], 2) ^ _mul(a[2], 3) ^ a[3]
        state[base + 2] = a[0] ^ a[1] ^ _mul(a[2], 2) ^ _mul(a[3], 3)
        state[base + 3] = _mul(a[0], 3) ^ a[1] ^ a[2] ^ _mul(a[3], 2)


def _inv_mix_columns(state: list):
    for col in range(4):
        base = 4 * col
        a = [state[base], state[base + 1], state[base + 2], state[base + 3]]

        state[base] = _mul(a[0], 14) ^ _mul(a[1], 11) ^ _mul(a[2], 13) ^ _mul(a[3], 9)
        state[base + 1] = _mul(a[0], 9) ^ _mul(a[1], 14) ^ _mul(a[2], 11) ^ _mul(a[3], 13)
        state[base + 2] = _mul(a[0], 13) ^ _mul(a[1], 9) ^ _mul(a[2], 14) ^ _mul(a[3], 11)
        state[base + 3] = _mul(a[0], 11) ^ _mul(a[1], 13) ^ _mul(a[2], 9) ^ _mul(a[3], 14)


def _encrypt_block(block: bytes, words: list, rounds: int) -> bytes:
    state = list(block)
    _add_round_key(state, words, 0)

    for round_index in range(1, rounds):
        _sub_bytes(state, _SBOX)
        _shift_rows(state)
        _mix_columns(state)
        _add_round_key(state, words, round_index)

    _sub_bytes(state, _SBOX)
    _shift_rows(state)
    _add_round_key(state, words, rounds)

    return bytes(state)


def _decrypt_block(block: bytes, words: list, rounds: int) -> bytes:
    state = list(block)
    _add_round_key(state, words, rounds)

    for round_index in range(rounds - 1, 0, -1):
        _inv_shift_rows(state)
        _sub_bytes(state, _INV_SBOX)
        _add_round_key(state, words, round_index)
        _inv_mix_columns(state)

    _inv_shift_rows(state)
    _sub_bytes(state, _INV_SBOX)
    _add_round_key(state, words, 0)

    return bytes(state)


def _cbc_decrypt(key: bytes, data: bytes) -> bytes:
    """AES-256-CBC 解密，IV 取密钥前 16 字节（企微的规定）"""
    words, rounds = _expand_key(key)
    previous = key[:BLOCK_SIZE]
    plain = bytearray()

    for offset in range(0, len(data), BLOCK_SIZE):
        block = data[offset:offset + BLOCK_SIZE]

        if len(block) != BLOCK_SIZE:
            raise CallbackError("密文长度不是 16 的整数倍")

        decrypted = _decrypt_block(block, words, rounds)
        plain.extend(a ^ b for a, b in zip(decrypted, previous))
        previous = block

    return bytes(plain)


def _cbc_encrypt(key: bytes, data: bytes) -> bytes:
    words, rounds = _expand_key(key)
    previous = key[:BLOCK_SIZE]
    cipher = bytearray()

    for offset in range(0, len(data), BLOCK_SIZE):
        block = bytes(a ^ b for a, b in zip(data[offset:offset + BLOCK_SIZE], previous))
        previous = _encrypt_block(block, words, rounds)
        cipher.extend(previous)

    return bytes(cipher)


def _pad(data: bytes) -> bytes:
    padding = PAD_SIZE - (len(data) % PAD_SIZE)

    return data + bytes([padding]) * padding


def _unpad(data: bytes) -> bytes:
    if not data:
        raise CallbackError("解密结果为空")

    padding = data[-1]

    if padding < 1 or padding > PAD_SIZE or padding > len(data):
        raise CallbackError("填充字节不合法（密钥多半不对）")

    if data[-padding:] != bytes([padding]) * padding:
        raise CallbackError("填充内容不合法（密钥多半不对）")

    return data[:-padding]


# ---------------------------------------------------------------------------
# 企微协议层

def decode_aes_key(encoding_aes_key: str) -> bytes:
    """EncodingAESKey（43 字符）→ 32 字节 AESKey"""
    text = str(encoding_aes_key or "").strip()

    if len(text) != 43:
        raise CallbackError("EncodingAESKey 必须是 43 位字符")

    try:
        raw = base64.b64decode(text + "=", validate = True)

    except (binascii.Error, ValueError):
        raise CallbackError("EncodingAESKey 不是合法的 Base64")

    if len(raw) != 32:
        raise CallbackError("EncodingAESKey 解码后不是 32 字节")

    return raw


def compute_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """签名 = sha1(四个参数值按字典序拼起来)。排序的是**参数值**，不是参数名"""
    parts = sorted([str(token or ""), str(timestamp or ""), str(nonce or ""), str(encrypt or "")])

    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()


def check_signature(token: str, msg_signature: str, timestamp: str, nonce: str, encrypt: str):
    expected = compute_signature(token, timestamp, nonce, encrypt)

    # 定时安全比较：签名本身是公开值，但没必要给出逐字节试探的余地
    if not hmac.compare_digest(expected, str(msg_signature or "")):
        raise CallbackError("签名校验不通过（Token 与企微后台不一致？）")


def _decrypt(token: str, aes_key: bytes, msg_signature: str, timestamp: str, nonce: str,
             encrypt: str, receive_id_expect: str = "") -> str:
    check_signature(token, msg_signature, timestamp, nonce, encrypt)

    try:
        payload = base64.b64decode(encrypt, validate = True)

    except (binascii.Error, ValueError):
        raise CallbackError("消息体不是合法的 Base64")

    if not payload or len(payload) % BLOCK_SIZE:
        raise CallbackError("密文长度不合法")

    plain = _unpad(_cbc_decrypt(aes_key, payload))

    if len(plain) < 20:
        raise CallbackError("解密结果太短，不像是企微的消息结构")

    msg_len = struct.unpack(">I", plain[16:20])[0]

    if msg_len > len(plain) - 20:
        raise CallbackError("消息长度字段越界")

    message = plain[20:20 + msg_len]
    receive_id = plain[20 + msg_len:].decode("utf-8", "replace")

    # 企业应用回调的 receiveid 就是 corpid。只在双方都非空时比对 ——
    # 用户没填企业 ID 时不该把回调整个挡掉，签名本身已经把伪造者挡住了
    if receive_id_expect and receive_id and receive_id != receive_id_expect:
        raise CallbackError("receiveid 与企业 ID 不一致")

    return message.decode("utf-8", "replace")


def verify_url(token: str, aes_key: bytes, timestamp: str, nonce: str,
               msg_signature: str, echostr: str, receive_id: str = "") -> str:
    """
    GET 验证：校验签名并解密 echostr，返回明文

    返回的内容必须**原样**回给企微：不加引号、不带 BOM、不带换行。
    """
    return _decrypt(token, aes_key, msg_signature, timestamp, nonce, echostr, receive_id)


def decrypt_message(token: str, aes_key: bytes, timestamp: str, nonce: str,
                    msg_signature: str, body: bytes, receive_id: str = "") -> str:
    """POST 收消息：从 XML 里取出 Encrypt 再解密，返回明文 XML"""
    if len(body) > MAX_MESSAGE_BYTES:
        raise CallbackError("请求体过大")

    try:
        root = ET.fromstring(body)

    except ET.ParseError:
        raise CallbackError("请求体不是合法的 XML")

    node = root.find("Encrypt")
    encrypt = (node.text or "").strip() if node is not None else ""

    if not encrypt:
        raise CallbackError("请求体里没有 Encrypt 字段")

    return _decrypt(token, aes_key, msg_signature, timestamp, nonce, encrypt, receive_id)


def encrypt_reply(token: str, aes_key: bytes, reply_xml: str, receive_id: str,
                  timestamp: str = None, nonce: str = None) -> str:
    """被动回复：加密消息体并组装响应 XML"""
    timestamp = str(timestamp or int(time.time()))
    nonce = str(nonce or os.urandom(8).hex())

    raw = str(reply_xml or "").encode("utf-8")
    payload = _pad(os.urandom(16) + struct.pack(">I", len(raw)) + raw + str(receive_id or "").encode("utf-8"))
    encrypt = base64.b64encode(_cbc_encrypt(aes_key, payload)).decode("ascii")
    signature = compute_signature(token, timestamp, nonce, encrypt)

    return (
        "<xml>"
        f"<Encrypt><![CDATA[{encrypt}]]></Encrypt>"
        f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
        f"<TimeStamp>{timestamp}</TimeStamp>"
        f"<Nonce><![CDATA[{nonce}]]></Nonce>"
        "</xml>"
    )


def parse_message(xml_text: str) -> dict:
    """明文 XML → 扁平字典。取不到的键不出现，调用方用 get 兜"""
    try:
        root = ET.fromstring(xml_text)

    except ET.ParseError:
        return {}

    message = {}

    for node in root:
        message[node.tag] = (node.text or "").strip()

    return message


def summarize(message: dict) -> str:
    """一行人类可读摘要，用于日志与面板的「最近收到」"""
    if not message:
        return "无法解析的消息"

    kind = message.get("MsgType", "?")

    if kind == "event":
        return "事件 %s%s" % (message.get("Event", "?"), (
            ":" + message["EventKey"] if message.get("EventKey") else ""
        ))

    if kind == "text":
        content = message.get("Content", "")
        return "文本消息：%s" % (content[:60] + ("…" if len(content) > 60 else ""))

    return "消息类型 %s" % kind
