#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补齐审批记录的「发起人」字段

历史记录里「抄送我」的发起人多半是已离职员工，通讯录查不到 → 写成了空。
这里先用库里已有的同名实例记录交叉填（同一审批编号在别人的「我发起 / 已处理」
里通常带名字），剩下查不到再回读一次审批详情。

用法：docker exec -w /app wbdash python3 backfill_origin.py [--limit N]
"""
import sys
import time

import db
import dingtalk
import settings
from sync import _origin_name

T = db.TABLES['ding']


def main():
    limit = 0
    if '--limit' in sys.argv:
        limit = int(sys.argv[sys.argv.index('--limit') + 1])

    name_of, missing = {}, []
    for p in db.persons_all():
        pid = p['person_id']
        try:
            rows = db.query(T, size=100000, person_id=pid)['results']
        except Exception:
            continue
        for r in rows:
            no = str(r.get('审批编号') or '')
            nm = str(r.get('发起人') or '').strip()
            if no and nm and no not in name_of:
                name_of[no] = nm
            if no and not nm:
                missing.append((pid, r))
    print('库里已知发起人 %d 个编号；缺发起人 %d 条' % (len(name_of), len(missing)), flush=True)

    done = 0
    rest = []
    for pid, r in missing:
        nm = name_of.get(str(r.get('审批编号') or ''))
        rid = r.get('record_id') or r.get('_id')
        if not nm or not rid:
            rest.append((pid, r))
            continue
        db.update_record(T, rid, {'发起人': nm}, person_id=pid)
        done += 1
    print('交叉填充 %d 条，需回读详情 %d 条' % (done, len(rest)), flush=True)

    if limit:
        rest = rest[:limit]
    if rest:
        pc = settings.get_platform('dingtalk')
        cli = dingtalk.Client(pc['app_key'], pc['app_secret'], pc.get('corp_id', ''))
        n, fail = 0, 0
        for pid, r in rest:
            no = str(r.get('审批编号') or '')
            rid = r.get('record_id') or r.get('_id')
            try:
                d = cli.approval_detail(no)
            except Exception:
                fail += 1
                continue
            nm = _origin_name(d.get('originator'), d.get('title'))
            if nm and rid:
                db.update_record(T, rid, {'发起人': nm}, person_id=pid)
                n += 1
            if n and n % 200 == 0:
                print('已回读 %d / %d' % (n, len(rest)), flush=True)
            time.sleep(0.02)
        print('详情回填 %d 条，失败 %d' % (n, fail), flush=True)

    left = 0
    for p in db.persons_all():
        try:
            rows = db.query(T, size=100000, person_id=p['person_id'])['results']
        except Exception:
            continue
        left += sum(1 for r in rows if not str(r.get('发起人') or '').strip())
    print('剩余缺发起人 %d 条' % left, flush=True)


if __name__ == '__main__':
    main()
