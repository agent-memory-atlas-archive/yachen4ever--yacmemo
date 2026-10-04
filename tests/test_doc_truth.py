"""文档守卫：工具面清单与计数必须与代码一致。

背景（2026-10-03 审查）：工具面从 13 一路长到 22 个，文档里的计数却散落着
13 / 17 / 20 三种说法，`docs/02-mcp-tools.md` 标题上的「17 个」甚至是把最后一
个工具的小节号 §17 当成了数量。同一轮审查还发现 `archive_note` / `unarchive_note`
两个工具从未进过 README 表格——即"加了工具但没写文档"。第二轮审查又发现上一版
守卫自己漏了四类漂移，全部已在这里堵上（见下"这一版补上的洞"）。

真值不写死在本文件里：`22` 这个数从**实际 MCP 握手**里现取（进程内起一个
FastMCP，用 `mcp.shared.memory` 跑一次真实的 list_tools 往返），文档必须自己跟上。
新增一个 `@tool()` 而忘了改文档，这里立刻红。

断言分五类：

1. **覆盖率（code→docs，认结构不认字面）**——每个工具名必须落在它该在的结构里：
   README zh/en 的**工具表格首列**、`docs/02-mcp-tools.md` zh/en 的**规格小节标题**、
   `docs/01-architecture.md` zh/en 工具面表格的首列。抓的是"加了工具却没写文档"。
   （上一版只检查"名字在某处出现过"，散文里随口提一句就能过。）
2. **反向覆盖（docs→code）**——`docs/02-mcp-tools.md` 里每个工具**规格小节**点名的
   工具，必须仍是已注册工具。抓的是"工具从 `tools.py` 删了、规格小节还留着"。
3. **计数**——散落的工具数量声明必须等于真值。抓的是"数量说错了"。
4. **握手 == 声明**——`tools.py` 里 `@tool()` 装饰的函数个数/名单，必须等于握手
   拿到的清单。上一版写的是 `len(tools) >= 20` 这种拍脑袋的下限；真值应该由代码
   自己推出来。
5. **正则自检**——计数正则本身也要有测试，否则它悄悄变窄时没人知道。
6. **行为键（结果契约）**——`Store.audit()` 返回字典的每一个键，必须在
   `docs/02-mcp-tools.md` zh/en 里以行内代码形式写出。前面五类抓的是数字与
   工具**名**，抓不到"新行为发了但没进文档"：本轮审查正是这样发现
   `quarantined_index_rows` 只活在 `store.py` 与 `tools.py` 的 docstring 里，
   `docs/**` 与两份 README 一个字都没有。规则与取舍见下"第 6 类断言"。

这一版补上的洞（上一版全漏）：

* **只扫 6 个文件** → 现在扫 `README.md`、`README.en.md`、`docs/**` 全部 `.md`，
  以及 `yacmemo/**` 的**全部 docstring**（用 `ast` 取，不扫注释也不扫普通字符串）。
  D-1 那个 `tools.py` 里的「17 memory tools」就是这么活下来的。
* **正则太挑措辞** → 数量与计数名词之间允许连接词（`MCP` / `记忆` / `memory`），
  `项`/`条` 也算量词，另认「工具面共 22 项」「MCP 工具面：22」这类名词在前的说法。
  这些写法都有 `test_count_patterns_cover_known_phrasings` 钉住。
* **单向** → 补了第 2 类断言。
* **任意下限** → 补了第 4 类断言。

关于计数正则（认得哪些写法、故意不认哪些）：

认得——
  * `22 个工具` / `22 tools` / `22 项工具` / `17 memory tools`（数量 → 可选量词
    → 可选连接词 → 计数名词）
  * `MCP 工具（22 个）` / `MCP 工具面（22 个）` / `工具面（累计 22 个）`
    / `MCP 工具规格（22 个）` / `MCP 工具面：22`（工具面点名后紧跟括号或冒号）
  * `工具面共 22 项` / `工具面包含 22 个记忆工具`（工具面点名后跟显式连接词）
  * `MCP Tools (22)` / `MCP tool surface (22 tools)` / `MCP Tool Specifications (22 tools)`

故意不认（否则测试会变成噪音，误报一次就没人再信它）——
  * 主题计数：`当前活跃主题（共 9 个）` —— 点名的是"主题"不是工具面
  * 测试计数：`51 个测试通过` —— 计数名词是"测试"不是"工具"
  * 演进记录：`工具面增减（13 → 16: …）` —— `增减` 紧跟名词破坏了"点名"，且带
    箭头是版本迁移史而非当前声明；英文 `Tool surface additions (13 → 16: …)` 同理
    （`additions` 夹在中间）。工具面名词后必须**独立成词**（后面不能跟字/汉字）。
  * 章节号 / 小节号 / 引用：`§17`、`见 6.1`、`[02-mcp-tools.md](…)` —— 数字前有
    `§`/`#`/`v`/数字/小数点，或后接的既不是量词也不是计数名词
  * 端口号、年份、版本号：`9721`、`2026-09-19`、`0.3.14`

**仍然存在的宽松（明说，别误以为这是完备证明）**：

* 计数只抓**词法上贴着工具面**的数字。有人写"工具面这两年长了一倍多"或
  "最近扩充到了两位数"，本守卫看不见。
* 计数只抓**单行**。跨行拆开的"22 个" + "工具"不会被当成一处声明。
* 规格小节只看 `docs/02-mcp-tools.md` 的 markdown 标题；把工具名只写进正文
  小节（不写进标题）不计入覆盖。反向检查也只看标题里的 snake_case 标识符。
* 工具表格检查只认**首列**。工具名写在表格第二列（签名列 / 用途列）不算收录。
* `docs/**` 之外的 `legacy/**`（v1 历史文档）有意不扫——那是归档，不是当前声明。
* 计数真值取自一次握手；它验证的是"文档与当前代码一致"，不验证"当前实现是否正确"。

## 第 6 类断言：审计结果键的规则与取舍（明说）

**为什么需要它**：前五类都是**形状**断言（有几个、叫什么名字、在不在某张表里），
看不见"行为"本身。一轮新的安全修复给 `Store.audit()` 加了 `quarantined_index_rows`
（外加 `invalid_topic_cards`、`card_invalid` / `card_error`）——代码里到处是注释与
docstring 讲得清清楚楚，文档面却零提及。数字没变、工具名没变、工具数量没变，
所以没有任何一类断言会响。这类漂移只能靠"键必须在规格里点名"来堵。

**真值怎么来**：优先**运行时**——在测试夹具上真跑一次 `Store.audit()`，取返回
字典的键；再从 `store.py` 的 `res = {...}` 字面量用 `ast` 取一份，两处必须相等。
相等这一条本身就是断言的一部分：它保证真值既没漏键（源码是全量字面量）、也没
虚增（运行时真的返回这些键）。将来 `audit()` 改成动态拼键，交叉核对会先红，
提示真值需要重新推导，而不是让守卫悄悄只看运行时那一半。

**匹配形状**：键必须以**行内代码**（一对反引号内）出现在规格里。纯子串匹配不可用
——`git` 只有三个 ASCII 字符，`docs/02-mcp-tools.md` 里早就有一句"git 快照状态行"
会白送它通过；`added` / `missing` / `stray` 同理。行内代码把"这里点名了这个键"
变成作者必须主动做出的动作，与本文件既有的"认结构不认字面"（表格首列 / 小节
标题）同一条路数。

**排除清单：没有。** 考虑过给"纯内部记账键"（`guard_stats`、`git`、
`pruned_blank_actions` 这类计数与状态行）开一个跳过列表，结论是不开：
`audit()` 的返回值被**三处消费方**共用——MCP `memory_audit` 逐字段拼文本、
WebUI 审计页读同一个字典、审计快照按同一份数据写 markdown——任何新增键都在
同时改变三处的行为，这正是"必须回答它是什么"的时刻，而代价只是一行表格
（`docs/02-mcp-tools.md` §7 的「审计结果键」表）。带任意排除项的守卫比一个窄而
诚实的守卫更糟：例外一旦合法化，后来者会照抄那个列表。若将来确有键需要排除，
请在这里连同理由一并写下来，而不是在断言里随手加一个 `if key in ...`。
"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import (
    create_connected_server_and_client_session as connected,
)

from yacmemo.search import Searcher
from yacmemo.store import Store
from yacmemo.tools import register_tools

REPO = Path(__file__).resolve().parents[1]
TOOLS_PY = REPO / "yacmemo" / "tools.py"

# 覆盖率断言：工具名必须出现在这些文件里（按结构认，见各自 *_rows / *_sections）
COVERAGE_FILES = (
    "README.md",
    "README.en.md",
    "docs/02-mcp-tools.md",
    "docs/en/02-mcp-tools.md",
)

# 工具规格文档（反向覆盖 docs→code 也认这一对）
SPEC_FILES = ("docs/02-mcp-tools.md", "docs/en/02-mcp-tools.md")

ARCH_FILES = ("docs/01-architecture.md", "docs/en/01-architecture.md")


# ---------------------------------------------------------------- 文件收集

# 计数断言扫描的文档面：README 两份 + docs 下全部 markdown。
# `legacy/**` 有意排除（v1 归档，不是当前声明）；`frontend/` 没有 markdown。
COUNT_DOCS = (
    "README.md",
    "README.en.md",
    *(f"docs/{p.relative_to(REPO / 'docs').as_posix()}"
      for p in sorted((REPO / "docs").rglob("*.md"))),
)


def count_sources() -> list[tuple[str, int, str]]:
    """(文件, 行号, 行文本)——计数断言的全部输入。

    两类来源：
    * `README*` 与 `docs/**` 的全部 markdown（`legacy/` 排除，见模块 docstring）
    * `yacmemo/**` 全部 `.py` 的**docstring**。用 `ast` 取而不是正则扫全文：注释里
      写「新加工具写 `@tool()`」不会变成断言，代码里的字符串字面量也不会。
      D-1 的 `tools.py` 「Register the 17 memory tools」正是 docstring 里的漏网之鱼。
    """
    rows: list[tuple[str, int, str]] = []
    for rel in COUNT_DOCS:
        for lineno, line in enumerate((REPO / rel).read_text(encoding="utf-8").splitlines(), 1):
            rows.append((rel, lineno, line))
    for path in sorted((REPO / "yacmemo").rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - 源码能跑就不该走到
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = getattr(node, "body", None)
            if not body or not isinstance(body[0], ast.Expr):
                continue
            value = body[0].value
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                continue
            # 用 docstring 自己的 lineno，不是外层 def/class 的——
            # 否则报错行号会指向 def 那一行，差两三行很难对上。
            start = body[0].lineno
            for offset, line in enumerate(value.value.splitlines()):
                rows.append((rel, start + offset, line))
    return rows


# ---------------------------------------------------------------- 计数正则

# 数字片段：左边不是数字/小数点（否则 `02 tool specs` 会被读成「2 个工具」），
# 也不是 § / # / v（否则 `§17`、`v0.3` 的尾数会被读成工具数），且首位非 0
# （计数从不写成 02、017 这类补零形式）。
NUM = r"(?<![§#vV\d.])[1-9]\d*"

# 工具面点名（名词必须"独立成词"：后面不能直接跟字/汉字，见 RE_工具面括号）
SURFACE = (r"(?:MCP\s*)?(?:工具面|工具规格|工具|"
           r"tool\s+surface|Tool\s+surface|Tool\s+Specifications|MCP\s+Tools|tools)")
# 名词后若紧跟词字符就不是"点名"而是长词的一部分（工具面增减 / Tool surface additions）
SURFACE_STANDALONE = rf"{SURFACE}(?![\w一-鿿])"
# 数量与计数名词之间允许的连接词
LINK = r"(?:MCP|记忆|memory|mcp)"
# 量词
MEASURE = r"(?:个|项|条)"
# 计数名词：中文没有 \b 可用（「工具」后面跟汉字），英文 tools 保留 \b 防 toolsets
NOUN = r"(?:工具|tools\b)"
# 名词在前时，名词与数量之间必须有的显式连接词
JOIN = r"(?:共|共计|包含|含|合计|累计|总共|in\s+total|total)"
# 演进迁移记号：`13 → 16` 是历史，不是当前声明
NOT_ARROW = r"(?!\s*(?:→|->|~|～|–|—|至))"

COUNT_PATTERNS = (
    # ① 数量 → 可选量词 → 可选连接词 → 计数名词。
    #    `22 个工具` / `22 tools` / `22 项工具` / `17 memory tools` /
    #    `22 个 MCP 工具` / `工具面包含 22 个记忆工具`
    ("数量+计数名词",
     re.compile(rf"({NUM})\s*{MEASURE}?\s*(?:{LINK}\s*)*{NOUN}")),
    # ② 工具面点名（独立成词）后紧跟括号或冒号，括号内是纯数量。
    #    `MCP 工具（22 个）` / `MCP 工具面：22` / `MCP Tools (22)` /
    #    `Tool surface (22 tools)` / `MCP Tool Specifications (22 tools)`
    ("工具面括号式",
     re.compile(rf"{SURFACE_STANDALONE}\s*[（(:：]\s*(?:{JOIN}\s*)?"
                rf"({NUM}){NOT_ARROW}\s*{MEASURE}?\s*{NOUN}?\s*[）)]?")),
    # ③ 工具面点名后跟显式连接词，再是数量（量词/计数名词可省）。
    #    `工具面共 22 项` / `工具面累计 22 个` / `Tool surface in total 22 tools`
    #    这里不套 SURFACE_STANDALONE：连接词本身就是消歧依据（`共`/`包含` 不是
    #    「增减」那种长词的后半截），套上反而把「工具面共」也否掉。
    ("工具面连接词式",
     re.compile(rf"{SURFACE}\s*{JOIN}\s*({NUM}){NOT_ARROW}"
                rf"\s*{MEASURE}?\s*{NOUN}?")),
)


# ---------------------------------------------------------------- 真值

def real_tool_names(store: Store, searcher: Searcher) -> list[str]:
    """从实际 MCP 握手取工具清单——真值只有一个来源：代码。"""
    mcp = FastMCP("doc-truth")
    register_tools(mcp, store, searcher)

    async def run() -> list[str]:
        async with connected(mcp._mcp_server) as c:
            return [t.name for t in (await c.list_tools()).tools]

    return sorted(asyncio.run(run()))


def declared_tool_names() -> list[str]:
    """`tools.py` 里被 `@tool()` 装饰的函数名——不经握手的第二份声明。

    这是「期望值」而不是「实际值」：握手说有几个，源码说应该有几个，两者相等
    才说明没有工具因为写错位置/漏注册而凭空消失。上一版的 `len(tools) >= 20`
    只是个拍脑袋的下限——删掉 3 个工具它照样绿。
    """
    tree = ast.parse(TOOLS_PY.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "register_tools")
    names = []
    for node in fn.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorated = any(
            isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "tool"
            for d in node.decorator_list
        )
        if decorated:
            names.append(node.name)
    return sorted(names)


# ---------------------------------------------------------------- 结构提取

# 工具名在本项目里的形状：小写 snake_case，且必须含下划线。
# 用它从标题里挑"这看起来是个工具名"：`## 0. 身份（identity）与专属记忆` 的
# `identity`（无下划线）不是工具，`## 7.5 memory_audit_update` 是。
SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")


def _cells(row: str) -> list[str]:
    return [c.strip().strip("`*").strip() for c in row.split("|")]


def table_first_column(text: str) -> set[str]:
    """markdown 表格每一行的**首列**取值。

    比"名字在文里出现过"紧得多：工具名写在签名列/用途列不算收录。
    """
    names: set[str] = set()
    for line in text.splitlines():
        m = TABLE_ROW.match(line)
        if m:
            cells = _cells(m.group(1))
            if cells:
                names.add(cells[0])
    return names


def spec_sections(text: str) -> dict[str, int]:
    """工具规格文档的小节标题 → 工具名（`## 14.5 archive_note / unarchive_note`
    同时认出两个工具；`## 总则`、`## Decision tree …` 认不出工具名，返回空）。"""
    found: dict[str, int] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = HEADING.match(line)
        if not m:
            continue
        for name in SNAKE.findall(m.group(2)):
            found.setdefault(name, lineno)
    return found


def architecture_table(text: str) -> str:
    """截出 01-architecture.md 的工具面表格区域（§六 到下一个二级标题）。"""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.search(r"MCP 工具面|MCP tool surface", line):
            start = i
            break
    assert start is not None, "docs/01-architecture.md 里找不到工具面章节标题"
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            return "\n".join(lines[start:j])
    return "\n".join(lines[start:])


# -------------------------------------------------- 审计结果键（行为契约）

STORE_PY = REPO / "yacmemo" / "store.py"
# 行内代码：一对反引号内的整段文字（同一行内闭合，跨行代码块不算）
CODE_SPAN = re.compile(r"`([^`\n]+)`")


def code_spans(text: str) -> set[str]:
    """文档里所有**行内代码**的内容——"作者点名了它"的结构证据。

    不用纯子串：`git` / `added` / `missing` 这类键在任何自然句子里都会顺带
    出现（"git 快照状态行"、"缺向量"），子串匹配会让守卫白送通过。
    """
    return {m.group(1).strip() for m in CODE_SPAN.finditer(text)}


def audit_result_keys(store: Store) -> set[str]:
    """运行时真值：在夹具上真跑一次审计，取返回字典的键。

    `audit()` 的返回值是三处消费方共用的契约（MCP 文本 / WebUI 审计页 / 快照
    写入），所以它就是"agent 与运维看得见的行为面"。
    """
    return set(store.audit())


def audit_result_keys_from_source() -> set[str]:
    """源码真值：`Store.audit()` 里 `res = {...}` 字面量的键（ast 取）。

    与运行时那份交叉核对（见测试）。它保证真值没漏——`audit()` 的返回值是
    一处字面量，没有分支拼键，所以"源码全量"与"运行时实际"必须相等。
    """
    tree = ast.parse(STORE_PY.read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "Store")
    fn = next(n for n in cls.body
              if isinstance(n, ast.FunctionDef) and n.name == "audit")
    literals = [n.value for n in ast.walk(fn)
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict)
                and any(isinstance(t, ast.Name) and t.id == "res"
                        for t in n.targets)]
    assert len(literals) == 1, (
        "store.py 的 Store.audit 里找不到唯一的 `res = {...}` 字面量"
        f"（找到 {len(literals)} 处）——真值推导方式要跟着实现改")
    return {k.value for k in literals[0].keys if isinstance(k, ast.Constant)}


# ---------------------------------------------------------------- 断言


def test_count_patterns_cover_known_phrasings():
    """计数正则本身要有测试——否则它悄悄变窄时没人知道。

    左边这些是真实文档里出现过的（或本轮审查点名要求认的）写法，全部必须被判成
    一处「数量声明」；右边这些必须一个都不认（年份、端口、章节号、演进迁移）。
    """
    must_match = [
        "22 个工具", "22 tools", "22 项工具", "共 22 项工具", "17 memory tools",
        "22 个 MCP 工具", "工具面包含 22 个记忆工具", "工具面共 22 项",
        "MCP 工具面：22", "MCP 工具（22 个）", "MCP 工具面（22 个）",
        "工具面（累计 22 个）", "MCP 工具规格（22 个）", "MCP Tools (22)",
        "MCP tool surface (22 tools)", "MCP Tool Specifications (22 tools)",
    ]
    must_not_match = [
        "当前活跃主题（共 9 个）", "51 个测试通过", "服务端口为 9721",
        "2026-09-19 TeleAgent 实测", "契约 0.3.15", "见 6.1", "§17",
        "[02-mcp-tools.md](02-mcp-tools.md)",
        "工具面增减（13 → 16：archive_topic 加入）",
        "Tool surface additions/removals (13 → 16: archive_topic added)",
        "跨用户最近 8 条 MCP 调用流", "使用记录（工具过滤、近 14 天概览）",
    ]

    def claims(line: str) -> list[str]:
        out = []
        for _, pattern in COUNT_PATTERNS:
            out += [m.group(1) for m in pattern.finditer(line)]
        return out

    missed = [s for s in must_match if not claims(s)]
    assert not missed, "计数正则认不出这些写法（变窄了）：\n  " + "\n  ".join(missed)

    noisy = [s for s in must_not_match if claims(s)]
    assert not noisy, "计数正则误伤了这些写法（太松了）：\n  " + "\n  ".join(noisy)


def test_handshake_matches_declared_tools(store: Store, searcher: Searcher):
    """握手拿到的工具面，必须等于 `tools.py` 里 `@tool()` 声明的那份。

    期望值从代码自己推出来，不写死数字，也不写拍脑袋的下限：
    少注册一个工具（装饰器写错位置、函数挪出 `register_tools`）这里就红。
    """
    live = real_tool_names(store, searcher)
    declared = declared_tool_names()
    assert live == declared, (
        f"握手与 tools.py 声明不一致（各 {len(live)}/{len(declared)} 个）：\n"
        f"  只在握手里：{sorted(set(live) - set(declared))}\n"
        f"  只在声明里：{sorted(set(declared) - set(live))}"
    )


def test_every_tool_is_documented(store: Store, searcher: Searcher):
    """新加工具忘了写文档 → 这里点名。

    认结构不认字面：README 认工具表格首列，工具规格认小节标题，架构文档认
    工具面表格首列。散文里随口提一句工具名不算收录。
    """
    tools = real_tool_names(store, searcher)
    missing: list[str] = []

    for rel in COVERAGE_FILES:
        text = (REPO / rel).read_text(encoding="utf-8")
        if rel in SPEC_FILES:
            have = set(spec_sections(text))
            kind = "规格小节"
        else:
            have = table_first_column(text)
            kind = "工具表格首列"
        for name in tools:
            if name not in have:
                missing.append(f"{rel}: 缺{name}（{kind}）")

    for rel in ARCH_FILES:
        table = table_first_column(architecture_table((REPO / rel).read_text(encoding="utf-8")))
        for name in tools:
            if name not in table:
                missing.append(f"{rel}: 工具面表格首列缺 {name}")

    assert not missing, (
        f"共 {len(missing)} 处文档未收录工具（真值 {len(tools)} 个）：\n  "
        + "\n  ".join(missing)
    )


def test_documented_tools_still_registered(store: Store, searcher: Searcher):
    """反向：规格文档里还留着小节、但代码里已经没有的工具 → 这里点名。

    `test_every_tool_is_documented` 只看 code→docs；工具从 `tools.py` 删掉时，
    它的完整规格小节会静静留在 `docs/02-mcp-tools.md` 里，没有任何断言会响。
    """
    tools = set(real_tool_names(store, searcher))
    ghosts: list[str] = []
    for rel in SPEC_FILES:
        for name, lineno in sorted(spec_sections((REPO / rel).read_text(encoding="utf-8")).items()):
            if name not in tools:
                ghosts.append(f"{rel}:{lineno} 规格小节仍在，但代码里已无 {name} 工具")
    assert not ghosts, (
        f"共 {len(ghosts)} 处规格文档残留已删除的工具：\n  " + "\n  ".join(ghosts)
    )


def test_documented_tool_count_matches_code(store: Store, searcher: Searcher):
    """文档与 docstring 里写的工具数量必须等于代码里的真值。

    扫描面：README 两份 + `docs/**` 全部 markdown + `yacmemo/**` 全部 docstring。
    认得/不认得的写法见模块 docstring。
    """
    truth = len(real_tool_names(store, searcher))
    bad: list[str] = []
    for rel, lineno, line in count_sources():
        for label, pattern in COUNT_PATTERNS:
            for m in pattern.finditer(line):
                claimed = int(m.group(1))
                if claimed != truth:
                    bad.append(
                        f"{rel}:{lineno} 声称 {claimed} 个（{label}："
                        f"「{m.group(0).strip()}」），实际 {truth} 个"
                    )
    assert not bad, (
        f"文档工具计数与代码不符（真值 {truth} 个），共 {len(bad)} 处：\n  "
        + "\n  ".join(bad)
    )


def test_audit_result_keys_are_documented(store: Store):
    """新行为进了 `audit()` 的返回值、却没进规格 → 这里点名。

    规则与"为什么不设排除清单"见模块 docstring 的「第 6 类断言」：每个键都要
    在 `docs/02-mcp-tools.md` zh/en 里以行内代码出现。守卫的是**"行为"漂移**，
    不是数字漂移——`quarantined_index_rows` 就是数字没变、工具名没变、只有行为
    变了的一类漏网。
    """
    live = audit_result_keys(store)
    source = audit_result_keys_from_source()
    assert live == source, (
        "audit() 的运行时键与源码 `res = {...}` 字面量不一致——真值推导方式"
        f"要跟着实现改。\n  只在运行时：{sorted(live - source)}\n"
        f"  只在源码：{sorted(source - live)}"
    )

    missing: list[str] = []
    for rel in SPEC_FILES:
        spans = code_spans((REPO / rel).read_text(encoding="utf-8"))
        for key in sorted(live - spans):
            missing.append(f"{rel}: 审计结果键 `{key}` 没有任何一行规格（写成 `{key}`）")

    assert not missing, (
        f"audit() 的 {len(live)} 个结果键里，有 {len(missing)} 处没写进工具规格"
        "（新增行为必须同时回答：它是什么、谁看得见）：\n  "
        + "\n  ".join(missing)
    )
