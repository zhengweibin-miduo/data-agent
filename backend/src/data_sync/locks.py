"""数据同步 generation 读写协调资源身份。"""

from __future__ import annotations

from base64 import urlsafe_b64encode
from hashlib import sha256


def generation_lock_name(dw_database: str, target_table: str) -> str:
    """生成同一二进制 DW target 共享且不超过 64 字节的稳定锁名。"""
    identity = (
        len(dw_database.encode("utf-8")).to_bytes(2, "big")
        + dw_database.encode("utf-8")
        + target_table.encode("utf-8")
    )
    digest = urlsafe_b64encode(sha256(identity).digest()).rstrip(b"=").decode("ascii")
    readable = target_table.encode("utf-8")[:12].decode("utf-8", errors="ignore")
    return f"dsg:{readable}:{digest}"
