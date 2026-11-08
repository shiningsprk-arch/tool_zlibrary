# zlibrary —— MyBooks 外置工具包：Z-Library 搜书入库

从 `https://zlib.ch/` 入口跟随跳转，搜索 Z-Library 并把选中的书下载入库。
支持号池轮换（多个 Z-Library 账号的会话轮换与额度查询），仅管理员自用。

> Z-Library 为第三方影子图书馆，可用性、账号条款与内容合规性由使用者自行负责。
> 多账号轮换可能违反站点条款并导致封号。

## 功能

- **入口固定 `zlib.ch`**：不写死最终域；DoorwaySession 手动跟随 3xx，逐跳处理
  JS 挑战，最终 origin 缓存 10 分钟；失败重新从入口进。
- **搜索**：优先 EAPI `POST /eapi/book/search`（form 编码，JSON 字段会被忽略）；
  不可用时回退 HTML `/s/<query>` 的 `<z-bookcard>` 解析。
- **下载**：账号模式优先 EAPI `/eapi/book/{id}/{hash}/file`；EAPI 不可用或游客
  模式走 HTML `/dl/<token>`，先过挑战再 302 到 CDN。流式下载、大小/类型校验、
  可取消；下载 URL 全程留在服务端。
- **号池**：默认邮箱+密码登录（工具页添加账号默认选中），也支持 Cookie/key 导入；
  只保存会话，不保存密码；EAPI profile 查询每日额度；按最近最少使用/轮询选择；
  额度耗尽、会话失效自动换号。
- **入库**：`calibre.import_file`；入库标签默认空，填了才追加；补 ISBN/出版社/
  语言等元数据可按需扩展。
- **DNS**：系统 DNS 优先；失败/返回 `0.0.0.0` 时走 DoH，并通过 SNI 保持原域名
  的 TLS 校验（不关闭证书验证）。

## 安装

1. 管理端开启 `ENABLE_TOOLBOX_DEV_MODE`（系统设置 → 高级配置项）。
2. `/admin/toolbox` 上传 `dist/zlibrary-<版本>.zip`。
3. **重启 mybooks 服务**（外置工具包只在进程启动时加载/挂路由）。更新同理。

安装后先在工具页点「测试入口 / 域名」：应显示 `zlib.ch → 最终 origin`、挑战次数
和 EAPI/HTML 模式。若目录服务器的 DNS 与浏览器不同，可能需要在设置里把 DNS 模式
保持 `auto`（系统 DNS 失败时 DoH）。

## 账号接入

- **默认：邮箱 + 密码**（工具页添加账号默认选这个）。工具先试 EAPI 登录，失败再试
  HTML `/login`；成功后只保存返回的会话，密码在本次请求后立即丢弃，不写入配置、
  不写日志。
- **备选：Cookie / Key 导入**。浏览器登录 Z-Library 后，从 Cookie 复制
  `remix_userid` / `remix_userkey`（可选 `bsrv`）粘贴到号池。工具只保存这些会话值。
- 社区实测 EAPI 登录端点可能对有效账号返回 `Authorization failed`；这不等同于
  密码错误，改用 Cookie / Key 导入即可。
- 免费账号的每日额度以 `GET /eapi/user/profile` 的
  `downloads_today`/`downloads_limit`（兼容 `dailyDownloadsCount`/`dailyDownloadLimit`）
  为准；游客按 IP 限额。号池用完后可在页面手动刷新额度，或等站点重置。

## 安全设计

- 所有路由显式 `@js` + `@is_admin`；外置工具宿主不会注入鉴权。
- 账号密码只在添加账号的登录请求中使用，不落库；会话 key/cookie 只在服务端，
  前端接口只返回掩码。
- 下载 URL 不经过前端：搜索接口只返回 `book_token`，下载任务按 token 取服务端
  缓存里的 book。
- 下载 CDN 使用独立 session，不携带账号 cookie；`remix-*` header 只发给 EAPI。
- 日志不输出密码、`remix_userkey`、`bsrv` 或 cookie 原值。
- `entry_url` 由管理员配置；跳转目标做 HTTPS/公网地址校验，防 SSRF。

## 开发与测试

```bash
python -m py_compile backend/*.py
python -m pytest tests/ -q
python scripts/build.py          # 纯 Python 打包 + 产物形状校验 + sha256
```

测试不依赖真实站点或账号：客户端协议、挑战 solver、HTML 解析、号池选择/脱敏、
下载重定向作用域、工具冒烟均为离线 fixture/mock。真实链路验证（可选）：
用环境变量临时注入测试账号，跑一次 EAPI 登录 → profile → 公版书下载 → 入库；
不要把账号密码写入仓库、README、测试 fixture 或日志。

## 已知限制

- EAPI 是第三方整理的非官方契约，站点改版可能导致搜索/下载字段变化；解析器集中
  在 `backend/zlib_client.py`，失败时先看工具页错误码和后端日志。
- `zlib.ch` 的跳转链、挑战算法、最终域可能变化；工具不硬编码最终域，挑战无法
  识别时会报 `challenge.unsupported`。
- 号池轮换只解决账号额度，不绕过网络/DNS/站点风控；多账号同 IP 的实际限额
  取决于站点。
- 单下载任务串行；搜索与下载可并行。

## 许可证

MIT（见 LICENSE）。
