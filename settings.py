# -*- coding: utf-8 -*-
"""配置读写：平台凭证统一加前缀存 config 表，Secret 永不回传前端。

键位约定
  dingtalk: app_key / app_secret / corp_id / agent_id
  feishu:   app_id / app_secret
  github:   token / repos（逗号分隔 owner/repo）
  kingdee:  host / acct_id / app_id / app_secret / user
  wecom:    corp_id / secret / agent_id
  admin:    pass_hash
"""
import db

PLATFORM_FIELDS = {
    'dingtalk': [('app_key', 'AppKey'), ('app_secret', 'AppSecret'), ('corp_id', 'CorpId'),
                 ('agent_id', 'AgentId'),
                 ('public_url', '对外访问地址（扫码登录回调，如 http://192.168.1.10:8787）'),
                 ('admin_userid', '管理员钉钉 UserID'),
                 ('sso_admin', '钉钉单点登录即管理员（填 1 开启；留空 = 一律按员工处理，配置中心只认密码登录）'),
                 ('robot_only_at_me', '群消息只收「@我」的（填 1 开启；留空 = 群里 @机器人 的消息全都收）'),
                 ('notify_new_wait', '有新的等待回复时发钉钉工作通知（填 0 关闭；默认开）'),
                 ('process_codes', '额外审批模板编码（补「我发起 / 抄送我」用，逗号分隔的 PROC-xxx）'),
                 ('cc_per_run', '「抄送我」每轮最多核对多少条审批（默认 600，调小可缩短同步耗时）'),
                 ('cc_days', '「抄送我」增量同步天数（默认 20，首次自动按 120 天补齐）'),
                 ('notify_new_approval', '有新审批（待我审批 / 抄送我）时发钉钉工作通知（填 0 关闭；默认开）'),
                 ('notify_approval_days', '审批提醒只看最近多少天发起的新单（默认 7，避免补历史时轰炸）'),
                 ('notify_digest', '钉钉消息统一汇总：待办 / 审批 / 结果 / 等待回复 / @我 合成一条发（填 0 关闭；默认开）'),
                 ('notify_quiet', '钉钉消息免打扰时段，期间只攒不发（默认 22:00-08:00；填 - 关闭）')],
    'feishu':   [('app_id', 'App ID'), ('app_secret', 'App Secret')],
    'github':   [('token', 'Personal Access Token'), ('repos', '仓库（owner/repo，逗号分隔）'),
                 ('org', '组织（可选）'), ('webhook_secret', 'Webhook 密钥（可选）')],
    'gitlab':   [('host', 'GitLab 地址（如 https://gitlab.local）'), ('token', 'Access Token'), ('org', '组路径（可选）')],
    'gitea':    [('host', 'Gitea 地址'), ('token', 'Access Token'), ('org', '组织（可选）')],
    'zentao':   [('host', '禅道地址'), ('account', 'API 账号'), ('password', '密码')],
    'kingdee':  [('host', '星空地址'), ('acct_id', '账套ID'), ('app_id', '应用ID'), ('app_secret', '应用密钥'), ('user', '接口用户')],
    'yonyou':   [('host', '开放平台地址'), ('app_key', 'AppKey'), ('app_secret', 'AppSecret'),
                 ('ent_code', '企业编码（可选）'), ('user', '接口用户编码'), ('todo_path', '待办接口路径（可选）')],
    'wecom':    [('corp_id', 'CorpID'), ('secret', '应用 Secret'), ('agent_id', 'AgentId')],
    'notable':  [('bases', '多维表 Base 链接（逗号分隔，可粘贴 alidocs 分享链接或填 baseId）'),
                 ('notify_open', '新任务发钉钉工作通知（填 0 关闭；默认开，每个 Base 首次导入静默）')],
}
SECRET_KEYS = {'app_secret', 'token', 'secret', 'webhook_secret', 'password'}

# 展示顺序
PLATFORM_ORDER = ('dingtalk', 'feishu', 'github', 'gitlab', 'gitea', 'zentao', 'kingdee', 'yonyou', 'wecom', 'notable')

PLATFORM_NAME = {
    'dingtalk': '钉钉', 'feishu': '飞书', 'github': 'GitHub', 'gitlab': 'GitLab',
    'gitea': 'Gitea/Gogs', 'zentao': '禅道', 'kingdee': '金蝶云星空',
    'yonyou': '用友', 'wecom': '企业微信', 'notable': '钉钉多维表格',
}


def g(key, default=''):
    return str(db.cfg_all().get(key) or default).strip()


def pref(platform, key):
    return '%s:%s' % (platform, key)


def get_platform(platform):
    """返回该平台的全部配置（含明文 secret，仅服务端内部用）"""
    out = {}
    for k, _ in PLATFORM_FIELDS.get(platform, []):
        out[k] = g(pref(platform, k))
    # 钉钉兼容老键位
    if platform == 'dingtalk':
        out['app_key'] = out['app_key'] or g('app_key')
        out['app_secret'] = out['app_secret'] or g('app_secret')
        out['corp_id'] = out['corp_id'] or g('corp_id')
        out['user_id'] = g('dingtalk:user_id') or g('user_id')
    return out


def platform_ready(platform):
    cfg = get_platform(platform)
    need = {
        'dingtalk': ['app_key', 'app_secret'],
        'feishu': ['app_id', 'app_secret'],
        'github': ['token'],
        'gitlab': ['host', 'token'],
        'gitea': ['host', 'token'],
        'zentao': ['host', 'account', 'password'],
        'kingdee': ['host', 'acct_id', 'app_id', 'app_secret'],
        'yonyou': ['host', 'app_key', 'app_secret'],
        'wecom': ['corp_id', 'secret'],
        'notable': ['bases'],
    }.get(platform, [])
    return all(cfg.get(k) for k in need)


def platform_view(platform):
    """给前端的状态视图：Secret 只回显是否已填"""
    cfg = get_platform(platform)
    out = {'platform': platform, 'ready': platform_ready(platform), 'fields': []}
    for k, label in PLATFORM_FIELDS.get(platform, []):
        v = cfg.get(k, '')
        out['fields'].append({
            'key': k, 'label': label,
            'value': '' if k in SECRET_KEYS else v,
            'has': bool(v) if k in SECRET_KEYS else None,
        })
    return out


def all_platform_views():
    return [platform_view(p) for p in PLATFORM_ORDER]


def save_platform(platform, body):
    """保存某平台配置；secret 字段留空表示不修改"""
    if platform not in PLATFORM_FIELDS:
        raise ValueError('未知平台：%s' % platform)
    upd = {}
    for k, _ in PLATFORM_FIELDS[platform]:
        if k not in body:
            continue
        v = str(body.get(k) or '').strip()
        if k in SECRET_KEYS and not v:
            continue  # 留空 = 不修改
        upd[pref(platform, k)] = v
    if platform == 'dingtalk':
        # 兼容单用户老配置
        if 'user_id' in body:
            upd['user_id'] = str(body.get('user_id') or '').strip()
        for legacy in ('app_key', 'app_secret', 'corp_id'):
            if pref(platform, legacy) in upd:
                upd[legacy] = upd[pref(platform, legacy)]
    if platform == 'dingtalk' and 'interval' in body:
        upd['interval_min'] = str(int(body.get('interval') or 30))
    db.cfg_set(upd)
    return upd
