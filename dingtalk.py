# -*- coding: utf-8 -*-
"""钉钉开放平台客户端（纯标准库，无第三方依赖）

用到的接口
1. 获取凭证     GET  https://oapi.dingtalk.com/gettoken
2. 用户详情     POST /topapi/v2/user/get            （userid → unionId / 姓名 / 手机 / 邮箱）
3. 免登换身份   POST /topapi/v2/user/getuserinfo     （authCode → userid）
4. 通讯录成员   POST /topapi/v2/user/list            （部门下的成员列表）
5. 待办任务     GET  https://api.dingtalk.com/v1.0/todo/users/{unionId}/tasks
6. 我的审批     POST /topapi/workrecord/getbyuserid  （某人的待办/已办审批列表）
7. 审批详情     POST /topapi/processinstance/get     （含表单字段 → 可提取金额）

所有方法失败都抛 DingError（带 status/code/message），由 sync 层记录到日志，前端可见。
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

OAPI = 'https://oapi.dingtalk.com'
NEWAPI = 'https://api.dingtalk.com'
TIMEOUT = 15


class DingError(Exception):
    def __init__(self, msg, code=None, raw=None):
        super().__init__(msg)
        self.code = code
        self.raw = raw


def _req(url, data=None, headers=None, method=None):
    body = None
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=body, method=method or ('POST' if body else 'GET'))
    req.add_header('Content-Type', 'application/json')
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            txt = r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        txt = e.read().decode('utf-8', 'replace')
        raise DingError('HTTP %s' % e.code, code=e.code, raw=txt[:600])
    except Exception as e:
        raise DingError('网络请求失败：%s' % e)
    try:
        return json.loads(txt)
    except Exception:
        raise DingError('返回内容不是 JSON', raw=txt[:300])


def parse_money(val):
    """从 '¥1,200.00元' 这类文本里取出数字；解析失败返回 None"""
    s = str(val or '')
    m = re.search(r'(-?\d[\d,]*(?:\.\d+)?)', s)
    if not m:
        return None
    try:
        return float(m.group(1).replace(',', ''))
    except Exception:
        return None


AMOUNT_WORDS = ('金额', '价款', '总额', '合计', '费用', '报销金额', '合同额')


def extract_amount(components):
    """从审批表单组件中提取金额：名称命中金额类关键词，取最精确的一个"""
    best = None
    for c in components or []:
        name = str(c.get('name') or '')
        if not any(w in name for w in AMOUNT_WORDS):
            continue
        num = parse_money(c.get('value'))
        if num is None:
            continue
        score = 2 if any(w in name for w in ('合同', '报销', '付款', '采购')) else 1
        if best is None or score > best[0]:
            best = (score, num, name)
    return (best[1] if best else None), (best[2] if best else '')


def flatten_components(comps):
    """把审批表单展平成 [{name,value}]

    钉钉现在大量用「表单套件 / 明细表」，值是嵌套 JSON：
      [{componentType:'TableField', props:{label:'应用配置变更'},
        value:'[{"rowValue":[{"label":"详情","value":"..."}]}]'}]
    直接按 name 取是取不到金额的，必须递归展开。
    """
    out = []

    def walk(items):
        for c in items or []:
            if not isinstance(c, dict):
                continue
            props = c.get('props') or {}
            val = c.get('value')
            if isinstance(val, str) and val.strip()[:1] in ('[', '{'):
                try:
                    parsed = json.loads(val)
                except Exception:
                    parsed = None
                if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                    if 'rowValue' in parsed[0]:           # 明细表行
                        for r in parsed:
                            for cell in (r.get('rowValue') or []):
                                out.append({'name': cell.get('label') or '', 'value': cell.get('value')})
                        continue
                    if 'componentType' in parsed[0] or 'children' in parsed[0]:   # 表单套件
                        walk(parsed)
                        continue
            name = c.get('name') or props.get('label') or props.get('bizAlias') or ''
            kids = c.get('children')
            if not name and isinstance(kids, list) and kids and isinstance(kids[0], dict):
                walk(kids)
                continue
            if name:
                out.append({'name': name, 'value': val})

    walk(comps)
    return out


PERSON_WORDS = ('姓名', '申请人', '发起人', '提交人', '员工姓名', '创建人')


def _person_of(comps, flat=None):
    """从审批详情里取发起人姓名

    优先 InnerContactField（钉钉通讯录控件，ext_value 里带 name），
    其次按「姓名/申请人/发起人」这些字段名兜底。
    """
    for c in comps or []:
        if not isinstance(c, dict) or c.get('component_type') != 'InnerContactField':
            continue
        try:
            arr = json.loads(c.get('ext_value') or '[]')
        except Exception:
            arr = []
        if isinstance(arr, list) and arr and isinstance(arr[0], dict):
            nm = str(arr[0].get('name') or '').strip()
            if nm:
                return nm
        nm = str(c.get('value') or '').strip()
        if nm and nm not in ('null', 'None'):
            return nm
    for c in (flat if flat is not None else flatten_components(comps)):
        name = str(c.get('name') or '')
        val = str(c.get('value') or '').strip()
        if name in PERSON_WORDS and val and val not in ('null', 'None'):
            return val
    return ''


def _forms_person(forms):
    """从「待办任务」的 forms 里取申请人：forms=[{title:姓名, content:张三}]"""
    for f in forms or []:
        if not isinstance(f, dict):
            continue
        if str(f.get('title') or '') in PERSON_WORDS:
            v = str(f.get('content') or '').strip()
            if v and v not in ('null', 'None'):
                return v
    return ''


class Client:
    def __init__(self, app_key, app_secret, corp_id='', user_id=''):
        self.app_key = (app_key or '').strip()
        self.app_secret = (app_secret or '').strip()
        self.corp_id = (corp_id or '').strip()
        self.user_id = (user_id or '').strip()
        self._token = None
        self._token_at = 0
        self._union_id = None
        self._ticket = None
        self._ticket_at = 0
        self.todo_ok = True   # 待办 API 不可用时自动降级，只告警一次

    # ── 基础 ──────────────────────────────────────────────
    def token(self):
        """缓存 access_token（钉钉有效期 7200s，提前 5 分钟刷新）"""
        if self._token and time.time() - self._token_at < 6900:
            return self._token
        if not self.app_key or not self.app_secret:
            raise DingError('请先填写 AppKey 和 AppSecret')
        q = urllib.parse.urlencode({'appkey': self.app_key, 'appsecret': self.app_secret})
        d = _req('%s/gettoken?%s' % (OAPI, q))
        if d.get('errcode'):
            raise DingError('获取凭证失败：%s' % d.get('errmsg'), code=d.get('errcode'), raw=json.dumps(d)[:300])
        self._token = d.get('access_token')
        self._token_at = time.time()
        return self._token

    def _oapi(self, path, payload):
        url = '%s%s?access_token=%s' % (OAPI, path, urllib.parse.quote(self.token()))
        d = _req(url, payload)
        if d.get('errcode'):
            raise DingError('%s 失败：%s' % (path, d.get('errmsg')), code=d.get('errcode'), raw=json.dumps(d)[:400])
        return d.get('result') or d

    # ── JSAPI 鉴权（免登必须） ─────────────────────────────
    def jsapi_ticket(self):
        """jsapi_ticket，有效期 7200s"""
        if self._ticket and time.time() - self._ticket_at < 6900:
            return self._ticket
        url = '%s/get_jsapi_ticket?access_token=%s' % (OAPI, urllib.parse.quote(self.token()))
        d = _req(url)
        if d.get('errcode'):
            raise DingError('获取 jsapi_ticket 失败：%s（检查应用权限「JSAPI 鉴权」）' % d.get('errmsg'),
                            code=d.get('errcode'), raw=json.dumps(d)[:300])
        self._ticket = d.get('ticket')
        self._ticket_at = time.time()
        return self._ticket

    def jsapi_sign(self, url, agent_id=''):
        """生成 dd.config 所需签名（url 必须是当前页面完整地址，不含 #）"""
        import hashlib
        import random
        import string
        ticket = self.jsapi_ticket()
        nonce = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(16))
        ts = str(int(time.time() * 1000))
        raw = 'jsapi_ticket=%s&noncestr=%s&timestamp=%s&url=%s' % (ticket, nonce, ts, url)
        sig = hashlib.sha1(raw.encode('utf-8')).hexdigest()
        return {
            'agentId': agent_id,
            'corpId': self.corp_id,
            'timeStamp': ts,
            'nonceStr': nonce,
            'signature': sig,
            'jsApiList': ['runtime.permission.requestAuthCode', 'biz.util.openLink'],
            'url': url,
        }

    def _newapi(self, path, query=None, method='GET', payload=None):
        url = NEWAPI + path
        if query:
            url += '?' + urllib.parse.urlencode(query)
        return _req(url, payload, headers={'x-acs-dingtalk-access-token': self.token()}, method=method)

    # ── 工作通知（应用消息推送） ─────────────────────────────
    def notify_work(self, agent_id, userids, title, markdown, url=''):
        """发企业内部应用工作通知（asyncsend_v2）。
        需要「企业内部应用 → 消息推送」权限（普通应用默认有）。
        agent_id: 微应用 AgentId；userids: str 或 list（钉钉 userid）
        url 非空时发 action_card（可点击跳转工作台），否则发纯文本。"""
        if isinstance(userids, str):
            userids = [userids]
        userids = [u for u in (userids or []) if u]
        if not agent_id or not userids:
            return None
        if url:
            msg = {'msgtype': 'markdown', 'markdown': {
                'title': title,
                'text': '%s\n\n[%s](%s)' % (markdown, '👉 打开工作台查看', url)}}
        else:
            msg = {'msgtype': 'text', 'text': {'content': '%s\n%s' % (title, markdown)}}
        return self._oapi('/topapi/message/corpconversation/asyncsend_v2', {
            'agent_id': agent_id,
            'userid_list': ','.join(userids),
            'msg': msg})

    # ── OAuth2 扫码登录（电脑浏览器，没有钉钉容器也能用） ─────────
    def oauth_url(self, redirect_uri, state=''):
        """钉钉登录页（自动出二维码）：手机钉钉扫码 → 回调带 code"""
        q = urllib.parse.urlencode({
            'redirect_uri': redirect_uri,
            'response_type': 'code',
            'client_id': self.app_key,
            'scope': 'openid',
            'state': state or '',
            'prompt': 'consent',
        })
        return 'https://login.dingtalk.com/oauth2/auth?' + q

    def oauth_user(self, code):
        """授权码 → 用户令牌 → 用户信息（unionId/nick/手机/邮箱）→ userid

        企业内部应用用 ClientID/ClientSecret（即 AppKey/AppSecret）即可，
        不需要单独的扫码应用；回调域名要在「登录与分享」里配置。
        """
        code = (code or '').strip()
        if not code:
            raise DingError('缺少授权码 code（请从扫码登录页重新进入）')
        try:
            d = _req(NEWAPI + '/v1.0/oauth2/userAccessToken', {
                'clientId': self.app_key, 'clientSecret': self.app_secret,
                'code': code, 'grantType': 'authorization_code'})
        except DingError as e:
            raise DingError('换取用户令牌失败：%s（检查 AppKey/AppSecret 是否正确、回调域名是否已在「登录与分享」中配置）' % e,
                            code=e.code, raw=e.raw)
        at = d.get('accessToken')
        if not at:
            raise DingError('换取用户令牌失败：%s' % (d.get('message') or d.get('code') or '未知原因'),
                            raw=json.dumps(d, ensure_ascii=False)[:300])
        try:
            me = _req(NEWAPI + '/v1.0/contact/users/me',
                      headers={'x-acs-dingtalk-access-token': at})
        except DingError as e:
            raise DingError('读取钉钉用户信息失败：%s' % e, code=e.code, raw=e.raw)
        unionid = me.get('unionId') or ''
        userid = ''
        if unionid:
            try:
                r = self._oapi('/topapi/user/getbyunionid', {'unionid': unionid})
                userid = (r or {}).get('userid') or ''
            except DingError:
                userid = ''
        return {'userid': userid, 'unionid': unionid,
                'name': me.get('nick') or '',
                'mobile': me.get('mobile') or '',
                'email': me.get('email') or '',
                'openid': me.get('openId') or ''}

    def userid_by_unionid(self, unionid):
        """unionId → userid（查不到返回 ''）。用于钉盘 modifierId 反查姓名。"""
        try:
            r = self._oapi('/topapi/user/getbyunionid', {'unionid': unionid})
            return (r or {}).get('userid') or ''
        except DingError:
            return ''

    # ── 用户 ──────────────────────────────────────────────
    def user_by_id(self, userid):
        r = self._oapi('/topapi/v2/user/get', {'userid': userid, 'language': 'zh_CN'})
        return {
            'userid': r.get('userid') or userid,
            'name': r.get('name') or '',
            'unionid': r.get('unionid') or '',
            'mobile': r.get('mobile') or '',
            'email': r.get('email') or '',
            'job_number': r.get('job_number') or '',
            'title': r.get('title') or '',
            'dept': ','.join(str(x) for x in (r.get('dept_id_list') or [])),
        }

    def user_info(self, user_id=None):
        uid = (user_id or self.user_id or '').strip()
        if not uid:
            raise DingError('请填写要同步的 UserID（钉钉管理后台 → 通讯录 → 成员 → userid）')
        r = self.user_by_id(uid)
        self._union_id = r.get('unionid') or self._union_id
        return r

    def userinfo_by_code(self, code):
        """免登：authCode → userid（必要时补查姓名）"""
        r = self._oapi('/topapi/v2/user/getuserinfo', {'code': code})
        uid = r.get('userid') or ''
        name = r.get('name') or ''
        if uid and not name:
            try:
                name = self.user_by_id(uid).get('name') or ''
            except DingError:
                name = ''
        return {'userid': uid, 'name': name}

    def union_id(self):
        if not self._union_id:
            self.user_info()
        return self._union_id

    def apply_url(self, scope):
        """钉钉开放平台的权限申请直达链接"""
        return ('https://open-dev.dingtalk.com/appscope/apply?content=%s%%23%s'
                % (urllib.parse.quote(self.app_key), scope))

    # ── 知识库（wiki）── 需要 Document.Workspace.Read + Document.Node.Read ──
    def wiki_ok(self):
        return getattr(self, '_wiki_ok', True)

    def wiki_spaces(self, unionid):
        """用户有权限的知识库列表：GET /v1.0/doc/workspaces?operatorId=
        无权限 403 → 置 _wiki_ok=False，由调用方提示申请权限"""
        if not unionid:
            return []
        try:
            d = self._newapi('/v1.0/doc/workspaces?operatorId=%s&maxResults=100'
                             % urllib.parse.quote(unionid), method='GET')
        except DingError as e:
            if '403' in str(e) or 'permission' in str(e).lower():
                self._wiki_ok = False
            raise
        out = []
        for w in (d or {}).get('workspaces') or (d or {}).get('result') or []:
            wid = str(w.get('workspaceId') or w.get('id') or '')
            if not wid or w.get('deleted'):
                continue
            out.append({'id': wid, 'name': str(w.get('name') or '未命名知识库'),
                        'url': str(w.get('url') or '')})
        return out

    def wiki_nodes(self, wid, unionid, parent_id=None, max_results=50, next_token=None):
        """知识库节点列表（一层）。
        先走老接口 /v1.0/doc/workspaces/{wid}/dentries?dentryId=，
        404 再试 /v1.0/doc/workspaces/{wid}/nodes（新版批量获取节点）。"""
        base = '/v1.0/doc/workspaces/%s' % urllib.parse.quote(wid)
        for path, key in ((base + '/dentries', 'children'), (base + '/nodes', 'nodes')):
            q = {'operatorId': unionid, 'maxResults': str(max_results)}
            if parent_id:
                q['dentryId' if key == 'children' else 'parentNodeId'] = parent_id
            if next_token:
                q['nextToken'] = next_token
            try:
                d = self._newapi(path + '?' + urllib.parse.urlencode(q), method='GET')
            except DingError as e:
                msg = str(e)
                if '403' in msg or 'permission' in msg.lower():
                    self._wiki_ok = False
                    raise
                if '404' in msg and key == 'children':
                    continue        # 换新版端点再试
                raise
            items = (d or {}).get(key) or (d or {}).get('nodes') or []
            token = (d or {}).get('nextToken') or ''
            return items, token
        return [], ''

    def perm_check(self, userid):
        """检查待办/审批读取权限，返回自检步骤"""
        steps = []
        now = int(time.time() * 1000)
        try:
            self._oapi('/topapi/workrecord/getbyuserid', {
                'userid': userid, 'status': 0, 'offset': 0, 'count': 1,
                'start_time': now - 30 * 86400000, 'end_time': now})
            steps.append({'name': 'OA 审批读取权限', 'ok': True,
                          'detail': '已开通（可拉待办/已办审批）', 'fix': ''})
        except DingError as e:
            msg = str(e)
            steps.append({'name': 'OA 审批读取权限', 'ok': False, 'detail': msg[:200],
                          'fix': '用管理员账号打开此链接申请并开通，然后重新发布应用：'
                                 + self.apply_url('qyapi_work_record')})
        un = ''
        try:
            un = self.user_by_id(userid).get('unionid') or ''
        except DingError:
            pass
        if un:
            try:
                self._newapi('/v1.0/todo/users/%s/tasks' % urllib.parse.quote(un),
                             {'maxResults': 1})
                self.todo_ok = True
                steps.append({'name': '钉钉待办读取权限', 'ok': True,
                              'detail': '已开通', 'fix': ''})
            except DingError as e:
                self.todo_ok = False
                steps.append({'name': '钉钉待办读取权限（可选）', 'ok': False,
                              'detail': str(e)[:200],
                              'fix': '如公司用「钉钉待办」再开通；只用审批待办可忽略此步。申请：'
                                     + self.apply_url('Todo.Todo.Read')})
        return steps

    def unionid_of(self, userid):
        return self.user_by_id(userid).get('unionid') or ''

    # ── 通讯录 ────────────────────────────────────────────
    def dept_users(self, dept_id=1, max_pages=50):
        """部门成员列表（根部门通常是 1）"""
        out, cursor, pages = [], 0, 0
        while pages < max_pages:
            r = self._oapi('/topapi/v2/user/list', {'dept_id': int(dept_id), 'cursor': cursor, 'size': 100})
            items = r.get('list') or []
            for u in items:
                out.append({
                    'userid': u.get('userid') or '',
                    'name': u.get('name') or '',
                    'mobile': u.get('mobile') or '',
                    'email': u.get('email') or '',
                    'job_number': u.get('job_number') or '',
                    'unionid': u.get('unionid') or '',
                    'dept': ','.join(str(x) for x in (u.get('dept_id_list') or [])),
                })
            pages += 1
            if not r.get('has_more'):
                break
            cursor = r.get('next_cursor') or 0
        return out

    def all_dept_ids(self, root=1, max_dept=500):
        """BFS 遍历全部部门 id（含根部门）"""
        seen, queue, out = {root}, [root], [root]
        while queue and len(out) < max_dept:
            d = queue.pop(0)
            try:
                r = self._oapi('/topapi/v2/department/listsubid', {'dept_id': d})
            except Exception:
                continue
            for cid in (r.get('dept_id_list') or []):
                if cid in seen:
                    continue
                seen.add(cid)
                out.append(cid)
                queue.append(cid)
        return out

    def all_users(self, root=1):
        """递归遍历所有部门，去重后返回全员（根部门直属只有少数人，必须遍历子部门）"""
        seen, out = set(), []
        for d in self.all_dept_ids(root):
            try:
                users = self.dept_users(d)
            except Exception:
                continue
            for u in users:
                uid = u.get('userid')
                if not uid or uid in seen:
                    continue
                seen.add(uid)
                out.append(u)
        return out

    # ── 待办 ──────────────────────────────────────────────
    def todos_by_union(self, unionid, done=None, max_pages=10):
        """done: True 已完成 / False 未完成 / None 全部

        只用「企业待办」查询接口：POST /v1.0/todo/users/{unionId}/org/tasks/query
        （旧的 GET /tasks 在多数租户返回 InvalidAction.NotFound，已废弃）
        注意：该接口只能查到「通过钉钉待办 OpenAPI 创建且带 detailUrl」的待办，
        员工自己在钉钉里手建的待办查不到 —— 那是客户端私有数据，开放平台不提供。
        """
        if not unionid or not self.todo_ok:
            return []
        out, token, pages = [], None, 0
        while pages < max_pages:
            body = {}
            if done is not None:
                body['isDone'] = bool(done)
            if token:
                body['nextToken'] = token
            try:
                d = self._newapi('/v1.0/todo/users/%s/org/tasks/query'
                                 % urllib.parse.quote(unionid), method='POST', payload=body)
            except DingError as e:
                msg = str(e)
                # 应用未开通「待办」产品时该接口不可用 → 降级跳过，不影响审批同步
                if '404' in msg or 'NotFound' in msg or 'InvalidAction' in msg:
                    self.todo_ok = False
                    return out
                raise
            cards = d.get('todoCards') or d.get('todos') or []
            for c in cards:
                out.append({
                    'subject': c.get('subject') or c.get('title') or '',
                    'createdTime': c.get('createdTime') or c.get('createTime') or '',
                    'dueTime': c.get('dueTime') or '',
                    'sourceId': c.get('sourceId') or c.get('id') or '',
                    'detailUrl': c.get('detailUrl') or '',
                })
            token = d.get('nextToken')
            pages += 1
            if not token or not cards:
                break
        return out

    def todos(self, done=None, max_pages=10):
        return self.todos_by_union(self.union_id(), done, max_pages)

    # ── 个人待办（可写、可回读单条；没有官方「列表查询」接口） ─────
    def todo_create(self, unionid, subject, description='', due_ms=None,
                    app_url='', pc_url=''):
        """在工作台建的任务 → 推到钉钉待办（手机端能看到、能勾完成）

        注意：个人待办只有「创建 / 单条查询 / 更新 / 删除」，没有列表查询，
        所以钉钉里手工建的待办永远读不回来；这里只做工作台 → 钉钉的单向下发。
        """
        body = {
            'subject': str(subject or '')[:120],
            'description': str(description or ''),
            'executorIds': [unionid],
            'detailUrl': {'appUrl': app_url or '', 'pcUrl': pc_url or ''},
        }
        if due_ms:
            body['dueTime'] = int(due_ms)
        d = self._newapi('/v1.0/todo/users/%s/tasks' % urllib.parse.quote(unionid),
                         method='POST', payload=body)
        return (d or {}).get('id') or ((d or {}).get('result') or {}).get('id') or ''

    def todo_get(self, unionid, task_id):
        """回读单条待办：拿 done 状态，用于把钉钉勾完成的结果同步回工作台"""
        return self._newapi('/v1.0/todo/users/%s/tasks/%s'
                            % (urllib.parse.quote(unionid), urllib.parse.quote(str(task_id))))

    # ── 审批 ──────────────────────────────────────────────
    def workrecord(self, userid, status=0, max_pages=10, days=365):
        """status: 0 待办 / 1 已办；返回 [{processInstanceId,title,createTime,status,url}]"""
        out, offset, pages = [], 0, 0
        start = int(time.time() * 1000) - days * 86400 * 1000
        while pages < max_pages:
            r = self._oapi('/topapi/workrecord/getbyuserid', {
                'userid': userid, 'offset': offset, 'count': 20, 'status': status,
                'start_time': start, 'end_time': int(time.time() * 1000),
            })
            items = r.get('list') or []
            out.extend(items)
            pages += 1
            offset += len(items)
            if not items or offset >= int(r.get('total_num') or 0):
                break
        return out

    def approval_ids(self, days=365, size=20, max_pages=15):
        """我发起的审批单 id（兼容老逻辑）"""
        uid = self.user_id
        if not uid:
            raise DingError('请填写 UserID，用于查询审批')
        end = int(time.time() * 1000)
        start = end - days * 86400 * 1000
        out, seen, cursor = [], set(), 0
        for _ in range(max_pages):
            r = self._oapi('/topapi/processinstance/listids', {
                'userId': uid, 'startTime': start, 'endTime': end,
                'size': size, 'cursor': cursor,
            })
            ids = r.get('list') or []
            for pid in ids:
                if pid and pid not in seen:
                    seen.add(pid)
                    out.append(pid)
            nxt = r.get('next_cursor')
            if not ids or not nxt or nxt == cursor:
                break
            cursor = nxt
        return out

    def approval_detail(self, pid):
        r = self._oapi('/topapi/processinstance/get', {'process_instance_id': pid})
        p = r.get('process_instance') or {}
        comps = p.get('form_component_values') or []
        flat = flatten_components(comps)
        amount, amount_field = extract_amount(flat)
        # 审批流转：谁审的、同意还是拒绝、几点处理、批语。用于让 AI 学「你过去怎么批的」
        ops = []
        act_map = {'AGREE': '同意', 'REFUSE': '拒绝', 'REDIRECT': '转交'}
        for t in (p.get('tasks') or []):
            if str(t.get('task_status') or '') != 'COMPLETED':
                continue
            res = str(t.get('task_result') or '')
            ops.append({'userid': str(t.get('userid') or ''),
                        'action': act_map.get(res, res or '同意'),
                        'time': str(t.get('finish_time') or t.get('create_time') or ''),
                        'remark': str(t.get('remark') or '').strip()})
        return {
            'id': pid,
            'title': p.get('title') or '',
            'status': p.get('status') or '',
            'result': p.get('result') or '',
            'create': p.get('create_time') or '',
            'finish': p.get('finish_time') or '',
            'originator': p.get('originator_userid') or '',
            'originator_name': _person_of(comps, flat),
            'type': p.get('business_id') or p.get('name') or '',
            'amount': amount,
            'amount_field': amount_field,
            'components': comps,
            'flat': flat,
            'ops': ops,
            # 抄送人 userid 列表：应用权限即可读，是「抄送我」唯一的可靠来源
            'cc': [str(x) for x in (p.get('cc_userids') or []) if x],
        }

    # ── 按部门枚举审批实例（用于「抄送我」全公司覆盖）──────────────
    def dept_ids(self):
        """根部门下的一级部门 ID

        实测 dept_id_list 是递归的：只传一级部门就能捞到所有子部门成员的实例
        （一级 18 个 vs 全树 57 个，返回结果完全一致），所以只取一层。
        """
        try:
            r = self._oapi('/topapi/v2/department/listsub', {'dept_id': 1})
        except DingError:
            return []
        items = r if isinstance(r, list) else ((r or {}).get('result') or [])
        return [d.get('dept_id') for d in items if d.get('dept_id')]

    def instances_by_dept(self, process_code, dept_ids, days=120, max_pages=80):
        """按部门查某模板的实例 ID（dept_id_list 递归含子部门，单次最多 10 个部门）

        「抄送我」拿不到「谁被抄送了」的索引，只能先把模板下所有实例捞出来，
        再逐条读详情里的 cc_userids 反推。部门过滤比逐个 userid 快一个量级：
        202 人需要 21 次请求（userIds 单次上限 10 人），按部门只要 2 次。
        """
        depts_all = [str(d) for d in (dept_ids or []) if d]
        if not depts_all:
            return []
        days = max(1, min(int(days or 120), 120))
        end = int(time.time() * 1000)
        start = end - days * 86400 * 1000
        out, seen = [], set()
        for i in range(0, len(depts_all), 10):
            chunk = depts_all[i:i + 10]
            cursor = 0
            for _ in range(max(1, max_pages)):
                r = self._oapi('/topapi/processinstance/listids', {
                    'process_code': process_code,
                    'start_time': start, 'end_time': end,
                    'size': 20, 'cursor': cursor,
                    'dept_id_list': ','.join(chunk),
                }) or {}
                ids = r.get('list') or []
                for pid in ids:
                    if pid and pid not in seen:
                        seen.add(pid)
                        out.append(pid)
                nxt = r.get('next_cursor')
                if not ids or not nxt or nxt == cursor:
                    break
                cursor = nxt
        return out

    # ── 审批模板全量（自动发现，别再靠手工维护 PROC-xxx）──────────────
    def process_templates(self, userid):
        """/topapi/process/listbyuserid —— 该用户可见的全部审批模板

        返回 [(name, processCode)]。这是「其它系统推送到钉钉的审批」的入口：
        第三方系统（宜搭、氚云、自建 CRM/ERP）只要通过钉钉审批 API 建单，
        在这里就有一个模板，也就能按模板把实例捞回来。
        分页用 next_cursor，实测 53 个模板分 4 页返回。
        """
        out, seen, cursor, pages = [], set(), 0, 0
        while pages < 20:
            r = self._oapi('/topapi/process/listbyuserid', {
                'userid': userid, 'offset': cursor, 'size': 100}) or {}
            items = r.get('process_list') or []
            for v in items:
                code = str(v.get('process_code') or '')
                if code and code not in seen:
                    seen.add(code)
                    out.append((str(v.get('name') or ''), code))
            pages += 1
            nxt = r.get('next_cursor')
            if not items or not nxt or nxt == cursor:
                break
            cursor = nxt
        return out

    # ── 审批待办 / 已办（不依赖 qyapi_work_record 的接口） ────────────
    def workrecord_tasks(self, userid, status=0, max_pages=8, count=20):
        """/topapi/process/workrecord/task/query —— 审批任务列表

        status=0「待我审批」、status=1「我已处理（已办）」。
        实测这个接口走的是另一套权限（审批任务），在应用没有
        qyapi_work_record 时依然能返回真实数据，历史已办靠它补齐。
        唯一拿不到的只有「我发起」（那条路要 process_code 或 workrecord）。
        返回：[{processInstanceId,title,url,taskId,forms,originator?}]
        """
        out, offset, pages, last_sig = [], 0, 0, None
        while pages < max_pages:
            r = self._oapi('/topapi/process/workrecord/task/query', {
                'userid': userid, 'offset': offset, 'count': count, 'status': status})
            items = (r or {}).get('list') or []
            if not items:
                break
            sig = tuple(str(x.get('task_id') or '') for x in items)
            # offset 偶尔不生效，接口会把同一页反复吐回来 —— 认出重复页立刻收手
            if sig == last_sig:
                break
            last_sig = sig
            for it in items:
                iid = str(it.get('instance_id') or '')
                if not iid or any(x['processInstanceId'] == iid for x in out):
                    continue
                out.append({
                    'processInstanceId': iid,
                    'title': it.get('title') or '',
                    'url': it.get('url') or '',
                    'taskId': str(it.get('task_id') or ''),
                    'forms': it.get('forms') or [],
                    'originatorName': _forms_person(it.get('forms') or []),
                })
            pages += 1
            offset += len(items)
            if not (r or {}).get('has_more'):
                break
        return out

    # ── 我发起 / 抄送我（OA 高级版专享接口）────────────────────────
    PREMIUM_PATHS = {
        'launched': '/v1.0/workflow/premium/processCentres/submittedInstances',
        'noticed': '/v1.0/workflow/premium/processCentres/noticedInstances',
    }

    def instance_ids(self, process_code, user_ids, days=120, max_pages=10):
        """按「审批模板 + 发起人」查实例 ID（v1.0 接口，不需要 OA 高级版）

        实测：userIds 参数就是「发起人」过滤，未开通 Premium.Workflow.ReadWrite.All
        也能按人取到「我发起」的实例 ID。时间窗上限 120 天（再大会报时间戳无效）。
        """
        users = [str(u) for u in (user_ids or []) if u]
        if not users:
            return []
        days = max(1, min(int(days or 120), 120))
        end = int(time.time() * 1000)
        start = end - days * 86400 * 1000
        out, seen, nxt = [], set(), '0'
        for _ in range(max(1, max_pages)):
            r = self._newapi('/v1.0/workflow/processes/instanceIds/query', method='POST',
                             payload={'processCode': process_code,
                                      'startTime': start, 'endTime': end,
                                      'userIds': users, 'maxResults': 20,
                                      'nextToken': str(nxt)})
            res = ((r or {}).get('result') or {})
            ids = res.get('list') or []
            for pid in ids:
                if pid and pid not in seen:
                    seen.add(pid)
                    out.append(pid)
            nxt = res.get('nextToken')
            if not ids or nxt in (None, '', 0):
                break
        return out

    def premium_instances(self, userid, kind='launched', pages=5, page_size=20):
        """我发起（launched）/ 抄送我（noticed）

        钉钉把这两个分类放在「审批中心」接口里，官方标注为 OA 高级版专享，
        权限点 Premium.Workflow.ReadWrite.All；未开通时直接 403
        Forbidden.AccessDenied，调用方据此降级并提示。

        返回结构对齐 workrecord_tasks：
        [{processInstanceId,title,url,createTime,statusText,originatorName,...}]
        """
        path = self.PREMIUM_PATHS.get(kind)
        if not path:
            raise DingError('未知审批分类：%s' % kind)
        out, seen = [], set()
        for page in range(1, max(1, pages) + 1):
            r = self._newapi(path, query={'userId': userid, 'pageSize': page_size,
                                          'pageNumber': page})
            items = ((r or {}).get('result') or {}).get('list') or []
            for it in items:
                pid = str(it.get('processInstanceId') or '')
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                st = str(it.get('status') or '').upper()
                res = str(it.get('result') or '').lower()
                if st == 'RUNNING':
                    stxt = '审批中'
                elif st == 'TERMINATED':
                    stxt = '已撤销'
                elif st == 'COMPLETED':
                    stxt = '已拒绝' if res == 'refuse' else '已通过'
                else:
                    stxt = ''
                out.append({
                    'processInstanceId': pid,
                    'title': it.get('title') or '',
                    'url': it.get('url') or '',
                    'createTime': it.get('processCreateTime') or '',
                    'statusText': stxt,
                    'originatorName': it.get('originatorName') or '',
                    'originatorId': str(it.get('originatorId') or ''),
                    'summary': it.get('formMassage') or '',
                })
            if len(items) < page_size:
                break
        return out

    def premium_ready(self, userid, kind='launched'):
        """探测高级版接口是否可用：True 可用 / False 403 无权限"""
        try:
            self.premium_instances(userid, kind, pages=1, page_size=1)
            return True
        except DingError as e:
            if getattr(e, 'code', None) == 403 or 'Premium' in str(e) or 'Premium' in (e.raw or ''):
                return False
            raise

    # ── 日程 / 会议 ──────────────────────────────────────────────
    def events(self, unionid, start_ms, end_ms, max_pages=20):
        """/v1.0/calendar/users/{unionId}/calendars/primary/events

        个人日历主表事件，实测不需要额外权限。会议在钉钉里就是日程：
        分类 displayName 含「会议」或带 meetingRooms 的事件即是会议。
        """
        out, token, pages = [], None, 0
        while pages < max_pages:
            q = {'startTime': start_ms, 'endTime': end_ms, 'maxResults': 50}
            if token:
                q['nextToken'] = token
            d = self._newapi('/v1.0/calendar/users/%s/calendars/primary/events'
                             % urllib.parse.quote(unionid), q)
            out.extend(d.get('events') or [])
            token = d.get('nextToken')
            pages += 1
            if not token or not d.get('events'):
                break
        return out

    def event_create(self, unionid, summary, start_dt, end_dt, description=''):
        """在用户 primary 日历创建事件（写入即出现在钉钉日历/日程里）。
        start_dt/end_dt: 'YYYY-MM-DDTHH:MM:SS'（北京时间）；返回事件 id。
        事件 id 会写回工作台日程表的 事件ID 字段，避免每日同步时重复。"""
        body = {'summary': str(summary)[:100],
                'start': {'dateTime': '%s+08:00' % start_dt, 'timeZone': 'Asia/Shanghai'},
                'end': {'dateTime': '%s+08:00' % end_dt, 'timeZone': 'Asia/Shanghai'}}
        if description:
            body['description'] = str(description)[:800]
        d = self._newapi('/v1.0/calendar/users/%s/calendars/primary/events'
                         % urllib.parse.quote(unionid), method='POST', payload=body)
        return d.get('id') or ''

    # ── 钉钉文档 / 钉盘 ──────────────────────────────────────────
    def drive_spaces(self, unionid, space_type='org', max_pages=10):
        """列出钉盘空间。space_type: org 企业空间 / personal 个人空间。
        需要 Drive.Space.Read 权限。"""
        out, token, pages = [], None, 0
        while pages < max_pages:
            q = {'unionId': unionid, 'spaceType': space_type, 'maxResults': 50}
            if token:
                q['nextToken'] = token
            d = self._newapi('/v1.0/drive/spaces', q)
            for sp in d.get('spaces') or []:
                # 统一字段：spaceId/spaceName → id/name，个人空间无权限时跳过
                sid = sp.get('spaceId') or sp.get('id')
                if not sid:
                    continue
                out.append({'id': sid,
                            'name': sp.get('spaceName') or sp.get('name') or '钉盘'})
            token = d.get('nextToken')
            pages += 1
            if not token or not d.get('spaces'):
                break
        return out

    # ── 钉钉文档正文（块元素读写，需 Storage.File.Read/Write）──
    # doc_uuid = dentry 的 uuid（32 位），见 drive_files 返回的 uuid 字段；
    # operator_id = 当前用户 unionId，钉钉按这个人的文档权限校验。
    def doc_blocks(self, doc_uuid, operator_id):
        d = self._newapi('/v1.0/doc/suites/documents/%s/blocks'
                         % urllib.parse.quote(doc_uuid),
                         {'operatorId': operator_id})
        return ((d.get('result') or {}).get('data')) or []

    def doc_block_update(self, doc_uuid, block_id, element, operator_id):
        return self._newapi('/v1.0/doc/suites/documents/%s/blocks/%s'
                            % (urllib.parse.quote(doc_uuid), urllib.parse.quote(block_id)),
                            {'operatorId': operator_id}, method='PUT',
                            payload={'element': element})

    def doc_block_insert(self, doc_uuid, element, operator_id, index=None):
        body = {'element': element}
        if index is not None:
            body['index'] = int(index)
        return self._newapi('/v1.0/doc/suites/documents/%s/blocks'
                            % urllib.parse.quote(doc_uuid),
                            {'operatorId': operator_id}, method='POST', payload=body)

    def doc_block_delete(self, doc_uuid, block_id, operator_id):
        return self._newapi('/v1.0/doc/suites/documents/%s/blocks/%s'
                            % (urllib.parse.quote(doc_uuid), urllib.parse.quote(block_id)),
                            {'operatorId': operator_id}, method='DELETE')

    def doc_url(self, doc_uuid):
        """文档在钉钉内的访问地址（点「在钉钉打开」用）"""
        return 'https://alidocs.dingtalk.com/i/nodes/' + (doc_uuid or '')

    def drive_files(self, space_id, unionid, max_pages=100):
        """平铺列出空间内全部文件/文件夹（含在线文档）。
        需要 Storage.File.Read 权限。

        注意：unionId 必须放 query，翻页参数必须放 body 的 option 里，
        放错位置时 nextToken 不生效、每页返回同样的第一条数据。
        """
        out, token, pages, seen = [], None, 0, set()
        while pages < max_pages:
            q = {'unionId': unionid}
            option = {'maxResults': 50}
            if token:
                option['nextToken'] = token
            d = self._newapi('/v1.0/storage/spaces/%s/dentries/listAll'
                             % urllib.parse.quote(space_id), q, method='POST',
                             payload={'option': option})
            items = d.get('dentries') or []
            for it in items:
                key = str(it.get('id') or '')
                if key and key not in seen:
                    seen.add(key)
                    out.append(it)
            token = d.get('nextToken')
            pages += 1
            if not token or not items:
                break
        return out
