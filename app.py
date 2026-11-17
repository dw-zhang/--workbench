# -*- coding: utf-8 -*-
"""独立版 · 任务安排工作台 服务端（纯标准库，无第三方依赖）

启动：python3 app.py           默认 http://0.0.0.0:8787

环境变量
  WB_PORT         监听端口（默认 8787）
  WB_DB           SQLite 路径（默认 ./data/workbench.db）
  WB_STATIC       前端目录（默认 ./static）
  WB_SEED         1=首次启动导入演示数据
  WB_ADMIN_PASS   管理密码（首次启动写入，未设置则 admin123）
  WB_DEV_LOGIN    1=开启 /auth/dev-login（本地联调用，生产勿开）

权限模型
  管理员（密码登录）：配置平台凭证、成员管理、全局数据
  员工（钉钉免登）：只看自己 person_id 的数据；公共表（知识库/会议/分类/应用）共享
"""
import json
import mimetypes
import os
import re
import secrets
import threading
import time
import traceback
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote, quote

import auth
import ai_bot
import approval_agent
import db
import github as gh
import match
import notify
import projects as projects_mod
import settings
import sync as syncer
from dingtalk import Client as DingClient, DingError
from feishu import Client as FeishuClient, FeishuError
from gitea import Client as GiteaClient, GiteaError
from gitlab import Client as GitlabClient, GitlabError
from kingdee import Client as KingdeeClient, KingdeeError
from zentao import Client as ZentaoClient, ZentaoError

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.environ.get('WB_STATIC', os.path.join(HERE, 'static'))
PORT = int(os.environ.get('WB_PORT', '8787'))
SEED = os.environ.get('WB_SEED', '0') == '1'
DEV_LOGIN = os.environ.get('WB_DEV_LOGIN', '0') == '1'

# 扫码登录的 state（防重放）：内存保留 15 分钟，单进程够用
_OAUTH_STATES = {}


def _state_new():
    s = secrets.token_urlsafe(18)
    now = time.time()
    for k, v in list(_OAUTH_STATES.items()):
        if now - v > 900:
            _OAUTH_STATES.pop(k, None)
    _OAUTH_STATES[s] = now
    return s


def _state_ok(s):
    ts = _OAUTH_STATES.pop(s, None)
    return bool(ts) and (time.time() - ts) <= 900


# ── 初始化 ────────────────────────────────────────────────
def bootstrap():
    db.connect()
    seed_file = os.path.join(HERE, 'seed_schema.json')
    if os.path.exists(seed_file):
        schemas = json.load(open(seed_file, encoding='utf-8'))
        if not db.all_schemas():
            for database, info in schemas.items():
                db.put_schema(database, info.get('title', database), info.get('properties', []))
            print('[init] 已导入 %d 张表结构' % len(schemas))
        else:
            # 增量迁移：把 seed 里新增的字段 / 下拉选项补进已有表，不动已有字段顺序
            n_add = 0
            for database, info in schemas.items():
                cur = db.get_schema(database)
                props = cur.get('properties') or []
                if not props:
                    continue
                names = {p.get('name') for p in props}
                changed = False
                for p in info.get('properties', []):
                    if p.get('name') not in names:
                        props.append(p)
                        changed = True
                        n_add += 1
                        continue
                    # 合并 select 选项
                    if p.get('type') == 'select':
                        for cp in props:
                            if cp.get('name') != p.get('name'):
                                continue
                            cur_opts = cp.setdefault('config', {}).setdefault('options', [])
                            have = {o.get('text') for o in cur_opts}
                            for o in p.get('config', {}).get('options', []):
                                if o.get('text') not in have:
                                    cur_opts.append(o)
                                    changed = True
                            break
                if changed:
                    db.put_schema(database, cur.get('title') or database, props)
            if n_add:
                print('[init] 表结构增量迁移：新增 %d 个字段' % n_add)
    if SEED:
        demo = os.path.join(HERE, 'seed_data.json')
        if os.path.exists(demo) and sum(db.stats().values()) == 0:
            data = json.load(open(demo, encoding='utf-8'))
            for database, rows in data.items():
                for row in rows:
                    db.add_record(database, row, source='demo')
            print('[init] 已导入演示数据')
    auth.ensure_admin()
    # 老配置迁移：有 user_id 但没有人员 → 建一个“我”，并把历史无归属记录划给他
    cfg = db.cfg_all()
    if not db.persons_all():
        pc = settings.get_platform('dingtalk')
        if pc.get('user_id'):
            pid = match.ingest_contact('dingtalk', pc['user_id'],
                                       name=cfg.get('app_name') or '我')
            db.person_set(pid, status='bound', match_by='legacy')
            db.assign_unowned(pid)
            print('[init] 已按老配置创建人员：%s' % (cfg.get('app_name') or '我'))
    elif len(db.persons_all()) == 1:
        # 单人员场景：历史遗留数据直接归他，避免员工登录后看不到旧数据
        db.assign_unowned(db.persons_all()[0]['person_id'])


def status_payload(sess):
    cfg = db.cfg_all()
    admin = sess.get('kind') == 'admin'
    plat = []
    for p in settings.all_platform_views():
        item = {'platform': p['platform'], 'ready': p['ready'],
                'lastSync': cfg.get('last_sync:' + p['platform'], '')}
        # 配置接口信息（AppKey/CorpID/地址等）只给管理员；员工只看到平台名与就绪状态
        if admin:
            item['fields'] = p['fields']
        plat.append(item)
    # 审批四分类的能力标记：我发起/抄送我 依赖 OA 高级版专享接口，
    # sync 每次同步时探测并把结果写进配置，前端据此区分「无数据」和「未开通」
    ding_cfg = settings.get_platform('dingtalk')
    premium_ok = cfg.get('dingtalk:premium_ok', '') != '0'
    app_key = ding_cfg.get('app_key') or ''
    return {
        'role': sess.get('kind'),
        'name': sess.get('name', ''),
        'configured': settings.platform_ready('dingtalk'),
        'platforms': plat,
        'dingApproval': {
            'premium': premium_ok,
            'apply': ('https://open-dev.dingtalk.com/appscope/apply?content=%s%%23Premium.Workflow.ReadWrite.All'
                      % quote(app_key)) if app_key else '',
        },
        'intervalMin': int(cfg.get('interval_min') or 30),
        'autoSync': cfg.get('auto_sync', '1') == '1',
        'lastSync': cfg.get('last_sync') or '',
        'running': syncer.is_running(),
        'syncProgress': cfg.get('sync:progress') or '',
        'counts': db.stats(None if admin else (sess.get('person_id') or '__none__')),
        'globalTotal': sum(db.stats().values()),
        'persons': match.report() if admin else None,
        'logs': db.logs(20) if admin else [],
        'admin': admin,
    }


# ── 定时同步 ──────────────────────────────────────────────
def scheduler():
    # 计时起点 = 上一轮「跑完」的时间，不是「开始」的时间。
    # 否则一轮全量（200 人可能跑 40+ 分钟）结束时早已超过间隔，会被立刻再触发一轮，
    # 变成永不休息的连续全量同步。进程重启时用配置里的 last_sync 兜底，避免重启就狂跑。
    last_run = 0
    try:
        _ls = (db.cfg_all().get('last_sync') or '')[:19]
        if _ls:
            last_run = time.mktime(time.strptime(_ls, '%Y-%m-%dT%H:%M:%S'))
    except Exception:
        last_run = 0
    was_running = False
    while True:
        try:
            # 攒批发送：本地几个同步脚本连着调接口时，只合成一条消息，不刷屏
            notify.flush_due()
            cfg = db.cfg_all()
            auto = cfg.get('auto_sync', '1') == '1'
            ready = any(settings.platform_ready(p) for p in settings.PLATFORM_ORDER)
            gap = max(5, int(cfg.get('interval_min') or 30)) * 60
            # 有人刚登录 → 立刻给他单独跑一轮（登录时写入 dingtalk:sync_focus），
            # 这样「第一次打开工作台」不用等整轮 200 人跑完
            focus = (cfg.get('dingtalk:sync_focus') or '').strip()
            running = syncer.is_running()
            if was_running and not running:
                # 刚刚跑完 → 从现在重新计时，等满一个间隔再跑下一轮
                last_run = time.time()
                print('[sched] 上一轮同步已结束，%.0f 分钟后再次检查' % (gap / 60))
            was_running = running
            due = time.time() - last_run >= gap
            if (auto or focus) and ready and (focus or due) and not running:
                last_run = time.time()
                syncer.run_async()
                print('[sched] 触发同步%s' % ('（登录优先）' if focus else ''))
        except Exception:
            traceback.print_exc()
        time.sleep(30)


def _first_sync():
    time.sleep(3)
    if any(settings.platform_ready(p) for p in settings.PLATFORM_ORDER):
        if sum(db.stats().values()) == 0:
            syncer.run_async()


# ── 通讯录拉取 ────────────────────────────────────────────
def pull_contacts(platform):
    """从平台拉成员名单 → 人员库（手机/邮箱用于自动匹配）"""
    pc = settings.get_platform(platform)
    if not settings.platform_ready(platform):
        raise ValueError('%s 凭证未配置完整' % platform)
    n = 0
    if platform == 'dingtalk':
        cli = DingClient(pc['app_key'], pc['app_secret'], pc.get('corp_id', ''))
        for u in cli.all_users():
            match.ingest_contact('dingtalk', u['userid'], u.get('name', ''),
                                 u.get('mobile', ''), u.get('email', ''),
                                 {'unionid': u.get('unionid', ''), 'job_number': u.get('job_number', '')})
            n += 1
    elif platform == 'feishu':
        cli = FeishuClient(pc['app_id'], pc['app_secret'])
        for u in cli.users():
            match.ingest_contact('feishu', u['open_id'], u.get('name', ''),
                                 u.get('mobile', ''), u.get('email', ''),
                                 {'union_id': u.get('union_id', ''), 'job_number': u.get('job_number', '')})
            n += 1
    elif platform == 'kingdee':
        cli = KingdeeClient(pc['host'], pc['acct_id'], pc['app_id'], pc['app_secret'], pc.get('user', ''))
        for u in cli.users():
            match.ingest_contact('kingdee', u['account'], u.get('name', ''),
                                 u.get('mobile', ''), u.get('email', ''))
            n += 1
    elif platform == 'gitlab':
        cli = GitlabClient(pc['host'], pc['token'])
        for u in cli.users():
            match.ingest_contact('gitlab', u['account'], u.get('name', ''),
                                 u.get('mobile', ''), u.get('email', ''))
            n += 1
    elif platform == 'gitea':
        cli = GiteaClient(pc['host'], pc['token'], pc.get('org', ''))
        for u in cli.users():
            match.ingest_contact('gitea', u['account'], u.get('name', ''),
                                 u.get('mobile', ''), u.get('email', ''))
            n += 1
    elif platform == 'zentao':
        cli = ZentaoClient(pc['host'], pc.get('account', ''), pc.get('password', ''))
        for u in cli.users():
            match.ingest_contact('zentao', u['account'], u.get('name', ''),
                                 u.get('mobile', ''), u.get('email', ''))
            n += 1
    elif platform == 'github':
        raise ValueError('GitHub 没有通讯录，请在人员绑定里手工填写用户名')
    elif platform == 'yonyou':
        raise ValueError('用友暂无标准通讯录接口，请在人员绑定里手工填写用户编码')
    else:
        raise ValueError('暂不支持该平台通讯录')
    merged = match.run()
    db.log('contacts', True, n, '%s 通讯录拉取 %d 人，合并 %d' % (platform, n, merged['merged']))
    return {'pulled': n, 'match': merged}


# ── 钉钉文档正文：站内查看 / 编辑 ─────────────────────────
# 读：GET  /v1.0/doc/suites/documents/{dentryUuid}/blocks
# 改：PUT .../blocks/{blockId}  增：POST .../blocks  删：DELETE .../blocks/{blockId}
# 权限：企业存储文件读/写（与钉盘同步同一套）；operatorId = 当前用户 unionId，
# 钉钉按这个人在文档里的真实权限校验，绕过不了 ACL。
_DOC_EDITABLE = ('paragraph', 'heading', 'unorderedList', 'orderedList')
_DENTRY_MAP = {}          # spaceId -> {dentryId: uuid}
_DENTRY_MAP_TS = {}


def _ding_client():
    if not settings.platform_ready('dingtalk'):
        raise DingError('钉钉凭证未配置完整：请管理员在「应用接入 → 钉钉」补齐 AppKey/AppSecret/CorpId')
    cfg = settings.get_platform('dingtalk')
    return DingClient(cfg['app_key'], cfg['app_secret'], cfg.get('corp_id', ''))


def _doc_operator(cli, sess):
    """当前会话用户 → unionId（文档接口的 operatorId）。

    权限以钉钉为准：普通用户只用**本人**钉钉身份，绝不回退到管理员账号，
    否则会出现「本人钉钉里无权看的文档，在工作台里却能打开」的越权。
    只有管理员会话（密码登录，没有绑定人员）才用配置里的管理员账号。
    """
    ids = []
    pid = sess.get('person_id') or ''
    if pid:
        for a in db.accounts_of(pid, 'dingtalk'):
            uid = str(a.get('account_id') or '').strip()
            if uid:
                ids.append(uid)
    if sess.get('kind') == 'admin' and not pid:
        cfg = settings.get_platform('dingtalk')
        for k in ('admin_userid', 'user_id'):
            uid = str(cfg.get(k) or '').strip()
            if uid and uid not in ids:
                ids.append(uid)
    if not ids:
        raise DingError('当前账号还没绑定钉钉身份，请用钉钉扫码重新登录后再看资料')
    last = ''
    for uid in ids:
        try:
            un = cli.unionid_of(uid)
        except DingError as e:
            last = str(e)
            continue
        if un:
            return un
    raise DingError(last or '没找到当前账号对应的钉钉 unionId，请重新登录后再试')


def _doc_uuid_of(cli, union, did):
    """spaceId:dentryId → dentryUuid（老数据没有「文档UUID」时兜底，按空间缓存）"""
    parts = str(did or '').split(':')
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return ''
    sid, dentry = parts[0], ':'.join(parts[1:])
    now = time.time()
    if sid in _DENTRY_MAP and now - _DENTRY_MAP_TS.get(sid, 0) < 1800:
        return _DENTRY_MAP[sid].get(dentry, '')
    m, token = {}, None
    for _ in range(20):
        q = {'unionId': union}
        opt = {'maxResults': 50}
        if token:
            opt['nextToken'] = token
        d = cli._newapi('/v1.0/storage/spaces/%s/dentries/listAll' % quote(sid), q,
                        method='POST', payload={'option': opt})
        for it in d.get('dentries') or []:
            if it.get('id'):
                m[str(it['id'])] = str(it.get('uuid') or '')
        token = d.get('nextToken')
        if not token:
            break
    _DENTRY_MAP[sid] = m
    _DENTRY_MAP_TS[sid] = now
    return m.get(dentry, '')


def _doc_block(b):
    """规范化一个块：只暴露渲染需要的信息，并判断能否原地编辑

    可编辑条件：段落/标题/列表 + 纯文本（没有行内图片、行内代码、链接等 elements），
    这样保存时只回写文字，不会把行内样式/图片冲掉。
    """
    t = str(b.get('blockType') or '')
    p = b.get(t) or {}
    text = p.get('text')
    rich = bool(p.get('elements'))
    if text is None:
        buf = []
        for e in p.get('elements') or []:
            et = e.get('elementType')
            if et == 'text':
                buf.append(((e.get('text') or {}).get('text')) or '')
            elif et == 'image':
                buf.append('［图片］')
        text = ''.join(buf)
    level = ''
    if t == 'heading':
        level = str(p.get('level') or 'heading-2').replace('heading-', '') or '2'
    item = {'id': str(b.get('id') or ''), 'index': b.get('index'), 'type': t,
            'text': str(text or ''), 'level': level, 'rich': rich,
            'table': p.get('cells') if t == 'table' else None}
    item['editable'] = (not rich) and t in _DOC_EDITABLE
    return item


def _doc_err(e):
    """把钉钉的英文错误翻成人话，避免界面上出现一串 requestid"""
    raw = str(getattr(e, 'raw', '') or '')
    msg = str(e)
    low = (msg + raw).lower()
    if 'forbidden' in low or 'permission' in low or '403' in msg or 'no.priviledge' in low:
        return '你在钉钉里没有这份文档的权限（需要文档所有者或知识库管理员给你授权）'
    if 'encoded dockey' in low or '无权访问' in raw or '节点不存在' in raw:
        return '钉钉里查不到这份文档：你不在该知识库/文件夹的可见范围内（让文档所有者共享给你后再试）'
    if 'invalid' in low and 'dentryuuid' in low:
        return '这份资料不是钉钉新版在线文档（可能是普通文件或旧版文档），站内暂不支持直接编辑'
    return msg + ('｜' + raw[:150] if raw else '')


def _doc_element(btype, text, level=''):
    btype = btype if btype in ('heading', 'unorderedList', 'orderedList') else 'paragraph'
    if btype == 'heading':
        # level 必须是正整数（1/2/3），传 'heading-3' 这类字符串会被钉钉拒掉
        try:
            lv = int(str(level or '2').replace('heading-', '').strip())
        except Exception:
            lv = 2
        lv = max(1, min(3, lv))
        return {'blockType': 'heading', 'heading': {'level': lv, 'text': text}}
    if btype in ('unorderedList', 'orderedList'):
        return {'blockType': btype, btype: {'text': text}}
    return {'blockType': 'paragraph', 'paragraph': {'text': text}}


# ── 统计分析 ──────────────────────────────────────────────
def analytics(person_id=None):
    rows = db.query(db.TABLES['ding'], size=100000, person_id=person_id)['results']
    by_type, monthly, quarterly = {}, {}, {}
    contract = {'total': 0, 'with_amount': 0, 'without_amount': 0, 'amount': 0.0}
    total_amount = 0.0
    for r in rows:
        t = str(r.get('审批类型') or '其他')
        amt = r.get('金额')
        try:
            amt = float(amt) if amt not in (None, '') else 0.0
        except Exception:
            amt = 0.0
        d = str(r.get('发起时间') or '')[:10]
        month = d[:7] if len(d) >= 7 else '未知'
        if len(d) >= 7 and d[5:7].isdigit():
            quarter = '%s-Q%d' % (d[:4], (int(d[5:7]) - 1) // 3 + 1)
        else:
            quarter = '未知'

        it = by_type.setdefault(t, {'count': 0, 'amount': 0.0})
        it['count'] += 1
        it['amount'] += amt
        monthly.setdefault(month, {'count': 0, 'amount': 0.0})
        monthly[month]['count'] += 1
        monthly[month]['amount'] += amt
        quarterly.setdefault(quarter, {'count': 0, 'amount': 0.0})
        quarterly[quarter]['count'] += 1
        quarterly[quarter]['amount'] += amt
        total_amount += amt

        if t == '合同':
            contract['total'] += 1
            if amt > 0:
                contract['with_amount'] += 1
                contract['amount'] += amt
            else:
                contract['without_amount'] += 1

    tasks = db.query(db.TABLES['task'], size=100000, person_id=person_id)['results']
    todo_open = sum(1 for t in tasks if str(t.get('状态') or '') != '已完成')
    return {
        'approvals': len(rows),
        'totalAmount': round(total_amount, 2),
        'byType': [{'type': k, 'count': v['count'], 'amount': round(v['amount'], 2)}
                   for k, v in sorted(by_type.items(), key=lambda x: -x[1]['count'])],
        'monthly': [{'month': k, 'count': v['count'], 'amount': round(v['amount'], 2)}
                    for k, v in sorted(monthly.items())],
        'quarterly': [{'quarter': k, 'count': v['count'], 'amount': round(v['amount'], 2)}
                      for k, v in sorted(quarterly.items())],
        'contract': dict(contract, amount=round(contract['amount'], 2)),
        'todoOpen': todo_open,
    }


# ── HTTP ─────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = 'Workbench/2.0'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        # send_error 走 log_error("code %d, message %s", code, msg)，args[0] 是状态码不是请求行；
        # 之前直接 `'/api/' in args[0]` 会 TypeError，把所有 4xx/5xx 日志吞掉
        try:
            line = fmt % args
        except Exception:
            line = ' '.join(str(a) for a in args)
        if 'code ' in line and 'message ' in line:
            print('[err]', line)  # send_error：404/400/500 必须现形
        if '/api/' in line or '/auth/' in line:
            print('[api]', line)

    # -- helpers --
    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        raw = body.encode('utf-8') if isinstance(body, str) else body
        try:
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # 客户端中途关掉了页面/刷新，写不回去是正常的，别刷 500 堆栈

    def _json_body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception:
            return {}

    def _static(self, path):
        if path in ('/', ''):
            path = '/index.html'
        fp = os.path.normpath(os.path.join(STATIC, unquote(path.lstrip('/'))))
        # 旧版多页链接兜底：tasks.html / waiting.html 这些原版文件已不存在，
        # 统一交回 index.html，由前端把 pathname 换成对应的 hash 路由，
        # 避免点「查看更多」直接甩一个 404
        if (not fp.startswith(STATIC) or not os.path.isfile(fp)) and path.lower().endswith('.html'):
            fp = os.path.join(STATIC, 'index.html')
        if not fp.startswith(STATIC) or not os.path.isfile(fp):
            return self._send(404, 'Not Found', 'text/plain; charset=utf-8')
        ctype = mimetypes.guess_type(fp)[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype in ('application/javascript', 'application/json'):
            ctype += '; charset=utf-8'
        with open(fp, 'rb') as f:
            data = f.read()
        try:
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    # -- 登录跳转 / 扫码回调 --
    def _base_url(self):
        """对外访问地址：优先用管理页里配置的 public_url，其次按请求 Host 推断"""
        cfg = settings.get_platform('dingtalk')
        b = (cfg.get('public_url') or '').strip().rstrip('/')
        if b:
            return b
        if not b:
            b = (settings.g('site:public_url') or '').strip().rstrip('/')
        if b:
            return b
        proto = (self.headers.get('X-Forwarded-Proto') or 'http').split(',')[0].strip() or 'http'
        host = (self.headers.get('Host') or ('127.0.0.1:%d' % PORT)).strip()
        return '%s://%s' % (proto, host)

    def _redirect(self, url):
        self.send_response(302)
        self.send_header('Location', url)
        self.send_header('Content-Length', '0')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    def _login_error(self, title, detail):
        html = (
            '<!doctype html><meta charset="utf-8"><title>%s</title>'
            '<body style="margin:0;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;'
            'background:#f8fafc;display:flex;align-items:center;justify-content:center;height:100vh">'
            '<div style="background:#fff;border:1px solid #e5e7eb;border-radius:16px;padding:26px 28px;'
            'max-width:520px;box-shadow:0 18px 50px rgba(15,23,42,.10)">'
            '<h2 style="margin:0 0 8px;font-size:17px;color:#111827">%s</h2>'
            '<p style="margin:0 0 16px;font-size:13px;line-height:1.7;color:#4b5563;word-break:break-all">%s</p>'
            '<a href="/" style="display:inline-block;padding:9px 16px;background:#2563eb;color:#fff;'
            'border-radius:9px;text-decoration:none;font-size:13px">返回登录页</a>'
            '</div></body>') % (
            title.replace('<', '&lt;'), title.replace('<', '&lt;'),
            (detail or '').replace('&', '&amp;').replace('<', '&lt;'))
        return self._send(200, html, 'text/html; charset=utf-8')

    def _auth_qr(self):
        """跳转到钉钉扫码登录页（PC 浏览器可用，无需钉钉容器）"""
        cfg = settings.get_platform('dingtalk')
        if not (cfg.get('app_key') and cfg.get('app_secret')):
            return self._login_error('钉钉扫码登录未就绪',
                                     '管理员尚未配置钉钉 AppKey / AppSecret，请先用管理密码登录并在「平台配置 → 钉钉」中填写。')
        cli = DingClient(cfg['app_key'], cfg['app_secret'], cfg.get('corp_id', ''))
        redirect_uri = self._base_url() + '/auth/dingtalk/callback'
        return self._redirect(cli.oauth_url(redirect_uri, _state_new()))

    def _auth_callback(self, q):
        """钉钉授权回调：code → 用户身份 → 会话 token → 回首页"""
        code = (q.get('code') or '').strip()
        state = (q.get('state') or '').strip()
        err = (q.get('error') or '').strip()
        if err:
            return self._login_error('钉钉授权未完成', '钉钉返回：%s（%s）' % (err, q.get('error_description') or ''))
        if state and not _state_ok(state):
            return self._login_error('登录链接已过期', '请回到登录页重新点「钉钉扫码登录」。')
        if not code:
            return self._login_error('未拿到授权码', '扫码未完成或已取消，请重新扫码。')
        try:
            sess, person = auth.dingtalk_qr_login(code)
        except DingError as e:
            return self._login_error('钉钉登录失败', str(e))
        except Exception as e:
            traceback.print_exc()
            return self._login_error('钉钉登录异常', str(e))
        print('[auth] 扫码登录成功：%s' % (person.get('name') or person.get('person_id')))
        return self._redirect('/?token=' + quote(sess['token']))

    def _sess(self, require_admin=False, q=None):
        s = auth.current(self.headers, (q or {}).get('token', ''))
        if not s:
            self._send(401, {'error': '未登录', 'needLogin': True})
            return None
        if require_admin and s.get('kind') != 'admin':
            self._send(403, {'error': '需要管理员权限'})
            return None
        return s

    # -- routes --
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        try:
            if p == '/api/ping':
                # 匿名可访问（健康检查 / 登录页判断）：只回「有没有配钉钉」和对外地址，
                # 不回企业标识。CorpId / AppKey 走 /api/ding/appinfo，登录后才下发。
                dc = settings.get_platform('dingtalk')
                return self._send(200, {'ok': True, 'ts': db.now(),
                                        'ding_login': bool(dc.get('corp_id') and dc.get('app_key')),
                                        'public_url': self._base_url()})
            if p == '/api/ding/appinfo':
                # 登录后才有意义：前端拼审批 deep link 需要 corpId + appKey（Secret 永不下发）
                if not self._sess(q=q):
                    return
                dc = settings.get_platform('dingtalk')
                return self._send(200, {'corp_id': dc.get('corp_id', ''),
                                        'app_key': dc.get('app_key', ''),
                                        'agent_id': dc.get('agent_id', '')})
            if p == '/api/ai/mycfg':
                # 每人自己的 AI 模型配置：登录即可读（Key 只回本人）
                s = self._sess(q=q)
                if not s:
                    return
                pid = s.get('person_id') or ''
                c = ai_bot.person_cfg(pid)
                g = ai_bot.ai_cfg()
                mine = None
                if c:
                    mine = {'base': c.get('base'), 'model': c.get('model') or '',
                            'name': c.get('name') or '', 'key': c.get('key') or ''}
                return self._send(200, {'cfg': mine,
                                        'has_global': bool(g and g.get('src') != 'person'),
                                        'global_model': (g or {}).get('model', '') if g and g.get('src') == 'global' else ''})
            if p == '/api/jsapi-config':
                # 免登前置：dd.config 需要的签名（公开，只暴露 corpId/agentId/签名，不含任何密钥）
                dc = settings.get_platform('dingtalk')
                missing = [k for k in ('app_key', 'app_secret', 'corp_id') if not dc.get(k)]
                if missing:
                    return self._send(200, {'ready': False,
                                            'reason': '管理员尚未配置钉钉凭证：缺少 ' + ' / '.join(missing)})
                pgurl = (q.get('url') or '').strip()
                if not pgurl:
                    return self._send(400, {'ready': False, 'reason': '缺少 url 参数'})
                try:
                    cli = DingClient(dc['app_key'], dc['app_secret'], dc.get('corp_id', ''))
                    cfg = cli.jsapi_sign(pgurl, dc.get('agent_id', ''))
                    cfg['ready'] = True
                    return self._send(200, cfg)
                except DingError as e:
                    return self._send(200, {'ready': False, 'reason': str(e)})
            if p == '/favicon.ico':
                svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
                       '<rect width="32" height="32" rx="7" fill="#2563eb"/>'
                       '<path d="M9 17l4 4 10-10" stroke="#fff" stroke-width="3" fill="none" '
                       'stroke-linecap="round" stroke-linejoin="round"/></svg>')
                return self._send(200, svg, 'image/svg+xml')

            if p == '/auth/qr':
                return self._auth_qr()
            if p == '/auth/dingtalk/callback':
                return self._auth_callback(q)

            if p == '/auth/me':
                s = self._sess(q=q)
                if not s:
                    return
                person = db.person_get(s.get('person_id') or '') if s.get('person_id') else None
                return self._send(200, {'kind': s['kind'], 'name': s.get('name', ''),
                                        'person': person, 'person_id': s.get('person_id', '')})
            if p == '/auth/logout':
                tok = auth.token_of(self.headers, q.get('token', ''))
                if tok:
                    db.session_delete(tok)
                return self._send(200, {'ok': True})
            if p == '/auth/dev-login':
                if not DEV_LOGIN:
                    return self._send(404, {'error': 'not found'})
                pid = q.get('person_id') or (db.persons_all()[0]['person_id'] if db.persons_all() else '')
                if not pid:
                    return self._send(400, {'error': '没有人员记录'})
                pp = db.person_get(pid) or {}
                tok, exp = db.session_create('user', pid, pp.get('name') or '')
                return self._send(200, {'token': tok, 'expires': exp, 'kind': 'user',
                                        'name': pp.get('name') or '', 'person_id': pid})

            if p == '/api/schema':
                if not self._sess(q=q):
                    return
                return self._send(200, db.get_schema(q.get('db', '')))
            if p == '/api/query':
                s = self._sess(q=q)
                if not s:
                    return
                if s['kind'] == 'admin':
                    target = q.get('person') or None
                else:
                    target = s.get('person_id') or ''
                    if not target:  # 未绑定人员的会话：不返回任何数据，避免退化成“看全量”
                        return self._send(200, {'results': [], 'hasMore': False, 'nextCursor': None})
                flt = None
                if q.get('filter'):
                    try:
                        flt = json.loads(q['filter'])
                    except Exception:
                        flt = None
                try:
                    _size = max(1, min(100000, int(q.get('size') or 200)))
                except Exception:
                    _size = 200
                return self._send(200, db.query(
                    q.get('db', ''), q.get('sort') or None, q.get('dir') or 'ascending',
                    _size, q.get('cursor'), person_id=target, filters=flt))
            if p == '/api/aggregate':
                s = self._sess(q=q)
                if not s:
                    return
                if s['kind'] == 'admin':
                    target = q.get('person') or None
                else:
                    target = s.get('person_id') or ''
                    if not target:
                        return self._send(200, {'results': []})
                aggs = None
                if q.get('aggs'):
                    try:
                        aggs = json.loads(q['aggs'])
                    except Exception:
                        aggs = None
                return self._send(200, db.aggregate(q.get('db', ''), aggs or [], person_id=target))
            if p == '/api/doc':
                s = self._sess(q=q)
                if not s:
                    return
                try:
                    cli = _ding_client()
                    union = _doc_operator(cli, s)
                    uuid = (q.get('uuid') or '').strip()
                    did = (q.get('did') or '').strip()
                    if not uuid and did:
                        uuid = _doc_uuid_of(cli, union, did)
                    if not uuid:
                        return self._send(400, {'error': '这份资料在钉钉里不是在线文档，站内无法直接查看正文'})
                    blocks = cli.doc_blocks(uuid, union)
                except DingError as e:
                    return self._send(400, {'error': _doc_err(e)})
                return self._send(200, {'ok': True, 'uuid': uuid,
                                        'title': q.get('name') or '',
                                        'url': cli.doc_url(uuid),
                                        'blocks': [_doc_block(b) for b in blocks]})
            if p == '/api/status':
                s = self._sess(q=q)
                if not s:
                    return
                return self._send(200, status_payload(s))
            if p == '/api/logs':
                if not self._sess(require_admin=True, q=q):
                    return
                return self._send(200, {'logs': db.logs(int(q.get('n') or 30))})
            # ── 审批 Agent ──
            if p == '/api/judge/pending':
                s = self._sess(q=q)
                if not s:
                    return
                target = q.get('person') if s['kind'] == 'admin' else s.get('person_id')
                items = approval_agent.pending(target or '', int(q.get('limit') or 8))
                for it in items:
                    old = db.judge_get(it['pid'], target or '')
                    if old:
                        it['verdict'] = old.get('verdict')
                        it['summary'] = old.get('payload', {}).get('summary')
                return self._send(200, {'items': items})
            if p == '/api/judge/stats':
                s = self._sess(q=q)
                if not s:
                    return
                target = q.get('person') if s['kind'] == 'admin' else s.get('person_id')
                return self._send(200, db.judge_stats(int(q.get('days') or 30), target or ''))
            if p == '/api/judge/policy':
                if not self._sess(require_admin=True, q=q):
                    return
                return self._send(200, {'policy': db.cfg_get(approval_agent.POLICY_KEY) or ''})
            if p == '/api/platforms':
                if not self._sess(require_admin=True, q=q):
                    return
                return self._send(200, {'platforms': settings.all_platform_views()})
            if p == '/api/persons':
                if not self._sess(require_admin=True, q=q):
                    return
                return self._send(200, persons_payload())
            if p == '/api/projects':
                s = self._sess(q=q)
                if not s:
                    return
                return self._send(200, projects_list(s))
            if p == '/api/projects/detail':
                s = self._sess(q=q)
                if not s:
                    return
                d = project_detail(q.get('id') or '', s)
                if d is None:
                    return self._send(404, {'error': '项目不存在'})
                return self._send(200, d)
            if p == '/api/analytics':
                s = self._sess(q=q)
                if not s:
                    return
                if s['kind'] == 'admin':
                    target = q.get('person') or None
                else:
                    target = s.get('person_id') or ''
                    if not target:
                        return self._send(200, analytics('__none__'))
                return self._send(200, analytics(target))
            return self._static(p)
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {'error': str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        body = self._json_body()
        try:
            # ── 公开：登录 / 免登 / webhook ──
            if p == '/auth/admin':
                r = auth.admin_login(body.get('password') or '')
                if not r:
                    return self._send(401, {'error': '密码不正确'})
                return self._send(200, r)
            if p == '/auth/dingtalk':
                try:
                    sess, person = auth.dingtalk_sso(body.get('code'))
                except DingError as e:
                    return self._send(401, {'error': str(e)})
                return self._send(200, sess)
            if p == '/dingtalk/robot':
                return self._robot_callback(body)
            if p == '/webhook/github':
                return self._github_webhook(body)

            s = self._sess(q=q)
            if not s:
                return
            if p == '/api/records':
                pid = s.get('person_id') if s.get('kind') == 'user' else body.get('person_id')
                rid = db.add_record(body.get('db', ''), body.get('properties') or {}, source='user',
                                    person_id=pid)
                return self._send(200, {'id': rid})
            if p == '/api/schedule/create':
                # 新建日程：写工作台日程表 + 回传钉钉日历（写入即出现在钉钉「日程」）
                pid = s.get('person_id') or ''
                if not pid:
                    return self._send(400, {'error': '当前账号未绑定工作台人员'})
                title = str(body.get('title') or '').strip()
                date = str(body.get('date') or '').strip()
                tm = str(body.get('time') or '').strip() or '09:00'
                try:
                    hours = max(0.5, float(body.get('hours') or 1))
                except (TypeError, ValueError):
                    hours = 1.0
                desc = str(body.get('desc') or '').strip()
                if not title or not date:
                    return self._send(400, {'error': '日程事项和日期必填'})
                import datetime as _dt
                try:
                    sd = _dt.datetime.strptime('%s %s' % (date, tm), '%Y-%m-%d %H:%M')
                except ValueError:
                    return self._send(400, {'error': '日期或时间格式不对'})
                ed = sd + _dt.timedelta(hours=hours)
                start_str = sd.strftime('%Y-%m-%dT%H:%M:%S')
                props = {'日程事项': title[:60], '开始时间': start_str,
                         '时长': ('%g小时' % hours) if hours else '1小时',
                         '来源应用': '工作台'}
                if desc:
                    props['说明'] = desc[:200]
                rid = db.add_record(db.TABLES['sched'], props, source='user',
                                    person_id=pid)
                pushed, err = False, ''
                try:
                    cli = _ding_client()
                    uni = ''
                    for a in db.accounts_of(pid, 'dingtalk'):
                        uid = str(a.get('account_id') or '').strip()
                        if uid:
                            uni = cli.user_by_id(uid).get('unionid') or ''
                            if uni:
                                break
                    if uni:
                        eid = cli.event_create(uni, title,
                                               sd.strftime('%Y-%m-%dT%H:%M:%S'),
                                               ed.strftime('%Y-%m-%dT%H:%M:%S'), desc)
                        if eid:
                            pushed = True
                            try:
                                db.update_record(db.TABLES['sched'], rid,
                                                 {'事件ID': eid}, person_id=pid)
                            except Exception:
                                pass
                    else:
                        err = '未找到钉钉账号绑定'
                except Exception as e:
                    err = str(e)[:120]
                return self._send(200, {'ok': True, 'id': rid,
                                        'dingtalk': pushed,
                                        'dingtalk_err': err if not pushed else ''})
            if p == '/api/judge':
                # 审批 Agent：读单 → 比对历史与制度 → 给结论。只出建议，不提交任何操作
                s = self._sess(q=q)
                pidv = str(body.get('pid') or '').strip()
                if not pidv:
                    return self._send(400, {'ok': False, 'error': '缺少审批编号'})
                target = (body.get('person_id') or '') if s['kind'] == 'admin' else (s.get('person_id') or '')
                r = approval_agent.judge(pidv, target, s.get('name') or '',
                                         bool(body.get('force')))
                return self._send(200 if r.get('ok') else 400, r)
            if p == '/api/judge/feedback':
                s = self._sess(q=q)
                pidv = str(body.get('pid') or '').strip()
                if not pidv:
                    return self._send(400, {'ok': False, 'error': '缺少审批编号'})
                target = (body.get('person_id') or '') if s['kind'] == 'admin' else (s.get('person_id') or '')
                n = db.judge_feedback(pidv, bool(body.get('adopted')), target)
                return self._send(200, {'ok': True, 'updated': n})
            if p == '/api/judge/policy':
                if not self._sess(require_admin=True, q=q):
                    return
                db.cfg_set(approval_agent.POLICY_KEY, str(body.get('policy') or '')[:20000])
                return self._send(200, {'ok': True})
            if p == '/api/ai/chat':
                # 网页对话统一走服务端大模型链路（与钉钉机器人一致：个人配置优先 + 工具调用）
                pid = s.get('person_id') or ''
                if not pid:
                    return self._send(400, {'error': '当前账号未绑定工作台人员'})
                content = str(body.get('content') or '').strip()
                if not content:
                    return self._send(400, {'error': '内容为空'})
                cfg = ai_bot.ai_cfg(pid)
                if not cfg:
                    return self._send(200, {'reply': '', 'reason': 'no_model'})
                name = s.get('name') or ''
                try:
                    reply = ai_bot.llm_reply(content, pid, name)
                except Exception as e:
                    return self._send(200, {'reply': 'AI 调用出错：%s' % e})
                return self._send(200, {'reply': reply or '', 'reason': 'ok' if reply else 'empty'})
            if p == '/api/ai/mycfg':
                # 每人保存自己的 AI 模型配置（OpenAI 兼容接口），机器人/前端即时生效
                pid = s.get('person_id') or ''
                if not pid:
                    return self._send(400, {'error': '当前账号未绑定工作台人员，无法保存个人 AI 配置'})
                if body.get('clear'):
                    db.cfg_set({ai_bot.PERSON_CFG_KEY % pid: ''})
                    return self._send(200, {'ok': True, 'cleared': True})
                # 测试连通：走服务端真实请求（与钉钉机器人调用路径一致），返回供应商原始响应
                if body.get('test'):
                    base = str(body.get('base') or '').strip().rstrip('/')
                    key = str(body.get('key') or '').strip()
                    model = str(body.get('model') or '').strip() or 'gpt-4o-mini'
                    if not key:
                        saved = ai_bot.person_cfg(pid)
                        if saved:
                            base = base or str(saved.get('base') or '').rstrip('/')
                            key = str(saved.get('key') or '')
                            model = model or str(saved.get('model') or '') or model
                    if not base.startswith('http') or not key:
                        return self._send(400, {'error': '缺少接口地址或 API Key'})
                    fp = key[:6] + '…' + key[-4:] + '（%d 位）' % len(key)
                    try:
                        import urllib.request as _u
                        _req = _u.Request(base + '/chat/completions', data=json.dumps(
                            {'model': model, 'messages': [{'role': 'user', 'content': '回复ok'}],
                             'max_tokens': 10}, ensure_ascii=False).encode('utf-8'),
                            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
                        with _u.urlopen(_req, timeout=30) as _r:
                            _d = json.load(_r)
                        _txt = ((_d.get('choices') or [{}])[0].get('message') or {}).get('content', '')
                        return self._send(200, {'ok': True, 'fp': fp, 'model': model,
                                                'reply': str(_txt)[:60]})
                    except Exception as e:
                        detail = ''
                        resp = getattr(e, 'read', None)
                        if callable(resp):
                            try:
                                detail = e.read().decode('utf-8', 'replace')[:400]
                            except Exception:
                                detail = ''
                        return self._send(200, {'ok': False, 'fp': fp, 'model': model,
                                                'error': '%s %s' % (type(e).__name__, e),
                                                'detail': detail})
                base = str(body.get('base') or '').strip().rstrip('/')
                key = str(body.get('key') or '').strip()
                model = str(body.get('model') or '').strip()
                name = str(body.get('name') or '').strip() or '我的 AI 模型'
                if not base.startswith('http'):
                    return self._send(400, {'error': '接口地址需要以 http(s) 开头，如 https://api.deepseek.com'})
                if not key:
                    return self._send(400, {'error': 'API Key 必填'})
                db.cfg_set({ai_bot.PERSON_CFG_KEY % pid: json.dumps(
                    {'base': base, 'key': key, 'model': model, 'name': name}, ensure_ascii=False)})
                return self._send(200, {'ok': True})
            if p == '/api/wait/upsert':
                # 本地「@我」同步脚本专用：按 消息ID（回落 事项名称+发起时间）去重写入
                if s['kind'] != 'admin':
                    return self._send(403, {'error': '需要管理员权限'})
                items = body.get('items') or []
                if not isinstance(items, list):
                    return self._send(400, {'error': 'items 需要是数组'})
                table = db.TABLES['wait']
                # 记录归属：管理员会话没有 person_id 时，挂到钉钉配置账号对应的人
                # （否则员工身份登录看不到这些记录）
                target_pid = s.get('person_id') or self._robot_person(
                    settings.get_platform('dingtalk'))
                st = {'new': 0, 'upd': 0, 'skip': 0}
                new_summs = []   # (时间, 事项, 发送人) 新增条目摘要，用于钉钉通知
                try:
                    rows = db.query(table, size=5000).get('results') or []
                except Exception:
                    rows = []
                by_mid, by_key = {}, {}
                for r in rows:
                    mid = str(r.get('消息ID') or '').strip()
                    if mid:
                        by_mid[mid] = r
                    key = (str(r.get('事项名称') or '').strip(),
                           str(r.get('发起时间') or '')[:16])
                    by_key[key] = r
                for it in items:
                    if not isinstance(it, dict):
                        st['skip'] += 1
                        continue
                    name = str(it.get('事项名称') or '').strip()
                    mid = str(it.get('消息ID') or '').strip()
                    if not name:
                        st['skip'] += 1
                        continue
                    props = {}
                    for k in ('事项名称', '来源', '发起时间', '状态', '备注',
                              '处理链接', '消息ID', '群名称', '发送人'):
                        v = it.get(k)
                        if v is not None and str(v).strip() != '':
                            props[k] = v
                    key = (name, str(it.get('发起时间') or '')[:16])
                    hit = by_mid.get(mid) if mid else None
                    if not hit:
                        hit = by_key.get(key)
                    if hit:
                        rid = hit.get('record_id') or hit.get('_id')
                        # 手工收口（标记已回复 / 关闭）的事项不再被同步覆盖回「等待回复」
                        if str(hit.get('状态') or '') in ('已关闭', '已回复'):
                            props.pop('状态', None)
                        if props and rid:
                            db.update_record(table, rid, props, person_id=target_pid)
                            st['upd'] += 1
                        else:
                            st['skip'] += 1
                    else:
                        db.add_record(table, props, source='dingtalk',
                                      person_id=target_pid)
                        st['new'] += 1
                        new_summs.append((str(it.get('发起时间') or '')[:16],
                                          name[:50], str(it.get('发送人') or '')))
                if st['new'] or st['upd']:
                    db.log('wait', True, st['new'] + st['upd'],
                           '同步钉钉@我消息：新增 %d / 更新 %d' % (st['new'], st['upd']))
                # 有新增 → 进「钉钉消息」队列，由 notify 按人合成一条发出
                if st['new'] and settings.g('dingtalk:notify_new_wait', '1') != '0':
                    try:
                        for _t, _name, _sender in new_summs:
                            notify.push(target_pid, 'at' if _sender else 'wait',
                                        _name, self._base_url(), extra=_sender)
                    except Exception as e:
                        db.log('notify', False, st['new'], '钉钉消息登记失败：%s' % e)
                        print('[notify] 登记失败：', e)
                return self._send(200, {'ok': True, **st})
            if p == '/api/todo/upsert':
                # 本地「待办」同步脚本专用：按 钉钉任务ID 去重写入
                # （钉钉个人待办只有本人授权的 dws 能读，应用 API 读不到）
                if s['kind'] != 'admin':
                    return self._send(403, {'error': '需要管理员权限'})
                items = body.get('items') or []
                if not isinstance(items, list):
                    return self._send(400, {'error': 'items 需要是数组'})
                table = db.TABLES['task']
                target_pid = (str(body.get('person_id') or '').strip()
                              or s.get('person_id')
                              or self._robot_person(settings.get_platform('dingtalk')))
                st = {'new': 0, 'upd': 0, 'skip': 0}
                new_todos = []
                try:
                    rows = db.query(table, size=5000).get('results') or []
                except Exception:
                    rows = []
                by_tid = {}
                for r in rows:
                    tid = str(r.get('钉钉任务ID') or '').strip()
                    if tid:
                        by_tid[tid] = r
                for it in items:
                    if not isinstance(it, dict):
                        st['skip'] += 1
                        continue
                    name = str(it.get('任务名称') or '').strip()
                    tid = str(it.get('钉钉任务ID') or '').strip()
                    if not name or not tid:
                        st['skip'] += 1
                        continue
                    props = {}
                    for k in ('任务名称', '优先级', '截止时间', '负责人', '状态',
                              '所属项目', '处理链接', '来源应用', '钉钉任务ID'):
                        v = it.get(k)
                        if v is not None and str(v).strip() != '':
                            props[k] = v
                    hit = by_tid.get(tid)
                    if hit:
                        rid = hit.get('record_id') or hit.get('_id')
                        if rid and props:
                            db.update_record(table, rid, props, person_id=target_pid)
                            st['upd'] += 1
                        else:
                            st['skip'] += 1
                    else:
                        db.add_record(table, props, source='dingtalk',
                                      person_id=target_pid)
                        st['new'] += 1
                        # 已完成/已撤销的历史任务（多维表格回灌、历史待办）不必再提醒
                        if str(props.get('状态') or '') not in ('已完成', '已撤销', '已关闭'):
                            new_todos.append((name, props.get('处理链接') or '',
                                              props.get('截止时间') or ''))
                if st['new'] or st['upd']:
                    db.log('task', True, st['new'] + st['upd'],
                           '同步钉钉待办：新增 %d / 更新 %d' % (st['new'], st['upd']))
                if new_todos and settings.g('dingtalk:notify_digest', '1') != '0':
                    try:
                        for _name, _link, _due in new_todos:
                            notify.push(target_pid, 'todo', _name, _link,
                                        extra=('截止 ' + _due[:10]) if _due else '')
                    except Exception as e:
                        db.log('notify', False, len(new_todos), '待办提醒登记失败：%s' % e)
                return self._send(200, {'ok': True, **st})
            if p == '/api/ding/upsert':
                # 本地「审批」同步脚本专用：按 审批编号 去重写入
                # （「我发起 / 抄送我」需 OA 高级版权限，应用接口拿不到；本机 dws 个人身份可读）
                if s['kind'] != 'admin':
                    return self._send(403, {'error': '需要管理员权限'})
                items = body.get('items') or []
                if not isinstance(items, list):
                    return self._send(400, {'error': 'items 需要是数组'})
                table = db.TABLES['ding']
                target_pid = (str(body.get('person_id') or '').strip()
                              or s.get('person_id')
                              or self._robot_person(settings.get_platform('dingtalk')))
                st = {'new': 0, 'upd': 0, 'skip': 0}
                new_dings = []
                try:
                    rows = db.query(table, size=5000, person_id=target_pid).get('results') or []
                except Exception:
                    rows = []
                by_no = {}
                for r in rows:
                    no = str(r.get('审批编号') or '').strip()
                    if no:
                        by_no[no] = r
                for it in items:
                    if not isinstance(it, dict):
                        st['skip'] += 1
                        continue
                    title = str(it.get('审批标题') or '').strip()
                    no = str(it.get('审批编号') or '').strip()
                    if not title or not no:
                        st['skip'] += 1
                        continue
                    props = {}
                    for k in ('审批标题', '审批类型', '发起人', '审批状态', '发起时间',
                              '审批编号', '审批来源', '来源应用', '处理链接', '涉及金额'):
                        v = it.get(k)
                        if v is not None and str(v).strip() != '':
                            props[k] = v
                    hit = by_no.get(no)
                    if hit:
                        rid = hit.get('record_id') or hit.get('_id')
                        if rid and props:
                            db.update_record(table, rid, props, person_id=target_pid)
                            st['upd'] += 1
                        else:
                            st['skip'] += 1
                    else:
                        db.add_record(table, props, source='dingtalk',
                                      person_id=target_pid)
                        st['new'] += 1
                        new_dings.append((props.get('审批来源') or '',
                                          title, props.get('处理链接') or '',
                                          props.get('发起人') or ''))
                if st['new'] or st['upd']:
                    db.log('ding', True, st['new'] + st['upd'],
                           '同步钉钉审批（含抄送）：新增 %d / 更新 %d' % (st['new'], st['upd']))
                if new_dings and settings.g('dingtalk:notify_new_approval', '1') != '0':
                    try:
                        for _src, _title, _link, _origin in new_dings:
                            if _src == '待我审批':
                                notify.push(target_pid, 'approval', _title, _link,
                                            extra=_origin)
                            elif _src == '抄送我':
                                notify.push(target_pid, 'cc', _title, _link,
                                            extra=_origin)
                    except Exception as e:
                        db.log('notify', False, len(new_dings), '审批提醒登记失败：%s' % e)
                return self._send(200, {'ok': True, **st})
            if p == '/api/doc/save':
                uuid = str(body.get('uuid') or '').strip()
                if not uuid:
                    return self._send(400, {'error': '缺少文档标识，请重新打开文档'})
                done = {'updated': 0, 'added': 0, 'deleted': 0}
                try:
                    cli = _ding_client()
                    union = _doc_operator(cli, s)
                    for u in body.get('updates') or []:
                        bid = str(u.get('id') or '').strip()
                        if not bid:
                            continue
                        cli.doc_block_update(
                            uuid, bid,
                            _doc_element(u.get('type'), str(u.get('text') or ''), u.get('level')),
                            union)
                        done['updated'] += 1
                    for a in body.get('adds') or []:
                        txt = str(a.get('text') or '')
                        if not txt.strip():
                            continue
                        cli.doc_block_insert(
                            uuid, _doc_element(a.get('type') or 'paragraph', txt, a.get('level')),
                            union, a.get('index'))
                        done['added'] += 1
                    for bid in body.get('deletes') or []:
                        bid = str(bid or '').strip()
                        if not bid:
                            continue
                        cli.doc_block_delete(uuid, bid, union)
                        done['deleted'] += 1
                except DingError as e:
                    return self._send(400, {'error': _doc_err(e), 'done': done})
                total = done['updated'] + done['added'] + done['deleted']
                if total:
                    db.log('doc', True, total, '站内编辑钉钉文档「%s」：改 %d / 增 %d / 删 %d'
                           % (body.get('title') or uuid, done['updated'], done['added'], done['deleted']))
                return self._send(200, {'ok': True, 'done': done})
            if p == '/api/sync':
                if s['kind'] != 'admin':
                    return self._send(403, {'error': '需要管理员权限'})
                ok, msg = syncer.run_async()
                return self._send(200, {'ok': ok, 'msg': msg})
            if p == '/api/test':
                if s['kind'] != 'admin':
                    return self._send(403, {'error': '需要管理员权限'})
                dc = settings.get_platform('dingtalk')
                try:
                    u2 = syncer.test_connection(
                        body.get('appKey') or dc['app_key'], body.get('appSecret') or dc['app_secret'],
                        body.get('corpId') or dc['corp_id'], body.get('userId') or dc.get('user_id', ''))
                    return self._send(200, {'ok': True, 'user': u2})
                except Exception as e:
                    return self._send(200, {'ok': False, 'error': str(e)})
            if p == '/api/dingtalk-check':
                # 免登自检：按链路逐项检查，返回每一步的结果与修复建议
                if s['kind'] != 'admin':
                    return self._send(403, {'error': '需要管理员权限'})
                dc = settings.get_platform('dingtalk')
                steps = []

                def add(name, ok, detail, fix=''):
                    steps.append({'name': name, 'ok': ok, 'detail': detail, 'fix': fix})

                miss = [k for k in ('app_key', 'app_secret', 'corp_id') if not dc.get(k)]
                add('凭证（AppKey/AppSecret/CorpId）', not miss,
                    '齐全' if not miss else '缺少：' + ' / '.join(miss),
                    '管理员页 → 平台配置 → 钉钉 填写并保存')
                add('AgentId（微应用ID）', bool(dc.get('agent_id')),
                    dc.get('agent_id') or '未填',
                    '钉钉开放平台 → 应用 → 凭证与基础信息 → AgentId（微应用的 AgentId）')
                _ru = self._base_url() + '/auth/dingtalk/callback'
                add('扫码登录回调地址', True, _ru,
                    '首次使用请在钉钉开放平台 → 应用 → 登录与分享 → 回调域名 中填写 '
                    + self._base_url().split('//')[-1].split('/')[0]
                    + '；若扫码登录报「redirect_uri 不匹配」，改这里后再试')
                cli = None
                if not miss:
                    try:
                        cli = DingClient(dc['app_key'], dc['app_secret'], dc.get('corp_id', ''))
                        cli.token()
                        add('获取 access_token', True, '成功（凭证有效、应用已发布）')
                    except DingError as e:
                        add('获取 access_token', False, str(e),
                            '检查 AppSecret 是否复制完整、应用是否已发布、服务器出口IP是否在钉钉白名单')
                        cli = None
                if cli:
                    try:
                        cli.jsapi_ticket()
                        add('获取 jsapi_ticket', True, '成功（JSAPI 鉴权可用）')
                    except DingError as e:
                        add('获取 jsapi_ticket', False, str(e),
                            '钉钉开放平台 → 权限管理 → 开通「JSAPI 鉴权」相关权限后重新发布')
                if cli:
                    uid0 = ''
                    for _p in db.persons_all():
                        _a = db.accounts_of(_p['person_id'], 'dingtalk')
                        if _a:
                            uid0 = _a[0]['account_id']
                            break
                    if uid0:
                        try:
                            steps.extend(cli.perm_check(uid0))
                        except Exception as _e:
                            add('数据读取权限', False, str(_e)[:160],
                                '在钉钉开放平台检查应用权限')
                try:
                    n = len(db.persons_all())
                    add('人员库', n > 0, '已有 %d 人' % n,
                        '人员管理 → 拉取钉钉通讯录')
                except Exception as e:
                    add('人员库', False, str(e))
                okall = all(x['ok'] for x in steps)
                return self._send(200, {'ok': okall, 'steps': steps,
                                        'hint': '' if okall else '按上面 fix 提示逐条处理；'
                                                '另需在钉钉开放平台把「应用首页地址」设为当前访问地址，'
                                                '同事在钉钉客户端内打开才能自动免登'})
            if p == '/api/config':
                if s['kind'] != 'admin':
                    return self._send(403, {'error': '需要管理员权限'})
                # 只写「请求里真的带了」的字段：以前这里会把没传的 AppKey/CorpId
                # 当空字符串写回去，只改同步间隔就会把凭证清空
                _dt = {}
                for _k, _bk in (('app_key', 'appKey'), ('app_secret', 'appSecret'),
                                ('corp_id', 'corpId'), ('agent_id', 'agentId'),
                                ('user_id', 'userId')):
                    if _bk in body and body.get(_bk) is not None:
                        _dt[_k] = str(body.get(_bk) or '').strip()
                if _dt:
                    settings.save_platform('dingtalk', _dt)
                upd = {}
                if body.get('intervalMin') is not None:
                    upd['interval_min'] = str(int(body['intervalMin']))
                if body.get('autoSync') is not None:
                    upd['auto_sync'] = '1' if body['autoSync'] else '0'
                db.cfg_set(upd)
                db.log('config', True, 0, '更新钉钉配置')
                return self._send(200, {'ok': True, 'status': status_payload(s)})
            if p == '/api/platforms':
                if s['kind'] != 'admin':
                    return self._send(403, {'error': '需要管理员权限'})
                settings.save_platform(body.get('platform') or '', body)
                db.log('config', True, 0, '更新 %s 配置' % body.get('platform'))
                return self._send(200, {'ok': True, 'platforms': settings.all_platform_views()})
            if p == '/api/persons':
                if s['kind'] != 'admin':
                    return self._send(403, {'error': '需要管理员权限'})
                return self._send(200, person_action(body))
            if p == '/api/projects/import':
                if s['kind'] != 'admin':
                    return self._send(403, {'error': '需要管理员权限'})
                try:
                    return self._send(200, projects_mod.analyze_link(body.get('link') or ''))
                except projects_mod.NotableError as e:
                    return self._send(400, {'error': str(e)})
            if p == '/api/projects':
                if s['kind'] != 'admin':
                    return self._send(403, {'error': '需要管理员权限'})
                try:
                    pid = projects_mod.create_project(
                        body.get('name') or '', body.get('baseId') or '', body.get('sheetId') or '',
                        sheet_name=body.get('sheetName') or '', url=body.get('url') or '',
                        field_map=body.get('fieldMap') or {},
                        owner_person_id=s.get('person_id') or '')
                except projects_mod.NotableError as e:
                    return self._send(400, {'error': str(e)})
                db.log('config', True, 0, '新建项目 %s' % (body.get('name') or ''))
                return self._send(200, {'ok': True, 'project_id': pid})
            if p == '/api/projects/resume':
                if s['kind'] != 'admin':
                    return self._send(403, {'error': '需要管理员权限'})
                p0 = db.project_get(body.get('projectId') or '')
                if not p0:
                    return self._send(404, {'error': '项目不存在'})
                db.project_set(p0['project_id'], status='active')
                db.log('config', True, 0, '恢复项目同步 %s' % p0['name'])
                return self._send(200, {'ok': True})
            if p == '/api/admin/password':
                if s['kind'] != 'admin':
                    return self._send(403, {'error': '需要管理员权限'})
                ok, msg = auth.admin_change_password(body.get('old') or '', body.get('new') or '')
                return self._send(200 if ok else 400, {'ok': ok, 'msg': msg})
            return self._send(404, {'error': 'not found'})
        except (DingError, FeishuError, GithubError, KingdeeError, ValueError) as e:
            return self._send(400, {'error': str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {'error': str(e)})

    def do_PUT(self):
        body = self._json_body()
        try:
            s = self._sess()
            if not s:
                return
            _db, _rid = body.get('db', ''), body.get('recordId', '')
            owner = db.record_person(_db, _rid)
            if owner is None:
                return self._send(404, {'error': '记录不存在'})
            # 员工只能改自己的记录；公共表/他人数据只有管理员能动
            if s['kind'] != 'admin' and owner != (s.get('person_id') or ''):
                return self._send(403, {'error': '只能修改自己的数据'})
            ok = db.update_record(_db, _rid, body.get('properties') or {})
            return self._send(200 if ok else 404, {'ok': ok})
        except Exception as e:
            return self._send(500, {'error': str(e)})

    def do_DELETE(self):
        body = self._json_body()
        try:
            s = self._sess()
            if not s:
                return
            if body.get('projectId'):
                pid0 = body.get('projectId')
                p0 = db.project_get(pid0)
                if not p0:
                    return self._send(404, {'error': '项目不存在'})
                if s['kind'] != 'admin' and p0.get('owner_person_id') != (s.get('person_id') or ''):
                    return self._send(403, {'error': '只有管理员或项目负责人可以删除项目'})
                db.project_del(pid0)
                db.log('config', True, 0, '删除项目 %s' % p0['name'])
                return self._send(200, {'ok': True})
            _db, _rid = body.get('db', ''), body.get('recordId', '')
            owner = db.record_person(_db, _rid)
            if owner is None:
                return self._send(404, {'error': '记录不存在'})
            if s['kind'] != 'admin' and owner != (s.get('person_id') or ''):
                return self._send(403, {'error': '只能删除自己的数据'})
            db.delete_record(_db, _rid)
            return self._send(200, {'ok': True})
        except Exception as e:
            return self._send(500, {'error': str(e)})

    # -- github webhook --
    def _github_webhook(self, body):
        raw = json.dumps(body, ensure_ascii=False).encode('utf-8')
        secret = db.cfg_all().get('github:webhook_secret', '')
        sig = self.headers.get('X-Hub-Signature-256') or ''
        if secret and not gh.verify_signature(secret, raw, sig):
            return self._send(401, {'error': 'bad signature'})
        event = self.headers.get('X-GitHub-Event') or 'ping'
        rec = gh.parse_webhook(event, body)
        if not rec:
            return self._send(200, {'ok': True, 'ignored': event})
        pid = ''
        for p in db.persons_all():
            accs = db.accounts_of(p['person_id'], 'github')
            if accs and accs[0]['account_id'].lower() in (rec.get('assignee') or '').lower():
                pid = p['person_id']
                break
        props = {
            '任务名称': rec['title'], '状态': rec['status'],
            '所属项目': 'GitHub' + (('/' + rec['repo']) if rec.get('repo') else ''),
            '来源应用': 'GitHub', '处理链接': rec['url'],
            '负责人': rec.get('assignee') or '',
        }
        if rec.get('updated'):
            props['截止时间'] = rec['updated']
        rid = db.find_record(db.TABLES['task'], '处理链接', rec['url'], pid or None)
        if rid:
            db.update_record(db.TABLES['task'], rid, props, person_id=pid or None)
        else:
            db.add_record(db.TABLES['task'], props, source='github', person_id=pid or None)
        db.log('webhook', True, 1, 'GitHub %s %s' % (event, rec['title'][:40]))

    # ── 钉钉群机器人回调（HTTP 模式）：接收 @机器人 的群消息 ──
    def _robot_person(self, cfg):
        uid = (cfg.get('admin_userid') or '').strip()
        if uid:
            acc = db.account_find('dingtalk', uid)
            if acc:
                return acc['person_id']
        ps = db.persons_all()
        return ps[0]['person_id'] if ps else None

    def _uid_name(self, uid):
        """钉钉 userId → 姓名（备注里写清这条消息 @ 了谁）"""
        uid = str(uid or '').strip()
        if not uid:
            return ''
        acc = db.account_find('dingtalk', uid)
        if not acc:
            return ''
        p = db.person_get(acc['person_id']) or {}
        return p.get('name') or ''

    def _robot_at_me(self, body, cfg):
        """这条消息是否 @ 了我（管理员本人）

        钉钉群里机器人只能收到 @机器人 的消息，所以这里判的是
        「顺带有没有 @我」：atUsers 里除机器人自己之外的成员，
        命中管理员 userid 就算 @我；单聊天然是找我。
        """
        if str(body.get('conversationType') or '') == '1':
            return True
        uid = (cfg.get('admin_userid') or '').strip()
        if not uid:
            return False
        bot = str(body.get('chatbotUserId') or '')
        for a in (body.get('atUsers') or []):
            if not isinstance(a, dict):
                continue
            if bot and str(a.get('dingtalkId') or '') == bot:
                continue
            if str(a.get('staffId') or '').strip() == uid:
                return True
        return False

    def _robot_callback(self, body):
        """钉钉把 @机器人 的消息 POST 到这里（明文 JSON + sign 签名）

        只做两件事：校验签名、把消息落进「等待回复」。不回复任何内容，
        避免在群里刷屏。历史消息拿不到（钉钉不开放群聊天记录读取）。
        """
        import base64
        import hashlib
        import hmac

        cfg = settings.get_platform('dingtalk')
        secret = (cfg.get('app_secret') or '').encode('utf-8')
        ts = (self.headers.get('timestamp') or self.headers.get('Timestamp') or '').strip()
        sign = (self.headers.get('sign') or self.headers.get('Sign') or '').strip()
        skip = str(cfg.get('robot_skip_sign') or '').strip() == '1'
        if not skip:
            ok = False
            if secret and ts and sign:
                try:
                    ok = abs(time.time() - float(ts) / 1000.0) < 3600
                except Exception:
                    ok = False
                if ok:
                    mac = hmac.new(secret, ts.encode('utf-8'), hashlib.sha256).digest()
                    ok = base64.b64encode(mac).decode() == sign
            if not ok:
                print('[robot] 签名校验失败 ts=%s sign=%s' % (ts, (sign or '')[:12]))
                return self._send(403, {'error': 'invalid signature'})

        # 取正文：text 或 richText
        content = ''
        t = body.get('text') or {}
        if isinstance(t, dict):
            content = str(t.get('content') or '')
        if not content.strip():
            c = body.get('content') or {}
            if isinstance(c, dict):
                for seg in (c.get('richText') or []):
                    if isinstance(seg, dict) and seg.get('text'):
                        content += str(seg.get('text'))
        content = content.strip()
        if not content:
            return self._send(200, {'msgtype': 'empty'})
        # 去掉 "@机器人" 前缀
        content = re.sub(r'^(@\S+\s*)+', '', content).strip() or content

        msgid = str(body.get('msgId') or '')
        wait_db = db.TABLES['wait']
        if msgid and db.find_record(wait_db, '消息ID', msgid):
            return self._send(200, {'msgtype': 'empty'})

        is_group = str(body.get('conversationType') or '') == '2'
        group = body.get('conversationTitle') or ('群聊' if is_group else '钉钉单聊')
        sender = body.get('senderNick') or ''

        # 这条消息有没有 @ 我？顺手把「@了谁」也解出来，方便核对
        at_me = self._robot_at_me(body, cfg)
        bot = str(body.get('chatbotUserId') or '')
        at_names = []
        for a in (body.get('atUsers') or []):
            if not isinstance(a, dict):
                continue
            if bot and str(a.get('dingtalkId') or '') == bot:
                continue
            nm = self._uid_name(a.get('staffId'))
            if nm and nm not in at_names:
                at_names.append(nm)
        # 配成「只收 @我」时，纯 @机器人 的消息不入库（避免刷屏）
        if str(cfg.get('robot_only_at_me') or '').strip() == '1' and not at_me:
            print('[robot] 未 @我，忽略：%s · %s：%s' % (group, sender, content[:40]))
            return self._send(200, {'msgtype': 'empty'})

        # AI 指令助手：指令直接回复，自由文本交给大模型（配了 AI 接口时）。
        # 命中即返回，不再落「等待回复」表；未配模型且非指令 → 走下面原有入库逻辑。
        try:
            import ai_bot
            rep = ai_bot.handle(body, content)
            if rep is not None:
                db.log('robot', True, 1, 'AI 指令：%s（%s）' % (content[:30], group))
                print('[robot] AI 指令：%s：%s' % (sender, content[:60]))
                return self._send(200, rep)
        except Exception as e:
            print('[robot] AI 处理失败（转原有入库）：%s' % e)

        src = '钉钉单聊' if not is_group else ('钉钉群@我' if at_me else '钉钉群')
        note = '%s · %s：%s' % (group, sender, content[:200])
        if at_me and is_group:
            note = '直接 @我 · ' + note
        if at_names:
            note += ' ｜ @%s' % '、'.join(at_names[:5])
        props = {
            '事项名称': content[:60],
            '来源': src,
            '状态': '等待回复',
            '备注': note[:400],
            '群名称': group if is_group else '',
            '发送人': sender,
            '消息ID': msgid,
        }
        ct = body.get('createAt')
        if ct:
            try:
                props['发起时间'] = time.strftime('%Y-%m-%dT%H:%M:%S',
                                                 time.localtime(float(ct) / 1000.0))
            except Exception:
                pass
        db.add_record(wait_db, props, source='dingtalk', person_id=self._robot_person(cfg))
        tag = '（@我）' if (at_me and is_group) else ''
        db.log('robot', True, 1, '群消息入库%s：%s（%s）' % (tag, content[:30], group))
        print('[robot] 收到消息%s：%s · %s：%s' % (tag, group, sender, content[:60]))
        return self._send(200, {'msgtype': 'empty'})


# ── 项目管理（多维表格绑定）───────────────────────────────────────────
def _project_task_rows(pid, person_id=None):
    """某项目的任务行；person_id=None 时取全量（管理员/项目负责人）"""
    flt = {'property': {'property': '所属项目ID', 'text': {'equals': pid}}}
    r = db.query(db.TABLES['task'], sort='截止时间', direction='ascending',
                 size=100000, person_id=person_id, filters=flt)
    return r.get('results') or []


def _project_stats(recs):
    today = time.strftime('%Y-%m-%d')
    total = len(recs)
    done = sum(1 for r in recs if r.get('状态') == '已完成')
    doing = sum(1 for r in recs if r.get('状态') == '进行中')
    overdue = sum(1 for r in recs if r.get('状态') != '已完成'
                  and r.get('截止时间') and str(r['截止时间'])[:10] < today)
    members = {}
    for r in recs:
        for nm in str(r.get('负责人') or '').split('、'):
            nm = nm.strip()
            if not nm:
                continue
            m = members.setdefault(nm, {'name': nm, 'total': 0, 'done': 0, 'overdue': 0})
            m['total'] += 1
            if r.get('状态') == '已完成':
                m['done'] += 1
            elif r.get('截止时间') and str(r['截止时间'])[:10] < today:
                m['overdue'] += 1
    dues = sorted(str(r['截止时间'])[:10] for r in recs if r.get('截止时间'))
    return {'total': total, 'done': done, 'doing': doing, 'todo': total - done - doing,
            'overdue': overdue,
            'progress': round(done * 100.0 / total) if total else 0,
            'members': sorted(members.values(), key=lambda m: -m['total']),
            'due_min': dues[0] if dues else '', 'due_max': dues[-1] if dues else ''}


def projects_list(s):
    """项目清单：管理员/项目负责人看全项目统计，普通成员只看到自己参与的项目与自己的任务量"""
    me = s.get('person_id') or ''
    is_admin = s['kind'] == 'admin'
    out = []
    for p in db.projects_all():
        pid = p['project_id']
        is_owner = p.get('owner_person_id') == me
        if is_admin or is_owner:
            recs = _project_task_rows(pid)
        else:
            recs = _project_task_rows(pid, person_id=me)
            if not recs:
                continue  # 与项目无关的员工：不出现（避免全公司看到项目名）
        st = _project_stats(recs)
        out.append({'project_id': pid, 'name': p['name'], 'sheet_name': p.get('sheet_name') or '',
                    'url': p.get('url') or '', 'status': p.get('status') or 'active',
                    'owner': is_owner, 'mine_only': not (is_admin or is_owner),
                    'stats': st})
    return {'projects': out, 'kind': s['kind']}


def project_detail(pid, s):
    """单项目详情：负责人/管理员拿全量任务，普通成员只拿自己的（含时间线所需字段）"""
    p = db.project_get(pid)
    if not p:
        return None
    me = s.get('person_id') or ''
    is_admin = s['kind'] == 'admin'
    is_owner = p.get('owner_person_id') == me
    recs = _project_task_rows(pid) if (is_admin or is_owner) else _project_task_rows(pid, person_id=me)
    fields = ('任务名称', '状态', '优先级', '截止时间', '负责人', '处理链接', '来源应用', '更新时间')
    tasks = [{k: r.get(k) for k in fields if r.get(k)} for r in recs]
    return {'project': {'project_id': pid, 'name': p['name'],
                        'sheet_name': p.get('sheet_name') or '',
                        'url': p.get('url') or '', 'status': p.get('status') or 'active',
                        'owner': is_owner,
                        'field_map': p.get('field_map') or {}},
            'stats': _project_stats(recs), 'tasks': tasks}


def persons_payload():
    out = []
    for p in db.persons_all():
        amap = {}
        for a in db.accounts_of(p['person_id']):
            amap[a['platform']] = {'account_id': a['account_id'], 'name': a.get('name') or ''}
        out.append({
            'person_id': p['person_id'], 'name': p.get('name') or '',
            'status': p.get('status') or '', 'match_by': p.get('match_by') or '',
            'accounts': amap,
            'phones': [mask_phone(x) for x in (p.get('phones') or [])],
            'emails': [mask_email(x) for x in (p.get('emails') or [])],
        })
    return {'report': match.report(), 'persons': out,
            'platforms': {p: settings.platform_ready(p) for p in settings.PLATFORM_ORDER}}


def mask_phone(v):
    s = str(v or '')
    return s[:3] + '****' + s[-4:] if len(s) >= 8 else s


def mask_email(v):
    s = str(v or '')
    if '@' not in s:
        return s
    a, b = s.split('@', 1)
    return (a[:2] + '***@' + b) if len(a) > 2 else ('*@' + b)


def person_action(body):
    action = body.get('action') or ''
    if action == 'pull':
        return pull_contacts(body.get('platform') or 'dingtalk')
    if action == 'match':
        return match.run()
    if action == 'bind':
        pid, plat = body.get('person_id') or '', body.get('platform') or ''
        acc = str(body.get('account_id') or '').strip()
        if not (pid and plat and acc):
            raise ValueError('缺少 person_id / platform / account_id')
        db.account_add(pid, plat, acc, body.get('name') or '')
        return {'ok': True, 'match': match.run()}
    if action == 'unbind':
        db.account_del(body.get('person_id') or '', body.get('platform') or '',
                       body.get('account_id') or '')
        return {'ok': True}
    if action == 'rename':
        db.person_set(body.get('person_id') or '', name=str(body.get('name') or '').strip())
        return {'ok': True}
    if action == 'contacts':
        db.person_set(body.get('person_id') or '',
                      phones=db.normalize_phones(body.get('phones') or []),
                      emails=db.normalize_emails(body.get('emails') or []))
        return {'ok': True, 'match': match.run()}
    if action == 'status':
        db.person_set(body.get('person_id') or '', status=body.get('status') or 'bound')
        return {'ok': True}
    if action == 'delete':
        db.person_del(body.get('person_id') or '')
        return {'ok': True}
    if action == 'add':
        pid = db.person_new(name=body.get('name') or '新成员', status='manual',
                            phones=db.normalize_phones(body.get('phones') or []),
                            emails=db.normalize_emails(body.get('emails') or []))
        return {'ok': True, 'person_id': pid}
    raise ValueError('未知操作：%s' % action)


def main():
    bootstrap()
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=_first_sync, daemon=True).start()
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print('=' * 56)
    print(' 任务安排工作台 · 独立版 v2')
    print(' 访问地址   http://<本机IP>:%d' % PORT)
    print(' 管理入口   页面右上角「管理员登录」')
    print(' 数据库     %s' % db.DB_PATH)
    print('=' * 56)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')


if __name__ == '__main__':
    main()
