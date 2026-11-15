# -*- coding: utf-8 -*-
"""人员匹配：手机 / 邮箱任一命中即视为同一人（并查集合并）

规则
- 手机归一化：去非数字、去 +86/0086 前缀，保留 ≥7 位
- 邮箱归一化：trim + 小写，必须含 @
- 空值不参与匹配（空对空不算命中）
- 同一连通块出现两个同平台账号 → 标记 conflict，等管理员裁决
"""
import db


def norm_phone(v):
    s = ''.join(ch for ch in str(v or '') if ch.isdigit())
    if s.startswith('0086'):
        s = s[4:]
    elif s.startswith('86') and len(s) == 13:
        s = s[2:]
    return s if len(s) >= 7 else ''


def norm_email(v):
    s = str(v or '').strip().lower()
    return s if ('@' in s and len(s) >= 5) else ''


def _load():
    persons = db.persons_all()
    accounts = db.accounts_all()
    return persons, accounts


def ingest_contact(platform, account_id, name='', phone='', email='', extra=None):
    """把一个平台账号落到某个人身上（先按账号，再按手机/邮箱，最后新建）"""
    account_id = str(account_id or '').strip()
    if not account_id:
        return None
    acc = db.account_find(platform, account_id)
    if acc:
        pid = acc['person_id']
    else:
        phone_n, email_n = norm_phone(phone), norm_email(email)
        hit = db.person_find_by_contact(phone_n, email_n)
        if hit:
            pid = hit['person_id']
        else:
            pid = db.person_new(name=name or account_id, status='bound',
                                match_by='auto', phones=[phone_n] if phone_n else [],
                                emails=[email_n] if email_n else [], source=platform)
    p = db.person_get(pid)
    if not p:
        return None
    db.account_add(pid, platform, account_id, name or p.get('name') or '', extra or {})
    # 补充联系信息
    phones = set(p.get('phones') or [])
    emails = set(p.get('emails') or [])
    if norm_phone(phone):
        phones.add(norm_phone(phone))
    if norm_email(email):
        emails.add(norm_email(email))
    if not p.get('name') and name:
        db.person_set(pid, name=name)
    if set(p.get('phones') or []) != phones or set(p.get('emails') or []) != emails:
        db.person_set(pid, phones=sorted(phones), emails=sorted(emails))
    return pid


def run():
    """全量重算：把共享手机/邮箱的人合并成一个 person"""
    persons, accounts = _load()
    parent = {p['person_id']: p['person_id'] for p in persons}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    index = {}   # 归一化标识 → 首个 person_id
    for p in persons:
        for ph in p.get('phones') or []:
            k = 'p:' + norm_phone(ph)
            if k == 'p:':
                continue
            if k in index:
                union(index[k], p['person_id'])
            else:
                index[k] = p['person_id']
        for em in p.get('emails') or []:
            k = 'e:' + norm_email(em)
            if k == 'e:':
                continue
            if k in index:
                union(index[k], p['person_id'])
            else:
                index[k] = p['person_id']

    # 账号上的 extra 也可能带手机/邮箱
    by_pid_acc = {}
    for a in accounts:
        by_pid_acc.setdefault(a['person_id'], []).append(a)

    clusters = {}
    for p in persons:
        clusters.setdefault(find(p['person_id']), []).append(p)

    merged, conflicts = 0, 0
    for root, group in clusters.items():
        if len(group) <= 1:
            continue
        # 选主身份：优先有钉钉账号的，其次最早创建
        def rank(p):
            plats = [a['platform'] for a in by_pid_acc.get(p['person_id'], [])]
            return (0 if 'dingtalk' in plats else 1, p.get('created_at') or '')
        group.sort(key=rank)
        keep = group[0]['person_id']
        phones, emails = set(), set()
        for p in group:
            phones |= set(p.get('phones') or [])
            emails |= set(p.get('emails') or [])
            if p['person_id'] == keep:
                continue
            for a in by_pid_acc.get(p['person_id'], []):
                db.account_add(keep, a['platform'], a['account_id'], a.get('name') or '')
            db.assign_person(p['person_id'], keep)
            db.person_del(p['person_id'])
            merged += 1
        db.person_set(keep, phones=sorted(phones), emails=sorted(emails), match_by='phone/email')

    # 冲突检测：一个 person 同一平台多个账号
    for p in db.persons_all():
        accs = db.accounts_of(p['person_id'])
        plats = [a['platform'] for a in accs]
        dup = any(plats.count(x) > 1 for x in set(plats))
        want = 'conflict' if dup else ('bound' if accs else 'unmatched')
        if p.get('status') != want and p.get('status') != 'manual':
            db.person_set(p['person_id'], status=want)
        if dup:
            conflicts += 1

    return {'merged': merged, 'conflicts': conflicts, 'persons': len(db.persons_all())}


def report():
    persons = db.persons_all()
    accs = db.accounts_all()
    per = {}
    for a in accs:
        per.setdefault(a['person_id'], []).append(a['platform'])
    return {
        'total': len(persons),
        'bound': sum(1 for p in persons if p.get('status') == 'bound'),
        'conflict': sum(1 for p in persons if p.get('status') == 'conflict'),
        'unmatched': sum(1 for p in persons if p.get('status') == 'unmatched'),
        'no_account': sum(1 for p in persons if not per.get(p['person_id'])),
        'by_platform': {pl: sum(1 for x in accs if x['platform'] == pl)
                        for pl in ('dingtalk', 'feishu', 'github', 'gitlab', 'gitea',
                                   'zentao', 'kingdee', 'yonyou', 'wecom')},
    }
