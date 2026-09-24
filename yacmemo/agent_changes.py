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

AGENT_CONTRACT_VERSION = "0.3.0"

# 版本 -> 该版本里 agent 需要知道的变化（措辞可直接执行）
AGENT_CHANGELOG: dict[str, str] = {
    "0.3.0": (
        "- 新增 identity（身份）机制：MCP 请求可携带 token 标明 agent+设备——"
        "HTTP 请求头 Authorization: Bearer <device>_<agent>（如 r9000x_teleagent，"
        "兼容 X-Yacmemo-Token 头），stdio 用环境变量 YACMEMO_TOKEN；token 由 "
        "WebUI 身份页确定性生成，也可直接按约定拼写；\n"
        "- agents/ 专属记忆区上线：agents/<agent>/ 下平铺文件为 agent 层"
        "（同 agent 跨设备共享），agents/<agent>/<device>/ 子树为本机专属；"
        "不同 identity 互相不可见（读、检索、列表、写全链路强制），未携带 "
        "token 的旧配置照常可用 user 层但看不到也写不了 agents/ 区；\n"
        "- memory_context 会自动注入你的专属必读（agent 层 + 本机层），"
        "不再依赖提示词提醒；memory_search 只返回 user 层 + 你的专属区；\n"
        "- 写入约定新增：你的专属必读写 agents/<agent>/必读.md（跨设备共享）"
        "或 agents/<agent>/<device>/必读.md（本机专属）；必读只放指针与纪律，"
        "事实一律进 topics/ 与所有 agent 共享。"
    ),
    "0.2.1": (
        "- memory_edit / memory_edit_section 成功返回在合并改写清掉旧冲突对时，"
        "追加\"（自动清除过期冲突对 N 对）\"——看到它即说明这次编辑消解了语义撞车；\n"
        "- memory_audit 输出新增\"== 自动清除过期冲突对 ==\"行；\n"
        "- 工具语义无其他变化。"
    ),
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
    "- 专属必读：写 agents/<agent>/必读.md（同 agent 跨设备共享，第一层只放"
    "平铺文件）或 agents/<agent>/<device>/必读.md（本机专属）；只放指针与纪律，"
    "事实进 topics/；\n"
    "- journal/、archive/、curator/、agents/ 免注册区不受限"
    "（agents/ 另有 identity 专属守卫）；\n"
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
