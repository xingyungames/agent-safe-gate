# 报销系统 · 敏感配置备份

本文件夹是**上传 GitHub 之前**从仓库里导出的敏感文件备份。仓库里对应的文件已经清空或删除，需要时从这里恢复。

## 文件对照

| 备份文件 | 原位置 | 里面是什么 | 怎么恢复 |
| --- | --- | --- | --- |
| `secrets.local.json` | `AiApprovalServer/config/secrets.local.json` | AI 密钥（DeepSeek）、飞书 AppId / AppSecret / AppToken、报销单主表与用户表的 table_id 和网页地址、初始超管「星涵」的口令哈希与盐 | 把值填回仓库同名文件即可（该文件已被 `.gitignore` 忽略，也不会被复制进构建与发布产物）。填回后重启服务：AI 生效、飞书镜像与账号同步生效、初始超管按配置播种 |
| `xiaocainu.env` | `xiaocainu/.env` | 协作方网关配置：DeepSeek 与 Agnes 的模型密钥、飞书凭证、`AUTH_SECRET`、主服务器地址与模式 | 改名为 `.env` 放回 `xiaocainu/` 目录（已被 `xiaocainu/.gitignore` 忽略） |
| `xiaocainu.env.example` | `xiaocainu/.env.example` | 网关配置模板（本身不含真实密钥） | 参考用，不必恢复；仓库里保留的是占位符版本 |
| `appsettings.json` | `AiApprovalServer/appsettings.json` | 后端主配置。其中的 AI 密钥与飞书凭证已经移到 `secrets.local.json`，本文件本身不含密钥 | 参考用；仓库里的版本是干净的，一般不必覆盖 |
| `施工方案.md` | 仓库根目录 | 早期方案笔记。里面的飞书 AppId / AppSecret / base 标识在**仓库副本里已替换成占位符** | 需要原始值从这里取。这两份笔记已列入 `.gitignore`，不会被推送 |
| `目前实际接入.md` | 仓库根目录 | 早期接入笔记（同上） | 同上 |
| `xiaocainu-git历史.zip` | `xiaocainu/.git` | 协作方源码目录自带的 git 历史（213 个文件、约 0.5 MB）。仓库里已删除，避免嵌套仓库在上传时变成坏掉的子模块 | 需要时解压回 `xiaocainu/.git` 即可恢复历史 |

## 注意

- 初始超管账号是 `xinghan`（姓名「星涵」），口令已改成 **Ss@123456**，只以哈希（PBKDF2-HMAC-SHA256、21 万次、base64）形式存在于 `appsettings.json` 的 `Bootstrap:Admin` 与本备份的 `secrets.local.json`，明文不在任何文件里。这是公开的演示口令，登录后必须立刻改掉。
- 仓库里的 `AiApprovalServer/config/secrets.local.json` 现在是清空状态（各字段为空、`BootstrapAdmin.Enabled=false`），缺少它服务照常启动，只是 AI 走降级模式、飞书镜像被跳过、不播种初始超管。
- 上传前请再确认一次：`git status` 里不应出现 `config/secrets.local.json`、`xiaocainu/.env`、`施工方案.md`、`目前实际接入.md`、任何 `.zip`。
