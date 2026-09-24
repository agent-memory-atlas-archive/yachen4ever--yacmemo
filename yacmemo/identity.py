"""Identity: agent+设备双维度的专属记忆边界（token 即身份）。

一个 user（记忆库）下可挂多个 identity，identity = (agent, device)。
token 约定为 `<device>_<agent>`（device 在前——同一台机器的各 agent token
在配置里按机器聚类；目录树里 agent 在上层——一个 agent 的全部记忆是
`agents/<agent>/` 下的一棵完整子树，memory_context 注入只扫这一个根）。

层级模型（visible/writable 是唯一权威实现，store/search/tools 共用）：
- user 层（所有 identity 共享）：topics/、journal/、archive/、curator/、
  TOPICS.md、PROFILE.md；
- agent 层（同 agent 跨设备共享）：agents/<agent>/ 下的平铺文件（必读.md）；
- identity 层（仅本 identity）：agents/<agent>/<device>/ 子树。

约定：agents/<agent>/ 下第一层只能是文件，子目录一律视为设备目录——
可见性判定因此是纯字符串逻辑，不需要文件系统参与。
无身份访问规则：
- 人类入口（WebUI、服务端内部）identity=None → 全库可见（人类是管理员）；
- MCP 工具未带 token → user 层照常可用，agents/ 区不可见不可写。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

AGENTS_PREFIX = "agents/"
# slug：小写字母/数字/短横线，1-32 位；不允许下划线——它是 token 的分隔符
_SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?")


class IdentityError(ValueError):
    """token / slug 非法。"""


@dataclass(frozen=True)
class Identity:
    agent: str
    device: str

    @property
    def token(self) -> str:
        if not self.agent:
            return ""
        return f"{self.device}_{self.agent}"

    @property
    def agent_prefix(self) -> str:
        """agent 层共享区（平铺文件）。"""
        return f"agents/{self.agent}/"

    @property
    def device_prefix(self) -> str:
        """identity 层专属区。"""
        return f"agents/{self.agent}/{self.device}/"


# MCP 未携带 token 时的调用身份：user 层照常可用，agents/ 区不可见不可写。
# identity=None 表示人类/服务端内部（WebUI 等）——全库可见。
ANONYMOUS = Identity(agent="", device="")


def _validate_slug(kind: str, value: str) -> str:
    v = (value or "").strip().lower()
    if not _SLUG_RE.fullmatch(v):
        raise IdentityError(
            f"{kind} 名非法: {value!r}（需小写字母/数字/短横线，1-32 位，"
            "不含下划线——它是 token 分隔符）")
    return v


def parse_token(token: str) -> Identity:
    """'r9000x_teleagent' -> Identity(agent='teleagent', device='r9000x')。

    device 在前、agent 在后，按第一个下划线切分（slug 本身不含下划线，
    因此切分无歧义）。
    """
    t = (token or "").strip()
    if "_" not in t:
        raise IdentityError(
            f"identity token 非法: {t!r}（期望 <device>_<agent>，"
            "如 r9000x_teleagent）")
    device, agent = t.split("_", 1)
    return Identity(agent=_validate_slug("agent", agent),
                    device=_validate_slug("device", device))


def make_identity(agent: str, device: str) -> Identity:
    """WebUI 新建 identity：分别校验两个 slug。"""
    return Identity(agent=_validate_slug("agent", agent),
                    device=_validate_slug("device", device))


def _segments(rel: str) -> list[str]:
    return rel.replace("\\", "/").split("/")


def visible(rel: str, identity: Identity | None) -> bool:
    """rel 是否对 identity 可见（MCP 读/检索/list 的统一过滤谓词）。

    identity=None（人类/服务端内部）恒可见。
    """
    if identity is None:
        return True
    if not rel.startswith(AGENTS_PREFIX):
        return True
    seg = _segments(rel)  # [agents, <agent>, ...]
    if len(seg) < 2 or seg[1] != identity.agent:
        return False
    if len(seg) <= 2:
        return True  # 目录本身（list 用）
    # 平铺文件（agent 层共享，len(seg)==3）对所有同 agent 设备可见；
    # 更深的路径都是设备子树，只有本机可见（含共享子目录约定上不存在，
    # 人类经 WebUI 手工创建的也会对 agent 隐藏——见模块 docstring）
    if len(seg) == 3:
        return True
    return seg[2] == identity.device


def writable(rel: str, identity: Identity | None) -> bool:
    """rel 是否对 identity 可写（MCP 写路径守卫谓词）。

    identity=None 对 agents/ 区一律不可写（user 层照常——由调用方区分：
    store 的守卫只在 rel 落在 agents/ 时才咨询本函数）。
    """
    if identity is None:
        return False
    if not rel.startswith(AGENTS_PREFIX):
        return False
    seg = _segments(rel)
    if len(seg) < 2 or seg[1] != identity.agent:
        return False
    if len(seg) == 3:
        return True  # agent 层平铺共享文件：同 agent 的设备共同维护
    return seg[2] == identity.device
