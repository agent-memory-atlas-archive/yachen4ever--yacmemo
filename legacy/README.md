# legacy/

本目录是 yacmemo v1（三层架构：extractor / enhancer / 一致性 LLM / WebUI）的冻结存档。

- **不属于构建与测试范围**：pytest 只收集 `tests/`；这些文件不再被任何运行路径导入。
- **为什么保留**：决策过程记录。设计演进与否决理由见 `docs/06-lean-architecture.md`。
- **不要在这里继续开发**。新架构代码在 `yacmemo/` 与 `tests/`。

| 目录 | 内容 |
|---|---|
| `v1/` | extractor / enhancer / consistency / db / llm / 旧 config、vector、mcp_server、webui |
| `v1_tests/` | v1 的测试套件（依赖已移除，仅供阅读） |
| `frontend/` | v1 WebUI 的 Vue 前端 |
