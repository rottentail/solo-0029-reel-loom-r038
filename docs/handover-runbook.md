# 声画织场（Reel Loom）运行与故障交接文档

适用对象：**未参与本服务开发的值班同事**。
适用范围：本机/单机部署的 `reel_loom` 服务（当前版本 `0.1.0`，见 `reel_loom/application.py:56`）。
文档原则：每条关键结论都标注源码依据（`文件:行`）或随附实测记录（`handover-evidence/` 下文件）。本文件不改变任何生产行为，只描述系统本来就有的机制与已验证的处置方法。

---

## 0. 一分钟速查（先看这里）

- 数据全部在一个目录里：`LOOM_DATA`（默认 `.reel-loom/`，`reel_loom/config.py:29`）。它由 `reel_loom.sqlite3`（含 `-wal`/`-shm`）、`blobs/`（媒体字节，按 sha256 分两段子目录存放）、`scratch/`（临时半成品）、`reel_loom.lock`（实例锁）组成（`reel_loom/config.py:37-42`、`reel_loom/blobs.py:16-19`）。
- **数据库记录和媒体字节必须同时存在且互相匹配，系统才可信。** 只复制 SQLite 得到的是一个“能打开、能回读 JSON、但缺来源/成片字节”的残缺系统。
- 渲染中断后：`succeeded` 的成片、已 `sealed` 的修订、已生成的印本**仍然可信**；卡在 `running`/`queued` 的任务**没有可信产物**，重启后会被自动改判，再用原单重试。
- 正常关停只有一种方式：**Ctrl-C / SIGINT**（`reel_loom/server.py:17-18`）。SIGTERM、断电、`kill -9` 都属于异常中断。
- 恢复是否成功，**以用户侧结果为准**：任务重试后能出片、成片通过全解码验规、印本 verify 通过、下载字节 sha256 与登记摘要一致；而不是“服务能启动、数据库 `ok`”。
- 无法确认某段媒体完整性时：**先固定证据、停止写入与打包，不要删除、不要 sweep、不要手工补文件**，按第 10 节升级处理。

---

## 1. 真实数据目录长什么样

服务启动时 `Settings.prepare()` 只创建两个子目录（`reel_loom/config.py:37-42`）：

```
<LOOM_DATA>/
├── reel_loom.sqlite3        # 主库（WAL 模式，reel_loom/database.py:23）
├── reel_loom.sqlite3-wal    # WAL，运行时可能有未落盘事务
├── reel_loom.sqlite3-shm    # 共享内存索引
├── reel_loom.lock           # 实例锁，空文件，真正的锁是 flock（reel_loom/instance.py:7-17）
├── blobs/                   # 所有权威媒体字节
│   └── <前2位哈希>/<其余62位>      # 路径即 sha256，reel_loom/blobs.py:16-19
└── scratch/                 # 仅存渲染/分析临时目录，loom-XXXX/media.*
```

实测目录形态见 `handover-evidence/layout-primary-during-running.json` 与 `layout-restored-after-delivery.json`。要点：

1. **`blobs/` 是内容寻址存储**：文件名就是字节的 sha256，相同内容天然去重（入库时先算哈希再决定是否写盘，`reel_loom/blobs.py:26-50`）。
2. **`scratch/` 不是权威数据**。它只通过 `tempfile.TemporaryDirectory` 存活（`reel_loom/blobs.py:21-24`），正常完成后临时目录会被自动清掉；进程被硬杀时才会残留 `loom-XXXX/media.*` 半成品。实测 SIGKILL 后残留的 `media.mp4`（95367 字节）在 `artifacts` 表里**没有任何登记**（`layout-primary-after-sigkill.json` 中 `count_artifacts: 0`）。
3. **SQLite 开 WAL、外键、立即写事务**（`reel_loom/database.py:17-23,35`）。备份时主库、`-wal`、`-shm` 要当作一个整体，不能只拿主库文件。

---

## 2. 媒体入库 → 冻结任务 → 成片 → 印本的真实引用链

理解引用关系，才能判断“缺了哪个字节会伤到什么”。

### 2.1 入库（assets → blobs）

- 上传原始字节先写临时文件，再用 PyAV 探针 `inspect_media` 真实解码校验（`reel_loom/assets.py:11-27`、`reel_loom/probe.py:7-46`）。不可解码的媒体直接拒绝、**不登记**。
- 校验通过后 `blobs.put` **先把字节落盘并 fsync、`os.replace` 原子改名，再在同一事务里写 `blobs` 与 `assets` 行**（`reel_loom/blobs.py:37-50`、`reel_loom/assets.py:17-27`）。因此“有 `assets` 行就应当有对应 blob 文件”，反之 blob 文件存在但库里没有行则是未登记孤儿。
- `assets.state` 只有 `ready` / `retired`（`reel_loom/schema.py:9`）。退役是状态位，不是物理删除（`reel_loom/assets.py:47-53`）。

### 2.2 冻结任务（tickets 冻结修订、版式、源闭包）

- 提交渲染单时，`Planner.freeze` 把**剪辑修订文档、版式版本、以及该卷用到的每个来源（asset id + blob 哈希 + 元信息）整体快照**写进 `tickets.snapshot`，并算一个 `fingerprint`（`reel_loom/planner.py:11-29`、`reel_loom/tickets.py:16-23`）。
- 快照一旦写入不可变。版式后续改版不会影响已排队任务（版式表按 `(id,version)` 版本化，`reel_loom/schema.py:21-24`、`reel_loom/looks.py:28-39`）。
- **渲染时来源 blob 是带哈希校验读取的**：`compile(..., resolve=lambda key: self.blobs.resolve(key, verify=True))`（`reel_loom/rendering.py:23`）。来源字节一旦对不上 sha256，渲染当场失败，不会静默产出错片。

### 2.3 成片（artifacts 与成功状态在同一事务原子登记）

这是判断“中断后哪些还可信”的核心机制：

- FFmpeg 在独立进程组、独立 scratch 文件里跑（`reel_loom/processes.py:34-47`、`reel_loom/rendering.py:22-27`）。
- 成功后 `Artifacts.commit_render` 在**一个 `BEGIN IMMEDIATE` 事务里**：写 blob → 插 `artifacts` 行 → 把 ticket 改成 `succeeded`（`reel_loom/artifacts.py:10-23`）。三者要么全成、要么全回滚。
- 因此：**只要 ticket 是 `succeeded` 且带 artifact，其成片字节必然已落盘并在同一事务登记；处于 `running` 时 scratch 里的任何文件都不是成片，系统从不把半成品当有效媒体。**
- 失败/取消路径只改状态、不登记产物（`reel_loom/tickets.py:83-92`）。

### 2.4 印本（imprints 冻结成片为 ZIP 并可自校验）

- 只有 `succeeded` 的任务能打包（`reel_loom/imprints.py:17-18`）。manifest 内含完整渲染快照、成片摘要（哈希+大小）、可选投影字幕（`reel_loom/imprints.py:28-29`）。
- 打包时成片字节带 `verify=True` 读取（`reel_loom/imprints.py:43`），ZIP 成员信息确定性化（`reel_loom/imprints.py:41-42`）。
- `GET /imprints/{id}/verify` 会**重新读包、重算包内每个媒体的 sha256、并和 manifest 逐项比对**（`reel_loom/imprints.py:71-95`）。这是交付前最强的端到端字节级证明。
- 历史印本不可变：上游再改稿，同一已完成任务再打包返回同一印本 id（实测见基线工作流 `reel_loom/demo.py:119-127`）。

### 2.5 引用关系总图（外键见 `reel_loom/schema.py`）

```
reels ─< revisions (文档 JSON 内含 slices，slice 引用 asset id)
looks(id,version)
assets ──► blobs(hash)                 来源媒体
tickets ──► reels/revisions、looks(id,version)；snapshot 内冻结各 source 的 blob 哈希
artifacts ──► tickets 或 assets；artifacts.blob ──► blobs     成片/接触表
imprints ──► tickets；imprints.blob ──► blobs                 交付 ZIP
analyses ──► assets（结果按 内容+参数+算法版本 去重，reel_loom/analysis.py:29）
cue_tracks ──► assets
```
血缘可通过 `GET /tickets/{id}/lineage` 取回有向闭包，且代码要求闭包完整否则 409（`reel_loom/lineage.py:39`）。实测恢复后的血缘包含 7 类节点、8 条边且闭包完整（`drill-results.json` 的 `lineage_node_kinds`/`lineage_edge_count`）。

---

## 3. 正常关停 vs 异常中断（必须区分）

两种情况在数据上的后果完全不同，已用实测确认。

### 3.1 正常关停：只接受 Ctrl-C / SIGINT

- 服务主循环只捕获 `KeyboardInterrupt`（`reel_loom/server.py:16-18`），收到后退出 `with` 上下文，`Application.close()` → `Renderer.close()` 置停止位并 `join` 工作线程（`reel_loom/rendering.py:62-66`、`reel_loom/application.py:60-64`）。
- 正在跑的 FFmpeg 会被取消逻辑收敛：工作线程在循环里检测停止位后终止子进程并抛 `RenderCancelled`（`reel_loom/processes.py:50-52`、`reel_loom/rendering.py:26`），票被落为终态。
- **实测（`drill-timeline.log:27-30`、`server-graceful.log`）**：对运行中任务发 SIGINT，进程退出码 **0**；重启后该票为 `cancelled / render_cancelled`，且 `health.recovery = {"interrupted": 0}`——说明没有遗留需要启动恢复的 running。
- 注意运维陷阱：**systemd 的 `KillSignal` 默认是 SIGTERM，不是 SIGINT**。本服务没有注册 SIGTERM 处理器，SIGTERM 走默认终止，效果等同异常中断。若用 systemd 管理，应显式 `KillSignal=SIGINT`，否则不要把“systemctl stop”当作正常关停。

### 3.2 异常中断：SIGTERM 默认处置、`kill -9`、断电、机器崩溃

- 进程没有机会执行 `close()`，正在运行的票在库里**停留在 `running`**，scratch 残留半成品。
- 下次启动时 `Application.__init__` 调用 `tickets.recover()`（`reel_loom/application.py:47`）：把所有 `running` 改判——未请求取消的标 `failed / worker_interrupted`，已请求取消的标 `cancelled`（`reel_loom/tickets.py:103-112`）。
- **实测（`drill-timeline.log:7-13`）**：SIGKILL 后库里票仍是 `running`；用全量备份在新目录重启，`health.recovery = {"interrupted": 1}`，该视频票变为 `failed`，错误体为
  `{"error":"worker_interrupted","detail":"Previous worker stopped before committing output"}`，`artifacts: []`。
- 恢复只改状态，**不自动重跑**。需要人工对失败/取消票显式 `POST /tickets/{id}/retry`。

### 3.3 两种情况对照

| 维度 | 正常关停（SIGINT/Ctrl-C） | 异常中断（SIGTERM 默认/kill -9/断电） |
| --- | --- | --- |
| 进程退出码 | 0（实测） | 负值（实测 SIGKILL=-9，SIGTERM=-15） |
| 在途票最终落库 | 退出前落为 `cancelled` | 库中停留 `running`，重启才改判 |
| 启动恢复计数 `recovery.interrupted` | 0 | ≥1 |
| scratch 半成品 | 临时目录随正常流程清理 | 残留 `loom-XXXX/media.*` 孤儿 |
| 恢复动作 | 直接对 cancelled 票 retry | 确认改判后对 failed/cancelled 票 retry |
| 已 `succeeded` 成片/印本 | 不受影响，保持可信 | 不受影响，保持可信（原子事务保证） |

---

## 4. 渲染中断后，哪些内容仍然可信

按实体给出可执行的判定依据，而不是凭感觉。

| 内容 | 中断后是否可信 | 判定依据（怎么查） |
| --- | --- | --- |
| 已 `succeeded` 渲染单的成片 | **可信** | `GET /tickets/{id}` 状态 `succeeded` 且 `artifacts` 非空；下载 `GET /artifacts/{id}/content` 会带哈希校验（`reel_loom/api.py:131-133`）。原子提交保证字节与状态同生共死（`reel_loom/artifacts.py:10-23`） |
| 已生成印本 ZIP | **可信** | `GET /imprints/{id}/verify` 返回 `valid:true`（`reel_loom/imprints.py:71-95`）。实测 verify 通过（`drill-results.json: imprint_verify: true`） |
| 剪辑卷历史修订、版式历史版本 | **可信且不可变** | 修订/版式只追加不改写（`reel_loom/reels.py:41-57`、`reel_loom/looks.py:28-39`）；`GET /reels/{id}?revision=n`、`GET /looks/{id}/history` 可回读 |
| `ready`/`retired` 来源媒体 | **仅当字节还在且哈希正确才可信** | `GET /resources` 看是否 `missing`；要确认字节内容用会触发 `verify=True` 的路径（渲染、验规、印本、`assets/{id}/restore`）。注意 `/resources` 不查哈希（见第 7 节） |
| 卡在 `running` 的票 | **不可信，无产物** | 重启后必被改判（`reel_loom/tickets.py:103-112`）；其 scratch 文件无登记，不得当片交付 |
| `queued` 的票 | 未开始，无产物，可继续 | 重启后仍在队列，工作线程会按 `created,id` 顺序领取（`reel_loom/tickets.py:52-59`） |
| `failed`/`cancelled` 的票 | 无成片，但冻结快照仍有效 | 可原单 `retry`，复用同一快照与 fingerprint（见第 5 节） |
| scratch 里的 `media.*` | **一律不可信、不可交付** | 只是临时半成品，无任何 DB 登记（实测 `count_artifacts:0`） |

---

## 5. 重开后怎样继续交付（标准操作步骤）

前提：你已经按第 6 节用**全量目录**把数据放到新的 `LOOM_DATA`，且该目录没有第二个进程在跑（实例锁，第 8 节）。

1. **启动服务并先读健康检查**
   `GET /health`。关注三个字段（`reel_loom/application.py:55-58`、`reel_loom/database.py:67-71`）：
   - `recovery.interrupted`：本次启动把多少个遗留 running 改判了；
   - `ticket_states`：各状态票数；
   - `database`：`PRAGMA quick_check` 结果；`blobs`：库内 blob 计数与字节数。
2. **核对资源一致性**：`GET /resources`（`reel_loom/resources.py:24-44`）。
   - `missing` 非空 = 库里有登记但字节文件缺失，**先解决缺失再谈交付**；
   - `bad_size` 非空 = 字节大小与登记不符；
   - `unregistered_blobs` = 有字节无登记（失败事务可能留下，系统不当有效媒体，可在确认无引用后处理）。
3. **逐票确认终态**：`GET /tickets?state=failed`、`?state=cancelled`，读 `error` 字段。异常中断的票会带 `worker_interrupted`。
4. **原单重试，不要新建渲染单**：`POST /tickets/{id}/retry`（`reel_loom/tickets.py:94-101`）。
   - 重试只接受 `failed`/`cancelled`，会把 `attempt+1`、清进度、清错误、回到 `queued`，**冻结快照和 fingerprint 原样保留**。
   - 实测：中断票重试后 `attempt:2`、`state:succeeded`、`fingerprint_unchanged:true`（`drill-results.json: retry_outcome`）。这保证交付的仍是当初封卷时的修订与版式，而不是被悄悄换成新稿。
5. **用用户侧结果证明恢复成功**（缺一不可，见第 9 节）：
   - 成片：`GET /artifacts/{id}/conformance` 全解码验规 `valid:true`；
   - 下载：`GET /artifacts/{id}/content`，核对字节 sha256 = 登记的 blob 哈希；
   - 打包：`POST /tickets/{id}/imprints` 后 `GET /imprints/{id}/verify` 为 `valid:true`；
   - 血缘：`GET /tickets/{id}/lineage` 闭包完整。
6. **再开放下游交付**。只有 verify 通过的印本/成片才能对外。

如果重试立刻又失败，先看返回的 `error.code`：`not_found(blob does not exist)` 说明来源字节缺失（第 6.4 节），`blob_corrupt` 说明来源字节哈希不符（第 7、10 节），`encoder_failed`/`encoder_timeout` 才是编码本身问题（`reel_loom/processes.py:55,70`）。不要在根因未明时反复重试。

---

## 6. 备份与恢复：避免“数据库正常却缺来源/成片字节”

### 6.1 必须备份什么

**备份单位是整个 `LOOM_DATA` 目录，不是单个 SQLite 文件。** 因为：

- 权威媒体字节在 `blobs/`，库里 `assets.blob`、`artifacts.blob`、`imprints.blob` 都以外键指向 `blobs.hash`（`reel_loom/schema.py:8,45,55`）。少了 blob 文件，外键照样成立、数据库照样 `ok`，但一渲染/下载就 `not_found`。
- SQLite 在 WAL 模式，运行时最新事务可能还在 `-wal` 里（`reel_loom/database.py:23`）。只复制主库会丢最近已提交事务。

一次完整备份应包含：`reel_loom.sqlite3`、`reel_loom.sqlite3-wal`、`reel_loom.sqlite3-shm`、整个 `blobs/`、整个 `scratch/`、`reel_loom.lock`。
（锁文件复制过去无害：`flock` 是咨询锁，进程退出即释放，新目录里不会因此误判，见 `reel_loom/instance.py:10-17`。）

### 6.2 首选：冷备份（停服后复制）

1. 用 SIGINT/Ctrl-C 正常关停（第 3.1 节），确认进程退出、端口释放；
2. 整体复制 `LOOM_DATA` 到备份目标；
3. 复制后在备份目录跑一次 `GET /resources` 等价核对（可临时起服务，或直接比对文件清单与 `blobs` 表）。

冷备份没有运行中写入，事务与字节最一致。

### 6.3 热备份（服务运行中复制）的正确做法与风险

本服务没有提供在线快照 API，热备份是文件系统级复制，**可能复制到一个事务中间态**。把风险讲清楚：

- 媒体 blob 的写入是“临时文件 → fsync → `os.replace` 原子改名 → 同事务登记”（`reel_loom/blobs.py:37-49`），所以单个 blob 文件要么完整出现、要么不出现，不会复制到半个改名后的文件；但可能复制到“文件已在、登记事务还在 WAL 未被你捕获”的未登记字节（系统会在 `/resources` 报 `unregistered_blobs`，不会当有效媒体）。
- SQLite 侧必须把主库+WAL+SHM 一起、尽量在同一瞬间复制，否则可能拿到不一致的 WAL。**更稳妥的热备方式是用 SQLite 自身的在线备份能力**对库做一致快照（例如 `.backup` / `sqlite3_backup_*`），同时用文件清单保证 `blobs/` 完整。
- 本次演练在“有任务 running”时做了整目录热复制（`drill-timeline.log:4-6`），恢复后服务正常、`reconcile.consistent=true`，说明在本工作负载下整目录复制可用；但这不能替代上面的一致性原则，生产上仍建议优先冷备或 SQLite 在线备份。

### 6.4 恢复步骤与“部分复制”反例（已实测）

恢复 = 把**完整备份目录**设为新的 `LOOM_DATA` 再启动。启动后按第 5 节走。

**反例（已实测，`drill-results.json` 的 `partial_*` 字段、`server-restored-partial.log`）**：只保留 SQLite、删掉 `blobs/` 下字节后启动——

- `/health` 仍返回 `database:"ok"`、`blobs.count:1`（库自认为有 1 个 blob），服务正常起、票的 JSON 快照也能完整回读（`partial_ticket_looks_normal`）；
- 但一旦 retry 真正去读来源字节，立即失败：
  `{"error":"not_found","detail":"blob does not exist","context":{"id":"5f13…"}}`（`partial_retry_error`）；
- `/resources` 如实报 `consistent:false, missing:1`（`partial_reconcile`）。

**结论：`database:ok` 只证明 SQLite 自身完整，绝不证明媒体字节齐全。** 判断恢复是否可用必须过 `/resources` + 实际 verify，而不是只看健康检查。

### 6.5 字节缺失时的安全补救：用原始字节重新入库

如果 `missing` 的是**来源媒体**且你还留着上传原始文件，可以重新 `POST /assets` 相同字节：

- `blobs.put` 发现目标哈希文件不存在会重新写盘（`reel_loom/blobs.py:37-45`），入库去重逻辑会复用同一 asset 与同一 blob 哈希，不产生重复行（`reel_loom/assets.py:19-25`）。
- **已实测（`reingest-remediation.json`）**：删掉来源 blob 后 `/resources` 报 missing；重新上传相同字节，`same_asset_id:true`、`same_blob:true`、文件恢复、`reconcile.consistent:true`、`verify` 通过，`blobs`/`assets` 行数都不增加。
- 前提是你能证明手上的字节与原哈希一致（重传后系统会自己校验）。**拿不出相同原始字节就不要凭空补文件**，走第 10 节。
- 若缺失的是**成片/印本 blob** 而来源还在且任务可重渲染，优先用原单 `retry` 重新生成；印本可由成功任务重新打包（内容寻址，结果同 id）。

### 6.6 关于 scratch：不要把“清空 scratch”当万能步骤

- scratch 只放临时半成品（`reel_loom/blobs.py:21-24`），**不参与任何引用完整性**。清掉它不会修好任何 `missing`/`blob_corrupt`，也不会恢复任何成片。
- 它唯一的“用处”是异常中断后的现场：里面的 `loom-XXXX/media.*` 可用于判断当时渲染到了什么程度、留作排查证据。**在你还需要证据时不要删。**
- 正常情况下临时目录由 `TemporaryDirectory` 自动清理；只有确认服务已停、没有 running 票、且证据已留存后，残留的 `loom-*` 目录才可人工清理。本次演练在恢复交付完成后，scratch 里仍保留 SIGKILL 时的孤儿 `media.mp4`（`layout-restored-after-delivery.json`），它不影响任何已交付结果。

### 6.7 关于资源回收 sweep

`POST /resources/sweep` 只删除**数据库已知、且不被 assets/artifacts/imprints 任何一方引用**的 blob（`reel_loom/resources.py:8-22`），默认 `dry_run:true`。它：

- 不会修复 `missing`（缺的是文件，无东西可删）；
- 不会处理 `bad_size`/损坏；
- 退役来源因仍被历史任务/产物引用而受到保护（基线工作流 `reel_loom/demo.py:147-154` 已验证引用来源不会被回收）。
**在故障未定性前不要执行 `dry_run:false`。**

---

## 7. 媒体完整性的两个层级（避免误判“一致”）

这是最容易踩的坑，已用对照实测确认（`supplemental-probes.json`）。

1. **`/resources` 的 `reconcile` 只查“在不在、大小对不对”**，不重算哈希（`reel_loom/resources.py:24-44`）。
   - 实测把一个 blob 中间翻转 1 个字节、**保持总大小不变**后，`reconcile` 仍报 `consistent:true`，`missing`/`bad_size` 全空。
2. **真正的字节完整性只在 `verify=True` 路径重算 sha256 才暴露**（`reel_loom/blobs.py:52-62`）。
   - 同一损坏文件，`blobs.resolve(..., verify=True)` 与 `assets.restore` 都报 `blob_corrupt / Media digest differs from stored content`；
   - 而此时 `/health` 依然 `database:ok`、`status:ready`。

**因此：**

- `reconcile.consistent=true` 只能证明“登记的文件都在、大小没差”，**不能证明字节没被同尺寸篡改/损坏**。
- 交付前必须用会做全量哈希/全解码的接口终判：成片 `conformance`（`reel_loom/conformance.py:12-56`，真实全流解码到结尾并核对时长/采样/尺寸/帧率/单调 PTS）、印本 `verify`、以及带校验的内容下载。
- 直接 `GET /blobs/{sha256}` 这条路由**不做 verify**（`reel_loom/api.py:136-139`），不要把它当成完整性校验手段；`artifacts`/`imprints` 的 content 下载才带 `verify=True`（`reel_loom/api.py:131-133`）。

---

## 8. 实例锁：防止两个进程互相误恢复

- 每个 `LOOM_DATA` 启动时用 `flock(LOCK_EX|LOCK_NB)` 抢 `reel_loom.lock`（`reel_loom/instance.py:10-17`）。第二个进程拿不到锁直接报
  `instance_active / Another service is using this media workspace`（409）。
- 实测同目录起第二个 Application 被拒（`supplemental-probes.json: instance_lock`）。
- 含义：
  - **不要在同一数据目录上并起两个服务**；恢复演练要在**新目录**进行。
  - 锁是咨询性的，进程正常/异常退出后锁自动释放；若确认没有任何存活进程却仍报 `instance_active`，先排查是否有残留进程/容器挂载，而不是删锁文件硬来。
- 这把锁也保证“启动恢复 running→failed”只由该存储的唯一属主执行，避免两个进程把同一任务状态反复改判（与 `PROJECT_SPEC.md` 第 9 行描述一致）。

---

## 9. 能证明恢复成功的用户结果（验收清单）

恢复/重试完成后，**逐条拿到下面这些用户可见证据，才算成功**；本次演练全部实际取得（见 `drill-results.json`、`drill-timeline.log`）：

1. **健康与恢复计数**：`GET /health` 中 `database:"ok"`，且能解释 `recovery.interrupted` 的数值（异常中断后应 ≥1，正常关停后为 0）。
2. **资源一致**：`GET /resources` → `consistent:true`、`missing:[]`、`bad_size:[]`。注意这只覆盖存在性/大小（第 7 节）。
3. **任务终态正确**：原中断票经 `retry` 后 `state:"succeeded"`、`attempt` 递增、`fingerprint` 与中断前一致（实测 `attempt:2`、`fingerprint_unchanged:true`）。
4. **成片全解码验规通过**：`GET /artifacts/{id}/conformance` → `valid:true`，六项检查（duration、audio_format、video_dimensions、frame_rate、full_decode、monotonic_frames）全过（实测见 `conformance_checks`）。
5. **下载字节可核对**：`GET /artifacts/{id}/content` 下载后本地 sha256 等于登记 blob 哈希（实测 95367 字节、`download_sha256_matches:true`）。
6. **印本自校验通过**：`GET /imprints/{id}/verify` → `valid:true`、列出 `verified_files`（实测 `imprint_verify:true`）。
7. **血缘闭包完整**：`GET /tickets/{id}/lineage` 节点覆盖 source_media、blob、reel_revision、look_version、render、rendition、imprint，且无断边（实测 7 类节点、8 条边）。

只满足“服务起来了、数据库 ok、票状态看着对”而拿不到 4–7 的，**不得判定恢复成功、不得对外交付**。

---

## 10. 无法确认媒体完整性时：保留什么证据、在哪里停手

当出现 `blob_corrupt`、`bad_size`、来源原始文件丢失、或你无法确认某 blob 是否为原字节时：

### 10.1 立即停止的动作

- 停止对涉及票的 `retry`、停止任何印本打包与对外下载/交付；
- **不要**手工编辑/替换 `blobs/` 里的文件、不要改 SQLite、不要删 `scratch/`；
- **不要**跑 `sweep`（`dry_run:false`），避免在定性前改变现场；
- 不要在该数据目录上再起第二个进程（实例锁，第 8 节）。

### 10.2 必须保留/收集的证据（原地保留，另复制一份到故障目录）

1. 出问题时刻的 `GET /health`、`GET /resources`、相关 `GET /tickets/{id}`、`GET /tickets/{id}/events` 响应（事件流见 `reel_loom/database.py:57-65`，含 queued/started/recovered/failed/succeeded 全过程）；
2. 相关 blob 的：登记哈希（`blobs.hash`）、登记大小（`blobs.size`）、磁盘实际大小、实际重算的 sha256、文件 mtime；
3. 对应 scratch 残留 `loom-XXXX/`（含 `media.*`）与服务日志；
4. 若走了 verify/验规，保存完整错误体（`blob_corrupt` / `decode_failed` / `manifest_mismatch` / `rendition_mismatch` 等 code，分别见 `reel_loom/blobs.py:61`、`reel_loom/conformance.py:51`、`reel_loom/imprints.py:76-92`）；
5. 备份目录本身保持原样，作为回退点。

### 10.3 判定与升级路径

- 若能拿到**与登记哈希一致的原始字节**：按 6.5 重新入库（来源）或原单 retry（成片）、重新打包（印本），再用第 9 节清单验收。
- 若**拿不出可证明一致的字节**，或损坏发生在已交付成片/印本上：停在当前状态，带上 10.2 的证据升级给服务负责人/制作方，由其决定重新上传来源、重渲染或召回已交付印本。值班同事不要自行“造”一个哈希文件。
- 判定原则重申：`/resources` 的 `consistent` 不等于字节可信（第 7 节）；只有 verify/conformance 这类全量哈希或全解码通过，才说明媒体真正完整。

---

## 11. 随附实测记录与复现方式

所有结论在隔离临时目录用真实 HTTP、真实 FFmpeg/PyAV 跑过，未修改任何生产代码。证据位于 `docs/handover-evidence/`：

| 文件 | 内容 |
| --- | --- |
| `drill-timeline.log` | 含进行中任务的完整备份→SIGKILL 中断→全量恢复→重试交付→部分复制反例→正常关停对照的时间线 |
| `drill-results.json` | 结构化结果：恢复计数、重试结果、验规/下载/印本/血缘、部分复制失败体、正常关停退出码与终态 |
| `supplemental-probes.json` | 同尺寸字节损坏下 reconcile 与 verify 的差异、实例锁拒绝 |
| `reingest-remediation.json` | 来源 blob 丢失后用相同原始字节重新入库的补救验证 |
| `layout-*.json` | 各阶段目录布局、表计数、票状态、blob 大小与 sha256、scratch 残留 |
| `server-*.log` | 各服务实例的真实 HTTP 访问日志 |
| `scripts/drill.py` | 备份恢复演练驱动（HTTP 驱动 + 文件级备份 + 信号注入） |
| `scripts/probe_integrity.py` | 损坏检测与实例锁探测 |
| `scripts/probe_reingest.py` | 重新入库补救探测 |
| `scripts/slow-ffmpeg` | 仅演练用的 FFmpeg 节流包装，让短渲染保持 running 以便观察 |

复现环境（与 README 一致的 Python 3.11+ / PyAV / FFmpeg 组合，本次实测为 Python 3.11.2、PyAV 16.1.0、imageio-ffmpeg 自带 FFmpeg 7.0.2，linux/arm64）：

```bash
# 1) 准备独立虚拟环境或独立依赖目录，安装 pyproject.toml 依赖（av、imageio-ffmpeg）
# 2) 让仓库与依赖可被 import，并指定 FFmpeg
export PYTHONPATH=/path/to/repo:/path/to/deps
export LOOM_FFMPEG=$(python -c "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())")
# 3) 运行基线工作流（仓库自带，不依赖外部媒体）
python -m reel_loom.demo --workflow recovery
python -m reel_loom.demo --workflow conformance
# 4) 运行交接演练（脚本内数据目录全部在系统临时目录，结束后可整体删除）
python -I docs/handover-evidence/scripts/drill.py
python -I docs/handover-evidence/scripts/probe_integrity.py
python -I docs/handover-evidence/scripts/probe_reingest.py
```

> 演练脚本里的路径（FFmpeg、临时目录）按本次环境写死，换环境时改脚本顶部常量即可；`slow-ffmpeg` 仅用于放大 running 窗口，生产环境不存在该包装。

---

## 12. 关键结论 → 依据索引（便于回溯）

| 结论 | 源码依据 | 实测记录 |
| --- | --- | --- |
| 数据=SQLite(WAL)+blobs+scratch+lock，同目录必须整体备份 | `config.py:37-42`、`database.py:23`、`blobs.py:16-19`、`instance.py:7-17` | `layout-*.json` |
| 内容先落盘 fsync 再同事务登记 | `blobs.py:37-50`、`assets.py:17-27` | `reingest-remediation.json` |
| 渲染读取来源强制哈希校验 | `rendering.py:23`、`blobs.py:52-62` | `supplemental-probes.json` |
| 成片与 succeeded 在同一事务原子提交 | `artifacts.py:10-23` | `drill-results.json: retry_outcome` |
| 异常中断 running 重启改判 worker_interrupted | `tickets.py:103-112`、`application.py:47` | `drill-timeline.log:7-13` |
| 正常关停仅 SIGINT，在途票落 cancelled、interrupted=0 | `server.py:16-18`、`rendering.py:62-66` | `drill-timeline.log:27-30`、`server-graceful.log` |
| retry 复用冻结快照、fingerprint 不变 | `tickets.py:94-101`、`planner.py:11-29` | `drill-results.json: retry_outcome` |
| 只复制 SQLite：health 正常但读取报 not_found | `schema.py:8,45,55`、`blobs.py:52-55` | `drill-results.json: partial_*` |
| reconcile 只查存在/大小，同尺寸损坏查不出 | `resources.py:24-44` | `supplemental-probes.json` |
| verify/conformance 才做全量哈希/全解码 | `blobs.py:56-61`、`conformance.py:36-54`、`imprints.py:71-95` | `supplemental-probes.json`、`drill-results.json` |
| 印本 verify 重算包内媒体哈希并比对 manifest | `imprints.py:71-95` | `drill-results.json: imprint_verify` |
| 实例锁拒绝同目录第二进程 | `instance.py:10-17` | `supplemental-probes.json: instance_lock` |
| 重新上传相同字节可恢复缺失来源且不产生重复行 | `blobs.py:37-45`、`assets.py:19-25` | `reingest-remediation.json` |
| scratch 仅临时半成品、无 DB 登记、非万能清理项 | `blobs.py:21-24` | `layout-primary-after-sigkill.json`（count_artifacts:0） |
| sweep 仅清无引用 blob 且默认 dry_run | `resources.py:8-22` | 基线 `demo.py:147-154` |
