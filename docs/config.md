# 配置与 API 修改位置

> 原则：**凭证与开关全部在网页里改，代码里不放任何密钥。**
> 改完保存即生效（少数需要重新同步），不用改代码、不用重启容器。

---

## 一、三层配置，从常用到少见

| 层 | 改什么 | 在哪 | 是否要重启 |
|---|---|---|---|
| **① 网页（99% 的场景）** | 各平台 API 凭证、同步间隔、免打扰、审批制度、AI 模型 | 管理控制台 / 设置页 | 否 |
| **② 环境变量** | 端口、管理员初始密码、数据库路径、演示数据、调试登录 | `.env`（docker compose 自动读取） | 是（`docker compose up -d`） |
| **③ 代码常量** | 钉钉接口基址、默认端口、AI 提示词、判断护栏 | `.py` 文件 | 是（要重建镜像） |

---

## 二、平台凭证：网页里填（推荐）

路径：**右上角身份栏 → 🛡 管理员登录 → 控制台 → 平台配置 → 对应卡片**

| 平台 | 要填的字段 | 存储键位（config 表） |
|---|---|---|
| 钉钉 | app_key / app_secret / corp_id / agent_id / public_url / admin_userid / sso_admin | `dingtalk:*` |
| 钉钉行为开关 | `notify_new_wait`、`notify_new_approval`、`notify_digest`、`notify_quiet`、`robot_only_at_me`、`cc_per_run`、`cc_days`、`process_codes` | `dingtalk:*` |
| 飞书 | app_id / app_secret | `feishu:*` |
| GitHub | token / repos / org / webhook_secret | `github:*` |
| GitLab | host / token / org | `gitlab:*` |
| Gitea | host / token / org | `gitea:*` |
| 禅道 | host / account / password | `zentao:*` |
| 金蝶 | host / acct_id / app_id / app_secret / user | `kingdee:*` |
| 用友 | host / app_key / app_secret / ent_code / user / todo_path | `yonyou:*` |
| 企业微信 | corp_id / secret / agent_id | `wecom:*` |
| 全局 | 同步间隔 `interval_min`、自动同步 `auto_sync`、站点地址 `site:public_url` | 无前缀 |

字段中文说明在 `settings.py` 的 `PLATFORM_FIELDS`（想加字段就改这里，前端会自动渲染成输入框）。

> **审批模板 ID 不预置**：`dingtalk:process_codes` 请填自己企业的 `PROC-xxxx`（逗号分隔），
> 或干脆留空 —— 同步时会用管理员身份自动枚举本企业模板（24 小时刷新一次），一般不需要手填。
>
> **企业标识不在匿名接口暴露**：`/api/ping` 只返回存活状态与「是否已配钉钉」，
> `CorpId / AppKey` 走 `/api/ding/appinfo`，登录后才下发。

> **Secret 永不回传前端**：接口只返回"已配置/未配置"，页面里留空表示不修改。

---

## 三、用 curl 改配置（不想开浏览器时）

```bash
HOST=http://127.0.0.1:8787
# 1) 换管理员 token（有效期见返回的 expires）
TOKEN=$(curl -s -X POST $HOST/auth/admin -H 'Content-Type: application/json' \
  -d '{"password":"你的管理员密码"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

# 2) 填钉钉凭证（sdk 会自动兼容老键位）
curl -s -X POST "$HOST/api/config?token=$TOKEN" -H 'Content-Type: application/json' -d '{
  "appKey":"dingxxxx","appSecret":"****","corpId":"dingxxxx","agentId":"123456",
  "intervalMin":30,"autoSync":true}'

# 3) 填任意平台（body 里的 platform 决定写哪张卡：dingtalk/feishu/github/gitlab/gitea/zentao/kingdee/yonyou/wecom）
curl -s -X POST "$HOST/api/platforms?token=$TOKEN" -H 'Content-Type: application/json' \
  -d '{"platform":"feishu","app_id":"cli_xxx","app_secret":"****"}'

# 4) 看配置现状（Secret 只显示是否已填）
curl -s "$HOST/api/platforms?token=$TOKEN" | python3 -m json.tool | head -40

# 5) 触发同步（异步，几秒到几分钟）
curl -s -X POST "$HOST/api/sync?token=$TOKEN" -H 'Content-Type: application/json' -d '{}'

# 6) 查数据：db 参数要填「表 ID」（不是 task/ding 这类别名，见下方映射）
curl -s "$HOST/api/query?db=VogzQYVe0XppAYgBuOpS1s&size=3&token=$TOKEN" | python3 -m json.tool | head -30
```

**表 ID 映射**（来自 `seed_schema.json`，固定不变）：

| 别名 | 表 ID | 内容 |
|---|---|---|
| task | `151WfQT8N0hn6nsOMnmMqh` | 任务 |
| sched | `KHYNKWUOMqxe4rqJJFOl6t` | 日程 |
| wait | `DiQ0qiGK3ZejC7g3bc1NDk` | 等待回复 |
| files | `8rrQVoUCjp1jWnVlVLkWIm` | 资料 |
| ding | `VogzQYVe0XppAYgBuOpS1s` | 钉钉审批 |
| meet | `8pG50L6vRsWi6v7P8YlGC7` | 会议 |
| cat | `TWmt9f5r0XRGHDZBc7v9Wx` | 分类 |
| apps | `OnTaihRfxXKvpltLhEXgEr` | 应用接入 |

程序内部用 `db.TABLES['ding']` 取，写脚本时不要硬编码字符串，直接 `import db; db.TABLES['ding']`。

> 普通员工 token 也可以调 `/api/query`，但**只会返回自己的数据**；管理员可用 `?person=<person_id>` 指定看谁的，`?person=__nobody__` 返回 0 条（用来反证隔离生效）。

---

## 四、AI 模型与制度

### 4.1 大模型接口（OpenAI 兼容）

三种生效顺序：**个人配置 > 全局配置 > 「应用接入」表里的 AI 记录**。

| 方式 | 在哪 | 适用 |
|---|---|---|
| 个人 | 设置页 → **我的 AI 模型** → 填 `接口地址` / `Key` / `模型名` | 每人用自己的 Key，成本自理 |
| 全局（推荐） | 管理员在 **应用接入** 加一条 `应用类型=AI 接口` 的记录：接口地址 / 应用密钥 / 应用ID 填模型名 | 全公司共用一个 Key |
| 直写库 | `config` 表 `ai:global` = `{"base":"https://api.xxx.com/v1","key":"sk-...","model":"gpt-4o-mini"}` | 批量/脚本部署 |

```bash
# 个人模型：需要「员工登录态」的 token（管理员 token 没有 person_id，会报错）
# 最省事的用法就是网页：设置 → 我的 AI 模型
curl -s -X POST "$HOST/api/ai/mycfg?token=<员工token>" -H 'Content-Type: application/json' \
  -d '{"base":"https://api.openai.com/v1","key":"sk-xxx","model":"gpt-4o-mini"}'

# 全局模型：写 config 表的 ai:global（等价于在「应用接入」加一条 AI 记录）
docker exec -it workbench python -c "
import db, json
db.cfg_set({'ai:global': json.dumps({'base':'https://api.deepseek.com/v1','key':'sk-xxx','model':'deepseek-chat'})})
print('ok')"
```

兼容任何 OpenAI 格式的网关：OpenAI、Azure OpenAI（兼容层）、DeepSeek、通义、Kimi、SiliconFlow、Ollama（`http://host:11434/v1`）等。
**接口地址填到 `/v1` 为止**，代码会自己拼 `/chat/completions`。

### 4.2 审批判断的"制度"口径

制度文本存 `config` 表的 `ai:policy`，建议结构：

```
【考勤】补卡每月 ≤3 次；跨夜超过 8 小时需同时提交加班单。
【采购】单笔 ≥5000 元需附 3 家比价；≥20000 元需总经理审批。
【付款】预付比例 >50% 需说明理由；合同编号必填。
【请假】年假提前 1 天申请；连续 ≥3 天需分管领导同意。
```

写入：

```bash
curl -s -X POST "$HOST/api/judge/policy?token=$TOKEN" -H 'Content-Type: application/json' \
  -d '{"policy":"【考勤】补卡每月 ≤3 次…"}'

# 读取
curl -s "$HOST/api/judge/policy?token=$TOKEN"
```

不配制度也能跑（只按历史习惯判断），面板会显示「未配制度」。

### 4.3 审批判断相关接口

| 接口 | 作用 |
|---|---|
| `GET /api/judge/pending?limit=8` | 待我审批 + 已有判断摘要 |
| `POST /api/judge` `{"pid":"审批编号","force":true}` | 对某张单出判断（`force` 忽略缓存重算） |
| `POST /api/judge/feedback` `{"pid":"...","adopted":true}` | 记录"判断对/有误"，算一致率 |
| `GET /api/judge/stats?days=30` | 一致率统计（`agreement` 百分比） |

---

## 五、环境变量（`.env`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `WB_ADMIN_PASS` | 无（缺失则容器拒绝启动） | 管理员初始密码，**只在首次启动写入数据库** |
| `WB_HOST_PORT` | `8787` | 宿主机端口 |
| `WB_NAME` | `workbench` | 容器名（多实例时改） |
| `TZ` | `Asia/Shanghai` | 时区，影响"今天/本周"的判定 |
| `WB_SEED` | `0` | `1` = 首次启动导入 `seed_data.json` 演示数据 |
| `WB_DEV_LOGIN` | `0` | `1` = 开启 `/auth/dev-login` 模拟身份（**生产必须 0**） |

进程级变量（直接 `python3 app.py` 时可用，Docker 里已固定）：`WB_PORT`、`WB_DB`（默认 `./data/workbench.db`）、`WB_STATIC`（默认 `./static`）。

---

## 六、真要改代码，改这几处

| 想改什么 | 文件与位置 |
|---|---|
| 钉钉接口基址（内网代理 / 私有化） | `dingtalk.py` 顶部 `OAPI`、`NEWAPI` |
| 端口默认值 | `app.py` 的 `PORT = int(os.environ.get('WB_PORT','8787'))` |
| 哪些表不按人隔离 | `db.py` 的 `PUBLIC_TABLES`（默认只有分类、应用接入） |
| 同步间隔下限、单轮人数 | `app.py` 调度处 `gap = max(5, interval_min)*60`；平台配置里的 `cc_per_run` |
| AI 助手的系统提示词 | `ai_bot.py` 里 `你是公司工作台的办公助手…` 那段 |
| AI 可用的工具集 | `ai_bot.py` 的 `LLM_TOOLS` 与 `_tool_exec` |
| 审批判断的提示词与口径 | `approval_agent.py` 的 `PROMPT` / `judge()` |
| 审批判断的护栏（阈值、白名单结论） | `approval_agent.py`：金额阈值 5000、`verdict` 白名单、`need_info`/`manual` 分支 |
| 平台字段（加/改输入框） | `settings.py` 的 `PLATFORM_FIELDS` |
| 页面文案与布局 | `static/index.html`（单文件，含样式）、`static/auth-ui.js`（登录/控制台） |

改完代码后必须重建镜像：

```bash
docker compose up -d --build
```

---

## 七、安全建议

1. **用完就轮换**：把 AppSecret / Token 贴进过聊天记录的，去平台后台重置一次。
2. **`.env` 不进版本库**（已在 `.gitignore`），权限设 `600`。
3. **`WB_DEV_LOGIN` 保持 0**，否则 `/auth/dev-login` 可伪造身份。
4. **对外只开 8787**，SQLite 文件不要放到 web 目录、不要挂 NFS。
5. 员工离职：人员管理里解绑/停用，或直接把该人的 `person_id` 记录清掉。
6. 备份含凭证：`data/workbench.db` 里是明文 Secret，备份文件要加密存放。
