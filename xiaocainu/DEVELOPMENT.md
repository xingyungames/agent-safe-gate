# 小财奴（xiaocainu）· 开发文档

> 配套专篇：[`SECURITY.md`](./SECURITY.md) —— 3D 安全架构（边界防御 / 凭证隔离 / 数据最小化）与评分映射。
> 本文聚焦：**开发框架 → 运行链路原理 → 目录/文件 → 配置参考 → 运行/测试/部署 → 已知坑**。

---

## 0. 文档导航

| 文档 | 内容 |
|------|------|
| `DEVELOPMENT.md`（本文） | 模块框架、运行链路、目录文件、配置、起停、已知坑 |
| `SECURITY.md` | 3D 安全设计、官方扫描适配清单、评分映射 |
| `knowledge/*.md` | 业务知识库（报销制度 / 填单规则 / 助手话术），A 分支检索源 |

---

## 1. 项目概览

**小财奴** 是面向 BRICS2026-ST-143「AI+应用创新与安全设计」赛项的**企业财务报销助手 Agent**。

- **技术路线**：纯 Python 源码自建（非低代码平台），提交源码供官方自动化安全扫描。
- **评分（100 分）**：场景功能完备 30 + AI 模型优化设计 20 + 安全防护 25 + 创新 10 + 展演 15。
- **硬约束**：① 源码提交 + 官方自动化安全扫描（6.3 一票否决）；② 须集成 **≥2 类不同 AI 模型**（本地/云端均可，**本地 Ollama 可省**）；③ 3D 安全。
- **四条业务分支**：
  - **A 政策咨询**：检索增强（BM25）+ LLM 问答。
  - **B 填单引导**：本地正则抽取 + 多轮追问 + 飞书回写。
  - **C 合规预审**：本地规则判定（合规/风险/不合规）+ 合规时回写飞书。
  - **D 进度查询**：经主服务器查报销单（按 申请人 / 单号），按提交时间倒序展示最近 50 条。

> 模型要求提示：BM25 **不算 AI 模型**；RAG 类要落到「真·AI 调用」（reranker / embedding）才算一类。当前用 **云端 LLM（DeepSeek/Agnes）+ 可插拔 reranker** 即可满足 ≥2 类，无需本地模型。

---

## 2. 技术栈与运行环境

| 项 | 说明 |
|----|------|
| 语言 | Python 3.13（受管环境） |
| Web 框架 | FastAPI（仅 M5 网关）+ pydantic JSON 契约 |
| 依赖最小化 | `requirements.txt` 仅 4 个：**openai / fastapi / uvicorn / httpx**；M1–M4、M7 全为**标准库实现**（无 requests / chromadb / jieba） |
| 前端 | `frontend/index.html` 单文件：原生 HTML + JS，无构建步骤、无 npm |
| 飞书 | 纯 `urllib` 实现 Bitable 客户端（不引 requests） |

### ⚠️ 解释器（关键坑）
- 本机 `python` 在终端解析到**受管 venv** `C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe`（已装 openai/fastapi/uvicorn/httpx），**这是跑 `api.main` 唯一可用的解释器**。
- 受管基础解释器 `...versions\3.13.12\python.exe` **缺 pydantic**，用它起网关会 `ModuleNotFoundError: No module named 'pydantic'` 直接崩。
- `run_server.bat` 已硬编码指向 venv 解释器，免 PATH 歧义。

### ⚠️ 网络代理坑
- 本机有本地代理 `http_proxy=127.0.0.1:54684`。本地 `curl` 测网关需加 `--noproxy '*'`，否则代理返回 502。

---

## 3. 目录结构与文件清单

```
xiaocainu/
├── api/                      # M5 API 网关
│   ├── __init__.py
│   ├── main.py               # FastAPI 应用 + pydantic 契约 + dispatch（认证墙 + 守卫 + 分支路由）
│   ├── auth_service.py       # 账号编排：调主服务器登录/查身份 + 身份缓存
│   ├── backend_client.py     # 主服务器适配器（无状态透传 + 请求签名 + 错误分类）
│   └── session.py            # SessionStore：内存级多轮会话 + 跨轮草稿，TTL 过期不落库
├── models/                   # M1–M4 核心逻辑（纯 stdlib 为主）
│   ├── __init__.py           # 包导出 + 自动 load_env()
│   ├── config.py             # 模型 provider 配置解析（agnes/deepseek/ollama）+ knowledge_dir
│   ├── env.py                # 极简 .env 加载器（stdlib，无 python-dotenv）
│   ├── llm.py                # M1 LLM 适配器（OpenAI 兼容，可切换 provider）
│   ├── retriever.py          # M2 BM25 检索（标题切块 / 相关度截断 / 可插拔 rerank·compress / category）
│   ├── feishu.py             # M3 飞书 Bitable 客户端（urllib）+ 枚举常量 + 发票附件上传（不建单不查单）
│   ├── actor.py              # M8 登录主体与权限键（身份来自主服务器）
│   └── agent.py              # M4 编排路由层（XiaoCaiAgent：A/B/C/D 路由 + 抽取 + 软提示）
├── security/                 # M7 边界防御
│   ├── __init__.py
│   └── guard.py              # sanitize_text / check_injection / RateLimiter（纯 stdlib）
├── frontend/
│   └── index.html            # M6 自包含商务风聊天 UI（连 /chat，GET / 由网关托管）
├── knowledge/                # A 分支检索知识库（Markdown）
│   ├── 报销制度.md           # 报销制度 L3（合规阈值来源）
│   └── 示例_填单规则.md       # 填单规则示例（抽取契约 + 枚举）
├── tests/                    # 单测（离线、零付费 API）
│   ├── test_agent.py  test_agnes_live.py  test_api.py  test_feishu.py
│   └── test_frontend.py  test_llm.py  test_retriever.py  test_retriever_enhanced.py  test_security.py
├── .env.example              # 配置模板（复制为 .env 填值；.env 不入库）
├── .env                      # 真实配置（被 .gitignore 忽略，切勿提交）
├── .gitignore
├── requirements.txt          # 4 个运行依赖
├── run_server.bat            # M5 网关启动器（硬编码 venv 解释器）
├── run_live.py               # 本地实时联调脚本（交互式验证 B/C/D 飞书链路）
├── live_probe_write.py       # 一次性飞书探针（调试用，非交付主体）
├── SECURITY.md               # 安全专篇
└── DEVELOPMENT.md            # 本文
```

---

## 4. 模块框架（M1–M9）

| 模块 | 文件 | 职责 | 依赖 | 关键 API |
|------|------|------|------|----------|
| **M1 LLM 适配层** | `models/llm.py` | 统一封装 Ollama(本地)/DeepSeek(云端)/Agnes(云端)；OpenAI 兼容 | openai（真实调用时） | `LLMAdapter.from_env()` / `.chat(...)`；`MockProvider`（测试） |
| **M2 检索增强** | `models/retriever.py` | BM25 检索：标题切块 → 倒排 → top-k + 相关度截断；可插拔 rerank/compress；category 路由 | 纯 stdlib | `Retriever.from_directory()` / `.query()`；`format_context()` |
| **M3 飞书 Bitable** | `models/feishu.py` | 纯 urllib 客户端：token-swap-first 鉴权、批量写、列表查、单条查、**单条更新**、**字段探测缓存**；报销单 schema + `build_reimbursement_record` + `public_view` + `filter_by_schema` | 纯 stdlib | `FeishuBitableClient.from_env()` / `.create_records()` / `.list_records()` / `.get_record()` / `.update_record()` / `.field_names()` |
| **M4 编排路由** | `models/agent.py` | `XiaoCaiAgent`：意图识别 → A/B/C/D 路由；B/C/D 本地正则抽取（零 API 成本）、多轮草稿合并、软提示；**M8 角色上下文**（C 闸/B 带身份/D 强制查自己） | M1+M2+M3+M8 | `XiaoCaiAgent.route(question, history, base_draft, user)` / `.classify_intent()` / `_extract_fields()` |
| **M5 API 网关** | `api/main.py` `api/session.py` `api/auth_service.py` | FastAPI：JSON 契约（入口防注入）+ 守卫集成 + 分支路由 + 内存会话 + 前端托管 + **认证墙** | fastapi/uvicorn | `dispatch()`（与框架解耦，可单测）；`SessionStore`；`AuthService` |
| **M6 前端** | `frontend/index.html` | 原生聊天 UI：渲染 answer / D 记录卡片；**登录遮罩 + 角色 chip + 401 拦截**；只连同源网关 | 无 | 调 `/login` `/logout` `/chat` |
| **M7 安全守卫** | `security/guard.py` | 输入净化 + prompt 注入拦截 + 按会话限流（双闸第二道）；`would_allow` 只读预检供登录限流复用 | 纯 stdlib | `sanitize_text()` / `check_injection()` / `RateLimiter.allow/would_allow` |
| **M8 账号认证** | `models/actor.py` `api/auth_service.py` | **账号唯一来源是主服务器 C#**：网关不再保存任何账号库，登录只调 `POST /api/auth/login`，请求校验只调 `GET /api/auth/me`（按令牌摘要缓存 + 失效负缓存）；角色判断一律看主服务器下发的权限键 | 主服务器 HTTP | `Actor` / `build_actor()` / `Actor.can()`；`AuthService.login/authenticate/logout` |
| **M9 发票 OCR** | `models/ocr.py` + 网关 `/ocr` + `models/feishu.upload_attachment` | **DeepSeek 视觉多模态**（`deepseek-flash`，OpenAI 兼容 image_url base64）：发票图 → 结构化字段（金额/开票日期/号码/销售方/类型/城市，枚举白名单校验）；确认提交后图片经 stdlib multipart 上传飞书「发票附件」列与单据关联；图片仅内存中转不落盘 | openai（视觉调用） | `InvoiceOCREngine.extract()` / `MockOcrEngine`；`build_invoice_preview()`；`FeishuBitableClient.upload_attachment()`；`POST /ocr` |

**设计原则**：
- M1–M4、M7、M8 全部 stdlib，便于源码审查/扫描通过、部署稳。
- 依赖隔离：核心模块优先 stdlib；仅 M5 网关用 FastAPI 做 JSON 契约与入口防注入。
- 离线可测：测试注入 `MockProvider` + `FakeTransport` + 内存 retriever + **Fake 用户表/FakeAuth**，不碰任何付费 API。
- M8 fail-closed：网关认证未配置时受保护端点一律 401，**无匿名旁路**（与 M3 飞书缺配置降级策略不同，认证宁可拒绝服务）。

---

## 5. 运行链路原理

### 5.1 整体架构

```
                          ┌──────────────────────────────────────────────┐
   浏览器/外部 ──HTTP──▶   │  M5 API 网关 (FastAPI)                        │
                          │   ① pydantic JSON 契约（422 拒非法）  —— 闸1  │
                          │   ② dispatch:                                 │
                          │      sanitize_text → check_injection →         │
                          │      RateLimiter.allow            —— 闸2(M7)  │
                          │   ③ SessionStore（内存：history + draft）      │
                          └───────────────┬──────────────────────────────┘
                                          │ agent.route(text, history, draft)
                          ┌───────────────┴──────────────────────────────┐
                          │  M4 XiaoCaiAgent                              │
                          │  classify_intent: D > C > A(疑问式) > B > A   │
                          └───┬───────┬──────────┬───────────┬───────────┘
              A 咨询          B 填单      C 合规       D 进度
         ┌──────┐      ┌─────────┐  ┌─────────┐  ┌─────────┐
         │ M2   │      │ _extract│  │ _extract│  │ _extract│
         │ BM25 │      │ +追问   │  │ +规则   │  │ +查飞书 │
         │ +M1  │      │ +写飞书 │  │ +写飞书 │  │ list/匹配│
         │ LLM  │      └────┬────┘  └────┬────┘  └────┬────┘
         └──┬───┘          │            │            │
            │              ▼            ▼            ▼
            │         M3 飞书表    M3 飞书表    M3 飞书表
            │        (create)     (create)      (list/get)
            └────────────────────────────────────────────
```

### 5.2 `/chat` 标准链路（逐步）

1. 前端 `POST /chat {session_id?, text}`。
2. **闸 1（契约层）**：`ChatRequest` pydantic 校验（`text` 1–2000 字等）。越界直接 **422**，不进业务。
3. **闸 2（语义层，M7）** `dispatch()` 顺序执行：
   - `sanitize_text(text, 2000)`：剥离 NUL/控制字符、去首尾空白、超长截断。
   - `check_injection(text)`：命中注入签名 → **400**（绝不进 LLM）。
   - `RateLimiter.allow(sid)`：单会话 60s/30 次 → 超限 **429**。
4. 取 `SessionStore.get_messages(sid)`（多轮 history）+ `get_draft(sid)`（B/C 跨轮草稿）。
5. `agent.route(text, history, draft)` → `classify_intent` → 分支处理。
6. 返回 `res`（含 `intent` / `answer` / `draft` 等）。`dispatch` 把本轮 user/assistant 写回 SessionStore；若 intent==B 则存 `draft` 供跨轮补全。

### 5.3 四分支处理细节

| 分支 | 入口 | 核心动作 | 是否调模型 | 是否写飞书 |
|------|------|----------|-----------|-----------|
| **A 咨询** | `_handle_consult` | `retriever.query` → `format_context` → `llm.chat`（带 history 多轮） | 是（仅 A） | 否 |
| **B 填单** | `_handle_fill` | `_extract_fields`（本地正则）→ `_merge_draft` 跨轮补全 → 缺必填（**申请人/部门/报销类型/金额**）则 `_ask_missing` → 必填齐则进入**「确认前核对」态**（`_ask_confirm` 回显参数 + 票据主要提醒 + 软提醒，`await_confirm=True`）→ 用户回「确认提交」才 `_do_submit`（`build_reimbursement_record` → `create_records` → `_fetch_bill_no` 真实单号回显） | 否（本地规则） | 是（**确认后**） |
| **C 合规** | `_handle_compliance` | `_extract_fields` → `_compliance_check`（阈值规则）→ 合规且信息齐 → 回写飞书（状态 待审核） | 否 | 是（齐后，阻断/风险不写） |
| **D 进度** | `_handle_progress` | 抽 单号/申请人 → 调主服务器 `GET /api/invoice/list`（范围由服务端按角色裁剪） → `_csharp_row_view` 转中文键 → 按提交时间**倒序** → 取最近 `_D_LIST_LIMIT`(50) 条；**`answer` 只给一句摘要**（查到 N 条 + 截断/范围提示），明细统一由前端 `res.records` 卡片渲染，避免同一批单据显示两遍 | 否 | 读 |

### 5.4 意图路由规则（关键词瀑布，零模型调用）

顺序即优先级（先命中先返回）：

1. **D 进度**：含「进度 / 到哪 / 查一下 / 我的报销单 / 审批没 / 到账 / 现在什么状态 / 查询 / 查」。
2. **C 合规**：含「合规 / 预审 / 能不能报 / 有没有风险 / 超标 / 能不能过」。
3. **A 咨询**（疑问式优先于 B）：含「吗 / 呢 / ？ / 多少 / 什么 / 标准 / 规定 / 介绍 / 说明 / 可以 / 能否 / 是否 / 为什么 / 有啥」→ 避免把「XX 报销多少需要审批」误判成填单。
4. **B 填单**：含「填 / 提交 / 上传 / 创建 / 新建 / 开报销单 / 怎么报 / 报一笔 / 帮我报 / 我要报销 / 报销单 / 报销 / 发票」。
5. 兜底 **A**。

> 注意：`classify_intent` 与 `INTENT_RULES` 两处都定义了路由词（前者在生产 `route` 用，后者为文档/备用常量），以 `agent.py` 内 `classify_intent` 实现为准。

#### 5.4.1 B 填单引导：确认前核对 + 提醒分级（UX 行为）

B 分支**不直接落表**，而是两段式：

1. **必填齐 → 确认前核对态**：回显当前已有参数（申请人/部门/报销类型/金额/城市/事由/票据），并提示「报销的主要事情/用途请通过『事由』补充」；草稿置 `await_confirm=True`（上下文粘性保证续答仍留在 B，不被误判成咨询）。
2. **用户回「确认提交」→ 真正写表**：`_is_confirm` 识别确认意图（含否定词优先拦截，如「先不提交」），走 `_do_submit`（合规预审 + 写飞书 + 真实单号回显）。被合规层「阻断」（如 无票）则保留待确认态，便于补正后重确认。
3. **无票软确认（C 方案，不硬拦截）**：确认提交时若合规层判「阻断」且为无票——第一次先回 `_ask_no_invoice_confirm` 二选一（🧾 回复「有发票了」补票 / 🚀 回复「仍要提交」坚持），草稿记 `no_invoice_warned=True`；用户坚持则放行写表，`_do_submit` 以覆盖参数写入 `合规结果=驳回`、`风险摘要=无票提交（用户确认坚持），财务将驳回`（「驳回」为真实表单选已有选项）。用户回「有发票了」→ 票据状态更新为有票并清除标记，回到正常确认摘要，提交不再标驳回。

**提醒分级**：
- **必填**：申请人 / 部门 / 报销类型(4 类) / 金额 / **事由（2–200 字，与主服务器校验口径一致）** / **票据（必须为「有票」）**。缺任意一项走 `_ask_missing`，**不会带着缺项去调主服务器**。
- **票据双闸**：① `_handle_fill` 把「票据非有票」当缺项，无票/未提都进不了确认卡；② `_do_submit` 再挡一次（`has_invoice != "有票"` 直接返回，不调主服务器）。主服务器与飞书字段不变，仍保留「未提只记风险」的口径，只是网关不再放行。
- **主要提醒（礼貌，区别于软提醒）**：票据状态——「有发票会加快审核进度，无发票需要更多核验时间」（`_invoice_reminder`，在追问与确认两处都出现）。
- **软提醒（非必填，礼貌）**：差旅城市（`_soft_hints`），仅建议补充。
- **事由承接**：追问事由时草稿置 `await_reason=True`；下一句先**剔除已被部门/类型/城市/金额/票据消费掉的词**（`_reason_from_answer`），剩下的才当事由。剔除后不足 2 字则继续追问，**不再整句硬塞**。例：「财务部 交通费 200元 通勤」→ 部门/类型/金额照常入草稿，事由只取「通勤」。
- **票据状态**：**图片证据优先** —— OCR 识别出发票号即判「有票」，即使口述说「没发票」也会被纠正（避免「传了票却被判无票而阻断」）；没识别出票号时才按口述取 有票/无票/未提。
- **确认卡风险前置**：`_ask_confirm` 先跑一次 `_compliance_check`，风险用「⚠️ 风险提示」提前告知，阻断用「🚫」并改用「补齐发票后回复我」收尾（不再邀请「确认提交」），避免提交后才被打回。
- **票据提醒去重**：只有「无票」「未提」才追加 `_invoice_reminder`；已阻断时上面那句已说明原因、不再重复；无票/未提的提醒会引导用户「上传发票，或上传详细的支付凭证（微信/支付宝「账单详情」页）」；「有票」只在「已记录」里出现一次。文案统一不暴露「上传飞书」这类内部实现细节。
- **前端按权限收敛文案**：欢迎语 `welcomeHtml()`、顶部副标题、意图标签 `intentLabel()` 都读 `perms`，没有 `invoice.review` 的账号完全看不到「合规预审」字样（按钮、副标题、欢迎语、气泡标签四处一致）。

### 5.5 飞书写/读链路（含关键坑）

- **写入**：`build_reimbursement_record(**)` → 12 业务字段（**不写「单号」** AutoNumber 只读；**不写「工号」** 真实表无此字段；**不写「审核人」**）→ `create_records` 返回 `record_id` 列表。
- **真实单号回取**：`create_records` 后立即可见单号，但有极短最终一致性延迟。`_fetch_bill_no(rid)` 首查为空会 `time.sleep(0.4)` 重试一次；仍空则返回 `""` → 调用方做**诚实兜底**（"稍后可在进度查询输入姓名查看"），**绝不显示「系统生成中」误导**。
- **⚠️ 单条查询结构坑（已修）**：飞书单条 `get` 返回结构是 `data.record.fields`（**不是** `data.fields`）。`get_record` 已做兼容：优先 `data.record`，异常时退回 `data`。曾因读错层级导致单号恒取不到。
- **读全表**：`list_records` 返回 `data.items[]`（批量 `batch_create` 返回 `data.records[]`，注意两者不同）。
- **内部字段屏蔽**：`public_view(record)` 只保留 `REIMBURSEMENT_FIELDS` 内的业务键，屏蔽 `record_id` 等内部键，聊天层永不外露。

---

### 5.6 M9 发票 OCR 链路（DeepSeek 视觉多模态）

```
📎 选图 → FileReader→base64 → POST /ocr（认证墙+OCR_LIMITER+魔数/8MB 闸）
  → InvoiceOCREngine（deepseek-flash, content 数组+image_url data URL, detail=high）
  → extract_json 健壮解析 → sanitize_fields 枚举白名单校验（脏值置空+warnings）
  → 返回 {fields, preview, usage} → 前端渲染识别卡片 + 自动 send(preview)
  → B 分支 _extract_fields/_merge_draft 预填草稿 → 用户补缺 → 「确认提交」（body 捎 image_b64）
  → _do_submit：发票信息(号码；销售方)随单写入 → upload_attachment(stdlib multipart→file_token)
  → update_record({"发票附件":[{"file_token":…}]}) → 回答附「📎 已存档」
```

关键约束：
- **模型类型账本**：LLM（M1）+ 排序 reranker（M2 可插拔）+ **视觉/OCR（M9）= 3 类 AI 模型**，满足赛项「≥2 类」要求且有富余。
- OCR 结果**只做抽取不做决策**：rtype/city 必须命中 `ALLOWED_TYPES`/`CITY_WHITELIST`，无法确定留空由 B 分支追问；申请人/部门一律以登录身份为准（M8 防冒充）。
- 图片仅内存中转（请求体 → base64 → 内存 bytes），不落本地磁盘；发票原件存用户自有飞书表附件列（数据主权在用户）。
- 计费纪律：仅用户主动上传触发；`usage` 随响应回显；占位 key（含「你的密钥」或长度<20）一律视为未配置，装配 503 而非真发调用。
- 降级链路：附件上传失败/旧表无「发票附件」列 → 仅提示不阻断（单据主流程优先）；旧表无「发票信息」列由 `_fit_schema` 自动剔除。

## 6. 配置参考

### 6.1 `.env` 全部键（模板见 `.env.example`）

| 键 | 作用 | 备注 |
|----|------|------|
| `LLM_PROVIDER` | 选模型：`ollama` / `deepseek` / `agnes` | 改这里即换模型，**零代码改动** |
| `OLLAMA_BASE_URL` / `OLLAMA_API_KEY` / `OLLAMA_MODEL` | 本地 Ollama（默认 `qwen2.5:7b`） | 本地免费，API_KEY 填 `ollama` 占位 |
| `DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL` | 云端 DeepSeek（按量计费） | 比赛前切这个 |
| `AGNES_BASE_URL` / `AGNES_API_KEY` / `AGNES_MODEL` | 云端 Agnes（开发用免费档） | **必须用 `agnes-2.5-flash`**（2.0 已废弃报 No available channel） |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书自建应用凭证 | **敏感，勿提交** |
| `FEISHU_APP_TOKEN` | 多维表格 base id（app_token） | 敏感 |
| `FEISHU_TABLE_ID` | 报销单主表 `tbl...`（例 `<报销单主表_table_id>`；建好 V2 企业级表后换成新表 id） | 敏感表 id |
| `BACKEND_BASE_URL` | **必填**：主服务器地址（如 `http://127.0.0.1:8899`） | 为空 → 认证墙关闭，受保护端点与登录一律 503（fail-closed） |
| `BACKEND_TIMEOUT` | 调主服务器单次超时秒数（默认 20） | 列表拉 500 条时给足时间 |
| `AUTH_LOGIN_LIMIT` / `AUTH_LOGIN_WINDOW` | 登录限流：失败次数上限 / 窗口秒（默认 5 次/300s，**仅失败计数**） | |
| `KNOWLEDGE_DIR` | 知识库目录（默认 `knowledge`） | 可选 |
| `OCR_BASE_URL` / `OCR_API_KEY` / `OCR_MODEL` | **M9 发票 OCR**：默认回落 `DEEPSEEK_BASE_URL/DEEPSEEK_API_KEY`，模型默认 `deepseek-flash`（官方视觉，legacy `deepseek-v4-flash-vision-exp` 自动路由） | 填好 DeepSeek key 即用，无需单独申请 |
| `OCR_DETAIL` | 图片精度 `high/original/low`（默认 `high`，发票文字保真） | 每图计费上限 384 tokens |
| `OCR_MAX_IMAGE_MB` | 原始图片字节上限（默认 8） | 超限 413；魔数白名单 jpeg/png/gif/webp |

### 6.2 入口契约约束（`api/main.py` pydantic，闸 1）

| 模型 / 字段 | 约束 | 防御点 |
|-------------|------|--------|
| `ChatRequest.text` | `min_length=1, max_length=2000` | 防空 / 超长 payload |
| `ReimburseRequest.amount` | `ge=0, le=10_000_000` | 防负数 / 越界 |
| 字符串字段 | `max_length` 8/16/32/200 | 防字段膨胀 |
| `session_id` | `max_length=64` | 防超长 sid |

### 6.3 M7 守卫参数（`security/guard.py`）

| 项 | 默认 | 说明 |
|----|------|------|
| `sanitize_text` 截断 | `max_length=2000` | 与契约层对齐（防御纵深） |
| `INJECTION_SIGNATURES` | 中英越权/指令覆盖短语（如「忽略上面的」「ignore previous」「jailbreak」） | 精确子串、大小写不敏感，降误报 |
| `RateLimiter` | `limit=30, window=60` | 单会话滑动窗口（进程内内存级） |

### 6.4 会话存储（`api/session.py`）

| 参数 | 默认 | 说明 |
|------|------|------|
| `ttl` | 1800 秒（30 分钟） | 无活动会话过期 |
| `max_turns` | 20 | 保留最大消息数（防无限增长） |
| 存储 | 进程内内存字典 | **不落库、不持久化、重启即失**（数据最小化） |

### 6.5 飞书主表 schema（旧表 `<报销单主表_table_id>` 14 字段；**V2 企业级表 17 字段，含完整枚举**）

`事由 / 单号 / 合规结果 / 审核人 / 报销类型 / 记录提交时间 / 状态 / 申请人 / 票据状态 / 部门 / 金额 / 风险摘要 / 城市 / 发票日期`

**V2 新增 3 字段**：`工号`（M8 登录身份自动带出）/ `审批意见`（多行文本）/ `审批时间`（日期）。
完整字段规格、企业标准枚举与 click-by-click 建表手册见 **`docs/TABLE_SETUP.md`（枚举版）**（含用户表 10 字段）。

**V2 企业标准枚举（全字段，详见 TABLE_SETUP.md 〇节总览）**：
- **部门**（V2 由文本升级为单选）：`研发部 / 市场部 / 财务部 / 人力资源部 / 行政部 / 生产部 / 销售部 / 采购部 / 信息技术部 / 质量管理部 / 客户服务部`（11 值，与用户表完全一致）。
- **城市**（V2 由文本升级为单选）：差旅分级城市主数据（一线 4 + 新一线 19 + 二线 14，共 37 城，见 TABLE_SETUP 附表）。
- **报销类型**（单选，4 类）：`差旅费 / 招待费 / 办公用品 / 交通费`（与代码 `ALLOWED_TYPES` 一致）。
- **票据状态**（单选，3 态）：`有票 / 无票 / 未提`。
- **合规结果**（单选）：`通过 / 风险 / 驳回 / 超2000需总经理审批`。
- **状态**（单选）：`待审核 / 审核中 / 已通过 / 已驳回`（B/C 落库默认「待审核」）。
- **金额**：货币 ¥，`0.01–100000.00`，`>2000` 触发总经理审批。
- **申请人 / 工号 / 事由 / 风险摘要 / 审核人 / 审批意见**：文本类取值约束与长度见 TABLE_SETUP（申请人=注册姓名、工号=`E`+4 位、事由 5–200 字、风险摘要风险时必填等）。

> ✅ **代码对齐（报销类型）**：已与用户确认收敛回 **4 类**（差旅费/招待费/办公用品/交通费），与代码 `ALLOWED_TYPES` 一致，无代码改动。

字段级要点：
- **单号**：AutoNumber 只读，代码**不写**（系统生成后回取）。
- **工号**：旧表无此列 → 写入前 `field_names()` 探测 + `filter_by_schema` 过滤（同一份代码兼容 14/17 字段两种表，.env 切换即用）。
- **发票日期**：东八区毫秒时间戳（由 `date_str` 解析，缺失兜底记录提交时间）。

**枚举强校验（代码层，M8 白名单）**：`models/feishu.py` 集中定义企业标准枚举常量（`DEPT_WHITELIST` 11 / `CITY_WHITELIST` 37 / `ALLOWED_TYPES` 4 / `ALLOWED_INVOICE` 3 / `COMPLIANCE_RESULTS` 4 / `STATUS_VALUES` 4）+ `validate_enum_fields(record)` 写前闸门。**两份清单（部门/城市）即飞书单选选项，代码与表逐字一致，唯一定义源在 `feishu.py`**。抽取层 `models/agent.py._extract_fields` 做**字段级归一路由**：城市只认 `CITY_WHITELIST`；部门经 `normalize_department` 把模糊输入（研发部门/研发/技术部）显式映射到字典，无法确定则留空由机器人追问；落库前 `_do_submit`/`_handle_compliance` 必过 `validate_enum_fields`，任何非标准枚举值（如「火星部」「电脑部门」）一律**拒写并友好提示**，杜绝脏值入表。

### 6.6 M8 账号认证（`models/actor.py` + `api/auth_service.py`）

> 账号体系已统一到主服务器：飞书用户表下线，网关不再保存账号、不再签发自己的令牌。
> 设计与取舍详见 `docs/账号统一改造-RFED_CAC.md`。

| 项 | 规格 |
|----|------|
| 账号来源 | 只有主服务器 `POST /api/auth/login`；网关侧无任何本地/飞书账号库 |
| 会话凭证 | **透传主服务器 token**：浏览器持有 C# 签发的 JWT，网关不签发、不改写 |
| 身份获取 | `sha256(token)` 查内存缓存 → 未命中先本地判 `exp` → 再调 `GET /api/auth/me` |
| 缓存策略 | 命中缓存零请求；失效能负缓存 30s（避免拿废令牌反复打主服务器累积攻击分） |
| 签名密钥 | 从 JWT 载荷的 `sk` 取，**只在服务端使用，绝不回传浏览器** |
| 角色判断 | 不看中文角色串，只看权限键：`invoice.submit` / `invoice.read.all` / `invoice.review` |
| 登录限流 | 网关侧 `RateLimiter`，key=`用户名\|IP`，默认 5 次/300s，**仅失败计数**；主服务器另有自己的限流 |
| 锁定口径 | 主服务器按 **`用户名\|IP` 组合**计数（组合锁），只有同源持续失败才限制；网关登录时带 `X-Client-Ip`，主服务器仅在受信来源时采信，防伪造 |
| sid 绑定 | `sid_bindings: dict[sid→主服务器 user.id]`：sid 首次出现即绑定，同 sid 换用户 → 403 |
| 登出 | 主服务器不做令牌吊销，网关只清本地缓存（令牌在过期/改密/改角色前仍有效） |
| 未知/空角色 | 按权限键判定，无权限即拒绝；不再有「默认降级为员工」的隐式放行 |

----|------|
| 密码哈希 | PBKDF2-HMAC-SHA256，每用户随机 16 字节盐、100k 迭代；`hmac.compare_digest` 恒时比较 |
| Token | `base64url(payload).b64url(HMAC-SHA256)`；含 `iat/exp/jti`；TTL≤2h；登出 = 内存 jti 黑名单 |
| 登录限流 | 复用 `RateLimiter`，key=`用户名\|IP`，5 次/300s；**仅失败计数**（`would_allow` 预检 + 失败才 `allow`） |
| sid 绑定 | `sid_bindings: dict[sid→用户名]`：sid 首次出现即绑定，同 sid 换用户 → 403（防会话串用/越权读草稿） |
| 用户表字段 | `工号/姓名/用户名/部门(单选)/角色(单选)/密码哈希/盐/账号状态(单选)/创建时间/最后登录时间` |
| 部门字典（两表共用） | `研发部/市场部/财务部/人力资源部/行政部/生产部/销售部/采购部/信息技术部/质量管理部/客户服务部`（主表与用户表**必须完全一致**） |
| 角色权限 | 员工：填单(自动带姓名/部门/工号)+查自己进度；财务：预审+查任意；管理员：预审+管账号(CLI)；**服务端强制**，前端 chip 隐藏仅是 UX |
| 管账号 | `python tools/user_admin.py add/reset/disable/enable/list`（密码哈希只能脚本生成写入，严禁手填） |
| 缓存 | UserStore 60s 内存缓存（刚停用账号最多 60s 缓冲期内仍可登录，演示可接受） |

---

## 7. 运行 / 测试 / 部署

### 7.1 起网关（必用 venv 解释器）

```bash
cd xiaocainu
# 方式一：双击/运行启动器（已硬编码 venv）
run_server.bat

# 方式二：直接指定 venv 解释器
"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe" -m api.main 0.0.0.0 8000
```

> ⚠️ 禁止在浏览器地址栏输 `0.0.0.0` —— 它是**绑定地址**不是访问地址。正确访问：`http://localhost:8000` 或 `http://127.0.0.1:8000`。

### 7.2 验证端点

```bash
# 本地 curl 需绕过代理
curl --noproxy '*' http://localhost:8000/health
curl --noproxy '*' http://localhost:8000/            # 前端单页
curl --noproxy '*' -X POST http://localhost:8000/chat -H 'Content-Type: application/json' -d '{"text":"招待费报销标准是多少"}'
```

### 7.3 本地实时联调（交互式验证飞书链路）

```bash
python run_live.py        # 用 venv 解释器；无 openai 时 A 降级 mock，B/C/D 飞书照常
```
输入示例：`我要报销差旅费5000元 我是张三` → 查一下张三的报销。

### 7.4 单元测试（离线、零付费 API）

```bash
# 用 venv 解释器逐个跑（pytest 未装进 venv 时直接 python 执行）
"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe" tests/test_agent.py
"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe" tests/test_feishu.py
# ... 共 10 个 test_*.py 全过（含账号链路的 test_backend_authority.py、M9 的 test_ocr.py）
```

### 7.5 部署注意事项
- 网关进程**跨轮不常驻**：agent 会话里 `run_in_background` 起的进程不会持久；用户需在自己终端里起 `run_server.bat` 才能持续访问。
- 凭证只服务端读取；前端登录 token 仅存 sessionStorage（关标签即清）；`.env` 已被 `.gitignore` 排除。
- **M8 上线三步**：① 起主服务器 `dotnet run`（账号权威在它那边）→ ② `.env` 填 `BACKEND_BASE_URL` → ③ 用 C# 管理后台创建账号（网关不再建号）。
- **M9 上线三步**：① 主表补「发票信息」「发票附件」两列（TABLE_SETUP 手册 1 分钟）→ ② `.env` 填真实 `DEEPSEEK_API_KEY`（顺带 `DEEPSEEK_MODEL=deepseek-flash`）→ ③ 上传一张真实发票照片冒烟（识别→预填→提交→附件落库）。
- 主服务器令牌有效期由 C# `Security:TokenMinutes` 控制（默认 120 分钟）；改密/改角色/停用会让旧令牌立即失效。

---

## 8. 开发纪律与迭代节奏

- **模块逐个建、单测通过再集成、确认后再拼接**（M1→M2→…→M7）。
- **不一次全建**；每个子模块先写单测再并入主干。
- **新增同类代码先照搬已验证模块**的入参/出参约定（如飞书回写节点复用既有字段结构），不另起一套。
- 仅提交代码/测试文件；业务知识库 `knowledge/*.md` 的微调与一次性探针（`live_probe_write.py`）保持未跟踪或显式确认。

---

## 9. 已知坑与实战约束（务必先看）

| # | 坑 | 现象 | 正确做法 |
|---|----|------|----------|
| 1 | **网关解释器** | 用基础版 3.13.12 起 `api.main` → `No module named 'pydantic'` | 一律用 venv `envs/default/Scripts/python.exe`（或 `run_server.bat`） |
| 2 | **浏览器地址** | 输 `http://0.0.0.0:8000` 打不开 | 访问用 `localhost:8000` / `127.0.0.1:8000` |
| 3 | **本地代理** | curl 网关返回 502 | 加 `--noproxy '*'` |
| 4 | **飞书单条查询结构** | 旧代码读 `data.fields` 导致单号恒空（显示兜底） | 已修：读 `data.record.fields`，兼容退回 `data`（见 5.5） |
| 5 | **AutoNumber 单号回取** | 写表后立即查可能空 | `_fetch_bill_no` 已重试一次；仍空走诚实兜底，不显示「系统生成中」 |
| 6 | **事由污染** | "出差费200元" 被正则误当「事由」 | `_extract_fields` 已加尾字过滤（需 ≥2 个非费/元/数字/标点字符才算事由） |
| 7 | **后台进程不常驻** | agent 起的网关下次会话没了 | 用户在自己终端起 `run_server.bat` |
| 8 | **agnes 模型名** | 用 `agnes-2.0-flash` → `No available channel` | 固定 `agnes-2.5-flash` |

---

## 10. 待办 / 可扩展（非主干，按需）

- **启用 reranker**：在 `Retriever.from_directory(reranker=...)` 注入 Agnes 排序模型，补足「≥2 类 AI 模型 + 检索质量」（当前默认关闭，纯 BM25 仍算 RAG 框架但缺真·AI 调用，比赛前需确认是否启用）。
- **可选合规审计日志**：若赛题要求审计，可加「脱敏 + 加密 + 留存期限 + 权限分级」模块（不进主干，作 PPT「未来小想法」）。
- **D 查询优化**：当前 `list_records` 全表拉取后内存匹配；量大时可改飞书 `filter` 表达式下推。

---

*文档基于当前主干代码（M1–M7 已落地）整理；模块细节以源码 docstring 与 `SECURITY.md` 为准。*
