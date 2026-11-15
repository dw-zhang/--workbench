# -*- coding: utf-8 -*-
"""补齐审批表单正文与流转记录（可重复执行，只补缺失的）

AI 判断审批需要「事由 / 金额 / 附件 / 谁批过」，原来的同步只存了标题+状态。
这个脚本把历史单的正文补回来；日常新单由 sync.py 自动写入。

用法：
  python backfill_form.py            # 补最近 90 天，上限 300 条
  python backfill_form.py 30 500     # 补最近 30 天，上限 500 条
"""
import json
import sys
import time

import db
import sync
from approval_agent import _recent_days
from dingtalk import Client, DingError


def main(days=90, limit=300):
    cfg = db.cfg_all()
    ak = cfg.get('dingtalk:app_key') or cfg.get('app_key')
    sk = cfg.get('dingtalk:app_secret') or cfg.get('app_secret')
    if not ak or not sk:
        print('未配置钉钉 AppKey/AppSecret')
        return 1
    cli = Client(ak, sk)
    tbl = db.TABLES['ding']
    c = db.connect()
    with db._lock:
        rows = c.execute('SELECT id, data FROM records WHERE database=? ORDER BY seq DESC LIMIT 4000',
                         (tbl,)).fetchall()

    todo = []
    for r in rows:
        try:
            d = json.loads(r['data'])
        except Exception:
            continue
        if d.get('表单明细'):
            continue
        if not d.get('审批编号'):
            continue
        if not _recent_days(d.get('发起时间'), days):
            continue
        todo.append((r['id'], d))
        if len(todo) >= limit:
            break

    print('待补 %d 条（最近 %d 天）' % (len(todo), days))
    ok = skip = err = 0
    for i, (rid, d) in enumerate(todo, 1):
        pid = d.get('审批编号')
        try:
            det = cli.approval_detail(pid)
        except DingError as e:
            err += 1
            print('  [%d/%d] %s 失败：%s' % (i, len(todo), (d.get('审批标题') or '')[:12], e))
            time.sleep(0.2)
            continue
        props = {}
        txt = sync._flat_text(det.get('flat'))
        if txt:
            props['表单明细'] = txt
        flow = sync._flow_text(det.get('ops'))
        if flow:
            props['审批流转'] = flow
        if det.get('amount') is not None and not d.get('金额'):
            props['金额'] = det['amount']
            props['涉及金额'] = '是'
        if props:
            db.update_record(tbl, rid, props)
            ok += 1
        else:
            skip += 1
        if i % 25 == 0:
            print('  进度 %d/%d（成功 %d 失败 %d）' % (i, len(todo), ok, err))
        time.sleep(0.08)
    print('完成：补全 %d，无内容 %d，失败 %d' % (ok, skip, err))
    return 0


if __name__ == '__main__':
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    sys.exit(main(days, limit))
