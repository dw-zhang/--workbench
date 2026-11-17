# -*- coding: utf-8 -*-
"""项目管理：钉钉多维表格链接 → 项目档案 → 按项目同步任务

复盘定稿的设计约定：
- 一个链接（Base+Sheet）= 一个项目；同一张数据表只能绑定一个项目（唯一索引兜底）
- 钉钉侧表格是主存储，工作台单向拉取；任务行打「所属项目ID/所属项目」标签进任务表
  —— 任务管理与项目视图是同一份数据的两种看法，不重复存储
- 结构漂移检测：field_map 里的字段在某轮拉取中消失 → 项目置 paused 并告警，
  绝不静默错位归人；恢复字段后手动/自动恢复
- 去重键与 notable.py 一致：aitbl:<recordId>；已绑定项目的数据表由本项目接管同步，
  未绑定的 Base 仍走 notable.sync_notable（跳过已绑定的 sheet）
- 首轮全量静默（config 键 project:imported:<pid>），之后增量新任务才通知
"""
import json
import urllib.parse

import db
import notify
import settings
from notable import (NotableError, _base_ids, _cell_date, _cell_members, _cell_text,
                     _iter_records, _list_sheets, _operator_id, _person_map, _pick,
                     _req, _token, API, PRIORITY_MAP, STATUS_MAP,
                     DUE_NAMES, PRIORITY_NAMES, STATUS_NAMES, TITLE_NAMES, USER_HINTS)

# 字段映射的角色键（field_map 的合法键）
ROLES = ('title', 'status', 'priority', 'due', 'user')
ROLE_NAMES = {'title': '任务名称', 'status': '状态', 'priority': '优先级',
              'due': '截止时间', 'user': '执行人'}


# ── 链接解析 + 结构分析 ───────────────────────────────────────────────
def _sheet_id(sheet):
    if isinstance(sheet, dict):
        return str(sheet.get('id') or sheet.get('sheetId') or sheet.get('sheetIdOrName') or '')
    return str(sheet)


def _sheet_name(sheet):
    if isinstance(sheet, dict):
        return str(sheet.get('name') or sheet.get('sheetName') or '')
    return ''


def _guess_roles(names, recs):
    """列语义推断：列名提示（高置信）+ 值形态扫描（中置信）"""
    roles = {}

    def by_name(hints):
        for h in hints:
            if h in names:
                return h, '高'
        return '', ''

    f, c = by_name(TITLE_NAMES)
    if f:
        roles['title'] = {'field': f, 'confidence': c}
    f, c = by_name(STATUS_NAMES)
    if f:
        roles['status'] = {'field': f, 'confidence': c}
    f, c = by_name(PRIORITY_NAMES)
    if f:
        roles['priority'] = {'field': f, 'confidence': c}
    f, c = by_name(DUE_NAMES)
    if f:
        roles['due'] = {'field': f, 'confidence': c}
    f, c = by_name(USER_HINTS)
    if f:
        roles['user'] = {'field': f, 'confidence': c}

    # 值形态兜底（只补缺的角色，避免覆盖列名判断）
    for n in sorted(names):
        if not recs:
            break
        vals = [(r.get('fields') or {}).get(n) for r in recs[:50]]
        vals = [v for v in vals if v not in (None, '', [])]
        if not vals:
            continue
        if 'user' not in roles and isinstance(vals[0], list) and vals[0] and \
                isinstance(vals[0][0], dict) and (vals[0][0].get('unionId') or
                                                  vals[0][0].get('userId') or
                                                  vals[0][0].get('nickName')):
            roles['user'] = {'field': n, 'confidence': '中'}
        elif 'due' not in roles and sum(1 for v in vals if _cell_date(v)) >= len(vals) * 0.6:
            roles['due'] = {'field': n, 'confidence': '中'}
    return roles


def analyze_link(link):
    """解析链接 → 枚举数据表 → 每张表拉样本推断字段结构（只读）"""
    bids = _base_ids(link)
    if not bids:
        raise NotableError('链接里没有找到 Base 标识（应为 alidocs.dingtalk.com/i/nodes/... 形式的链接或 16 位以上 baseId）')
    bid = bids[0]
    tok = _token()
    sheets = _list_sheets(tok, bid)
    out = {'base_id': bid, 'url': 'https://alidocs.dingtalk.com/i/nodes/%s' % bid,
           'sheets': []}
    for sh in sheets[:20]:
        sid = _sheet_id(sh)
        sname = _sheet_name(sh)
        if not sid:
            continue
        bound = db.project_by_sheet(bid, sid)
        try:
            _, recs = _iter_records(tok, bid, sid)
        except NotableError as e:
            out['sheets'].append({'id': sid, 'name': sname, 'rows': 0, 'error': str(e),
                                  'bound': bool(bound)})
            continue
        names = set()
        for r in recs[:100]:
            names.update((r.get('fields') or {}).keys())
        sample = []
        for r in recs[:3]:
            f = r.get('fields') or {}
            sample.append({k: _cell_text(v)[:40] for k, v in list(f.items())[:8]})
        out['sheets'].append({'id': sid, 'name': sname, 'rows': len(recs),
                              'fields': sorted(names), 'roles': _guess_roles(names, recs),
                              'sample': sample, 'bound': bool(bound),
                              'bound_project': (bound or {}).get('name', '')})
    return out


# ── 项目 CRUD（供 API 调用） ──────────────────────────────────────────
def create_project(name, base_id, sheet_id, sheet_name='', url='', field_map=None,
                   owner_person_id=''):
    name = str(name or sheet_name or '未命名项目').strip()[:60]
    if not base_id or not sheet_id:
        raise NotableError('缺少 baseId 或 sheetId')
    if db.project_by_sheet(base_id, sheet_id):
        raise NotableError('这张数据表已绑定项目「%s」，一张表只能是一个项目' %
                           db.project_by_sheet(base_id, sheet_id)['name'])
    fm = {k: str((field_map or {}).get(k) or '').strip() for k in ROLES}
    if not fm['title'] or not fm['user']:
        raise NotableError('字段映射不完整：至少需要「任务名称」和「执行人」两列（否则无法按人归属）')
    return db.project_add(name, base_id, sheet_id, sheet_name=sheet_name, url=url,
                          field_map=fm, owner_person_id=owner_person_id)


def delete_project(pid, by_admin=False, by_person=''):
    p = db.project_get(pid)
    if not p:
        return False
    if not by_admin and p.get('owner_person_id') != by_person:
        return False
    db.project_del(pid)
    return True


# ── 按项目同步 ────────────────────────────────────────────────────────
def _admin_pid():
    admin = settings.g('dingtalk:admin_userid') or settings.g('dingtalk:user_id') or ''
    if admin:
        acc = db.account_find('dingtalk', admin)
        if acc:
            return acc['person_id']
    return ''


def sync_projects(cfg, say):
    prjs = db.projects_all('active')
    if not prjs:
        return None
    pc = settings.get_platform('notable') or {}
    notify_open = str(pc.get('notify_open') or '1') != '0'
    tok = _token()
    u2p, i2n = _person_map()
    admin_pid = _admin_pid()
    stat = {'new': 0, 'upd': 0, 'skip': 0, 'unmatched': 0, 'paused': 0, 'err': []}

    for p in prjs:
        pid, pname = p['project_id'], p['name']
        fm = p.get('field_map') or {}
        if not fm.get('title') or not fm.get('user'):
            stat['err'].append('项目 %s：字段映射不完整，跳过' % pname)
            continue
        first = not settings.g('project:imported:%s' % pid)
        try:
            sname, recs = _iter_records(tok, p['base_id'], p['sheet_id'])
        except NotableError as e:
            stat['err'].append('项目 %s：%s' % (pname, e))
            continue
        if not recs:
            say('项目 %s：数据表为空' % pname)
            continue
        # 结构漂移检测：映射字段必须在本次拉取里仍然存在
        names = set()
        for r in recs:
            names.update((r.get('fields') or {}).keys())
        missing = [ROLE_NAMES[k] for k in ('title', 'user') if fm.get(k) not in names]
        if missing:
            db.project_set(pid, status='paused')
            stat['paused'] += 1
            msg = '项目「%s」的表格结构变化（缺字段：%s），已自动暂停同步，请检查多维表格后重新启用' % (
                pname, '、'.join(missing))
            stat['err'].append(msg)
            say(msg)
            if admin_pid and notify_open:
                notify.push(admin_pid, 'todo', '⚠️ ' + msg, p.get('url') or '', extra='项目管理')
            continue

        f_title, f_status = fm['title'], fm.get('status', '')
        f_pri, f_due, f_user = fm.get('priority', ''), fm.get('due', ''), fm['user']
        new = upd = skip = 0
        for r in recs:
            f = r.get('fields') or {}
            rid = str(r.get('id') or r.get('recordId') or '')
            members = _cell_members(f.get(f_user))
            if not members:
                skip += 1
                continue
            title = ' '.join(_cell_text(f.get(f_title)).split())[:120]
            if not title:
                skip += 1
                continue
            status = STATUS_MAP.get(_cell_text(f.get(f_status)),
                                    _cell_text(f.get(f_status)) or '待处理') if f_status else '待处理'
            pri = PRIORITY_MAP.get(_cell_text(f.get(f_pri)), '中') if f_pri else '中'
            due = _cell_date(f.get(f_due)) if f_due else ''
            owners = [nm for _, nm in members if nm]
            props = {
                '任务名称': title, '优先级': pri, '状态': status,
                '截止时间': due, '负责人': '、'.join(owners)[:40],
                '所属项目ID': pid, '所属项目': pname[:40],
                '处理链接': p.get('url') or '', '来源应用': '钉钉多维表格',
                '钉钉任务ID': 'aitbl:%s' % rid,
            }
            for uid, nm in members:
                p2 = u2p.get(uid)
                if not p2:
                    stat['unmatched'] += 1
                    continue
                exist = db.find_record(db.TABLES['task'], '钉钉任务ID', props['钉钉任务ID'], p2)
                if exist:
                    db.update_record(db.TABLES['task'], exist, props, person_id=p2)
                    upd += 1
                else:
                    db.add_record(db.TABLES['task'], props, source='notable', person_id=p2)
                    new += 1
                    if notify_open and not first and status != '已完成':
                        notify.push(p2, 'todo', '新任务【%s】：%s' % (pname, title),
                                    p.get('url') or '', extra='项目任务')
        stat['new'] += new
        stat['upd'] += upd
        stat['skip'] += skip
        say('项目 %s：记录 %d，新 +%d / 更 %d / 空执行人 %d%s'
            % (pname, len(recs), new, upd, skip, '（首轮静默）' if first else ''))
        if first and (new or upd):
            db.cfg_set({'project:imported:%s' % pid: db.now()})
    if stat['unmatched']:
        stat['err'].append('有 %d 条成员匹配不到工作台人员（跳过）' % stat['unmatched'])
    return stat
