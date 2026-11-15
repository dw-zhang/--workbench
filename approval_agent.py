# -*- coding: utf-8 -*-
"""审批 Agent —— 不是问答，是干活。

一次完整的活：
  1 读单   取表单正文（事由/金额/时间/附件）+ 流转记录
  2 比对   近 90 天同类型历史怎么批的 + 制度条款
  3 判断   通过 / 退回 / 需补材料 / 建议人工，附依据与风险
  4 出建议 生成可直接写进审批意见的话
  5 校准   记录人是否采纳，算一致率（db.judge_feedback / judge_stats）

设计红线：
  - 绝不自动提交，所有写操作由人点
  - 没有正文不下结论（need_info）
  - 没有依据不猜（manual）
  - 制度与历史冲突时显式标出，不替人做价值判断
"""
import json
import re
import time
import urllib.request

import db
import ai_bot

DING = db.TABLES['ding']
POLICY_KEY = 'ai:policy'
CACHE_SEC = 3600
HISTORY_LIMIT = 12
BIG_AMOUNT = 5000


def _today():
    return time.strftime('%Y-%m-%d')


def _recent_days(s, days=90):
    """粗略判断时间字符串是否在最近 days 天内"""
    if not s:
        return False
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', str(s))
    if not m:
        return False
    try:
        t = time.mktime((int(m.group(1)), int(m.group(2)), int(m.group(3)),
                         0, 0, 0, 0, 0, -1))
    except Exception:
        return False
    return (time.time() - t) <= days * 86400


def _digits(v):
    s = re.sub(r'[^\d.]', '', str(v or ''))
    try:
        return float(s) if s else None
    except Exception:
        return None


# ── 1 读单 ────────────────────────────────────────────────────────────
def load_case(pid, person_id=''):
    """取一张单的完整信息；person_id 非空则只在该人名下找（数据隔离）"""
    rid, data = db.find_record_rec(DING, '审批编号', pid, person_id or None)
    if data and person_id:
        owner = db.record_person(DING, rid) if rid else ''
        if owner and owner != person_id:
            return None
    if not data:
        return None
    return {
        'pid': pid,
        'rid': rid,
        'title': data.get('审批标题') or '',
        'type': data.get('审批类型') or data.get('审批标题') or '',
        'status': data.get('审批状态') or '',
        'from': data.get('发起人') or '',
        'src': data.get('审批来源') or '',
        'time': data.get('发起时间') or '',
        'amount': data.get('金额') or '',
        'form': data.get('表单明细') or '',
        'flow': data.get('审批流转') or '',
        'link': data.get('处理链接') or '',
    }


# ── 2 比对 ────────────────────────────────────────────────────────────
def similar_history(case, person_id='', days=90, limit=HISTORY_LIMIT):
    """近 days 天同类型、已有结论的单子，作为「你过去怎么批的」样本"""
    typ = case.get('type') or case.get('title') or ''
    if not typ:
        return []
    c = db.connect()
    sql = 'SELECT data FROM records WHERE database=? ORDER BY seq DESC LIMIT 4000'
    args = [DING]
    with db._lock:
        rows = c.execute(sql, args).fetchall()
    out = []
    for r in rows:
        try:
            d = json.loads(r['data'])
        except Exception:
            continue
        if (d.get('审批类型') or d.get('审批标题') or '') != typ:
            continue
        st = d.get('审批状态') or ''
        if st not in ('已通过', '已拒绝'):
            continue
        if person_id and d.get('person_id') not in (person_id, None, ''):
            continue
        if not _recent_days(d.get('发起时间'), days):
            continue
        out.append({
            'time': str(d.get('发起时间') or '')[:10],
            'from': d.get('发起人') or '',
            'status': st,
            'amount': d.get('金额') or '',
            'form': (d.get('表单明细') or '')[:260],
            'flow': (d.get('审批流转') or '')[:120],
        })
        if len(out) >= limit:
            break
    return out


def policy_text():
    try:
        return (db.cfg_get(POLICY_KEY) or '').strip()
    except Exception:
        return ''


# ── 3 判断 ────────────────────────────────────────────────────────────
SYS = (
    '你是企业审批助手，替「%(who)s」判断一张钉钉审批单该怎么处理。\n'
    '你的价值是给出可直接采用的结论，不是罗列信息。\n'
    '规则：\n'
    '1. 只依据用户提供的「单据正文」「历史同类」「制度条款」判断，禁止编造制度条款、编号或数字。\n'
    '2. 正文为空或关键信息缺失（无事由、无金额、无附件支撑）时，结论必须是 need_info，并列出要补什么。\n'
    '3. 既无制度、又无历史样本、且属于管理裁量类（如采购必要性、人员处分）时，结论必须是 manual。\n'
    '4. 制度与实际历史做法不一致时，写进 conflict 字段，不要替人调和。\n'
    '5. 重点核查：事由是否具体到项目/时段、金额是否合理、是否有支撑附件、是否重复提交、是否符合次数或额度上限、是否跨夜或超范围用工。\n'
    '6. 结论只能取 approve / reject / need_info / manual 之一。\n'
    '7. 只输出 JSON，不要任何解释性文字、不要 markdown 代码块。\n'
    '输出格式：\n'
    '{"verdict":"approve|reject|need_info|manual","confidence":"high|mid|low",'
    '"summary":"一句话结论，30字内",'
    '"reasons":["依据，每条不超过40字，最多4条"],'
    '"risks":["风险点，最多3条，没有就空数组"],'
    '"missing":["需补充的材料，最多3条，没有就空数组"],'
    '"conflict":"制度与历史做法的冲突点，没有就空字符串",'
    '"comment":"建议写入审批意见的话，60字内，可直接使用"}'
)


def _llm_json(cfg, sys_text, user_text):
    body = {
        'model': cfg['model'],
        'messages': [{'role': 'system', 'content': sys_text},
                     {'role': 'user', 'content': user_text}],
        'temperature': 0.2,
    }
    req = urllib.request.Request(
        cfg['base'] + '/chat/completions',
        data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + cfg['key']})
    with urllib.request.urlopen(req, timeout=90) as r:
        j = json.load(r)
    msg = (j.get('choices') or [{}])[0].get('message') or {}
    txt = str(msg.get('content') or '').strip()
    m = re.search(r'\{[\s\S]*\}', txt)
    if not m:
        raise ValueError('模型未返回 JSON：%s' % txt[:120])
    return json.loads(m.group(0))


def _normalize(d):
    verdict = str(d.get('verdict') or '').strip().lower()
    if verdict not in ('approve', 'reject', 'need_info', 'manual'):
        verdict = 'manual'
    conf = str(d.get('confidence') or '').strip().lower()
    if conf not in ('high', 'mid', 'low'):
        conf = 'mid'
    out = {'verdict': verdict, 'confidence': conf,
           'summary': str(d.get('summary') or '').strip()[:60],
           'reasons': [str(x).strip()[:80] for x in (d.get('reasons') or []) if str(x).strip()][:4],
           'risks': [str(x).strip()[:80] for x in (d.get('risks') or []) if str(x).strip()][:3],
           'missing': [str(x).strip()[:80] for x in (d.get('missing') or []) if str(x).strip()][:3],
           'conflict': str(d.get('conflict') or '').strip()[:200],
           'comment': str(d.get('comment') or '').strip()[:120]}
    return out


def judge(pid, person_id='', who='', force=False):
    """判断一张单。返回 {'ok':True, 'data':{...}, 'cached':bool} 或 {'ok':False,'error':...}"""
    t0 = time.time()
    case = load_case(pid, person_id)
    if not case:
        return {'ok': False, 'error': '找不到这张审批单（或无权查看）'}

    if not force:
        old = db.judge_get(pid, person_id)
        if old:
            try:
                age = time.time() - time.mktime(time.strptime(old['created_at'], '%Y-%m-%dT%H:%M:%S'))
            except Exception:
                age = 1e9
            if 0 <= age < CACHE_SEC:
                return {'ok': True, 'cached': True, 'data': old['payload']}

    # 护栏一：没有正文，不判
    if not case['form']:
        res = {
            'pid': pid, 'title': case['title'], 'type': case['type'],
            'verdict': 'need_info', 'confidence': 'high',
            'summary': '没有拿到单据正文，无法判断',
            'reasons': ['该单同步时未取到表单明细'],
            'risks': [], 'missing': ['表单正文'],
            'conflict': '',
            'comment': '单据内容未同步，请打开原单查看后处理',
            'source': 'guard', 'at': db.now(),
        }
        jid = db.judge_save(pid, person_id, case['title'], case['type'],
                            'need_info', 'high', res, 'guard', 0)
        res['jid'] = jid
        return {'ok': True, 'cached': False, 'data': res}

    cfg = ai_bot.ai_cfg(person_id)
    if not cfg:
        return {'ok': False, 'error': '未配置 AI 模型，请先在设置中填写接口地址与密钥'}

    hist = similar_history(case, person_id)
    pol = policy_text()

    case_txt = (
        '【单据】\n'
        '标题：%s\n类型：%s\n发起人：%s\n发起时间：%s\n来源：%s\n'
        '金额：%s\n状态：%s\n'
        '正文：\n%s\n'
        '流转：%s\n'
    ) % (case['title'], case['type'], case['from'], case['time'][:19],
         case['src'], case['amount'] or '未填写', case['status'],
         case['form'], case['flow'] or '暂无')

    if hist:
        lines = []
        for i, h in enumerate(hist, 1):
            lines.append('%d) %s %s %s 金额%s ｜ %s ｜ 流转:%s' % (
                i, h['time'], h['from'], h['status'], h['amount'] or '-',
                (h['form'] or '').replace('\n', ' ')[:160], h['flow'] or '-'))
        hist_txt = '【近 90 天同类型历史（共 %d 条）】\n%s' % (len(hist), '\n'.join(lines))
    else:
        hist_txt = '【近 90 天同类型历史】无。没有可比对的先例，判断需保守。'

    pol_txt = ('【制度条款】\n%s' % pol) if pol else \
        '【制度条款】未配置。只能在历史做法范围内判断；涉及金额或管理裁量时倾向 manual。'

    user_txt = case_txt + '\n' + hist_txt + '\n' + pol_txt + '\n\n请判断这张单该怎么处理。'

    try:
        raw = _llm_json(cfg, SYS % {'who': who or '审批人'}, user_txt)
        res = _normalize(raw)
    except Exception as e:
        return {'ok': False, 'error': 'AI 判断失败：%s' % e}

    # 护栏二：金额大 → 必定提示复核，且不允许高置信
    amt = _digits(case['amount'])
    if amt and amt >= BIG_AMOUNT and res['verdict'] == 'approve':
        res['risks'] = ([('涉及金额 %s 元，建议人工复核预算与合同' % int(amt))] + res['risks'])[:3]
        if res['confidence'] == 'high':
            res['confidence'] = 'mid'

    res.update({'pid': pid, 'title': case['title'], 'type': case['type'],
                'amount': case['amount'], 'form': case['form'],
                'hist_n': len(hist), 'has_policy': bool(pol),
                'source': 'llm', 'model': cfg['model'], 'at': db.now()})

    cost = int((time.time() - t0) * 1000)
    res['jid'] = db.judge_save(pid, person_id, case['title'], case['type'],
                              res['verdict'], res['confidence'], res, cfg['model'], cost)
    return {'ok': True, 'cached': False, 'data': res}


def pending(person_id='', limit=8):
    """列出可判断的待审批单（有正文的优先）"""
    c = db.connect()
    with db._lock:
        rows = c.execute('SELECT data FROM records WHERE database=? ORDER BY seq DESC LIMIT 3000',
                         (DING,)).fetchall()
    out = []
    for r in rows:
        try:
            d = json.loads(r['data'])
        except Exception:
            continue
        if (d.get('审批状态') or '') not in ('审批中', '待审批', '待我审批', '进行中'):
            continue
        if person_id and d.get('person_id') not in (person_id, None, ''):
            continue
        out.append({'pid': d.get('审批编号') or '', 'title': d.get('审批标题') or '',
                    'type': d.get('审批类型') or '', 'from': d.get('发起人') or '',
                    'time': str(d.get('发起时间') or '')[:19],
                    'has_form': bool(d.get('表单明细'))})
        if len(out) >= limit:
            break
    return out
