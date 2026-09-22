"""脱敏：全仓唯一一份"哪些字符串不能出现在输出里"。

为什么单独一层：审计条目、Trace 预览、Provider 返回与异常文案都要过同一层脱敏，
而"什么是凭据"有两个来源 ——

1. **当前进程的环境变量**：名单在 ``providers._SECRET_ENV_NAMES``（那里定义"哪些环境
   变量是 Key"），这里只读它，不抄第二份。抄一份的结果是新接一个 Provider 时两边
   不一致，而漏掉的那一边正好决定"会不会把 Key 泄出去"。
2. **正文里自带的凭据字样**：``api_key=…`` / ``token: …`` / ``Bearer …`` / ``cookie=…``，
   这类字符串出现在第三方返回、异常堆栈、模型输出里，跟环境变量无关。

两条硬约定
----------
* **只做减法，不抛异常**：脱敏是旁路，它失败不该让调用方（写审计、渲染 Trace）挂掉。
* **只替换值，不替换键名**：``api_key: sk-xxx`` 要变成 ``api_key: ***``。把 ``api_key``
  这个词本身擦掉，读的人就不知道这里原本有个 Key，也看不出脱敏发生过。
"""

from __future__ import annotations

import os
import re

from .providers import _SECRET_ENV_NAMES

#: 预览的默认字符上限。第三方正文与模型输出动辄几百 KB，而预览要写进
#: trace span / audit_report（JSON 列）；不设上限会把库与响应一起撑大。
PREVIEW_CHARS = 2000

#: ``key = value`` / ``"key": "value"`` 形状的凭据。键名沿用管理端原本那套词表：
#: 它已经在真实日志上跑过，换一套只会让同一段文本在两个页面上脱敏结果不同。
_KEY_VALUE_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|authorization|secret|password|cookie)"
    r"(\"?\s*[:=]\s*\"?)([A-Za-z0-9_\-./+]{6,})"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-./+=]{6,}")


def scrub(text: str) -> str:
    """把凭据从文本里抹掉。"""

    if not text:
        return text
    for name in _SECRET_ENV_NAMES:
        secret = (os.environ.get(name) or "").strip()
        # 短值（测试里塞的 "x"）直接替换会把正文打成筛子；阈值与 providers 侧保持一致。
        if secret and len(secret) >= 8:
            text = text.replace(secret, "***")
    text = _BEARER_PATTERN.sub("Bearer ***", text)
    return _KEY_VALUE_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}***", text)


def clip(text: str, *, limit: int = PREVIEW_CHARS) -> str:
    """按字符数截断，并留下"截断过、原文多长"的尾注。

    尾注不是装饰：读者必须能一眼分辨"模型就说了这么多"和"我们只留了前 N 个字符"，
    否则一份被截断的输出会被当成完整的输出来读。
    """

    text = text or ""
    if len(text) > limit:
        return f"{text[:limit]}…（已截断，原文 {len(text)} 字符）"
    return text
