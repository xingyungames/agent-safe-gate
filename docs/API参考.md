# API 参考（AiApprovalServer）

## 0. 适用范围 / 版本

| 项 | 说明 |
| --- | --- |
| 对应代码 | 仓库 `AiApprovalServer/`（ASP.NET Core Minimal API，`net8.0`），测试脚本在 `tests/` |
| 版本状态 | 未发布正式版本，本文对应仓库当前状态（工作副本快照），接口以代码为准 |
| 数据库 | **纯 JSON 文件**（`App_Data/` 下的 `users.json` / `tasks.json` / `versions.json` / `outbox.json` / `audit.jsonl` / `datasets/*.json` / `workspaces/`），没有外部数据库 |
| 第三方依赖 | **无任何第三方 NuGet 包**：JWT（HS256）、PBKDF2-HMAC-SHA256 口令散列、AES-256-GCM 字段加密、HMAC 请求签名、审计哈希链全部为自实现 |
| 默认监听 | `http://127.0.0.1:8899`（环境变量 `AISERVER_LISTEN_URL` 或 `Server:ListenUrl` 可改） |
| 页面与接口同源 | 页面（`/`、`/finance`、`/admin`）与 API 由同一进程提供，浏览器端用原生 `fetch` + Web Crypto 计算请求签名 |
| 演示账号 | 四角色各一个：`employee`（员工，工号 E0001）/ `finance`（财务部，E0002）/ `admin`（管理员，E0003）/ `root`（超级管理员，E0004），口令见 README 演示口令（本文不记录口令）。详见 [账号与权限设计.md](账号与权限设计.md) 第 8 节 |
| 账号权威 | 本机 `App_Data/users.json` 是唯一账号权威；飞书用户表只是镜像且允许失败，不参与鉴权（见 [飞书镜像对接.md](飞书镜像对接.md)） |
| 初始超管 | 全新部署由 `Bootstrap:Admin`（环境变量 `AISERVER_BOOTSTRAP_*` 或 `config/secrets.local.json` 的 `BootstrapAdmin` 段）播种一个超级管理员：只在账号库里不存在该用户名时创建一次，不写飞书用户表。详见 [账号与权限设计.md](账号与权限设计.md) 第 9 节 |

阅读约定：

- 文中「请求体字段」按 **camelCase** 书写（`AppJson.Wire` 使用 camelCase 命名策略，且反序列化大小写不敏感）。
- 凡是代码里没有明确写死、或本文未能从代码中确认的地方，均标注「以代码为准」，不做推测。
- 所有响应均为 `application/json; charset=utf-8`。

---

## 1. 通用约定

### 1.1 请求管线（顺序即防线）

`AiApprovalServer/Program.cs` 与 `Security/SecurityMiddleware.cs` 决定的处理顺序：

1. 安全响应头（CSP、nosniff、DENY、no-referrer 等，任何响应都带）
2. IP 封禁检查 → `403 IP_BANNED`
3. 蜜罐路径探测（命中即记分/封禁）
4. `/robots.txt` 诱饵（记录 `recon.robots`，不拦截）
5. WAF 特征扫描（路径、原始查询串、关键请求头）→ `400 WAF_BLOCKED`
6. 请求体缓冲（仅状态变更方法，带硬上限）
7. 跨站来源校验（Origin/Referer）→ `403 BAD_ORIGIN`
8. JWT 校验 + 请求签名/时间戳/一次性随机数校验
9. 鉴权：`/api/*` 除公开路径（`/api/public/health`、`/api/auth/login`）外必须有身份；`/api/admin/*` 必须是管理员或超级管理员
10. GET `/api/*` 读接口限流（在路由之前）
11. 业务端点（`AuthApi` / `AgentApi` / `InvoiceApi` / `AdminApi`）

### 1.2 统一响应结构（业务端点）

由 `Api/Api.cs` 统一产出：

成功：

```json
{ "ok": true, "data": { } }
```

失败：

```json
{ "ok": false, "code": "RATE_LIMITED", "message": "请求过于频繁，请稍后再试" }
```

- `data` 为 `null` 时该字段会被省略（`AppJson.Wire` 忽略 null 值）。
- 业务端点的失败一律通过 `Api.Error(status, code, message)` 返回，HTTP 状态码与 `code` 同时给出。

### 1.3 中间件层的错误结构（另一套形状，客户端必须兼容）

安全中间件与 fallback 直接写响应，**不使用** `ok/message`，而是 `error/code`：

```json
{ "error": "登录状态无效，请重新登录", "code": "BAD_TOKEN" }
```

读接口限流用的是 `ok` 形状（由 `Program.cs` 内联写出）：

```json
{ "ok": false, "code": "RATE_LIMITED", "message": "读取过于频繁，请稍后再试" }
```

统一 404（`MapFallback`）：

```json
{ "ok": false, "code": "NOT_FOUND", "message": "未找到资源" }
```

> 客户端判定建议：先看 HTTP 状态码，再取 `code`；消息字段可能是 `message`（业务层）或 `error`（中间件层）。

### 1.4 参数清洗（`Api.Clean`）

对用户可控字符串统一做：去首尾空白 → 去掉控制字符（保留 `\n`、`\t`）→ 超长截断。各接口的截断长度见各接口表（例如登录用户名 64、注册用户名 32、消息体不截断但由编排器限制总长等）。

### 1.5 状态码一览

| HTTP | code | 触发点 |
| --- | --- | --- |
| 200 | —— | 业务成功（注意：被规则引擎拒绝的业务动作也返回 200，业务结果在 `data.status` 里） |
| 400 | `BAD_REQUEST` | 请求体无法反序列化、缺少必填字段 |
| 400 | `EMPTY_MESSAGE` | 对话内容为空 |
| 400 | `TOO_MANY_ARGS` | 业务动作参数超过 12 个 |
| 400 | `WAF_BLOCKED` | WAF 命中（中间件层，`error` 结构） |
| 400 | `REASON_REQUIRED` | 审批/打回/回退未填意见（少于 2 个字符） |
| 400 | `WEAK_PASSWORD` / `REGISTER_FAILED` / `CREATE_FAILED` / `RESET_FAILED` / `BAD_ROLE` | 账号相关校验失败（口令强度、用户名重复、邮箱非法、重置失败等） |
| 400 | `BAD_TOOL` / `EXECUTE_FAILED` | 只读直通接口：工具不可用 / 执行失败 |
| 403 | `RULE_DENIED` | 只读直通接口被规则引擎拒绝 |
| 400 | `ROLLBACK_FAILED` / `QUERY_FAILED` | 回退失败 / 数据集查询失败 |
| 400 | `SUBMIT_REJECTED` / `REVIEW_REJECTED` / `UPDATE_REJECTED` / `DELETE_REJECTED` / `ROLLBACK_REJECTED` | 报销单写操作被拒：枚举/金额/城市/事由等校验未通过、状态流转非法、字段不在可改白名单、版本已回退过、飞书未配置（见 [报销单写入口与财务流程.md](报销单写入口与财务流程.md)） |
| 400 | `BAD_DATE` | 报销单发票日期不是 `yyyy-MM-dd` |
| 502 | `FEISHU_UNAVAILABLE` | 报销单列表查询时飞书不可用（未配置凭证/主表，或调用失败）；其它报销单接口的同类失败走上面的 `*_REJECTED` |
| 403 | `FORBIDDEN` | 账号策略拒绝：改自己的角色（`不能修改自己的角色（避免自我提权）`）、停用/删除自己、非超管授予管理员、把最后一个超管降级等；具体文案由 `AccountPolicy` 给出，见 [账号与权限设计.md](账号与权限设计.md) |
| 401 | `UNAUTHENTICATED` / `BAD_TOKEN` / `TOKEN_STALE` / `BAD_SIGNATURE` / `AUTH_FAILED` | 未登录 / 令牌非法 / 令牌失效 / 签名失败 / 口令错误 |
| 403 | `FORBIDDEN` / `IP_BANNED` / `BAD_ORIGIN` / `TOKEN_REJECTED` | 越权（含账号策略 `AccountPolicy` 的拒绝，原因原文在 `message`）/ 封禁 / 跨站来源（未命中 `Security:TrustedOrigins` 且与本站不同源，见 2.4）/ 审批令牌被拒 |
| 404 | `NOT_FOUND` | 任务/版本/通知/数据集/路由不存在 |
| 409 | `CONFLICT` / `ALREADY_REVERTED` | 任务状态不允许再次审批 / 版本已回退过 |
| 409 | `SYNC_UNAVAILABLE` | 账号同步时飞书未配置，或从对面用户表读到 0 行（见 7.2） |
| 413 | `PAYLOAD_REJECTED` | 请求体超限 |
| 429 | `RATE_LIMITED` | 各类限流 |
| 499 | `CANCELLED` | 客户端中途断开（非标准码，代码中显式使用） |
| 500 | `INTERNAL` / `FAILED` | 未捕获异常 / 口令修改失败 |

### 1.6 审计动作名速查

审计条目写入 `App_Data/audit.jsonl`（JSONL + 哈希链），字段：`seq/time/actorId/actorName/actorRole/ip/action/target/outcome/detail/prevHash/hash`。
不同类型的调用点可能把 `actorRole` 或 `ip` 写成空字符串（例如后台用户管理、任务执行链路），以代码为准。

| 动作名 | 写入方 | 含义 |
| --- | --- | --- |
| `system.startup` | `Program.cs` | 服务启动 |
| `system.seed` | `UserService` | 首次播种演示账号 |
| `system.bootstrap-admin` | `UserService` | 按 `Bootstrap:Admin` 配置播种初始超级管理员（只在账号库里不存在该用户名时创建一次，口令取自配置） |
| `system.migrate-roles` | `UserService` | 启动时把旧三角色（`user` / `staff`）迁移到新四角色，并给缺工号的历史账号补号 |
| `system.error` | 安全中间件 | 未捕获异常 |
| `auth.login` | `AuthApi` / `UserService` | 登录成功 / 失败 |
| `auth.password-upgraded` | `UserService` | 登录成功后把旧口令方案（无服务端密钥、或迭代次数偏低）静默升级为当前方案，明细写明新方案与迭代次数 |
| `auth.logout` | `AuthApi` | 主动登出 |
| `auth.register` | `AuthApi` | 管理员调用 `POST /api/auth/register` 创建账号（成功 / 失败 / 被策略拒绝） |
| `auth.token` | 安全中间件 | 令牌校验失败（格式/签名/过期/世代不匹配） |
| `auth.lockout` | `AuthApi` | 账号因连续失败被锁定 |
| `auth.rate-limit` | `AuthApi` | 登录频率超限 |
| `authz.admin` | 安全中间件 | 非管理员访问 `/api/admin/*` |
| `user.create` | `UserService` | 管理员创建账号（`POST /api/admin/users`，成功时一条 `SUCCESS`，被策略拒绝时另有一条 `DENIED`） |
| `user.role` | `UserService` / `AdminApi` | 修改角色（成功 / `DENIED`） |
| `user.disable` / `user.enable` | `UserService` / `AdminApi` | 停用 / 启用账号（成功 / `DENIED`） |
| `user.password` | `UserService` / `AdminApi` | 修改或重置口令（成功 / 失败 / `DENIED`） |
| `user.delete` | `UserService` / `AdminApi` | 删除账号（成功 / `DENIED`） |
| `user.mirror-password` | `AdminApi` | 管理员读取飞书镜像演示口令 |
| `feishu.mirror.sync` | `FeishuMirror` | 账号镜像写入飞书用户表成功 |
| `feishu.mirror.failed` | `FeishuMirror` | 账号镜像失败（已静默处理，不影响业务结果） |
| `feishu.mirror.foreign-account` | `Program.cs`（启动时） | 飞书用户表里存在、本机账号库没有的账号（`outcome = IGNORED`，只留痕不建号，它们无法登录本系统） |
| `user.sync-feishu` | `UserService` / `AdminApi` | 从飞书用户表手工同步账号：预览 `PREVIEW`、执行 `SUCCESS`、读到 0 行 `FAIL`、无 `account.create` 权限 `DENIED` |
| `task.create` | `TaskService` | 任务落库（`PENDING` 待审批 / `AUTO` 自动执行） |
| `task.execute` | `TaskService` | 工具执行完成（`SUCCESS` / `FAILED`） |
| `task.reject-rule` / `task.reject-ai` | `TaskService` | 被规则引擎 / AI 审核拒绝 |
| `task.reject` | ——（**当前不会产生**） | 历史版本的通用打回动作名；写入它的 `TaskService.RejectAsync` 已删除，打回统一走 `DecideAsync`（`approval.reject`） |
| `task.expire` | `TaskService` | 审批超时自动作废 |
| `task.cancel` | `Harness`（`cancel_my_task` 工具） | 提交人撤回自己的排队任务 |
| `approval.approve` / `approval.reject` | `TaskService.DecideAsync` | 管理员批准 / 打回 |
| `approval.token` | `TaskService.DecideByTokenAsync` | 邮件令牌校验失败 |
| `approval.token-used` | `AdminApi` | 邮件令牌审批成功 |
| `ai.plan` | `AgentOrchestrator` | 意图解析调用（`OK` / `FALLBACK`） |
| `ai.review` | `AgentOrchestrator` | AI 安全审核意见（`UNAVAILABLE` 表示审核不可用） |
| `ai.declined` | `AgentOrchestrator` | 模型未调用任何工具即作答（用于复盘"模型自行拒绝"） |
| `agent.rate-limit` | `AgentApi` | 对话频率超限 |
| `input.injection-suspect` | `AgentOrchestrator` | 输入/业务参数命中注入特征（只观测，不阻断） |
| `invoice.submit` | `InvoiceService` | 提交报销单（`SUCCESS` / `REJECTED` 校验未通过 / `FAILED` 飞书写入失败） |
| `invoice.list` | `InvoiceService` | 查询报销单列表（`SUCCESS` / `FAILED`）；详情与元数据接口不写审计 |
| `invoice.review` | `InvoiceService` | 审核报销单（写状态、审核人、审批意见、审批时间） |
| `invoice.update` | `InvoiceService` | 财务修改报销单字段 |
| `invoice.delete` | `InvoiceService` | 删除报销单 |
| `invoice.rollback` | `InvoiceService` | 按版本日志回退报销单（`SUCCESS` / `SKIPPED` 幂等跳过 / `FAILED`） |
| `invoice.ai.score` | `InvoiceAiService` | AI 预审打分（`SUCCESS` / `PARTIAL`），详情含单量、跳过数、失败批次、注入标记 |
| `invoice.ai.find` | `InvoiceAiService` | AI 按需求选单（`SUGGESTED`，只记建议，不代表执行） |
| `invoice.batch` | `InvoiceService` | 批量审核（`SUCCESS` / `PARTIAL`），逐单另有 `invoice.review` |
| `security.allowlist` | 安全中间件 | 白名单 IP 请求放行（可在 `Security:AllowlistAuditEveryHit=false` 关闭） |
| `admin.rollback` | `AdminApi` | 文件/数据集类版本回退（报销单请用 `invoice.rollback`） |
| `admin.unban` | `AdminApi` | 管理员手动解封 IP |
| `admin.activity-summary` / `admin.task-list` | `Harness` | 管理端 AI 助手的聚合查询 |
| `security.blocked` | 安全中间件 | 被封禁 IP 的请求被拒 |
| `waf.block` | 安全中间件 | WAF 命中（路径 / 查询串 / 请求头） |
| `waf.body` | 安全中间件 | WAF 命中**请求体**（JSON body，见 2.6 节；由 `Security:WafScanBody` 控制） |
| `csrf.block` | 安全中间件 | 跨站来源被拒 |
| `honeypot.probe` / `honeypot.credential` | 安全中间件 | 蜜罐探测 / 向诱饵后台提交凭据 |
| `recon.robots` | 安全中间件 | 读取 `robots.txt` |
| `request.signature` | 安全中间件 | 请求签名校验失败 |

历史动作名 `biz.action` / `biz.read` / `biz.rate-limit` / `biz.link-account` 属于已移除的旧业务对接台（甲方乙方客户档案那套），当前代码不会再产生它们；老审计文件里仍能查到这些记录。

---

## 2. 鉴权与请求签名协议

### 2.1 访问令牌（自实现 JWT，HS256）

- 传输：`Authorization: Bearer <token>`
- 头部固定 `{"alg":"HS256","typ":"JWT"}`；只接受 `HS256`，`alg: none` 或其它算法一律拒绝（防算法混淆）。
- 载荷字段（`Security/JwtService.cs`）：

| 字段 | 含义 |
| --- | --- |
| `sub` | 用户 id |
| `name` | 用户名 |
| `role` | `superadmin` / `admin` / `finance` / `employee`（四角色，见 [账号与权限设计.md](账号与权限设计.md)） |
| `sk` | **请求签名密钥**（与登录响应里的 `signKey` 同值） |
| `jti` | 令牌随机标识 |
| `epo` | 令牌世代（见 2.2） |
| `iat` / `exp` | 签发时间 / 过期时间（Unix 秒） |
| `iss` / `aud` | 固定为 `aiapproval` / `aiapproval-web` |

- 有效期：`Security:TokenMinutes`（默认 120 分钟）。
- 校验项：算法与 `typ`、签名（恒定时间比较）、`iss`/`aud`、`exp`、`iat`（不得超过服务器当前时间 60 秒）。
- 失败响应：
  - 令牌格式/签名/过期 → `401 {"error":"登录状态无效，请重新登录","code":"BAD_TOKEN"}`
  - 账号不存在 / 被禁用 / 世代不匹配 → `401 {"error":"登录状态已失效，请重新登录","code":"TOKEN_STALE"}`
- 认证方式不含 Cookie，因此天然不受 CSRF 影响；另外仍有 Origin 校验作为双保险（见 2.4）。

### 2.2 令牌世代（`epo`）

`users.json` 中每个账号有 `TokenEpoch`。以下操作都会把它 `+1`，**使该账号所有已签发的令牌立即失效**（下一次请求返回 `TOKEN_STALE`）：

- 修改自己的口令（`POST /api/auth/password`）
- 管理员重置口令（`POST /api/admin/users/{id}/reset-password`）
- 管理员修改角色（`POST /api/admin/users/{id}/role`）
- 管理员禁用/启用账号（`POST /api/admin/users/{id}/status`）

### 2.3 请求签名（`X-Timestamp` / `X-Nonce` / `X-Signature`）

实现位置：`Security/SecurityMiddleware.cs` 的 `VerifySignature`（拼串为最终事实来源）。

**需要签名的请求**：带 `Authorization: Bearer` 令牌、且方法为 `POST` / `PUT` / `PATCH` / `DELETE` 的请求。
**不需要签名**：所有 `GET`；未带令牌的公开接口（`/api/public/health`、`/api/auth/login`、`/api/auth/register`）。

#### 2.3.1 拼串（canonical）

五段用单个换行符 `\n`（LF）连接，**没有尾随换行**：

```
METHOD
PATH?QUERY
X-Timestamp
X-Nonce
SHA256_HEX(rawBody)
```

| 段 | 取值 |
| --- | --- |
| `METHOD` | 大写方法名，例如 `POST` |
| `PATH?QUERY` | 服务端解析到的 `Request.Path.Value` 与 **原始** `Request.QueryString.Value` 直接拼接；查询串自带前导 `?`，无查询串时该行就是路径本身 |
| `X-Timestamp` | 请求头里的**原样字符串**（同时必须能解析为 Unix 毫秒整数） |
| `X-Nonce` | 请求头里的原样字符串 |
| `SHA256_HEX(rawBody)` | 对**请求体原始字节**做 SHA-256，输出**小写十六进制**（64 字符）。空请求体按空字节数组计算，值为 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |

#### 2.3.2 签名值与编码

```
X-Signature = Base64( HMAC-SHA256( key = UTF8(signKey), message = UTF8(canonical) ) )
```

- `signKey` 取自登录响应顶层 `signKey` 字段（同一值也编码在 JWT 载荷 `sk` 中），每次登录都会重新随机生成。
- 使用**标准 Base64**（不是 base64url），比较为恒定时间比较。

#### 2.3.3 时间窗与防重放

| 项 | 规则 | 配置 |
| --- | --- | --- |
| 时间戳容差 | `abs(服务器当前毫秒 − X-Timestamp) ≤ ClockSkewSeconds` | `Security:ClockSkewSeconds`（默认 300 秒） |
| 时间戳格式 | 必须是可解析的 Unix **毫秒** 整数 | —— |
| nonce 长度 | 16 ~ 128 字符 | 硬编码 |
| nonce 唯一性 | 同一用户 + 同一 nonce 在时间窗内只能用一次（重放必然失败） | `Security:NonceWindowSeconds`（默认 300 秒） |
| 记忆上限 | nonce 表超过 20000 条时清理过期项 | 硬编码 |

签名失败（含时间戳超窗、缺头、nonce 复用）统一返回：

```json
HTTP 401 { "error": "请求签名校验失败", "code": "BAD_SIGNATURE" }
```

并写入审计 `request.signature`（`outcome` 为 `FAIL`，触发封禁时为 `BANNED`），同时给来源 IP 记 2 分。

#### 2.3.4 计算示例（PowerShell）

要点：**必须对"实际发出的字节"做哈希**，所以下面把请求体先转成 `byte[]` 再作为 `-Body` 传入，避免 PowerShell 用非 UTF-8 编码发送中文导致签名不匹配。

```powershell
$base    = 'http://127.0.0.1:8899'
$token   = '<登录响应 data.token>'
$signKey = '<登录响应 data.signKey>'

$method = 'POST'
$path   = '/api/agent/chat'
$body   = '{"message":"列出我的文件"}'
$bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)

# 1) 时间戳（Unix 毫秒）与一次性随机数（18 字节 → 36 位十六进制）
$ts    = [string][long][DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$nonce = -join ((1..36) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) })

# 2) 请求体哈希（小写十六进制）
$sha = [System.Security.Cryptography.SHA256]::Create()
$bodyHash = -join ($sha.ComputeHash($bodyBytes) | ForEach-Object { $_.ToString('x2') })

# 3) 拼串：五段以 \n 连接，无尾随换行
$canonical = @($method, $path, $ts, $nonce, $bodyHash) -join "`n"

# 4) HMAC-SHA256 → 标准 Base64
$hmac = [System.Security.Cryptography.HMACSHA256]::new([System.Text.Encoding]::UTF8.GetBytes($signKey))
$sig  = [Convert]::ToBase64String($hmac.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($canonical)))

# 5) 发送
Invoke-RestMethod -Method Post -Uri "$base$path" -ContentType 'application/json' -Body $bodyBytes -Headers @{
  Authorization = "Bearer $token"
  'X-Timestamp' = $ts
  'X-Nonce'     = $nonce
  'X-Signature' = $sig
}
```

`curl` 等价形式（先登录拿令牌，再签名；`body` 必须是文件内容原样）：

```bash
# 登录（公开接口，无需签名）
curl -s http://127.0.0.1:8899/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"userName":"employee","password":"<见 README 演示口令>"}'

# 组装签名（body 必须与将要发送的字节完全一致）
body='{"message":"列出我的文件"}'
ts=$(date +%s%3N)
nonce=$(head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n')
bodyHash=$(printf '%s' "$body" | sha256sum | cut -d' ' -f1)
canonical=$(printf 'POST\n/api/agent/chat\n%s\n%s\n%s' "$ts" "$nonce" "$bodyHash")
sig=$(printf '%s' "$canonical" | openssl dgst -sha256 -hmac "$SIGN_KEY" -binary | base64)

curl -s http://127.0.0.1:8899/api/agent/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H "X-Timestamp: $ts" -H "X-Nonce: $nonce" -H "X-Signature: $sig" \
  -d "$body"
```

> 注意：查询串参与签名。例如 `/api/agent/files/content?scope=private&path=note.txt` 是 GET，不需要签名；但如果用 POST 调带查询串的接口，第二行必须是 `/api/xxx?scope=private&path=note.txt`（参数的编码形式以服务端看到的原始查询串为准）。

### 2.4 跨站来源校验（Origin）

对**状态变更方法**（POST/PUT/PATCH/DELETE）：若请求带 `Origin` 头，按下面的顺序判定（`Security/SecurityMiddleware.cs` 的 `IsSameOrigin`）：

1. 取 `Origin` 的「协议 + 主机 + 端口」（`UriPartial.Authority`，大小写不敏感比较）；
2. **命中 `Security:TrustedOrigins` 白名单 → 放行**；
3. 否则必须与 `{当前请求 Scheme}://{当前请求 Host}` **严格同源**才放行；
4. 两条都不满足 → 拒绝。

拒绝响应：

```json
HTTP 403 { "error": "请求来源不被信任", "code": "BAD_ORIGIN" }
```

并写入审计 `csrf.block`、记 1 分。不带 `Origin` 的客户端（curl / 脚本 / 服务端调用）直接放行，由签名机制负责真实性。

**`Security:TrustedOrigins` 白名单**（`Core/AppConfig.cs`）：

| 项 | 说明 |
| --- | --- |
| 配置形式 | `appsettings.json` 中的数组：`Security:TrustedOrigins:0`、`:1`、`:2` … |
| 环境变量 | `AISERVER_TRUSTED_ORIGINS`，多个值用**分号、逗号或空格**分隔 |
| 取值格式 | 必须是**完整来源**（形如 `https://ops.example.com`）；解析时只取「协议+主机+端口」，路径被忽略 |
| 非法值 | 不是合法绝对 URI 的条目被忽略，并在启动提示（`StartupWarnings`）中列出 |
| 为空时 | 行为与不配置完全一致：只有严格同源才放行 |

**它为什么存在（反向代理终止 TLS）**：当 HTTPS 在反向代理/网关上终止、应用自身只监听内网 http 地址时，浏览器发出的 `Origin` 是 https 域名，而应用看到的 `Scheme://Host` 是内网地址，两者天然不同源，严格同源校验会把正常写请求全部拒掉。此时把对外的 https 来源（如 `https://ops.example.com`）加入 `Security:TrustedOrigins` 即可放行；**未列入白名单的其它来源仍然被拒**，因此这不是"关闭 CSRF 校验"。

> 认证不使用 Cookie（`Authorization: Bearer` + 请求签名），Origin 校验只是双保险，见 2.1。

### 2.5 安全响应头（所有响应）

`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`、`Permissions-Policy`（关闭定位/麦克风/摄像头/支付/USB/磁力计）、`Cross-Origin-Opener-Policy: same-origin`、`Cross-Origin-Resource-Policy: same-origin`、`X-XSS-Protection: 0`、`Content-Security-Policy`（`default-src 'self'`，禁用内联脚本/样式、`object-src 'none'`、`frame-ancestors 'none'`）；HTTPS 时追加 `Strict-Transport-Security`。响应头不含 `Server`；`Server` 头也在 Kestrel 层关闭。

静态资源（HTML/JS/CSS）响应带 `Cache-Control: no-store`。

### 2.6 限流与自动封禁

固定窗口计数与登录失败锁定都在**进程内存**（重启即清零）。

| 场景 | 计数键 | 默认阈值 | 配置项 | 超限响应 |
| --- | --- | --- | --- | --- |
| 登录 | `login:ip:{ip}` | 120 次 / 5 分钟 | `Limits:LoginPerFiveMinutesPerIp` | 429 `RATE_LIMITED` + 审计 `auth.rate-limit` |
| 注册（管理员创建账号） | `register:user:{id}` | 20 次 / 1 小时 | 硬编码 | 429 `RATE_LIMITED` |
| 修改口令 | `password:{userId}` | 5 次 / 15 分钟 | 硬编码 | 429 `RATE_LIMITED` |
| 对话 `/api/agent/chat` | `chat:user:{id}` 与 `chat:ip:{ip}` | 20 次 / 5 分钟、200 次 / 5 分钟 | `Limits:AgentChatPerFiveMinutesPerUser` / `...PerIp` | 429 + 审计 `agent.rate-limit` |
| 提交报销单 `/api/invoice/submit` | `invoice:user:{id}` | 30 次 / 5 分钟 | 硬编码 | 429 `RATE_LIMITED` |
| 报销单审核/修改/删除/回退 | `invoicewrite:{userId}` | 60 次 / 1 分钟 | `Limits:AdminWritePerMinutePerAdmin` | 429 `RATE_LIMITED` |
| 后台写操作 | `adminwrite:{userId}` | 60 次 / 1 分钟 | `Limits:AdminWritePerMinutePerAdmin` | 429 `RATE_LIMITED` |
| 所有 `GET /api/*` | `read:user:{id}` 或 `read:ip:{ip}` | 600 次 / 1 分钟 | `Limits:ReadPerMinutePerUser` | 429（`ok` 结构） |

**请求体 WAF（`Security:WafScanBody`，默认 `true`）**

路径、查询串、请求头之外，**状态变更请求的 JSON body 也会被扫描**（攻击特征藏在 body 里同样拦得住）。扫描前会先还原 `\uXXXX` 与一层 `%XX` 编码，避免用编码绕过。

body 侧用的是**更保守**的特征集——只拦"在本系统任何正常业务里都不该出现"的形态：SQL 注入（`' OR 1=1--`、`union select`、`information_schema`、`xp_cmdshell`、`into outfile`…）、JNDI、SSRF（`gopher://` / `dict://` / `file:///` / 云元数据地址）、CRLF 注入、路径穿越特征。

**XSS / 命令注入 / 模板注入刻意不放进 body 拦截集**：写文件接口会合法写入脚本内容，若在此拦截，正常的"帮我写个脚本"根本提交不了——那类内容由 `ContentRiskScanner` 判定并**强制人工审批**处理。命中 body 规则时返回 `400 {"code":"WAF_BLOCKED"}`，并记审计 `waf.body`、按 WAF 计分。

**IP 白名单（`Security:AllowlistIps`，也可用 `AISERVER_ALLOWLIST_IPS`，分号分隔）**

用于安全测试放行。白名单 IP **只豁免限流与自动封禁**：

- 上表所有限流对该 IP 恒放行（仍计数，`SecurityGuard.AllowlistedHits` 可查）；
- `IsBanned` 恒 `false`，`Ban` 直接返回，WAF/蜜罐**照常记分但不封禁**；
- **不豁免**：JWT 校验、请求签名、防重放、权限矩阵、规则引擎、枚举白名单、路径守卫、WAF 拦截本身（命中仍 400）。

只接受可解析的**字面 IP**；`*`、CIDR、域名会在启动时告警并被忽略（不静默放行）。命中默认记审计 `security.allowlist`（`Security:AllowlistAuditEveryHit`，默认 true），`/api/admin/security` 的 `allowlist / allowlistedHits` 字段可查看。

**可信代理与真实 IP 还原（`Security:TrustedProxies`，环境变量 `AISERVER_TRUSTED_PROXIES`）**

协作方网关是服务端代打，所有用户对服务器来说都是同一个来源 IP，会让限流与登录锁定退化成"全局"。为此提供：

- 当 **TCP 对端就是 `TrustedProxies` 里的 IP** 时，采信它带来的 `X-Client-Ip` 头作为请求的真实来源；
- **其他来源带这个头一律忽略**（防伪造），一律使用 TCP 对端地址；
- 头值必须能解析成**单个字面 IP**，逗号串 / CIDR / 乱码 → 回退到对端地址，不报错；
- 生效后，限流、封禁、登录锁定的键、以及审计的 `ip` 字段，全部按真实用户 IP 计算。

`X-Client-Ip` 只用于"还原来源"，不参与鉴权——令牌与签名校验照旧。

`Limits:BizActionPerFiveMinutesPerUser` 与 `Limits:BizActionPerFiveMinutesPerIp` 是旧业务对接台留下的配置项，当前代码里已**没有调用方**（对接台已移除），仅为兼容旧配置文件保留。

账号级防护（`AuthApi` + `UserService`）：

- **锁定按 (来源 IP + 账号) 组合计数**（不再是账号全局锁定）：同一来源对同一账号连续失败达到 `Limits:LoginPerFifteenMinutesPerAccount`（默认 5 次）→ 锁定该**组合** `Limits:AccountLockMinutes`（默认 15 分钟），审计 `auth.lockout`。
  这样攻击者只能锁死"自己这条 IP 对某个账号"的通道，**无法通过连错他人账号把人锁在门外**；锁定期间该来源用正确口令也被 401 `AUTH_LOCKED`，而**其他来源、其他账号不受影响**。
- **账号不存在 / 账号被禁用 / 口令错误 三条路径都只跑一次等价的 PBKDF2**，响应时间不可区分（防用户名枚举）；账号级失败次数仅用于审计，不再据此锁定。
- 账号不存在时同样执行一次等价 PBKDF2 计算（消除用户名枚举的时间差），并额外延迟约 120ms（口令错误约 60ms）。
- 错误文案统一为「用户名或口令错误」，不泄露账号是否存在。

口令存储（慢哈希 + 服务端密钥）：

| 项 | 说明 |
| --- | --- |
| 方案标识 | `pbkdf2-sha256+pepper`（当前） / `pbkdf2-sha256`（早期数据，无服务端密钥） / `partner-pbkdf2-sha256`（从协作方用户表导入） |
| 计算方式 | `PBKDF2-HMAC-SHA256( HMAC-SHA256(服务端密钥, 口令), 每账号随机盐, 迭代次数 )`，输出 32 字节 base64 |
| 迭代次数 | `Security:PasswordIterations`（默认 1200000，可用环境变量 `AISERVER_PASSWORD_ITERATIONS` 覆盖）；调大更抗离线爆破，代价是每次登录更慢 |
| 服务端密钥 | 由本机密钥材料派生（`App_Data/secrets.json` 自动生成，生产用 `AISERVER_AES_KEY` 注入）。**没有它，即使拿到 `users.json` 也无法离线爆破口令**；但换掉它会同时让所有加密字段与口令失效，必须与备份一起保管 |
| 自动升级 | 登录成功后若账号仍停在旧方案或迭代次数低于当前配置，立即用新参数重算并写回（用户无感知，口令不变），审计 `auth.password-upgraded` |
| 管理端可见 | `GET /api/admin/users` 返回 `passwordScheme`（`none` = 尚未设置口令）与 `passwordIterations`，便于核对哪些账号还没升级 |

自动封禁（`Security/SecurityGuard.cs`）：

| 触发 | 计分 | 阈值 |
| --- | --- | --- |
| WAF 命中 | +2 | 累计满 `Limits:WafStrikesBeforeBan`（默认 8）→ 封禁 `Limits:BanMinutes`（默认 60 分钟） |
| 非管理员访问 `/api/admin/*` | +3 | 同上 |
| 请求签名校验失败 | +2 | 同上 |
| 令牌校验失败 / 登录失败 / 跨站来源 | +1 | 同上 |
| 蜜罐路径探测（GET） | +1 | 累计满 `Limits:HoneypotStrikesBeforeBan`（默认 3）→ 封禁 |
| 向蜜罐后台 POST 凭据 | +3 | 直接封禁 |

被封禁期间的所有请求：`403 {"error":"请求被安全策略拒绝","code":"IP_BANNED"}`，并按 IP 限频写审计 `security.blocked`。

来源 IP 只取 TCP 连接对端地址（不信任 `X-Forwarded-For`）；反向代理部署需在代理层覆盖。

### 2.7 请求体上限

- `Security:MaxRequestBodyBytes`（默认 262144，即 256 KiB）：Kestrel `MaxRequestBodySize` 与中间件缓冲双重校验，超限抛 `BadHttpRequestException` → `413 PAYLOAD_REJECTED`。
- 请求头超时 15 秒、Keep-Alive 60 秒（Kestrel 层，硬编码）。
- 对话内容总长限制 `Security:MaxUserInputChars`（默认 2000 字符）：超长**不报错**，返回 200 且 `data.status = "REJECTED_INPUT"`。

---

## 3. 页面路由与蜜罐

### 3.1 页面与静态资源

| 方法 | 路径 | 响应 | 说明 |
| --- | --- | --- | --- |
| GET | `/` | 200 `text/html` | 业务工作台（`wwwroot/index.html`） |
| GET | `/index.html` | 200 `text/html` | 同上文件 |
| GET | `/admin` | 200 `text/html` | 管理后台（`wwwroot/admin.html`） |
| GET | `/admin/` 及 `/admin/{*rest}` | 302 → `/admin` | 带斜杠/子路径统一重定向（目标是硬编码常量，不存在开放重定向） |
| GET | `/finance` | 200 `text/html` | 财务工作台（`wwwroot/finance.html`）；页面自身按角色拦截：非 `finance` / `admin` / `superadmin` 显示"无权访问"面板 |
| GET | `/finance/` 及 `/finance/{*rest}` | 302 → `/finance` | 带斜杠/子路径统一重定向（目标是硬编码常量，不存在开放重定向） |
| GET | `/assets/*.js`、`/assets/app.css` 等 | 200 | 由静态文件中间件提供（`wwwroot/` 下的实际文件） |
| GET | `/robots.txt` | 200 `text/plain` | 诱饵：`Disallow` 列出的路径正是蜜罐路径；命中写审计 `recon.robots`（`OBSERVED`，不拦截、不记分） |
| 任意 | 其它未匹配路径 | 404 JSON | `MapFallback`：`{"ok":false,"code":"NOT_FOUND","message":"未找到资源"}`（不区分"路径不存在"与"被安全策略忽略"） |

> 页面路由是**显式映射的固定文件**，不接受任何路径参数，避免路径穿越。

### 3.2 蜜罐路由

命中即视为攻击信号（正常用户不会访问这些路径）。完整清单见 `Security/Honeypot.cs` 的 `DecoyPaths`：

```
/admin.php            /admin/login.php      /wp-login.php        /wp-admin     /wp-admin/
/phpmyadmin           /pma                 /.env                /.env.local   /.git/config
/config.php           /backup.sql          /db.sql              /dump.sql     /administrator
/manager/html         /actuator/env        /actuator/health     /druid/index.html
/console              /jenkins             /api/v1/admin/users  /api/v1/admin/login
/swagger/index.html   /server-status       /owa/auth/logon.aspx /hudson
/solr/admin           /.svn/entries        /web.config
```

目录型前缀：`/wp-admin/`、`/.git/`、`/.svn/`、`/phpmyadmin/`（匹配 `前缀*`）。

行为：

| 请求 | 响应 | 审计 | 处置 |
| --- | --- | --- | --- |
| `GET /admin.php` | 200 `text/html` 诱饵登录页（"运维控制台 V3.2"，不含任何真实接口） | `honeypot.probe` | +1 分 |
| `POST` 任意蜜罐路径 | `403 {"error":"请求被安全策略拒绝","code":"IP_BANNED"}` | `honeypot.credential`（`BANNED`） | +3 分并**立即封禁**（凭据不落库，仅记录行为） |
| `GET` 其它蜜罐路径 | `404 {"error":"未找到资源","code":"NOT_FOUND"}`（与普通 404 表现一致） | `honeypot.probe` | +1 分 |

---

## 4. 鉴权与账号接口

### 4.1 公开接口（无需令牌、无需签名）

| 方法 | 路径 | 请求体 | 限流 | 审计 |
| --- | --- | --- | --- | --- |
| GET | `/api/public/health` | 无 | 读接口 180 次/分钟（按来源 IP） | 无 |
| POST | `/api/auth/login` | `{ userName, password }` | 6 次/5 分钟（按 IP） | `auth.login`、`auth.lockout`、`auth.rate-limit` |

> `POST /api/auth/register` **不再是公开接口**（创建账号是管理员行为），见 4.2。
> 未带令牌访问它会被安全中间件以 `401 UNAUTHENTICATED` 拦下，请求不会到达业务处理逻辑。

**GET `/api/public/health`** —— 运行状态与降级说明。

`data` 字段：

| 字段 | 说明 |
| --- | --- |
| `status` | 固定 `ok` |
| `serverTime` | 服务器 UTC 时间 |

> **刻意只回这两个字段。** 早期版本在这里暴露过 `aiConfigured` / `aiModel` / `aiStatus` / `aiLastError` / `mode` / `selfRegistration`，
> 等于把"用的哪家模型、AI 开没开、内部错误文案、配置开关状态"免费告诉未认证的扫描者。
> 需要看 AI 状态的界面改用**登录后**的 `GET /api/auth/me`（该接口返回 `aiConfigured` 与 `aiStatus`）。

**POST `/api/auth/login`** —— 登录换取令牌与签名密钥。

请求字段（`Api.Clean` 后：用户名 ≤ 64 字符；口令不做清洗）：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `userName` | 是 | 用户名 |
| `password` | 是 | 口令；长度必须为 1 ~ 128，否则 400 |

成功（200）`data`：

| 字段 | 说明 |
| --- | --- |
| `token` | JWT 访问令牌 |
| `signKey` | 请求签名密钥（后续所有状态变更请求都要用它） |
| `expiresAt` | 过期时间 |
| `user` | `{ id, userName, displayName, employeeId, role, roleLabel, emailMasked, permissions: [{ key, label }] }`（`employeeId` 为工号，形如 `E0001`） |
| `signing` | `{ algorithm: "HMAC-SHA256", canonical: "METHOD\nPATH?QUERY\nX-Timestamp\nX-Nonce\nSHA256_HEX(rawBody)", headers: ["X-Timestamp","X-Nonce","X-Signature"], requiredFor: ["POST","PUT","PATCH","DELETE"] }` |

失败：

| 状态码 | code | 场景 |
| --- | --- | --- |
| 403 | `IP_BANNED` | 来源 IP 已被封禁 |
| 429 | `RATE_LIMITED` | 超过 6 次/5 分钟（同时记 1 分） |
| 400 | `BAD_REQUEST` | 请求体非法、用户名/口令为空、口令超过 128 字符 |
| 401 | `AUTH_FAILED` | 用户名或口令错误 / 账号被禁用 / 账号被锁定（`message` 中给出解锁时间） |

> 该接口是公开路径，因此**不校验请求签名**；但仍受 Origin 校验约束（浏览器跨站提交会被 `BAD_ORIGIN` 拒绝）。

### 4.2 需要身份（令牌）的账号接口

| 方法 | 路径 | 权限 | 签名 | 请求体 | 响应 `data` | 失败码 | 审计 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| POST | `/api/auth/register` | `account.create`（目标为管理员时还需 `account.grant_admin`） | 需要 | `{ userName, password, displayName, email, role }` | `{ message, userName, employeeId, role, roleLabel, mirrorState, mirrorDemoPassword, mirrorNote }` | 401 / 403 `FORBIDDEN` / 429 / 400 | `auth.register` |
| POST | `/api/auth/logout` | 任意已登录角色 | 需要 | 无（或 `{}`） | `{ message }` | 401 | `auth.logout` |
| GET | `/api/auth/me` | 任意已登录角色 | 不需要 | 无 | `{ id, userName, displayName, employeeId, role, roleLabel, emailMasked, permissions[], lastLoginAt }` | 401 | 无 |
| POST | `/api/auth/password` | 任意已登录角色 | 需要 | `{ newPassword }` | `{ message, relogin: true }` | 429 / 400 `WEAK_PASSWORD` / 500 `FAILED` | `user.password` |

- `POST /api/auth/register` 是创建账号的第一个入口，另一个入口是 `POST /api/admin/users`（见 7.2），两者共用同一个 `UserService.CreateAsync`。
- `POST /api/auth/logout`：只写审计并返回提示，服务端**没有令牌吊销列表**（令牌在过期或世代变更前仍然有效），客户端需要自行清除本地令牌。
- `POST /api/auth/password`：独立限流 5 次/15 分钟；成功后 `TokenEpoch++`，所有旧令牌立即失效，必须重新登录；同时会用新口令重新生成飞书镜像的演示口令。
- 四个接口都要求 `Authorization: Bearer`；写接口（register/logout/password）还需要请求签名。

**POST `/api/auth/register`** —— 创建账号。**这是管理员行为**：需要登录，且操作者必须拥有 `account.create` 权限（对齐权限矩阵：员工与财务不能注册账号）。

请求字段（`Api.Clean` 截断）：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `userName` | 是 | ≤ 32 字符；3-32 位，仅字母/数字/下划线/点/短横线 |
| `password` | 是 | ≥ 10 位且包含大写/小写/数字/符号中至少三类；不得包含用户名、`admin`、常见弱口令片段 |
| `displayName` | 否 | ≤ 32 字符，缺省取用户名 |
| `email` | 否 | ≤ 128 字符；格式非法直接失败；仅以掩码形式保存展示，原值 AES-256-GCM 加密存储 |
| `role` | 否 | 四角色之一（`superadmin` / `admin` / `finance` / `employee`），缺省为 `employee`；传 `superadmin` 会被策略拒绝 |

成功（200）`data`：

| 字段 | 说明 |
| --- | --- |
| `message` | 形如"已创建账号 xxx（员工，工号 E0001）" |
| `userName` / `employeeId` / `role` / `roleLabel` | 新账号的基本信息（工号由服务端分配） |
| `mirrorState` | 飞书镜像结果：`synced` / `skipped` / `failed`（失败不影响账号创建） |
| `mirrorDemoPassword` | 镜像模式为 `demo` 时给出的**演示口令**（可交给协作方；与用户真实口令无关，见 [飞书镜像对接.md](飞书镜像对接.md)） |
| `mirrorNote` | 镜像结果的文字说明 |

失败：

| 状态码 | code | 场景 |
| --- | --- | --- |
| 401 | `UNAUTHENTICATED` | 未登录（该路径不在公开白名单内，中间件层直接拦下） |
| 403 | `FORBIDDEN` | 账号策略拒绝：无 `account.create` 权限、目标角色非法、目标为超管、创建管理员但操作者不是超管（原因原文在 `message`）；同时给来源 IP 记 2 分 |
| 429 | `RATE_LIMITED` | 超过 20 次/小时（按操作者账号） |
| 400 | `BAD_REQUEST` / `REGISTER_FAILED` | 请求体非法；用户名重复、口令不合规、邮箱非法（原因在 `message`） |

审计：`auth.register`（`SUCCESS` / `FAIL` / `DENIED`）。

---

## 5. 对话式代理 `/api/agent/*`

权限：任意已登录角色（`superadmin` / `admin` / `finance` / `employee`）。
`Scope`：所有接口都作用于**当前登录者自己的**数据；管理员查看他人数据请走 `/api/admin/*`。

### 5.1 接口总表

| 方法 | 路径 | 说明 | 签名 | 限流 | 审计 |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/agent/tools` | 当前角色可用的工具目录与权限 | 否 | 读 180/分钟 | 无 |
| GET | `/api/agent/help` | 本地指令帮助与示例 | 否 | 读 180/分钟 | 无 |
| POST | `/api/agent/chat` | 对话式代理（多步循环） | **是** | 20/5 分钟（用户）、60/5 分钟（IP） | 见 5.3 |
| GET | `/api/agent/tasks` | 我的任务列表（最多 100 条） | 否 | 读 180/分钟 | 无 |
| GET | `/api/agent/tasks/{id}` | 单个任务详情 | 否 | 读 180/分钟 | 无 |
| GET | `/api/agent/files?scope=&path=` | 只读直通：列目录 | 否 | 读 180/分钟 | 无 |
| GET | `/api/agent/files/content?scope=&path=` | 只读直通：读文件 | 否 | 读 180/分钟 | 无 |
| GET | `/api/agent/notices` | 我的站内通知（最多 50 条） | 否 | 读 180/分钟 | 无 |
| GET | `/api/agent/versions` | 我的版本/快照记录（最多 100 条，含 diff） | 否 | 读 180/分钟 | 无 |
| POST | `/api/agent/session/reset` | 清空我的会话上下文 | **是** | 无单独限流 | 无 |

所有接口未登录均返回 `401 UNAUTHENTICATED`。

### 5.2 GET `/api/agent/tools`

`data`：

| 字段 | 说明 |
| --- | --- |
| `role` / `roleLabel` | 当前角色与中文名 |
| `permissions` | `[{ key, label }]`，当前角色的权限键（如 `file.read`、`client.search`、`user.read` 等） |
| `tools` | 工具数组：`name`、`title`、`description`、`risk`（`LOW/MEDIUM/HIGH/CRITICAL`）、`riskLabel`、`requiresApproval`、`stateChanging`、`permissions`（中文标签数组）、`params`（参数名 → 说明）、`required`（必填参数名数组） |

> 该接口**始终过滤掉 `AdminOnly` 工具**，即使调用者是管理员。

### 5.3 POST `/api/agent/chat`

请求：`{ "message": "读一下日报并追加一行总结", "sessionId": "可选，≤40 字符" }`

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `message` | 是 | 空白 → 400 `EMPTY_MESSAGE`；超过 `Security:MaxUserInputChars`（2000）→ 200 且 `status=REJECTED_INPUT` |
| `sessionId` | 否 | 省略则新建会话；用于多轮上下文（进程内存储） |

成功（200）`data`：

| 字段 | 说明 |
| --- | --- |
| `sessionId` | 会话 id |
| `reply` | 给用户的回复（自动执行的动作用的是**真实执行结果**摘要，不是模型的推测） |
| `source` | 计划来源：`ai`（模型解析）/ `ai-fallback`（模型输出不可解析，退回本地解析）/ `quota-fallback`（今日额度用尽，退回本地解析）/ `local-fallback`（未配置 AI 或调用失败，本地指令解析）。历史值 `biz-ui`（旧业务对接台的结构化动作）仍存在于 `AgentOrchestrator.HandleActionAsync` 的路径里，但随着对接台移除，当前没有 HTTP 入口会触发它 |
| `status` / `statusLabel` | `CHAT` 对话、`NEED_INPUT` 需要补充信息、`REJECTED_INPUT` 输入非法、`REJECTED_RULE` 规则拦截、`REJECTED_AI` 审核拒绝、`PENDING_APPROVAL` 待人工审批、`EXECUTED` 已执行、`FAILED` 执行失败 |
| `taskId` | 产生任务时的任务号（其余情况为 `null`） |
| `tool` / `intent` | 选中的工具与意图摘要 |
| `riskLevel` / `riskLabel` | `LOW/MEDIUM/HIGH/CRITICAL` 与中文名 |
| `actionSummary` | 规范化动作摘要 |
| `reviewVerdict` | AI 安全审核结论：`allow`（放行）/ `deny`（拒绝，任务落为 `REJECTED_AI`）/ `review`（转人工，含"审核返回不可解析"的保守处理）/ `skipped`（被规则引擎拦下前未做审核）/ `not-required`（只读动作不做审核）/ `unavailable`（审核服务不可用）/ `admin-action`（管理员直接操作） |
| `injectionFlags` | 命中的注入特征标签（仅记录，不阻断） |
| `notes` | 过程提示（降级说明、翻译记录、重复调用收敛提示等） |
| `result` | 自动执行时的工具返回数据 |
| `steps` | 多步执行轨迹：`[{ index, tool, title, args, summary, status, result, elapsedMs }]` |
| `translations` | 参数被系统翻译的记录（口语 → 枚举值、名字 → 编号） |
| `stepLimitReached` | 是否因步数/墙钟上限而停止（此时回答基于已获得的信息） |

失败：

| 状态码 | code | 场景 |
| --- | --- | --- |
| 400 | `EMPTY_MESSAGE` / `BAD_REQUEST` | 内容为空 / 请求体非法 |
| 429 | `RATE_LIMITED` | 用户或 IP 维度超限（审计 `agent.rate-limit`） |
| 401 | `UNAUTHENTICATED` / `BAD_TOKEN` / `TOKEN_STALE` / `BAD_SIGNATURE` | 身份或签名问题 |
| 499 | `CANCELLED` | 客户端断开 |

行为要点（`Ai/AgentOrchestrator.cs`）：

- 多步循环：只读工具会自动连续执行（先搜 → 再读 → 汇总），上限为 `Ai:MaxAgentSteps`（默认 5，未配置 AI 时强制 1）与 `Ai:AgentTimeoutSeconds`（默认 60 秒，钳制 15~300）。
- **一旦出现状态变更动作，循环立即停止并转人工审批**，不会边审批边继续跑。
- 同一个规范化动作重复出现会被收敛（不重复执行）；同一工具重复调用会注入"不要重复"的提示。
- AI 未配置或调用失败时降级为本地意图解析（`source = local-fallback`），变更类操作全部转人工审批。

审计写入：`input.injection-suspect`、`ai.plan`、`ai.review`、`ai.declined`、`agent.rate-limit`，以及任务链路的 `task.create` / `task.execute` / `task.reject-rule` / `task.reject-ai`。

### 5.4 GET `/api/agent/tasks` 与 `/api/agent/tasks/{id}`

返回 `TaskService.ToUserView` 的结构（数组或单对象）：

| 字段 | 说明 |
| --- | --- |
| `id` | 任务号（形如 `t_xxxxxxxx`） |
| `intent` | 意图摘要 |
| `tool` | 工具名 |
| `action` | 规范化动作（对象） |
| `riskLevel` / `riskLabel` | 风险等级与中文名 |
| `status` / `statusLabel` / `statusReason` | `PENDING_APPROVAL`、`EXECUTING`、`EXECUTED`、`REJECTED_RULE`、`REJECTED_AI`、`REJECTED_ADMIN`、`EXPIRED`、`CANCELLED`、`FAILED` 及其说明 |
| `ruleReasons` | 规则引擎判定理由 |
| `reviewVerdict` / `reviewReason` | AI 审核结论与理由 |
| `injectionFlags` | 注入特征 |
| `createdAt` / `decidedAt` / `decidedBy` / `decisionReason` / `executedAt` | 生命周期时间与裁决信息 |
| `result` | 执行结果对象 |
| `versionIds` | 关联的版本/快照 id |
| `rollbacked` | 是否已被回退 |
| `approvalExpiresAt` | 审批有效期 |

- 列表最多 100 条，按创建时间倒序。
- `GET /api/agent/tasks/{id}`：任务不存在 → 404 `NOT_FOUND`；**不是自己的任务且自己不是管理员** → 403 `FORBIDDEN`（水平越权防护）。

### 5.5 GET `/api/agent/files` 与 `/api/agent/files/content`

界面用的**只读直通**：仍然经过规则引擎（权限、路径守卫、参数白名单），但不产生任务、不进入审批。

| 查询参数 | 默认 | 说明 |
| --- | --- | --- |
| `scope` | `private` | `private`（我的工作区）/ `share`（共享区，需 `file.share.read` 权限） |
| `path` | 空 | 相对路径；`list_files` 可为空表示当前目录 |

成功（200）：`data = { summary, data }`（`summary` 为人类可读摘要，`data` 为工具原始返回，结构随工具而定）。

失败：

| 状态码 | code | 场景 |
| --- | --- | --- |
| 403 | `RULE_DENIED` | 规则引擎判定 DENY（权限不足、路径非法、保护文件等，理由在 `message`） |
| 400 | `BAD_TOOL` | 命中的工具不是只读工具（理论上不会发生） |
| 400 | `EXECUTE_FAILED` | 执行失败（例如文件不存在），原因在 `message` |

### 5.6 GET `/api/agent/notices`

返回当前用户可见的出站通知（最多 50 条）：`{ id, createdAt, subject, body, status, taskId, sentAt, error }`。
`status` 取值见 `OutboxMessage`：`QUEUED` / `SENT` / `FAILED` / `RECALLED`。
审批人收到的"待审批"通知不属于提交人的收件箱，因此不会出现在这里。

### 5.7 GET `/api/agent/versions`

返回当前用户的版本/快照记录（最多 100 条）：`{ id, taskId, kind, operation, target, createdAt, addedLines, removedLines, diff, reverted, revertedBy }`。

### 5.8 POST `/api/agent/session/reset`

清空当前用户的对话上下文（进程内存态），返回 `{ message: "会话上下文已清空" }`。需要请求签名（POST）。

---

## 6. 报销单 `/api/invoice/*`

**本服务是报销单的唯一写入口**：协作方前端只调这里，由服务端校验、盖章、写飞书并留版本与审计；数据落在飞书「报销单主表V2」。
行为细则（合规规则、状态流转、数据边界、回退语义、财务工作台说明）见 [报销单写入口与财务流程.md](报销单写入口与财务流程.md)。

| 方法 | 路径 | 所需权限键 | 签名 | 限流 | 审计 |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/invoice/meta` | 任意已登录角色 | 否 | 读 180/分钟 | 无 |
| GET | `/api/invoice/list` | `invoice.read.self`（员工自动只返回本人） | 否 | 读 180/分钟 | `invoice.list` |
| GET | `/api/invoice/detail` | 任意已登录角色（归属校验：员工只能看自己的） | 否 | 读 180/分钟 | 无 |
| POST | `/api/invoice/submit` | `invoice.submit` | **是** | 30 次/5 分钟（按账号） | `invoice.submit` |
| POST | `/api/invoice/review` | `invoice.review` | **是** | 后台写限流（默认 60 次/分钟） | `invoice.review` |
| POST | `/api/invoice/update` | `invoice.update` | **是** | 后台写限流 | `invoice.update` |
| POST | `/api/invoice/delete` | `invoice.delete`（仅管理员及以上） | **是** | 后台写限流 | `invoice.delete` |
| POST | `/api/invoice/rollback` | `log.rollback`（仅管理员及以上） | **是** | 后台写限流 | `invoice.rollback` |

角色与权限键的对应关系见 [账号与权限设计.md](账号与权限设计.md)：员工只有 `invoice.read.self` 与 `invoice.submit`；财务部另有 `invoice.read.all`、`invoice.review`、`invoice.update`；管理员与超级管理员另有 `invoice.delete`、`log.rollback`。

所有响应都是 1.2 的统一结构；被权限或策略拒绝时返回 4xx，`message` 为可直接展示给用户的中文原因。

### 6.1 GET `/api/invoice/meta`

给前端做下拉与前置校验用的契约元数据（纯本地计算，不依赖飞书）。

`data` 字段：

| 字段 | 说明 |
| --- | --- |
| `tableUrl` | 报销单原表在飞书网页端的地址，供前端做「在飞书中打开原表」的跳转。取值来自 `Feishu:TableUrl`；未显式配置时，若 `Feishu:AppToken` 与 `Feishu:TableId` 都在，服务按 `https://my.feishu.cn/base/{appToken}?table={tableId}` 自动拼一条；两者都不满足时为空串 |
| `userTableUrl` | 用户表在飞书网页端的地址，拼接规则同上（用 `Feishu:UserTableUrl` 与 `Feishu:UserTableId`） |
| `feishuConfigured` | 是否已配置 `Feishu:AppId` / `AppSecret` / `AppToken` / `UserTableId` 四项（即账号镜像链路可用）。**注意它比报销单写入的要求更严**：报销单只需要前三项加 `Feishu:TableId`，因此这里为 `false` 时报销单接口仍可能可用 |
| `departments` / `types` / `invoiceStates` / `complianceResults` / `statuses` / `cities` | 六组枚举字典（部门 11 项、报销类型 4 项、票据状态 3 项、合规结果 2 项、状态 4 项、城市 37 项） |
| `cityTierCaps` | 住宿分级上限：`{ tier1: 500, tier2: 350, tier3: 250 }` |
| `amountRange` | `{ min: 0.01, max: 100000 }` |
| `readonlyFields` | 只读列（`单号`、`发票附件`） |
| `serverStampedFields` | 服务端盖章列（申请人、工号、记录提交时间、合规结果、风险摘要、状态） |
| `financeEditableFields` | 财务可改列白名单 |
| `rules` | 合规预审规则的可读文字（6 条） |
| `canSeeAll` / `canReview` / `canUpdate` / `canDelete` / `canRollback` | 当前登录角色在这张表上的能力（前端据此显隐按钮；服务端仍会再校验一次） |

### 6.2 GET `/api/invoice/list`

查询参数：`limit`（可选，默认 100，内部限制在 1 到 500）。

`data` 字段：

| 字段 | 说明 |
| --- | --- |
| `scope` | `all`（财务及以上）或 `mine`（员工） |
| `applicant` | 当前登录人姓名 |
| `count` | 返回条数 |
| `rows` | 单据数组，字段见 6.3 |

失败码：401 `UNAUTHENTICATED`、403 `FORBIDDEN`（无 `invoice.read.self`）、502 `FEISHU_UNAVAILABLE`（飞书未配置或调用失败，`message` 说明具体原因）。

### 6.3 GET `/api/invoice/detail`

查询参数：`recordId`（必填，飞书记录号）。

`data.row` 字段（与列表行结构一致）：

| 字段 | 含义 |
| --- | --- |
| `recordId` | 飞书记录号（审核、修改、删除、回退都用它） |
| `billNo` | 单号（飞书自动编号 `BX-` + 六位） |
| `applicant` / `employeeId` | 申请人姓名 / 工号 |
| `department` / `rtype` | 部门 / 报销类型 |
| `amount` | 金额（数字） |
| `reason` | 事由 |
| `hasInvoice` | 票据状态 |
| `city` / `invoiceDate` | 城市 / 发票日期（毫秒时间戳字符串） |
| `submittedAt` | 记录提交时间（毫秒时间戳字符串） |
| `compliance` / `riskSummary` | 合规结果 / 风险摘要 |
| `status` / `reviewer` / `reviewOpinion` / `reviewedAt` | 状态 / 审核人 / 审批意见 / 审批时间 |
| `invoiceInfo` | 发票信息（号码；销售方） |
| `raw` | 飞书原始字段的 JSON 文本映射，排查字段差异时用 |

失败码：401、403 `FORBIDDEN`（员工读他人单据，`message` 为"无权查看他人的报销单"）、404 `NOT_FOUND`（记录不存在或已被删除）。

### 6.4 POST `/api/invoice/submit`

请求字段：

| 字段 | 必填 | 约束 |
| --- | --- | --- |
| `department` | 是 | 必须命中 11 个部门之一 |
| `rtype` | 是 | `差旅费` / `招待费` / `办公用品` / `交通费` |
| `amount` | 是 | 0.01 到 100000 |
| `reason` | 是 | 2 到 200 个字符 |
| `hasInvoice` | 是 | `有票` / `无票` / `未提`（`无票` 会被阻断） |
| `city` | 差旅费必填 | 必须命中 37 城清单 |
| `invoiceDate` | 否 | `yyyy-MM-dd`，非法 → 400 `BAD_DATE` |
| `invoiceInfo` | 否 | 不超过 128 个字符 |
| `taskId` | 否 | 关联任务号（审计用，可空） |

前端传的 `申请人`、`工号`、`记录提交时间`、`合规结果`、`风险摘要`、`状态` 一律不生效，服务端用登录身份与预审结果覆盖。

成功（200）`data`：`{ recordId, billNo, message, hint }`。
`billNo` 是写完从飞书**回取的自动编号**（形如 `BX-000123`）；飞书自动编号有极短的最终一致性延迟，
服务端会短暂重试一次，仍取不到时返回空串（此时 `message` 里也不会编造"系统生成中"）。
`message` 形如"报销单已提交：单号 BX-000123，招待费 2500 元，合规结果 风险"。

失败码：401、403 `FORBIDDEN`（无提交权限）、429 `RATE_LIMITED`、400 `BAD_REQUEST`、400 `BAD_DATE`、400 `SUBMIT_REJECTED`（校验未通过、无票阻断或飞书不可用，`message` 为原因）。

### 6.5 POST `/api/invoice/review`

请求字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `recordId` | 是 | 飞书记录号 |
| `status` | 是 | 目标状态，只能是 `审核中` / `已通过` / `已驳回`；改回 `待审核` 会被拒 |
| `opinion` | 是 | 审批意见，2 到 300 个字符 |
| `taskId` | 否 | 关联任务号 |

服务端会一并写入 `审核人`（当前登录人姓名）与 `审批时间`。

成功（200）`data`：`{ recordId, message }`。

失败码：401、403 `FORBIDDEN`、429 `RATE_LIMITED`、400 `BAD_REQUEST`、400 `REVIEW_REJECTED`（状态流转非法、意见长度不合法、单据不存在或飞书不可用）。

### 6.6 POST `/api/invoice/update`

请求字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `recordId` | 是 | 飞书记录号 |
| `fields` | 是 | 字段名到新值的映射，至少一个；键只能是财务可改字段白名单 |
| `taskId` | 否 | 关联任务号 |

取值约束由服务端再次校验：枚举字段必须在字典内；`金额` 必须是 0.01 到 100000 的数字；`发票日期` 必须是 `yyyy-MM-dd`；`事由` 与 `风险摘要` 不超过 300 字符；`单号`、`发票附件` 等只读列一律拒绝。

成功（200）`data`：`{ recordId, message }`。

失败码：401、403 `FORBIDDEN`、429 `RATE_LIMITED`、400 `BAD_REQUEST`、400 `UPDATE_REJECTED`（字段不允许修改、取值非法、没有可改字段或飞书不可用）。

### 6.7 POST `/api/invoice/delete`

请求字段：`recordId`（必填）、`reason`（必填，2 到 200 字符）、`taskId`（可选）。

删除前把整行快照存入版本库，因此可以按日志回退重建。成功（200）`data`：`{ recordId, message }`，`message` 会提示"快照已保存，可按日志回退重建"。

失败码：401、403 `FORBIDDEN`（仅管理员及以上）、429 `RATE_LIMITED`、400 `REASON_REQUIRED`（未填删除理由）、400 `DELETE_REJECTED`。

### 6.8 POST `/api/invoice/rollback`

请求字段：`versionId`（必填，来自 `/api/admin/versions`；对应版本 `kind` 必须是 `invoice`）、`taskId`（可选）。

行为：按版本的撤销日志分支处理——原动作是 `submit` 则删除该记录（记录已不存在时视为已回退，返回成功）；原动作是 `delete` 则按快照重建（**单号由飞书重新分配**，并另记一条 `rebuild` 版本）；原动作是 `review` 或 `update` 则把快照里的旧值写回（写回前按字段类型归一：`金额` 转数字、日期列转毫秒数字、文本去掉快照里的引号）。

成功（200）`data`：`{ recordId, message }`。

失败码：401、403 `FORBIDDEN`（仅管理员及以上）、429 `RATE_LIMITED`、400 `ROLLBACK_REJECTED`（版本不存在、版本类型不符、已经回退过、快照缺失或飞书不可用）。

### 6.9 AI 预审与批量审核（AI 只有建议权，没有执行权）

> **设计边界**：本组接口里只有 `batch-review` 会改变单据状态，且必须由财务本人带令牌点击提交。
> `ai-scores` 与 `ai-find` 是**纯只读**——它们不写飞书、不改状态，只产出等级与候选清单。

#### GET `/api/invoice/ai-scores?refresh=false`

权限：`invoice.review`（财务部 / 管理员 / 超管）。限流：读接口。

行为：拉取报销单列表后逐单产出 **1–4 级** 预审等级：
先由确定性规则重算（`InvoicePolicy.Evaluate`，免费），再按 `InvoiceAi:BatchSize`（默认 20）分片批量调用 AI 打分，
最后取 `max(规则等级, AI 等级)`——**AI 只能升级不能降级**。结果按单据内容指纹缓存到 `App_Data/invoice_ai.json`。

**增量规则**：状态为「已通过/已驳回」的单跳过；缓存命中且指纹未变跳过（不重复消耗 AI）；财务改动过关键字段则自动重算。
`refresh=true` 强制全部重算。

等级 ↔ 颜色：`1` 正常（绿）、`2` 注意（黄）、`3` 危险（橙）、`4` 严重（红）。

成功（200）`data`：`{ aiAvailable, aiError, quotaExhausted, note, stats:{total,skippedDecided,skippedCached,scored,ruleOnly,aiFailedBatches,injected}, rows:[…] }`，
每行含 `recordId / billNo / applicant / amount / status / hasScore / ruleLevel / aiLevel / finalLevel / levelLabel / ruleSummary / aiReason / suggest / flags / scoredAt`。

AI 未配置或调用失败时：`aiLevel=0`，仍返回规则等级（`aiReason` 说明原因），**不会降级为放行**，也不会写缓存（下次重试）。

#### POST `/api/invoice/ai-find`

请求字段：`requirement`（必填，2–200 字，如"帮我找出所有没问题的单子"）。

行为：把需求 + 在审单据摘要（**转义后**作为不可信数据）交给 AI，AI 只返回候选 `recordId` 列表。
**服务端二次校验**：编造的 recordId 丢弃、状态已变的丢弃、去重、截断到 `InvoiceAi:MaxFindItems`（默认 50）。
本接口**不执行任何操作**。

成功（200）`data`：`{ action: "approve|reject|ask", note, message, picked:[{recordId,reason,level,levelLabel,status,applicant,amount,rtype}], dropped:[{recordId,droppedReason}] }`。

失败码：401、403 `FORBIDDEN`、429 `RATE_LIMITED`、400 `AI_FIND_FAILED`（AI 不可用 / 返回不可解析 / 需求非法）。

#### POST `/api/invoice/batch-review`

请求字段：`recordIds`（必填，数组，去重后 ≤ `InvoiceAi:MaxBatchReview` 默认 50）、`action`（`approve` 或 `reject`）、`opinion`（可选补充说明）。

行为：**唯一写入口**。逐单调用单条审核逻辑 `ReviewAsync` —— 每单独立做状态流转校验、归属校验、审批意见长度校验，
并各自写飞书快照、版本记录与审计（因此每单都能单独回退）。审批人字段写**操作者本人姓名**，从不写 AI。
部分失败不影响其他单。

成功（200）`data`：`{ status, success, failed, message, items:[{recordId, ok, error, message}] }`。

失败码：401、403 `FORBIDDEN`、429 `RATE_LIMITED`、401 `BAD_SIGNATURE`（缺签名）、
400 `BAD_ACTION`（action 非 approve/reject）、400 `EMPTY_SELECTION`、400 `BATCH_REJECTED`（超上限或整体拒绝）。

### 6.10 已移除的旧接口

`/api/biz/*`（业务对接台：客户档案与需求单）已随旧业务域一并删除，接口、页面与数据播种都不再存在，相关审计动作名也不会再产生；历史实现可在 git 历史里查到。

---


## 7. 管理后台 `/api/admin/*`

**访问控制**：安全中间件强制"必须是管理员"，否则 `403 {"error":"无权访问该接口","code":"FORBIDDEN"}`，并记 **3 分**（几乎立刻触发 IP 封禁），审计 `authz.admin`。

所有 **POST** 接口（带令牌）都需要请求签名。除特别说明外，所有 POST 都受"后台写操作"限流（默认 60 次/分钟，由 `Limits:AdminWritePerMinutePerAdmin` 配置；超限 → 429 `RATE_LIMITED`）。所有 GET 受"读接口 180 次/分钟"限流（`Limits:ReadPerMinutePerUser`）。

### 7.1 总览与任务

| 方法 | 路径 | 请求 | 响应 `data` | 失败码 | 审计 |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/admin/overview` | 无 | 见下表 | —— | 无 |
| GET | `/api/admin/tasks?status=&userId=&limit=` | 查询串 | `ToAdminView` 数组 | —— | 可能写 `task.expire`（先调用超时作废） |
| GET | `/api/admin/tasks/{id}` | —— | `{ task, versions: [...] }` | 404 `NOT_FOUND` | 无 |
| POST | `/api/admin/tasks/{id}/approve` | `{ reason }` | `{ message, task }` | 400 `REASON_REQUIRED`、409 `CONFLICT` | `approval.approve`、`task.execute` |
| POST | `/api/admin/tasks/{id}/reject` | `{ reason }` | `{ message, task }` | 400 `REASON_REQUIRED`、409 `CONFLICT` | `approval.reject` |
| POST | `/api/admin/tasks/approve-by-token` | `{ taskId, token, reason? }` | `{ message, task }` | 400 `BAD_REQUEST`、403 `TOKEN_REJECTED` | `approval.token`（失败）、`approval.token-used`、`approval.approve` |
| POST | `/api/admin/tasks/expire` | 无 | `{ message, expired }` | —— | `task.expire`（每个超时任务一条） |

**GET `/api/admin/overview`** 的 `data`：

| 字段 | 说明 |
| --- | --- |
| `pendingCount` | 待审批数量（最多统计最近 300 条任务） |
| `executingCount` / `executedCount` / `rejectedCount` / `failedCount` | 各状态计数（`rejectedCount` 含全部拒绝类状态） |
| `userCount` | 账号数 |
| `versionCount` | 版本/快照数 |
| `auditCount` / `auditChainOk` / `auditChainMessage` | 审计条数与哈希链校验结果 |
| `outboxCount` | 出站通知数 |
| `aiConfigured` / `aiModel` | AI 配置 |
| `security` | 安全态势快照（同 `/api/admin/security` 的 `snapshot`） |
| `datasets` | `[{ name, label, fieldCount }]` |
| `newestPending` | 最近 5 条待审批任务的 `ToAdminView` |

**`ToAdminView`**（管理端任务视图，比用户视图多出管理员字段）：`id`、`userId`、`userName`、`userRole`、`userRoleLabel`、`intent`、`tool`、`action`、**`rawInput`（解密后的用户原始输入，仅管理员可见）**、`rawInputLength`、`riskLevel`/`riskLabel`、`rule`（`decision`/`requiresApproval`/`reasons`）、`review`（`verdict`/`available`/`risk`/`category`/`reason`）、`injectionFlags`、`status`/`statusLabel`/`statusReason`、`createdAt`、`decidedAt`、`decidedBy`、`decisionReason`、`executedAt`、`result`、`versionIds`、`rollbacked`、`approvalUsed`、`approvalExpiresAt`。

审批语义（`Services/TaskService.cs`）：

- `reason` 少于 2 个字符 → 400 `REASON_REQUIRED`（批准与打回都要求填写意见）。
- 状态机 CAS：只有把任务从 `PENDING_APPROVAL` 抢到 `EXECUTING`（或 `REJECTED_ADMIN`）的那一次写才生效；重复点击、已被处理或超时作废 → **409 `CONFLICT`**，`message` 说明当前状态。
- 批准后会真实执行工具（执行前已保存快照），并给提交人写一条站内通知（"已执行"/"执行失败"）。
- 邮件审批令牌：格式为 `base64url(过期Unix秒).base64url(HMAC-SHA256("approve|taskId|过期Unix秒"))`；与任务绑定、**一次性**（用后 `approvalUsed=true`）、有效期 `Security:ApprovalTokenMinutes`（默认 240 分钟）；库里只保存令牌的 SHA-256 摘要。改 `taskId`、改过期时间、重放旧链接都会被 `403 TOKEN_REJECTED` 拒绝。

### 7.2 账号管理

权限来源：`Core/AccountPolicy.cs` 的权限矩阵与人际约束（详见 [账号与权限设计.md](账号与权限设计.md)）。
本机的 `App_Data/users.json` 是账号权威；表中改动本机账号的写操作（创建、改角色、停用/启用、重置口令、重试镜像）都会**顺带触发飞书镜像**（best-effort，失败静默，见 [飞书镜像对接.md](飞书镜像对接.md)）。另外两个接口方向相反——从对面读：`GET /api/admin/users/feishu-rows` 只读预览，`POST /api/admin/users/sync-from-feishu` 手工补账号，它们都不改写飞书用户表。

| 方法 | 路径 | 需要权限 | 请求体 | 响应 `data` | 失败码 | 审计 |
| --- | --- | --- | --- | --- | --- | --- |
| GET | `/api/admin/users` | `account.list` | —— | 账号数组（见下） | 403 `FORBIDDEN` | 无 |
| GET | `/api/admin/users/feishu-rows` | `account.list` | —— | `{ configured, count, rows[] }` | 403 `FORBIDDEN` | 无 |
| POST | `/api/admin/users/sync-from-feishu` | `account.create` | `{ dryRun, reason }` | `{ dryRun, message, read, created, adopted, exists, skipped, needsPasswordReset, items[] }` | 403 `FORBIDDEN` / 429 `RATE_LIMITED` / 409 `SYNC_UNAVAILABLE` | `user.sync-feishu` |
| GET | `/api/admin/users/{id}/mirror-password` | `account.manage` | —— | `{ userName, demoPassword, hint }` | 403 / 404 | `user.mirror-password` |
| POST | `/api/admin/users` | `account.create`（目标为管理员时还需 `account.grant_admin`） | `{ userName, password, displayName, role, email }` | `{ message, user, mirrorDemoPassword, mirrorNote }` | 400 / 403 / 429 | `user.create` |
| POST | `/api/admin/users/{id}/role` | `account.manage`；涉及管理员/超管时需 `account.grant_admin` | `{ role, reason }` | `{ message }` | 400 `REASON_REQUIRED` / 403 / 404 | `user.role` |
| POST | `/api/admin/users/{id}/status` | `account.manage` | `{ disabled, reason }` | `{ message }` | 403 / 404 | `user.disable` / `user.enable` |
| POST | `/api/admin/users/{id}/reset-password` | `account.manage`（目标为超管时仅超管可做） | `{ newPassword, reason }` | `{ message, mirrorDemoPassword }` | 400 `RESET_FAILED` / 403 / 404 | `user.password` |
| POST | `/api/admin/users/{id}/delete` | `account.delete`（仅超管） | 无（可传 `{}`） | `{ message }` | 403 / 404 | `user.delete` |
| POST | `/api/admin/users/{id}/mirror-retry` | `account.manage` | 无（可传 `{}`） | `{ ok, skipped, error, message }` | 403 / 429 | `feishu.mirror.sync` / `feishu.mirror.failed` |

**GET `/api/admin/users`** 每条记录字段：`id`、`userName`、`displayName`、`employeeId`、`role`、`roleLabel`、`emailMasked`、`disabled`、`locked`、`lockedUntil`、`failedLoginCount`、`createdAt`、`createdBy`、`lastLoginAt`、`lastLoginIp`、`aiCallsToday`、`origin`（账号来源：`local` / `feishu-sync` / `bootstrap`，见 [账号与权限设计.md](账号与权限设计.md) 1.3）、`passwordScheme`（`none` = 尚未设置口令 / `pbkdf2-sha256+pepper` / `pbkdf2-sha256` / `partner-pbkdf2-sha256`）、`passwordIterations`、`mirrorState`、`mirrorError`、`mirrorAt`、`mirrorRoleLabel`（按 `employeeId` 升序）。**不返回任何口令散列字段**（`passwordHash` / `passwordSalt` / `mirrorHashHex` / `mirrorSaltHex` / `mirrorPasswordCipher` 都不在响应里）。

**POST `/api/admin/users`** 的 `user` 字段：`id`、`userName`、`displayName`、`employeeId`、`role`、`roleLabel`、`mirrorState`、`mirrorError`。
`mirrorNote` 会把镜像结果翻译成三种说明：已同步 / 未配置飞书已跳过 / 镜像失败已静默处理（可在后台重试）。

**GET `/api/admin/users/feishu-rows`** —— 只读预览对面的用户表。同步是写操作，先看清对面有什么、与本机是否重合，再决定要不要落库。

| 项 | 说明 |
| --- | --- |
| 鉴权 | 需登录，且操作者拥有 `account.list`（`/api/admin/*` 本身还要求管理员或超级管理员） |
| 签名 | GET，不需要请求签名 |
| 限流 | 读接口 180 次/分钟 |
| 审计 | 不写审计 |

`data` 字段：

| 字段 | 说明 |
| --- | --- |
| `configured` | 是否已配置飞书（`Feishu:AppId` / `AppSecret` / `AppToken` / `UserTableId` 四项齐备） |
| `count` | 对面用户表的行数（未配置飞书时为 0） |
| `rows` | 行数组，见下 |
| `message` | 仅在 `configured = false` 时出现："未配置飞书，无法读取对面的用户表" |

`rows[]` 字段：

| 字段 | 说明 |
| --- | --- |
| `recordId` | 对面这一行的 `record_id`（认领与更新镜像行时用它定位） |
| `userName` / `displayName` / `employeeId` | 对面的「用户名」/「姓名」/「工号」列 |
| `roleLabel` | 对面「角色」列的中文标签（`管理员` / `财务` / `员工`） |
| `statusLabel` | 对面「账号状态」列（`启用` / `停用`） |
| `hasPassword` | 对面这一行是否带有可用的口令材料（十六进制哈希 ≥ 64 位且盐 ≥ 32 位） |
| `existsLocally` | 本机账号库里是否已有同名账号（用户名按大小写不敏感比较） |
| `createdAt` / `lastLoginAt` | 对面的创建时间与最后登录时间（对面没填时为 `null`） |

> 这个接口**不返回口令哈希**：管理端预览不需要它，少一处泄露面。

**POST `/api/admin/users/sync-from-feishu`** —— 把对面有、本机没有的账号补进本机账号库（管理端手工触发，不会自动执行）。

| 项 | 说明 |
| --- | --- |
| 鉴权 | 需登录，且操作者拥有 `account.create`（`/api/admin/*` 本身还要求管理员或超级管理员） |
| 签名 | POST，需要请求签名（见 2.3） |
| 限流 | 后台写操作限流（`Limits:AdminWritePerMinutePerAdmin`，默认 60 次/分钟） |
| 审计 | `user.sync-feishu`：预览 `PREVIEW`、执行 `SUCCESS`、读到 0 行 `FAIL`、无权限 `DENIED` |

请求字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `dryRun` | 否 | `true` 时只预览不落盘（默认 `false`） |
| `reason` | 否 | 操作理由（截断 200 字符），会追加进响应的 `message`，便于查账 |

成功（200）`data`：

| 字段 | 说明 |
| --- | --- |
| `dryRun` | 本次是否为预览 |
| `message` | 结果摘要（预览时以"预览结果（未写入）："开头；填了 `reason` 时追加"；理由：…"） |
| `read` | 从对面读到的行数 |
| `created` | 新建的账号数 |
| `adopted` | 认领的行数（本机已有该账号，但此前缺 `record_id`） |
| `exists` | 本机已有、无需处理的账号数 |
| `skipped` | 跳过的行数（用户名不可用、对面用户名重复、操作者无权创建该角色等） |
| `needsPasswordReset` | 其中需要管理员重置口令后才能登录的账号数 |
| `items` | 逐行处置明细 |

`items[]` 字段：`userName`、`displayName`、`employeeId`、`roleLabel`、`action`、`detail`、`needsPasswordReset`。

`action` 取值：

| action | 含义 |
| --- | --- |
| `created` | 本机没有这个账号，已新建（工号沿用对面的或由本机新发；口令材料见下） |
| `adopted` | 本机已有这个账号但缺 `record_id`，已认领对面的行——之后本机改动会更新到那一行，而不是再建一行重复账号 |
| `exists` | 本机已有这个账号且无需处理：**本机是权威，口令、角色、启停一律不动** |
| `skipped` | 这一行没有处理，原因写在 `detail` |

失败：

| 状态码 | code | 场景 |
| --- | --- | --- |
| 401 | `UNAUTHENTICATED` | 未登录 |
| 403 | `FORBIDDEN` | 无 `account.create` 权限（同时写一条 `user.sync-feishu` 的 `DENIED` 审计） |
| 429 | `RATE_LIMITED` | 超过后台写操作限流 |
| 409 | `SYNC_UNAVAILABLE` | 未配置飞书（四项凭证缺一），或从对面读到 0 行（表为空、凭证或表号不对；读取失败按静默处理，具体原因见服务日志） |

语义要点：

- **只补不改**：本机已有的账号不会被覆盖，同步不会改写它们的口令散列、角色与启停状态；否则"在对面表里改一行"就等于改本系统的权限。
- **外部表导不出超管**：对面的「管理员」只映射成 `admin`，其余（含「财务」以外的任何值）映射成 `employee`；超级管理员只能由配置播种或直接编辑 `users.json` 得到。
- **权限矩阵优先**：操作者无权创建的角色（例如普通管理员同步出管理员账号）会被逐行跳过并写明原因，因此同步不会变成绕过 `AccountPolicy` 的提权通道。
- **口令材料**：对面存的是十六进制编码的 PBKDF2-HMAC-SHA256（迭代次数取 `Feishu:MirrorPbkdf2Iterations`，默认 100000），导入时转成 base64 存进 `users.json`，按方案 `partner-pbkdf2-sha256` 记录，并把该账号的迭代次数记成同一个值（默认 100000，登录时按账号自己的这个值校验）——**对方用他在对面前端用的原口令就能登录本系统**。该账号在本系统首次登录成功后，口令存储会自动升级为当前方案（`pbkdf2-sha256+pepper`，迭代次数取 `Security:PasswordIterations`），原口令继续可用。对面没有可用哈希的账号照样建，但 `needsPasswordReset = true`（`passwordScheme` 显示为 `none`），管理员重置口令前登不进来。
- **工号**：对面工号格式合法且不与本机冲突时沿用（报销单靠工号认人，两边必须一致），冲突或没填则由本机新发（`E` + 四位自增）。
- `dryRun = true` 用当前账号库的只读快照跑同一套判断，因此不产生任何写入。

示例（预览）：

```json
{ "dryRun": true, "reason": "接管协作方已有账号，先预览" }
```

响应（截断）：

```json
{
  "ok": true,
  "data": {
    "dryRun": true,
    "message": "预览结果（未写入）：飞书用户表读到 2 个账号：新建 1 个，认领 1 个，已存在 0 个；理由：接管协作方已有账号，先预览",
    "read": 2,
    "created": 1,
    "adopted": 1,
    "exists": 0,
    "skipped": 0,
    "needsPasswordReset": 0,
    "items": [
      { "userName": "chenqi", "displayName": "陈七", "employeeId": "E0011", "roleLabel": "员工", "action": "created", "detail": "已导入（飞书角色「员工」→ 员工；工号 E0011；沿用对面口令哈希，可用原口令登录）", "needsPasswordReset": false },
      { "userName": "sunba", "displayName": "孙八", "employeeId": "E0012", "roleLabel": "员工", "action": "adopted", "detail": "本机已有该账号，已认领飞书第 tblXXXXXXX 行（之后本机改动会更新到那一行）", "needsPasswordReset": false }
    ]
  }
}
```

**约束（代码强制，前端隐藏按钮只是 UX）**：

| 约束 | 触发条件与文案（`AccountPolicy`） |
| --- | --- |
| 目标角色必须是四角色之一 | `目标角色非法` |
| 超管账号不能通过接口创建 | `超级管理员账号只能由系统初始化，不能在界面上创建` |
| 非超管不能创建管理员账号 | `只有超级管理员可以创建管理员账号` |
| 不能改自己的角色 | `不能修改自己的角色（避免自我提权）` |
| 授予/撤销管理员（或超管）仅超管可做 | `只有超级管理员可以授予或撤销管理员/超级管理员` |
| 必须保留最后一个可用超管 | `系统必须保留至少一个超级管理员`（改角色、停用、删除三处都会拦） |
| 不能停用/删除自己 | `不能停用自己的账号` / `不能删除自己的账号` |
| 管理员不能重置超管口令 | `只有超级管理员可以重置超级管理员的口令` |
| 删除账号仅超管 | `只有超级管理员可以删除账号` |

其它行为：

- 新建账号的口令强度、用户名格式与注册完全一致（同一个 `UserService.CreateAsync`）；工号由服务端分配（`E` + 4 位），**不可自填**。
- 改角色、停用/启用、重置口令都会 `TokenEpoch++`，目标账号的旧令牌立即失效。
- 改角色必须填 `reason`（少于 2 个字符 → 400 `REASON_REQUIRED`）；停用/启用与重置口令的请求模型里也有 `reason` 字段，但当前实现**没有读取也没有校验**它（以代码为准）。
- 删除账号只删除本机权威记录，**不会**删除飞书用户表里的镜像行（响应 `message` 里明确提示"飞书镜像行需人工清理"），也不会删除该账号的工作区目录。
- 被策略拒绝时，除接口返回 403 外，还会给来源 IP 记分（创建账号与改角色记 2 分，`weight: 2, waf: true`），频繁尝试会触发自动封禁。
- `POST /api/admin/users`、`/role`、`/status`、`/reset-password`、`/delete`、`/mirror-retry`、`/users/sync-from-feishu` 都走"后台写操作"限流（`Limits:AdminWritePerMinutePerAdmin`，默认 60 次/分钟）。

> 安全中间件对 `/api/admin/*` 的门禁判定是 `Roles.IsAdminSide(role)`，即 **`superadmin` 与 `admin` 都可以访问**本接口；
> 具体"能不能做这件事"再由 `AccountPolicy` 逐条判定（例如授予管理员只有超管可以）。财务与员工访问 `/api/admin/*` 会被 403 拦下并记 3 分。

### 7.3 审计

| 方法 | 路径 | 查询参数 | 响应 `data` | 失败码 | 审计 |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/admin/audit` | `action`（子串匹配，≤40）、`actor`（用户名子串，≤64）、`limit`（默认 200，钳制 1~500）、`offset`（默认 0） | 审计数组（见下） | —— | 无 |
| GET | `/api/admin/audit/verify` | 无 | `{ ok, checkedCount, brokenAt, message }` | —— | 无 |

`/api/admin/audit` 每条：`seq`、`time`、`actorName`、`actorRole`、`ip`、`action`、`target`、`outcome`、`detail`、`hash`（前 12 位）、`prevHash`（前 12 位）。按 `seq` 倒序。

`/api/admin/audit/verify` 重新计算整条哈希链：`brokenAt` 为 0 表示完整，否则给出第一个断链的序号，`message` 说明是"序号断裂"、"前序哈希不匹配"还是"内容哈希不匹配"。

### 7.4 版本与回退

| 方法 | 路径 | 请求 | 响应 `data` | 失败码 | 审计 |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/admin/versions?userId=&limit=` | 查询串（`limit` 默认 100） | 版本数组 | —— | 无 |
| POST | `/api/admin/versions/{id}/rollback` | `{ reason }` | `{ message, taskId, newVersionId, data, note }` | 400 `REASON_REQUIRED` / `ROLLBACK_FAILED`、404 `NOT_FOUND`、409 `ALREADY_REVERTED` | `admin.rollback` |

版本记录字段：`id`、`taskId`、`userId`、`userName`、`kind`、`operation`、`target`、`previousExisted`、`previousHash`、`previousSize`、`newSize`、`addedLines`、`removedLines`、`diff`、`createdAt`、`reverted`、`revertedAt`、`revertedBy`、`compensateAction`。

回退语义：

- 同一个版本只能回退一次（`reverted` 为真时 → 409 `ALREADY_REVERTED`）。
- 回退本身也会建一条任务（`tool = admin_rollback`，`status` 从 `EXECUTING` 到 `EXECUTED`/`FAILED`），因此**回退操作自身也能再被回退**（撤销误回退）。
- 成功后会标记原任务 `rollbacked = true` 并记录 `rolledBackAt` / `rolledBackBy`。

### 7.5 数据集与出站箱

| 方法 | 路径 | 请求 | 响应 `data` | 失败码 | 审计 |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/admin/datasets/{name}?limit=` | 路径参数 + `limit`（默认 50） | `{ dataset, label, query, totalRows, fields[], rows[] }` | 404 `NOT_FOUND`（未知数据集）、400 `QUERY_FAILED` | 无 |
| GET | `/api/admin/outbox?limit=` | `limit` 默认 100 | 通知数组 | —— | 无 |
| POST | `/api/admin/outbox/{id}/recall` | 无（可传 `{}`） | `{ message }` | 404 `NOT_FOUND` | **无**（以代码为准，该分支未写审计） |

- `{name}` 取值（大小写不敏感）：`customers`（甲方客户档案）、`requirements`（甲方需求单）、`orders`（订单记录）、`contracts`（合同台账）；字段定义与可写性见 `Tools/DatasetStore.cs` 的 `DatasetCatalog`（`fields[].writable` 为 `false` 的字段由服务端维护，不接受客户端赋值）。
- 出站箱每条：`id`、`createdAt`、`to`（**脱敏**）、`subject`、`body`、`status`、`taskId`、`ownerUserName`、`approvalLink`、`sentAt`、`error`、`compensatedAt`。
- 撤回是**补偿事务**：只把通知标记为已撤回（`RECALLED`），不影响已执行的任务。

### 7.6 安全态势

| 方法 | 路径 | 请求 | 响应 `data` | 失败码 | 审计 |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/admin/security` | 无 | `{ snapshot, limits, defenses }` | —— | 无 |
| POST | `/api/admin/security/unban` | `{ ip }` | `{ message }` | 400 `BAD_REQUEST`、404 `NOT_FOUND` | `admin.unban` |

- `snapshot`（`SecurityGuard.Snapshot()`）：`wafBlocks`、`honeypotHits`、`loginFailures`、`rateLimited`、`replayRejected`、`signatureRejected`、`bannedRequests`、`activeBans`、`trackedIps`、`offsets`（按计分排序的前 50 个 IP，含 `strikes`/`honeypotHits`/`wafHits`/`loginFails`/`banned`/`bannedUntil`/`lastReason`/`lastSeen`/`events`）。
- `limits`：`loginPerFiveMinutesPerIp`、`loginPerFifteenMinutesPerAccount`、`accountLockMinutes`、`agentChatPerFiveMinutesPerUser`、`wafStrikesBeforeBan`、`honeypotStrikesBeforeBan`、`banMinutes`。
- `defenses`：当前启用的防线清单（文字数组）。
- `POST /api/admin/security/unban` 未经过"后台写操作 60 次/分钟"限流（代码中未调用该限流函数），只有既有的签名与管理员校验。

### 7.7 管理端 AI 助手

管理后台的 AI 助手**没有独立接口**：它复用 `POST /api/agent/chat`（`admin` 角色），提示词边界限定为**只读分析**（查待审批任务、任务上下文、审计检索、使用情况汇总等），**没有任何审批/执行/回滚接口**，因此不存在"AI 自我批准"的路径。

---

## 8. 配置项与接口的对应关系

| 配置项（`appsettings.json` / 环境变量） | 影响 |
| --- | --- |
| `Server:ListenUrl` / `AISERVER_LISTEN_URL` | 监听地址（默认 `http://127.0.0.1:8899`） |
| `Security:DataRoot` / `AISERVER_DATA_ROOT` | 数据目录（默认可执行文件同级 `App_Data/`） |
| `Security:TokenMinutes` | 令牌有效期（默认 120 分钟） |
| `Security:ClockSkewSeconds` | 签名时间戳容差（默认 300 秒） |
| `Security:NonceWindowSeconds` | nonce 防重放窗口（默认 300 秒） |
| `Security:ApprovalTokenMinutes` | 审批有效期 = 邮件令牌有效期（默认 240 分钟） |
| `Security:AllowSelfRegistration` | 是否开放 `POST /api/auth/register`。**当前已不参与门禁**：注册改为"需登录 + `account.create` 权限"，该配置只在 `/api/public/health` 的 `selfRegistration` 字段里展示（见第 9 节） |
| `Security:SelfRegistrationPerHourPerIp` | 注册限流（默认 3 次/小时/IP）。**当前已不再使用**：注册限流改为按操作者账号的硬编码 20 次/小时 |
| `Security:MaxRequestBodyBytes` | 请求体上限（默认 262144） |
| `Security:MaxUserInputChars` | 对话内容上限（默认 2000） |
| `Security:AutoApproveSandboxWrites` | 私人工作区低风险写是否免人工审批（默认 `true`；置 `false` 则全部转人工） |
| `Security:TrustedOrigins` / `AISERVER_TRUSTED_ORIGINS` | 反向代理终止 TLS 场景的合法来源白名单（数组；环境变量用分号/逗号/空格分隔），见 2.4 |
| `Limits:LoginPerFiveMinutesPerIp` / `LoginPerFifteenMinutesPerAccount` / `AccountLockMinutes` | 登录限流与账号锁定阈值 |
| `Limits:AgentChatPerFiveMinutesPerUser` / `AgentChatPerFiveMinutesPerIp` | 对话接口限流 |
| `Limits:ReadPerMinutePerUser` | 所有 `GET /api/*` 的读接口限流 |
| `Limits:AdminWritePerMinutePerAdmin` | 后台写操作限流（默认 60 次/分钟）；报销单的审核、修改、删除、回退共用这一档 |
| `Limits:BizActionPerFiveMinutesPerUser` / `BizActionPerFiveMinutesPerIp` | 旧业务对接台留下的配置项，当前无调用方（对接台已移除），仅为兼容旧配置文件保留 |
| `Limits:WafStrikesBeforeBan` / `HoneypotStrikesBeforeBan` / `BanMinutes` | 自动封禁的计分阈值与封禁时长 |
| `Security:PasswordIterations` / `AISERVER_PASSWORD_ITERATIONS` | 口令慢哈希的迭代次数（默认 1200000）；调大更抗爆破，代价是每次登录更慢 |
| `Ai:ApiKey` / `AISERVER_AI_KEY` | AI 是否可用（未配置则降级为本地解析）；取值顺序为 `AISERVER_AI_KEY` → `config/secrets.local.json` 的 `Ai.ApiKey` → `Ai:ApiKey` |
| `Ai:BaseUrl` / `Ai:AllowedHosts` | 出站模型地址与主机白名单 |
| `Ai:MaxAgentSteps` / `Ai:AgentTimeoutSeconds` | 多步循环步数上限（默认 5）与墙钟上限（默认 60 秒） |
| `Ai:DailyCallsPerUser` | 每账号每日 AI 调用额度（默认 200）；用尽后该次对话降级为本地解析并在 `notes` 中说明 |
| `Mail:Enabled` / `Mail:PublicBaseUrl` / `Mail:ApproverAddress` | 是否真实发信、审批链接基地址、审批人地址 |
| `Feishu:AppId` / `AppSecret` / `AppToken` / `TableId` / `UserTableId` | 飞书凭证与表 id。解析优先级：环境变量 `AISERVER_FEISHU_*` → `config/secrets.local.json` → `appsettings.json`；`UserTableId` 是账号镜像的写入目标。`AppToken` / `TableId` / `UserTableId` 允许直接粘贴飞书网页地址，启动时自动取出 base 与 table 标识（`&view=…` 之类参数被截断）并在启动日志给出提示。见 [飞书镜像对接.md](飞书镜像对接.md) |
| `Feishu:TableUrl` / `Feishu:UserTableUrl` | 报销单原表与用户表在飞书网页端的地址，`GET /api/invoice/meta` 的 `tableUrl` / `userTableUrl` 由它给出（供前端做「在飞书中打开原表」的跳转）。未显式配置时，若 `AppToken` 与对应表 id 都在，按 `https://my.feishu.cn/base/{appToken}?table={tableId}` 自动拼一条 |
| `Feishu:AllowedHosts` / `BaseUrl` | 出站主机白名单（默认 `open.feishu.cn`，回环地址默认放行）与 API 基地址（可指向本地假飞书做离线验收） |
| `Feishu:TimeoutMs` / `MaxRetries` | 镜像单次超时（默认 3000ms）与重试次数（默认 1），只影响镜像，不影响业务结果 |
| `Feishu:MirrorPasswordMode` / `DemoPasswordLength` / `MirrorPbkdf2Iterations` | 镜像口令模式（`demo` 默认 / `real` / `none`）、演示口令长度（默认 12）、镜像散列迭代次数（默认 100000，需与协作方一致） |
| `Feishu:DetectForeignAccounts` | 启动时是否对比飞书用户表：开启（默认）且飞书已配置时，把"飞书有、本机没有"的账号写一条审计 `feishu.mirror.foreign-account`（`outcome = IGNORED`）并在控制台提示；它们不会被建号，也无法登录本系统 |
| `Bootstrap:Admin:*` / `AISERVER_BOOTSTRAP_*` | 初始超级管理员引导。解析优先级：环境变量 `AISERVER_BOOTSTRAP_*` → `config/secrets.local.json` 的 `BootstrapAdmin` 段 → `appsettings.json` 的 `Bootstrap:Admin:*`。只在账号库里不存在该用户名时创建一次，不写飞书用户表。见 [账号与权限设计.md](账号与权限设计.md) 第 9 节 |

---

## 9. 需要留意的点与以代码为准的说明

第 1 ~ 4 条是已核对代码后确认的现状（含与历史版本/配置名不一致的地方）；第 5 条起是本文没有从代码中得到确定结论的内容，使用前请以代码为准：

1. 曾经的 `AiAnalyzeRequest` 请求模型**已从仓库移除**，代码中不存在此类请求体，也不存在对应的 HTTP 接口（如 AI 分析入口）；管理端的 AI 分析走 `POST /api/agent/chat`（见 7.7）。
2. 任务打回只有一条路径：`TaskService.DecideAsync`（审计 `approval.reject`）。写入 `task.reject` 的 `TaskService.RejectAsync` 已删除，因此 **`task.reject` 这个动作名在当前版本不会再产生**（1.6 中保留该行仅为对照历史数据）。
3. 后台写操作限流读自 `Limits:AdminWritePerMinutePerAdmin`（默认 60 次/分钟），报销单的审核/修改/删除/回退共用这一档；报销单提交有自己的账号维度限流（30 次/5 分钟，硬编码）。`Limits:BizActionPerFiveMinutesPerUser` / `BizActionPerFiveMinutesPerIp` 是旧业务对接台留下的配置项，当前**没有调用方**（对接台已移除）。注意这些阈值都**没有**出现在 `/api/admin/security` 的 `limits` 返回里（该接口只回登录/对话/封禁相关项，见 7.6）。
4. 出站通知只有一个 `App_Data/outbox.json` 文件，**没有** `outbox/` 目录（`StoreNames.OutboxDir` 常量已删除）。
5. `/api/admin/outbox/{id}/recall` 分支没有写审计（`recall` 只做补偿事务），若需要留痕请以代码为准或后续补齐。
6. 请求签名中「路径」段的解码形态：代码取的是 ASP.NET Core 解析后的 `Request.Path.Value` 与原始 `Request.QueryString.Value` 拼接。常规路径与常规编码下与服务端一致；涉及特殊编码（如路径内的 `%2F`、重复编码的查询参数）时，建议以实际服务端行为（或 `Security/SecurityMiddleware.cs` 的实现）为准。
7. `AgentOutcome.source` / `reviewVerdict` 的取值集合由 `Ai/AgentOrchestrator.cs` 内的字面量决定，本文列出的是当前代码中出现的取值，新增分支时以代码为准。
8. 会话上下文（`ChatSessionStore`）为**进程内存**存储，多实例部署时不共享；限流与封禁计数同样在内存中，重启即清零（账号锁定状态除外，它持久化在 `users.json`）。
9. **`Security:AllowSelfRegistration` / `Security:SelfRegistrationPerHourPerIp` 是展示用的遗留配置**（刻意收敛，不是漏接线）：注册已改为"需要登录 + `account.create` 权限"，即注册属于管理员行为；这两个配置项只被 `GET /api/public/health` 读取展示，不再参与门禁。注册接口自身的限流是硬编码的 **20 次/小时/操作者账号**（限流键 `register:user:<userId>`）。
10. **停用/启用与重置口令的请求模型里带 `reason` 字段，但当前实现不读取也不校验它**；只有改角色接口会校验（少于 2 个字符 → 400 `REASON_REQUIRED`）。
11. **删除账号不清理工作区目录，也不删除飞书用户表里的镜像行**（响应 `message` 已提示"飞书镜像行需人工清理"）；工号在账号删除后可能被后续新账号复用（`users.json` 没有唯一索引，工号取"当前最大号 + 1"）。
12. **未配置飞书时 `mirrorState` 的两处口径不一致**：创建账号的接口响应里是 `skipped`（"未配置飞书，已跳过镜像"），但落盘的 `users.json` 里仍是初始值 `pending`——因为镜像在禁用时提前返回、没有落盘。见 [飞书镜像对接.md](飞书镜像对接.md) 第 6 节。
13. **拒绝类操作会计入攻击计分**：越权创建账号、越权改角色各给来源 IP 记 2 分，非管理员访问 `/api/admin/*` 记 3 分，默认 `Limits:WafStrikesBeforeBan` 为 8 分。
    用脚本连续跑越权用例时请临时放宽该阈值（如 `Limits__WafStrikesBeforeBan=40`），否则本机会被自动封禁、后续用例全返回 403。
14. **登录与账号维度的限流默认值**：登录 6 次/5 分钟/IP（`Limits:LoginPerFiveMinutesPerIp`）、账号连续失败 5 次锁定 15 分钟（`Limits:LoginPerFifteenMinutesPerAccount` / `Limits:AccountLockMinutes`）。
    批量验收登录流程时需放宽这两项，否则会撞 429 或把演示账号锁住（锁定状态持久化在 `users.json`，重启不会解除）。
15. **报销单详情是按 `recordId` 在全量列表里找**：`InvoiceService.GetAsync` 先拉最多 500 条记录再匹配，记录量变大后应改用 `FeishuApi.GetRecordAsync` 单条查询（该方法已存在但报销单路径未使用）。
16. **报销单归属优先按工号匹配**（`InvoicePolicy.BelongsTo` 先比对登录人工号与记录「工号」列，唯一且不受改名影响）；历史数据缺工号时才回退到申请人姓名匹配。
17. **报销单写操作不经过人工审批链路**：提交、审核、修改、删除、回退都是即时生效，但每次都会落版本快照与审计（可在管理后台按日志回退）。审批链路仍然服务于其它工具。
18. **字段名探测结果缓存 10 分钟**（`FeishuApi.FieldNamesAsync`）：在飞书表里改了列名后，最迟十分钟后生效，必要时重启服务。
19. **`GET /api/invoice/detail` 与 `/api/invoice/meta` 不写审计**，只有 `GET /api/invoice/list` 会写 `invoice.list`。
20. **`GET /api/invoice/meta` 的 `feishuConfigured` 比报销单写入的要求更严**：它要求 `Feishu:AppId` / `AppSecret` / `AppToken` / `UserTableId` 四项齐备（账号镜像链路可用），而报销单写入只要求前三项加 `Feishu:TableId`，因此该字段为 `false` 时报销单接口仍可能可用。`tableUrl` / `userTableUrl` 只是给前端跳转用的网页地址，服务本身不会访问它。
21. **初始超管的 `mirrorState` 会落盘为 `skipped`**（`mirrorError` 写明"初始超级管理员不镜像"），这与第 12 条"未配置飞书时接口响应 `skipped`、落盘仍是 `pending`"是两种不同情形，排查账号列表时不要混用。
22. **账号同步不覆盖、也不删除**：`POST /api/admin/users/sync-from-feishu` 只新建本机缺失的账号与认领 `record_id`，不改动本机已有账号（包括对面标了「停用」而本机仍在用的账号，也会保持本机状态），也不会清理对面多出来的行。
23. **飞书用户表会翻页读到最后一页**：`feishu-rows` 预览、账号同步与启动期的 `feishu.mirror.foreign-account` 比对都走 `FeishuApi.ListRecordsAsync`（每次 `page_size=200`，按 `page_token` 翻页，最多 20 页）。对面表行数超过单页时不会被悄悄截断；超过一万行会停在第二十页，此时同步结果不完整，需要分批处理。
