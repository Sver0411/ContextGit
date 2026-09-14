# ContextGit 5.0.1 修复报告

**范围**：V1–V5 的核心正确性、可靠性、安全性、语义一致性修复。
**未做**：不新增 V6、不新增 UI/数据库/LLM 合并/Agent 自动执行、不重设计 UACP、不破坏兼容性、不删除既有能力。

**工作目录**：`/Users/mac/Documents/ChatGPT/skill`（即 `github.com/Sver0411/ContextGit`）

---

## 1. 修复的问题

| # | 级别 | 问题 |
|---|---|---|
| 1 | P0 | 敏感路径检测区分大小写，`.ENV` / `.SSH/config` / `ID_RSA` / `SECRET.PEM` / `TOKEN.BAK` 可绕过 |
| 2 | P0 | 未传 `--root` 时项目根固定为 `cwd`，在子目录运行会找错 store，甚至嵌套出第二个 store |
| 3 | P1 | 漂移检测只比较 working tree 的**路径集合**，`git add`（unstaged → staged）完全漏检 |
| 4 | P1 | `core_view()` 的 `important_files` 只含 `path`/`why`，文件内容变了但 Git 状态/统计不变时误报 `nothing to commit` |
| 5 | P1 | 本地 `load_context()` 几乎不做完整性校验，手改 `.context-git/contexts/*.json` 后仍被信任 |
| 6 | P2 | `_numstat()` 依赖普通 `splitlines()`/`split("\t")`，tab、换行、rename 等特殊文件名解析错误 |
| 7 | P2 | File Remote 的 `.context-git.lock` 被强杀后永久残留，之后所有 push 永久失败且无恢复手段 |
| 8 | P2 | Context ID 仅 8 hex（32 bit），在分支/合并/远端/网络长期 DAG 下碰撞风险不值得承担 |
| 9 | P1 | 文档语义与自身 Evidence Model 冲突：SKILL 写 "Trust the briefing, not the repo." |
| 10 | P2 | 嵌套 store 的路径未被剔除（`_strip_own_store` 只检查首段），工具自身文件泄漏进上下文（**修复 #2 后才可达**） |
| 11 | P2 | `CTX_ID_RE` 为 `{8,16}`，会接受任何写入者都不会产生的宽度（9–15 hex） |

> 补充：修复过程中自己引入并修掉一个真实缺陷 —— Context ID 正则交替分支顺序导致非锚定 `search` 返回 16 位 id 的前 8 位前缀（见 §7）。

---

## 2. 每个问题的根因

**#1 敏感路径大小写**
`is_forbidden_path()` 直接用**原始大小写**做 `in` / `startswith` / `endswith` 比较，目录标记比较用 `set(parts) & FORBIDDEN_DIR_MARKERS`。安全规则事实上只在「文件名恰好是小写」时生效，一旦同一份仓库在大小写不敏感文件系统（macOS 默认、Windows）上被读取即失效。

**#2 项目根发现**
`cli.main()` 只有 `root = Path(getattr(args, "root", ".")).resolve()`，没有任何向上查找。README/SKILL 却承诺 "Run from any directory inside the project"。

**#3 漂移漏检索引变化**
`drift.check()` 第 4 段取 `staged ∪ unstaged ∪ untracked` 的**并集**再求对称差。`git add` 让文件从 `unstaged` 移到 `staged`，并集完全不变 → delta 为空 → 无任何 signal。Git 的索引状态确实变了，但路径集合看不见。

**#4 meaningful change 漏判**
`core_view()` 把 `important_files` 归一化为 `{path, why}`。而 `git.diff_stat` 只记录 `files_changed/additions/deletions`。于是 `x = 2` → `x = 3` 这类改动：路径相同、状态相同、`+1/-1` 相同、`why` 相同 → `is_meaningful_change()` 为 False → `commit` 报 "nothing to commit"。

**#5 本地完整性**
`Store.load_context()` 只做 `read_json()` + `context_id == 文件名`。Context ID 本就包含 `goal`/`progress`/`decisions`/`git.head` 的哈希，但**没有任何路径去重算它**，所以本地的不可变性只是声明。Remote 有完整校验，本地反而更弱 —— 与「本地不应比远端更不可信」相反。

**#6 numstat**
`for line in _lines(out)` + `line.split("\t")`。`-z` 的 NUL 定界形式本来就是为了消除这种歧义而存在的，却没有使用；rename 记录还会以 `old => new` 拼接形式落进 key。

**#7 残留锁**
`FileTransport.write()` / `write_network()` 内联 `os.open(..., O_CREAT|O_EXCL)`，锁体只写 `"{pid} {iso}"`。`finally` 只在正常/异常返回时删除；`kill -9`、崩溃、断电都不经过 `finally`，锁永久残留，且没有任何命令能查它或清它 —— 只从 `RemoteError("remote is locked by another writer; retry later")` 无从判断持有者是否还活着。

**#8 ID 宽度**
`compute_context_id()` 返回 `"ctx_" + h.hexdigest()[:8]`。32 bit 在单机短链路上够用，但 V3 引入 DAG、V4 引入跨机器同步、V5 引入长期 handoff 之后不再合适。

**#9 文档语义**
SKILL.md 的 Resume workflow 第 1 步要求 Agent **"Trust the briefing, not the repo."**，`drift.py` 的 NONE 摘要写 "Context can be trusted as-is."，`format_report()` 的结论行写 "context trustworthy"。而项目自己在 protocol.md 第 3 节定义的 Evidence Model 明确把 `observed`（机器观测）与 `agent`（Agent 声称）分开。把 drift NONE 描述成「context 是 truth」直接违背了这套模型。

**#10 嵌套 store 泄漏**
`_strip_own_store().keep()` 用 `path.startswith(".context-git/")` 判断，只看**首段**。store 位于项目根时成立；一旦 store 位于 `packages/app/.context-git/`（修复 #2 之后成为合法用法），`.context-git` 不再是首段，于是 `HANDOFF.md`、`contexts/*.json`、`refs/*` 全部进入 `untracked`/`changed_files`，`diff_stat` 随之虚高，并立刻产生自指漂移。

**#11 ID 正则过宽**
`^ctx_[0-9a-f]{8,16}$` 是区间而非枚举。没有任何写入者会产生 9–15 hex，接受它们等于放宽入口。

---

## 3. 修改的文件

**源码（11 个）**

| 文件 | 改动 |
|---|---|
| `context_git/common.py` | `normalise_path_component()`（casefold + 去尾部点/空格）新增；`is_forbidden_path()` 全部走归一化；`is_ignored_dir()` 归一化；备份文件标记词表扩展 |
| `context_git/gitstate.py` | `parse_numstat_z()` 新增（NUL 解析，纯函数便于直接测试）；`_numstat()` 改用 `--numstat -z`；调用点补 `-z`；`_strip_own_store()` 改为逐段检查 |
| `context_git/context.py` | `ID_HASH_VERSION` / `ID_HASH_HEX_LEN` / `CORE_VIEW_VERSION` / `LEGACY_CORE_VIEW_VERSION` 常量；`core_view(obj, version=None)` 支持两种视图；`_id_hex_len()` 新增；`compute_context_id()` / `verify_context_id()` 按声明代次截断；`is_meaningful_change()` 两侧统一按 v2 比较；payload 新增 `core_view_version`、`id_hash_version` |
| `context_git/storage.py` | `CTX_ID_BODY` / `CTX_ID_SEARCH_RE` 新增且收紧为 exactly 8 或 16；`context_problems()`、`verify_graph()` 新增；`load_context()` 契约显式化 |
| `context_git/drift.py` | `_WORKING_STATE_KEYS` / `_working_states()` 新增；第 4 段改为比较每路径状态集，新增 `index-state-changed` signal 与 `counts.index_state_changed`；validation 新鲜度与索引漂移解耦；NONE 摘要与结论行文案改写 |
| `context_git/remote.py` | `_pid_alive()` / `_age_seconds()` / `read_lock()` / `lock_status()` / `describe_lock()` / `FileRemoteLock` / `unlock()` 新增；`FileTransport.write()` 与 `write_network()` 改用上下文管理器 |
| `context_git/cli.py` | `resolve_project_root()` 新增并接入 `main()`；`cmd_status()` 增加完整性检查；`cmd_resume()` 完整性 fail-closed；`cmd_verify()` 重写为完整性 + 密钥双检；`cmd_remote_unlock()` 新增；`cmd_remote_show()` 显示锁；`remote unlock` 子命令注册 |
| `context_git/render.py` | "Before you trust the context:" → "Before you act on the context:" |
| `context_git/__init__.py` | `__version__` → `5.0.1` |
| `pyproject.toml` | version → `5.0.1`；classifier → `Development Status :: 4 - Beta` |
| `SKILL.md` | 新增 Project root 章节；Resume workflow 语义改写；V4 增加锁恢复段落；命令表补 `verify` / `remote unlock` |

**文档与 Schema（7 个）**
`CHANGELOG.md`（新增 5.0.1 段）、`README.md`、`README.zh-CN.md`、`references/protocol.md`（ID 代次 + 漂移语义）、`references/schema.md`（新增两个字段）、`references/remote.md`（锁契约）、`references/security.md`（威胁模型 + 大小写规则 + 本地完整性）、`schemas/uacp-1.0.schema.json`、`schemas/uacp-remote-1.0.schema.json`、`schemas/uacp-handoff-1.0.schema.json`

**测试（3 个）**
`tests/test_v5_1_regressions.py`（新增，68 个用例 / 10 个测试类）、`tests/test_storage_context.py`、`tests/test_v4_remote.py`

合计 22 个已跟踪文件改动 + 1 个新测试文件，1051 行新增 / 137 行删除。

---

## 4. 新增的 Regression Tests

新增 `tests/test_v5_1_regressions.py`，**按缺陷编号组织**（T1…T1112），每个用例在 5.0.0 下失败、在 5.0.1 下通过。

| 编号 | 覆盖内容 |
|---|---|
| 1 | 25 种大小写变体（含 `.ENV.` / `.ENV ` 尾点空格形式、`.SSH/`、`nested/.GnuPG/secring.gpg`、`Store.JKS`）；Git state / important files / fingerprints 三层不泄漏；`.GIT/`、`.CONTEXT-GIT/`、`packages/app/.context-git/` 逐段剔除 |
| 2 | `repo`/`repo/src/a/b` 下 `status`/`log`/`snapshot` 解析到同一根；子目录 `init` 落在仓库根且不产生嵌套 store；最近 store 胜出；无 git 无 store 时回落 cwd |
| 3 | `--root` 在子命令前/后均优先于 store 发现；`init`/`snapshot` 经 `--root` 落到目标 store |
| 4 / 5 | `unstaged → staged`、`staged → unstaged`、`untracked → staged` 均报 `index-state-changed`(LOW) 且**不**报 `working-tree-changed`；`staged → committed` 走 head-moved |
| 6 | 同为 `modified`、同 `+1/-1`、同路径，仅内容不同 → `is_meaningful_change` 为 True；`commit` 不再误报 no-op；真正的 no-op 仍被拒；两种 core view 形状差异被固定 |
| 7 | 篡改 `goal` / `current_objective` / `progress` / `decisions` / `git.head` / `important_files[].fingerprint` 六处，`verify` 均以 exit 7 报 `Context ID does not match object contents`；`resume` 拒绝且不输出简报；非 HEAD 对象被篡改不阻塞 HEAD 的 resume；`load` 保持宽容而 `verify` 是关卡 |
| 8 | 删除父对象后 `verify` 报 `parent context is missing`；干净 DAG 通过 |
| 9 | 空格 / Unicode / tab / 换行文件名、rename（按新路径为 key）、rename+改内容、binary；畸形与截断的 numstat 流被丢弃而不抛错；git 拒绝时降级为空 |
| 10 | 无锁 / 死 pid 免 `--force` / 活 pid 需 `--force` / 畸形体需 `--force` / 旧 `pid iso` 体仍能识别 / 异主机需 `--force` / 老时间戳+活 pid 不按时间清理 / `remote show` 报告持有者 / push 与 network 写入被锁阻断并给出修复指引 / HTTPS 远端拒绝 unlock / 成功与异常路径都释放锁 / 获取失败不误删他人锁 |
| 11 / 12 | 新建 id 为 16 hex 且 `id_hash_version`=2；真·旧对象（无三个新字段、8 hex、标量父）仍 verify 且通过 remote 往返（push → fetch → verify）；16 位 id 在 HEAD 中完整往返；分离 HEAD 的 checkout 会刷新 HANDOFF；宽度 0/4/7/9/12/15/17/20/32 被拒；`save_context` 拒绝异常宽度 |
| 13 | 纯索引漂移下 validation 保持 `FRESH`；内容变化仍使其 `STALE` |
| 14 | 制造 merge 冲突后 `conflicted` 状态变化被上报 |

**关于 §13「测试是否按 bug 写」的复查结论**
复查了 `test_drift.py`、`test_gitstate.py`、`test_security.py`、`test_storage_context.py`、`test_cli_acceptance.py`、`test_v3_branch_merge.py`、`test_v4_remote.py`、`test_v5_network.py`：

- **未发现**「实现有 bug + 测试按 bug 写」的案例。`test_gitstate.py` 的 `test_own_store_filtered` / `test_sensitive_names_filtered`、`test_drift.py` 的 `test_untracked_only_low` 等断言方向都正确。
- **修改 2 个既有测试的期望值**（属于设计变更后的必要更新，非掩盖 bug）：
  - `test_storage_context.py::test_protocol_fields`：断言 `^ctx_[0-9a-f]{8}$` → `{16}`，并补断言两个代次字段。
  - `test_v4_remote.py::test_v1_v2_scalar_parent_identity_is_still_verified`：构造「真·旧对象」时补 pop `core_view_version` / `id_hash_version`，否则它模拟的是「一个 5.0.1 对象戴旧 id」，那本就应当被拒绝。
- **3 个由我自己引入的回归**（`test_v3_branch_merge` × 2、`test_cli_acceptance` × 1）由**修复产品代码**解决，**没有**修改这些测试的期望值（详见 §7）。

---

## 5. 向后兼容情况

| 维度 | 结论 |
|---|---|
| 旧 Context ID（8 hex） | 完全兼容。**读取、校验、分支/HEAD 解析、remote push/fetch、network handoff** 全部继续工作；新正则 `^ctx_(?:[0-9a-f]{16}|[0-9a-f]{8})(?![0-9a-f])$` 精确接受两种宽度 |
| 旧 `core_view` 形状 | 完全兼容。identity 仍固定在 `core_view_version` 1（无指纹），新对象显式声明 2，校验按对象自身声明的代次重算 |
| 旧 `parent_context_id`（V1/V2 标量父） | 兼容，`verify_context_id()` 的标量分支保留 |
| 旧 `parent_context_ids`（V3+ 数组） | 兼容，分支未改 |
| JSON Schema | `additionalProperties: true`，新增 `core_view_version` / `id_hash_version` 不破坏旧读者；schema 已同步这两个字段及其枚举 |
| Remote manifest / network / handoff / receipt 格式 | 未改动版本号与结构；`remote:NAME/BRANCH`、hex digest、size 校验逻辑不变 |
| 磁盘布局 `.context-git/` | 未变（HEAD / refs / contexts / config / HANDOFF.md 位置与格式一致） |
| 退出码 | 0/1/2/3/4/5/6 语义未变；**新增 7** 表示完整性校验失败（`verify` / `resume` / `status`） |
| CLI 习惯 | 仅新增 `remote unlock` 子命令与 2 个输出行（`status` 的 `Integrity:`、`remote show` 的 `Writer lock:`），无参数语义变更 |
| 已验证的旧对象 | 仓库内 `examples/context-object.example.json` 用 5.0.0 与 5.0.1 两份代码校验结果**相同**（都为 False，见 §12） |

**唯一无法"自动升级"的部分**：5.0.0 写出的 8 hex 对象保持 8 hex，不会就地改写。要获得 16 hex 需重新 snapshot。这是有意的 —— 就地改写会破坏不可变性。

---

## 6. Security Changes

1. **路径归一化统一**：新增 `normalise_path_component()`（`casefold()` + `rstrip(" .")`），`is_forbidden_path()` / `is_ignored_dir()` / `_strip_own_store()` 共用。**所有平台执行同一套规则**，不做 Windows 特判 —— 恰恰因为大小写敏感检查在仓库被搬到大小写不敏感文件系统时会静默失效。`rstrip(" .")` 覆盖 Windows 把 `.env.` 与 `.env` 视为同一文件的语义，在 POSIX 上只是少见的文件名。
2. **判定覆盖**：basename、suffix、path parts、目录标记、文件名、临时备份规则全部走归一化。备份后缀扩展为 `.bak/.old/.orig/.copy/.swp/.swo/.tmp/.temp/.backup/.save/~`，标记词扩展为 `.env/secret/credential/password/passwd/token/apikey/api_key/id_rsa/id_dsa/id_ecdsa/id_ed25519/privatekey/private_key`。刻意**不**加入裸 `key`，避免把 `monkey.bak` 误判 —— 真密钥文件已由 `.key` 后缀规则覆盖。
3. **工具自身 store 逐段剔除**：任意路径段为 `.git` / `.context-git`（归一化后）即剔除，修复嵌套 store 的元数据泄漏（#10）。
4. **本地完整性 = 远端强度**：`context_problems()` 从对象内容重算 Context ID（覆盖 `goal`、`current_objective`、`progress`、`architecture`、`decisions`、`constraints`、`do_not_change`、`known_issues`、`important_files`、`git.head`、`git.branch`、`git.working_tree`、`git.diff_stat`、`validation`、`recommended_actions`），并检查首父指针与血缘结构；`verify_graph()` 追加悬空父检测。`verify` 在篡改对象上 exit 7；`resume` 在产出任何简报之前 exit 7，因此不存在"基于不可信对象生成可信简报"的窗口。
5. **远端拒绝被篡改对象**：`_reachable()` → `validate_context_object()` 的既有校验保持不变，现在也覆盖指纹字段（因为指纹已进入 core view），验收确认篡改后的 store 无法 push（exit 5）。
6. **锁不会被误删**：`_pid_alive()` 在 Windows 上**绝不探测**（`os.kill` 在 Windows 会把非控制台信号映射成 `TerminateProcess`，探测可能杀掉无关进程），返回 `None` → 必须 `--force`。异主机锁、畸形锁、旧格式锁一律需要 `--force`。锁从不按时间自动清理。
7. **fail-closed 方向保持**：路径判定宁可 false positive；ID 校验宁可拒绝可疑对象；`--force` 一律是显式人工决定。

---

## 7. Drift Changes

**核心改变**：比较单元从「路径集合」改为「每路径的状态集」。

```
old_states[path] = {staged|unstaged|untracked|conflicted...}  from context.git.working_tree
new_states[path] = 同上，取自 live git_collect()
entered  = new - old        → working-tree-changed（内容漂移）
left     = old - new        → working-tree-changed（内容漂移）
restated = 交集中状态集不同 → index-state-changed（索引漂移，LOW）
```

**两类漂移的语义分工**

- **内容漂移**（路径进出工作集 / HEAD 移动 / 重要文件指纹变化）→ 会使已记录的 validation 变 `STALE`。字节变了，之前 build/test 过的代码就不在了。
- **索引漂移**（`git add`，字节未变）→ `index-state-changed`，固定 `LOW`，**永不**影响 validation 新鲜度。`git add` 不改变被构建/被测试的字节。

`validation_freshness` 的判定条件改为 `stale_files ∨ commits_since ∨ working_tree_changed`，显式排除 `index_state_changed`。新增计数 `counts.index_state_changed`，`counts.working_tree_changed` 语义收敛为「进入/离开工作集的路径数」。

`index-state-changed` 的 detail 会给出具体迁移，例如：
`1 path(s) changed Git index state; file contents unchanged (src/auth.py: unstaged → staged)`，并在 `transitions` 里给出结构化的 `from`/`to`。

**文案语义修正（#9）**

| 位置 | 5.0.0 | 5.0.1 |
|---|---|---|
| SKILL.md Resume 第 1 步 | "Trust the briefing, not the repo." | "The Context is the map; the live repository is the ground truth for code facts." |
| drift NONE 摘要 | "No drift — repository matches the context's observed state. Context can be trusted as-is." | "No drift — observed repository evidence is current. Agent-supplied claims retain their recorded confidence and provenance." |
| `format_report()` 结论 | "context trustworthy" / "re-verify stale sections before trusting them" | "observed evidence is current; agent claims keep their recorded provenance" / "re-verify the flagged sections — the live repository is the source of truth for code facts" |
| `render.py` | "Before you trust the context:" | "Before you act on the context:" |
| protocol.md §8 | 未区分索引/内容漂移；未说明 NONE 的边界 | 明确 NONE 只陈述 observed 证据；明确两类漂移与 validation 的关系 |

**未破坏 resume 的价值**：目标仍是「Context 优先导航、只验证需要验证的事实、drift NONE 时不重复探索」。SKILL.md 补充说明：NONE 只代表观测证据仍成立，Agent 声明保留其记录的置信度与来源，承载性结论仍应回到代码确认 —— 但**不需要**因此重新扫描整个仓库。

**过程中修复的自引入回归（重要）**
把 `CTX_ID_RE` 收紧时，`CTX_ID_BODY` 一度写成 `ctx_(?:[0-9a-f]{8}|[0-9a-f]{16})`。在 `CTX_ID_SEARCH_RE` 这种**非锚定 search** 下，短分支先匹配，于是从 HEAD 读回 `context: ctx_67086e7f487be3cf` 得到的是 `ctx_67086e7f` —— 一个不存在的 id。连锁后果：`store.head()` 返回无效 id → `head_object()` 变 `None` → `checkout` 后 HANDOFF.md 不刷新 → `ensure_branch_layout()` 写入无效 ref → 3 个看似无关的既有测试失败。
**修法**：长分支在前 + 负向前瞻 `(?![0-9a-f])`，并在注释中写明理由；新增 3 个用例固定该行为（`test_id_search_never_returns_a_truncated_prefix`、`test_head_round_trips_the_full_16_hex_id`、`test_detached_checkout_refreshes_the_handoff`）。
**这 3 个失败是产品回归，不是测试期望过时 —— 修的是产品代码，没有改这些测试。**

---

## 8. Context Integrity Changes

**分层职责（刻意区分）**

| 入口 | 职责 | 成本 |
|---|---|---|
| `load_context()` | 读 JSON、确认是对象、文件名与 `context_id` 对齐 | 单文件读 |
| `context_problems()` | 重算 Context ID、协议判别符、首父指针与血缘结构 | 单对象哈希 |
| `verify_graph()` | 遍历全部对象 + 悬空父引用 | 全 store |
| `verify`（CLI） | 上述全部 + 残留密钥扫描 | 全 store |
| `resume`（CLI） | **仅校验当前 HEAD 的 `context_problems()`**，不通过即 exit 7 | 单对象 |

`load_context()` 保持轻量是刻意的：普通命令（`log`/`show`/`diff`）不应为全量校验付费。但它**不会**因为对象可疑就返回 `None`（那会把篡改伪装成"文件缺失"，反而掩盖问题）—— 门槛在 `verify` 与 `resume`。

**`verify` 输出升级**

```
Context objects:
ctx_3b09b0df2f348952: clean
ctx_efd5f52e27aaca30:
  ERROR Context ID does not match object contents
```
退出码：0 全清 / 2 发现残留密钥 / 7 完整性失败（同时命中时 2 优先 —— 磁盘上真有密钥更紧急）。

**`resume` fail-closed**
校验不通过时 stderr 输出 `resume refused: <id> failed integrity verification` 与逐条原因，**stdout 不含任何简报内容**，退出码 7。

**`status` 增加旁路提示**
完整性失败时打印 `Integrity: FAILED — <首条原因>` + 提示运行 `verify`，退出码 7。正常 store 上输出不变（不引入噪声）。

**覆盖字段**：`goal`、`current_objective`、`progress`、`architecture`、`decisions`、`constraints`、`do_not_change`、`known_issues`、`important_files`（含指纹）、`git.head`、`git.branch`、`git.working_tree`、`git.diff_stat`、`validation`、`recommended_actions`。

---

## 9. CLI Root Discovery Changes

`cli.resolve_project_root(explicit, start)` 取代 `Path(getattr(args, "root", ".")).resolve()`：

1. **显式 `--root PATH`** —— 永远优先，永不被发现逻辑覆盖（推荐路径也会 `expanduser()`）
2. **从 cwd 向上找最近的含 `.context-git/` 的目录**（含 cwd 自身）
3. **所在 Git 仓库根**（复用 `gitstate.find_repo_root()`）
4. **cwd**

发现只会**向上**移动，永远不会逃逸到无关的兄弟目录。`init` 走同一套解析，因此 `repo/src/auth/` 下执行 `context-git init` 写入 `repo/.context-git/` 而不是 `repo/src/auth/.context-git/`。

**副作用（已作为一等公民处理）**：嵌套 store 成为合法用法。这暴露了 #10（`_strip_own_store` 只查首段），已一并修复并补测试。嵌套 store 必须显式传 `--root`，因为发现逻辑会（正确地）选择外层项目 —— 这一点已写入 SKILL.md 与两份 README。

---

## 10. Remote Changes

**锁的实现抽出为 `FileRemoteLock` 上下文管理器**，`O_CREAT|O_EXCL` 语义不变（真正起互斥作用的部分），锁体升级为：

```json
{"pid": 12345, "host": "machine.local", "operation": "push", "created_at": "..."}
```

**读取兼容**：`read_lock()` 同时理解 5.0.1 的 JSON 体与 5.0.0 的 `"<pid> <iso>"` 体，畸形体报「属性全未知」（从而强制 `--force`）。

**新增 `context-git remote unlock [NAME] [--force]`**

允许免 `--force` 清理需要同时满足三条件：锁记录了 pid、该 pid 本机可确认已不存在、锁归属本机。其余情况（pid 存活 / 异主机 / 畸形体 / 平台无法探测存活）一律需要显式 `--force`，并在错误信息里说明**具体原因**。

**安全性取舍**：锁**永不**自动清理，也**永不**按时间清理 —— 时间无法区分「慢写入者」与「死写入者」。崩溃的写入者代价是一条显式命令；误删仍有效的锁代价是丢失互斥。这是有意的 fail-closed 方向，已写入 README / `references/remote.md` / `references/security.md`。

**可发现性**：`remote show` 现在报告 `Writer lock: <pid/host/operation/时间/存活/>` 并提示 `remote unlock`；轮询失败的 push 错误信息也包含持有者详情与修复指引，不再是裸的 "retry later"。

**HTTP 远端**：无本地锁，`remote unlock` 对其明确拒绝（`only file remotes use a local writer lock`）。`HttpTransport` 的 ETag/`If-Match` 逻辑未改动。

**往返正确性**：`push`/`fetch`/`pull`/`network` 的校验链（Context ID → 血缘 → 完整 SHA-256 → 字节大小 → 残留密钥）完全保留；验收确认篡改后的 store 无法 push（exit 5）。

---

## 11. 测试结果

| 项目 | 数值 |
|---|---|
| 总测试数 | **216** |
| Passed | **216** |
| Failed | **0** |
| Skipped | **0** |
| Errors | **0** |

```
Ran 216 tests in 519.142s
OK
```

**基线对比**：改动前同一命令为 `Ran 148 tests in 242.433s / OK`。新增 68 个用例（全部来自 `tests/test_v5_1_regressions.py`，10 个测试类），无一个既有用例被删除。

**平台**

| 平台 | 是否在本次会话执行 |
|---|---|
| macOS 3.13.12（本机 Python 3.13.12 / Darwin 24.6.0 arm64） | ✅ 已执行：216/216 |
| Linux（Ubuntu, Python 3.8 / 3.13） | ❌ 本地不可执行 —— 本机为 macOS，未做本机验证 |
| Windows（Python 3.12） | ❌ 本地不可执行 —— 未做本机验证 |

CI 矩阵（`.github/workflows/tests.yml`：ubuntu 3.8、ubuntu 3.13、macos 3.12、windows 3.12）**未做任何修改、未减少**，由 CI 覆盖其余平台。若干用例按设计条件跳过 Windows（tab / 换行文件名；死 pid 存活探测因 Windows 上探测不安全而不可用），跨平台差异已在用例内注释说明。

**额外的人工 CLI 验收**（真实临时仓库 + 双 clone，覆盖 §14 清单）
**68 项检查，0 失败**，涵盖：子目录 `init`/`snapshot`（落在仓库根、无嵌套 store）、`status`/`log`/`show`/`diff`、`commit` 增量与 no-op 拒绝、`resume`（含 `--json` / `--write-briefing`）、`verify`、`branch`/`switch`/`checkout -`/`merge`/`log --all`、`remote add/show/unlock` + `push`/`push --dry-run`/`fetch`/`pull`/`branch --all`、`network register/agents/send/inbox/accept --switch/reply/status`（两 clone 真实交接：`hnd_07dddc8a19165cdb` 走完 pending → accepted → completed）、`adapters`/`capabilities`/`sessions`/`import-session --dry-run`/`--version`/`--help`；以及 4 项完整性/锁的负向验证（篡改 → `verify`/`resume`/`status` 均 exit 7、push 拒绝发布；异主机锁与畸形锁在无 `--force` 时被拒且**锁未被删除**）。

`python scripts/context_git.py --version` → `context-git 5.0.1`；`--help` 正常；全部 `schemas/*.json` 通过 JSON 解析；`context_git/` 与 `tests/` 全部通过 `compileall`。

---

## 12. 仍存在的 Known Limitations

1. **旧 8 hex ID 的强度仍是 32 bit**。旧对象可读可校验，但不会被就地升级；要获得 64 bit 需重新 snapshot。本地校验能发现损坏与随意篡改，**不能**防住一个既能改对象内容、又能同时改写所声明 ID 代次的攻击者（这是任何"必须继续校验旧格式"的方案都固有的下界）。
2. **文件远端锁永不自动恢复**。崩溃的写入者必须显式执行一次 `remote unlock`。代价已知且被刻意选择，不是遗漏。
3. **语义合并仍是确定性的结构合并**，不使用 LLM：措辞不同但语义相同的两条笔记可能仍需要显式 `--resolve`。
4. **本地 store 仍假设同一时刻只有一个写入者**。
5. **漂移只由 Git 与文件证据计算**。索引漂移被刻意排除在 validation 失效条件之外，这是语义判断而非纯观测；若某个团队的流程确实把 `git add` 视为"代码已变更"，需要重新讨论。
6. **`windows-latest` / Linux 未在本会话实测**，仅由未改动的 CI 矩阵覆盖。
7. **`examples/context-object.example.json` 的 `context_id` 与其内容不匹配** —— 5.0.0 下同样不匹配（已用仓库内 5.0.0 冻结副本对照验证），因此**不是本次引入的回归**。它是渲染样例而非可校验对象：其父对象 `ctx_f6a843a0` 在 `examples/` 中根本不存在，所以即便重算 id 也无法让它真正可校验；且该 id 被 `HANDOFF.example.md`、`context-diff.example.txt`、`resume.example.txt`、`README.md` 交叉引用。**建议后续单独处理**：要么重算并同步 5 处引用，要么在 README 里注明 examples 是渲染样例、不承诺通过 `verify`。
8. **`build/`、`dist/`、`context_git.egg-info/` 是 5.0.0 的陈旧构建产物**，未重新生成。注意 `build/lib/context_git/` 仍是 5.0.0 的冻结副本（本次正好用它当"旧版本 oracle"来对照行为，判断某个失败是既有问题还是新回归）—— 若决定清理，会失去这个便利。
9. **工作区在我动手前已有未提交改动**：`README.zh-CN.md`（未跟踪）与 `MANIFEST.in`（增加了一行 `README.zh-CN.md`）。**非本次修改**，仅在此声明以免混淆 blame。
10. **未在本机执行 PyPI 打包/安装验证**（`pip install .`）；CI 中的 `Install package` 与 `Verify CLI` 步骤会覆盖。

---

## 13. 是否建议继续 V6

**NO。**

理由：

1. **5.0.1 引入了一次磁盘格式的代次演进**（`id_hash_version` / `core_view_version`），目前在真实数据上只验证过本仓库自身的产物。在真实用户项目跑过一轮、确认旧 store 的读取/校验/合并/同步都没有意外之前，不应再叠加新格式层。
2. **分类器已按你的要求从 Stable 降为 Beta**。这与"先让核心基础站稳"是一致的 —— Beta 期间应当继续收集真实边界上的漂移/差异异常，而不是扩面。
3. **V5 的核心承诺尚未被真实使用验证**：Agent Context Network 的两 clone 交接、残留锁恢复、跨机器 fetch 后的漂移重评估，都还只是测试与人工验收级别的证据。
4. **本次暴露的教训说明基础设施仍在"找 bug"阶段**：一个纯粹为正则收紧而做的改动，就通过 `head()` 静默传播成 3 处不相关行为异常。这说明读取路径的耦合度与可观测性还有提升空间，值得在扩面之前先把读取/校验路径的可观测性补上（例如 `doctor` 类命令一次性输出 store、HEAD、refs、对象代次分布与锁状态）。
5. 若一定要推进，优先级建议是：**先做一轮真实项目实战 + 一个只读 `doctor`/自我诊断命令**，再考虑 V6；且 V6 若要做，方向应是"让现有能力更可信"（例如对象签名、加密-at-rest、prune/GC、真实多写入者并发），而不是新增概念层。
