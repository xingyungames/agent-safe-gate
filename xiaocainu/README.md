# 小财奴 · 对话式报销前端网关

> 本目录是系统的**表现层**：对话交互、意图理解、发票 OCR、结果回显。
> 账号判定、权限判定、报销单写入**全部委托给 C# 主服务器**——本侧不持有账号库，也不直接写报销单主表。

**作者**：小新 · **技术栈**：Python 3 + FastAPI · **默认端口**：8000

完整系统说明见仓库根 [README.md](../README.md) 第 5 节；开发细节见 [DEVELOPMENT.md](DEVELOPMENT.md)。

---

## 它负责什么

| 负责 | 不负责 |
| --- | --- |
| 对话界面与多轮追问 | 决定用户是谁（一律问主服务器） |
| 自然语言 → 结构化报销字段 | 决定能不能提交（一律由主服务器校验） |
| 发票图片 OCR | 直接写报销单主表（一律经主服务器写入口） |
| 回显单号、状态、进度 | 自己维护一份账号库 |

唯一的直连飞书动作是**上传发票附件**（挂到主服务器返回的 `record_id` 那一行上），附件不参与任何判断逻辑。

---

## 快速开始

```bash
python -m pip install -r requirements.txt
cp .env.example .env
# 编辑 .env：BACKEND_BASE_URL 指向主服务器，填模型密钥
python -m api.main 127.0.0.1 8000
```

打开 <http://127.0.0.1:8000>，用主服务器上的账号登录即可。

**`BACKEND_BASE_URL` 必须指向一个已启动的主服务器**（默认 `http://127.0.0.1:8899`）。留空时网关拒绝服务——因为账号与权限只在主服务器那一侧。

---

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 网关自身存活（不反映主服务器状态） |
| POST | `/login` | 转发到主服务器 `/api/auth/login`，返回令牌与 `signKey` |
| GET | `/me` | 转发到主服务器 `/api/auth/me` |
| POST | `/logout` | 只清网关侧身份缓存 |
| POST | `/chat` | 对话主入口：意图理解 + 多轮 + OCR 回填 |
| POST | `/ocr` | 发票图片 → 结构化字段 |
| POST | `/reimburse` | 结构化填单，经主服务器 `/api/invoice/submit` 落表 |
| POST | `/progress` | 进度查询，范围由主服务器按角色裁剪 |

写请求会自动带上 `X-Timestamp` / `X-Nonce` / `X-Signature`（HMAC-SHA256），密钥取自登录返回的 `signKey`。

---

## 目录

```
xiaocainu/
├── api/
│   ├── main.py            FastAPI 入口与路由分发
│   ├── auth_service.py    认证缓存层（令牌只校验，不签发）
│   ├── backend_client.py  主服务器适配器（无状态：转发 + 签名）
│   └── session.py         会话内存存储
├── models/                Agent、LLM 适配、OCR、知识库检索、飞书附件
├── frontend/index.html    对话界面（原生 JS，零依赖）
├── knowledge/             报销制度知识库
├── security/              输入净化、注入特征检查、限流
└── tests/                 单元与接口测试
```

---

## 测试

```bash
python -m pytest tests -q
```

---

## 维护提醒

- **不要**在这里重建账号体系或本地签发令牌——主服务器是唯一权威，绕过它等于绕过全部权限与审计。
- **不要**为了"跑通"而直接写飞书主表——所有报销单写入必须走主服务器 `/api/invoice/submit`。
- `.env` 含真实密钥，已在 `.gitignore` 中，切勿提交。
