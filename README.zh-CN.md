<div align="center">

# Context Git

**面向 AI Agent 的上下文版本控制系统。**

[English](README.md) · [简体中文](README.zh-CN.md)

把 Agent 的工作状态像代码一样进行快照、分支、合并、同步与定向交接。

快照 · 语义差异 · 上下文分支 · 安全同步 · Agent 交接 · 漂移检测

```text
Codex / Claude Code / Cursor / Gemini / OpenCode
                         ↓
        ctx_001 → ctx_002 → ctx_003
                         ↓
             Context Diff / Handoff
                         ↓
                    另一个 Agent
```

[![protocol](https://img.shields.io/badge/protocol-UACP%2F1.0-blue)](references/protocol.md)
[![tests](https://github.com/Sver0411/ContextGit/actions/workflows/tests.yml/badge.svg)](https://github.com/Sver0411/ContextGit/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.8%2B-informational)]()
[![deps](https://img.shields.io/badge/dependencies-std--lib%20only-green)]()
[![license](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

</div>

---

<a id="toc"></a>

## 目录

- [项目简介](#introduction)
- [核心能力](#features)
- [为什么不只写 HANDOFF.md](#comparison)
- [安装](#installation)
- [快速开始](#quick-start)
- [V5 Agent Context Network](#v5-network)
- [版本能力](#versions)
- [协议与安全](#protocol-security)
- [项目结构](#structure)
- [兼容性与限制](#compatibility)
- [开发与测试](#development)

<a id="introduction"></a>

## 项目简介

AI Agent 在会话结束、模型切换或设备迁移后，通常会丢失已经形成的工作
认知：当前目标是什么、哪些任务已经完成、为何作出某个决策、哪些验证
已经过期，以及下一步应该做什么。

Context Git 将这些“最小充分工作状态”保存为不可变的 **Context Object**，
并提供类似 Git 的版本历史、分支、语义合并和远端同步能力。它保存的是
压缩后的工作成果与证据，而不是聊天记录、思维过程或仓库副本。

项目实现开放协议 **UACP/1.0（Universal Agent Context Protocol）**，使用
纯 JSON/Markdown 和 Python 标准库，可在 Windows、macOS 与 Linux 上运行。

[返回目录](#toc)

<a id="features"></a>

## 核心能力

| Git 概念 | Context Git 对应能力 |
|---|---|
| commit | Context Commit：记录一次有意义的工作状态变化 |
| HEAD | Context HEAD：指向当前上下文 |
| diff | Semantic Context Diff：展示进度、决策和问题的语义变化 |
| log | Context Log：查看完整上下文历史 |
| checkout | Context Checkout：检查历史状态，不触碰源码 Git |
| branch | Context Branch：并行探索方案，不覆盖主上下文 |
| merge | Semantic Merge：基于共同祖先合并工作成果 |
| remote | Remote Context：跨设备安全同步上下文对象 |
| handoff | Agent Context Handoff：向指定 Agent 发送已发布上下文 |
| status | Drift Detection：判断上下文与当前代码是否已经漂移 |

Context Git 会区分两类信息：

- `observed`：由工具观测到的 Git 状态、文件指纹和运行环境。
- `agent`：由 Agent 给出的目标、决策、约束与进度描述。

接手者因此能知道哪些内容可以验证、哪些声明需要重新确认。

需要明确的是：**Context 是导航层，代码事实的最终依据始终是当前仓库本身**。
漂移等级为 `NONE` 只表示「Context 记录的观测证据在当前机器上仍然成立」，
并不意味着其中的 Agent 声明已经过验证——那些声明保留其记录时的置信度与来源。

[返回目录](#toc)

<a id="comparison"></a>

## 为什么不只写 HANDOFF.md

| 能力 | 一次性 HANDOFF.md | Context Git |
|---|---|---|
| 多次交接成本 | 每次重新生成全部内容 | 首次完整，后续增量提交 |
| 是否过期 | 无法判断 | NONE/LOW/MEDIUM/HIGH 漂移等级 |
| 验证结论 | 只能相信文字 | 代码变化后自动标记为 STALE |
| 历史记录 | 容易被覆盖 | 不可变 Context 历史 |
| 并行工作 | 依赖人工约定 | 独立上下文分支与三路语义合并 |
| 跨设备 | 手动复制 | 文件或 HTTPS 远端同步 |
| 跨 Agent | 自由格式文字 | 定向 Handoff、能力匹配和状态回执 |
| 敏感信息 | 依赖使用者谨慎 | 写入前脱敏与独立残留扫描 |

[返回目录](#toc)

<a id="installation"></a>

## 安装

要求：Python 3.8+，无第三方运行时依赖。

```bash
# 直接从仓库运行
python scripts/context_git.py --help

# 或安装为系统命令
pip install .
context-git --help
```

也可以从 [GitHub Releases](https://github.com/Sver0411/ContextGit/releases)
下载构建好的 wheel：

```bash
pip install context_git-5.0.0-py3-none-any.whl
```

[返回目录](#toc)

<a id="quick-start"></a>

## 快速开始

```bash
# 初始化上下文仓库
context-git init

# 保存第一个 Context
context-git snapshot --no-prompt \
  --set goal="交付认证模块" \
  --set current_objective="实现令牌轮换"

# 工作状态发生变化后进行增量提交
context-git commit --no-prompt \
  -m "认证中间件已完成" \
  --set completed="认证中间件" \
  --set in_progress="令牌轮换" \
  --set validation.test=pass

# 查看历史、语义差异和当前漂移
context-git log
context-git diff
context-git status

# 生成 Agent 可直接接手的恢复简报
context-git resume

# 完整性校验（Context ID / 父子血缘 / DAG 引用）+ 残留密钥扫描
context-git verify
```

命令可以在项目内**任意子目录**执行：不带 `--root` 时，项目根依次取
「最近的包含 `.context-git/` 的祖先目录」→「所在 Git 仓库根」→「当前目录」；
显式传入的 `--root PATH` 始终优先。因此从 `repo/src/auth/` 执行
`context-git init` 会写入 `repo/.context-git/`，而不会再嵌套一个 store。

`resume` 在生成简报前会先校验当前 Context 的完整性；对象与自身 id 不一致时
直接以退出码 7 拒绝，不会基于一个不可信的上下文产出简报。

### 上下文分支与合并

Context 分支与源码 Git 分支完全独立：

```bash
context-git switch -c auth-passkeys
context-git commit --no-prompt --set completed="Passkey 方案设计"
context-git switch main

# 先预览，再执行语义合并
context-git merge auth-passkeys --dry-run
context-git merge auth-passkeys
```

出现语义冲突时，命令会拒绝写入并给出稳定字段名。使用
`--resolve KEY=ours|theirs|base` 明确选择后再合并。

### 远端同步

```bash
# 文件远端必须位于项目目录之外
context-git remote add origin /Volumes/team/demo-context
context-git push origin main

# 另一台设备或另一个 checkout
context-git remote add origin /Volumes/team/demo-context
context-git pull origin main
context-git branch --all

# 查看 / 清理文件远端上遗留的写入锁
context-git remote show origin
context-git remote unlock origin
```

文件远端通过 `O_CREAT|O_EXCL` 创建 `.context-git.lock` 来串行化写入，锁文件里
记录了 pid / 主机名 / 操作 / 时间。写入进程被强杀或崩溃后锁会残留，所以
`remote show` 会报告它，`remote unlock` 负责清理——只有当该 pid 在本机确认已
不存在时才允许直接清理；其它情况（pid 仍存活、锁来自其它主机、锁内容损坏、
平台无法探测存活）都必须显式加 `--force`。锁不会因为「看起来过期」被自动删除，
也不会按时间自动清理。

HTTPS 认证只保存环境变量名，不保存令牌值：

```bash
export CONTEXT_GIT_TOKEN="由密钥管理器提供的值"
context-git remote add origin https://contexts.example.com/team/demo \
  --auth-env CONTEXT_GIT_TOKEN
context-git push
```

[查看完整远端协议](references/remote.md) · [返回目录](#toc)

<a id="v5-network"></a>

## V5 Agent Context Network

V5 在 V4 Context 远端上增加异步、定向的 Agent 交接网络。它不会启动
Agent，也不是聊天系统；它只协调经过验证的工作上下文。

### 1. 注册 Agent

```bash
context-git network register alice --name "Alice" --agent codex
context-git network agents
```

注册信息包含 Agent ID、显示名称、适配器和能力画像。已有 ID 默认不能
被其他本地身份接管；`--force` 仅用于明确的管理性接管。

### 2. 发送定向交接

```bash
context-git push origin main
context-git network send bob \
  --message "检查认证边界" \
  --expires-hours 24
```

Handoff 只包含发送者、接收者、Context 指针、简短意图、有效期、所需能力
及兼容性结果，不包含源码、完整 diff、聊天、Prompt、工具载荷或凭据。

### 3. 接收并反馈状态

```bash
context-git network inbox
context-git network accept hnd_0123456789abcdef --switch
context-git network reply hnd_0123456789abcdef accepted -m "开始检查"
context-git network reply hnd_0123456789abcdef completed -m "检查完成"
```

接受交接会创建 `handoff/SENDER/ID` 形式的本地 Context 分支，不会修改
源码文件、源码 Git 分支、索引或工作区。

发送者可以读取不可变回执：

```bash
context-git network status hnd_0123456789abcdef
```

状态流支持：

```text
pending ──> accepted ──> completed
   │            └──────> rejected
   ├───────────────────> completed
   └───────────────────> rejected
```

[查看完整 V5 网络协议](references/network.md) ·
[查看运行示例](examples/network-handoff.example.txt) · [返回目录](#toc)

<a id="versions"></a>

## 版本能力

- **V1 — Context Versioning**：快照、提交、语义差异、历史、漂移检测与恢复。
- **V2 — Richer Adapters**：私有会话显式导入与能力自动画像。
- **V3 — Context Branch/Merge**：独立分支、共同祖先和三路语义合并。
- **V4 — Remote Contexts**：文件/HTTPS 远端、完整性校验和并发保护。
- **V5 — Agent Context Network**：Agent 注册、定向 Handoff、Inbox 与状态回执。
- **5.0.1 — 核心正确性与安全修复**：项目根发现、敏感路径大小写、索引状态漂移、
  文件指纹参与「有意义的变更」判定、本地完整性校验、特殊文件名、残留锁恢复、
  16 位 Context ID（兼容旧的 8 位）。

完整变化记录见 [CHANGELOG.md](CHANGELOG.md)。

[返回目录](#toc)

<a id="protocol-security"></a>

## 协议与安全

Context Object 使用 **UACP/1.0**：

- [UACP 协议说明](references/protocol.md)
- [Context Object Schema](schemas/uacp-1.0.schema.json)
- [字段参考](references/schema.md)
- [分支与语义合并](references/branching.md)
- [远端同步协议](references/remote.md)
- [Agent Context Network 协议](references/network.md)
- [安全模型与边界](references/security.md)
- [Agent 适配器](references/adapters.md)

主要安全规则：

1. `.env*`、私钥、SSH/AWS/Kubernetes 凭据目录等敏感路径不会被读取。
   路径匹配在**所有平台**统一做大小写与结尾点/空格归一化，因此 `.ENV`、
   `.SSH/config`、`ID_RSA`、`SECRET.PEM`、`TOKEN.BAK` 与对应小写写法同样被拒绝；
   工具自身的 store 目录（`.GIT/`、`.CONTEXT-GIT/`，含嵌套位置）任意路径段命中即剔除。
2. 常见 API Key、JWT、Bearer Header、密码赋值在写入前被脱敏。
3. 独立残留扫描失败时拒绝写入；可随时运行 `context-git verify`。
4. 不保存聊天历史、系统/开发者指令、思维过程、工具调用或完整 diff。
5. 远端对象使用 Context ID、完整 SHA-256、字节大小和父级闭包校验。
6. HTTPS 禁止凭据内嵌 URL 和跨域重定向；并使用 ETag 防止丢失更新。
7. 本地存储同样受完整性保护：`verify` 会用对象内容重算 Context ID、校验
   首父指针与血缘结构、报告悬空的父对象；`resume` 在完整性不通过时以退出码 7
   拒绝出简报，`status` 会标出 `Integrity: FAILED`。本地 Context 的可信度不低于
   远端拉取的对象。

Agent ID 是某个远端命名空间内的名称，不是密码学身份。V5 未提供
Agent 独立签名；发布者真实性依赖 TLS、作用域凭据与服务端访问控制。

[返回目录](#toc)

<a id="structure"></a>

## 项目结构

```text
ContextGit/
├── context_git/
│   ├── cli.py              # 命令行入口
│   ├── context.py          # Context Object 构建与 ID
│   ├── storage.py          # 本地对象、HEAD 与 refs
│   ├── diff.py             # 语义差异
│   ├── merge.py            # 三路语义合并
│   ├── remote.py           # 文件/HTTPS 远端同步
│   ├── network.py          # Agent、Handoff 与 Receipt
│   ├── drift.py            # 漂移与验证新鲜度
│   ├── security.py         # 脱敏和残留扫描
│   ├── sessions.py         # 显式私有会话导入
│   └── adapters/           # Agent 适配器
├── schemas/                # 规范 JSON Schema
├── references/             # 协议与安全文档
├── examples/               # 可运行流程示例
└── tests/                  # 标准库 unittest 测试
```

[返回目录](#toc)

<a id="compatibility"></a>

## 兼容性与限制

已内置 Codex、Claude Code、OpenCode、Cursor、Gemini CLI 和通用 Agent
适配器。所有 Context 都是普通 JSON/Markdown，其他工具也可以直接消费。

当前限制：

- 语义合并是确定性的结构合并，不使用 LLM 自动猜测冲突意图。
- 单个本地 `.context-git/` 默认假设同一时间只有一个写入者。
- 远端暂不支持删除、裁剪、浅拉取、对象签名和端到端加密。
- Agent Network 不提供在线状态、实时聊天、任务调度或自动执行 Agent。
- 会话导入依赖第三方工具的私有格式，升级后应先使用 `--dry-run` 预览。
- 验证结果由 Agent 记录，工具不会自行运行项目测试；漂移系统负责标记过期。
- 旧的 8 位十六进制 Context ID 仍然可读可校验，但强度只有 32 位；5.0.1 之前
  写入的对象不会被就地改写，需要重新 snapshot 才会得到新的 16 位 ID。本地完整性
  校验防的是损坏与随意篡改，不是能同时改写对象内容与所声明 ID 代次的攻击者。
- 文件远端的写入锁永远不会被自动清理，即使看起来已经过期；崩溃的写入者需要
  显式执行一次 `remote unlock`。这是为了避免误删仍有效的锁而做的取舍。
- 漂移等级只由 Git 与文件证据计算。仅索引状态变化（内容未变的 `git add`）
  记为 LOW，且不会让已记录的验证结果过期。

[返回目录](#toc)

<a id="development"></a>

## 开发与测试

```bash
python -m unittest discover -s tests -v
```

当前版本：**V5.0.0**。项目采用 [MIT License](LICENSE)。

- [GitHub 仓库](https://github.com/Sver0411/ContextGit)
- [版本发布](https://github.com/Sver0411/ContextGit/releases)
- [问题反馈](https://github.com/Sver0411/ContextGit/issues)

[返回目录](#toc)
