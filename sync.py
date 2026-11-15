# -*- coding: utf-8 -*-
"""同步引擎：按白名单逐人拉取各平台的待办 / 审批 / 任务

原则
- 一份应用凭证查所有人（员工零配置、不授权）
- 每条记录带 person_id（属于谁）、来源应用、处理链接
- 单平台失败不影响其他平台，全部记入 sync_log

去重键
- 审批：审批编号（钉钉 processInstanceId / 飞书 instance_code / 金蝶 FormId:单据号）
- 任务：钉钉按任务名称；GitHub 按处理链接（issue URL 唯一）
"""
import json
import os
import re
import threading
import time
import urllib.parse

import db
import match
import notify
import settings
from dingtalk import Client as DingClient, DingError
from feishu import Client as FeishuClient, FeishuError
from github import Client as GithubClient, GithubError
from gitlab import Client as GitlabClient, GitlabError
from gitea import Client as GiteaClient, GiteaError
from kingdee import Client as KingdeeClient, KingdeeError
from yonyou import Client as YonyouClient, YonyouError
from zentao import Client as ZentaoClient, ZentaoError

_running = threading.Lock()

STATUS_MAP = {
    'COMPLETED': '已通过', 'RUNNING': '审批中', 'NEW': '审批中',
    'TERMINATED': '已撤销', 'CANCELED': '已撤销', 'REVOKED': '已撤销',
}

TYPE_KEYWORDS = [
    ('离职', ['离职']), ('绩效', ['绩效', 'KPI']), ('加班', ['加班']),
    ('IT需求', ['IT 类型', 'IT类型', 'ERP', '权限', '系统', '网络', '设备', '需求']),
    ('合同', ['合同', '协议', '签约']),
    ('付款申请', ['付款', '请款', '打款']),
    ('采购申请', ['采购', '请购', '询价']),
    ('发文', ['发文', '公告', '通知']), ('报销', ['报销', '费用']),
    ('请假', ['请假', '调休', '年假']), ('出差', ['出差']),
    ('用印', ['用印', '盖章']), ('补卡', ['补卡']),
]

# 金蝶单据类型 → 审批类型
FORM_TYPE = [
    ('PUR_', '采购申请'), ('SAL_', '销售申请'), ('AR_', '收款申请'),
    ('AP_', '付款申请'), ('CON_', '合同'), ('PAY', '付款申请'),
]


def _type_of(title):
    t = str(title or '')
    for name, kws in TYPE_KEYWORDS:
        for k in kws:
            if k in t:
                return name
    return '其他'


# 审批任务列表里混着系统推送（试用邀请之类），不是真审批，直接丢弃
SKIP_APPROVAL_WORDS = ('邀请你试用', '邀请您试用', '邀请你体验', '邀请您体验',
                       '邀请你加入', '邀请您加入', '开通提醒')


def _skip_approval(title):
    t = str(title or '')
    return any(w in t for w in SKIP_APPROVAL_WORDS)


def _ding_person_name(uid, fallback=''):
    """审批详情只给 originator 的 userid，用通讯录缓存反查姓名

    注意：列表接口的 originatorName 有时直接回原始 userid（纯数字），
    这种值不能当姓名用，必须拿它当 userid 再去通讯录反查。
    """
    fb = str(fallback or '').strip()
    if fb and not _looks_like_uid(fb):
        return fb
    uid = str(uid or '').strip() or (fb if _looks_like_uid(fb) else '')
    if not uid:
        return ''
    try:
        acc = db.account_find('dingtalk', uid)
        if acc and acc.get('name'):
            return acc['name']
    except Exception:
        pass
    return ''


def _looks_like_uid(v):
    """纯数字（钉钉 userid 形如 17168608696007064）当作 ID，不是姓名"""
    s = str(v or '').strip()
    return s.isdigit() and len(s) >= 8


def _type_of_form(form_id):
    f = str(form_id or '')
    for prefix, name in FORM_TYPE:
        if f.startswith(prefix):
            return name
    return '其他'


def _iso(ms):
    # 已是日期字符串：钉钉 create_time 是北京时间（无时区），必须标 +08:00，
    # 否则浏览器会当 UTC 解析，时间整体偏移 8 小时
    if isinstance(ms, str) and len(ms) >= 10 and ms[4] == '-':
        s = ms.replace(' ', 'T')
        if re.search(r'(Z|[+-]\d{2}:?\d{2})$', s):
            return s
        return s[:19] + '+08:00'
    try:
        return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(int(ms) / 1000))
    except Exception:
        return ''


def _ms_of(v):
    """各种时间表示 → epoch 毫秒（钉钉有的接口给 ms 字符串，有的给日期字符串）"""
    if v in (None, ''):
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if s.isdigit():
        return int(s)
    try:
        return int(time.mktime(time.strptime(s[:19].replace('T', ' '),
                                            '%Y-%m-%d %H:%M:%S')) * 1000)
    except Exception:
        return 0


def _oa_link(cfg, pid):
    return ('https://applink.dingtalk.com/approval/detail?corpId=%s&instanceId=%s&from=%s'
            % (cfg.get('corp_id', ''), urllib.parse.quote(pid), cfg.get('app_key', '')))


def _todo_link():
    return 'dingtalk://dingtalkclient/action/switchtab?index=1&type=task'


_MOD_NAMES = {}


def _ding_name(cli, uid):
    """钉钉 userId/unionId → 姓名（进程内缓存）。

    查不到就返回 ''——宁可空着也不把一串 ID 摆到界面上。
    钉盘 dentry 的 modifierId 是 unionId，先按 userid 匹配通讯录，
    不行再走 getbyunionid 换 userid 后匹配。
    """
    uid = str(uid or '').strip()
    if not uid:
        return ''
    if uid in _MOD_NAMES:
        return _MOD_NAMES[uid]
    nm = ''
    acc = db.account_find('dingtalk', uid)
    if acc:
        nm = (db.person_get(acc['person_id']) or {}).get('name') or ''
    if not nm:
        userid = cli.userid_by_unionid(uid)
        acc = db.account_find('dingtalk', userid) if userid else None
        if acc:
            nm = (db.person_get(acc['person_id']) or {}).get('name') or ''
    _MOD_NAMES[uid] = nm
    return nm


def _upsert(table, key_field, key, props, person_id, source):
    # 只按「同一个人」去重：同一条审批会同时出现在多个审批人的待办里，
    # 若按编号全局去重，后同步的人会把前面人的记录抢走（person_id 被覆盖）。
    rid = db.find_record(table, key_field, key, person_id)
    if rid:
        db.update_record(table, rid, props, person_id=person_id)
        return False
    db.add_record(table, props, source=source, person_id=person_id)
    return True


_MONTHS = {'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
           'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12}


def _fmt_dt(s):
    """多种时间格式 → 'YYYY-MM-DD HH:MM'
    支持 ISO（2026-07-01T15:00:00+08:00）与钉盘 Java 风格
    （Fri Mar 04 14:17:21 CST 2022，CST 即东八区）"""
    s = str(s or '')
    m = re.match(r'(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})', s)
    if m:
        return '%s %s' % (m.group(1), m.group(2))
    m = re.match(r'\w{3}\s+(\w{3})\s+(\d{1,2})\s+(\d{2}:\d{2}):\d{2}\s+\S+\s+(\d{4})', s)
    if m and m.group(1) in _MONTHS:
        return '%s-%02d-%02d %s' % (m.group(4), _MONTHS[m.group(1)],
                                    int(m.group(2)), m.group(3))
    return ''


def _dur(start, end, all_day=False):
    """时长文本：'1.5小时' / '45分钟' / '全天'"""
    if all_day:
        return '全天'
    try:
        from datetime import datetime
        f = '%Y-%m-%dT%H:%M:%S'
        a = datetime.strptime(str(start)[:19], f)
        b = datetime.strptime(str(end)[:19], f)
        mins = int((b - a).total_seconds() // 60)
        if mins <= 0:
            return ''
        if mins < 60:
            return '%d分钟' % mins
        h = mins / 60.0
        return ('%g小时' % h) if h == int(h) else ('%.1f小时' % h)
    except Exception:
        return ''


# ── 钉钉 ──────────────────────────────────────────────────────────────
def sync_dingtalk(cfg, say):
    pc = settings.get_platform('dingtalk')
    if not (pc.get('app_key') and pc.get('app_secret')):
        return None
    cli = DingClient(pc['app_key'], pc['app_secret'], pc.get('corp_id', ''))
    cli.token()
    if pc.get('user_id'):
        try:
            db.cfg_set({'app_name': cli.user_by_id(pc['user_id']).get('name') or ''})
        except DingError:
            pass

    stat = {'todo_new': 0, 'todo_upd': 0, 'ding_new': 0, 'ding_upd': 0, 'err': []}
    # 本轮新增的审批（person_id → [(发起毫秒, 标题, 来源, 编号, 链接)]），
    # 同步结束后汇总成一条钉钉工作通知推给本人，补上「工作通知」不会提醒的那部分
    pending = {}
    # 审批模板清单：自动发现（含第三方系统建的模板），一天刷新一次
    try:
        _refresh_process_codes(cli, (pc.get('admin_userid') or '').strip(), say)
    except Exception as e:
        stat['err'].append('审批模板发现：%s' % e)
    # 「我发起 / 抄送我」已有模板枚举 + 抄送人反推的补齐路径；高级版接口只在
    # 从没试过（premium_ok 未标记为 0）时探测一次，403 后就别再每轮报错了
    astate = {'legacy': True, 'notified': False,
              'premium': db.cfg_get('dingtalk:premium_ok', '1') != '0',
              'premium_notified': True, 'err': stat['err']}
    _all = db.persons_all()
    persons = [p for p in _all
               if (p.get('status') or '') != 'disabled'
               and db.accounts_of(p['person_id'], 'dingtalk')]
    # 同步范围（dingtalk:sync_scope）
    #   all   —— 全员（默认）：任何人第一次打开工作台，数据已经在库里，不用干等下一轮
    #   login —— 只同步「管理员 + 登录过的人」（老行为，人特别多 / 想省接口调用时用）
    scope_uid = (pc.get('admin_userid') or '').strip()
    admin_p = [p for p in persons
               if scope_uid and any(a['account_id'] == scope_uid
                                    for a in db.accounts_of(p['person_id'], 'dingtalk'))]
    mode = (db.cfg_get('dingtalk:sync_scope') or 'all').strip().lower()
    focus = (db.cfg_get('dingtalk:sync_focus') or '').strip()
    picked = persons
    if mode == 'login':
        seen, picked = set(), []
        for p in admin_p + [p for p in persons if (p.get('last_login') or '').strip()]:
            if p['person_id'] not in seen:
                seen.add(p['person_id'])
                picked.append(p)
    focused = False
    if focus:
        # 谁刚登录就先同步谁：登录后几十秒内就能看到自己的数据，不用等整轮跑完
        db.cfg_set({'dingtalk:sync_focus': ''})
        fp = [p for p in persons if p['person_id'] == focus]
        if fp:
            picked, focused = fp, True
            say('优先同步刚登录的人：%s' % (fp[0].get('name') or focus))
    # 每轮人数上限：202 人一轮要跑近一小时，会把 30 分钟的定时器永远占住。
    # 默认每轮 sync_limit 人，按「上次同步时间」从老到新取（从没同步过的排最前），
    # 效果是新人第一时间有数据、其余人公平轮转。0 = 不限（首次全量补数时用）。
    try:
        limit = int(db.cfg_get('dingtalk:sync_limit') or 60)
    except Exception:
        limit = 60
    if limit > 0 and not focused and len(picked) > limit:
        _cf = db.cfg_all()
        picked = sorted(picked, key=lambda x: (
            float(_cf.get('sync:at:' + x['person_id']) or 0), x['person_id']))[:limit]
        ids = {x['person_id'] for x in picked}
        for p in admin_p:            # 管理员始终在范围内（他要在界面上看进度）
            if p['person_id'] not in ids:
                picked.append(p)
    if picked:
        persons = picked
        say('钉钉同步范围：%d/%d 人（%s%s）' % (
            len(persons), len([p for p in _all if (p.get('status') or '') != 'disabled']),
            '全员' if mode != 'login' else '仅登录过的人',
            '，本轮上限 %d' % limit if (limit and not focused) else ''))
    if not persons:
        pid = _legacy_person(pc)
        persons = [p for p in db.persons_all() if p['person_id'] == pid] if pid else []
    if not persons:
        stat['err'].append('没有可同步的钉钉成员（先在管理员页拉取通讯录并启用成员）')
        return stat

    try:
        pause = float(db.cfg_get('dingtalk:sync_pause') or 0.2)
    except Exception:
        pause = 0.2
    total_p = len(persons)
    cold = set()          # 首次建档的人：不发通知
    for idx, p in enumerate(persons, 1):
        pid = p['person_id']
        acc = db.accounts_of(pid, 'dingtalk')[0]
        uid = acc['account_id']
        name = p.get('name') or acc.get('name') or ''
        say('钉钉：%s（%d/%d）' % (name or uid, idx, total_p))
        # 第一次同步的人只「静默建档」：数据照样入库，但不给他推钉钉消息。
        # 否则上线第一轮等于给全公司每人发一条「你有 N 条待审批」，纯骚扰。
        try:
            first_time = db.person_records(pid) == 0
        except Exception:
            first_time = False
        notify.silence(pid, first_time)
        if first_time:
            cold.add(pid)
        try:
            unionid = (acc.get('extra') or {}).get('unionid') or cli.unionid_of(uid)
            if unionid and not (acc.get('extra') or {}).get('unionid'):
                db.account_add(pid, 'dingtalk', uid, name, {'unionid': unionid})
        except DingError as e:
            stat['err'].append('%s unionId：%s' % (name, e))
            unionid = ''

        # 待办 → 任务表
        if unionid:
            try:
                for done in (False, True):
                    for item in cli.todos_by_union(unionid, done):
                        subj = str(item.get('subject') or item.get('title') or '').strip()
                        if not subj:
                            continue
                        props = {
                            '任务名称': subj, '状态': '已完成' if done else '待处理',
                            '所属项目': '钉钉待办', '来源应用': '钉钉', '负责人': name,
                        }
                        ct = item.get('createdTime') or item.get('createTime')
                        if ct:
                            props['截止时间'] = _iso(ct)
                        sid = str(item.get('sourceId') or '')
                        p2 = sid.split(':')[0] if ':' in sid else (sid if sid.startswith(('z', 'e')) else '')
                        props['处理链接'] = _oa_link(pc, p2) if p2 else _todo_link()
                        if _upsert(db.TABLES['task'], '任务名称', subj, props, pid, 'dingtalk'):
                            stat['todo_new'] += 1
                            notify.push(pid, 'todo', subj,
                                        props.get('处理链接') or _todo_link())
                        else:
                            stat['todo_upd'] += 1
            except DingError as e:
                stat['err'].append('%s 待办：%s' % (name, e))

        # 工作台 → 钉钉待办（个人待办没有列表接口，只能推下去、再按单条回读状态）
        if unionid and settings.g('dingtalk:todo_push', '1') != '0':
            try:
                for r in db.query(db.TABLES['task'], size=5000, person_id=pid)['results']:
                    tname = str(r.get('任务名称') or '').strip()
                    rid = r.get('record_id') or r.get('_id')
                    if not tname or not rid:
                        continue
                    if str(r.get('来源应用') or '') == '钉钉':
                        continue      # 从钉钉拉回来的不再回推，避免自循环
                    tid = str(r.get('钉钉任务ID') or '').strip()
                    if not tid:
                        if str(r.get('状态') or '') == '已完成':
                            continue
                        due_ms = None
                        due = str(r.get('截止时间') or '').replace('T', ' ')
                        if len(due) >= 10:
                            try:
                                due_ms = int(time.mktime(time.strptime(due[:19], '%Y-%m-%d %H:%M:%S')) * 1000)
                            except Exception:
                                due_ms = None
                        try:
                            new_id = cli.todo_create(unionid, tname,
                                                     str(r.get('所属项目') or ''), due_ms)
                        except DingError as e:
                            stat['err'].append('待办推送「%s」：%s' % (tname[:12], e))
                            continue
                        if new_id:
                            db.update_record(db.TABLES['task'], rid, {'钉钉任务ID': new_id})
                            stat['todo_push'] = stat.get('todo_push', 0) + 1
                    else:
                        try:
                            td = cli.todo_get(unionid, tid)
                        except DingError:
                            continue
                        if (td or {}).get('done') and str(r.get('状态') or '') != '已完成':
                            db.update_record(db.TABLES['task'], rid, {'状态': '已完成'})
                            stat['todo_done'] = stat.get('todo_done', 0) + 1
            except Exception as e:
                stat['err'].append('%s 待办桥接：%s' % (name, e))

        # 审批（待办 + 已办）→ 审批表
        seen = set()
        items_all = _approval_items(cli, uid, astate, name)
        for wstatus, it, src in items_all:
            pidv = str(it.get('processInstanceId') or '')
            if not pidv or pidv in seen:
                continue
            seen.add(pidv)
            try:
                d = cli.approval_detail(pidv)
            except DingError as e:
                # 详情拿不到也要入库：列表里已有标题/链接，别整条丢掉
                if not it.get('title'):
                    stat['err'].append('审批 %s：%s' % (pidv[:8], e))
                    continue
                d = {'title': it.get('title'), 'amount': None}
            if wstatus is None:
                # 我发起 / 抄送我：列表接口自带状态，不必依赖详情
                stxt = it.get('statusText') or '审批中'
            else:
                stxt = STATUS_MAP.get(str(d.get('status') or ''), '审批中')
                if stxt == '已通过' and str(d.get('result') or '').lower() == 'refuse':
                    stxt = '已拒绝'
            title = _clean_title(d.get('title') or it.get('title') or '')
            if _skip_approval(title):
                continue            # 自己发起的单子一律归到「我发起」；「待我审批」优先（那是要动手的）
            origin_uid = str(d.get('originator') or it.get('originatorId') or '')
            src_eff = src
            if uid and origin_uid and origin_uid == str(uid) and src != '待我审批':
                src_eff = '我发起'
            props = {
                '审批标题': title,
                '审批类型': _type_of(title),
                '发起人': _ding_person_name(d.get('originator'),
                                            it.get('originatorName') or d.get('originator_name') or ''),
                '审批状态': stxt,
                '审批编号': pidv,
                '审批来源': src_eff,
                '来源应用': '钉钉',
            }
            if d.get('create') or it.get('createTime'):
                props['发起时间'] = _iso(d.get('create') or it.get('createTime'))
            if d.get('amount') is not None:
                props['金额'] = d['amount']
                props['涉及金额'] = '是'
            else:
                props['涉及金额'] = '否'
            if it.get('url'):
                props['处理链接'] = it['url']
            # AI 判断审批的原料：表单正文 + 历史流转（谁批的、批还是拒）
            form_txt = _flat_text(d.get('flat'))
            if form_txt:
                props['表单明细'] = form_txt
            flow_txt = _flow_text(d.get('ops'))
            if flow_txt:
                props['审批流转'] = flow_txt
            if _upsert(db.TABLES['ding'], '审批编号', pidv, props, pid, 'dingtalk'):
                stat['ding_new'] += 1
                if not first_time:
                    pending.setdefault(pid, []).append((
                        _ms_of(d.get('create')) or _ms_of(it.get('createTime')),
                        title, src_eff, pidv,
                        props.get('处理链接') or _oa_link(pc, pidv)))
            else:
                stat['ding_upd'] += 1
            # 「待我审批」同时进「等待回复」表，让待回复模块有真实事项；
            # 审批一旦办结，把同名事项收口成「已回复」，避免一直挂着
            if title:
                wid, wrec = db.find_record_rec(db.TABLES['wait'], '事项名称', title, pid)
                wst_now = str((wrec or {}).get('状态') or '')
                if wstatus == 0:
                    wprops = {
                        '事项名称': title,
                        '来源': '钉钉审批',
                        '状态': '等待回复' if stxt in ('审批中', '待处理') else stxt,
                        '备注': '%s · 发起人 %s · 编号 %s' % (_type_of(title),
                                                           props.get('发起人') or '未知', pidv[:8]),
                    }
                    # 手工标记「已回复/已关闭」的事项，审批没办结前不再刷回「等待回复」
                    if wst_now in ('已回复', '已关闭') and wprops.get('状态') == '等待回复':
                        wprops.pop('状态', None)
                    if props.get('发起时间'):
                        wprops['发起时间'] = props['发起时间']
                    if it.get('url'):
                        wprops['处理链接'] = it['url']
                    if _upsert(db.TABLES['wait'], '事项名称', title, wprops, pid, 'dingtalk'):
                        stat['wait_new'] = stat.get('wait_new', 0) + 1
                    else:
                        stat['wait_upd'] = stat.get('wait_upd', 0) + 1
                elif wid and wstatus is not None:
                    db.update_record(db.TABLES['wait'], wid, {'状态': '已回复'}, person_id=pid)
                    stat['wait_done'] = stat.get('wait_done', 0) + 1

        # 日程 / 会议 → 日程表 + 会议表（钉钉的会议就是带会议室/会议分类的日程）
        if unionid:
            now_ms = int(time.time() * 1000)
            try:
                for ev in cli.events(unionid, now_ms - 30 * 86400000,
                                     now_ms + 90 * 86400000):
                    eid = str(ev.get('id') or '')
                    summary = str(ev.get('summary') or '').strip()
                    if not eid or not summary:
                        continue
                    start = (ev.get('start') or {}).get('dateTime') or ''
                    end = (ev.get('end') or {}).get('dateTime') or ''
                    cats = ' '.join(c.get('displayName') or ''
                                    for c in (ev.get('categories') or []))
                    rooms = ', '.join(r.get('displayName') or ''
                                      for r in (ev.get('meetingRooms') or []))
                    loc = (ev.get('location') or {}).get('displayName') or rooms
                    is_meet = ('会议' in cats) or bool(rooms) \
                        or bool(ev.get('conference'))
                    st = '已取消' if str(ev.get('status')) == 'cancelled' else '已确认'
                    atts = ', '.join(a.get('displayName') or ''
                                     for a in (ev.get('attendees') or []))
                    org = ((ev.get('organizer') or {}).get('displayName') or '')
                    dur = _dur(start, end, ev.get('isAllDay'))
                    common = {'开始时间': _fmt_dt(start), '事件ID': eid}
                    if dur:
                        common['时长'] = dur
                    if is_meet:
                        mprops = dict(common, 会议主题=summary, 状态=st,
                                      来源应用='钉钉')
                        if loc:
                            mprops['地点'] = loc
                        if org:
                            mprops['组织人'] = org
                        if atts:
                            mprops['参会人'] = atts
                        if rooms:
                            mprops['纪要链接'] = rooms
                        if _upsert(db.TABLES['meet'], '事件ID', eid, mprops,
                                   pid, 'dingtalk'):
                            stat['meet_new'] = stat.get('meet_new', 0) + 1
                        else:
                            stat['meet_upd'] = stat.get('meet_upd', 0) + 1
                    else:
                        sprops = dict(common, 日程事项=summary,
                                      来源应用='钉钉')
                        if str(ev.get('description') or ''):
                            sprops['说明'] = str(ev['description'])[:200]
                        if _upsert(db.TABLES['sched'], '事件ID', eid, sprops,
                                   pid, 'dingtalk'):
                            stat['sched_new'] = stat.get('sched_new', 0) + 1
                        else:
                            stat['sched_upd'] = stat.get('sched_upd', 0) + 1
            except DingError as e:
                stat['err'].append('%s 日程：%s' % (name, e))

        # 钉钉文档 / 钉盘 → 资料表（需 Drive.Space.Read + Storage.File.Read）
        def _err_text(e):
            return '%s %s' % (e, getattr(e, 'raw', '') or '')

        def _is_app_perm(e):
            """应用级权限缺失（需去开放平台申请），区别于空间级 ACL 拒绝"""
            s = _err_text(e)
            return 'AccessTokenPermissionDenied' in s or '尚未开通' in s

        if unionid:
            spaces = []
            perm_err = ''
            try:
                for stype in ('org', 'personal'):
                    try:
                        spaces.extend(cli.drive_spaces(unionid, stype))
                    except DingError as e:
                        perm_err = str(e)
                        if not ('400' in perm_err or 'no.priviledge' in _err_text(e)):
                            stat['err'].append('%s 钉盘空间(%s)：%s' % (name, stype, e))
                for sp in spaces:
                    sid = str(sp.get('id') or '')
                    sname = sp.get('name') or '钉盘'
                    try:
                        dentries = cli.drive_files(sid, unionid)
                    except DingError as e:
                        if _is_app_perm(e) and not any('Storage.File.Read' in x
                                                       for x in stat['err']):
                            stat['err'].append('钉钉文档读取需要 Storage.File.Read 权限：'
                                               + cli.apply_url('Storage.File.Read'))
                        elif 'permissionDenied' in _err_text(e) or 'no.priviledge' in _err_text(e):
                            # 空间级 ACL：操作人对该空间无权限，提示一次即可
                            if not any('无访问权限' in x and sname in x for x in stat['err']):
                                stat['err'].append('%s 对钉盘空间「%s」无访问权限（空间权限由钉钉管理员在钉盘后台分配，非开放平台权限）'
                                                   % (name, sname))
                        else:
                            stat['err'].append('%s 钉盘文件(%s)：%s' % (name, sname, e))
                        continue
                    for dentry in dentries:
                        did = '%s:%s' % (sid, dentry.get('id') or '')
                        fname = str(dentry.get('name') or '').strip()
                        if not did or not fname:
                            continue
                        ext = str(dentry.get('extension') or '').lower()
                        if str(dentry.get('type')) == 'FOLDER':
                            ftype = '文件夹'
                        elif ext in ('adoc', 'doc', 'docx') or 'doc' in ext:
                            ftype = '在线文档'
                        else:
                            ftype = '文件'
                        fprops = {
                            '文件名称': fname, '类型': ftype,
                            '知识库': ('钉钉/' + sname)[:120],
                            '父节点ID': str(dentry.get('parentId') or ''),
                            '文档ID': did, '来源应用': '钉钉',
                        }
                        # dentry.uuid（32 位）= 文档正文接口要的 dentryUuid
                        duuid = str(dentry.get('uuid') or '').strip()
                        if duuid:
                            fprops['文档UUID'] = duuid
                        else:
                            fprops['文档UUID'] = ''
                        du = str(dentry.get('url') or '').strip()
                        if du:
                            fprops['链接'] = du
                        mt = _fmt_dt(dentry.get('modifiedTime') or dentry.get('createTime'))
                        if mt:
                            fprops['更新时间'] = mt
                        sz = dentry.get('size')
                        if str(dentry.get('type')) != 'FOLDER' and sz:
                            try:
                                sz = int(sz)
                                fprops['大小'] = ('%.1fMB' % (sz / 1048576)) if sz >= 1048576 \
                                    else ('%dKB' % (sz // 1024) if sz >= 1024 else '%dB' % sz)
                            except Exception:
                                pass
                        path = str(dentry.get('path') or '')
                        if path:
                            fprops['层级路径'] = path
                        # modifierId 是 unionId，反查成姓名；查不到写空，别把 ID 摆上界面
                        fprops['更新人'] = _ding_name(cli, dentry.get('modifierId'))
                        if _upsert(db.TABLES['files'], '文档ID', did, fprops, pid, 'dingtalk'):
                            stat['file_new'] = stat.get('file_new', 0) + 1
                        else:
                            stat['file_upd'] = stat.get('file_upd', 0) + 1
                if spaces:
                    say('钉钉：文档来自 %d 个空间' % len(spaces))
                elif perm_err and not any('Drive.Space.Read' in x for x in stat['err']):
                    stat['err'].append('钉钉文档/钉盘同步需要 Drive.Space.Read 权限：'
                                       + cli.apply_url('Drive.Space.Read'))
            except DingError as e:
                stat['err'].append('%s 钉盘：%s' % (name, e))

        # 钉钉知识库（wiki 全量层级）→ 资料表，24 小时一次，权限缺失自动降级
        try:
            _wiki_sync(cli, unionid, pid, stat)
        except Exception as e:
            stat['err'].append('%s 知识库：%s' % (name, e))

        notify.silence(pid, False)
        try:
            db.cfg_set({'sync:at:' + pid: '%.3f' % time.time()})
        except Exception:
            pass
        if pause > 0 and idx < total_p:
            time.sleep(pause)      # 人和人之间留一点间隔，别把接口打到限流

    # 「我发起」补齐：按审批模板 + 发起人查实例（v1.0 接口，不需要 OA 高级版权限）
    try:
        _initiated_sync(cli, pc, persons, stat, say)
    except Exception as e:
        stat['err'].append('我发起补齐：%s' % e)

    # 「抄送我」补齐：按模板 + 部门枚举实例，读详情 cc_userids 反推归属
    try:
        _cc_sync(cli, pc, persons, stat, say, pending, cold)
    except Exception as e:
        stat['err'].append('抄送我补齐：%s' % e)

    # 新增审批 → 钉钉工作通知（按人汇总，见 _notify_approvals）
    try:
        _notify_approvals(cli, pc, pending, stat, say)
    except Exception as e:
        stat['err'].append('审批提醒：%s' % e)

    # 待办 / 审批 / 等待回复 / @我 / 审批结果 → 每人一条汇总消息
    try:
        notify.flush(say=say)
    except Exception as e:
        stat['err'].append('钉钉消息发送：%s' % e)

    say('钉钉：待办 +%d/%d · 审批 +%d/%d · 我发起 +%d/%d · 抄送我 +%d/%d · 待回复 +%d/%d · 日程 +%d/%d · 会议 +%d/%d' % (
        stat['todo_new'], stat['todo_upd'], stat['ding_new'], stat['ding_upd'],
        stat.get('init_new', 0), stat.get('init_upd', 0),
        stat.get('cc_new', 0), stat.get('cc_upd', 0),
        stat.get('wait_new', 0), stat.get('wait_upd', 0),
        stat.get('sched_new', 0), stat.get('sched_upd', 0),
        stat.get('meet_new', 0), stat.get('meet_upd', 0)))
    return stat


# 内置的默认审批模板列表。开源版留空：模板 ID 属于企业自身配置，
# 请放到「平台配置」里的 dingtalk:process_codes（逗号分隔的 PROC-xxx）。
# 正常情况下也不需要手工维护 —— _refresh_process_codes() 会自动枚举本企业模板。
DEFAULT_PROCESS_CODES = []


def _clean_title(t):
    """详情标题带「张三提交的xxx」，去掉提交人前缀"""
    t = str(t or '').strip()
    m = re.match(r'^.{2,10}?提交的(.+)$', t)
    return (m.group(1) if m else t).strip()


def _flat_text(flat, limit=2500):
    """审批表单 → 可读正文。这是 AI 判断审批的原料，别丢。

    产出形如：
        请假事由：配合 M-102 客户项目改线
        开始时间：2026-07-29 上午
        附件：[附件]https://...
    """
    lines = []
    for kv in flat or []:
        if not isinstance(kv, dict):
            continue
        name = str(kv.get('name') or '').strip() or '说明'
        v = kv.get('value')
        if v is None:
            continue
        if isinstance(v, (list, dict)):
            try:
                v = json.dumps(v, ensure_ascii=False)
            except Exception:
                v = str(v)
        s = str(v).strip()
        if not s or s.lower() in ('null', 'none', '[]', '{}'):
            continue
        if s.startswith('http') or s.startswith('["http'):
            s = '[附件]' + s
        if len(s) > 300:
            s = s[:300] + '…'
        lines.append('%s：%s' % (name, s))
    return '\n'.join(lines)[:limit]


def _flow_text(ops, limit=600):
    """审批流转 → 「张三同意 2026-08-26 13:53；李四同意…」，用于学历史批法"""
    out = []
    for o in ops or []:
        if not isinstance(o, dict):
            continue
        who = _ding_person_name(o.get('userid'), '') or '审批人'
        out.append('%s%s %s %s' % (who, o.get('action') or '同意',
                                   str(o.get('time') or '')[:16],
                                   (o.get('remark') or '').strip()))
    return '；'.join(x.strip() for x in out)[:limit]


def _process_codes():
    """本轮要扫的审批模板：自动发现 + 手工追加

    自动发现（dingtalk:process_codes_auto，24 小时刷新）才是主来源：
    企业模板数量随时会变，靠代码里写死一份清单迟早会漏；第三方系统
    （宜搭/氚云/自建 ERP）建的审批单在钉钉里同样是模板，所以「其它系统
    推来的审批」也走这条路。dingtalk:process_codes 只用于临时补几个。
    """
    raw = settings.g('dingtalk:process_codes', '') or ''
    extra = [x.strip() for x in re.split(r'[,;\s]+', raw) if x.strip().upper().startswith('PROC-')]
    try:
        auto = json.loads(db.cfg_get('dingtalk:process_codes_auto') or '[]')
    except Exception:
        auto = []
    if not isinstance(auto, list):
        auto = []
    out = []
    for c in list(auto) + DEFAULT_PROCESS_CODES + extra:
        if c and c not in out:
            out.append(c)
    return out


def _refresh_process_codes(cli, userid, say):
    """每天刷一次审批模板清单（模板增删不用再改代码/填配置）"""
    have = (db.cfg_get('dingtalk:process_codes_auto') or '').strip()
    try:
        ts = float(db.cfg_get('dingtalk:process_codes_ts') or 0)
    except Exception:
        ts = 0
    if have and time.time() - ts < 86400:
        return
    if not userid:
        return
    try:
        tpl = cli.process_templates(userid)
    except DingError as e:
        say('审批模板：自动发现失败（%s），沿用上次清单' % str(e)[:60])
        return
    codes = [c for _, c in tpl if c]
    if not codes:
        return
    db.cfg_set({'dingtalk:process_codes_auto': json.dumps(codes),
                'dingtalk:process_codes_ts': str(int(time.time()))})
    say('审批模板：自动发现 %d 个（24 小时刷新一次）' % len(codes))


def _notify_approvals(cli, cfg, pending, stat, say):
    """本轮新增的审批 → 汇总队列（不直接发，由 notify.flush 按人合成一条）

    对应「工作通知」里的审批提醒：钉钉自己只在被指派审批人时推送一次，
    抄送、加签、转交这些后续动作不会再提醒，工作台补的就是这一段。
    三条约束：
      1. 只收「待我审批 / 抄送我」，自己发起的本来就知道，不打扰；
      2. 只收发起时间在 notify_approval_days（默认 7 天）内的，避免补历史时轰炸；
      3. 首次运行只登记时间戳、不入队，防止上线瞬间把陈年单子全推一遍。
    """
    if settings.g('dingtalk:notify_new_approval', '1') == '0':
        return
    if not pending:
        return
    try:
        days = int(settings.g('dingtalk:notify_approval_days', '7') or 7)
    except Exception:
        days = 7
    floor = (time.time() - days * 86400) * 1000
    first = not (db.cfg_get('dingtalk:approval_notify_at') or '').strip()
    if first:
        db.cfg_set({'dingtalk:approval_notify_at': db.now()})
        say('审批提醒：首次运行只登记不推送，下一轮开始提醒')
        return

    n = 0
    for pid, items in pending.items():
        seen = set()
        for ms, title, src, pidv, link in items:
            if src not in ('待我审批', '抄送我'):
                continue
            if not ms or ms < floor or pidv in seen:
                continue
            seen.add(pidv)
            if notify.push(pid, 'approval' if src == '待我审批' else 'cc',
                           title, link, ms):
                n += 1
    if n:
        say('审批提醒：%d 条新审批进入本轮汇总（%d 天内）' % (n, days))


def _origin_name(uid, raw_title=''):
    """发起人姓名：通讯录 → 本地账号表 → 详情标题里的「张三提交的」前缀

    已离职的人不在通讯录里（user_by_id 查不到），但标题前缀一定带名字，
    不兜这一层「发起人」列就是空的。
    """
    uid = str(uid or '')
    nm = ''
    if uid:
        try:
            nm = _ding_person_name(uid, '')
        except Exception:
            nm = ''
        if not nm:
            try:
                acc = db.account_find('dingtalk', uid)
                nm = (acc or {}).get('name') or ''
            except Exception:
                nm = ''
    if not nm:
        m = re.match(r'^(.{2,10}?)提交的', str(raw_title or ''))
        if m:
            nm = m.group(1).strip()
    return nm


def _approval_props(d, pidv, cfg, src):
    """审批详情 → 记录字段（「我发起」「抄送我」共用一套写法）"""
    title = _clean_title(d.get('title'))
    if not title or _skip_approval(title):
        return None
    stxt = STATUS_MAP.get(str(d.get('status') or ''), '审批中')
    if stxt == '已通过' and str(d.get('result') or '').lower() == 'refuse':
        stxt = '已拒绝'
    props = {
        '审批标题': title,
        '审批类型': _type_of(title),
        '发起人': _origin_name(d.get('originator'), d.get('title')),
        '审批状态': stxt,
        '审批编号': pidv,
        '审批来源': src,
        '来源应用': '钉钉',
        '处理链接': _oa_link(cfg, pidv),
    }
    if d.get('create'):
        props['发起时间'] = _iso(d.get('create'))
    if d.get('amount') is not None:
        props['金额'] = d['amount']
        props['涉及金额'] = '是'
    else:
        props['涉及金额'] = '否'
    return props


def _initiated_sync(cli, cfg, persons, stat, say):
    """按「模板 + 发起人」补齐每个人的「我发起」

    原理：/v1.0/workflow/processes/instanceIds/query 的 userIds 就是发起人过滤，
    应用权限即可用（实测未开通 Premium.Workflow.ReadWrite.All 也能查）。
    限制：必须逐个模板查，模板清单来自 DEFAULT_PROCESS_CODES + 配置追加；
    时间窗上限 120 天。
    """
    codes = _process_codes()
    uid2pid = {}
    for p in persons:
        for a in db.accounts_of(p['person_id'], 'dingtalk'):
            if a.get('account_id'):
                uid2pid[str(a['account_id'])] = p['person_id']
    if not codes or not uid2pid:
        return
    uids = list(uid2pid.keys())
    stat.setdefault('init_new', 0)
    stat.setdefault('init_upd', 0)
    stat.setdefault('init_result', 0)
    # 已有状态快照：「我发起」的单子从审批中变成通过/拒绝时要发一条结果通知，
    # 否则自己提的单子办完了都不知道
    old_status = {}
    for p in persons:
        try:
            rows = db.query(db.TABLES['ding'], size=100000,
                            person_id=p['person_id'])['results']
        except Exception:
            rows = []
        for r in rows:
            no = str(r.get('审批编号') or '')
            if no:
                old_status[(p['person_id'], no)] = str(r.get('审批状态') or '')
    seen, hit = set(), 0
    for code in codes:
        try:
            ids = cli.instance_ids(code, uids, days=120)
        except DingError as e:
            stat['err'].append('我发起（模板 %s）：%s' % (code[-8:], e))
            continue
        for pidv in ids:
            if pidv in seen:
                continue
            seen.add(pidv)
            try:
                d = cli.approval_detail(pidv)
            except DingError:
                continue
            owner = uid2pid.get(str(d.get('originator') or ''))
            if not owner:
                continue          # 不是本次范围内的人发起的，交给原审批同步处理
            props = _approval_props(d, pidv, cfg, '我发起')
            if not props:
                continue
            if _upsert(db.TABLES['ding'], '审批编号', pidv, props, owner, 'dingtalk'):
                stat['init_new'] += 1
            else:
                stat['init_upd'] += 1
            # 状态变了且已办结 → 发一条结果通知（自己发起的单子，只有这个时刻需要提醒）
            cur = str(props.get('审批状态') or '')
            before = old_status.get((owner, pidv), '')
            if before and before != cur and cur in ('已通过', '已拒绝'):
                notify.push(owner, 'result', props.get('审批标题') or '',
                            props.get('处理链接') or _oa_link(cfg, pidv),
                            _ms_of(props.get('发起时间')), cur)
                stat['init_result'] += 1
            old_status[(owner, pidv)] = cur
            hit += 1
    say('我发起：%d 个模板 · 命中 %d 条' % (len(codes), hit))


def _cc_sync(cli, cfg, persons, stat, say, pending=None, cold=None):
    """按「模板 + 部门」枚举全公司实例，用详情里的 cc_userids 反推「抄送我」

    为什么这么做：钉钉的「抄送我」索引在 OA 高级版接口里（没开通，403），
    而每条审批详情的 cc_userids 用应用权限就能读。于是先把模板下的实例按
    部门捞全（dept_id_list 递归含子部门，10 个部门一次，比按人查快一个量级），
    再逐条把抄送人缓存到 dingtalk:cc_map（实例编号 → 抄送人 userid 列表）。

    有 cc_map 之后，谁再登录进来都能立刻按缓存归属，不用重新扫全公司；
    只有 CC 缓存里没有的新实例才去拉详情，每轮上限 dingtalk:cc_per_run 条。
    """
    watch = {}
    for p in persons:
        for a in db.accounts_of(p['person_id'], 'dingtalk'):
            if a.get('account_id'):
                watch[str(a['account_id'])] = p['person_id']
    if not watch:
        return
    codes = _process_codes()
    if not codes:
        return

    # 部门树 24 小时缓存一次（一级部门即可覆盖全公司，递归由接口负责）
    try:
        cached = json.loads(db.cfg_get('dingtalk:dept_ids') or '[]')
    except Exception:
        cached = []
    try:
        dept_ts = float(db.cfg_get('dingtalk:dept_ts') or 0)
    except Exception:
        dept_ts = 0
    depts = cached
    if not cached or time.time() - dept_ts > 86400:
        try:
            got = cli.dept_ids()
            if got:
                depts = got
                db.cfg_set({'dingtalk:dept_ids': json.dumps(got),
                            'dingtalk:dept_ts': str(int(time.time()))})
        except DingError as e:
            stat['err'].append('抄送我（部门树）：%s' % e)
    if not depts:
        return

    try:
        cc_map = json.loads(db.cfg_get('dingtalk:cc_map') or '{}')
    except Exception:
        cc_map = {}
    if not isinstance(cc_map, dict):
        cc_map = {}
    try:
        budget = int(settings.g('dingtalk:cc_per_run', '600') or 600)
    except Exception:
        budget = 600
    full = not db.cfg_get('dingtalk:cc_full_at')
    # 模板清单变了（新上了审批模板 / 第三方系统接了新流程）就再扫一次 120 天，
    # 否则新模板只会从当天往后补，历史单据永远进不来
    codes_sig = str(len(codes))
    if (db.cfg_get('dingtalk:cc_codes_sig') or '') != codes_sig:
        full = True
        db.cfg_set({'dingtalk:cc_codes_sig': codes_sig})
    try:
        days = int(settings.g('dingtalk:cc_days', '20') or 20)
    except Exception:
        days = 20
    if full:
        days = 120          # 首次补齐用最长窗口（接口上限 120 天）

    # 已有记录（避免把「我发起 / 待我审批 / 我已处理」覆盖成「抄送我」）
    have = {}
    for pid_p in set(watch.values()):
        try:
            rows = db.query(db.TABLES['ding'], size=100000, person_id=pid_p)['results']
        except Exception:
            rows = []
        for r in rows:
            no = str(r.get('审批编号') or '')
            if no:
                have[(pid_p, no)] = str(r.get('审批来源') or '')

    stat.setdefault('cc_new', 0)
    stat.setdefault('cc_upd', 0)
    new_n = 0
    props_cache = {}

    def attr(pidv, ccuids, origin, props):
        """把这条审批按抄送人记到各人名下（同一人已有记录就不动）"""
        nonlocal new_n
        if not props or not ccuids:
            return
        for uid in ccuids:
            owner = watch.get(uid)
            if not owner or uid == origin:
                continue        # 自己的单子归「我发起」，不重复记抄送
            if have.get((owner, pidv)):
                continue
            if _upsert(db.TABLES['ding'], '审批编号', pidv, props, owner, 'dingtalk'):
                stat['cc_new'] += 1
                new_n += 1
                if pending is not None and owner not in (cold or ()):
                    pending.setdefault(owner, []).append((
                        _ms_of(props.get('发起时间')), props.get('审批标题') or '',
                        '抄送我', pidv, props.get('处理链接') or _oa_link(cfg, pidv)))
            else:
                stat['cc_upd'] += 1
            have[(owner, pidv)] = '抄送我'

    def take(pidv):
        """拉一条详情并缓存抄送人，返回 (抄送人列表, 字段, 发起人)"""
        nonlocal budget
        try:
            d = cli.approval_detail(pidv)
        except DingError:
            cc_map[pidv] = []        # 拿不到详情的别再反复试
            return [], None, ''
        budget -= 1
        ccu = [str(u) for u in (d.get('cc') or [])]
        cc_map[pidv] = ccu
        props = _approval_props(d, pidv, cfg, '抄送我')
        props_cache[pidv] = props
        return ccu, props, str(d.get('originator') or '')

    # 1) 老版本只记了「核对过哪些单」，没有抄送人缓存 —— 先补这一类
    try:
        legacy = json.loads(db.cfg_get('dingtalk:cc_done') or '[]')
    except Exception:
        legacy = []
    for pidv in [x for x in legacy if isinstance(x, str) and x not in cc_map]:
        if budget <= 0:
            break
        ccu, props, origin = take(pidv)
        attr(pidv, ccu, origin, props)

    # 2) 按模板 + 部门枚举，新实例补详情
    for code in codes:
        if budget <= 0:
            break
        try:
            ids = cli.instances_by_dept(code, depts, days=days)
        except DingError as e:
            stat['err'].append('抄送我（模板 %s）：%s' % (code[-8:], e))
            continue
        for pidv in ids:
            if pidv in cc_map or budget <= 0:
                continue
            ccu, props, origin = take(pidv)
            attr(pidv, ccu, origin, props)

    # 3) 缓存里命中但这个人还没记录（新员工首次登录）→ 按需补写
    for pidv, ccu in list(cc_map.items()):
        need = [u for u in (ccu or []) if u in watch and not have.get((watch[u], pidv))]
        if not need:
            continue
        props = props_cache.get(pidv)
        origin = ''
        if props is None:
            if budget <= 0:
                continue        # 这轮额度用完，下一轮接着补
            try:
                d = cli.approval_detail(pidv)
            except DingError:
                continue
            budget -= 1
            props = _approval_props(d, pidv, cfg, '抄送我')
            props_cache[pidv] = props
            origin = str(d.get('originator') or '')
        attr(pidv, need, origin, props)

    # 4) 还在「审批中」的抄送单，回读一次状态（数量少，不占预算）
    for pid_p in set(watch.values()):
        try:
            rows = db.query(db.TABLES['ding'], size=100000, person_id=pid_p)['results']
        except Exception:
            continue
        for r in rows:
            if str(r.get('审批来源') or '') != '抄送我':
                continue
            if str(r.get('审批状态') or '') != '审批中':
                continue
            rid = r.get('record_id') or r.get('_id')
            pidv = str(r.get('审批编号') or '')
            if not rid or not pidv:
                continue
            try:
                d = cli.approval_detail(pidv)
            except DingError:
                continue
            props = _approval_props(d, pidv, cfg, '抄送我')
            if props:
                db.update_record(db.TABLES['ding'], rid, {'审批状态': props['审批状态']},
                                 person_id=pid_p)

    # 缓存只留最近 6000 条，避免配置无限膨胀
    items = list(cc_map.items())[-6000:]
    db.cfg_set({'dingtalk:cc_map': json.dumps(dict(items))})
    if full and budget > 0:
        db.cfg_set({'dingtalk:cc_full_at': str(int(time.time()))})
    say('抄送我：%d 个模板 · 新增 %d 条 / 覆盖 %d 人（已缓存 %d 单的抄送人）'
        % (len(codes), new_n, len(watch), len(cc_map)))


def _wiki_sync(cli, unionid, pid, stat):
    """钉钉知识库全量层级 → 资料表（和离线快照同构：知识库/节点ID/父节点ID/层级/排序/子项数）。

    - 需要 Document.Workspace.Read + Document.Node.Read；没开通时降级为一次性日志提示
    - 全量走一遍要几百个请求，每人 24 小时最多自动跑一次（wiki:full_at:<pid>）
      说明：按人计时是必须的——资料库现在按钉钉权限（每人只看到自己有权看到的），
      若沿用全局时间闸门，只有当天第一个同步的人会被枚举，其他人资料库会是空的。
    """
    if not unionid or not cli.wiki_ok():
        return
    _gate = 'wiki:full_at:%s' % (pid or '')
    last = float(db.cfg_get(_gate) or 0)
    if time.time() - last < 86400:
        return
    try:
        spaces = cli.wiki_spaces(unionid)
    except DingError:
        if not cli.wiki_ok():
            stat['err'].append('钉钉知识库同步需要 Document.Workspace.Read 权限：'
                               + cli.apply_url('Document.Workspace.Read'))
            return
        raise
    ext2type = {'adoc': '文档', 'md': '文档', 'doc': '文档', 'docx': '文档',
                'xlsx': '表格', 'xls': '表格', 'csv': '表格', 'pdf': 'PDF',
                'pptx': '演示文稿', 'ppt': '演示文稿',
                'png': '图片', 'jpg': '图片', 'jpeg': '图片', 'gif': '图片', 'svg': '图片'}
    root = '__root__'
    for sp in spaces:
        wid, sname, surl = sp['id'], sp['name'], sp.get('url') or ''
        level = [(None, None, 0, '')]
        seen = set()
        kids = {}
        used = 0
        budget = 1200
        new_n = upd_n = 0
        while level and used < budget:
            nxt = []
            for parent, pkey, depth, ppath in level:
                try:
                    items, token = cli.wiki_nodes(wid, unionid, parent)
                    used += 1
                    while token and used < budget:
                        items2, token = cli.wiki_nodes(wid, unionid, parent, next_token=token)
                        items = items + items2
                        used += 1
                except DingError as e:
                    if not cli.wiki_ok():
                        stat['err'].append('钉钉知识库同步需要 Document.Node.Read 权限：'
                                           + cli.apply_url('Document.Node.Read'))
                        return
                    stat['err'].append('知识库「%s」节点：%s' % (sname[:16], e))
                    break
                kids[pkey] = len(items) if pkey else 0
                for idx, nd in enumerate(items):
                    nid = str(nd.get('nodeId') or nd.get('dentryId') or nd.get('id') or '').strip()
                    if not nid or nid in seen:
                        continue
                    seen.add(nid)
                    name = str(nd.get('name') or nd.get('title') or '（无标题）').strip()[:200]
                    ext = str(nd.get('extension') or '').lower()
                    ntype = str(nd.get('nodeType') or nd.get('type') or '')
                    is_folder = ntype.lower() in ('folder', 'folder;') or ntype == 'FOLDER'
                    ftype = '文件夹' if is_folder else ext2type.get(ext, '文档' if 'doc' in ext else '其他')
                    props = {
                        '文件名称': name, '类型': ftype,
                        '知识库': ('钉钉/' + sname)[:120],
                        '节点ID': nid,
                        '父节点ID': parent or root,
                        '层级': depth, '排序': idx,
                        '来源应用': '钉钉', '更新人': '钉钉知识库',
                    }
                    url = str(nd.get('url') or nd.get('docUrl') or '').strip()
                    if url:
                        props['链接'] = url[:500]
                    if not is_folder and nid:
                        props['文档UUID'] = nid       # wiki 节点ID 即 dentryUuid，站内可直接查看
                    if ppath:
                        props['层级路径'] = ppath[:200]
                    if surl:
                        props['空间链接'] = surl[:300]
                    mt = nd.get('modifiedTime') or nd.get('updateTime') or ''
                    mts = _fmt_dt(str(mt)) if mt else ''
                    if mts:
                        props['更新时间'] = mts
                    if _upsert(db.TABLES['files'], '节点ID', nid, props, pid, 'dingtalk'):
                        new_n += 1
                    else:
                        upd_n += 1
                    has_children = nd.get('hasChildren')
                    if has_children is None:
                        has_children = is_folder
                    if has_children:
                        nxt.append((nid, nid, depth + 1, (ppath + '/' + name) if ppath else name))
            level = nxt
        # 回填子项数（目录在下一层被拉到时才知道有几个孩子）
        for nid2, cnt in kids.items():
            if not nid2:
                continue
            rid = db.find_record(db.TABLES['files'], '节点ID', nid2, pid)
            if rid:
                db.update_record(db.TABLES['files'], rid, {'子项数': cnt}, person_id=pid)
        if used:
            say('知识库「%s」：%d 节点（新 %d / 更新 %d）' % (sname[:16], len(seen), new_n, upd_n))
    db.cfg_set({_gate: str(int(time.time()))})


def _approval_items(cli, uid, state, name):
    """取某人要入库的审批：[(wstatus, item, 来源标签)]

    优先用旧接口 workrecord（能拿到「待我审批」+「已办」）；
    一旦遇到 60011（缺 qyapi_work_record），整个进程内降级为
    workrecord_tasks —— 这个接口走「审批任务」权限，实测不新开权限就能
    同时拿到待办（status=0）和已办历史（status=1），只有「我发起」拿不到。
    """
    out = []
    if state['legacy']:
        for st in (0, 1):
            try:
                for it in cli.workrecord(uid, status=st):
                    out.append((st, it, '我已处理' if st == 1 else '待我审批'))
            except DingError as e:
                msg = str(e)
                if 'qyapi_work_record' in msg or '60011' in msg:
                    state['legacy'] = False
                    if not state['notified']:
                        state['notified'] = True
                        state['err'].append(
                            '审批总表（workrecord）需 qyapi_work_record，已改用「审批任务」接口：'
                            '待办 + 已办历史正常同步，仅「我发起」缺失：'
                            + cli.apply_url('qyapi_work_record'))
                    break
                state['err'].append('%s 审批列表：%s' % (name, e))
    if not state['legacy']:
        for st, src in ((0, '待我审批'), (1, '我已处理')):
            try:
                for t in cli.workrecord_tasks(uid, status=st):
                    out.append((st, t, src))
            except DingError as e:
                state['err'].append('%s %s：%s' % (name, src, e))

    # 「我发起」「抄送我」：钉钉审批中心专享接口（OA 高级版
    # Premium.Workflow.ReadWrite.All）。没开通就一次性提示并置标记，
    # 之后不再每轮重试，前端据此显示「需开通」而不是「暂无数据」。
    if state.get('premium', True):
        for kind, src in (('launched', '我发起'), ('noticed', '抄送我')):
            try:
                items = cli.premium_instances(uid, kind, pages=5)
                for it in items:
                    out.append((None, it, src))
                state['premium_seen'] = True
            except DingError as e:
                raw = (getattr(e, 'raw', '') or '') + str(e)
                if getattr(e, 'code', None) == 403 or 'Premium' in raw:
                    state['premium'] = False
                    try:
                        db.cfg_set({'dingtalk:premium_ok': '0'})
                    except Exception:
                        pass
                    if not state['premium_notified']:
                        state['premium_notified'] = True
                        state['err'].append(
                            '「我发起 / 抄送我」高级版接口未开通（已在用模板 + 部门枚举补齐，'
                            '这条提示可忽略）；如需官方接口：' + cli.apply_url('Premium.Workflow.ReadWrite.All'))
                    break
                state['err'].append('%s %s：%s' % (name, src, e))
        else:
            if state.get('premium_seen'):
                try:
                    db.cfg_set({'dingtalk:premium_ok': '1'})
                except Exception:
                    pass
    return out


def _legacy_person(pc):
    """兼容老配置：只有 user_id 时自动建一个人"""
    uid = (pc.get('user_id') or '').strip()
    if not uid:
        return None
    acc = db.account_find('dingtalk', uid)
    if acc:
        return acc['person_id']
    name = db.cfg_all().get('owner_name') or '我'
    pid = match.ingest_contact('dingtalk', uid, name=name)
    db.cfg_set({'dingtalk:user_id': uid})
    return pid


# ── 飞书 ──────────────────────────────────────────────────────────────
def sync_feishu(cfg, say):
    pc = settings.get_platform('feishu')
    if not (pc.get('app_id') and pc.get('app_secret')):
        return None
    cli = FeishuClient(pc['app_id'], pc['app_secret'])
    cli.token()
    stat = {'new': 0, 'upd': 0, 'err': []}
    persons = [p for p in db.persons_all() if db.accounts_of(p['person_id'], 'feishu')]
    if not persons:
        stat['err'].append('没有绑定飞书账号的人员（先拉取飞书通讯录）')
        return stat
    for p in persons:
        acc = db.accounts_of(p['person_id'], 'feishu')[0]
        name = p.get('name') or ''
        say('飞书：%s' % name)
        try:
            tasks = cli.approval_tasks(acc['account_id'])
        except FeishuError as e:
            stat['err'].append('%s 审批：%s' % (name, e))
            continue
        for t in tasks:
            code = t['instance_code'] or t['task_id']
            props = {
                '审批标题': t['title'], '审批类型': _type_of(t['title']),
                '审批状态': t['status'], '审批编号': code,
                '审批来源': '待我审批', '来源应用': '飞书', '涉及金额': '否',
            }
            if t.get('create_time'):
                props['发起时间'] = t['create_time']
            if _upsert(db.TABLES['ding'], '审批编号', code, props, p['person_id'], 'feishu'):
                stat['new'] += 1
            else:
                stat['upd'] += 1
    say('飞书：审批 +%d/%d' % (stat['new'], stat['upd']))
    return stat


# ── GitHub ────────────────────────────────────────────────────────────
def sync_github(cfg, say):
    pc = settings.get_platform('github')
    if not pc.get('token'):
        return None
    cli = GithubClient(pc['token'], pc.get('repos', ''))
    stat = {'new': 0, 'upd': 0, 'err': []}
    persons = [p for p in db.persons_all() if db.accounts_of(p['person_id'], 'github')]
    if not persons:
        stat['err'].append('没有绑定 GitHub 账号的人员（管理员页手工填用户名）')
        return stat
    for p in persons:
        acc = db.accounts_of(p['person_id'], 'github')[0]
        login = acc['account_id']
        name = p.get('name') or login
        say('GitHub：%s' % login)
        try:
            issues = cli.issues_for(login) if cli.repos else cli.my_issues()
        except GithubError as e:
            stat['err'].append('%s：%s' % (name, e))
            continue
        for it in issues:
            props = {
                '任务名称': it['title'], '状态': it['status'],
                '所属项目': 'GitHub' + (('/' + it['repo']) if it['repo'] else ''),
                '来源应用': 'GitHub', '负责人': name,
                '处理链接': it['url'],
                '说明': ('标签：' + it['labels']) if it['labels'] else '',
            }
            if it.get('updated'):
                props['截止时间'] = it['updated']
            if _upsert(db.TABLES['task'], '处理链接', it['url'], props, p['person_id'], 'github'):
                stat['new'] += 1
            else:
                stat['upd'] += 1
    say('GitHub：任务 +%d/%d' % (stat['new'], stat['upd']))
    return stat


# ── 金蝶云星空 ────────────────────────────────────────────────────────
def sync_kingdee(cfg, say):
    pc = settings.get_platform('kingdee')
    if not (pc.get('host') and pc.get('acct_id') and pc.get('app_id') and pc.get('app_secret')):
        return None
    cli = KingdeeClient(pc['host'], pc['acct_id'], pc['app_id'], pc['app_secret'], pc.get('user', ''))
    cli.login()
    forms = [f.strip() for f in (cfg.get('kingdee:forms') or 'PUR_PurchaseOrder,SAL_SaleOrder').split(',')
             if f.strip()]
    stat = {'new': 0, 'upd': 0, 'err': []}
    persons = [p for p in db.persons_all() if db.accounts_of(p['person_id'], 'kingdee')]
    if not persons:
        stat['err'].append('没有绑定金蝶用户的人员（管理员页做人员绑定）')
        return stat
    for p in persons:
        acc = db.accounts_of(p['person_id'], 'kingdee')[0]
        code = acc['account_id']
        name = p.get('name') or code
        say('金蝶：%s' % name)
        try:
            bills = cli.pending_bills(code, forms)
        except KingdeeError as e:
            stat['err'].append('%s：%s' % (name, e))
            continue
        for b in bills:
            props = {
                '审批标题': b['title'],
                '审批类型': _type_of_form(b.get('form')),
                '审批状态': b['status'],
                '审批编号': b['id'],
                '审批来源': '待我审批',
                '来源应用': '金蝶',
                '发起人': b['creator'],
                '涉及金额': '否',
            }
            if b.get('date'):
                props['发起时间'] = b['date']
            if _upsert(db.TABLES['ding'], '审批编号', b['id'], props, p['person_id'], 'kingdee'):
                stat['new'] += 1
            else:
                stat['upd'] += 1
    say('金蝶：单据 +%d/%d' % (stat['new'], stat['upd']))
    return stat


# ── GitLab ────────────────────────────────────────────────────────────
def sync_gitlab(cfg, say):
    pc = settings.get_platform('gitlab')
    if not (pc.get('host') and pc.get('token')):
        return None
    cli = GitlabClient(pc['host'], pc['token'])
    stat = {'new': 0, 'upd': 0, 'err': []}
    persons = [p for p in db.persons_all() if db.accounts_of(p['person_id'], 'gitlab')]
    if not persons:
        stat['err'].append('没有绑定 GitLab 账号的人员（可先拉取 GitLab 用户自动匹配）')
        return stat
    for p in persons:
        acc = db.accounts_of(p['person_id'], 'gitlab')[0]
        login = acc['account_id']
        name = p.get('name') or login
        say('GitLab：%s' % login)
        try:
            items = cli.merge_requests(login) + cli.issues(login)
        except GitlabError as e:
            stat['err'].append('%s：%s' % (name, e))
            continue
        for it in items:
            props = {
                '任务名称': it['title'], '状态': it['status'],
                '所属项目': 'GitLab' + (('/' + it['repo']) if it['repo'] else ''),
                '来源应用': 'GitLab', '负责人': name,
                '处理链接': it['link'],
                '说明': ('标签：' + it['labels']) if it.get('labels') else '',
            }
            if it.get('updated'):
                props['截止时间'] = it['updated']
            if _upsert(db.TABLES['task'], '处理链接', it['link'], props, p['person_id'], 'gitlab'):
                stat['new'] += 1
            else:
                stat['upd'] += 1
    say('GitLab：任务 +%d/%d' % (stat['new'], stat['upd']))
    return stat


# ── Gitea / Gogs ──────────────────────────────────────────────────────
def sync_gitea(cfg, say):
    pc = settings.get_platform('gitea')
    if not (pc.get('host') and pc.get('token')):
        return None
    cli = GiteaClient(pc['host'], pc['token'], pc.get('org', ''))
    stat = {'new': 0, 'upd': 0, 'err': []}
    persons = [p for p in db.persons_all() if db.accounts_of(p['person_id'], 'gitea')]
    if not persons:
        stat['err'].append('没有绑定 Gitea 账号的人员（可先拉取 Gitea 用户自动匹配）')
        return stat
    for p in persons:
        acc = db.accounts_of(p['person_id'], 'gitea')[0]
        login = acc['account_id']
        name = p.get('name') or login
        say('Gitea：%s' % login)
        try:
            items = cli.issues(login, kinds=('issue',)) + cli.issues(login, kinds=('pr',))
        except GiteaError as e:
            stat['err'].append('%s：%s' % (name, e))
            continue
        for it in items:
            props = {
                '任务名称': it['title'], '状态': it['status'],
                '所属项目': ('Gitea/' + it['repo']) if it.get('repo') else 'Gitea',
                '来源应用': 'Gitea', '负责人': name,
                '处理链接': it['link'],
                '说明': ('标签：' + it['labels']) if it.get('labels') else '',
            }
            if it.get('updated'):
                props['截止时间'] = it['updated']
            if _upsert(db.TABLES['task'], '处理链接', it['link'], props, p['person_id'], 'gitea'):
                stat['new'] += 1
            else:
                stat['upd'] += 1
    say('Gitea：任务 +%d/%d' % (stat['new'], stat['upd']))
    return stat


# ── 禅道 ──────────────────────────────────────────────────────────────
ZENTAO_KIND = {'task': '禅道任务', 'bug': '禅道Bug', 'todo': '禅道待办'}


def sync_zentao(cfg, say):
    pc = settings.get_platform('zentao')
    if not settings.platform_ready('zentao'):
        return None
    cli = ZentaoClient(pc['host'], pc.get('account', ''), pc.get('password', ''))
    cli.login()
    stat = {'new': 0, 'upd': 0, 'err': []}
    persons = [p for p in db.persons_all() if db.accounts_of(p['person_id'], 'zentao')]
    if not persons:
        stat['err'].append('没有绑定禅道账号的人员（可先拉取禅道用户自动匹配）')
        return stat
    for p in persons:
        acc = db.accounts_of(p['person_id'], 'zentao')[0]
        code = acc['account_id']
        name = p.get('name') or code
        say('禅道：%s' % name)
        for kind in ('task', 'bug', 'todo'):
            try:
                items = cli.items(kind, code)
            except ZentaoError as e:
                stat['err'].append('%s %s：%s' % (name, kind, e))
                continue
            for it in items:
                props = {
                    '任务名称': it['title'], '状态': it['status'],
                    '所属项目': ZENTAO_KIND[kind], '来源应用': '禅道', '负责人': name,
                    '处理链接': it['link'],
                    '说明': ('优先级 %s' % it['pri']) if it.get('pri') not in ('', '0') else '',
                }
                if it.get('date'):
                    props['截止时间'] = it['date']
                if _upsert(db.TABLES['task'], '处理链接', it['link'], props, p['person_id'], 'zentao'):
                    stat['new'] += 1
                else:
                    stat['upd'] += 1
    say('禅道：任务 +%d/%d' % (stat['new'], stat['upd']))
    return stat


# ── 用友 ──────────────────────────────────────────────────────────────
def sync_yonyou(cfg, say):
    pc = settings.get_platform('yonyou')
    if not settings.platform_ready('yonyou'):
        return None
    cli = YonyouClient(pc.get('host', ''), pc.get('app_key', ''), pc.get('app_secret', ''),
                       pc.get('ent_code', ''), pc.get('user', ''), pc.get('todo_path', ''))
    stat = {'new': 0, 'upd': 0, 'err': []}
    persons = [p for p in db.persons_all() if db.accounts_of(p['person_id'], 'yonyou')]
    if not persons:
        stat['err'].append('没有绑定用友账号的人员（人员绑定里手工填写用户编码）')
        return stat
    try:
        items = cli.todo_items()
    except YonyouError as e:
        stat['err'].append('用友待办接口：%s' % e)
        say('用友：%s' % e)
        return stat
    for p in persons:
        acc = db.accounts_of(p['person_id'], 'yonyou')[0]
        code = acc['account_id']
        name = p.get('name') or code
        say('用友：%s' % name)
        for it in items:
            props = {
                '审批标题': it['title'],
                '审批类型': _type_of(it['title']),
                '审批状态': it['status'],
                '审批编号': it['id'],
                '审批来源': '待我审批', '来源应用': '用友',
                '发起人': it.get('creator') or '', '涉及金额': '否',
            }
            if it.get('date'):
                props['发起时间'] = it['date']
            if it.get('link'):
                props['处理链接'] = it['link']
            if _upsert(db.TABLES['ding'], '审批编号', it['id'], props, p['person_id'], 'yonyou'):
                stat['new'] += 1
            else:
                stat['upd'] += 1
    say('用友：待办 +%d/%d' % (stat['new'], stat['upd']))
    return stat


# ── 调度 ──────────────────────────────────────────────────────────────
def sync_all(progress=None):
    say = progress or (lambda m: None)
    # 跨进程互斥：应用首次同步 / 定时器 / 手动触发可能并发，
    # 并发写会产生重复记录，这里用文件锁保证同一时刻只有一轮同步
    lock_path = os.path.join(os.path.dirname(db.DB_PATH) or '/tmp', 'sync.lock')
    lock_f = open(lock_path, 'w')
    try:
        import fcntl
        try:
            fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            say('已有同步在运行，本轮跳过')
            return {'skipped': True}
    except ImportError:
        pass
    cfg = db.cfg_all()
    result, errors = {}, []
    for name, fn in (('dingtalk', sync_dingtalk), ('feishu', sync_feishu),
                     ('github', sync_github), ('gitlab', sync_gitlab), ('gitea', sync_gitea),
                     ('zentao', sync_zentao), ('kingdee', sync_kingdee), ('yonyou', sync_yonyou)):
        try:
            st = fn(cfg, say)
            if st is None:
                continue
            result[name] = st
            if st.get('err'):
                errors.extend('%s: %s' % (name, e) for e in st['err'][:3])
            db.cfg_set({'last_sync:' + name: db.now()})
        except Exception as e:
            errors.append('%s: %s' % (name, e))
            db.log('sync', False, 0, '%s 同步失败：%s' % (name, e))
    db.cfg_set({'last_sync': db.now()})
    result['errors'] = errors
    return result


def _summary(st):
    parts = []
    if 'dingtalk' in st:
        d = st['dingtalk']
        extra = ''
        if d.get('todo_push'):
            extra += ' 推送%d' % d['todo_push']
        if d.get('todo_done'):
            extra += ' 回写完成%d' % d['todo_done']
        parts.append('钉钉 待办+%d 审批+%d%s' % (d['todo_new'], d['ding_new'], extra))
    if 'feishu' in st:
        parts.append('飞书 +%d' % st['feishu']['new'])
    if 'github' in st:
        parts.append('GitHub +%d' % st['github']['new'])
    if 'gitlab' in st:
        parts.append('GitLab +%d' % st['gitlab']['new'])
    if 'gitea' in st:
        parts.append('Gitea +%d' % st['gitea']['new'])
    if 'zentao' in st:
        parts.append('禅道 +%d' % st['zentao']['new'])
    if 'kingdee' in st:
        parts.append('金蝶 +%d' % st['kingdee']['new'])
    if 'yonyou' in st:
        parts.append('用友 +%d' % st['yonyou']['new'])
    return ' · '.join(parts) or '没有可同步的平台（检查凭证）'


def run_async(progress_sink=None):
    if not _running.acquire(blocking=False):
        return False, '已有同步任务在跑'

    def job():
        def sink(msg):
            # 进度落到配置里，管理员右下角的同步条能直接看到「同步到第几个人」
            try:
                db.cfg_set({'sync:progress': str(msg or '')[:120]})
            except Exception:
                pass
            if progress_sink:
                try:
                    progress_sink(msg)
                except Exception:
                    pass

        try:
            st = sync_all(sink)
            msg = _summary(st)
            if st.get('errors'):
                msg += ' | 部分失败：' + '；'.join(st['errors'][:3])
            rows = sum(v.get('new', 0) + v.get('ding_new', 0) + v.get('todo_new', 0)
                       for k, v in st.items() if isinstance(v, dict))
            db.log('sync', not st.get('errors'), rows, msg)
            try:
                db.cfg_set({'sync:progress': ''})
            except Exception:
                pass
        except Exception as e:
            db.log('sync', False, 0, '同步异常：%s' % e)
        finally:
            try:
                db.cfg_set({'sync:progress': ''})
            except Exception:
                pass
            _running.release()

    threading.Thread(target=job, daemon=True).start()
    return True, '已开始同步'


def is_running():
    return _running.locked()


def test_connection(app_key, app_secret, corp_id, user_id):
    cli = DingClient(app_key, app_secret, corp_id, user_id)
    cli.token()
    return cli.user_info()
