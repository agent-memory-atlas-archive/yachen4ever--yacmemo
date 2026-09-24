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

AGENT_CONTRACT_VERSION = "0.3.4"

# 版本 -> 该版本里 agent 需要知道的变化（措辞可直接执行）
AGENT_CHANGELOG: dict[str, str] = {
    "0.3.4": (
        "- 已结案提案默认隐去：curator/ 提案报告的全部条目都执行完成或忽略后，"
        "文件头部会打「> 状态：已结案」标记，memory_search 默认不再返回它"
        "（显式 memory_read 仍可读——那是明确查阅）；不要再去执行已结案"
        "提案里的条目；\n"
        "- 质量提案裁决简化：WebUI 不再有「采纳」按钮——派发即采纳"
        "（复制执行指令给 agent），执行进度一律用 memory_audit_update 汇报；\n"
        "- 工具语义无其他变化。"
    ),
    "0.3.3": (
        "- 新增 memory_audit_update(issue_id, event, note) 工具：执行审计"
        "问题修复时向 server 汇报进度——executing 开始 / progress 过程 / "
        "executed 完成 / blocked 受阻需人工；issue_id 用审计报告或 WebUI "
        "执行指令里的 id（D3:.../P:... 形式），identity 自动记录；\n"
        "- 执行与判断分离：修复由 agent 执行并汇报，人只做忽略/派发判断，"
        "复审由审计自动确认（已执行且下轮不再报告即复审通过）——不要在"
        "汇报里声称\"已验证\"，也不要代替人做忽略；\n"
        "- memory_audit 输出新增「== 执行进度 ==」「== 复审通过 ==」两节，"
        "执行中的问题会带 agent 汇报的最新动态；\n"
        "- 工具语义无其他变化。"
    ),
    "0.3.2": (
        "- agent 层共享区路径变更：共享内容（必读等）迁入 "
        "agents/<agent>/shared/ 子树；本机层不变（agents/<agent>/<device>/）。"
        "第一层平铺文件（agents/<agent>/x.md）只读兼容——写入一律进 shared/ "
        "或本机设备子树，编辑/新建平铺路径会被拦截；\n"
        "- shared/必读.md 占位模板：WebUI 建 identity 时自动预创建，注入时"
        "若仍是模板（含'占位模板'标记）会持续提醒用 memory_edit 填写；\n"
        "- device 名不能叫 shared（保留目录名）；\n"
        "- 工具语义无其他变化。"
    ),
    "0.3.1": (
        "- 会话必读口径明确：memory_context 注入的画像 + agent 层必读 + "
        "本机层必读三件套即会话必读内容，注入即视为已读，无需再单独读文件；\n"
        "- 专属区指针路径卫生：引用其他层的必读/笔记必须代入真实设备名"
        "（agents/teleagent/r9000x/必读.md）；模板占位一律写尖括号形式"
        "（agents/<agent>/<device>/…），禁止留空段——agents/teleagent//必读.md "
        "会被当成真实路径、检索必然失败（2026-09-24 TeleAgent 实例）；\n"
        "- 工具语义无变化。"
    ),
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
    "- 会话必读三件套：memory_context 自动注入用户画像 + agent 层必读 + "
    "本机层必读，注入即视为已读，无需再单独读文件；\n"
    "- 长期记忆只写注册主题目录内：topics/<主题>/<笔记名>"
    "（缺 topics/ 前缀会被硬拦截，force 不豁免）；\n"
    "- abstract（topics/<主题>/abstract.md）是摘要卡，保持一句话现状；"
    "详细内容写成模块笔记；\n"
    "- 更新事实用 memory_edit / memory_edit_section 就地改，不新建重复笔记；\n"
    "- 专属必读：agent 层写 agents/<agent>/shared/必读.md（同 agent 跨设备"
    "共享），本机层写 agents/<agent>/<device>/必读.md；只放指针与纪律，"
    "事实进 topics/；引用其他层路径必须代入真实设备名，模板占位一律用尖括号"
    "（agents/<agent>/<device>/…），禁止留空段（agents/x//必读.md 会被当成"
    "真实路径、检索必然失败）；\n"
    "- journal/、archive/、curator/、agents/ 免注册区不受限"
    "（agents/ 另有 identity 专属守卫）；\n"
    "- 审计问题执行：修复时用 memory_audit_update 汇报进度"
    "（executing / progress / executed / blocked），复审由审计自动确认；\n"
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
