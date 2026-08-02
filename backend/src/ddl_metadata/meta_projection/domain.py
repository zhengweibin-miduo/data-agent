"""Meta Projection 的确定性版本策略。"""

import hashlib
import json


def metadata_desired_version(payload: object) -> str:
    """为规范化 desired payload 生成稳定版本。"""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
