"""Identity: agent+设备双维度的专属记忆边界（token 即身份）。

一个 user（记忆库）下可挂多个 identity，identity = (agent, device)。
token 约定为 `<device>_<agent>`（device 在前——同一台机器的各 agent token
在配置里按机器聚类；目录树里 agent 在上层——一个 agent 的全部记忆是
`agents/<agent>/` 下的一棵完整子树，memory_context 注入只扫这一个根）。

层级模型（visible/writable 是唯一权威实现，store/search/tools 共用）：
- user 层（所有 identity 共享）：topics/、journal/、archive/、curator/、
  TOPICS.md、PROFILE.md；
- agent 层（同 agent 跨设备共享）：agents/<agent>/shared/ 子树（必读.md）；
- identity 层（仅本 identity）：agents/<agent>/<device>/ 子树。

约定：agents/<agent>/ 第一层只有两类子目录——shared/ 与 <device>/
（shared 为保留目录名，不能用作设备名）。历史平铺文件
（agents/<agent>/x.md）只读兼容，写入一律收敛到上述两类子树——
可见性判定因此仍是纯字符串逻辑，不需要文件系统参与。
无身份访问规则：
- 人类入口（WebUI、服务端内部）identity=None → 全库可见（人类是管理员）；
- MCP 工具未带 token → user 层照常可用，agents/ 区不可见不可写。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

AGENTS_PREFIX = "agents/"
# agent 层共享子树目录名（保留字：设备名不得占用）
SHARED_DIR = "shared"
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
        """agent 根目录（其下只有 shared/ 与 <device>/ 两类子目录）。"""
        return f"agents/{self.agent}/"

    @property
    def shared_prefix(self) -> str:
        """agent 层共享子树（同 agent 跨设备共享）。"""
        return f"agents/{self.agent}/{SHARED_DIR}/"

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
    if kind == "device" and v == SHARED_DIR:
        raise IdentityError(
            "device 名不能是 'shared'——它是 agent 层共享子树的保留目录名")
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
    agents/<agent>/ 下两类子目录：shared/（同 agent 共享）与 <device>/
    （本机专属）。历史平铺文件（agents/<agent>/x.md）保持可读——写入已
    一律收敛到 shared/，见 writable。
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
    if len(seg) == 3:
        return True  # 历史平铺文件：只读兼容
    return seg[2] == SHARED_DIR or seg[2] == identity.device


def writable(rel: str, identity: Identity | None) -> bool:
    """rel 是否对 identity 可写（MCP 写路径守卫谓词）。

    identity=None 对 agents/ 区一律不可写（user 层写守卫由调用方区分：
    store 的守卫只在 rel 落在 agents/ 时才咨询本函数）。
    agents/<agent>/ 第一层不再允许平铺文件（只读兼容历史），写入收敛为
    两类子树：shared/（同 agent 共享）与本机 <device>/。
    """
    if identity is None:
        return False
    if not rel.startswith(AGENTS_PREFIX):
        return False
    seg = _segments(rel)
    if len(seg) < 2 or seg[1] != identity.agent:
        return False
    if len(seg) == 3:
        return False  # 平铺层禁止写入：共享内容进 shared/，设备内容进设备子树
    return seg[2] == SHARED_DIR or seg[2] == identity.device
