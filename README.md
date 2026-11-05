# Plex302 · Plex 云盘 302 直链助手

Plex 通过 **CD2 挂载**直接扫描云盘里的真实文件，**不生成 strm**；外网播放时把 `/library/parts` 请求 **302** 到 115 / AList / CD2 直链，NAS 不再中转视频流量；CMS 整理完还能自动做 **Plex 局部扫描**，只入库变化的目录。

> 一句话：Plex 负责刮削和播放决策，plex302 负责把“读文件”这件事甩给云盘。

---

## 功能

| 能力 | 说明 |
|---|---|
| 不依赖 strm | Plex 直接扫描 CD2 挂载目录里的真实文件；不是 strm 占位 |
| CD2 挂载入库 | Plex 媒体库指向 CD2 挂载路径，正常扫描、刮削 |
| 外网 302 | 拦截 `/library/parts/*` 和 `universal/start`，返回云盘直链 |
| 内网回源 | 局域网默认走 Plex 原链路，转码/字幕/WebUI 不受影响 |
| 局部扫描 | 调 Plex 官方 `GET /library/sections/<id>/refresh?path=<目录>`，不扫全库 |
| CD2 缓存刷新 | 扫描前用 CD2 gRPC `GetSubFiles(forceRefresh=true)` 刷新目录，避免新文件不可见 |
| CMS 自动入库 | 目录监听 + Webhook 两条路：CMS 整理完落盘即扫，或主动调接口 |
| 115 扫码 | 115 开放平台设备码授权，token 自动续期 |
| 多直链源 | 115 开放平台 / CloudDrive2 直链 / AList(OpenList) / 自定义 HTTP |
| WebUI | 状态、媒体库、扫描记录、日志、路径映射、扫码登录全在网页里 |

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

CMS 整理完成 ──► CD2 目录出现新文件
                     │
                     ├─ 目录监听（轮询）
                     └─ Webhook / 手动
                              ▼
                 CD2 刷新目录 ──► Plex refresh?path= ──► 只扫这个目录
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
- 115 账号（用 115 直链时），或 AList / CD2 直链权限
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

点「扫码登录 115」，手机 115 App 扫码确认。token 会自动续期。

默认 `client_id` 是 MoviePilot 的公开应用，所以扫码页显示「MoviePilot」，这是正常的；也可以到 [open.115.com](https://open.115.com/) 申请自己的应用后替换。

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

## CMS 整理完自动入库

CMS（CloudMediaSynC）目前文档里没有对外 webhook，所以给了两条路：

### 方式 A：目录监听（默认，开箱即用）

「设置 → 扫描与自动入库」打开「目录监听」，并在「媒体库」页勾选要监听的库。

- 启动后先等 20 秒，再按 `interval`（默认 180 秒）轮询
- **第一次运行只建索引，不触发扫描**，避免一启动就全库扫描
- 之后发现新增/变化的视频文件，按规则推导目录：
  - 电影：`/电影/片名 (年)/片.mkv` → 扫 `/电影/片名 (年)`
  - 剧集：`/电视剧/剧名/Season 01/S01E01.mkv` → 扫 `/电视剧/剧名`
- 文件修改时间小于 `settle_seconds`（默认 90 秒）先跳过，避免整理中途被扫
- `.part` / `.!qB` / `.crdownload` 等临时文件自动忽略

CMS 整理 = 把文件移动到 115 的媒体库目录 = CD2 挂载里出现新文件，监听会接住。

### 方式 B：Webhook（给脚本 / 其它工具）

设置页会生成一条带 token 的地址：

```bash
# 最简：GET，path 用云盘路径或 Plex 路径都行
curl "http://NAS:999/api/hook?token=p302_xxx&path=/电影/沙丘2 (2024)"

# 或 POST JSON
curl -X POST "http://NAS:999/api/hook?token=p302_xxx" \
  -H "Content-Type: application/json" \
  -d '{"path":"/电视剧/测试剧/Season 01/S01E01.mkv"}'
```

- 传文件路径会自动推导到电影/剧集目录
- 云盘路径会自动反查成 Plex 路径（依赖路径映射）
- 支持 `path` / `paths` / `file` / `file_path` / `dir` / `save_path` 等字段
- 一次最多处理 20 条，90 秒内重复路径自动去重

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
| `scan.settle_seconds` | `90` | 文件静置多久才处理 |
| `scan.max_depth` | `4` | 目录监听最大深度 |
| `scan.min_size_mb` | `20` | 小于这个大小的视频忽略 |
| `scan.first_run_scan` | `false` | 首次索引时是否也扫描（默认只建索引） |
| `scan.watch_sections` | `[]` | 空 = 全部媒体库；`__none__` = 全部关闭 |
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
- 第一次入库大库建议先在 Plex 里手动全量扫一次，后续再用局部扫描

**Q：CMS 整理完没自动扫？**

- 「媒体库」页对应库的「自动监听」是否打开
- 第一次运行只建索引，要等下一轮才会扫新文件
- `max_depth` 是否够（剧集通常是 3 层）
- 看日志 `[监听]` 和 `[扫描]`；也可以点「立即执行监听」

**Q：Plex Web 播放不能 302？**

Plex Web 大量使用 HLS/转码分片（`/video/:/transcode/universal/session`），这类请求必须回源，302 会破坏播放。
这个工具的目标是 **Direct Play**：Infuse、Plex Android TV、Apple TV、手机 Plex App 等在「原画」模式下会直接请求 `/library/parts`。

**Q：会不会修改/删除我的文件？**

不会。只读 Plex API、115 开放平台、CD2 gRPC；不写媒体目录，不生成 strm，不转存。

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
| `/config/config.json` | 所有设置（含密文密码、webhook token） |
| `/config/115_token.json` | 115 access/refresh token |
| `/config/state.db` | 扫描记录、监听文件索引、302 统计 |
| `/config/logs/app.log` | 运行日志，5MB × 3 轮转 |

迁移时把 `./config` 整个拷走即可。

---

## 安全

- WebUI 默认 `admin/admin`，首次登录必须改
- 不要把 999 端口直接暴露到公网；需要远程访问请走 VPN 或加一层认证反代
- Webhook 地址自带 token，泄露后到设置页重新生成（改 `config.json` 里的 `security.webhook_token` 后重启）
- 115 token 存在 `config` 目录，注意备份权限

---

## 构建

```bash
git clone https://github.com/celestial520/plex302
cd plex302
docker compose up -d --build
```

镜像：`ghcr.io/celestial520/plex302:latest`（amd64 + arm64）。
推 `main` 或打 `v*` tag 会自动构建（`.github/workflows/docker.yml`）。

国内构建加速：

```bash
docker build --build-arg APT_MIRROR=mirrors.aliyun.com -t plex302 .
```

---

## 已知限制

- **只对 Direct Play 生效**。客户端坚持转码时，302 没有意义，会回源 Plex。
- 115 直链和请求 UA 绑定；`ua_mode=client` 是默认值，换客户端/换 UA 可能 403。
- CD2 缓存刷新是“尽力而为”，失败只记日志，不阻塞 Plex 扫描。
- 目录监听是轮询，不是 inotify；大库首次索引会慢一些，之后只处理变化。
- 本项目不内置任何账号、Cookie 或破解逻辑，只调用你已有的 Plex / 115 / CD2 / AList 接口。

---

## 免责声明

仅供个人媒体库的技术研究与自用。请遵守 Plex、115、CloudDrive2 的服务条款；不要用于传播侵权内容或商业用途。因使用本工具导致的账号风控、数据或服务中断，作者不承担责任。
