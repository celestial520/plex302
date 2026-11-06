# Plex302 · Plex 云盘 302 直链助手

[![build](https://github.com/celestial520/plex302/actions/workflows/docker.yml/badge.svg)](https://github.com/celestial520/plex302/actions/workflows/docker.yml)
[![image](https://img.shields.io/badge/ghcr.io-plex302-blue)](https://github.com/celestial520/plex302/pkgs/container/plex302)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Plex 通过 **CD2 挂载**直接扫描云盘里的真实文件，**不生成 strm**；播放时把 `/library/parts` 请求 **302** 到 115 直链，NAS 不中转视频流量；CMS 整理完靠 **115 操作事件**自动局部入库，每天再用 **全量同步** 兜底。

> 一句话：一个 115 Cookie 搞定直链和入库，Plex 只负责刮削和播放决策。

---

## v1.5.0 更新

| 变化 | 说明 |
|---|---|
| **移除自建整理** | 设置里的「识别与整理（TMDB / AI）」整段删除，整理交回 CMS / 其它工具；只保留「链接入库」 |
| **企业微信不能再触发整理** | 只保留发链接和「状态」，`整理` / `预演` 指令下线 |
| **移除 CMS 对接设置项** | 设置页不再出现「CMS 对接」；`/emby` 和 `/api/hook` 接口保留，地址见文档 |
| **企业微信通知改版** | 改成一字段一行（企业ID / 应用Secret / 应用ID / 接收人 / API代理 / 状态），去掉示例文字 |
| **115 请求间隔统一到 2.05 秒** | 和 CMS 默认的 2.0 秒对齐（只慢不快）：事件反查、入库、目录操作全部走这个节奏；直链有缓存，走独立通道 |
| **compose 不再挂 CD2 路径** | 和 plex-demo 一样，挂载路径全部在 WebUI 的「路径映射」里配 |

从 1.4.x 升级：旧配置里的 `organize` / `metadata` / `ai` 会被自动忽略，不用手动清。

---

## v1.4.0 更新

> 其中「自建整理 / AI 兜底 / 出站代理 / 企业微信整理指令」已在 1.5.0 移除，仅作历史记录。

| 变化 | 说明 |
|---|---|
| **自建整理（可以不要 CMS）** | 扫描待整理目录 → TMDB 识别 → 在 115 上**真实移动 + 重命名** → 通知 Plex 扫库；不生成 strm、不下载 |
| **默认只预演** | `dry_run=true` 时只出「会改成什么样」的清单，一个文件都不动；确认无误再关掉 |
| **AI 兜底识别** | TMDB 认不出时，把文件名发给 DeepSeek（或任意 OpenAI 兼容接口），只在失败时调用 |
| **链接入库** | 企业微信里直接发 115 分享 / 磁力 / ed2k，自动转存/离线到待整理目录，再自动整理 |
| **出站代理** | TMDB / AI 可单独走 HTTP 代理；115 接口保持直连，避免风控 |
| **企业微信收消息** | 自建应用回调（签名 + AES 解密），支持「整理」「预演」「状态」指令，结果用应用消息回推 |

从 1.3.x 升级只需换镜像，旧配置自动补齐；整理默认关闭，不会动你现有文件。

---

## v1.3.0 更新

| 变化 | 说明 |
|---|---|
| **去掉 115 开放平台** | 直链改走网页版 `GET /files/download?pick_code=`，和事件、目录反查共用同一个 Cookie；不用再扫码、不用管 client_id / token 续期 |
| **局域网也能 302** | 「设置 → 网络与安全 → 局域网也 302」打开后，电视/盒子在内网也直接从 115 拉流，NAS 完全不上行 |
| **时间设置改成数字 + 单位** | 不再给固定档位：填多少分钟 / 多少小时都行（和 115 秒传助手一样） |
| **风控参数不再让你调** | 静置 90 秒、单目录最长 10 分钟、扫完 3 分钟复核、每次 100 条、事件间 0.6 秒——全部内置 |
| **CD2 API Token 变可选** | 不填就跳过缓存刷新，Plex 照样扫得到；填了只是更快看到刚整理的文件 |
| **CD2 目录监听默认关闭** | 它要遍历挂载目录 = 持续打 115 接口，有风控风险；日常用事件就够了 |
| **UI 重新分区** | 概览把「115 增量入库」和「CD2 目录监听」拆成两块；设置页「外观」提到最上面 |
| **企业微信通知** | 增加「增量入库完成」通知（目录 + 媒体库 + 来源），和全量、错误通知分开控制 |

从 1.2.x 升级只需换镜像，`config.json` 会自动补齐 / 收敛字段。

---

## 功能

| 能力 | 说明 |
|---|---|
| 不依赖 strm | Plex 直接扫描 CD2 挂载目录里的真实文件；不是 strm 占位 |
| 外网 302 | 拦截 `/library/parts/*` 和 `universal/start`，返回 115 直链 |
| 内网可 302 | 一个开关：内网也直接连云盘，NAS 不上行；关掉则回源 Plex |
| 局部扫描 | 调 Plex 官方 `GET /library/sections/<id>/refresh?path=<目录>`，不扫全库 |
| 全量同步 | 调 `refresh` 不带 path，整库扫一遍；支持每天定时 + 手动一键 |
| 115 操作事件 | 读 115「最近操作」流水，游标增量，CMS 一落盘就能发现 |
| CD2 缓存刷新 | 扫描前用 CD2 gRPC 刷新目录（Token 可选），避免新文件不可见 |
| Emby 兼容入口 | `/emby/Library/Refresh`，给 CMS / 其它只支持 Emby 通知的工具调用 |
| 链接入库 | 115 分享转存 / 磁力 / ed2k 离线，企业微信或 WebUI 直接发 |
| 企业微信通知 | 增量入库、全量结果、错误告警，同 115 秒传助手一套配置 |
| WebUI | 状态、媒体库、扫描记录、事件流水、日志、路径映射；深/浅色 + 手机版 |

---

## 工作原理

```text
外网 / 内网 Plex 客户端
      │  http(s)://NAS:32500
      ▼
┌──────────────────────────────────────────────┐
│ plex302  (nginx + njs)                       │
│                                              │
│  /library/parts/*  ──► resolver ──► 302 ─────┼──► 115 直链（Cookie 换 pick_code）
│  universal/start   ──► resolver ──► 302 ─────┤
│  其它请求           ──► Plex :32400           │   转码 / 字幕 / WebUI
└──────────────────────────────────────────────┘

Plex 媒体库 ──► CD2 挂载的真实文件（扫描入库，无 strm）

CMS 整理完成 ──► 115 里出现新文件 / 目录变化
                     │
                     ├─ 115 操作事件（主，游标增量 + 静置合并 + 延迟复核）
                     ├─ Emby 兼容入口 / Webhook（CMS 主动通知，仅在开 strm 时可用）
                     └─ CD2 目录监听（兜底，默认关）
                              ▼
                 CD2 刷新目录 ──► Plex refresh?path= ──► 只扫这个目录

每天 04:30 ──► Plex refresh（不带 path）──► 所有媒体库整库扫一遍（兜底）
```

302 的判定顺序：

1. 请求来自**非局域网**（或打开了「局域网也 302」）
2. 文件路径命中**路径映射**
3. 用 Cookie 把路径换成 `pick_code`，再换到直链
4. 客户端确实在**直接播放**（`/library/parts`，或 `start?directPlay=1`）

任何一条不满足就回源 Plex，不会把不能播的请求硬跳走。

---

## 前置条件

- Plex Media Server，能拿到 `X-Plex-Token`
- CloudDrive2 已挂载 115，且 **Plex 能看到这个挂载路径**
- 115 网页 Cookie（直链 + 事件都要）
- Docker / docker compose，建议宿主 Linux + `network_mode: host`

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
```

```bash
mkdir -p /volume1/docker/plex302 && cd /volume1/docker/plex302
docker compose up -d
```

打开 `http://NAS地址:999`，默认 **admin / admin**，登录后先到「设置 → 网络与安全」改密码。

### 2. 配三件事

#### ① Plex 连接

| 配置 | 填什么 |
|---|---|
| Plex 地址 | host 网络通常是 `http://127.0.0.1:32400` |
| X-Plex-Token | Plex Web 打开任意影片 → 查看网页源码 → 搜 `X-Plex-Token` |

点「测试 Plex」，能看到服务器名和媒体库数量即可。

#### ② 115 Cookie（直链和入库全靠它）

浏览器登录 [115.com](https://115.com) → F12 → Network → 点任意一个 `webapi.115.com` 请求 → 复制 Request Headers 里的 `Cookie` 整行（`UID=...; CID=...; SEID=...`），粘到「设置 → 115（Cookie）」。

点「测试 Cookie」，会一次验证四件事：

1. 最近文件接口（`/files/recent`）
2. 操作事件接口（`/behavior/detail`）
3. 目录反查（`/files`）
4. **路径 → pick_code → 直链**（`/files/download`）

Cookie 失效时 115 返回 405，这里会直接提示；企业微信也会推一条告警。

> 这个 Cookie 同时用于播放直链和事件监听。115 网页端重新登录一般不会把旧 Cookie 踢掉，但有效期通常几周，失效了重贴一次即可。

#### ③ CloudDrive2（Token 可留空）

| 配置 | 填什么 |
|---|---|
| gRPC 地址 | 默认 `127.0.0.1:19798` |
| API Token | 可选。填了扫描前会强制刷新目录缓存；不填就跳过 |

**Token 不填会不会有问题？** 不会。CD2 挂载本身对 Plex 可见，扫描照常；只是刚转存进来的文件如果还在 CD2 的目录缓存里，可能要等缓存过期才看得到。有事件监听时一般也不影响。

#### ④ 路径映射

把 **Plex 看到的路径** 映射成 **云盘路径**（给 115 用）和 **CD2 内部路径**（给刷新缓存用）。

| Plex 里看到 | 115 网盘里 | CD2 内部 |
|---|---|---|
| `/mnt/cd2/115/电影/沙丘2 (2024)/沙丘2.mkv` | `/电影/沙丘2 (2024)/沙丘2.mkv` | `/115/电影/沙丘2 (2024)/沙丘2.mkv` |

对应填：Plex 前缀 `/mnt/cd2/115`、云盘前缀 `/`、CD2 前缀 `/115`。

### 3. Plex 侧设置

1. **添加媒体库**：路径选 CD2 挂载路径，例如 `/mnt/cd2/115/电影`
2. **关闭自动扫描**：设置 → 媒体库 → 取消「自动扫描我的媒体库」
3. **减少云盘读取**：关闭视频预览缩略图、片头/片尾标记、章节缩略图
4. **网络**：安全连接选「首选」；「自定义服务器访问 URL」填 `http://NAS地址:32500`
5. **客户端**：远程画质设「原画」，关闭自动调整质量，端口填 **32500**

---

## 局域网要不要也 302？

这是一个开关，在「设置 → 网络与安全 → 局域网也 302」。

| | 关闭（默认） | 打开 |
|---|---|---|
| 外网播放 | 302 到 115 | 302 到 115 |
| 内网播放（电视/盒子） | 回源 Plex，**NAS 上行给电视** | 直接连 115，**NAS 不上行** |
| 转码 / 字幕 | 走 Plex，正常 | 走 Plex，正常 |
| 前提 | — | 电视能访问公网（能连 115 CDN） |

你说的「电视看片要 NAS 上传给电视」，就是默认的内网回源。打开这个开关后，电视拿到的也是 115 直链，NAS 只处理起播那一个请求。

代价：内网播放会走公网 CDN，消耗宽带；如果电视所在网络访问 115 很慢，反而不如 NAS 局域网直传。**建议先打开试一晚，卡就关掉。**

> 注：Plex Web 的 HLS/转码分片永远回源，这个开关只影响 Direct Play。

---

## 增量入库：115 操作事件

CMS 整理 = 在 115 里移动 / 改名 / 复制 / 上传，这些动作都会出现在 **115 App 的「最近操作」** 里。
plex302 直接读这条流水（`GET /behavior/detail`），游标增量消费：

```text
拉一页事件（按 id 倒序）
   └─ 遇到 id <= 游标 立刻停，只处理新的
        └─ 过滤浏览/收藏/标签类事件
             └─ parent_id → 完整云盘路径 → 路径映射 → Plex 路径
                  └─ 推导目录（电影目录 / 剧集目录 / 删除时扫父目录）
                       └─ 静置队列，安静下来才真正入队扫描
```

内置的四层防漏（都不用你调）：

| 机制 | 值 | 作用 |
|---|---|---|
| **静置合并** | 90 秒，单目录最多 10 分钟 | 同目录每来一个新事件就重新计时；CMS 连着移动 10 个文件，只在最后扫一次 |
| **扫完复核** | 3 分钟后 | 接住“扫描时文件还没写完 / 字幕还没到位” |
| **最近文件兜底** | 事件接口空返回时 | 用 `/files/recent` 补一刀 |
| **每日全量** | 04:30 | 前面都失效时，整库扫一遍 |

另外：**Plex 不可用时不推进游标**，等恢复再消费，不会把事件白白吃掉。

设置里只有两个开关和一个间隔：

- 启用 / 停用
- 首次运行是否补扫历史（默认关，只记游标）
- 检查间隔：填数字 + 选分钟/小时（默认 1 分钟）

---

## 全量同步（兜底）

```http
GET /library/sections/<sectionId>/refresh?X-Plex-Token=<token>
```

不带 `path` 就是整库扫描。触发方式：概览按钮、每天定时（默认 04:30）、启动时可选、`POST /api/sync/full`、CMS 的 Emby 通知。

整库扫描很吃 CPU 和磁盘 IO（Plex 要逐文件读元数据，云盘还要拉取），建议放凌晨；它只是兜底，日常靠事件。

---

## 和 CMS / MoviePilot 怎么配合？

plex302 不依赖 CMS，也不生成 strm。CMS 整理完的文件落在 115 上，115 会记录「移动 / 改名」事件，
plex302 读这条事件就知道该扫哪个目录——**不需要 CMS 主动通知**。

如果 CMS 开了 strm、想让它调扫库，可以把 Emby 地址指向本容器：

```yaml
EMBY_HOST_PORT=http://NAS:999
EMBY_API_KEY=<webui 里 /emby 入口带的 api_key>
```

支持 `/emby/System/Info`、`/emby/Library/Refresh`、`/emby/Library/Sections/{id}/Refresh`、`/emby/Items/{id}/Refresh`。

其它工具（MoviePilot / 自己写的脚本）用通用 Webhook：

```bash
curl -G "http://NAS:999/api/hook" --data-urlencode "token=p302_xxx" --data-urlencode "path=/电影/沙丘2 (2024)"
curl -X POST "http://NAS:999/api/hook?token=p302_xxx&full=1"
```

> 中文路径记得 URL 编码（`curl -G --data-urlencode` 或浏览器会自动处理）。

---

## 链接入库（115 分享 / 磁力 / ed2k）

把链接发到企业微信，或者粘到「媒体库 → 链接入库」，plex302 会转存 / 离线到指定目录，之后照常由 115 事件触发扫库。

### 1. 建自建应用

企业微信管理后台 → 应用管理 → 自建 → 创建应用 → 记下 `AgentId`、`Secret`，企业信息里拿 `CorpID`。
这三个填到「设置 → 通知（企业微信）」。

### 2. 开接收消息

应用详情 → 接收消息 → 设置 API 接收：

| 字段 | 填什么 |
|---|---|
| URL | 设置页「链接入库」里显示的 `https://你的域名/api/wecom/callback` |
| Token | 自己编一串，和设置页一致 |
| EncodingAESKey | 点「随机生成」，把 43 位复制到设置页 |

企业微信会立刻发一个 GET 校验，plex302 用 Token 验签 + AES 解密后原样返回 `echostr`，通过即保存成功。

> 回调地址必须**公网可达**。如果 plex302 只在局域网，用反代 / 内网穿透把 `/api/wecom/callback` 暴露出去即可，不需要暴露整个后台。

### 3. 用法

在微信里（企业微信绑定微信后，直接从微信的「企业微信」会话进）给这个应用发：

| 发什么 | 效果 |
|---|---|
| `https://115.com/s/xxxx?password=abcd` | 转存到入库目录 |
| `magnet:?xt=urn:btih:...` / `ed2k://...` | 115 离线下载到入库目录 |
| `状态` | 最近的事件 / 目录监听结果 |

重活在后台线程跑，企业微信 5 秒超时不会卡住；结果用应用消息推回来。

---

## CD2 目录监听：有没有用？会不会风控？

**有用，但默认关。** 它做的是「遍历 CD2 挂载目录，比对文件列表找新增」，和 115 操作事件是两套完全独立的机制。

| | 115 操作事件 | CD2 目录监听 |
|---|---|---|
| 原理 | 115 服务端告诉你发生了什么 | 自己一层层翻目录 |
| 请求量 | 1 次/分钟（拉一页） | 每轮遍历所有目录，每个目录都可能触发 115 请求 |
| 风控风险 | 低 | **高**（库大时明显） |
| 时效 | 秒级~分钟级 | 取决于间隔 |
| 依赖 | Cookie | CD2 挂载 |

CD2 挂的是 115，`os.scandir` 到没缓存的目录时，CD2 会去请求 115。库大 + 间隔短 = 持续打接口，115 会返回 405/403（风控），严重时影响账号。

所以：

- **日常不用开**，事件已经覆盖
- 只有 Cookie 失效、事件接口抽风时才临时打开
- 真要长期开，间隔填 1 小时以上，并把「媒体库」页里不需要的库关掉

---

## 企业微信通知

和 [115 秒传助手](https://github.com/celestial520/115-rapid-upload) 同一套应用消息，配置项完全一样。

| 配置 | 填什么 |
|---|---|
| CorpID | 企业微信 → 我的企业 → 企业 ID |
| Secret | 应用管理 → 自建应用 → Secret |
| AgentID | 同一个应用的 AgentId |
| 接收人 | `@all` 或成员账号 |
| 代理地址 | 可选，企业微信 API 反代 |

三类通知独立开关：

```text
Plex302 · 增量入库完成
已刷新 3 个目录
· 沙丘2 (2024)（电影）· 115 事件
· 测试剧（电视剧）· CMS 通知
· 流浪地球3（电影）· 延迟复核
```

- 一轮多个目录 **15 秒内汇总成一条**
- 错误同类 **30 分钟只发一次**
- 配好点「发送测试消息」立即验证

---

## 配置说明

| 配置 | 默认 | 说明 |
|---|---|---|
| `provider.type` | `115` | `115`（Cookie）/ `cd2` / `alist` / `custom` / `none` |
| `provider.ua_mode` | `client` | 115 直链绑定 UA，建议跟随客户端 |
| `provider.force_https` | `true` | 把 115 返回的 http 直链改成 https |
| `mounts[].plex_prefix` | `/mnt/cd2` | Plex 看到的路径前缀 |
| `mounts[].cloud_prefix` | `/` | 云盘路径前缀（115/AList 用） |
| `mounts[].cd2_prefix` | 空 | CD2 内部路径前缀（刷新缓存用，可空） |
| `scan.enabled` | `true` | 总开关 |
| `scan.watch_enabled` | `false` | CD2 目录监听（兜底，默认关） |
| `scan.interval` | `1800` | 目录监听间隔（秒） |
| `scan.max_depth` | `3` | 目录监听最大深度 |
| `scan.min_size_mb` | `20` | 小于这个大小的视频忽略 |
| `scan.full_enabled` | `true` | 每日全量 |
| `scan.full_time` | `04:30` | 全量时间 |
| `scan.full_on_start` | `false` | 启动时是否立刻全量 |
| `events.enabled` | `true` | 115 操作事件监听 |
| `events.interval` | `60` | 检查间隔（秒，界面上按分钟/小时填） |
| `events.first_scan` | `false` | 首次运行是否补扫历史 |
| `events.settle_seconds` | `90` | 目录静置（内置，界面不暴露；每来一个新事件顺延） |
| `events.max_wait_seconds` | `600` | 单目录最长等待 |
| `events.verify_delay` | `180` | 扫完复核延迟 |
| `events.limit` / `max_pages` | `100` / `5` | 每页条数 / 最多翻页 |
| `notify.wecom_enabled` | `false` | 企业微信总开关 |
| `notify.on_scan` | `true` | 增量入库通知 |
| `notify.on_full` | `true` | 全量通知 |
| `notify.on_error` | `true` | 错误通知 |
| `ingest.enabled` | `false` | 链接入库总开关 |
| `ingest.target_dir` | 空 | 转存 / 离线的目标目录 |
| `ingest.wecom_token` | 空 | 企业微信回调 Token |
| `ingest.wecom_aes_key` | 空 | 企业微信 EncodingAESKey（43 位） |
| `ingest.allow_users` | 空 | 允许使用的成员 UserID，留空=全部 |
| `security.emby_token` | 自动生成 | `/emby` 入口的 `api_key` |
| `security.webhook_token` | 自动生成 | Webhook token |
| `u115.cookie` | 空 | 115 网页 Cookie（直链 + 事件） |
| `u115.recent_enabled` | `true` | 最近文件兜底 |
| `server.redirect_lan` | `false` | 局域网也 302 |
| `server.lan_cidrs` | 常见内网段 | 这些 IP 被视为局域网 |
| `advanced.force_direct_play` | `true` | 改写 Plex decision，强制 directPlay |
| `advanced.url_cache_ttl` | `1800` | 直链缓存（不会超过实际有效期） |

---

## 常见问题

**Q：115 直链会不会因为 UA 不对打不开？**

会。115 的直链和请求 UA 绑定，所以默认 `ua_mode=client`（用播放器自己的 UA 去换链接）。如果换了客户端还是 403，先看是不是被反代改了 UA。

**Q：Cookie 多久失效？**

一般几周。失效时 115 返回 405，日志和「链路状态」里都能看到，企业微信也会推一条。重新从浏览器复制一次即可。

**Q：为什么设置里看不到静置时间、拉取条数了？**

按 115 的风控节奏固定好了（90 秒 / 10 分钟 / 3 分钟 / 100 条），不需要调。所有 115 请求由统一闸门控制：后台任务（事件、入库、目录操作）**任意两次至少隔 2.05 秒**——比 CMS 默认的 2.0 秒只慢不快；直链解析有目录缓存（10 分钟）和 pick_code 缓存（30 分钟），走独立通道，不会拖慢起播。

**Q：怎么确认 302 生效了？**

```bash
curl -I -H "X-Forwarded-For: 1.2.3.4" \
  "http://127.0.0.1:32500/library/parts/12345/1700000000/file.mkv?X-Plex-Token=xxx"
```

看到 `HTTP/1.1 302`、`X-Plex302: redirect`、`Location: https://...` 就对了。`X-Plex302: proxy` 会带原因。

**Q：Plex 里文件显示不可用 / 扫不到？**

- CD2 挂载在 Plex 容器里是否可见？路径是否和 plex302 里配的一致？
- 用「媒体库 → 扫描」手动扫一次，看「扫描记录」的报错
- 实在不行点「全量同步」

**Q：CMS 整理完没自动扫？**

1. 「概览 → 115 增量入库」里能不能看到刚才那次整理的事件
2. 看到了但没扫：看「待扫目录」，静置 90 秒没到不会扫
3. 扫描记录里 source 是 `115event` 才说明走的是事件；`cms-emby` 是 CMS 主动通知；`115recent` 是兜底
4. 都没有就手动点「全量同步」

**Q：会不会修改/删除我的文件？**

不会。只读 Plex API、115 webapi、CD2 gRPC；不写媒体目录，不生成 strm，不转存。

**Q：ARM / 群晖 / 绿联能跑吗？**

镜像构建了 `linux/amd64` + `linux/arm64`，compose 用 host 网络。

---

## 数据目录

| 路径 | 内容 |
|---|---|
| `/config/config.json` | 所有设置（含密文密码、webhook / emby token） |
| `/config/state.db` | 扫描记录、监听索引、115 事件流水与游标、302 统计 |
| `/config/logs/app.log` | 运行日志，5MB × 3 轮转 |

`state.db` 的 `poll_state` 表存事件游标（`115_event_cursor`）和全量日期（`full_last_date`）。
删掉它会让程序从当前时刻重新记事件（不补扫历史）。

---

## 安全

- WebUI 默认 `admin/admin`，首次登录必须改
- 不要把 999 端口直接暴露公网
- Webhook / Emby 入口都带 token，泄露后改 `config.json` 里的 `security.*_token` 后重启
- 115 Cookie 和企业微信 Secret 存在 `config` 目录，注意备份权限

---

## 构建

```bash
git clone https://github.com/celestial520/plex302
cd plex302
docker compose up -d --build
```

镜像：`ghcr.io/celestial520/plex302:latest`（amd64 + arm64）。推 `main` 或打 `v*` tag 自动构建，冒烟通过才推镜像。冒烟覆盖：

- 容器启动、nginx/njs 正常
- 自定义直链：外网 302 / 内网回源
- **115 Cookie 直链**：路径 → pick_code → `/files/download` → 302
- **局域网也 302**：打开开关后内网同样 302
- 115 操作事件 → 游标增量 → 局部入队 → 扫描记录带 `115event`
- 全量同步扫完所有媒体库
- Emby 兼容入口（401 / 200 / 204）

### 项目结构

```text
app/
  main.py            路由、鉴权、Webhook、Emby 兼容入口
  plex.py            Plex API（局部/整库扫描、parts 查路径、decision）
  cd2.py             CloudDrive2 gRPC（刷缓存、取直链）
  u115web.py         115 网页 Cookie：列目录 / pick_code / 直链 / 最近文件
  u115events.py      115 生活事件接口（App 里的「最近操作」）
  events.py          事件监听：游标增量 + 静置合并 + 延迟复核
  scanner.py         扫描队列（局部扫描 / 全量同步）
  watcher.py         CD2 目录轮询（兜底）
  scheduler.py       每天定时全量同步
  notify.py          企业微信应用消息
  providers.py       直链来源：115 Cookie / CD2 / AList / 自定义
  resolver.py        路径映射 + 局域网判断 + 直链缓存
  u115ops.py         115 写操作：建目录 / 移动 / 改名 / 转存 / 离线
  ingest.py          115 分享 / 磁力 / ed2k 入库
  wecom.py           企业微信回调：验签 + AES 解密 + 指令分发
nginx/
  plex302.js         njs：拦截 /library/parts、decision 改写、302 或回源
tests/mock_plex.py   假 Plex + 假 CDN
tests/mock_115.py    假 115 webapi（事件 + 目录 + 直链 + 写操作 + 转存）
```

---

## 和 plex-demo 的关系

| 能力 | plex-demo | 本项目 |
|---|---|---|
| 外网 302 / 内网回源 | ✅ | ✅ |
| 局域网也 302 | ❌ | ✅ 开关 |
| Plex 局部刷新 | ✅ | ✅ |
| 全量同步（整库扫） | ❌ | ✅ 手动 + 每日定时 |
| 115 授权方式 | 开放平台扫码 | Cookie（直链 + 事件同一套） |
| 115 操作事件增量入库 | ❌ | ✅ 游标 + 静置 + 复核 |
| 目录监听自动入库 | 监控 strm | ✅ 监控 CD2 真实目录（默认关） |
| strm 生成 / 监控 | ✅ | ❌ 不需要 |
| 115 秒传 | ✅ | ❌ 与入库无关 |
| 自动重命名 / 写 NFO | ✅ | ❌ 交给 CMS / 其它工具 |
| 微信通知 | ✅ | ✅ 企业微信应用消息 |
| WebUI | Vue | 原生 JS（无构建，双主题） |

---

## 已知限制

- **只对 Direct Play 生效**，转码请求一定回源。
- 115 直链和 UA 绑定，换客户端可能 403。
- 同一个 115 Cookie 被网页端重新登录挤掉时需要重贴（一般不会）。
- CD2 缓存刷新是“尽力而为”，失败只记日志，不阻塞扫描。
- 事件接口偶发空返回，程序会重试并保留游标，极端情况靠每日全量补。
- 不内置整理：改名 / 归档 / 洗版交给 CMS 或其它工具，本项目只负责 302、增量扫库和链接转存。
- 115 的写接口（建目录 / 转存 / 离线）按网页版公开接口实现，只在假 115 上验证过；第一次先拿一个小分享试。
- 115 分享转存受账号空间和会员权益限制；磁力离线受 115 离线额度限制。
- 企业微信回调要求公网可达，纯内网部署需要自己做反代或内网穿透。
- CD2 目录监听会持续打 115 接口，**默认关闭**。
- 本项目不内置任何账号、Cookie 或破解逻辑。

---

## 免责声明

仅供个人媒体库的技术研究与自用。请遵守 Plex、115、CloudDrive2 的服务条款；不要用于传播侵权内容或商业用途。因使用本工具导致的账号风控、数据或服务中断，作者不承担责任。
