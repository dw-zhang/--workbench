# 安装与部署

> 目标读者：拿到服务器、准备把它跑起来的人。全程命令行，复制粘贴即可。
> 默认端口 `8787`，下面的 `SERVER_IP` 换成你的服务器地址。

---

## 0. 前置条件

| 项 | 要求 | 说明 |
|---|---|---|
| 系统 | Linux（amd64/arm64）/ macOS / Windows(WSL2) | 生产推荐 Ubuntu 22.04+ |
| Docker | 20.10+，含 compose 插件 | `install.sh` 会自动装 |
| 内存 | 512MB 可用即可 | 纯 Python 标准库，无第三方依赖 |
| 磁盘 | 1GB 起 | 数据是单个 SQLite 文件，增长很慢（1 万条约 20MB） |
| 网络 | 能访问 `oapi.dingtalk.com`、`api.dingtalk.com` | 公司有出口白名单的话要放行 |
| 端口 | 8787（可改） | 被占用就 `--port 9000` |

钉钉要把事件回调发到你这台机器，所以**必须有公网可访问地址**（或反向代理 + 域名）。纯内网部署时，免登/扫码/机器人回调不可用，但手动同步仍可工作。

---

## 1. 一键安装

```bash
# 1) 拿代码
git clone https://github.com/dw-zhang/dingtalk-workbench.git
cd dingtalk-workbench

# 2) 跑安装（自动：查 docker → 建 .env → 构建 → 启动 → 等健康 → 打印地址和密码）
./install.sh
```

输出示例：

```
✅ 安装完成
 访问地址   http://192.168.1.10:8787
 本机地址   http://127.0.0.1:8787
 管理员密码 7fK2pQ9xRt
 数据目录   /root/dingtalk-workbench/data
```

常用参数：

```bash
./install.sh --port 9000     # 换宿主机端口
./install.sh --seed          # 首次导入演示数据（先看效果，之后再清库）
./install.sh --yes           # 全自动（缺 docker 也直接装）
./install.sh --uninstall     # 停容器 + 删容器，./data 保留
```

如果服务器拉 `python:3.11-slim` 很慢：

```bash
sudo mkdir -p /etc/docker && sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{ "registry-mirrors": ["https://docker.m.daocloud.io", "https://dockerproxy.com"] }
EOF
sudo systemctl restart docker
```

---

## 2. 手动部署（等价于脚本做的事）

```bash
cp .env.example .env
# 编辑 .env：把 WB_ADMIN_PASS 改成强密码

docker compose up -d --build
docker compose ps            # STATUS 里出现 (healthy) 就算成功
docker compose logs -f       # 看启动日志，Ctrl+C 退出不影响服务
```

`docker-compose.yml` 关键点：

| 配置 | 作用 |
|---|---|
| `${WB_HOST_PORT:-8787}:8787` | 宿主机端口可变，容器内固定 8787 |
| `./data:/app/data` | **所有数据都在这个目录**，容器重建不丢 |
| `WB_ADMIN_PASS` | 首次启动写入数据库；之后改这里无效 |
| `WB_SEED=1` | 首次启动导入演示数据 |
| `healthcheck` | 每 30s 打 `/api/ping`，失败 3 次标记 unhealthy |
| `restart: unless-stopped` | 服务器重启自动拉起 |

---

## 3. 首次配置（网页里，5 分钟）

### 3.1 管理员登录

1. 打开 `http://SERVER_IP:8787`
2. 登录卡切到 **「管理员」** tab → 输入 `.env` 里的 `WB_ADMIN_PASS`（不要点「用钉钉身份进入」，那是员工通道）
3. 登录后顶栏出现 `🛡 你的名字` → 点它 → 弹出**管理员控制台**：人员管理 / 平台配置 / 钉钉免登自检 / 修改密码

### 3.2 填钉钉凭证

控制台 → **平台配置** → 钉钉卡片：

| 字段 | 从哪拿 |
|---|---|
| AppKey / AppSecret | 钉钉开放平台 → 应用开发 → 企业内部应用 → 你的应用 → 凭证与基础信息 |
| CorpId | 同页「企业信息」，或开发者后台首页 |
| AgentId | 应用 → 版本管理与发布 / 基础信息里的微应用 AgentId |
| 对外访问地址 | 你实际访问的地址，如 `http://192.168.1.10:8787`（扫码回调按它生成） |
| 管理员钉钉 UserID | 可选。填了才允许「钉钉单点登录即管理员」 |

保存后验证：控制台 → **钉钉免登自检**（逐项体检，直接告诉你缺什么权限、点哪个链接去申请）；
或到设置页 `#/settings` 底部的「钉钉连接」卡点 **测试连接**。

### 3.3 拉人员

控制台 → **人员管理** → 「拉取钉钉通讯录」→ 全员进人员库
→ 点「重新自动匹配」（手机号/邮箱任一命中即绑定，冲突项人工裁决）。

> 只绑需要的人也行：没绑定的人不会产生数据、不会占用同步时间。

### 3.4 第一次同步

回首页 → 右下角同步状态卡点 **立即同步**（管理员才可见）。同步条会实时显示
「同步中 · 钉钉：张三（12/202）」，不用盲等。

首次是**全员建账**：库里有多少人，就要按人拉一遍（每人 10–25 秒，200 人约 40–60 分钟）。
所以默认每轮只跑 `dingtalk:sync_limit`（60）人，按「上次同步时间」从老到新排队，
新人永远排最前面——这意味着**任何员工第一次打开工作台，他的数据已经在了**，
不用等整轮跑完。人数少（<60）的话一轮就是全员，没有这个感觉。

第一次被同步的人只入库、**不发钉钉通知**，避免上线当天给全公司每人推一条「你有 N 条待审批」。

---

## 4. 钉钉后台还差三步

进 [钉钉开放平台](https://open-dev.dingtalk.com) → 你的应用：

1. **应用首页地址**（微应用）＝ `http://SERVER_IP:8787`
   钉钉客户端内打开才走免登，地址必须与应用配置一致。
2. **登录与分享 → 回调域名** ＝ `SERVER_IP:8787`（不带 `http://`）
   浏览器扫码登录回调 `http://SERVER_IP:8787/auth/dingtalk/callback`，域名对不上会报 `redirect_uri 不匹配`。
3. **机器人 → 消息接收地址** ＝ `http://SERVER_IP:8787/dingtalk/robot`
   消息接收模式选 HTTP 推送。签名校验用 AppSecret 的 HMAC-SHA256(timestamp)。对不上时先看日志里 `[robot] 签名校验失败`。

每次改完权限或回调，都要**发布一个新版本**，否则不生效。

---

## 5. 反向代理与 HTTPS（可选但推荐）

钉钉对 http 的兼容性没问题，但如果你要用域名或证书，Nginx 这样配：

```nginx
server {
    listen 80;
    server_name wb.example.com;

    location / {
        proxy_pass         http://127.0.0.1:8787;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;   # 同步和 AI 判断可能跑几十秒，别用默认 60s
    }
}
```

要点：
- 必须转发 `Host` 和 `X-Forwarded-Proto`，否则扫码回调地址会拼错。
- 加了域名后，把「平台配置 → 钉钉 → 对外访问地址」同步改成 `https://wb.example.com`。
- 服务是单进程 ThreadingHTTPServer，**不要把 worker 开多进程**（内存态会话/缓存不共享）。要抗量就上 Nginx 缓存静态资源。

---

## 6. 升级

```bash
cd dingtalk-workbench
git pull
docker compose up -d --build     # 重建镜像并滚动重启，数据在 ./data 不受影响
docker compose ps                # 等 healthy
```

只改了前端（`static/`）也要重建镜像，因为静态文件是打进镜像的。

---

## 7. 备份 / 恢复 / 迁移

```bash
# 备份（在线安全，SQLite 单文件）
tar czf wb-backup-$(date +%F).tar.gz data/

# 恢复 / 迁移到新机器
# 1) 新机器装好代码
git clone ... && cd dingtalk-workbench
# 2) 解压 data（保持目录名为 data）
tar xzf wb-backup-2026-11-15.tar.gz
# 3) 起容器
docker compose up -d --build
```

建议加个每日定时：

```bash
# crontab -e
0 2 * * * cd /root/dingtalk-workbench && tar czf /backup/wb-$(date +\%F).tar.gz data/ && find /backup -name 'wb-*.tar.gz' -mtime +14 -delete
```

---

## 8. 排错

### 8.1 先看三个地方

```bash
docker compose ps                    # 容器是否 healthy
docker compose logs --tail=100       # 启动/同步/AI 报错
curl -s http://127.0.0.1:8787/api/ping   # 服务是否活着
```

### 8.2 常见报错对照

| 现象 / 报错 | 原因 | 处理 |
|---|---|---|
| `不合法的appKey或appSecret` | 凭证错 / 应用未发布 | 核对 AppKey、AppSecret；发布版本 |
| `appkey permission denied` | 权限未开通或未生效 | 开放平台申请 → 重新发布 → 重新同步 |
| `ip out of range` | 出口 IP 未加白 | 钉钉后台 → 服务器出口IP，加服务器公网 IP |
| `获取 jsapi_ticket 失败` | 缺 JSAPI 鉴权 | 应用权限里加「JSAPI 鉴权」 |
| `Forbidden.AccessDenied.AccessTokenPermissionDenied` | 新版接口权限点缺 | 按提示的权限点（如 `Document.Workspace.Read`）申请 |
| 员工打开空白，提示"不在钉钉环境" | 用普通浏览器打开了 | 用钉钉客户端/工作台进入；浏览器请点扫码登录 |
| `redirect_uri 不匹配` | 回调域名没配 | 登录与分享 → 回调域名填 `host:port` |
| `[robot] 签名校验失败` | 机器人地址/密钥不对 | 核对地址 `/dingtalk/robot`、AppSecret；确认接收模式是 HTTP 推送 |
| 数据全空但同步成功 | 人员未绑定 | 人员管理 → 重新自动匹配 |
| 容器 healthy 但页面 502（反代） | 超时太短 | Nginx `proxy_read_timeout 300s` |
| 管理密码忘了 | 存在库里 | 见 8.3 |

### 8.3 忘记管理员密码

```bash
# 方式一：网页改（还记得旧密码）
控制台 → 修改密码

# 方式二：清掉库里的密码，重启后按 .env 重新初始化
docker compose stop
docker run --rm -v "$PWD/data:/app/data" python:3.11-slim \
  python3 -c "import sqlite3;c=sqlite3.connect('/app/data/workbench.db');c.execute(\"delete from config where key='admin:pass_hash'\");c.commit()"
# 改 .env 里的 WB_ADMIN_PASS，再启动
docker compose up -d
```

### 8.4 想彻底重来

```bash
docker compose down
rm -rf data            # 删库（不可恢复，先备份！）
docker compose up -d   # 按 .env 重新初始化
```

---

## 9. 多实例并存

同一台机器跑两个（测试 + 生产）：

```bash
cp -r dingtalk-workbench wb-test && cd wb-test
./install.sh --port 8788
# .env 里把 WB_NAME 改成 workbench-test，避免容器名冲突
```

两个实例数据目录独立，互不影响。
