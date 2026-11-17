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
import re
import urllib.parse

import db
import notify
import settings
from notable import (NotableError, _base_ids, _cell_date, _cell_members, _cell_text,
                     _iter_records, _list_sheets, _operator_id, _person_map, _pick,
                     _req, _token, API, PRIORITY_MAP, STATUS_MAP,
                     DUE_NAMES, PRIORITY_NAMES, STATUS_NAMES, TITLE_NAMES, USER_HINTS,
                     create_record, delete_record)

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


def _record_title_text(fields):
    """一行记录的显示文本：优先任务/名称类字段，否则取第一个有值的字段"""
    fields = fields or {}
    tset = set(TITLE_NAMES)
    for k, v in fields.items():
        if k in tset:
            s = _cell_text(v)
            if s:
                return s
    for k, v in fields.items():
        s = _cell_text(v)
        if s:
            return s
    return ''


def _link_map(tok, base_id):
    """整库记录 ID → 主键显示文本 的映射，用于把关联记录字段解析成可读文本"""
    out = {}
    try:
        for sh in _list_sheets(tok, base_id):
            sid = _sheet_id(sh)
            if not sid:
                continue
            try:
                _, recs = _iter_records(tok, base_id, sid)
            except NotableError:
                continue
            for r in recs:
                t = _record_title_text(r.get('fields') or {})
                rid = str(r.get('id') or '')
                if rid and t:
                    out[rid] = t
    except Exception:
        pass
    return out


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


def _member_text(v, linkmap):
    """人员/关联字段值 → 姓名文本（关联记录经 linkmap 解析成姓名，文本原样）"""
    if isinstance(v, dict) and 'linkedRecordIds' in v:
        ids = v.get('linkedRecordIds') or []
        return '、'.join(x for x in ((linkmap or {}).get(str(i)) for i in ids) if x)
    return _cell_text(v)


def _name_map():
    """钉钉账号姓名 → person_id（姓名降级归属用）；persons 表真名优先，跳过脱敏名"""
    out = {}
    c = db.connect()
    with db._lock:
        rows = c.execute("SELECT a.person_id, p.name, a.name FROM accounts a "
                         "LEFT JOIN persons p ON p.person_id = a.person_id "
                         "WHERE a.platform='dingtalk'").fetchall()
    for pid, pname, aname in rows:
        nm = (pname or aname or '').strip()
        if nm and '*' not in nm:
            out.setdefault(nm, pid)
    return out


def _status_norm(txt):
    """状态归一：先精确映射，再按关键词兜底（适配「延期完成/按时完成/进行中/未开始」等自定义状态）"""
    s = str(txt or '').strip()
    if not s:
        return '待处理'
    if s in STATUS_MAP:
        return STATUS_MAP[s]
    if '完成' in s or '关闭' in s or '归档' in s:
        return '已完成'
    if '进行' in s or '推进' in s or '处理中' in s or '执行中' in s:
        return '进行中'
    if '未开始' in s or '待' in s or '计划' in s or '延期' in s or '逾期' in s:
        return '待处理'
    return s


def _pri_norm(txt):
    """优先级归一：高/中/低 + 紧急程度兜底"""
    s = str(txt or '').strip()
    if not s:
        return '中'
    if s in PRIORITY_MAP:
        return PRIORITY_MAP[s]
    if '紧急' in s or '严重' in s or '高' in s:
        return '高'
    if '低' in s:
        return '低'
    return '中'


def _auto_field_map(names, recs):
    """自动推断任务字段映射：精确列名 → 子串包含兜底（按提示词优先级遍历）。
    仅当能同时确定「任务名称 + 执行人」且执行人列可解析（钉钉成员 / 关联记录 / 文本姓名）时返回映射，
    否则返回 None（保持原样模式）"""
    roles = _guess_roles(set(names), recs)
    fm = {k: roles[k]['field'] for k in ROLES if roles.get(k)}

    def fuzzy(hints):
        for h in hints:            # 提示词优先级优先（'名称' 比 '任务' 更具体靠前）
            for n in sorted(names):
                if h in n:
                    return n
        return ''

    if not fm.get('title'):
        t = fuzzy(TITLE_NAMES)
        if t:
            fm['title'] = t
    if not fm.get('user'):
        u = fuzzy(USER_HINTS)
        if u:
            fm['user'] = u
    if not (fm.get('title') and fm.get('user')):
        return None
    # 执行人列可解析即可：真实成员 / 关联记录（经姓名匹配）/ 文本姓名
    ok = 0
    for r in recs[:200]:
        v = (r.get('fields') or {}).get(fm['user'])
        if _cell_members(v) or (isinstance(v, dict) and 'linkedRecordIds' in v) or _cell_text(v):
            ok += 1
    return fm if ok else None


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
LOCAL_PREFIX = 'local:'


def is_local(p):
    """本地项目：工作台手工创建、还没绑定钉钉多维表格"""
    return str((p or {}).get('base_id') or '').startswith(LOCAL_PREFIX)


def _local_map():
    return {'title': '任务名称', 'user': '负责人', 'status': '状态',
            'priority': '优先级', 'due': '截止时间'}


def create_local(name, owner_person_id=''):
    """在工作台手工建项目：先本地跑起来，之后可绑定钉钉多维表格做双向同步"""
    name = str(name or '').strip()[:60]
    if not name:
        raise NotableError('项目名称不能为空')
    return db.project_add(name, '', '', sheet_name='工作台任务', url='',
                          field_map=_local_map(), owner_person_id=owner_person_id)


def _owner_rows(p, owners):
    """执行人姓名 → [(姓名, person_id)]；匹配不到人的先记为 ('', '')"""
    n2p = _name_map()
    out = []
    for nm in owners:
        out.append((nm, n2p.get(nm) or ''))
    return out


def add_local_task(p, title, owners, status='', priority='', due=''):
    """本地项目新增任务：直接落工作台任务表（钉钉侧无写入）
    返回落库的记录 ID 列表（一个执行人一条，与多维表同步口径一致）"""
    title = ' '.join(str(title or '').split())[:120]
    if not title:
        raise NotableError('任务名称不能为空')
    owners = [str(x).strip() for x in (owners or []) if str(x).strip()]
    pairs = _owner_rows(p, owners)
    due = str(due or '')[:10]
    props = {
        '任务名称': title, '状态': status or '待处理', '优先级': priority or '中',
        '截止时间': due, '负责人': '、'.join(owners) or '未分配',
        '所属项目ID': p['project_id'], '所属项目': p['name'][:40],
        '来源应用': '工作台', '处理链接': p.get('url') or '',
    }
    ids = []
    for nm, pid in pairs:
        if pid:
            ids.append(db.add_record(db.TABLES['task'], props, source='local', person_id=pid))
    if not ids and p.get('owner_person_id'):
        # 执行人没匹配上工作台人员时，落在项目负责人名下，保证任务不丢
        ids.append(db.add_record(db.TABLES['task'], props, source='local',
                                 person_id=p['owner_person_id']))
    return ids


def del_local_task(p, record_id):
    """删本地任务行；只允许删本项目名下的记录"""
    record_id = str(record_id or '').strip()
    if not record_id:
        raise NotableError('缺少任务记录 ID')
    c = db.connect()
    with db._lock:
        row = c.execute("SELECT id, data FROM records WHERE id=? AND database='task'",
                        (record_id,)).fetchone()
        if not row:
            raise NotableError('任务不存在或已删除')
        try:
            d = json.loads(row['data'] or '{}')
        except Exception:
            d = {}
        if str(d.get('所属项目ID') or '') != p['project_id']:
            raise NotableError('该任务不属于此项目')
        c.execute('DELETE FROM records WHERE id=?', (record_id,))
        c.commit()
    return 1


def bind_sheet(p, base_id, sheet_id, sheet_name='', url='', field_map=None):
    """把本地项目绑定到一张钉钉多维表格；之后新增任务会同时写钉钉。"""
    base_id, sheet_id = str(base_id or '').strip(), str(sheet_id or '').strip()
    if not base_id or not sheet_id:
        raise NotableError('缺少 baseId 或 sheetId')
    other = db.project_by_sheet(base_id, sheet_id)
    if other and other['project_id'] != p['project_id']:
        raise NotableError('这张数据表已绑定项目「%s」' % other['name'])
    fm = {k: str((field_map or {}).get(k) or '').strip() for k in ROLES}
    # 空映射 = 原样接管（只读展示），不硬套本地列名
    db.project_set(p['project_id'], base_id=base_id, sheet_id=sheet_id,
                   sheet_name=sheet_name or '', url=url or '',
                   field_map=json.dumps(fm, ensure_ascii=False))
    return db.project_get(p['project_id'])


def push_local_tasks(p, recs):
    """把本地任务批量写进已绑定的钉钉表（每条写成功后回填钉钉任务ID，避免重复推送）"""
    u2p, _ = _person_map()
    p2u = {}
    for uid, pid in u2p.items():
        p2u.setdefault(pid, uid)
    n2p = _name_map()
    ok, errs = 0, []
    for r in recs:
        title = ' '.join(str(r.get('任务名称') or '').split())[:120]
        if not title:
            continue
        names = [x.strip() for x in str(r.get('负责人') or '').split('、') if x.strip()]
        users = [(nm, p2u.get(n2p.get(nm) or '', '')) for nm in names]
        try:
            rid, _ = add_task(p, title, users, r.get('状态') or '', r.get('优先级') or '',
                              str(r.get('截止时间') or '')[:10])
        except Exception as e:
            errs.append('%s：%s' % (title[:16], str(e)[:70]))
            continue
        db.update_record(db.TABLES['task'], r.get('record_id') or r.get('_id') or '',
                         {'钉钉任务ID': 'aitbl:%s' % rid, '来源应用': '钉钉多维表格'})
        ok += 1
    return ok, errs


def create_project(name, base_id, sheet_id, sheet_name='', url='', field_map=None,
                   owner_person_id='', mode='auto'):
    """建项目档案。
    mode='auto'（默认）：服务端自动识别任务列（任务名称+执行人），识别成功即任务项目（七视图），
                        识别不出则退化为原样镜像展示 —— 全程无需人填字段映射
    mode='raw'         ：强制原样导入（整表镜像，不做任务归属）"""
    name = str(name or sheet_name or '未命名项目').strip()[:60]
    if not base_id or not sheet_id:
        raise NotableError('缺少 baseId 或 sheetId')
    if db.project_by_sheet(base_id, sheet_id):
        raise NotableError('这张数据表已绑定项目「%s」，一张表只能是一个项目' %
                           db.project_by_sheet(base_id, sheet_id)['name'])
    fm = {k: str((field_map or {}).get(k) or '').strip() for k in ROLES}
    if mode != 'raw' and not (fm.get('title') and fm.get('user')):
        # 服务端自动识别：拉一次记录，按列名 + 值形态推断任务字段
        try:
            tok = _token()
            _, recs = _iter_records(tok, base_id, sheet_id)
            names = set()
            for r in recs:
                names.update((r.get('fields') or {}).keys())
            fm = _auto_field_map(names, recs) or {}
        except NotableError:
            fm = {}
    if not (fm.get('title') and fm.get('user')):
        fm = {}          # 原样模式
    return db.project_add(name, base_id, sheet_id, sheet_name=sheet_name, url=url,
                          field_map=fm, owner_person_id=owner_person_id)


def project_mode(p):
    """local=工作台自建（暂未绑钉钉） / task=任务项目（按人归属） / raw=原样导入（整表镜像）"""
    if is_local(p):
        return 'local'
    fm = p.get('field_map') or {}
    return 'task' if (fm.get('title') and fm.get('user')) else 'raw'


def delete_project(pid, by_admin=False, by_person=''):
    p = db.project_get(pid)
    if not p:
        return False
    if not by_admin and p.get('owner_person_id') != by_person:
        return False
    db.project_del(pid)
    return True


# ── 工作台直接新增任务（写多维表格） ──────────────────────────────────
def add_task(p, title, users, status='', priority='', due=''):
    """把一行任务写进项目绑定的多维表格（钉钉侧为主存储，写入后由同步回流）
    p: db.project_get() 的项目档案
    users: [(姓名, unionId)]；unionId 为空的成员降级为姓名文本
    返回 (recordId, fields)"""
    import datetime as _dt
    fm = p.get('field_map') or {}
    title = str(title or '').strip()
    if not title:
        raise NotableError('任务名称不能为空')
    if not fm.get('title') or not fm.get('user'):
        raise NotableError('项目字段映射不完整，无法写入')
    tok = _token()
    fields = {fm['title']: title[:120]}
    # 执行人列类型探测：取 1 行现有记录看值形态
    #   钉钉成员字段 → 写 [{'unionId':..}]；关联记录字段 → 写 {'linkedRecordIds':[..]}
    try:
        _, probe = _iter_records(tok, p['base_id'], p['sheet_id'])
        pv = (probe[0].get('fields') or {}).get(fm['user']) if probe else None
    except NotableError:
        pv = None
    if isinstance(pv, dict) and 'linkedRecordIds' in pv:
        lm = _link_map(tok, p['base_id'])
        r2n = {}
        for rid2, nm in lm.items():
            r2n.setdefault(nm, rid2)
        ids = [r2n[nm] for nm, _ in users if nm in r2n]
        if ids:
            fields[fm['user']] = {'linkedRecordIds': ids}
        else:
            fields[fm['user']] = '、'.join(nm for nm, _ in users if nm)
    else:
        members = [{'unionId': uid} for _, uid in users if uid]
        if members:
            fields[fm['user']] = members
        elif users:
            fields[fm['user']] = '、'.join(nm for nm, _ in users if nm)
    if fm.get('status') and status:
        fields[fm['status']] = str(status)
    if fm.get('priority') and priority:
        fields[fm['priority']] = str(priority)
    if fm.get('due') and due:
        y, m, d = str(due)[:10].split('-')
        fields[fm['due']] = int(_dt.datetime(int(y), int(m), int(d), 12, 0).timestamp() * 1000)
    rid = create_record(tok, p['base_id'], p['sheet_id'], fields)
    return rid, fields


def delete_task(p, record_id):
    """从多维表格删一行任务，并同步清理工作台本地镜像。
    record_id 是钉钉多维表格里的记录 ID（本地任务的「钉钉任务ID」= aitbl:<rid>）。
    返回本地清理的行数；钉钉侧失败抛 NotableError（本地不动）"""
    fm = p.get('field_map') or {}
    if not fm.get('title') or not fm.get('user'):
        raise NotableError('项目字段映射不完整，无法删除')
    tok = _token()
    delete_record(tok, p['base_id'], p['sheet_id'], record_id)
    key = 'aitbl:%s' % record_id
    c = db.connect()
    n = 0
    with db._lock:
        rows = c.execute("SELECT id FROM records WHERE database='task' AND data LIKE ?",
                         ('%' + key + '%',)).fetchall()
        for r in rows:
            c.execute('DELETE FROM records WHERE id=?', (r['id'],))
            n += 1
        c.commit()
    return n


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
    n2p = _name_map()
    admin_pid = _admin_pid()
    stat = {'new': 0, 'upd': 0, 'skip': 0, 'unmatched': 0, 'paused': 0, 'err': []}

    for p in prjs:
        pid, pname = p['project_id'], p['name']
        if is_local(p):
            continue          # 工作台自建项目：本地为主存储，没有钉钉表可同步
        fm = p.get('field_map') or {}
        first = not settings.g('project:imported:%s' % pid)
        try:
            sname, recs = _iter_records(tok, p['base_id'], p['sheet_id'])
        except NotableError as e:
            stat['err'].append('项目 %s：%s' % (pname, e))
            continue
        # ── 原样导入项目：先尝试自动识别任务字段，识别成功则升级为任务项目 ──
        if not fm.get('title') or not fm.get('user'):
            names = set()
            for r in recs:
                names.update((r.get('fields') or {}).keys())
            auto = _auto_field_map(names, recs)
            if auto:
                # 自动升级：写入识别结果，走任务同步路径 → 前端自动获得七视图 + 新增/删除任务
                fm = auto
                db.project_set(pid, field_map=json.dumps(fm, ensure_ascii=False))
                p = db.project_get(pid)
                pname = p['name']
                say('项目 %s：自动识别出任务字段（%s / %s），已生成看板/日历/甘特等视图'
                    % (pname, fm['title'], fm['user']))
            else:
                # 关联记录字段解析表：整库记录 ID → 主键文本（渲染时把 linkedRecordIds 变成可读名称）
                try:
                    lm = _link_map(tok, p['base_id'])
                    db.cfg_set({'raw:linkmap:%s' % pid: json.dumps(lm, ensure_ascii=False)})
                except Exception:
                    pass
                db.raw_replace(pid, recs)
                say('项目 %s（原样导入）：镜像 %d 行' % (pname, len(recs)))
                if first:
                    db.cfg_set({'project:imported:%s' % pid: db.now()})
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
        linkmap = None          # 关联记录解析表，遇到关联人员列时按需构建
        for r in recs:
            f = r.get('fields') or {}
            rid = str(r.get('id') or r.get('recordId') or '')
            # 执行人解析两级降级：① 钉钉成员字段（unionId 直连）② 关联记录/文本姓名（按姓名匹配）
            members = _cell_members(f.get(f_user))
            pairs = [(u2p[uid], nm) for uid, nm in members if u2p.get(uid)]
            if not pairs:
                if linkmap is None:
                    linkmap = _link_map(tok, p['base_id'])
                txt = _member_text(f.get(f_user), linkmap)
                for nm in [x.strip() for x in re.split('[、,，;；/]', txt) if x.strip()]:
                    p2 = n2p.get(nm)
                    if p2:
                        pairs.append((p2, nm))
            if not pairs:
                if members or _member_text(f.get(f_user), linkmap):
                    stat['unmatched'] += 1   # 有人但匹配不到工作台人员
                skip += 1
                continue
            title = ' '.join(_cell_text(f.get(f_title)).split())[:120]
            if not title:
                skip += 1
                continue
            status = _status_norm(_cell_text(f.get(f_status))) if f_status else '待处理'
            pri = _pri_norm(_cell_text(f.get(f_pri))) if f_pri else '中'
            due = _cell_date(f.get(f_due)) if f_due else ''
            owners = [nm for _, nm in pairs]
            props = {
                '任务名称': title, '优先级': pri, '状态': status,
                '截止时间': due, '负责人': '、'.join(owners)[:40],
                '所属项目ID': pid, '所属项目': pname[:40],
                '处理链接': p.get('url') or '', '来源应用': '钉钉多维表格',
                '钉钉任务ID': 'aitbl:%s' % rid,
            }
            for p2, nm in pairs:
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
