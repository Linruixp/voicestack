# VoiceStack 会议体验改进 — 设计文档

- Status: Draft for review（待用户复审）
- Date: 2026-10-02
- Scope: `speaker-service` 的「会议录音 → 转写 → 说话人识别 → 声纹库管理」在 **OpenCode(Agent) ↔ 本地 Web UI** 之间的配合体验
- Platform: 本地、loopback、vanilla JS（无框架/无构建）

---

## 1. 背景与目标

### 1.1 现状（代码事实）

- Web UI 是 `speaker-service/ui_static/` 的 vanilla 单页，5 个 view（upload / transcript / assign / speakers / meetings），无路由、无框架；由 `ui.py` 在 `GET /` 提供并签发 HttpOnly session cookie。
- `OpenCode` 与 Web UI **零交接**：`transcribe_meeting` 只返回 `meeting_id / segments / unknown_clusters`，不含任何 UI URL 或信号；唯一入口是手动执行 `bin/vs-web.sh`。
- 会议标题来自文件名 stem，**不可编辑**（无 `update_meeting`、无 `PATCH /meetings/{id}`、无 MCP `rename_meeting`）。
- `meetings` 表只有 `id/title/date/audio_path/created_at`；**`date` 恒为 NULL**（pipeline 未传），无 `location/summary/topic/participants`。
- 转写页顶部只有一行 `标题 · speakers · N unknown`，无时间/地点/摘要。
- 说话人字段仅 `name/organization/notes`，**无职务/title**。
- `re_enroll_required` 只有信号，**无解决路径**。
- 项目**无任何 LLM**（所有 `mlx` 命中是 `mlx-whisper` ASR）。

### 1.2 目标

1. 当一次转写产生较多未知说话人时，Agent 在**征得同意**后**自动拉起** Web UI，并定位到对应会议的「说话人命名向导」；用户在 UI 内**试听音频片段**后完成命名/关联；结果回流，Agent 继续。
2. **说话人命名与声纹库管理全部在 Web UI 完成**（因判断说话人必须试听）。
3. 会议详情页在转写正文之前展示「会议介绍」：可编辑标题 + 时间/时长/地点/参会人 + 摘要。
4. 会议历史可管理（标题可编辑、搜索/筛选）。
5. 本地中文会议摘要（P2）。

### 1.3 非目标

- 不迁移/重写前端为 React（保持 vanilla）。
- 不做实时/边录边转。
- 不在本期做云端 LLM。
- 不改动只读参考 `VoiceStudio-src/`。

---

## 2. 已锁定的决策

| # | 决策 | 值 |
|---|---|---|
| D1 | 说话人命名 + 声纹库管理位置 | **全部在 Web UI**（含音频试听） |
| D2 | 前端平台 | **vanilla**（沿用 `ui_static/`，无框架/构建） |
| D3 | 交接触发方式 | **自动拉起** UI + 终端明确提示用户转到 UI |
| D4 | 摘要执行位置 | **本地**（Ollama + **Qwen3.5-9B**） |
| D5 | `ui_url` 主机/端口 | **从配置读**，默认 `http://127.0.0.1:3910` |
| D6 | 声纹默认保留期 | **12 个月**，到期提示复核 |
| D7 | 分期 | **方案 A**：P1 = schema v2 + 深链交接 + 带试听的命名向导 + Intro Header + 可编辑标题；P2 = 本地摘要 + 历史管理 + 声文库治理 |
| D8 | 大文件下载（>1GB，含运行时/模型/依赖） | **下载前必须暂停提示用户**；用户切国内网络走镜像，下完通知用户切回国际网络 |

### 2.1 外部最佳实践依据（简）

- 「先答案、后原文」：主流工具（Otter/Fireflies/Granola/Circleback/Teams/Zoom/Notion/Jamie）都把摘要/决策/行动项/章节置于转写之上。
- 说话人命名要先**放一段语音样本**再让人命名（Descript 的 Identify-speakers 弹窗为模板）；「应用到所有同名 cluster」；合并需**声纹相似度护栏**（不能只按名字）。
- **enrollment 是同意闸门**，不是命名的副作用；声纹属受管制生物特征数据（GDPR Art.9 / BIPA / CUBI / 华盛顿）。
- HITL：按 `置信度 × 风险 × 可逆性` 分流；**本地化不确定性**；**让用户改而不是拒**；**预填草稿**；**处处可取消**。
- 交接：**服务端为唯一事实源**；深链只传句柄不传 PII；`pending` 状态持久化；恢复语义幂等。

---

## 3. 架构总览

```
① 用户(OpenCode): "转写这个会议录音"
        │  MCP: vs-speaker.transcribe_meeting(path)
        ▼
② speaker-service: ASR → diarize → embed → match → 落库
        │  生成 speaker_batch (待处理说话人)
        ▼
③ 返回: meeting_id, segments[], unknown_clusters[], ui_url, handoff{needed,batch_id,count}
        │
        ▼
④ Agent: 若 handoff.needed → open <ui_url>  (自动拉起)
        终端打印: "识别出 N 个新说话人，已打开声纹管理界面，请在浏览器中完成命名。"
        │
        ▼
⑤ WebUI: 深链进入 → 命名向导 (试听→命名/关联/跳过→是否记住声纹)
        │  POST /speaker-batches/{id}/resolve
        ▼
⑥ speaker-service: 执行 enroll/attach/skip，批次→resolved，广播事件
        │
        ▼
⑦ Agent: 监听 SSE GET /events (或轮询 GET /speaker-batches/{id}) 感知完成 → 继续
```

**事实源**：`speaker-service`（registry）。Agent 与 UI 均为 HTTP 客户端。MCP 仍是薄代理，不直接打开 DB。

---

## 4. 数据模型（schema v2）

`registry_schema.py` 当前 `SCHEMA_VERSION = 1`，`_migrate()` 仅处理 `from_version < 1`。本期升到 **v2**：迁移前已自动备份（既有 `_backup` 逻辑），迁移函数扩为 `2`。

### 4.1 meetings 扩展

```sql
ALTER TABLE meetings ADD COLUMN topic       TEXT;
ALTER TABLE meetings ADD COLUMN location    TEXT;
ALTER TABLE meetings ADD COLUMN duration_s  REAL;
ALTER TABLE meetings ADD COLUMN summary_json TEXT;   -- P2 填充；P1 预留
ALTER TABLE meetings ADD COLUMN original_title TEXT; -- 可编辑标题时保留原始（文件名 stem）
```

- `date` 不再恒 NULL：转写时若无显式值，用上传文件 `mtime`，否则 `created_at`。
- `duration_s` 由最后一段 `end` 得到。

### 4.2 speakers 扩展

```sql
ALTER TABLE speakers ADD COLUMN title TEXT;  -- 职务/角色
```

### 4.3 新增：交接批次（P1）

```sql
CREATE TABLE IF NOT EXISTS speaker_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'open'
        CHECK (state IN ('open', 'resolved', 'dismissed')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_speaker_batches_meeting ON speaker_batches(meeting_id);

CREATE TABLE IF NOT EXISTS batch_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES speaker_batches(id) ON DELETE CASCADE,
    cluster_id INTEGER NOT NULL UNIQUE REFERENCES clusters(id) ON DELETE CASCADE,
    suggested_speaker_id INTEGER REFERENCES speakers(id) ON DELETE SET NULL, -- top-1 建议（不强采用）
    similarity REAL,
    resolution TEXT CHECK (resolution IN ('enrolled','attached','skipped')),
    resolved_speaker_id INTEGER REFERENCES speakers(id) ON DELETE SET NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_batch_items_batch ON batch_items(batch_id);
```

### 4.4 新增：同意记录（P2，但 P1 的勾选需写入）

```sql
CREATE TABLE IF NOT EXISTS enroll_consents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    granted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    purpose TEXT NOT NULL,
    retention_until TEXT NOT NULL,       -- granted_at + 12 个月（可配）
    source_batch_id INTEGER REFERENCES speaker_batches(id) ON DELETE SET NULL,
    revoked_at TEXT
);
```

> 设计取舍：`clusters` 现有 `state ∈ unknown|named|attached` 保持不变；批次 item 自带 `resolution`，不新增 cluster 状态，避免多状态源。

---

## 5. 交接协议（§核心）

### 5.1 触发

- `config.py` 新增 `handoff_threshold: int = 3`（`VASTACK_HANDOFF_THRESHOLD`）：`unknown_clusters.length >= 阈值` 时认为 `handoff.needed = true`。
- 转写完成后自动创建一条 `speaker_batches`（`state='open'`）及其 `batch_items`。
- `batch_item.suggested_speaker_id/similarity`：若 match 有 top-N（低于阈值）候选则填入，供 UI 预填；**绝不自动采用**。

### 5.2 返回给 Agent 的契约

`transcribe_meeting`（MCP）与 `POST /meetings`（HTTP）的返回新增：

```jsonc
{
  "meeting_id": 12,
  "segments": [ ... ],
  "speakers": [ ... ],
  "unknown_clusters": [ ... ],
  "ui_url": "http://127.0.0.1:3910/?meeting=12&task=speakers&batch=7",
  "handoff": { "needed": true, "batch_id": 7, "unknown_count": 4, "threshold": 3 }
}
```

- `ui_url` 由 `settings.ui_base_url`（默认 `http://127.0.0.1:{port}`，D5）拼接，仅含句柄。
- 无未知说话人时 `handoff.needed=false`，不建批次。

### 5.3 Agent 行为（OpenCode 侧）

- 新增 MCP 工具 `open_speaker_ui(meeting_id: int | None)`：确保服务在线（`vs-speaker-up.sh`）后 `open` 深链（复用 `vs-web.sh` 逻辑）。
- Agent 在 `handoff.needed` 时调用它（自动拉起，D3），并在终端打印提示。
- 结果回流：Agent 通过 `GET /events`（SSE）或轮询 `GET /speaker-batches/{id}` 感知 `state='resolved'` 后继续。

### 5.4 事件流（P1）

- `GET /events`：SSE，广播 `{type:"speaker_batch", batch_id, state}` 等事件；仅供本地 UI/Agent 客户端。
- 退化为轮询 `GET /speaker-batches/{id}`（幂等）。

---

## 6. 说话人命名向导（§核心，UI，P1）

### 6.1 音频片段能力（前置件）

- 端点 `GET /clusters/{cluster_id}/sample`：返回该 cluster 的代表性短音频片段（取最长 1–3 段发言，拼成约 10–20s）。
- **切片实现**：优先复用管线现有音频解码路径生成短 WAV，缓存于 `data_dir/samples/`；若解码不可行，回退为对原文件做 HTTP Range 流式播放 + 客户端 `currentTime` seek（`<audio>` 原生支持）。
- 该端点受 `require_read_auth` 保护（与其它读一致）。
- 实现时需确认现有音频解码依赖（`asr.py`）是否可直接切片；列为风险 R2（见 §12）。

### 6.2 向导视图（`view-wizard`，新增）

- 深链 `/?meeting={id}&task=speakers&batch={batch_id}` → `init()` 解析 query，加载批次并 `show("wizard")`。
- 交互：**一次一个 cluster**（渐进式）：

```
┌ 说话人命名 · 3 / 6                         [稍后继续] ┐
│ ● SPEAKER_03   时长 4:12 · 发言 7 段                     │
│   ▶ ━━━━━━━━━●━━━━━━━  [试听样本] ♻  (原生 <audio>)      │
│                                                          │
│ ○ 关联已有说话人   [ 搜索姓名…        ▾ ]                 │
│ ○ 新建说话人      姓名[__] 单位[__] 职务[__]              │
│ ○ 跳过                                                    │
│                                                          │
│ ☐ 同时记住此声纹用于以后会议                              │
│   └ 同意说明（见 §7.3），留空则仅标本次转写                │
│                                                          │
│ [上一步]                            [完成并继续]          │
├ 建议候选: 张三 62% · 李四 55%  (来自 match top-N，仅建议) ┤
└──────────────────────────────────────────────────────────┘
```

- 顶部进度；「稍后继续」退出但批次已持久化。
- 提交：`POST /speaker-batches/{id}/resolve`，体为逐条决策数组；服务端执行 enroll/attach/skip 并置批次 `resolved`。
- 完成后回到 transcript 视图，unknown cluster 就地变姓名。
- 保留既有 `assign` 视图（合并/拆分/纠错）作为高级入口，二者不冲突。

### 6.3 合并 / 拆分护栏

- 合并（UI 既有）：选两个 cluster → 若**声纹相似度低**则红色告警并要求二次确认（防只按名字误并）。
- 拆分（UI 既有）：可重切时间边界。
- `re_enroll_required`：在向导/声文库显示 `⟳ 需要重新登记` 徽标，引导重新采样（复用某场会议最长片段）。

---

## 7. Intro Header、可编辑标题、声文库（P1 / P2）

### 7.1 Intro Header（P1：元数据 + 标题；摘要位 P2 填充）

布局（置于转写正文之上）：

```
[标题（点击可编辑）]                     [Summary | Transcript]
时间 · 时长 · 地点 · 参会人(未确认者打问号)
──────────────────────────────────────────────
TL;DR …                       ← P2 前显示"尚未生成摘要 [生成]"
关键决策 / 行动项 / 章节        ← P2
──────────────────────────────────────────────
[原始转写]
```

- P1 先呈现：可编辑标题、时间（`date`）、时长（`duration_s`）、地点（`location`）、参会人（`meeting_detail.speakers` + 未确认标记）。`location`/`topic` 一期仅支持手动编辑（无外部日历来源）。
- 摘要区域 P1 占位，P2 由本地摘要填充（见 §8）。

### 7.2 可编辑标题（P1）

- 后端：`registry_meetings.update_meeting(meeting_id, *, title?, topic?, location?)`；路由 `PATCH /meetings/{id}`（`require_mutation_auth`）；`api_schemas.MeetingPatch`。
- 首次重命名时写入 `original_title`（保留原名）；不覆盖用户已改标题。
- UI：详情页标题 inline 编辑；会议历史行亦可重命名。
- MCP：新增 `rename_meeting(meeting_id, title)`。

### 7.3 声文库管理（P1 基础 / P2 治理）

- P1：说话人字段增加 `title`（职务）；向导可搜索/关联已有、可新建（姓名/单位/职务）。
- P2（治理）：
  - 目录增强：关联会议数、最后使用、声纹质量、登记日期；**试听声纹样本**；删除/导出/重新登记。
  - 同意与保留（D6）：每次 enrollment 落 `enroll_consents`；保留期默认 12 个月；删除/导出可达；到期提示复核。
  - **同意文案（草稿，待用户确认口径）**：
    > 「勾选后，将保存此人的声纹（属于生物特征数据），用于在以后的会议中自动识别其发言。保存期限默认为 12 个月，你可以在「声文库」中随时试听、导出或删除，也可撤销同意。未勾选时仅用于本次转写，不保存声纹。」
  - **不静默登记第三方**：从录音给未同意者建声纹必须走向导的显式勾选。

---

## 8. 本地会议摘要（P2）

```
SummarizerBackend (可插拔)
  runtime : Ollama(默认) | mlx-lm(可选)       ← 借鉴 VoiceStudio-src provider 架构
  model   : qwen3.5:9b (默认) | qwen3:14b (可选高保真)
  约束    : Ollama format=<JSON schema> → Pydantic 校验（可靠性关键）
  长会议  : > ~32k token 时 章节 map → 汇总 reduce
  输出    : meetings.summary_json =
            { tldr, decisions[], action_items[{text,owner,due}], chapters[{title,start}] }
            每条带 source_segment_ids（可回链、防幻觉）
  执行    : 异步任务 + 进度 + 可编辑 / 重新生成
```

- 运行时来源：新增独立依赖与配置（`VASTACK_SUMMARY_MODEL`、`VASTACK_OLLAMA_BASE_URL`）；默认 `http://localhost:11434/v1`。
- 防幻觉：每条决策/行动项**必须带转写引用**；无负责人写"待定"；UI 标注"AI 生成，可编辑"。
- **下载约束（D8）**：安装 Ollama、拉取 Qwen3.5-9B（约 6.6GB）前必须暂停提示用户切国内网络/镜像，完成后再通知切回。
- 性能预期（M4/24GB，参考）：20k token 首 token ~1.5–2 分钟 → **按异步体验设计**（进度、可离开、完成推送）。

---

## 9. 会议历史管理（P2）

- 列表行：标题、日期分组（今天/昨天/具体日）、时长、参会人、标签、`有未确认说话人` 标记。
- 搜索：SQLite **FTS5** 覆盖 标题 + 摘要 + 转写全文。
- 筛选：参会人 / 日期区间 / 标签 / 是否有未解决说话人。
- 组织：文件夹 **和** 标签并存。
- 后端：`list_meetings` 加查询参数 + FTS 虚拟表。

---

## 10. HTTP API 变更清单

| Method | Path | 本期 | 说明 |
|---|---|---|---|
| GET | `/meetings/{id}` | P1 | payload 扩展：topic/location/duration_s/summary_json/original_title |
| PATCH | `/meetings/{id}` | P1 | 编辑 title/topic/location（`MeetingPatch`） |
| GET | `/clusters/{id}/sample` | P1 | cluster 代表性短音频片段 |
| GET | `/speaker-batches/{id}` | P1 | 批次详情（items + 建议 + 状态） |
| POST | `/speaker-batches/{id}/resolve` | P1 | 提交逐条决策，执行 enroll/attach/skip |
| GET | `/events` | P1 | SSE 事件流（本地客户端） |
| GET | `/speakers` | P1 | payload 增加 `title`、`voiceprint_count` 等 |
| GET | `/meetings` | P2 | 查询参数（q/tag/participant/date/unresolved） |
| POST | `/meetings/{id}/summary` | P2 | 触发/重新生成摘要 |

所有变更路由沿用 `require_read_auth` / `require_mutation_auth`（cookie 变更走 Origin CSRF 检查）。

---

## 11. MCP 工具变更

| 工具 | 本期 | 说明 |
|---|---|---|
| `transcribe_meeting` | P1 | 返回新增 `ui_url` + `handoff{}` |
| `open_speaker_ui(meeting_id?)` | P1 | 新增：确保服务在线并 `open` 深链（自动拉起，D3） |
| `rename_meeting(meeting_id, title)` | P1 | 新增 |
| `list_speakers` | P1 | payload 增加 `title` |
| `merge_speakers` / `split_cluster` | P2 | 新增（现仅 HTTP/UI），供 Agent 闭环修正 |
| `resolve_re_enroll` | P2 | 新增 |

`mcp_server.py` 仍是薄代理；`service_client.py` 增加对应映射。`tests/test_guide.py` 的 MCP 清单断言需同步更新。

---

## 12. 分期路线图（方案 A）

### P1（本期）
1. schema v2 迁移（§4）。
2. 交接批次 + 深链 + 自动拉起 + SSE（§5）。
3. 带**音频试听**的说话人命名向导（§6）。
4. Intro Header（元数据）+ 可编辑标题（§7.1–7.2）。
5. 说话人 `title` 字段 + 向导搜索/关联/新建（§7.3 前半）。
6. 对应 API/MCP 变更（§10–11）。

### P2
1. 本地中文摘要（Ollama + Qwen3.5-9B）（§8）。
2. 会议历史管理（§9）。
3. 声文库治理：同意/保留、质量、导出、re-enroll（§7.3 后半）。
4. 转写深度体验：播放同步、inline 纠错、章节导航。

### P3
- 更细的转写编辑与章节联动、检索增强。

### 风险
- **R1** 数据迁移：v1→v2 需覆盖老库（含 `date` 回填），迁移前已备份；需新增迁移测试。
- **R2** 音频切片：依赖现有解码路径能否直接切 WAV；否则回退 Range 流播放。
- **R3** 交接时机：自动拉起可能打断用户；默认阈值 3，可配；终端始终给出明确提示。
- **R4** 本地摘要性能：M4/24GB 首 token 慢，需异步 + 进度。
- **R5** 兼容：`ui_static` 的 `data-testid` 被 `tests/e2e/ui_e2e.mjs` 断言，改动需同步。

---

## 13. 测试与验证

- **迁移测试**：构造 v1 库 → 打开 → 断言升到 v2、数据保留、备份生成。
- **批次流程**：转写→建批次→resolve（enroll/attach/skip）→批次 closed → 事件发出。
- **音频片段**：`GET /clusters/{id}/sample` 返回可解码音频、时长在预期范围。
- **可编辑标题**：`PATCH /meetings/{id}` 生效、`original_title` 保留、越权被拒（只读 token）。
- **MCP**：`test_guide.py` 清单与 `opencode.json` 键一致；`transcribe_meeting` 含 `ui_url`/`handoff`。
- **UI e2e**：深链进入向导、试听控件存在、命名后 transcript 就地更新；同步更新 `tests/e2e/ui_e2e.mjs` 的 testid 断言。
- **隐私**：勾选"记住声纹"才写 `enroll_consents` 与 voiceprint；不勾选只标本次。

---

## 14. 兼容与迁移

- `SCHEMA_VERSION` 1→2；`_migrate` 增补 `if from_version < 2: _apply_v2(conn)`。
- 新列均可空，老库读取不破。
- `clusters.state` 语义不变；批次状态独立。
- 旧 UI 行为（assign 视图）保留；新增 wizard 不替换。

---

## 15. 待用户确认 / 未决

- 同意文案口径（§7.3 草稿）是否采用。
- `handoff_threshold` 默认 3 是否合适（可配）。
- 音频切片实现路径（R2）在实现时确认。
- P2 摘要的 Ollama 具体 tag（`qwen3.5:9b` 的量化版本）在实现时按本机内存确认。
