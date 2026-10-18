﻿﻿﻿﻿﻿﻿﻿﻿﻿﻿﻿﻿﻿﻿﻿﻿﻿﻿# ROMP：通用安全 DSL 与受控工具执行

当前版本 `romp-mu++/0.3`，Python 3.11+。JSON 路径无运行时依赖；
YAML 是可选载体，H-DSL 与 WASM 尚未实现。

## Docker：免构建评审路径

评审者使用已发布到 GHCR 的镜像，无需自行构建代码。发布者须将镜像包
设为 Public，并确认未登录用户也能拉取：

```bash
docker pull ghcr.io/tide999/romp-asr:0.3.0
docker run --rm ghcr.io/tide999/romp-asr:0.3.0              # 用法
docker run --rm ghcr.io/tide999/romp-asr:0.3.0 pytest -q    # 144 项检查
docker run --rm ghcr.io/tide999/romp-asr:0.3.0 policy-demo
docker run --rm ghcr.io/tide999/romp-asr:0.3.0 agent-demo
```

两个 demo 均离线运行，不需要 API 密钥；演示使用本地支付账本和临时文件。
预期结果包括预算超限拒绝、多维效果预留、UNKNOWN 保留占用，
以及 agent-demo 中普通文件被删除、受保护文件被保留。
Windows 用户可在配置好 Docker 服务的 WSL 中执行以上命令。

镜像固定 `python:3.12-slim-bookworm` + `pytest==9.1.1` + `PyYAML==6.0.3`，
与论文所述 WSL 环境一致，容器内实测 144 passed。依赖版本可由
`Dockerfile` 顶部改写，但改动后请同步论文中的环境陈述。

Linux 容器天然满足文件后端所需的 POSIX `dir_fd` unlink，因此在 Windows 上
评审也能看到删除场景，而不必像源码路径那样静默少两行结果。

镜像不含任何凭证：`.dockerignore` 显式排除 `.env`（构建上下文不受 gitignore
保护），并且不打包验收作用域 `state/` 与生成的 fixture 目录，因此镜像里不含
已消耗的预算。

真实模型验收（第 4.3 节）需要评审者自己的密钥，且会消耗托管模型额度：

```bash
mkdir -p out && cp .env out/
docker run --rm -v "$PWD/out:/work" ghcr.io/tide999/romp-asr:0.3.0 acceptance
```

`accept.py` 是一次性harness，检测到已有证据或作用域会拒绝运行，因此入口脚本
先把 harness 复制到可写目录并清空既有证据；产物写回 `out/`。请勿删除失败样本。

### 维护者构建与发布

以下命令仅供维护者使用，在源码仓库根目录的 Bash / WSL 中执行。
评审者直接使用上面的免构建入口：

```bash
docker build --platform linux/amd64 -t ghcr.io/tide999/romp-asr:0.3.0 .
docker login ghcr.io -u tide999
docker push ghcr.io/tide999/romp-asr:0.3.0
```

登录时输入有 write:packages 权限的 GitHub classic Token，不是账号密码。
已用于投稿的版本标签不应覆盖；后续变更使用新标签。
推送成功不代表公开：在 GitHub 的 Packages / romp-asr / Package settings
中将可见性设置为 Public，再从未登录环境验证拉取和运行。

## 安装与演示

```bash
python -m pip install -e .
python -m pip install ".[dev,yaml]"
python -m romp_asr --version
PYTHONPATH=src python examples/policy_v03_walkthrough.py
```

Windows PowerShell 可用 `$env:PYTHONPATH="src"` 后运行示例。
扣款后端为持久本地支付账本；真实临时文件删除仅在 POSIX 下演示。

## 编译与纯求值

```bash
python -m romp_asr check examples/policy_v02.json
python -m romp_asr eval examples/policy_v02.json context.json
```

`context.json` 包含 operation、amount_minor、candidate_total_minor、
target_protected，必须由可信宿主构造。CLI 仅编译和求值，不执行工具。
退出码：0=编译成功或 ALLOW；1=REJECT/ESCALATE；2=参数/编译/输入文件错误。
JSON 输出中的 decision 才是实际决策，不以进程成功猜测业务动作。

公共 API：v0.2 规则层为 compile_policy、evaluate、PolicyExecutor、
OperationAdapter、charge_adapter、delete_adapter；v0.3 效果层为
compile_state_policy、admit、StateEffectExecutor、StateEffectAdapter，
适配器由宿主或示例自行定义。
硬拒绝不能被软权重覆盖；无效 Context 先整体拒绝。
准入事务预留累计额度，重试不重复调用，UNKNOWN/PENDING 保留占用。

只有已准入 ID 才持久绑定参数；REJECT/ESCALATE 不占用 ID，可重新求值。
FAILED 的同 ID 重试返回原失败，新尝试须用新 ID。
规则编辑应在数据库初始化前完成；已有策略变更需要显式迁移，
当前不提供迁移/未知结果对账 API。新数据库属于新预算作用域，
不能替换旧库以恢复已经消耗的额度。
宿主必须保证一次调用整个生命周期的累计毛扣款不超过预留。

## 测试与边界

```bash
python -m pytest tests -q
```

当前正式测试：WSL 144 passed；Windows 127 passed、17 项 POSIX skipped。
v0.3 扣款演示预期：作用域 A 账本为 60、60、60、100，作用域 B 由
charge_count 单独拒绝，作用域 C 的 UNKNOWN 保留整份效果向量；
POSIX 下 system/core 保留，workspace/draft 删除。
SQLite 要求 DELETE journal、synchronous=FULL；测试覆盖所选进程退出窗口，
不据此保证掉电或存储损坏恢复。

可信宿主负责认证、事实提供、数据库保管及工具权限隔离。
文件后端要求宿主独占目录命名空间，不保证恶意并发 rename 下目标身份。
不是银行连接器、OS 沙箱或绝对业务正确性证明。

## 正式参考 Agent

安装后使用 `romp-agent`，不是固定实验 episode 入口：

```bash
romp-agent run --config examples/reference_agent.json \
  --task "Process invoice-001, then invoice-002. Report denied operations."
romp-agent status --config examples/reference_agent.json
```

先在宿主环境设置 `ROMP_API_MODEL_KEY`，并按实际服务修改配置的模型和
HTTPS Chat Completions 地址。配置不保存密钥。订单金额、稳定业务 ID、
策略和状态目录都由宿主配置；模型只能提交已注册资源别名。
可选文件目标使用已有目录，仅支持 POSIX，不自动创建临时 fixture。

模型提案经 StateEffectExecutor 按 v0.3 效果声明准入后才执行，结果反馈模型继续任务。
请求与本地账本跨任务、重启保留；`status` 显示账本合计、多维 held 与准入决策。
`agent-trace.jsonl` 记录任务、工具反馈和最终回答，可能含私密内容，
不是事务账本。终止为 completed 仅表示模型给出最终回答，不代表任务成功。
不提供任意代码执行、OS 沙箱、UNKNOWN 自动对账或生产银行接入。

配置、需求、架构决策与验收映射见 `docs/reference_agent.md`。
新增参考 Agent 测试使用脚本模型，与真实模型实验结果分开报告。

## 迁移

旧模型 Runtime/ActionProposal、controlled-tools/1 专用入口、
bench/eval 聚合/verify/perf CLI、LLM/baseline 实验链退出公共运行路径。
开发工作区保留历史源码、测试、实验数据与指纹；独立公开源码包不包含该归档。
旧数据库不得当作新数据库直接使用，不得通过切换版本清空实际已占用额度。
历史合同不再描述当前版本；不承诺旧命令兼容。

当前效果记账规范：`docs/v03_state_effect_spec.md`。
规则层迁移说明：`docs/v02_migration.md`。
