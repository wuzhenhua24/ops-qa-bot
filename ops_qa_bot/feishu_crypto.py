"""飞书事件订阅 / 卡片回调的加密解密和签名校验。

开启 encrypt_key 后（飞书开放平台"事件订阅"页配置），飞书会：
1. 用 AES-256-CBC 加密原 JSON payload，外包 `{"encrypt": "<base64>"}`
2. 在请求头带上签名字段供服务端校验来源：
   - `X-Lark-Request-Timestamp`: 时间戳
   - `X-Lark-Request-Nonce`:     随机数
   - `X-Lark-Signature`:         sha256_hex(timestamp + nonce + encrypt_key + body_bytes)

使用：
    crypto = FeishuCrypto(encrypt_key)

    # 1) 校验签名
    if not crypto.verify_sig(ts, nonce, body_bytes, sig_header):
        raise HTTPException(401)

    # 2) 解密 payload（若含 encrypt 字段）
    wrapped = json.loads(body_bytes)
    payload = crypto.unwrap(wrapped)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class FeishuCrypto:
    def __init__(self, encrypt_key: str):
        if not encrypt_key:
            raise ValueError("encrypt_key 不能为空")
        self._encrypt_key = encrypt_key
        # AES 密钥是 encrypt_key 的 SHA-256 哈希，定长 32 字节
        self._aes_key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()

    def decrypt(self, encrypt_field: str) -> dict[str, Any]:
        """解密 encrypt 字段，返回原始 payload dict。

        格式：base64 解码后，前 16 字节是 IV，其余是 AES-256-CBC 密文（PKCS7 填充）。
        """
        raw = base64.b64decode(encrypt_field)
        if len(raw) < 32 or len(raw) % 16 != 0:
            raise ValueError("密文长度异常")
        iv, ciphertext = raw[:16], raw[16:]

        cipher = Cipher(algorithms.AES(self._aes_key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()

        # PKCS7 去填充
        pad_len = padded[-1]
        if pad_len < 1 or pad_len > 16:
            raise ValueError("PKCS7 填充异常")
        plaintext = padded[:-pad_len]
        return json.loads(plaintext.decode("utf-8"))

    def unwrap(self, wrapped: dict[str, Any]) -> dict[str, Any]:
        """如果 payload 被加密了（有 `encrypt` 字段）就解密，否则原样返回。"""
        if "encrypt" in wrapped:
            return self.decrypt(wrapped["encrypt"])
        return wrapped

    def verify_sig(
        self, timestamp: str, nonce: str, body: bytes, signature: str
    ) -> bool:
        """校验请求签名。缺任何一个头就视为校验失败。

        算法：sha256_hex(timestamp + nonce + encrypt_key + body_bytes)
        """
        if not (timestamp and nonce and signature):
            return False
        h = hashlib.sha256()
        h.update(timestamp.encode("utf-8"))
        h.update(nonce.encode("utf-8"))
        h.update(self._encrypt_key.encode("utf-8"))
        h.update(body)
        expected = h.hexdigest()
        # constant-time 比较防止 timing attack
        return hmac.compare_digest(expected, signature)
