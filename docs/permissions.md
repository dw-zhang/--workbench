# 需要开通哪些权限

> 先说结论：**钉钉四个权限点不通，就有四块功能不通**；其余平台按需开通，不开只影响对应平台。
> 所有凭证都在网页「管理控制台 → 平台配置」里填，代码里没有密钥。

---

## 一、最小可用 vs 全功能

| 目标 | 需要开通 |
|---|---|
| **能登录、能看审批**（最小集） | 通讯录部门信息读 + 通讯录部门成员读 + OA 审批读（`qyapi_work_record`） |
| 能看钉钉待办 | ＋ `Todo.Todo.Read` |
| 能看知识库/文档正文 | ＋ `Document.Workspace.Read` + `Document.Node.Read` |
| 能看钉盘文件 | ＋ `Drive.Space.Read` + `Storage.File.Read` |
| 能看「我发起 / 抄送我」全量 | ＋ `Premium.Workflow.ReadWrite.All`（或登记审批模板编码替代） |
| 群里 @ 的消息进「等待回复」 | ＋ 机器人（消息接收地址 `/dingtalk/robot`） |
| 主动推钉钉通知 | ＋ 工作通知（企业内部应用默认有） |

---

## 二、钉钉（企业内部应用）

申请入口统一在：**钉钉开放平台 → 应用开发 → 企业内部开发 → 你的应用 → 权限管理**，
搜索权限名 → 勾选 → **批量申请**（部分要钉钉小二审批，一般 1 个工作日）。
**开通后必须「版本管理与发布」发一个新版本，否则不生效。**

| 权限名（后台显示） | 权限点代码 | 对应接口 | 缺了会怎样 |
|---|---|---|---|
| 通讯录部门信息读权限 | — | `/topapi/v2/department/listsubid`、`listsub` | 拉不到部门树，通讯录同步直接停 |
| 通讯录部门成员读权限 | — | `/topapi/v2/user/list`、`/topapi/v2/user/get` | 人员库空，全员无法免登 |
| **OA 审批读权限** | `qyapi_work_record` | `/topapi/workrecord/getbyuserid` | **审批一条都同步不进来（唯一硬阻塞）** |
| 钉钉待办读权限 | `Todo.Todo.Read` | `/v1.0/todo/users/{unionId}/tasks` | 待办不同步；只用审批可忽略 |
| 知识库读权限 | `Document.Workspace.Read` | `/v1.0/doc/workspaces` | 知识库列表拿不到 |
| 知识库节点读权限 | `Document.Node.Read` | `/v1.0/doc/suites/documents/{id}/blocks` | 有目录没正文 |
| 钉盘空间读权限 | `Drive.Space.Read` | `/v1.0/drive/spaces` | 钉盘不同步 |
| 文件读权限 | `Storage.File.Read` | `/v1.0/storage/spaces/{id}/dentries/listAll` | 钉盘文件下载/正文不可用 |
| 审批高级接口 | `Premium.Workflow.ReadWrite.All` | `/v1.0/workflow/premium/...` | 「我发起/抄送我」需改用模板编码补齐（见 config.md） |
| JSAPI 鉴权 | — | `jsapi_sign` / `get_jsapi_ticket` | 钉钉客户端内免登失败 |
| 消息通知 | 默认开通 | `/topapi/message/corpconversation/asyncsend_v2` | 推不了工作通知 |

### 权限申请直达链接

控制台「测试连接」会直接给出带 AppKey 的链接，格式：

```
https://open-dev.dingtalk.com/appscope/apply?content=<AppKey>%23<权限点>
```

例（OA 审批）：
`https://open-dev.dingtalk.com/appscope/apply?content=dingxxxx%23qyapi_work_record`

### 后台还要配的四处（不配＝权限对了也不通）

| 配在哪 | 填什么 | 影响的场景 |
|---|---|---|
| 应用首页地址 | `http://你的地址:8787` | 钉钉客户端内免登 |
| 登录与分享 → 回调域名 | `你的地址:8787`（不带协议） | 浏览器扫码登录 |
| 机器人 → 消息接收地址 | `http://你的地址:8787/dingtalk/robot`，模式 HTTP 推送 | 群消息 @ 入库 |
| 服务器出口 IP | 服务器公网 IP | 否则报 `ip out of range` |

### 关于「降级」

没开 `qyapi_work_record` 时，代码会自动降级到 `/topapi/process/workrecord/task/query`（审批任务接口），
仍能拉到「待我审批」，但**拿不到历史「我发起 / 已办」**，状态只有「审批中」。
日志里会出现一条黄色提示，属于正常降级，不是故障。

---

## 三、飞书

| 项 | 值 |
|---|---|
| 在哪建 | [飞书开放平台](https://open.feishu.cn) → 开发者后台 → 创建企业自建应用 |
| 填什么 | `App ID` / `App Secret`（凭证与基础信息） |
| 权限（权限管理里搜） | `approval:approval`（审批实例/任务查询）、`contact:user.base:readonly`、`contact:department.base:readonly` |
| 同步什么 | 审批待办（审批中心 → 我审批的） |
| 额外步骤 | 应用要「创建版本并发布」，并加到企业可用范围 |

报错 `99991672` / `permission denied` = 权限没加或版本没发布。
拉不到人 = 通讯录权限没开，或应用可用范围没覆盖该部门。

---

## 四、GitHub

| 项 | 值 |
|---|---|
| 填什么 | Personal Access Token（+ 仓库 `owner/repo` 逗号分隔，或组织名） |
| Token 从哪来 | GitHub → Settings → Developer settings → Personal access tokens |
| 需要的范围 | Classic PAT：`repo`、`read:org`、`notifications`；Fine-grained：Issues / Pull requests / Metadata = Read |
| 同步什么 | Issue、Pull Request、通知 |
| Webhook（可选） | 仓库 Settings → Webhooks → `http://你的地址:8787/webhook/github`，Content type `application/json`，Secret 填平台配置里的「Webhook 密钥」（HMAC 验签） |

`401 Bad credentials` = token 过期或复制错；`404` = 仓库名写错或 token 无该仓库权限。

---

## 五、GitLab

| 项 | 值 |
|---|---|
| 填什么 | GitLab 地址（如 `https://gitlab.example.com`）、Access Token、组路径（可选） |
| Token 从哪来 | User Settings → Access Tokens，勾 `read_api` |
| 同步什么 | Issue、Merge Request |

`401` = token 无 `read_api`；`404` = 地址多了/少了 `/api/v4`。

---

## 六、Gitea / Gogs

| 项 | 值 |
|---|---|
| 填什么 | 实例地址、Token、组织（可选） |
| Token 从哪来 | Settings → Applications → Generate Token，勾 `read:issue`、`read:user`、`read:organization`（或全选） |
| 同步什么 | Issue、Pull Request |

---

## 七、禅道

| 项 | 值 |
|---|---|
| 填什么 | 禅道地址、API 账号、密码 |
| 前提 | 禅道 ≥ 15，已开启 REST API（`/api.php/v1`） |
| 同步什么 | 任务、Bug、待办 |

建议单独建一个只读账号跑同步，别用管理员账号。
`404` = 版本太低或 API 未开启；`401` = 账号密码错。

---

## 八、金蝶云星空

| 项 | 值 |
|---|---|
| 填什么 | 星空地址、账套ID（acct_id）、应用ID、应用密钥、接口用户 |
| 从哪来 | 星空 BOS → 集成管理 → 第三方系统登录授权 → 新建应用，拿到 AppId / AppSecret，并指定一个接口用户 |
| 同步什么 | 审批单据（待办） |

需要该接口用户对目标单据有查询权限，否则 `ExecuteBillQuery` 返回空。
连接失败先测：服务器能否访问星空地址、账套ID 是否含前后空格。

---

## 九、用友

| 项 | 值 |
|---|---|
| 填什么 | 开放平台地址、AppKey、AppSecret、接口用户编码、**待办接口路径** |
| 认证 | 固定 `POST {host}/iuap-api-auth/open-auth/selfAppAuth/getAccessToken` |
| 待办路径 | 按你的版本填，如 `/yonbip/xxx/todo/list`（YonBIP / YonSuite / U8 cloud 各不相同） |

路径没填或填错 → 同步日志明确提示「请在平台配置里填写待办接口路径」，不影响其他平台。

---

## 十、企业微信

代码里预留了凭证位（CorpID / 应用 Secret / AgentId），**同步逻辑尚未接入**（`wecom` 只有配置卡片）。
需要时可基于同一套 `records` 模型补一个适配器，参考 `feishu.py`。

---

## 十一、AI 能力需要的额外配置

AI 本身不依赖上面任何平台权限，但工具能不能用取决于数据在不在：

| 能力 | 额外要求 |
|---|---|
| 对话 / 周报 / 判断审批 | 配一个 OpenAI 兼容的大模型接口（地址 + Key + 模型名） |
| `send_email` | 配 SMTP 发件邮箱 |
| `submit_approval` | 钉钉审批模板（可用 `process_codes` 登记模板编码） |
| `send_dingtalk_msg` | 应用 AgentId + 消息通知权限（默认有） |

配置位置见 [config.md](config.md#四ai-模型与制度)。

---

## 十二、权限自检清单（复制去核对）

```
钉钉
[ ] 通讯录部门信息读权限        已开通/已发布
[ ] 通讯录部门成员读权限        已开通/已发布
[ ] OA 审批读权限 qyapi_work_record   已开通/已发布
[ ] 待办 Todo.Todo.Read（可选）
[ ] 知识库 Document.Workspace.Read + Document.Node.Read（可选）
[ ] 钉盘 Drive.Space.Read + Storage.File.Read（可选）
[ ] JSAPI 鉴权
[ ] 应用首页地址 = 实际访问地址
[ ] 回调域名 = host:port
[ ] 机器人接收地址 = http://host:port/dingtalk/robot
[ ] 服务器出口 IP 已加白
[ ] 已发布版本
```

网页上点 **平台配置 → 测试连接**，会以同样顺序逐项检查并直接给出申请链接。
