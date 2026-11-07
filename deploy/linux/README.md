# Linux 部署手册（AI 报销系统）

> 面向：把 `AIServer` 部署到一台 **Linux 服务器**（Ubuntu 22.04/24.04、Debian 12 均适用）。
> 本目录是本仓库**新增**的部署套件：systemd 单元、nginx 配置、env 模板、安装脚本。
> 仓库文档 `docs/部署与配置.md` 已明确"Linux 下的服务化（systemd）未随仓库提供单元文件"，这里补齐这一块。
>
> 状态说明：**发布包由本机 `dotnet publish -r linux-x64` 实打实产出并校对过内容**；
> systemd 单元与 nginx 配置是按服务代码的实际行为写的，但**未在真实 Linux 机器上跑过**
> （`AiApprovalServer` 启动横幅、回环监听、`403 BAD_ORIGIN` 行为均已在 Windows 本机实测）。

---

## 1. 要部署的是什么：两个进程，缺一不可

| 服务 | 目录 | 技术栈 | 监听 | 作用 |
| --- | --- | --- | --- | --- |
| **主服务器** | `AiApprovalServer/` | C# .NET 8（自包含，服务器**不需要装 .NET**） | `127.0.0.1:8899` | 账号权威、四角色权限、报销单**唯一写入口**、审计哈希链、审批与回退、`/`、`/finance`、`/admin` 三个页面 |
| **对话网关** | `xiaocainu/` | Python 3.10+ / FastAPI | `127.0.0.1:8000` | 对话界面、意图解析、发票 OCR、回显；**不持有账号库、不直接写报销单** |

关键约束（决定部署形态）：

1. **网关必须能连到主服务器**，且主服务器必须先起来（网关登录会把口令转发给主服务器判定）。
2. **两个服务自身都不做 TLS**，对外必须由 nginx/Caddy 终止 TLS 后转发回环。
3. **主服务器只按 TCP 对端地址记客户端 IP**，不解析 `X-Forwarded-For`；反代后按 IP 的限流会退化成全局限流。
4. 主服务器带**来源校验**：TLS 在代理终止时，写请求（登录、提交……）会因 `Origin: https://...` 与服务端看到的 `http://...` 不一致而 **403 `BAD_ORIGIN`**，必须配 `AISERVER_TRUSTED_ORIGINS`。
5. 网关节点的 `BACKEND_MODE`（`preferred` / `required`）**已从代码中删除**，`docs/部署与配置.md` 1.2 与 11 节仍提到它，属过期描述；现在是否启用只看 `BACKEND_BASE_URL` 是否为空。
6. 网关的 `knowledge/` 用**相对路径**（`models/config.py: knowledge_dir()`），所以工作目录必须是应用目录，否则知识库静默丢失。

---

## 2. 服务器要求

| 项目 | 要求 |
| --- | --- |
| 系统 | Ubuntu 22.04 / 24.04、Debian 12 等 x86_64 Linux |
| 内存 / 磁盘 | 1 GB 内存起（不含本地大模型）；程序约 100 MB，数据目录随审计链增长 |
| 必需软件 | `nginx`、`openssl`、`curl`、`tar`、`python3`（3.10+）+ `python3-venv` |
| 出网（Egress） | 主服务器 → `api.deepseek.com`（AI）、`open.feishu.cn`（飞书）；网关 → 同上 |
| 入网（Ingress） | 只开 **80/443**；**8899 与 8000 一律不对外** |
| 时钟 | 必须 NTP 同步：请求签名有效窗口 `Security:ClockSkewSeconds`（默认 300 秒） |
| 架构 | 发布包是 `linux-x64`；ARM 服务器需在开发机重新 `-r linux-arm64` 发布 |

```bash
# Ubuntu/Debian 一次性准备
sudo apt update
sudo apt install -y nginx openssl curl tar python3 python3-venv python3-pip
```

---

## 3. 目录规划（脚本按这个布局安装）

```
/opt/ai-approval/app/           主服务器发布包（AiApprovalServer + *.so + wwwroot/ + appsettings.json + config/）
/opt/xiaocainu/app/             网关源码 + .venv/
/var/lib/ai-approval/           主服务器数据目录（users.json / tasks.json / audit.jsonl / blobs/）★ 备份对象
/var/lib/xiaocainu/             网关预留的可写目录（网关本身无状态）
/etc/ai-approval/ai-approval.env   主服务器密钥与配置（0640 root:ai-approval）
/etc/ai-approval/xiaocainu.env     网关密钥与配置（0640 root:ai-approval）
```

运行账号：系统账号 `ai-approval`（`nologin`，无家目录 shell），两个服务共用。

---

## 4. 开发机：产出两个部署包

### 4.1 主服务器（自包含 linux-x64，约 96 MB / 328 个文件）

```powershell
cd D:\dsh-sandbox\AIServer
dotnet publish AiApprovalServer\AiApprovalServer.csproj -c Release -r linux-x64 `
  --self-contained true -p:PublishSingleFile=false -p:PublishTrimmed=false `
  -o dist\AiApprovalServer-linux-x64
tar -czf dist\AiApprovalServer-linux-x64.tar.gz -C dist AiApprovalServer-linux-x64
```

- **必须保持 `PublishSingleFile=false` 且 `PublishTrimmed=false`**（csproj 已锁定）：`wwwroot/` 与 `appsettings.json` 必须是**松散文件**，否则没法在服务器上直接改页面和配置。
- 产物里已经带 `appsettings.json`、`wwwroot/`、`config/secrets.local.example.json`；**不带** `config/secrets.local.json`（csproj 显式禁止复制，避免把凭证打进交付包）。
- Linux 上执行的 `AiApprovalServer` 是 apphost，上传后需要 `chmod +x`（安装脚本已处理）。

### 4.2 对话网关（源码包，去掉密钥与垃圾）

```powershell
tar -czf dist\xiaocainu-linux.tar.gz `
  --exclude='xiaocainu/.env' --exclude='xiaocainu/.venv' --exclude='__pycache__' `
  --exclude='.pytest_cache' --exclude='xiaocainu/*.zip' --exclude='xiaocainu/tests' `
  xiaocainu
```

> **务必排除 `xiaocainu/.env`**：本机那份 `.env` 里有真实的 DeepSeek API Key 与飞书 `app_secret`。
> 仓库根目录现成的 `xiaocainu-deploy.tar.gz` **包含 `.env`，不要直接传到服务器**。

### 4.3 上传

```bash
scp dist/AiApprovalServer-linux-x64.tar.gz dist/xiaocainu-linux.tar.gz user@server:/tmp/
scp -r deploy/linux user@server:/tmp/ai-deploy/
```

---

## 5. 服务器：一键安装

```bash
sudo chmod +x /tmp/ai-deploy/install.sh
sudo /tmp/ai-deploy/install.sh /tmp/AiApprovalServer-linux-x64.tar.gz /tmp/xiaocainu-linux.tar.gz
```

脚本做的事（**幂等，可反复执行当作升级**）：

1. 建 `ai-approval` 系统账号与目录（数据目录 `0700`，配置目录 `0750`）；
2. 解包主服务器到 `/opt/ai-approval/app`，**保留**已存在的 `appsettings.json` 与 `config/`；
3. 解包网关到 `/opt/xiaocainu/app`，建 venv、`pip install -r requirements.txt`（需能访问 PyPI）；
4. 首次运行时**生成密钥**（`openssl rand`）写入两个 env 文件并用 `0640` 保护；已存在则原样保留；
5. 安装并 `enable --now` 两个 systemd 单元，最后打一次 `/api/public/health` 与 `/health` 自检。

装完**立刻**编辑两个 env 文件（见第 6 节），否则 AI 走降级、飞书写入不可用、登录会 403。

---

## 6. 必改配置

### 6.1 `/etc/ai-approval/ai-approval.env`（主服务器）

| 变量 | 说明 |
| --- | --- |
| `AISERVER_LISTEN_URL` | 保持 `http://127.0.0.1:8899`（**不要** `0.0.0.0`） |
| `AISERVER_DATA_ROOT` | `/var/lib/ai-approval`（独立于程序目录，便于备份与升级） |
| `AISERVER_JWT_SECRET` / `AISERVER_AES_KEY` / `AISERVER_PEPPER` | 脚本已随机生成；**换掉 JWT 会让所有人掉线，换掉 AES/pepper 会让历史密文读不出** |
| `AISERVER_AI_KEY` | DeepSeek Key；留空 = 降级模式（本地解析 + 变更类操作全转人工），功能仍完整 |
| `AISERVER_TRUSTED_ORIGINS` | **必填** `https://ops.你的域名`（多个用逗号/分号）；不填则浏览器写请求 403 |
| `AISERVER_FEISHU_APPID` / `_APPSECRET` / `_APPTOKEN` / `_TABLEID` | 注意是**大写拼接、没有下划线**（`AISERVER_FEISHU_APPID`，不是 `FEISHU_APP_ID`） |
| `AISERVER_TRUSTED_PROXIES` | **保持空**。nginx 与服务同机都在 `127.0.0.1`，一旦放行就等于允许客户端伪造 `X-Client-Ip` |

其余旋钮（`Limits:*`、`Ai:*`、`Feishu:TimeoutMs` …）都在 `/opt/ai-approval/app/appsettings.json`，明文，改完**重启生效**。
反代场景建议把按 IP 的限流放宽（`LoginPerFiveMinutesPerIp` 等），因为所有请求的 IP 都会是 `127.0.0.1`。

### 6.2 `/etc/ai-approval/xiaocainu.env`（网关）

| 变量 | 说明 |
| --- | --- |
| `BACKEND_BASE_URL` | `http://127.0.0.1:8899`；**留空网关直接拒绝服务** |
| `BACKEND_TIMEOUT` | 30 秒足够；不要超过 nginx 的 `proxy_read_timeout` |
| `LLM_PROVIDER` | `deepseek`（云端）或 `ollama`（本机）；`agnes` 亦可 |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | 网关侧也调模型（对话解析 + OCR） |
| `OCR_MAX_IMAGE_MB` | 默认 8；决定 nginx 的 `client_max_body_size`（base64 膨胀约 1.37 倍） |
| `FEISHU_APP_ID` / `_SECRET` / `_APP_TOKEN` / `_TABLE_ID` | 仅用于上传发票附件到主服务器返回的 record 行 |

> 环境变量优先于应用目录里的 `.env`（网关的 `.env` 加载器不覆盖已存在变量），所以服务器上**不需要**放 `.env`，密钥只留在 `/etc` 那个 0600/0640 文件里。

---

## 7. nginx + TLS

```bash
sudo cp /tmp/ai-deploy/nginx-ai-approval.conf /etc/nginx/conf.d/ai-approval.conf
sudo sed -i 's/example.com/你的域名/g' /etc/nginx/conf.d/ai-approval.conf
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d app.你的域名 -d ops.你的域名
sudo nginx -t && sudo systemctl reload nginx
```

两个入口的设计：

| 入口 | 后端 | 谁用 |
| --- | --- | --- |
| `https://app.你的域名/` | 网关 `:8000` | 员工对话填单（`client_max_body_size 12m`，容纳 8 MB 发票图） |
| `https://ops.你的域名/`、`/finance`、`/admin` | 主服务器 `:8899` | 财务审核、管理后台（`client_max_body_size 256k`，与 `Security:MaxRequestBodyBytes` 对齐） |

要点：

- **`Host` 原样透传**，`Origin` 不要改（靠 `AISERVER_TRUSTED_ORIGINS` 放行）；本服务**不启用** ForwardedHeaders，不要试图用 `X-Forwarded-Proto` 修正 scheme。
- ops 入口显式 `proxy_set_header X-Client-Ip "";` —— 清掉浏览器伪造的 IP 头。
- 本服务只在 `Request.IsHttps` 时自己加 HSTS，所以 HSTS 在 nginx 侧加。
- 只有一个域名时：先只对外暴露员工入口，运维侧用 `ssh -L 8899:127.0.0.1:8899 user@server` 访问后台页面（把两套 SPA 塞进同一域名的子路径会同时踩前端绝对路径与 Origin 校验两个坑）。
- 请求签名覆盖"方法 + 路径与查询串 + 时间戳 + nonce + 请求体哈希"，代理**禁止**压缩或改写请求体，代理机与服务机都要 NTP 对时。

---

## 8. 启动顺序与验收

```bash
sudo systemctl start ai-approval        # 1. 先主服务器
sudo systemctl start xiaocainu          # 2. 再网关

# 主服务器启动横幅（入口、数据目录、AI 状态、演示账号）
sudo journalctl -u ai-approval -n 40 --no-pager
```

验收清单（按这个顺序，失败点好定位）：

```bash
# ① 主服务器存活（只返回 status + serverTime，不需要登录）
curl -s http://127.0.0.1:8899/api/public/health

# ② 网关存活
curl -s http://127.0.0.1:8000/health

# ③ 经 nginx 的员工入口（应拿到对话页面 HTML）
curl -sI https://app.你的域名/ | head -3

# ④ 经 nginx 的运维入口 + 来源校验（关键一步）
curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'Origin: https://ops.你的域名' -H 'Content-Type: application/json' \
  -d '{"userName":"root","password":"Overlord#2026ak"}' \
  https://ops.你的域名/api/auth/login
# 期望 200；若 403，就是 AISERVER_TRUSTED_ORIGINS 没配或写错（必须带协议）

# ⑤ 浏览器：https://app.你的域名 用主服务器账号登录 → 说一句“昨天打车38块要报” →
#    回显单号 = 三跳（网关 → 主服务器 → 飞书）全通
```

首次登录后**立刻**处理账号：

- 演示账号 `employee` / `finance` / `admin` / `root`（口令见根 `README.md` 6.1）只在 `users.json` 为空时播种，改密或删除后不会被重建；
- 初始超管（`Bootstrap:Admin`）只在用户名不存在时播种一次；
- 管理后台 → 账号管理 → 重置口令；改密后该账号令牌世代 +1，旧令牌立即失效。

---

## 9. 升级、备份、回滚

```bash
# 升级（程序与前端换新，配置与数据保留）
sudo /tmp/ai-deploy/install.sh /tmp/AiApprovalServer-linux-x64.tar.gz /tmp/xiaocainu-linux.tar.gz
sudo systemctl restart ai-approval xiaocainu

# 手工升级也可以：只换二进制与静态页
sudo cp /tmp/new/AiApprovalServer.dll /opt/ai-approval/app/
sudo cp -r /tmp/new/wwwroot/* /opt/ai-approval/app/wwwroot/
sudo systemctl restart ai-approval
```

- **`appsettings.json` 与 `config/` 不要被新包覆盖**（脚本已保留），那里有手改的 `TrustedOrigins`、限流阈值与飞书凭证。
- 备份对象：整个 `/var/lib/ai-approval/`（含 `secrets.json`、`audit.jsonl`、`blobs/`）。停服复制或文件系统快照最稳。
- 恢复：停服 → 整目录覆盖 → 启动 → 后台"审计链校验"确认没断链。
- `audit.jsonl` 是哈希链，**禁止手工编辑**；改动会让链校验报"序号断裂/前序哈希不匹配"。
- 回滚程序：脚本会留 `/opt/ai-approval/app.old`，直接换回并重启即可。

---

## 10. 已知坑（都是代码里的真实行为）

| 现象 | 原因与处理 |
| --- | --- |
| 浏览器登录 403 `BAD_ORIGIN` | TLS 在 nginx 终止 → 来源不一致。把 `https://ops.域名` 写进 `AISERVER_TRUSTED_ORIGINS`（必须带协议、不支持通配符），或在 nginx 里改写 `Origin`（备选，二者不要同时依赖） |
| 正常用户互相挤占限流 / 频繁 429 | 反代后客户端 IP 都是 `127.0.0.1`，按 IP 的限流退化为全局。放宽 `Limits:LoginPerFiveMinutesPerIp` 等，账号级锁定仍在 |
| 页面全是相对路径 404 / 知识库"没命中" | 网关 `WorkingDirectory` 必须是应用目录（`knowledge/` 走相对路径）；unit 里已双保险设了 `KNOWLEDGE_DIR` |
| 网关登录提示"本地用户表校验（主服务器未启用或不可达）" | 主服务器没起、`BACKEND_BASE_URL` 写错，或该账号不在 `users.json`（账号权威只在主服务器） |
| 服务一起来就退出 | 看 `journalctl -u ai-approval`：`StrictSecrets=true` 但没给 `AISERVER_JWT_SECRET`；或给了非法 base64 的 `AES_KEY` |
| 改了 `TrustedOrigins` 没生效 | 只认环境变量与 `appsettings.json`，改完必须 `systemctl restart ai-approval` |
| 上传发票 413 | 网关入口 `client_max_body_size` 小于 `OCR_MAX_IMAGE_MB × 1.37`；ops 入口的上限应与 `Security:MaxRequestBodyBytes`（262144）一致 |
| 请求签名报"时间戳超出有效期" | 代理机或服务机时钟漂移超过 300 秒，装 NTP |
| 单实例边界 | JSON 文件存储、无数据库、无分布式锁：**只能单实例**、不能多机共享同一数据目录；重启后进程内的登录锁定计数与限流计数清零 |

---

## 11. 交付/上传前自查

- [ ] 上传包不含 `xiaocainu/.env`、`config/secrets.local.json`、`App_Data/`
- [ ] `appsettings.json` 里 `Ai:ApiKey` 为空（真实 Key 只走 env）
- [ ] `/etc/ai-approval/*.env` 权限 `0640`，属主 `root:ai-approval`
- [ ] 安全组只放 80/443，`8899`、`8000` 不可从公网访问（`sudo ss -ltnp | grep -E '8899|8000'` 应只见 `127.0.0.1`）
- [ ] 演示账号口令已改；初始超管 `BootstrapAdmin` 段已清空
- [ ] 已配置备份任务（`/var/lib/ai-approval`）
- [ ] 已在真实浏览器验证：登录、对话填单、`/finance` 审核、`/admin` 审计链校验各一次
