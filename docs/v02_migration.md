# 全面切换 v0.2

## 决策与范围

用户确认全面切换，不保留旧模型公共 API 或旧 CLI 兼容入口。
源码、测试、结果、原指纹保存到 `../../archive/romp-asr/`，
不作为当前实现证据。63 个确认无依赖的一次性中间文件已删除。

保留运行模块：
`policy_v02.py`、`policy_tools.py`、`tool_backends.py`、新 `cli.py`。
公共包不再加载旧 LLM/baseline 链或修改 telemetry 环境。
旧专用执行器中的纯后端已提取；政策判断仅在通用引擎中执行。

破坏性变化：旧 Runtime/ActionProposal 与 ToolPolicy 不再导出；
bench、verify、perf、dsl 及旧 flags 返回参数错误。
新 check/eval 仅编译与纯求值；不能通过 CLI 的 Context 自报状态
获得真实工具权限。工具接入仍由可信宿主使用 PolicyExecutor。

## 数据与状态

不自动把旧账本转换成新账本，不自动重置实际预算。部署迁移必须停用旧
执行路径、核对已成功/未决请求，并由操作者安排初始占用；
当前未实现生产状态转换工具。归档数据文件没有销毁。
已有 v0.2 策略绑定与去重协议保持，策略变更不能静默复用账本。

## 迁移回归

旧测试原文归档，不删失败测试掩盖行为变化。新测试覆盖：
旧入口不可用、无研究依赖导入、无 telemetry 环境副作用、
JSON CLI、重复 Context 键拒绝、演示退出与连接释放。
有价值的并发、金额、路径、重复删除和未知结果场景转到通用接入测试。

Windows 演示暴露连接生命周期问题：SQLite 的 connection 上下文管理器
只提交/回滚，不会关闭连接。现用 finally 显式关闭账本与执行器连接，
并用子进程演示测试验证临时目录可删除。

H-DSL、WASM、银行连接器、完整拒绝审计和恶意共享目录防护尚未实现。

## 最终验收

- 清理前全量 Windows 回归：258 passed、18 skipped、1 条旧依赖警告。
- 切换后 Windows：61 passed、13 POSIX skipped，无 warning。
- 切换后 WSL：74 passed，无 warning；真实文件保留/删除演示成功。
- editable 安装成功，`romp-agentos --version` 为 0.2.0。
- 新 check 示例输出策略哈希，旧入口测试明确拒绝。
- 论文四 pass 均 RC=0，四页，引用/BibTeX warning/overfull 均为 0。

最终论文预览已生成，但本轮最后一次图片上传因网络错误失败；
不能把构建诊断等同于本轮最终稿的完整视觉验收。
第一阶段归档了 29 个历史稿图文文件，第二阶段再归档旧源码、测试、
示例和实验目录；归档中的字节码/相对路径不构成可部署历史发布包。

## 针对性证据补强

新增 5 项检查，当前 WSL 79 passed，Windows 65 passed、14 skipped：
非幂等后端成功/超时两种情况下，四个并发提交和重启重试分别断言
调用次数=1、效果条数=1；真实子进程在提交效果后 os._exit(17)，
恢复后保留 PENDING 且不重调；SQLite 配置检查与 WAL 拒绝；
移除保护硬规则后临时文件被删除的职责对照（POSIX）。

SQLite 显式要求 DELETE journal、synchronous=FULL，拒绝不兼容数据库。
仅测试进程退出，不据此声称掉电耐久或存储损坏恢复。
charge_adapter 的 binding 是宿主声明的语义版本，不是后端代码/配置指纹；
改变实际语义必须更新 binding，现阶段不能自动识别恶意或错误宿主变更。

论文增加条件性预算不变量、规则允许示例、soft 平局精确定义、
决策/状态分离及来源支撑的职责比较。AgentSpec NR 表示 inspected
description 未报告，非证明其不支持；未运行 Cedar/AgentSpec 比较实验。
公开工具、视频与作者 TBD 未猜填。历史参考文献原文保存于
`../../archive/paper/en/paper.bib`；当前 BibTeX 只保留使用的来源。
