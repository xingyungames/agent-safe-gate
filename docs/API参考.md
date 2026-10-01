# API 参考（AiApprovalServer）

## 0. 适用范围 / 版本

| 项 | 说明 |
| --- | --- |
| 对应代码 | 仓库 `AiApprovalServer/`（ASP.NET Core Minimal API，`net8.0`），测试脚本在 `tests/` |
| 版本状态 | 未发布正式版本，本文对应仓库当前状态（工作副本快照），接口以代码为准 |
| 数据库 | **纯 JSON 文件**（`App_Data/` 下的 `users.json` / `tasks.json` / `versions.json` / `outbox.json` / `audit.jsonl` / `datasets/*.json` / `workspaces/`），没有外部数据库 |
| 第三方依赖 | **无任何第三方 NuGet 包**：JWT（HS256）、PBKDF2-HMAC-SHA256 口令散列、AES-256-GCM 字段加密、HMAC 请求签名、审计哈希链全部为自实现 |
| 默认监听 | `http://127.0.0.1:8899`（环境变量 `AISERVER_LISTEN_URL` 或 `Server:ListenUrl` 可改） |
| 页面与接口同源 | 页面（`/`、`/biz`、`/admin`）与 API 由同一进程提供，浏览器端用原生 `fetch` + Web Crypto 计算请求签名 |
| 演示账号 | 用户名 `zhangsan`（用户）/ `lisi`（员工）/ `admin`（管理员），口令见 README 演示口令（本文不记录口令） |

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
9. 鉴权：`/api/*` 除三个公开路径外必须有身份；`/api/admin/*` 必须是管理员
10. GET `/api/*` 读接口限流（在路由之前）
11. 业务端点（`AuthApi` / `AgentApi` / `BizApi` / `AdminApi`）

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
| 400 | `WEAK_PASSWORD` / `REGISTER_FAILED` / `CREATE_FAILED` / `RESET_FAILED` / `BAD_ROLE` | 账号相关校验失败 |
| 400 | `BAD_TOOL` / `EXECUTE_FAILED` | 只读直通接口：工具不可用 / 执行失败 |
| 403 | `RULE_DENIED` | 只读直通接口被规则引擎拒绝 |
| 400 | `ROLLBACK_FAILED` / `QUERY_FAILED` | 回退失败 / 数据集查询失败 |
| 400 | `SELF_DEMOTE` / `SELF_DISABLE` | 管理员试图降级/禁用自己 |
| 401 | `UNAUTHENTICATED` / `BAD_TOKEN` / `TOKEN_STALE` / `BAD_SIGNATURE` / `AUTH_FAILED` | 未登录 / 令牌非法 / 令牌失效 / 签名失败 / 口令错误 |
| 403 | `FORBIDDEN` / `IP_BANNED` / `BAD_ORIGIN` / `REGISTER_DISABLED` / `TOKEN_REJECTED` | 越权 / 封禁 / 跨站来源（未命中 `Security:TrustedOrigins` 且与本站不同源，见 2.4）/ 关闭自助注册 / 审批令牌被拒 |
| 404 | `NOT_FOUND` | 任务/版本/通知/数据集/路由不存在 |
| 409 | `CONFLICT` / `ALREADY_REVERTED` | 任务状态不允许再次审批 / 版本已回退过 |
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
| `system.error` | 安全中间件 | 未捕获异常 |
| `auth.login` | `AuthApi` / `UserService` | 登录成功 / 失败 |
| `auth.logout` | `AuthApi` | 主动登出 |
| `auth.register` | `AuthApi` | 自助注册（成功 / 失败） |
| `auth.token` | 安全中间件 | 令牌校验失败（格式/签名/过期/世代不匹配） |
| `auth.lockout` | `AuthApi` | 账号因连续失败被锁定 |
| `auth.rate-limit` | `AuthApi` | 登录频率超限 |
| `authz.admin` | 安全中间件 | 非管理员访问 `/api/admin/*` |
| `user.create` | `UserService` | 管理员创建账号 |
| `user.role` | `UserService` | 修改角色 |
| `user.disable` / `user.enable` | `UserService` | 禁用 / 启用账号 |
| `user.password` | `UserService` | 修改或重置口令（成功 / 失败） |
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
| `biz.action` | `BizApi` | 业务动作提交（`SUBMIT`） |
| `biz.read` | `BizApi` | 员工/管理员批量读取客户与需求单 |
| `biz.rate-limit` | `BizApi` | 业务动作提交过频 |
| `biz.link-account` | `BusinessSeed` | 演示账号与客户档案绑定 |
| `admin.rollback` | `AdminApi` | 版本回退 |
| `admin.unban` | `AdminApi` | 管理员手动解封 IP |
| `admin.activity-summary` / `admin.task-list` | `Harness` | 管理端 AI 助手的聚合查询 |
| `security.blocked` | 安全中间件 | 被封禁 IP 的请求被拒 |
| `waf.block` | 安全中间件 | WAF 命中 |
| `csrf.block` | 安全中间件 | 跨站来源被拒 |
| `honeypot.probe` / `honeypot.credential` | 安全中间件 | 蜜罐探测 / 向诱饵后台提交凭据 |
| `recon.robots` | 安全中间件 | 读取 `robots.txt` |
| `request.signature` | 安全中间件 | 请求签名校验失败 |

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
| `role` | `user` / `staff` / `admin` |
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
  -d '{"userName":"zhangsan","password":"<见 README 演示口令>"}'

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

固定窗口计数（内存态，进程重启清零；账号锁定则持久化在 `users.json`）。

| 场景 | 计数键 | 默认阈值 | 配置项 | 超限响应 |
| --- | --- | --- | --- | --- |
| 登录 | `login:ip:{ip}` | 6 次 / 5 分钟 | `Limits:LoginPerFiveMinutesPerIp` | 429 `RATE_LIMITED` + 审计 `auth.rate-limit` |
| 注册 | `register:ip:{ip}` | 3 次 / 1 小时 | `Security:SelfRegistrationPerHourPerIp` | 429 `RATE_LIMITED` |
| 修改口令 | `password:{userId}` | 5 次 / 15 分钟 | 硬编码 | 429 `RATE_LIMITED` |
| 对话 `/api/agent/chat` | `chat:user:{id}` 与 `chat:ip:{ip}` | 20 次 / 5 分钟、60 次 / 5 分钟 | `Limits:AgentChatPerFiveMinutesPerUser` / `...PerIp` | 429 + 审计 `agent.rate-limit` |
| 业务动作 `/api/biz/action` | `biz:user:{id}` 与 `biz:ip:{ip}` | 30 次 / 5 分钟、90 次 / 5 分钟 | `Limits:BizActionPerFiveMinutesPerUser` / `Limits:BizActionPerFiveMinutesPerIp` | 429 + 审计 `biz.rate-limit` |
| 后台写操作 | `adminwrite:{userId}` | 60 次 / 1 分钟 | `Limits:AdminWritePerMinutePerAdmin` | 429 `RATE_LIMITED` |
| 所有 `GET /api/*` | `read:user:{id}` 或 `read:ip:{ip}` | 180 次 / 1 分钟 | `Limits:ReadPerMinutePerUser` | 429（`ok` 结构） |

账号级防护（`AuthApi` + `UserService`）：

- 连续口令错误达到 `Limits:LoginPerFifteenMinutesPerAccount`（默认 5 次）→ 账号锁定 `Limits:AccountLockMinutes`（默认 15 分钟），状态持久化；锁定期间即使口令正确也返回 401。审计 `auth.lockout`。
- 账号不存在时同样执行一次等价 PBKDF2 计算（消除用户名枚举的时间差），并额外延迟约 120ms（口令错误约 60ms）。
- 错误文案统一为「用户名或口令错误」，不泄露账号是否存在。

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
| GET | `/biz` | 200 `text/html` | 业务对接台（`wwwroot/biz.html`） |
| GET | `/biz/` 及 `/biz/{*rest}` | 302 → `/biz` | 同上 |
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
| POST | `/api/auth/register` | `{ userName, password, displayName, email }` | 3 次/小时（按 IP） | `auth.register` |

**GET `/api/public/health`** —— 运行状态与降级说明。

`data` 字段：

| 字段 | 说明 |
| --- | --- |
| `status` | 固定 `ok` |
| `serverTime` | 服务器 UTC 时间 |
| `aiConfigured` | 是否配置了可用的 AI 密钥 |
| `aiModel` | 模型名（未配置时为 `null`） |
| `aiStatus` | `off` / `degraded` / `online`：只有"最近一次调用成功"才算 `online` |
| `aiLastSuccessAt` / `aiLastFailureAt` / `aiLastError` | 最近成功/失败时间与错误（脱敏后字符串） |
| `mode` | 当前模式的文字说明（AI 意图解析 + 审核 / 已降级 / 本地解析） |
| `selfRegistration` | 是否开放自助注册（`Security:AllowSelfRegistration`） |

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
| `user` | `{ id, userName, displayName, role, roleLabel, emailMasked, permissions: [{ key, label }] }` |
| `signing` | `{ algorithm: "HMAC-SHA256", canonical: "METHOD\nPATH?QUERY\nX-Timestamp\nX-Nonce\nSHA256_HEX(rawBody)", headers: ["X-Timestamp","X-Nonce","X-Signature"], requiredFor: ["POST","PUT","PATCH","DELETE"] }` |

失败：

| 状态码 | code | 场景 |
| --- | --- | --- |
| 403 | `IP_BANNED` | 来源 IP 已被封禁 |
| 429 | `RATE_LIMITED` | 超过 6 次/5 分钟（同时记 1 分） |
| 400 | `BAD_REQUEST` | 请求体非法、用户名/口令为空、口令超过 128 字符 |
| 401 | `AUTH_FAILED` | 用户名或口令错误 / 账号被禁用 / 账号被锁定（`message` 中给出解锁时间） |

> 该接口是公开路径，因此**不校验请求签名**；但仍受 Origin 校验约束（浏览器跨站提交会被 `BAD_ORIGIN` 拒绝）。

**POST `/api/auth/register`** —— 自助注册（只能得到「用户」角色，角色不由客户端决定）。

| 字段 | 必填 | 说明（`Api.Clean` 截断） |
| --- | --- | --- |
| `userName` | 是 | ≤ 32 字符；3-32 位，仅字母/数字/下划线/点/短横线 |
| `password` | 是 | ≥ 10 位且包含大写/小写/数字/符号中至少三类；不得包含用户名、`admin`、常见弱口令片段 |
| `displayName` | 否 | ≤ 32 字符，缺省取用户名 |
| `email` | 否 | ≤ 128 字符；格式非法直接失败；仅以掩码形式保存展示，原值 AES-256-GCM 加密存储 |

成功（200）：`data = { message, userName }`。
失败：403 `REGISTER_DISABLED`（关闭自助注册）、429 `RATE_LIMITED`、400 `BAD_REQUEST` / `REGISTER_FAILED`（用户名重复、口令不合规、邮箱非法等，原因在 `message`）。

### 4.2 需要身份（令牌）的账号接口

| 方法 | 路径 | 权限 | 签名 | 请求体 | 响应 `data` | 失败码 | 审计 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| POST | `/api/auth/logout` | 任意已登录角色 | 需要 | 无（或 `{}`） | `{ message }` | 401 | `auth.logout` |
| GET | `/api/auth/me` | 任意已登录角色 | 不需要 | 无 | `{ id, userName, displayName, role, roleLabel, emailMasked, permissions[], lastLoginAt }` | 401 | 无 |
| POST | `/api/auth/password` | 任意已登录角色 | 需要 | `{ newPassword }` | `{ message, relogin: true }` | 429 / 400 `WEAK_PASSWORD` / 500 `FAILED` | `user.password` |

- `POST /api/auth/logout`：只写审计并返回提示，服务端**没有令牌吊销列表**（令牌在过期或世代变更前仍然有效），客户端需要自行清除本地令牌。
- `POST /api/auth/password`：独立限流 5 次/15 分钟；成功后 `TokenEpoch++`，所有旧令牌立即失效，必须重新登录。
- 三个接口都要求 `Authorization: Bearer`；写接口（logout/password）还需要请求签名。

---

## 5. 对话式代理 `/api/agent/*`

权限：任意已登录角色（`user` / `staff` / `admin`）。
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
| `source` | 计划来源：`ai`（模型解析）/ `ai-fallback`（模型输出不可解析，退回本地解析）/ `quota-fallback`（今日额度用尽，退回本地解析）/ `local-fallback`（未配置 AI 或调用失败，本地指令解析）/ `biz-ui`（业务对接台的结构化动作） |
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

## 6. 业务对接台 `/api/biz/*`

甲方（`user`）/ 乙方（`staff`、`admin`）对接业务的读写入口。**写操作没有旁路**：全部转交 `AgentOrchestrator.HandleActionAsync`，走「规则引擎 → AI 安全审核 → 人工审批 → Harness 执行」。

| 方法 | 路径 | 权限 | 签名 | 限流 | 审计 |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/biz/overview` | 任意已登录角色（数据按角色裁剪） | 否 | 读 180/分钟 | `biz.read`（员工及以上且客户数 > 0 时） |
| POST | `/api/biz/action` | 任意已登录角色（工具权限另行校验） | **是** | 30/5 分钟（用户，`Limits:BizActionPerFiveMinutesPerUser`）、90/5 分钟（IP，`Limits:BizActionPerFiveMinutesPerIp`） | `biz.action`（`SUBMIT`），以及下游 `input.injection-suspect`、`ai.review`、`task.*` |

### 6.1 GET `/api/biz/overview`

`data` 字段：

| 字段 | 说明 |
| --- | --- |
| `role` / `roleLabel` | 当前角色 |
| `aiStatus` / `aiLastError` | AI 状态与最近错误（未配置时为空串） |
| `self` | `{ linked, client }`：甲方账号绑定的客户档案（未绑定时 `linked=false`、`client=null`） |
| `myRequirements` | 甲方自己的需求单（最多 50 条）；员工/管理员为空数组 |
| `clients` | 客户档案（员工及以上最多 100 条；甲方为空数组） |
| `requirements` | 需求单（员工及以上最多 200 条；甲方为空数组） |
| `stats` | **仅管理员**：`{ clientCount, requirementCount, pendingCount, myPendingCount }`；其它角色为 `null` |

- 甲方只能看到与**自己账号绑定**的那一条客户档案，以及该客户的需求单（水平越权防护）。
- 员工及以上批量读取客户资料属于留痕行为，会写审计 `biz.read`。

### 6.2 POST `/api/biz/action`

请求：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `tool` | 是 | 工具名（业务对接台使用 `stateChanging=true` 的业务工具，例如 `submit_requirement`、`update_client_profile`、`assign_client_owner`、`update_requirement`、`update_record`，完整清单见 `Tools/ToolCatalog.cs` 或 `GET /api/agent/tools`）；空 → 400 |
| `args` | 是 | 参数对象；键 ≤ 40 字符、值 ≤ 2000 字符（`Api.Clean`），**最多 12 个键**，超出 → 400 `TOO_MANY_ARGS` |
| `reason` | 否 | 业务理由（≤ 200 字符），会写入审计与审批上下文 |

成功（200）`data`：与 `/api/agent/chat` 基本一致，但 `steps` 只包含 `{ index, tool, title, summary, status, elapsedMs }`（**不含** `args` 与 `result`），且没有 `stepLimitReached` 字段。

要点：

- 被规则引擎/AI 拒绝、待审批、自动执行等结果都返回 **HTTP 200**，业务结果看 `data.status`（`REJECTED_RULE` / `REJECTED_AI` / `PENDING_APPROVAL` / `EXECUTED` / `FAILED` / `NEED_INPUT` / `CHAT`）。
- 业务参数与 `reason` 也会做注入特征扫描（命中只观测，写操作仍强制转人工）。
- 失败码：400 `BAD_REQUEST` / `TOO_MANY_ARGS`、429 `RATE_LIMITED`（审计 `biz.rate-limit`）、401 系列、499 `CANCELLED`。

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

### 7.2 用户管理

| 方法 | 路径 | 请求体 | 响应 `data` | 失败码 | 审计 |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/admin/users` | —— | 账号数组（见下） | —— | 无 |
| POST | `/api/admin/users` | `{ userName, password, displayName, role, email }` | `{ message, user }` | 400 `BAD_REQUEST` / `BAD_ROLE` / `CREATE_FAILED` | `user.create` |
| POST | `/api/admin/users/{id}/role` | `{ role }` | `{ message }` | 400 `BAD_ROLE` / `SELF_DEMOTE`、404 `NOT_FOUND` | `user.role` |
| POST | `/api/admin/users/{id}/status` | `{ disabled: true|false }` | `{ message }` | 400 `BAD_REQUEST` / `SELF_DISABLE`、404 `NOT_FOUND` | `user.disable` / `user.enable` |
| POST | `/api/admin/users/{id}/reset-password` | `{ newPassword }` | `{ message }` | 400 `BAD_REQUEST` / `RESET_FAILED` | `user.password` |

`GET /api/admin/users` 每条记录：`id`、`userName`、`displayName`、`role`、`roleLabel`、`emailMasked`、`disabled`、`locked`、`lockedUntil`、`failedLoginCount`、`createdAt`、`createdBy`、`lastLoginAt`、`lastLoginIp`、`aiCallsToday`（按用户名排序）。**不返回任何口令散列字段。**

约束：

- `role` 只能是 `user` / `staff` / `admin`。
- 新建账号的口令强度与注册相同（≥ 10 位、三类字符、不含用户名/`admin`/常见弱口令）。
- 不能把自己的管理员角色降级（`SELF_DEMOTE`），不能禁用自己的账号（`SELF_DISABLE`）。
- 改角色、禁用/启用、重置口令都会 `TokenEpoch++`，目标账号的旧令牌立即失效。
- 自助注册产生的账号固定为 `user`，员工与管理员只能由管理员在后台分配。

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
| `Security:AllowSelfRegistration` | 是否开放 `POST /api/auth/register` |
| `Security:SelfRegistrationPerHourPerIp` | 注册限流（默认 3 次/小时/IP） |
| `Security:MaxRequestBodyBytes` | 请求体上限（默认 262144） |
| `Security:MaxUserInputChars` | 对话内容上限（默认 2000） |
| `Security:AutoApproveSandboxWrites` | 私人工作区低风险写是否免人工审批（默认 `true`；置 `false` 则全部转人工） |
| `Security:TrustedOrigins` / `AISERVER_TRUSTED_ORIGINS` | 反向代理终止 TLS 场景的合法来源白名单（数组；环境变量用分号/逗号/空格分隔），见 2.4 |
| `Limits:LoginPerFiveMinutesPerIp` / `LoginPerFifteenMinutesPerAccount` / `AccountLockMinutes` | 登录限流与账号锁定阈值 |
| `Limits:AgentChatPerFiveMinutesPerUser` / `AgentChatPerFiveMinutesPerIp` | 对话接口限流 |
| `Limits:ReadPerMinutePerUser` | 所有 `GET /api/*` 的读接口限流 |
| `Limits:AdminWritePerMinutePerAdmin` | 后台写操作限流（默认 60 次/分钟） |
| `Limits:BizActionPerFiveMinutesPerUser` / `BizActionPerFiveMinutesPerIp` | 业务对接台写操作限流（默认 30 / 90 次每 5 分钟） |
| `Limits:WafStrikesBeforeBan` / `HoneypotStrikesBeforeBan` / `BanMinutes` | 自动封禁的计分阈值与封禁时长 |
| `Ai:ApiKey` / `AISERVER_AI_KEY` | AI 是否可用（未配置则降级为本地解析） |
| `Ai:BaseUrl` / `Ai:AllowedHosts` | 出站模型地址与主机白名单 |
| `Ai:MaxAgentSteps` / `Ai:AgentTimeoutSeconds` | 多步循环步数上限（默认 5）与墙钟上限（默认 60 秒） |
| `Ai:DailyCallsPerUser` | 每账号每日 AI 调用额度（默认 200）；用尽后该次对话降级为本地解析并在 `notes` 中说明 |
| `Mail:Enabled` / `Mail:PublicBaseUrl` / `Mail:ApproverAddress` | 是否真实发信、审批链接基地址、审批人地址 |

---

## 9. 需要留意的点与以代码为准的说明

第 1 ~ 4 条是已核对代码后确认的现状（含与历史版本/配置名不一致的地方）；第 5 条起是本文没有从代码中得到确定结论的内容，使用前请以代码为准：

1. 曾经的 `AiAnalyzeRequest` 请求模型**已从仓库移除**，代码中不存在此类请求体，也不存在对应的 HTTP 接口（如 AI 分析入口）；管理端的 AI 分析走 `POST /api/agent/chat`（见 7.7）。
2. 任务打回只有一条路径：`TaskService.DecideAsync`（审计 `approval.reject`）。写入 `task.reject` 的 `TaskService.RejectAsync` 已删除，因此 **`task.reject` 这个动作名在当前版本不会再产生**（1.6 中保留该行仅为对照历史数据）。
3. 后台写操作限流读自 `Limits:AdminWritePerMinutePerAdmin`（默认 60 次/分钟）；业务对接台写操作读自 `Limits:BizActionPerFiveMinutesPerUser` / `Limits:BizActionPerFiveMinutesPerIp`（默认 30 / 90 次每 5 分钟）。两个阈值都可配置；注意这两组值都**没有**出现在 `/api/admin/security` 的 `limits` 返回里（该接口只回登录/对话/封禁相关项，见 7.6）。
4. 出站通知只有一个 `App_Data/outbox.json` 文件，**没有** `outbox/` 目录（`StoreNames.OutboxDir` 常量已删除）。
5. `/api/admin/outbox/{id}/recall` 分支没有写审计（`recall` 只做补偿事务），若需要留痕请以代码为准或后续补齐。
6. 请求签名中「路径」段的解码形态：代码取的是 ASP.NET Core 解析后的 `Request.Path.Value` 与原始 `Request.QueryString.Value` 拼接。常规路径与常规编码下与服务端一致；涉及特殊编码（如路径内的 `%2F`、重复编码的查询参数）时，建议以实际服务端行为（或 `Security/SecurityMiddleware.cs` 的实现）为准。
7. `AgentOutcome.source` / `reviewVerdict` 的取值集合由 `Ai/AgentOrchestrator.cs` 内的字面量决定，本文列出的是当前代码中出现的取值，新增分支时以代码为准。
8. 会话上下文（`ChatSessionStore`）为**进程内存**存储，多实例部署时不共享；限流与封禁计数同样在内存中，重启即清零（账号锁定状态除外，它持久化在 `users.json`）。
