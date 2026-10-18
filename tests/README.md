# 当前正式功能测试

| 标记 | 内容 |
| --- | --- |
| `dsl_current` | v0.2 通用编译器与解释器 |
| `runtime_support` | 从历史保障迁移来的后端、并发、路径与重试用例 |
| `cli_current` | v0.2 CLI、公共 API 隔离、旧入口明确拒绝和演示退出 |
| `agent_current` | 正式参考 Agent 配置、模型工具闭环、持久状态和实际副作用 |

```bash
python -m pytest tests -q
python -m pytest tests -m dsl_current -q
python -m pytest tests -m runtime_support -q
python -m pytest tests -m cli_current -q
python -m pytest tests -m agent_current -q
```

`test_policy_v03.py` 覆盖 v0.3 效果声明、不变量与状态绑定，尚未归入上述标记。

当前正式测试共 144 项：WSL 144 passed；Windows 127 passed、17 项 POSIX skipped。
参考 Agent 的脚本模型测试与真实模型实验分开报告，不是实际 LLM 测量。

原旧模型、CLI、baseline 测试原文归档在 `../../archive/romp-asr/tests/`，
不参与当前版本默认测试，也不以新测试数量伪装成历史契约仍受支持。
历史数据与冻结核心均保存；后端仍有价值的场景已迁移至当前测试。
