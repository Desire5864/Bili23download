"""
util/common/wecom_callback.py —— 企微回调的加解密。

这里的用例几乎全部来自**官方给的向量**，而不是自造数据自证：自造的
round-trip 只能证明"加密与解密互为逆运算"，证明不了实现符合企微协议。
真有人把 S 盒写错、把 msg_len 当成小端、把 receiveid 位置猜错，round-trip
一样能过，上线后却怎么都验证不过 —— 那是没有对照物才有的盲区。
"""

from util.common.wecom_callback import (
    CallbackError, compute_signature, decode_aes_key, decrypt_message, encrypt_reply,
    parse_message, summarize, verify_url, _cbc_encrypt, _decrypt_block, _encrypt_block,
    _expand_key, _INV_SBOX, _pad, _SBOX,
)

import base64
import pytest
import struct

# ---------------------------------------------------------------------------
# 官方文档「加解密方案说明」里的样例（token / EncodingAESKey / 密文 / 签名）
# ---------------------------------------------------------------------------

SAMPLE_TOKEN = "QDG6eK"
SAMPLE_AES_KEY = "jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C"
SAMPLE_SIGNATURE = "477715d11cdb4164915debcba66cb864d751f3e6"
SAMPLE_TIMESTAMP = "1409659813"
SAMPLE_NONCE = "1372623149"
SAMPLE_CORP_ID = "wx5823bf96d3bd56c7"
SAMPLE_ENCRYPT = (
    "RypEvHKD8QQKFhvQ6QleEB4J58tiPdvo+rtK1I9qca6aM/wvqnLSV5zEPeusUiX5L5X/0lWfrf0QADHHhGd3QczcdCUpj911L3vg3W/sYYvuJTs3TUUkSUX"
    "xaccAS0qhxchrRYt66wiSpGLYL42aM6A8dTT+6k4aSknmPj48kzJs8qLjvd4Xgpue06DOdnLxAUHzM6+kDZ+HMZfJYuR+LtwGc2hgf5gsijff0ekUNXZiq"
    "ATP7PF5mZxZ3Izoun1s4zG4LUMnvw2r+KqCKIw+3IQH03v+BCA9nMELNqbSf6tiWSrXJB3LAVGUcallcrw8V2t9EL4EhzJWrQUax5wLVMNS0+rUPA3k22Ncx"
    "4XXZS9o0MBH27Bo6BpNelZpS+/uh9KsNlY6bHCmJU9p8g7m3fVKn28H3KDYA5Pl/T8Z1ptDAVe0lXdQ2YoyyH2uyPIGHBZZIs2pDBS8R07+qN+E7Q=="
)

# 自造的凭据（格式与真实值同构，用来跑往返）
LOCAL_TOKEN = "VCGWG"
LOCAL_AES_KEY = "0XlyvMcFO3ZTQ3QIy48m14eC8qeYmCRN1uzVEZdQIVX"
LOCAL_CORP_ID = "ww182aa9502bd5aff3"


def build_encrypt(aes_key: bytes, message_xml: str, receive_id: str, random_seed: bytes = None) -> str:
    """按企微的格式把一段明文封装成密文，供端点测试造请求用"""
    raw = message_xml.encode("utf-8")
    payload = (random_seed or b"\x07" * 16) + struct.pack(">I", len(raw)) + raw + receive_id.encode("utf-8")

    return base64.b64encode(_cbc_encrypt(aes_key, _pad(payload))).decode("ascii")


class TestAesPrimitives:
    """AES 本体。先钉住表，再钉住块 —— 整体偏移与轮函数写错是两回事"""

    def test_sbox_known_values(self):
        # 来自 FIPS-197 的 S 盒前几项与几个经典取值。整表推导错位时，
        # 这些点会同时错，是最省的哨兵
        assert _SBOX[0x00] == 0x63
        assert _SBOX[0x01] == 0x7C
        assert _SBOX[0x02] == 0x77
        assert _SBOX[0x10] == 0xCA
        assert _SBOX[0x53] == 0xED
        assert _SBOX[0xFF] == 0x16

    def test_inverse_sbox_is_a_true_inverse(self):
        assert all(_INV_SBOX[_SBOX[i]] == i for i in range(256))

    def test_aes256_block_matches_fips_197(self):
        """FIPS-197 附录 C.3 的 AES-256 用例，key/明文/密文全部照抄标准原文"""
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        cipher = bytes.fromhex("8ea2b7ca516745bfeafc49904b496089")

        words, rounds = _expand_key(key)

        assert rounds == 14
        assert len(words) == 60
        assert _encrypt_block(plain, words, rounds) == cipher
        assert _decrypt_block(cipher, words, rounds) == plain


class TestOfficialSample:
    """官方样例：签名 → 解密 → 字段，全链路对一次"""

    def test_aes_key_decodes_to_32_bytes(self):
        assert len(decode_aes_key(SAMPLE_AES_KEY)) == 32

    def test_signature_matches_official(self):
        assert compute_signature(SAMPLE_TOKEN, SAMPLE_TIMESTAMP, SAMPLE_NONCE, SAMPLE_ENCRYPT) == SAMPLE_SIGNATURE

    def test_verify_url_returns_plain_text(self):
        plain = verify_url(
            SAMPLE_TOKEN, decode_aes_key(SAMPLE_AES_KEY), SAMPLE_TIMESTAMP, SAMPLE_NONCE,
            SAMPLE_SIGNATURE, SAMPLE_ENCRYPT, SAMPLE_CORP_ID,
        )

        message = parse_message(plain)

        assert message["Content"] == "hello"
        assert message["FromUserName"] == "mycreate"
        # 企业应用回调里 receiveid 就是 corpid，解出来应在 ToUserName 上
        assert message["ToUserName"] == SAMPLE_CORP_ID
        assert message["MsgType"] == "text"

    def test_plain_text_carries_no_wrapping(self):
        """回给企微的明文必须原样：多一个引号、换行或 BOM 都判失败"""
        plain = verify_url(
            SAMPLE_TOKEN, decode_aes_key(SAMPLE_AES_KEY), SAMPLE_TIMESTAMP, SAMPLE_NONCE,
            SAMPLE_SIGNATURE, SAMPLE_ENCRYPT, SAMPLE_CORP_ID,
        )

        assert plain.startswith("<xml>")
        assert plain.rstrip() == plain
        assert not plain.startswith("\ufeff")


class TestRejections:
    """拒收分支。匿名端点上，这些判断就是唯一的门禁"""

    def test_wrong_signature_rejected(self):
        with pytest.raises(CallbackError, match = "签名"):
            verify_url(SAMPLE_TOKEN, decode_aes_key(SAMPLE_AES_KEY), SAMPLE_TIMESTAMP,
                       SAMPLE_NONCE, "0" * 40, SAMPLE_ENCRYPT)

    def test_wrong_token_rejected(self):
        with pytest.raises(CallbackError, match = "签名"):
            verify_url("WrongToken", decode_aes_key(SAMPLE_AES_KEY), SAMPLE_TIMESTAMP,
                       SAMPLE_NONCE, SAMPLE_SIGNATURE, SAMPLE_ENCRYPT)

    def test_wrong_aes_key_cannot_be_unpadded(self):
        """签名对、密钥错：必须抛，而不是解出一段乱码当明文回过去"""
        signature = compute_signature(SAMPLE_TOKEN, SAMPLE_TIMESTAMP, SAMPLE_NONCE, SAMPLE_ENCRYPT)

        with pytest.raises(CallbackError):
            verify_url(SAMPLE_TOKEN, decode_aes_key("a" * 43), SAMPLE_TIMESTAMP,
                       SAMPLE_NONCE, signature, SAMPLE_ENCRYPT)

    def test_aes_key_length_and_charset(self):
        with pytest.raises(CallbackError, match = "43"):
            decode_aes_key("tooshort")

        with pytest.raises(CallbackError, match = "Base64"):
            decode_aes_key("!" * 43)

    def test_receive_id_mismatch_rejected(self):
        signature = compute_signature(SAMPLE_TOKEN, SAMPLE_TIMESTAMP, SAMPLE_NONCE, SAMPLE_ENCRYPT)

        with pytest.raises(CallbackError, match = "receiveid"):
            verify_url(SAMPLE_TOKEN, decode_aes_key(SAMPLE_AES_KEY), SAMPLE_TIMESTAMP,
                       SAMPLE_NONCE, signature, SAMPLE_ENCRYPT, "ww_someone_else")

    def test_receive_id_skipped_when_corp_id_not_configured(self):
        """没填企业 ID 时不该把回调整个挡掉：签名已经把伪造者挡住了"""
        signature = compute_signature(SAMPLE_TOKEN, SAMPLE_TIMESTAMP, SAMPLE_NONCE, SAMPLE_ENCRYPT)

        plain = verify_url(SAMPLE_TOKEN, decode_aes_key(SAMPLE_AES_KEY), SAMPLE_TIMESTAMP,
                           SAMPLE_NONCE, signature, SAMPLE_ENCRYPT, "")

        assert parse_message(plain)["Content"] == "hello"

    def test_message_body_must_be_xml_with_encrypt(self):
        key = decode_aes_key(LOCAL_AES_KEY)

        with pytest.raises(CallbackError, match = "XML"):
            decrypt_message(LOCAL_TOKEN, key, "1", "2", compute_signature(LOCAL_TOKEN, "1", "2", ""), b"not xml")

        with pytest.raises(CallbackError, match = "Encrypt"):
            decrypt_message(LOCAL_TOKEN, key, "1", "2", compute_signature(LOCAL_TOKEN, "1", "2", ""), b"<xml><ToUserName>x</ToUserName></xml>")

    def test_oversized_body_rejected(self):
        key = decode_aes_key(LOCAL_AES_KEY)
        huge = b"<xml>" + b"x" * (64 * 1024 + 1)

        with pytest.raises(CallbackError, match = "过大"):
            decrypt_message(LOCAL_TOKEN, key, "1", "2", "sig", huge)


class TestRoundTrip:
    """自造的往返。它证明不了协议正确（上面那组才管），只保证两个方向自洽"""

    def test_text_message_round_trip(self):
        key = decode_aes_key(LOCAL_AES_KEY)
        xml = (
            f"<xml><ToUserName><![CDATA[{LOCAL_CORP_ID}]]></ToUserName>"
            "<FromUserName><![CDATA[Desire]]></FromUserName><CreateTime>1409659813</CreateTime>"
            "<MsgType><![CDATA[text]]></MsgType><Content><![CDATA[下载器在线吗]]></Content>"
            "<MsgId>4561255354251345929</MsgId><AgentID>1000009</AgentID></xml>"
        )

        encrypt = build_encrypt(key, xml, LOCAL_CORP_ID)
        body = (
            f"<xml><ToUserName><![CDATA[{LOCAL_CORP_ID}]]></ToUserName>"
            f"<Encrypt><![CDATA[{encrypt}]]></Encrypt><AgentID><![CDATA[1000009]]></AgentID></xml>"
        ).encode("utf-8")

        signature = compute_signature(LOCAL_TOKEN, "1759385000", "aabbccdd", encrypt)
        plain = decrypt_message(LOCAL_TOKEN, key, "1759385000", "aabbccdd", signature, body, LOCAL_CORP_ID)

        assert plain == xml
        assert summarize(parse_message(plain)) == "文本消息：下载器在线吗"

    def test_reply_is_decryptable_by_the_same_rules(self):
        """企微收到我们的被动回复时也走这套解密，所以回复必须能被它解开"""
        key = decode_aes_key(LOCAL_AES_KEY)
        reply_xml = "<xml><Content><![CDATA[ok]]></Content></xml>"

        packed = encrypt_reply(LOCAL_TOKEN, key, reply_xml, LOCAL_CORP_ID, "1759385000", "aabbccdd")
        fields = parse_message(packed)

        assert set(fields) >= {"Encrypt", "MsgSignature", "TimeStamp", "Nonce"}
        assert fields["MsgSignature"] == compute_signature(LOCAL_TOKEN, "1759385000", "aabbccdd", fields["Encrypt"])
        assert verify_url(LOCAL_TOKEN, key, "1759385000", "aabbccdd",
                          fields["MsgSignature"], fields["Encrypt"], LOCAL_CORP_ID) == reply_xml


class TestSummarize:
    """摘要只进日志与面板，不该把整条消息糊上去"""

    def test_text_is_truncated(self):
        summary = summarize({"MsgType": "text", "Content": "あ" * 100})

        assert summary.startswith("文本消息：")
        assert summary.endswith("…")
        assert len(summary) < 80

    def test_event_includes_key(self):
        assert summarize({"MsgType": "event", "Event": "click", "EventKey": "SYNC"}) == "事件 click:SYNC"

    def test_unknown_type_falls_back(self):
        assert summarize({"MsgType": "image"}) == "消息类型 image"
        assert summarize({}) == "无法解析的消息"
