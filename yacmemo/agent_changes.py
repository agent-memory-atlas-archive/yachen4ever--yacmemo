"""Agent 接入契约的版本与增量变更（integration_check 的数据源）。

服务器不推送更新：memory_context 头部随身携带当前契约版本，agent 把自己
接入时所依据的版本记在本地接入提示词里，每次会话开始比对，落后即调用
integration_check(onboarded_version=...) 获取增量变更与最新写入约定，
自主刷新本地提示词。

契约版本只在 agent 可感知的行为变化（工具语义、返回文案、写入约定）时
前进，独立于包版本；数值上当前与包版本保持一致。改这里的行为约定时，
务必同步追加 AGENT_CHANGELOG 条目并前进版本号。
"""

from __future__ import annotations

AGENT_CONTRACT_VERSION = "0.1.3"

# 版本 -> 该版本里 agent 需要知道的变化（措辞可直接执行）
AGENT_CHANGELOG: dict[str, str] = {
    "0.1.3": (
        "- 拦截错误自带近失诊断：写入缺 topics/ 前缀会被点名，"
        "并给出可直接重试的 title；\n"
        "- topic_register 成功返回含可复制的写入模板：详细笔记写 "
        "topics/<主题>/<笔记名>，abstract 是摘要卡（保持一句话现状），"
        "不要把长文塞进 abstract；\n"
        "- 新增 integration_check 工具（本机制）与 memory_context 版本头。"
    ),
}

# 最新写入约定速览：integration_check 返回全文，agent 据此刷新本地提示词
AGENT_CONTRACT_DIGEST = (
    "## 写入约定速览\n"
    "- 长期记忆只写注册主题目录内：topics/<主题>/<笔记名>"
    "（缺 topics/ 前缀会被硬拦截，force 不豁免）；\n"
    "- abstract（topics/<主题>/abstract.md）是摘要卡，保持一句话现状；"
    "详细内容写成模块笔记；\n"
    "- 更新事实用 memory_edit / memory_edit_section 就地改，不新建重复笔记；\n"
    "- journal/、archive/、curator/ 免注册区不受限；\n"
    "- topic_register / topic_unregister / archive_topic / memory_delete"
    " 仅在用户明确要求时调用。\n"
    "完整规格：docs/09-agent-onboarding.md 与 docs/02-mcp-tools.md。"
)


def contract_version_key(version: str) -> tuple[int, ...]:
    """'v0.1.3' / '0.1.3' -> (0, 1, 3)；解析失败按最旧 (0,) 处理。"""
    try:
        return tuple(int(x) for x in version.strip().lstrip("vV").split("."))
    except (ValueError, AttributeError):
        return (0,)
