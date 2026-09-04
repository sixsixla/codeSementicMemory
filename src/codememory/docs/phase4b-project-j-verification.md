# Phase 4B 实施记录：Project_J 代码绑定验证

## 为什么是 Project_J 专用适配器

P4 和 CodeBaseMemory 目前只对本地最大的 `Project_J` 工程可用。验证层因此把
“是否允许调用外部代码证据源”做成逻辑作用域守卫，而不是把两个工具写进通用
存储层。其他工程仍可写入和检索记忆卡，只会保持 `unverified`，以后可以接入
Git、文件系统、LSP 或其他 manifest provider。
即使请求使用 Project_J logical id，显式标注为其他工程的 provider manifest 也会被
拒绝，不会落盘快照或改变绑定。

## 已实现的链路

1. `SnapshotManifest` 统一 P4、CodeBaseMemory MCP 和未来 provider 的输入；支持
   `files`、`symbols`、`nodes` 以及 MCP `content/structuredContent` envelope。
2. `normalize_repo_path` 把 Windows client/depot 路径归一化到 `Assets/...`，并
   保留未知根路径用于诊断。
3. `ManifestIndex` 做精确 qualified symbol 匹配；只有短名唯一时才允许短名回退，
   不会在同名类型之间猜测。
   只有存在符号索引证据时，文件存在但符号缺失才会判为 `stale`；P4 的
   `fstat` 文件清单本身不会被误当成类/方法索引。
4. `collect_p4_fstat_manifest` 是显式、只读、可注入 runner 的 P4 CLI 采集器。它
   不保存密码、不自动 checkout；连接参数由调用方传入。
5. `VerificationService` 只选择当前 card version 的绑定，按任务可限缩；状态规则：

   | 证据 | 状态 |
   |---|---|
   | 文件和符号在同一当前路径，且 revision 一致 | `verified` |
   | 符号在另一当前文件 | `renamed` |
   | 文件在快照且有符号索引但符号消失，或 P4 `haveRev != headRev` | `stale` |
   | 完整快照中找不到目标 | `missing` |
   | 快照不可用或仅覆盖选中路径 | `unverified` |
   | URL、glob、空目标等非绑定 | `rejected` |

6. write 模式更新 `memory_card_bindings.status/snapshot_id/metadata`；所有原始
   事件、候选、卡片版本和链接保持不变。不可用 provider 不会降级已有权威状态。

## 命令和 API

```powershell
# 只读预览（不会改变当前绑定，但会留下 audit run）
py -m codememory verify-bindings --db $db `
  --logical-project-id logical-319e6e98c97340e7807d6bb7 `
  --manifest fixtures/verification/project-j-baseline.json `
  --task-id codex-thread:01a04cae-4577-7f22-b29f-80ffb07afcbc

# 确认后写入当前绑定投影
py -m codememory verify-project-j --db $db --manifest $manifest --write

# 查看覆盖、状态、快照和最近运行
py -m codememory verification-report --db $db

# 小范围审计时附带完整 manifest
py -m codememory verification-report --db $db --limit 3 --include-manifest
```

HTTP 对应：

- `POST /v1/verification/bindings`（别名 `/v1/quality/verify-bindings`）
- `GET /v1/verification/report`
- `GET /v1/verification/report?include_manifest=true`（小范围审计时附带 manifest）
- `GET /v1/verification/snapshots/{snapshot_id}`（读取完整规范化 manifest）
- `GET /v1/verification/bindings`

`migrations/0008_snapshot_manifest_content.sql` 在 `code_snapshots.manifest_json`
中保存每次规范化输入的完整文件/符号/版本内容。`captured_at` 仍用于审计，但不参与
`manifest_hash` 或验证 `input_hash`，所以相同证据跨进程重放保持幂等；旧的 `{}` 快照
会在再次遇到相同内容时安全回填，不改变快照身份。

## 真实 Project_J 验证

验证使用三个已授权 Codex 任务：任务面板、护送归属、化形之魂。CodeBaseMemory
MCP 的索引状态为 `ready`，817,030 nodes / 2,022,384 edges；通过
`search_graph` 得到四个实际文件和四个符号。P4 在
`D:\P4Workspace\client\mainline` 上用只读 `fstat -Ol -T ...` 取得同四个文件的
head/have revision。证据被整理为不含源码和凭据的
`fixtures/verification/project-j-baseline.json`。

在 `selected_paths` 覆盖声明下的任务范围写入结果：38 个当前绑定中 4 个
`verified`、33 个 `unverified`、1 个 `rejected`。其余绑定没有被标成 `missing`，
因为四文件清单不是完整仓库快照；这正是覆盖安全规则要保证的行为。当前可复现的
任务范围写入运行是 `verification-f74f3073-e42f-4388-9628-ec2a1328c5a2`，输入
hash 为 `667584b957d7bfacf592362a28e4b5374af5c77066e4dbad69c627b5e3b82f83`；同一
证据重复运行会复用快照 ID `snapshot-584957afd75a632672e3167aafb8962c`（P4 与
CodeBaseMemory 快照 ID 分别为 `snapshot-d61d4b447018f93bb5899d24ff2e22f6` 和
`snapshot-fa2391b55c7a2c29f2c81d23d27f9268`）。

随后对 Project_J 当前可见绑定做了同一基线的全量投影检查：6412 个绑定被检查，
6 个 `verified`、4 个 `renamed`、6376 个 `unverified`、26 个 `rejected`；51 个
已有非 `unverified` 状态被保留，避免有限快照造成降级。这个全量结果只用于展示
当前覆盖边界，不把四文件清单宣称为完整仓库扫描。

## 后续边界

- 要得到可靠的 `missing`，需要由 P4/CodeBaseMemory 适配器提供
  `coverage=complete` 的仓库快照或显式查询范围。
- 还可接入 CodeBaseMemory 的调用图/AST 关系，验证方法级 qualified name 和
  owner module；当前版本已能保存这些字段但不依赖 MCP SDK。
- 周期刷新、快照过期和绑定自动晋升应建立在本阶段 audit rows 之上，不应绕过
  `VerificationService` 直接改 SQL。
