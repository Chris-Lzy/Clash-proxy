# fnos-proxy

一个跑在 NAS / Docker 上的 **mihomo（Clash Meta）代理 + 中文 Web 控制台**。用一个网页管理多个订阅、一键开关代理、查看实时网速与流量用量，并为节点测速。

专为飞牛 fnOS 编写，但任何支持 Docker Compose 的 Linux 机器都能用。

## 功能

- **一键开关**：网页上点一下即可切换「代理 / 直连」
- **多订阅管理**：添加、删除、启停、单独更新订阅，节点自动标注来自哪个订阅
- **实时网速与用量**：当前上下行速率、今日/累计流量、近 7 天流量柱状图、每日活跃时长
- **节点测速**：一键检测全部节点延迟，结果自动用于「自动选路」挑最快节点
- **自动选路 / 手动选节点**：`AUTO`（url-test）与 `MANUAL`（select）两种模式
- **订阅自动更新**：mihomo 每天拉取订阅，健康检查和选路后台自动进行
- **零上报**：所有数据存本地 SQLite，不上传任何第三方

## 架构

```
浏览器 ──HTTP 8099──> dashboard (Python 标准库 + SQLite)
                            │  RESTful API
                            ▼
                       mihomo (Clash Meta)
                    7890 HTTP / 7891 SOCKS5
                            │
                            ▼
                       订阅节点
```

- `dashboard/app.py`：无第三方依赖，用 Python 标准库实现 Web 服务和 API，负责订阅增删、生成 mihomo 配置并热重载、流量采样与统计
- `mihomo/config.yaml`：mihomo 基础配置（首次启动会被 dashboard 根据订阅列表重写）

## 快速开始

### 1. 准备

```bash
git clone <你的仓库地址>
cd fnos-proxy
cp .env.example .env
# 编辑 .env，至少修改 MIHOMO_SECRET
```

### 2. 配置订阅（二选一）

- **启动时导入**：在 `.env` 里填 `INIT_SUB_URL=<你的订阅链接>`
- **启动后添加**：留空，启动后打开 Web 控制台，在「订阅管理」里粘贴链接添加

> 订阅链接属于敏感信息，**永远不要**把它写进 `mihomo/config.yaml` 或提交到 Git。旧版本从 config.yaml 迁移的逻辑仍保留，但会自动跳过占位符链接。

### 3. 启动

```bash
docker compose up -d
```

### 4. 访问

- Web 控制台：`http://<机器IP>:8099`
- 代理端点：HTTP `http://<机器IP>:7890`，SOCKS5 `socks5://<机器IP>:7891`

群晖、飞牛 fnOS 等 NAS 也可以直接在 Docker 管理界面用 `docker-compose.yml` 创建容器栈。

## 配置说明

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MIHOMO_SECRET` | `change-me-please` | mihomo 外部控制器的 API 密钥；dashboard 与 mihomo 必须一致（dashboard 会用它重写配置） |
| `INIT_SUB_URL` | 空 | 首次启动导入的订阅链接；已有订阅时会被忽略 |

端口映射在 `docker-compose.yml` 里按需调整。注意 mihomo 的 `9090` 管理端口只绑定到 `127.0.0.1`，不要暴露到公网。

## API

控制台对外提供以下接口（均无需鉴权，仅限内网使用）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/overview` | 版本、模式、开关状态、节点列表、订阅信息 |
| GET | `/api/traffic` | 当前上下行速率与累计流量 |
| GET | `/api/stats` | 今日/近 7 天流量、活跃时长、操作记录 |
| GET | `/api/subs` | 订阅列表 |
| POST | `/api/switch` | `{"on": true/false}` 开 / 关代理 |
| POST | `/api/node` | `{"name": "节点名"}` 手动选节点 |
| POST | `/api/auto` | 切换为自动选路 |
| POST | `/api/speedtest` | 测速全部节点 |
| POST | `/api/refresh` | `{"id": 1}` 更新指定订阅，留空更新全部 |
| POST | `/api/subs/add` | `{"name": "...", "url": "..."}` 添加订阅 |
| POST | `/api/subs/delete` | `{"id": 1}` 删除订阅 |
| POST | `/api/subs/toggle` | `{"id": 1, "enabled": true/false}` 启停订阅 |

## 安全提示

- 控制台目前**没有登录验证**，局域网内任何人都能操作。请只在可信内网使用；需要公网访问时务必加反向代理 + 认证。
- 订阅链接、API 密钥都是敏感信息，请勿提交到仓库或分享配置文件。
- `.gitignore` 已忽略 `.env` 和运行时数据，提交前请再次确认。

## 技术栈

- [mihomo（Clash Meta）](https://github.com/MetaCubeX/mihomo) —— 代理内核
- Python 3.11（标准库）+ SQLite —— 控制台后端
- 原生 HTML / CSS / JavaScript —— 前端，无构建步骤

## License

MIT
