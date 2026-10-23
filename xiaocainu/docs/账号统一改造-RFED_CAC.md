# 小财奴账号体系统一改造（飞书用户表下线 · 登录/填单/查询改认 C# 主服务器）

**文档版本**：v1.1（v1.0 基础上锁定登录限流阈值）

**适用范围**：`xiaocainu/`（Python 网关，改动主体）；`AiApprovalServer/`（C# 主服务器，只改配置不改逻辑）

**模型依据**：RFED_CAC

---

## Requirements

| 需求项 | 详细设计 |
|---|---|
| R1 账号唯一来源 | 登录口令判定只由 C# `POST /api/auth/login` 决定。Python 侧删除飞书「用户表」账号体系（`models/users.py`、`tools/user_admin.py`），不再保留任何本地/飞书账号库 |
| R2 删除飞书用户表 | 代码层：`FEISHU_USER_TABLE_ID` 全量下线。运维层：飞书多维表「用户表」由用户手动删除。C# 侧只清空 `Feishu:UserTableId`，镜像/同步/外部账号检测自动"不连接、静默跳过、返回空"，报销单写入不受影响 |
| R3 会话凭证 | **完全透传 C# token**：浏览器持有 C# JWT，网关不再自签会话 token，`AUTH_SECRET` 与 `security/auth.py` 删除 |
| R4 角色校验改源 | 填单/查询/预审一律改看 **C# 下发的权限键**（`invoice.submit` / `invoice.read.all` / `invoice.review`），不再比较角色中文字符串 |
| R5 写入唯一入口 | 报销单只能经 C# `POST /api/invoice/submit` 写飞书主表。**删除 Python 全部直连飞书写单路径**，C# 不可用即失败（fail-closed） |
| R6 查询唯一来源 | 进度查询改调 C# `GET /api/invoice/list`，范围由 C# 按 token 角色服务端裁剪（员工只见本人） |
| R7 不许替人报账 | 只有在抽取到**像真名的姓名**（2–4 汉字，且不含费用/部门/动作字）且不是本人时才拦截。抽取只认明确句式（`我是X` / `X+部门` / `查X` / `替帮代X报`），**不再从自由文本兜底猜名字**——否则「我要报销交通费」会被切成「销交通」而误判成替人报账 |
| R8 合规预审不建单 | C 分支只输出「合规 / 风险 / 不合规」结论与整改建议，**不写任何表** |
| R9 发票附件 | OCR 出文本 → B 分支确认 → C# 提交成功返回 `record_id` → **图片写入该行「发票附件」列**（修好当前从未上传成功的 M9） |
| R10 登录限流 | C# `Limits:LoginPerFiveMinutesPerIp` **由 6 调整为 60**（用户确认）。理由见 Feasibility 风险 1 |
| R11 非功能 | 单进程部署；网关只连 C#，不直接建单；不新增第三方依赖 |

---

## Feasibility

**意见：可行**（"减法 + 换身份源"，不引入新框架、不新增依赖）

| AI 意见 | 用户意见 | AI 解决方案 | 用户解决方案 |
|---|---|---|---|
| 可行 | 可行 | 按 S0–S9 分步落地，每步可单独验证 | 提供 `BACKEND_BASE_URL`；演示前删除飞书用户表 |
| 部分可行（风险 1）：网关所有请求共用 `127.0.0.1` 访问 C#，C# 的「6 次/5 分钟/IP + 满 8 分封 60 分钟且封全部 `/api/*`」实际变成全场共用一个额度，一次演示可能把系统封死 | **已确认：调到 60** | ①`Limits:LoginPerFiveMinutesPerIp` 6 → 60（本次执行）；②代码侧加 JWT `exp` 本地预检 + 失效负缓存 30s，避免过期 token 反复触发 `weight:1` 记分 | 无 |
| 部分可行（风险 2）：透传 token 后网关重启不会让用户掉线（token 在浏览器），但网关内存 Actor 缓存丢失，首个请求补调一次 `/api/auth/me` | 无异议 | 正常降级，多一次往返；`/me` 返回 401 才要求重登 | 无 |
| 部分可行（风险 3）：C# 账号**没有部门字段**，部门只能仍由用户口述 | 无异议 | 保持现状（C# `ValidateSubmit` 只校验部门在 11 枚举内，不比对账号），文档标注为已知边界 | 无 |
| 不可行（已排除）：让 C# 支持「代他人提交」 | 明确否决：**不允许替人报账** | C 分支改纯预审不建单；B 分支申请人比对不一致直接拦截 | 无 |

---

## Environment

**代码技术栈**：Python 3.13（标准库为主）+ FastAPI；C# .NET 8（不改进程逻辑）

**开发环境**：本地 Windows。`AiApprovalServer/` 用 `dotnet run`（8899），`xiaocainu/` 用 venv 解释器 `python -m api.main 127.0.0.1 8000`

**部署环境**：Windows 单机，两进程同机。**必须单 worker**（会话、限流、Actor 缓存都是进程内存；现有 `uvicorn.run(app)` 默认单进程，不要改成 `--workers`）

**开发工具**：仅已有依赖（openai / fastapi / uvicorn / httpx），不新增包。测试沿用现有 stdlib 脚本 + 新建 FakeBackend

---

## Development Difficulty

**总体难度：中等偏低，但波及面广**。逻辑不复杂（换身份来源 + 删写路径），难点三处：

1. **删除顺序敏感**：`models/users.py` 被 `api/main.py:25`、`models/agent.py:25`、`models/__init__.py` 间接依赖，删早了是整仓 import 失败（所有测试一起挂）。
2. **错误语义要重做**：现在"所有 4xx = 业务拒绝"，改造后 401（要重登）/ 403 `IP_BANNED`（被自己封了）/ 429 / 400·409（业务拒绝）/ 5xx 必须分开映射。
3. **测试改造量大**：`test_api.py`(616 行)、`test_agent.py`(953 行)、`test_backend_authority.py`(179 行) 因模块删除会**整体 import 失败**，必须同批重写。

工作量：新增 2 文件、重写 4 文件、删除 5 文件、改造 4 个测试文件、更新 5 份文档。

---

## Conflict

### 对比现有项目的冲突项

| 冲突 | 定位 | 性质 |
|---|---|---|
| **两套账号系统**（主冲突） | A 套：`xiaocainu/models/users.py`（`UserStore` 读飞书用户表）+ `api/auth_service.py:47-60`（本地表兜底）+ `tools/user_admin.py`（往飞书表建号）<br>B 套：`AiApprovalServer/Services/UserService.cs`（`App_Data/users.json` 权威） | **真冲突**。同一句"能不能登录"两个裁判；`tools/user_admin.py` 建出来的号还登不进 C#（`docs/主服务器对接说明.md:177` 自认） |
| **三条写飞书路径** | ①C#：`Services/InvoiceService.cs`<br>②`models/agent.py:859-881`（`_do_submit` 回落）<br>③`models/agent.py:997-1013`（C 分支直写）<br>④`api/main.py:348-373`（`/reimburse` 回落） | **真冲突**。②③④ 无快照、无审计、绕过枚举与服务端盖章 |
| **角色语义两套** | 中文标签 `"员工"/"财务"/"管理员"`（`models/agent.py:312,1125`、`api/main.py:379`）vs C# 四角色 + 15 个权限键（`Core/AccountPolicy.cs`） | **真冲突**。`ROLE_LABELS` 把 superadmin 折叠成"管理员"丢权限；`agent.py:1125` 是反向白名单（role 变英文即全部放行） |
| **两条读飞书路径** | `models/agent.py:1136` 全表拉 50 条 + 内存子串匹配 vs C# `InvoiceService.ListAsync:81-84` 服务端裁剪 | **重复实现**。Python 侧无行级权限，且 `agent.py:1082-1085` 有任意字段子串命中的越权隐患 |
| 附件上传的孤立写路径 | `models/agent.py:948-954` | **保留但需修复**：C# 无附件接口，且当前调用链已断 |

### 结论

四个真冲突（账号、写单、角色、读单）统一按"**C# 是唯一权威，Python 只做表现层**"收敛。改造后 Python 对飞书的直接调用**只剩附件列写入**一条。

---

## Architecture

**核心架构**：BFF + 令牌透传。网关不做身份签发，只做"取身份 + 转发 + 呈现"。

```
浏览器 ──Authorization: Bearer <C# JWT>──▶ Python 网关 (:8000)
                                            ├─ ① 取身份：sha256(token) 查 Actor 缓存
                                            │    miss → 本地 exp 预检 → GET /api/auth/me
                                            ├─ ② 业务：带同一 token 调 C#，由 C# 判权限
                                            ▼
                                    C# 主服务器 (:8899) ──▶ 飞书报销单主表
                                            ▲
                                            └─ 附件：C# 提交成功后，网关用 record_id
                                               调飞书 upload_attachment + update_record（只写列）
```

**具体分层**

| 层 | 文件 | 职责 | 边界 |
|---|---|---|---|
| 表现层 | `frontend/index.html` | 登录遮罩、存 token、按 `perms` 显隐按钮、渲染卡片 | 不判断权限 |
| 网关层 | `api/main.py` | 入口契约、认证墙、分派、错误映射 | 不做授权判断 |
| 身份层 | `api/auth_service.py` + `models/actor.py` | 取 C# 身份、缓存、`Actor.can()` | 不透传 `signKey` 给浏览器 |
| 适配层 | `api/backend_client.py` | 无状态 HTTP + 请求签名 + 错误分类 | 不持有 token 全局缓存 |
| 业务层 | `models/agent.py` | A/B/C/D 分支、字段抽取、追问、卡片 | 写单只调 C#；查单只调 C# |
| 数据层 | `models/feishu.py` | **只保留附件上传 + 表结构探测 + 枚举常量** | 不建单、不查单 |

**健康度**：四层单向依赖无回环；权威判断全收敛到 C#；除 `Actor` 外不新增抽象。

**残留耦合（已知边界）**：附件列仍由 Python 直写飞书（C# 无接口）。缓解：只在 C# 提交成功后按 `record_id` 写单列，失败不阻断单据。

---

## Configuration

| 配置项 | 动作 | 说明 |
|---|---|---|
| `BACKEND_BASE_URL` | **改为必需** | 如 `http://127.0.0.1:8899`。为空 → `build_auth()` 返回 `None` → 受保护端点与 `/login` 均 503（fail-closed） |
| `BACKEND_MODE` | **删除** | 三态消失，退化为 `enabled() = bool(BACKEND_BASE_URL)` |
| `BACKEND_TIMEOUT` | 保留（建议 20） | 列表拉 500 条时给足时间 |
| `AUTH_SECRET` / `AUTH_TOKEN_TTL` | **删除** | 网关不再签 token |
| `AUTH_LOGIN_LIMIT` / `AUTH_LOGIN_WINDOW` | 保留 | 网关侧登录失败限流（只计失败） |
| `FEISHU_USER_TABLE_ID` | **删除** | 账号体系下线 |
| `FEISHU_APP_ID` / `_APP_SECRET` / `_APP_TOKEN` / `_TABLE_ID` | 保留 | 仅供 R9 附件上传与表结构探测 |
| C# `Feishu:UserTableId` | **清空** | → `FeishuConfigured=false` → 镜像 `skipped`、外部账号检测跳过、`feishu-rows` 返回 `{configured:false, rows:[]}`；报销单写入不受影响（只依赖 AppId/AppSecret/AppToken + TableId，已核实 `Services/InvoiceService.cs:63`） |
| C# `Feishu:DetectForeignAccounts` | **改 `false`** | 用户表删除后该检测无意义，避免每次启动打无效请求 |
| C# `Limits:LoginPerFiveMinutesPerIp` | **6 → 60（本次执行）** | 已确认。理由：网关是 C# 唯一客户端，所有请求同源 IP；原值 6 会让"全场共用 6 次/5 分钟"，加上失败记分满 8 分封 60 分钟且封全部 `/api/*`，演示时 5 人各重登一两次即可打死系统 |
| C# `POST /api/admin/users/sync-from-feishu` | **保持 409 不变** | 未配置飞书时它返回 409 `SYNC_UNAVAILABLE`（`AdminApi.cs`），刻意不改：这是"执行动作"，静默返回成功会误导。只读预览 `feishu-rows` 已按要求返回 `configured:false, rows:[]` |

---

## 具体施工方案

### 数据结构

**`xiaocainu/models/actor.py`（新增）**

```python
PERM_INVOICE_SUBMIT    = "invoice.submit"
PERM_INVOICE_READ_SELF = "invoice.read.self"
PERM_INVOICE_READ_ALL  = "invoice.read.all"
PERM_INVOICE_REVIEW    = "invoice.review"

@dataclass(frozen=True)
class Actor:
    user_id: str        # C# user.id
    username: str       # C# userName
    name: str           # C# displayName（兜底 userName）
    employee_id: str    # C# employeeId
    role: str           # C# 角色代码 employee/finance/admin/superadmin（不折叠）
    role_label: str     # C# roleLabel 超级管理员/管理员/财务部/员工
    permissions: frozenset
    token: str = ""
    sign_key: str = ""  # 从 JWT payload 的 sk 取，绝不回传浏览器
    expires_at: int = 0

    def can(self, perm: str) -> bool: ...
```

**错误映射（`backend_client` 统一出口）**

| C# 返回 | 网关抛 | 对浏览器 |
|---|---|---|
| 200 且 `ok` | — | 200 |
| 401 | `BackendUnauthorized` | 401「登录已过期，请重新登录」+ 清该 token 缓存 |
| 403 且 `code=IP_BANNED` | `BackendUnavailable` | 503「服务端安全策略临时拦截，请稍后重试」 |
| 429 | `BackendRejected` | 429 + C# 原文案 |
| 400 / 409 | `BackendRejected` | 400 + C# 原文案（业务拒绝，绝不回落） |
| 5xx / 连接失败 / 非 JSON | `BackendUnavailable` | 503「主服务器暂时不可用」 |

**请求签名**：`sign_key` 从 JWT payload 的 `sk` 取（已核实 `Security/JwtService.cs:49`）；**不验签只取值**，伪造的 sk 会被 C# 401；POST 每次重新生成 `X-Timestamp`/`X-Nonce`；**不转发浏览器的 `Origin` 头**（C# `TrustedOrigins` 默认为空，转发会 403）。

### 分步执行（顺序不可调换）

**S0 打地基（不破坏现状）**
- 新增 `xiaocainu/models/actor.py`
- 新增 `xiaocainu/tests/mock_backend.py`（FakeBackend：login/me/submit/list 四桩 + 错误注入）

**S1 适配层加新函数（旧函数暂留）**
- `api/backend_client.py` 新增 `me(token)`、`sign_key_of(token)`（解 base64url payload 取 `sk`）、`submit_with_token(...)`、`list_invoices(token, sign_key, limit=500)`、`review_with_token(...)`、`meta_with_token(...)`
- 新增异常 `BackendUnauthorized(BackendError)`；统一 `_raise_for(status, payload)` 按上表分类
- 验证：对假 C# 跑 login → me → list

**S2 重写身份层 `api/auth_service.py`**
- `build_auth()`：开关改 `BACKEND_BASE_URL` 非空；不再 import `FeishuBitableClient` / `models.users`
- `login()`：调 `backend_client.login` → **剥掉 `signKey`** → 返回 `{token, expires_at, user{工号,姓名,用户名,角色,角色代码}, perms[], session_id}`
- `authenticate(token) -> Actor | None`：`sha256(token)` 查缓存（TTL=min(exp,2h)）→ miss 则本地解 payload 查 `exp`（过期进负缓存 30s）→ 再调 `/me` → 200 建 Actor 入缓存 / 401 进负缓存 / 5xx 抛 `BackendUnavailable`
- `logout(token)`：只清网关缓存（C# 无吊销，注释写明）
- `bind_sid(sid, actor.user_id)`
- **删除** `_issue` / `_login_via_backend` / `ROLE_LABELS` / `UserStore`

**S3 网关层切换 + 删写路径（`api/main.py`）**
- import 改 `from models.actor import Actor`，删 `models.users` 与 `build_reimbursement_record` 的导入
- 认证墙：`auth is None` → **503**（「网关未启用登录：未配置 BACKEND_BASE_URL」）；`actor is None` → 401
- `/login`、`/me`、`/logout` 按新结构；`/me` 返回展示字段 + `perms`
- `/chat`：`agent.route(..., actor=actor)`
- `/reimburse`：**删 `:348-373` 直连飞书写单段**；`except BackendRejected → 400`、`except BackendUnauthorized → 401`、`except BackendError → 503`
- `/progress`：**删 `:379-381` 角色字符串重写**，改 `agent.route(q, actor=actor)`
- `ReimburseRequest` 删掉 C# 会忽略的 `applicant` / `compliance` / `risk_summary` / `status`
- `build_agent()` 仅在 `FEISHU_APP_ID` + `FEISHU_TABLE_ID` 齐备时装配飞书客户端，日志去掉"用户表"

**S4 业务层改造（`models/agent.py`）**
- `route(..., actor: Actor | None = None)`；`actor is None` 时 B/C/D 全部拒绝并提示登录（**fail-closed**，替代现在 `if user:` 的放行）
- `_handle_fill`：申请人**与 `actor.name` 比对**——不一致返回 `报销单只能由本人提交：当前登录身份是 张三（E0001），不能替 李四 报账。`；一致或无值则硬绑定 `actor.name` + `actor.employee_id`。**删 `:696-697`** 的 `user.get("department")` 死分支
- `_handle_compliance`：`actor.can(PERM_INVOICE_REVIEW)` 否则拒绝；**删 `:986-1013` 建单段**，只输出预审结论 + 「本预审不创建单据，正式提交请由申请人本人操作」
- `_handle_progress`：`actor.can(PERM_INVOICE_READ_ALL)` → 可查全部，否则只看本人；**数据源改 `list_invoices(...limit=500)`**；新增 `_csharp_row_view(row)` 输出**中文键**（`单号/申请人/工号/部门/报销类型/金额/票据状态/合规结果/风险摘要/状态/审核人/审批意见/记录提交时间/发票日期/事由/城市`），因为前端 `index.html:359-388` 硬编码中文键
- `_record_hit`：**删 `:1082-1085` 任意字段子串命中**，只保留 `billNo` 精确/包含 + `applicant` 匹配
- `_do_submit`：**删 `:859-881` 直连飞书回落**；改 `submit_with_token(...)`；`BackendRejected` → 提示原文；`BackendError` → 「主服务器暂时不可用，单据未提交」
- **R9 附件**：C# 返回 `record_id` 后、return 前调 `_attach_invoice(rid, image_b64)`，`try/except` 包住，失败只追加「📎 附件未存档」
- **删** `_fit_schema` / `_fetch_bill_no` / `public_view` / `filter_by_schema` / `validate_enum_fields` 的调用（保留 `ALLOWED_TYPES` / `DEPT_WHITELIST` / `CITY_*` 抽取用常量）
- `models/__init__.py`：导出删 `build_reimbursement_record` / `public_view`，加 `Actor`

**S5 删旧文件（必须在 S3/S4 之后）**
- 删 `models/users.py`、`security/auth.py`、`tools/user_admin.py`
- 删 `tests/test_users.py`、`tests/test_auth.py`
- 清理 `live_verify.py` / `probe_new.py` / `live_probe_write.py` / `tools/live_ocr_smoke.py` 里的用户表与建单段
- `api/backend_client.py` 删旧函数：`_tokens` / `_cache_token` / `_cached` / `ensure_login` / `submit_as` / `submit` / `my_invoices` / `mode()`
- `models/feishu.py` 删 `build_reimbursement_record` / `public_view` / `filter_by_schema` / `validate_enum_fields`
- 验证：`python -c "import models, api.main"` 不报错

**S6 前端（`frontend/index.html`）**
- 存 `xc_token`（C# token）+ `xc_user` + `xc_perms`
- 「合规预审」按钮显隐改 `xc_perms.includes("invoice.review")`（**C# 的 `permissions` 是 `[{key,label}]` 数组，网关在 S2 已压平成 key 字符串数组**）
- 启动时调一次 `GET /me` 验活，失败即弹遮罩（消掉"假登录"窗口期）
- `/chat` 与 `/ocr` 的 401 处理抽成 `handle401()`
- 文案清理：删掉提到"飞书用户表"的脚注

**S7 配置**
- `.env.example`：删 `FEISHU_USER_TABLE_ID` / `AUTH_SECRET` / `AUTH_TOKEN_TTL` / `BACKEND_MODE`；`BACKEND_BASE_URL` 标为必填
- C# `appsettings.json`：`Feishu:UserTableId` 置空、`Feishu:DetectForeignAccounts` 改 `false`、**`Limits:LoginPerFiveMinutesPerIp` 6 → 60**
- 飞书控制台：手动删除「用户表」多维表（代码已不引用）

**S8 测试改造**

| 文件 | 动作 |
|---|---|
| `tests/test_api.py` | 换 `MockBackend` + 重写 `_auth/_tok` fixture；重写 `test_login_flow` / `test_login_rejections` / `test_auth_wall_401_fail_closed`（503 而非 401）/ `test_reimburse_endpoint` / `test_progress_endpoint` / `test_role_overrides_gateway`（改「替人报单被拦截」）/ `test_logout_revokes_token`（改「本地清除」）；OCR 段只换 fake |
| `tests/test_agent.py` | fixture 换 `Actor`；**删** `test_role_anonymous_backward_compatible`；改 `test_role_b_autofill_identity`（覆写 → 拦截）/ `test_role_b_spoken_department_wins`；C 分支断言改「不写表」；D 分支全部改 mock `list_invoices` |
| `tests/test_backend_authority.py` | 重写为「透传 + 错误映射」；**删** `test_backend_down_falls_back_in_preferred`、`test_backend_disabled_uses_local_table`、`test_role_label_mapping` |
| `tests/test_frontend.py` | `_FakeAuth` 补 `authenticate`；断言更新（`xc_perms`、`/me`） |
| `tests/test_feishu.py` | 删 4 个函数对应用例 |
| `test_ocr` / `test_llm` / `test_retriever*` / `test_security` / `test_agnes_live` | 不动（已核实无 user/role 依赖） |

**S9 文档**
- `xiaocainu/DEVELOPMENT.md`：M8 段重写，删用户表建表步骤
- `xiaocainu/docs/主服务器对接说明.md`：第八节「账号权威边界」重写，删 `BACKEND_MODE`/回落语义
- `xiaocainu/docs/TABLE_SETUP.md`、`飞书字段对接手册.md`：删「用户表」章节
- 根 `docs/前后端分离与接入指南.md`：登录链路改为「已完全统一，无兜底」
- 本文件复制到 `xiaocainu/docs/账号统一改造-RFED_CAC.md`

### 验收标准

| # | 场景 | 期望 |
|---|---|---|
| 1 | 四个演示账号登录 | 角色标签 超级管理员/管理员/财务部/员工（不再折叠） |
| 2 | 员工说"查一下所有人的报销" | 只返回本人单据（C# `scope=mine`） |
| 3 | 财务说"查一下张三的报销" | 返回全量中匹配张三的单 |
| 4 | 员工说"帮李四报一笔差旅费5000" | **被拦截**：`报销单只能由本人提交…` |
| 5 | 财务在 C 分支说完整一单 | 只给合规结论，**飞书无新行** |
| 6 | 正常 B 分支提交 | 飞书主表新增一行，申请人/工号=登录人；答复带 `BX-` 单号 |
| 7 | OCR 上传发票 + 提交 | 落库后「发票附件」列有图，答复带「📎 已存档」 |
| 8 | 停掉 C# 后登录 | 503「主服务器暂时不可用」，**不是** 401 |
| 9 | 停掉 C# 后填单 | 503，飞书**无新行** |
| 10 | 清空 `Feishu:UserTableId` 后启动 C# | 横幅「飞书镜像 未配置（跳过镜像）」；`feishu-rows` 返回 `configured:false, rows:[]`；报销单接口正常 |
| 11 | 改 C# 里某账号角色后 | 下一次业务请求（或重登）后生效；不出现"一直提示未登录" |
| 12 | 连续 20 次登录尝试 | 不触发 C# 封 IP（60 次/5 分钟阈值下） |
| 13 | 跑全部 Python 测试 | 全绿 |
