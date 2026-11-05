# Plex302 · Plex 云盘 302 直链助手

[![build](https://github.com/celestial520/plex302/actions/workflows/docker.yml/badge.svg)](https://github.com/celestial520/plex302/actions/workflows/docker.yml)
[![image](https://img.shields.io/badge/ghcr.io-plex302-blue)](https://github.com/celestial520/plex302/pkgs/container/plex302)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Plex 通过 **CD2 挂载**直接扫描云盘里的真实文件，**不生成 strm**；外网播放时把 `/library/parts` 请求 **302** 到 115 / AList / CD2 直链，NAS 不再中转视频流量；CMS 整理完自动 **局部入库**（115 操作事件驱动），每天再用 **全量同步** 兜底。

> 一句话：Plex 负责刮削和播放决策，plex302 负责把“读文件”这件事甩给云盘，顺便把该扫的目录扫掉。

---

## v1.2.0 更新

| 变化 | 说明 |
|---|---|
| **115 操作事件增量入库** | 改成读 115 App 里的「最近操作」流水（`/behavior/detail`），游标增量消费，比轮询最近文件更准 |
| **静置 + 复核 + 每日兜底** | 事件不断顺延静置时间，扫完再复核一次，每天固定时间整库扫一遍，不怕 CMS 整理到一半被打断 |
| **全量同步** | 调 `GET /library/sections/{id}/refresh`（不带 path）让 Plex 把所有媒体库整库扫一遍，可手动、可定时 |
| **企业微信通知** | 入库完成 / 全量完成 / 出错误发应用消息，配置项和 115 秒传助手一致 |
| **CMS 对接两种方式** | 新增 Emby 兼容入口（CMS 里直接把 `EMBY_HOST_PORT` + `EMBY_API_KEY` 指过来），原来的 Webhook 保留 |
| **双主题 + 手机版** | 自动 / 浅色 / 深色手动切换；手机上侧栏变顶栏 + 底部导航 + 表格卡片化 |
| **间隔可直接点选** | 间隔输入框点一下弹出常用值（1 分钟 / 2 分钟 / 5 分钟…），也可以自己敲 |

从 1.1.x 升级只需换镜像，`config.json` 会自动补齐新字段（不用重新配）。

---

## 功能

| 能力 | 说明 |
|---|---|
| 不依赖 strm | Plex 直接扫描 CD2 挂载目录里的真实文件；不是 strm 占位 |
| 外网 302 | 拦截 `/library/parts/*` 和 `universal/start`，返回云盘直链 |
| 内网回源 | 局域网默认走 Plex 原链路，转码/字幕/WebUI 不受影响 |
| 局部扫描 | 调 Plex 官方 `GET /library/sections/<id>/refresh?path=<目录>`，不扫全库 |
| 全量同步 | 调 `refresh` 不带 path，整库扫一遍；支持每天定时 + 手动一键 |
| 115 操作事件 | 读 115「最近操作」流水，游标增量，CMS 一落盘就能发现 |
| CD2 缓存刷新 | 扫描前用 CD2 gRPC `GetSubFiles(forceRefresh=true)` 刷新目录，避免新文件不可见 |
| CMS 对接 | Emby 兼容入口（CMS 原生支持）+ Webhook + 目录监听三重保险 |
| 企业微信通知 | 入库汇总、全量结果、错误告警，同 115 秒传助手一套配置 |
| 115 扫码 | 115 开放平台设备码授权，token 自动续期 |
| 多直链源 | 115 开放平台 / CloudDrive2 直链 / AList(OpenList) / 自定义 HTTP |
| WebUI | 状态、媒体库、扫描记录、事件流水、日志、路径映射、扫码登录；深/浅色 + 手机版 |

---

## 工作原理

```text
外网 Plex 客户端
      │  http(s)://NAS:32500
      ▼
┌──────────────────────────────────────────────┐
│ plex302  (nginx + njs)                       │
│                                              │
│  /library/parts/*  ──► resolver ──► 302 ─────┼──► 115 / AList / CD2 直链
│  universal/start   ──► resolver ──► 302 ─────┤
│  其它请求           ──► Plex :32400           │   内网播放 / 转码 / 字幕 / WebUI
└──────────────────────────────────────────────┘

Plex 媒体库 ──► CD2 挂载的真实文件（扫描入库，无 strm）

CMS 整理完成 ──► 115 里出现新文件 / 目录变化
                     │
                     ├─ 115 操作事件（主，游标增量 + 静置合并 + 延迟复核）
                     ├─ Emby 兼容入口 / Webhook（CMS 主动通知，最快）
                     └─ CD2 目录监听（兜底）
                              ▼
                 CD2 刷新目录 ──► Plex refresh?path= ──► 只扫这个目录

每天 04:30 ──► Plex refresh（不带 path）──► 所有媒体库整库扫一遍（兜底）
```

302 的判定顺序：

1. 请求来自**非局域网**（或打开「局域网也 302」）
2. 文件路径命中**路径映射**
3. 直链 Provider 能返回 URL
4. 客户端确实在**直接播放**（`/library/parts`，或 `start?directPlay=1`）

任何一条不满足就回源 Plex，不会把不能播的请求硬跳走。

---

## 前置条件

- Plex Media Server，能拿到 `X-Plex-Token`
- CloudDrive2 已挂载云盘，且 **Plex 能看到这个挂载路径**
- 115 账号（115 直链 + 操作事件都需要；事件只要网页 Cookie）
- Docker / docker compose
- 建议宿主 Linux，compose 使用 `network_mode: host`

> Plex 和 plex302 可以不在同一台机器，只要 plex302 能访问 Plex 的 32400，且两边看到的媒体路径能通过「路径映射」对上。

---

## 快速开始

### 1. docker-compose.yml

```yaml
services:
  plex302:
    image: ghcr.io/celestial520/plex302:latest
    # build: .                        # 想自己编译就注释掉上面这行、打开这行
    container_name: plex302
    network_mode: host               # 必须 host：拿客户端真实 IP + 直连 CD2 gRPC
    restart: unless-stopped
    environment:
      TZ: Asia/Shanghai
      NGINX_PORT: 32500              # Plex 客户端以后连这个端口
      WEBUI_PORT: 999                # 管理页面
    volumes:
      - ./config:/config
      - ./log:/var/log/nginx
      # 必须和 Plex 里看到的 CD2 路径一致；只做路径映射，不读文件内容
      - /mnt/cd2:/mnt/cd2
```

```bash
mkdir -p /volume1/docker/plex302 && cd /volume1/docker/plex302
# 保存上面的 compose
docker compose up -d
```

打开 `http://NAS地址:999`，默认账号 **admin / admin**，登录后先到「设置 → 安全」改密码。

### 2. 配四件事

#### ① Plex 连接

| 配置 | 填什么 |
|---|---|
| Plex 地址 | host 网络通常是 `http://127.0.0.1:32400` |
| X-Plex-Token | Plex Web 打开任意影片 → 查看网页源码 → 搜 `X-Plex-Token` |

点「测试 Plex」，能看到服务器名和媒体库数量即可。

#### ② CloudDrive2

| 配置 | 填什么 |
|---|---|
| gRPC 地址 | 默认 `127.0.0.1:19798` |
| API Token | CD2 后台 → 设置 → API Token（**不是登录密码**） |

点「测试 CD2」。这一步只用于**刷新目录缓存**，不填也能扫，但 CMS 刚整理完的新文件可能因为 CD2 缓存看不到。

#### ③ 115 授权

两条独立的链路，用途不同：

| 配置 | 用途 |
|---|---|
| 扫码登录 115（开放平台） | 取**直链**（播放时 302 用） |
| 115 网页 Cookie | 取**操作事件**（入库用），开放平台没有这个接口 |

Cookie 获取：浏览器登录 [115.com](https://115.com) → F12 → Network → 随便点一个 `webapi.115.com` 请求 → 复制 Request Headers 里的 `Cookie` 整行（`UID=...; CID=...; SEID=...`）。

填完点「测试 Cookie」，会同时验证「最近文件」和「操作事件」两个接口，能返回条数就通了。Cookie 失效时 115 会返回 405，这里会直接提示。

> 默认 `client_id` 是 MoviePilot 的公开应用，所以扫码页显示「MoviePilot」，这是正常的；也可以到 [open.115.com](https://open.115.com/) 申请自己的应用后替换。

#### ④ 路径映射

把 **Plex 看到的路径** 映射成 **云盘路径**（给 115/AList）和 **CD2 内部路径**（给刷新缓存）。

例：

| Plex 里看到 | 115 网盘里 | CD2 内部 |
|---|---|---|
| `/mnt/cd2/115/电影/沙丘2 (2024)/沙丘2.mkv` | `/电影/沙丘2 (2024)/沙丘2.mkv` | `/115/电影/沙丘2 (2024)/沙丘2.mkv` |

对应填：

| Plex 前缀 | 云盘前缀 | CD2 前缀 |
|---|---|---|
| `/mnt/cd2/115` | `/` | `/115` |

- **云盘前缀**：115 根目录对应 `/`；如果 115 里是 `/媒体/电影`，就填 `/媒体`
- **CD2 前缀**：CD2 里这个挂载点叫什么就填什么；不知道可以留空，只跳过缓存刷新
- 多条映射按顺序匹配，取最长前缀

点「测试直链」，填一个真实文件路径，能返回 URL 就通了（会自动复制）。

### 3. Plex 侧设置

1. **添加媒体库**：路径选 CD2 挂载路径，例如 `/mnt/cd2/115/电影`、`/mnt/cd2/115/电视剧`
2. **关闭自动扫描**：设置 → 媒体库 → 取消「自动扫描我的媒体库」；交给 plex302 做局部扫描
3. **减少云盘读取**（强烈建议）：
   - 关闭「生成视频预览缩略图」
   - 关闭「生成片头/片尾标记」
   - 关闭「扫描时生成章节缩略图」
4. **网络**：
   - 设置 → 网络 → 安全连接选 **首选（Preferred）**，不要选「必需」
   - 「自定义服务器访问 URL」填 `http://NAS地址:32500`；如果前面有 HTTPS 反代，就填反代地址
5. **客户端**：
   - 远程画质设为 **原画 / Original**
   - 关闭「自动调整质量」
   - 连接端口用 **32500**，不是 32400

> Plex 的服务器发现仍可能把 32400 广播给客户端。最稳的是在客户端里手动添加 `NAS:32500`，或在 Plex 设置里填「自定义服务器访问 URL」。

---

## 从 plex-demo 迁移

之前用过 `registry.cn-hangzhou.aliyuncs.com/nanfeng_super/plex-demo` 那套 compose 的，换镜像即可，注意这几处差异：

| 旧写法 | 新写法 | 说明 |
|---|---|---|
| `./config:/shared-config` | `./config:/config` | 配置目录变了，且数据结构不兼容，建议重新配一遍（几分钟） |
| `PUID` / `PGID` | 不需要 | 容器内以 root 运行，只读写 `./config` 和 `/var/log/nginx` |
| `- ./strm:/strm` | 不需要 | 不生成 strm，这一行可以删掉 |
| `NGINX_PORT` / `WEBUI_PORT` / `TZ` | 保持不变 | 同名环境变量依旧生效 |

端口、host 网络、`./log:/var/log/nginx` 都不用改。

---

## 局部扫描是怎么做的

不是“扫全库”，是 Plex 官方支持的按路径扫描：

```http
GET /library/sections/<sectionId>/refresh?path=<URL 编码后的目录>&X-Plex-Token=<token>
```

plex302 会：

1. 用路径最长前缀匹配到媒体库
2. 通过 CD2 gRPC 从挂载根到目标目录逐级 `GetSubFiles(forceRefresh=true)`
3. 调 Plex 的 `refresh?path=`
4. 记录到「扫描记录」，失败会写原因

单集/单部电影入库只扫它所在的剧集/电影目录，不会碰其它文件。

---

## 全量同步（兜底）

把 **所有媒体库整库扫一遍**，走的是同一个官方接口，只是不带 `path`：

```http
GET /library/sections/<sectionId>/refresh?X-Plex-Token=<token>
```

| 触发方式 | 说明 |
|---|---|
| 概览页「全量同步」按钮 | 手动跑一次，页面会显示 `已完成 x/y` |
| 每天定时 | 默认 `04:30`，到点自动跑；同一个媒体库不会重复扫 |
| 启动时 | 默认**关**，避免一装好就把整库拉一遍，可在设置里打开 |
| `POST /api/sync/full` | 给脚本用 |
| `POST /emby/Library/Refresh` | CMS 的 Emby 对接会打到这个接口 |

整库扫描很吃 CPU 和磁盘 IO（还要逐个文件读元数据），建议放在凌晨；它只是**兜底**，日常入库靠下面的增量链路。

---

## CMS 整理完自动入库

### 主链路：115 操作事件（默认开启，推荐）

CMS 整理 = 在 115 里移动 / 改名 / 复制 / 上传文件，这些动作都会出现在 **115 App 的「最近操作」** 里。
plex302 直接读这条流水（`GET /behavior/detail?limit&offset`），游标增量消费：

```text
拉一页事件（按 id 倒序）
   └─ 遇到 id <= 游标 立刻停，只处理新的
        └─ 过滤：浏览/收藏/标签类事件直接忽略
             └─ 反查 parent_id -> 完整云盘路径 -> 路径映射 -> Plex 路径
                  └─ 推导要扫的目录（电影目录 / 剧集目录 / 删除时扫父目录）
                       └─ 进入「静置队列」，安静下来才真正入队扫描
```

关键的三层防漏：

| 机制 | 默认值 | 作用 |
|---|---|---|
| **静置合并** | 120 秒，单目录最多等 900 秒 | 同一目录每来一个新事件就把倒计时往后推；CMS 连着移动 10 个文件，只在最后扫一次，不会扫到一半 |
| **扫完复核** | 5 分钟后 | 接住“扫描时文件还没写完 / 字幕还没到位”的情况，到点强制再扫一次 |
| **每日全量** | 04:30 | 上面两层都失效时（Cookie 挂了、事件接口抽风），整库扫一遍补回来 |

另外还有两个保险：**115 最近文件兜底**（事件接口偶发空返回时用 `/files/recent` 补一刀）和 **Plex 不可用时不推进游标**（免得事件被白白消费掉）。

设置项在「设置 → 扫描与自动入库」：

- 事件监听开关、拉取间隔（15 秒 ~ 1 小时，点输入框直接选常用值）
- 首次运行：默认**只记录游标**，不补扫历史（打开「首次也扫描」会按现有事件补一遍）
- 静置时间 / 最长等待 / 是否复核 / 复核延迟

### 即时链路：CMS 主动通知（可选，最快）

CMS（CloudMediaSynC）内置的是 Emby 对接，所以给了一个 **Emby 兼容入口**，不用改 CMS 代码：

| CMS 里填 | 值 |
|---|---|
| `EMBY_HOST_PORT` | `http://NAS:999`（plex302 的 WebUI 端口） |
| `EMBY_API_KEY` | 设置页「CMS 对接」里显示的 `emby_token` |

支持这几个接口：

| 接口 | 行为 |
|---|---|
| `GET /emby/System/Info` | 返回假服务器信息，CMS 用来探活 |
| `POST /emby/Library/Refresh` | 触发**全量同步** |
| `POST /emby/Library/Sections/{id}/Refresh` | 按库全量扫 |
| `POST /emby/Items/{id}/Refresh` | Emby 的 item id 对不上 Plex 路径，10 分钟内最多触发一次全量（避免刷屏） |

也有通用 Webhook，给脚本或其它工具用：

```bash
# 最简：GET，path 用云盘路径或 Plex 路径都行
curl "http://NAS:999/api/hook?token=p302_xxx&path=/电影/沙丘2 (2024)"

# POST JSON
curl -X POST "http://NAS:999/api/hook?token=p302_xxx" \
  -H "Content-Type: application/json" \
  -d '{"path":"/电视剧/测试剧/Season 01/S01E01.mkv"}'

# 直接要求全量同步
curl -X POST "http://NAS:999/api/hook?token=p302_xxx&full=1"
```

- 传文件路径会自动推导到电影/剧集目录
- 云盘路径会自动反查成 Plex 路径（依赖路径映射）
- 支持 `path` / `paths` / `file` / `file_path` / `dir` / `save_path` 等字段
- 一次最多处理 20 条，90 秒内重复路径自动去重

### 兜底链路：CD2 目录监听（默认开启）

「设置 → 扫描与自动入库」打开「目录监听」，并在「媒体库」页勾选要监听的库。

- 按 `scan.interval`（默认 180 秒）轮询 CD2 挂载目录
- **第一次运行只建索引，不触发扫描**
- 发现新增/变化的视频文件后按规则推导目录：
  - 电影：`/电影/片名 (年)/片.mkv` → 扫 `/电影/片名 (年)`
  - 剧集：`/电视剧/剧名/Season 01/S01E01.mkv` → 扫 `/电视剧/剧名`
- 文件修改时间小于 `settle_seconds`（默认 90 秒）先跳过
- `.part` / `.!qB` / `.crdownload` 等临时文件自动忽略

### 三条链路的关系

```text
CMS 整理完
   ├─ ① Emby 接口 / Webhook  ──► 立即局部扫描（最快，需要 CMS 支持）
   ├─ ② 115 操作事件         ──► 静置后局部扫描（默认，最稳）
   ├─ ③ CD2 目录监听         ──► 轮询兜底（不需要任何 Cookie）
   └─ ④ 每天 04:30 全量      ──► 整库扫一遍（终极兜底）
```

它们**同时生效**，多扫几次没有副作用（Plex 对同一目录重复 refresh 是幂等的），所以不用纠结选哪个，全开就行。

---

## 会不会漏？三个常见担心的答案

**Q：只监控 115 最近操作，会不会漏事件？**

会，但概率很低，而且有兜底：

- 事件接口偶发返回空列表 → 自动重试一次，还不行这轮就**不推进游标**，下轮重新拉
- 事件接口短时间内超过 `limit × max_pages` 条（默认 500 条）→ 分页拉取，够用；真超了也不会丢，游标停在上次位置
- Plex 挂了 / Cookie 失效 → 不推进游标，恢复后从断点继续
- 实在漏了（比如事件被 115 自己清理）→ 每天全量同步补回来

**Q：间隔扫描会不会正好扫在 CMS 整理中途，导致后面漏入库？**

这正是把“定时轮询目录”换成“事件驱动 + 静置”的原因：

- 定时扫描的漏法是：`T` 时刻扫了一遍 → CMS 在 `T+10s` 又落了一个文件 → 下次扫描只认“比上次新”的文件，这个就漏了
- 现在不是定时扫，而是**有事件才扫**；而且同一目录**每来一个新事件就把静置时间往后推**（默认 120 秒），CMS 连续动作期间根本不会触发扫描
- 扫描之后还有**延迟复核**（默认 5 分钟），接住晚落盘的字幕/花絮
- 最后还有每日全量

换句话说：整理中途不会扫，整理完才扫，扫完还会再看一眼。

**Q：CMS 整理完成后怎么通知 plex302？**

两种都行，建议都配：

1. **CMS 主动通知**（最即时）：CMS 里把 Emby 地址指向 plex302（`EMBY_HOST_PORT` + `EMBY_API_KEY`），整理完 CMS 自己会发 refresh
2. **什么都不配**（也能用）：115 操作事件会接住，静置 2 分钟后自动局部扫描

前者快（秒级），后者稳（不依赖 CMS 配置）。两个同时开也没问题。

---

## 企业微信通知

和 [115 秒传助手](https://github.com/celestial520/115-rapid-upload) 用同一套企业微信应用消息，配置项完全一样，方便统一维护。

设置 → 通知（企业微信）：

| 配置 | 填什么 |
|---|---|
| CorpID | 企业微信管理后台 → 我的企业 → 企业 ID |
| Secret | 应用管理 → 自建应用 → Secret |
| AgentID | 同一个自建应用页面的 AgentId |
| 接收人 | `@all` 或成员账号 ID，多个账号用竖线分隔 |
| 代理地址 | 可选，形如 `https://qyapi.weixin.qq.com` 的反代；留空走官方 |
| 通知时机 | 入库完成 / 全量完成 / 出错告警，三个独立开关 |

消息长这样：

```text
Plex302 · 入库完成
已刷新 3 个目录
· 沙丘2 (2024)（电影）
· 测试剧（电视剧）
· 流浪地球3（电影）
```

- 一轮事件触发多个目录时，**15 秒内的扫描结果汇总成一条**，不会一次整理发十几条
- 出错告警同类 30 分钟内只发一次，不会刷屏
- 配好点「发送测试消息」立即验证

---

## WebUI

- **主题**：顶栏「外观」按钮循环切换 **自动 / 浅色 / 深色**，跟随系统时不写死，系统换主题页面跟着变；选择存在浏览器本地，刷新不闪白
- **手机版**：宽度 ≤ 900px 时侧栏变顶栏、底部固定导航（概览 / 媒体库 / 记录 / 设置）、表格自动卡片化、弹窗改成底部弹出，避开 iPhone 安全区
- **间隔选择器**：所有“秒/分钟”输入框点一下弹出常用值（15 秒、1 分钟、2 分钟、5 分钟…），也能直接手输
- **事件流水**：概览页直接看最近的 115 操作事件（类型、文件名、映射到的目录、是否已扫）

---

## 配置说明

| 配置 | 默认 | 说明 |
|---|---|---|
| `provider.type` | `115` | `115` / `cd2` / `alist` / `custom` / `none` |
| `provider.ua_mode` | `client` | 115 直链会绑定请求 UA，建议跟随客户端 |
| `provider.force_https` | `true` | 把 115 返回的 http 直链改成 https |
| `mounts[].plex_prefix` | `/mnt/cd2` | Plex 看到的路径前缀 |
| `mounts[].cloud_prefix` | `/` | 云盘路径前缀（115/AList 用） |
| `mounts[].cd2_prefix` | 空 | CD2 内部路径前缀（刷新缓存用，可空） |
| `scan.interval` | `180` | 目录轮询间隔（秒） |
| `scan.settle_seconds` | `90` | 目录监听里文件静置多久才处理 |
| `scan.max_depth` | `4` | 目录监听最大深度 |
| `scan.min_size_mb` | `20` | 小于这个大小的视频忽略 |
| `scan.first_run_scan` | `false` | 目录监听首次索引时是否也扫描 |
| `scan.watch_sections` | `[]` | 空 = 全部媒体库；`__none__` = 全部关闭 |
| `scan.full_enabled` | `true` | 开启每日全量同步 |
| `scan.full_time` | `04:30` | 全量同步时间（`HH:MM`） |
| `scan.full_on_start` | `false` | 启动时是否立刻跑一次全量 |
| `events.enabled` | `true` | 115 操作事件监听（增量入库主通道） |
| `events.interval` | `60` | 事件拉取间隔（秒，15~3600） |
| `events.limit` | `100` | 单页条数 |
| `events.max_pages` | `5` | 一轮最多翻几页 |
| `events.first_scan` | `false` | 首次运行是否补扫历史事件 |
| `events.settle_seconds` | `120` | 目录静置多久再扫（来新事件会顺延） |
| `events.max_wait_seconds` | `900` | 单目录最长等待，避免一直有事件就永远不扫 |
| `events.verify_enabled` | `true` | 扫完是否延迟复核一次 |
| `events.verify_delay` | `300` | 复核延迟（秒） |
| `events.recent_fallback` | `true` | 是否同时用「最近文件」兜底 |
| `notify.wecom_enabled` | `false` | 开启企业微信通知 |
| `notify.wecom_corpid` / `wecom_secret` / `wecom_agentid` | 空 | 企业微信自建应用三件套 |
| `notify.wecom_touser` | `@all` | 接收人 |
| `notify.wecom_proxy` | 空 | 企业微信 API 反代地址（可选） |
| `notify.on_scan` / `on_full` / `on_error` | `true` | 三类通知开关 |
| `security.emby_token` | 自动生成 | CMS 里填的 `EMBY_API_KEY` |
| `security.webhook_token` | 自动生成 | Webhook 地址里的 token |
| `u115.cookie` | 空 | 115 网页 Cookie（操作事件 / 最近文件） |
| `u115.recent_enabled` | `true` | 「最近文件」兜底开关 |
| `u115.recent_interval` | `120` | 兜底拉取间隔（秒） |
| `u115.recent_limit` | `50` | 兜底每次拉多少条 |
| `u115.recent_first_scan` | `false` | 兜底首次是否也扫描 |
| `server.lan_cidrs` | 常见内网段 | 这些 IP 默认回源 Plex |
| `server.redirect_lan` | `false` | 内网也 302 |
| `advanced.force_direct_play` | `true` | 改写 Plex decision，强制 directPlay |
| `advanced.url_cache_ttl` | `1800` | 直链缓存秒数（不会超过 Provider 返回的有效期） |

---

## 常见问题

**Q：怎么确认 302 生效了？**

在 NAS 上执行（把路径换成真实的 part 路径）：

```bash
curl -I -H "X-Forwarded-For: 1.2.3.4" \
  "http://127.0.0.1:32500/library/parts/12345/1700000000/file.mkv?X-Plex-Token=xxx"
```

看到 `HTTP/1.1 302`、`X-Plex302: redirect`、`Location: https://...` 就对了。
`X-Plex302: proxy` 会带原因，比如「局域网客户端」「不是播放请求」「直链失败」。

**Q：外网播放还是走 NAS 中转？**

按顺序排查：

1. 客户端远程画质是不是「原画」？Plex 决定转码就不会请求 `/library/parts`
2. Plex 设置 → 网络 → 安全连接是不是「必需」？改成「首选」
3. 客户端连的是不是 32500？
4. 路径映射的 `plex_prefix` 是否和 Plex 报的路径完全一致（区分大小写）
5. 「设置 → 直链方式 → 测试直链」能不能出 URL
6. 看日志里的 `[302]` 和 `[302] 直链获取失败`

**Q：115 返回 403 / 链接打不开？**

- `ua_mode` 用「跟随客户端」，不要用固定 UA
- 关掉「强制 https」再试（少数链接签名和 scheme 绑定）
- 重新扫码登录，确认 token 没过期
- 115 对并发/风控敏感，短时间内大量起播可能限流

**Q：Plex 里文件显示不可用 / 扫不到？**

- CD2 挂载在 Plex 容器里是否可见？路径是否和 plex302 里配的一致？
- CD2 API Token 是否有效（影响缓存刷新，不影响 Plex 直接扫）
- 用「媒体库 → 扫描」手动扫一次，看「扫描记录」的报错
- 实在不行点「全量同步」整库扫一遍
- 第一次入库大库建议先在 Plex 里手动全量扫一次，后续再用局部扫描

**Q：CMS 整理完没自动扫？**

按链路逐个查：

1. 「设置 → 扫描与自动入库」里事件监听是否开着，Cookie 是否有效（点「测试 Cookie」看事件条数）
2. 「概览 → 115 最近操作」里能不能看到刚才那次整理的事件
3. 事件看到了但没扫：看「待扫目录」数量，静置时间没到不会扫（默认 120 秒）
4. 扫描记录里 source 是 `115event` 才说明走的是事件链路；`cms-emby` 是 CMS 主动通知；`115recent` 是兜底
5. 都没有就直接点「全量同步」

**Q：全量同步很慢 / 卡住？**

整库扫描本来就是重活（Plex 要逐文件读元数据，云盘还要拉取）。看「全量同步」面板的 `已完成 x/y` 和当前库名，慢是正常的；失败会在扫描记录里写原因，通常是 CD2 挂了或路径不对。

**Q：企业微信收不到消息？**

- 先点「发送测试消息」，报错会直接显示（常见是 AgentID 不是数字、Secret 填成了应用的 Secret 之外的、IP 不在可信 IP 列表）
- 确认企业微信后台该应用的「可见范围」包含接收人
- 走代理的话 `notify.wecom_proxy` 只填协议+域名，不要带 `/cgi-bin`

**Q：Plex Web 播放不能 302？**

Plex Web 大量使用 HLS/转码分片（`/video/:/transcode/universal/session`），这类请求必须回源，302 会破坏播放。
这个工具的目标是 **Direct Play**：Infuse、Plex Android TV、Apple TV、手机 Plex App 等在「原画」模式下会直接请求 `/library/parts`。

**Q：会不会修改/删除我的文件？**

不会。只读 Plex API、115 网页/开放平台接口、CD2 gRPC；不写媒体目录，不生成 strm，不转存。

**Q：支持哪些网盘？**

- 115：走 115 开放平台，最完整
- 其它：CD2 直链（需要 CD2 直链权限）或 AList/OpenList（`/api/fs/get`）
- 自定义 HTTP：任何能返回 `{"url":"https://..."}` 的服务

**Q：ARM / 群晖 / 绿联能跑吗？**

镜像构建了 `linux/amd64` + `linux/arm64`。compose 用 host 网络，绿联/群晖的 Docker 都支持。

---

## 数据目录

| 路径 | 内容 |
|---|---|
| `/config/config.json` | 所有设置（含密文密码、webhook / emby token） |
| `/config/115_token.json` | 115 access/refresh token |
| `/config/state.db` | 扫描记录、监听索引、115 事件流水与游标、302 统计 |
| `/config/logs/app.log` | 运行日志，5MB × 3 轮转 |

`state.db` 里的 `poll_state` 表存了事件游标（`115_event_cursor`）和全量同步日期（`full_last_date`），
删掉它会让程序重新从“当前时刻”开始记事件（不会补扫历史）。

迁移时把 `./config` 整个拷走即可。

---

## 安全

- WebUI 默认 `admin/admin`，首次登录必须改
- 不要把 999 端口直接暴露到公网；需要远程访问请走 VPN 或加一层认证反代
- Webhook / Emby 入口都自带 token，泄露后到设置页重新生成（改 `config.json` 里的 `security.webhook_token` / `security.emby_token` 后重启）
- 115 Cookie 和企业微信 Secret 存在 `config` 目录，注意备份权限

---

## 构建

```bash
git clone https://github.com/celestial520/plex302
cd plex302
docker compose up -d --build
```

镜像：`ghcr.io/celestial520/plex302:latest`（amd64 + arm64）。
推 `main` 或打 `v*` tag 会自动构建（`.github/workflows/docker.yml`）：先跑冒烟测试，通过后才推镜像。冒烟覆盖：

- 容器能不能起来、nginx 和 njs 是否正常
- `/library/parts` 外网 302 / 内网回源
- 115 操作事件 → 游标增量 → 局部入队 → 扫描记录带 `115event`
- 全量同步扫完所有媒体库
- Emby 兼容入口（无 token 401 / 有 token 200 / 刷新 204）

国内构建加速：

```bash
docker build --build-arg APT_MIRROR=mirrors.aliyun.com -t plex302 .
```

### 项目结构

```text
app/                 FastAPI 后端
  main.py            路由、鉴权中间件、Webhook、Emby 兼容入口
  plex.py            Plex API（局部/整库扫描、parts 查路径、decision）
  cd2.py             CloudDrive2 gRPC（刷缓存、取直链）
  u115.py            115 开放平台（设备码授权、直链、token 续期）
  u115web.py         115 网页版 Cookie 接口（最近文件、目录路径）
  u115events.py      115 生活事件接口（App 里的「最近操作」）
  events.py          事件监听：游标增量 + 静置合并 + 延迟复核
  scanner.py         扫描队列（局部扫描 / 全量同步）
  watcher.py         CD2 目录轮询，发现新文件后推导要扫的目录
  scheduler.py       每天定时全量同步
  notify.py          企业微信应用消息（汇总、全量、告警）
  providers.py       直链来源：115 / CD2 / AList / 自定义
  resolver.py        路径映射 + 局域网判断 + 直链缓存
nginx/
  plex302.js         njs：拦截 /library/parts、decision 改写、302 或回源
  plex302.conf.template  站点模板（端口由 entrypoint 注入）
tests/mock_plex.py   假 Plex + 假 CDN，本地联调用
tests/mock_115.py    假 115 webapi（最近文件 + 操作事件），测入库链路
```

---

## 和 plex-demo 的关系

最先那版是按 `plex-demo` 的思路做的（nginx 拦截 → 本地 API 取直链 → 302 或回源），
但它是 **strm 工作流**，本项目是 **Plex 直接扫 CD2 真实文件**，路线不同，功能不是一一对应：

| 能力 | plex-demo | 本项目 |
|---|---|---|
| 外网 302 / 内网回源 | ✅ | ✅ |
| Plex 局部刷新 | ✅ | ✅ |
| 全量同步（整库扫） | ❌ | ✅ 手动 + 每日定时 |
| 115 开放平台扫码 | ✅ | ✅ |
| 115 操作事件增量入库 | ❌ | ✅ 游标 + 静置 + 复核 |
| 目录监听自动入库 | 监控 strm | ✅ 监控 CD2 真实目录 |
| strm 生成 / 监控 | ✅ | ❌ 不需要，Plex 直接扫真实文件 |
| 115 秒传 | ✅ | ❌ 与入库无关 |
| 自动重命名 / 写 NFO | ✅ | ❌ 建议交给 CMS / tinyMediaManager，避免误改 |
| 微信通知 | ✅ | ✅ 企业微信应用消息 |
| WebUI | Vue | 原生 JS（无构建，单容器，双主题） |

如果你确实需要 strm 生成、秒传、自动重命名这些，我可以按需加，但它们和“不依赖 strm、不让 NAS 中转”
的路线是冲突的，默认没做。

---

## 已知限制

- **只对 Direct Play 生效**。客户端坚持转码时，302 没有意义，会回源 Plex。
- 115 直链和请求 UA 绑定；`ua_mode=client` 是默认值，换客户端/换 UA 可能 403。
- CD2 缓存刷新是“尽力而为”，失败只记日志，不阻塞 Plex 扫描。
- 事件接口依赖 115 网页 Cookie，Cookie 会过期（一般几周），失效时通知里会提醒。
- 事件接口偶发返回空列表，程序会重试一次并保留游标，但极端情况下仍可能漏单条——每日全量兜底。
- 目录监听是轮询，不是 inotify；大库首次索引会慢一些，之后只处理变化。
- 本项目不内置任何账号、Cookie 或破解逻辑，只调用你已有的 Plex / 115 / CD2 / AList 接口。

---

## 免责声明

仅供个人媒体库的技术研究与自用。请遵守 Plex、115、CloudDrive2 的服务条款；不要用于传播侵权内容或商业用途。因使用本工具导致的账号风控、数据或服务中断，作者不承担责任。
