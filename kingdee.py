# -*- coding: utf-8 -*-
"""金蝶云星空适配器（纯标准库）

流程
1. LoginByAppSecret  → 拿到 kdservice-sessionid（第三方应用授权，无需账密）
2. ExecuteBillQuery  → 按 FormId 查单据（增量用 FApproveDate / FDate 过滤）
3. 用户列表           → SEC_User（用户账号）用于人员绑定

状态码：Z 暂存 / A 创建 / B 审核中 / C 已审核 / D 重新审核
"""
import json
import time
import urllib.error
import urllib.request

AUTH_PATH = '/K3Cloud/Kingdee.BOS.WebApi.ServicesStub.AuthService.LoginByAppSecret.common.kdsvc'
QUERY_PATH = '/K3Cloud/Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.ExecuteBillQuery.common.kdsvc'
TIMEOUT = 20

STATUS_MAP = {
    'A': '待处理', 'B': '进行中', 'C': '已完成', 'D': '进行中', 'Z': '',
}
STATUS_TEXT = {
    'A': '创建', 'B': '审核中', 'C': '已审核', 'D': '重新审核', 'Z': '暂存',
}


class KingdeeError(Exception):
    def __init__(self, msg, code=None, raw=None):
        super().__init__(msg)
        self.code = code
        self.raw = raw


class Client:
    def __init__(self, host, acct_id, app_id='', app_secret='', user='', password=''):
        h = (host or '').strip().rstrip('/')
        if h and not h.startswith('http'):
            h = 'http://' + h
        self.host = h
        self.acct_id = (acct_id or '').strip()
        self.app_id = (app_id or '').strip()
        self.app_secret = (app_secret or '').strip()
        self.user = (user or '').strip()
        self.password = (password or '').strip()
        self.cookie = ''
        self._at = 0

    # ── 登录 ──────────────────────────────────────────────
    def login(self):
        if not self.host or not self.acct_id:
            raise KingdeeError('请先填写金蝶地址与账套ID')
        data = {'acctID': self.acct_id, 'username': self.user, 'lcid': 2052}
        if self.app_id and self.app_secret:
            data.update({'appId': self.app_id, 'appSecret': self.app_secret})
        else:
            data['password'] = self.password
        body = json.dumps({'data': data}, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(self.host + AUTH_PATH, data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                txt = r.read().decode('utf-8', 'replace')
                cookies = r.headers.get_all('Set-Cookie') or []
        except urllib.error.HTTPError as e:
            raise KingdeeError('登录 HTTP %s' % e.code, code=e.code,
                               raw=e.read().decode('utf-8', 'replace')[:400])
        except Exception as e:
            raise KingdeeError('连接金蝶失败：%s（检查地址与网络）' % e)
        for ck in cookies:
            if 'kdservice-sessionid' in ck:
                self.cookie = ck.split(';')[0]
                break
        try:
            d = json.loads(txt)
        except Exception:
            raise KingdeeError('登录返回非 JSON', raw=txt[:300])
        if int(d.get('LoginResultType') or 0) != 1:
            raise KingdeeError('登录失败：%s' % (d.get('Message') or d.get('MessageCode') or txt[:120]))
        self._at = time.time()
        return True

    def _post(self, path, payload):
        if not self.cookie or time.time() - self._at > 900:
            self.login()
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(self.host + path, data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        req.add_header('Cookie', self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                txt = r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            raise KingdeeError('HTTP %s' % e.code, code=e.code, raw=e.read().decode('utf-8', 'replace')[:400])
        except Exception as e:
            raise KingdeeError('请求失败：%s' % e)
        try:
            return json.loads(txt)
        except Exception:
            raise KingdeeError('返回非 JSON', raw=txt[:300])

    # ── 单据查询 ──────────────────────────────────────────
    def bill_query(self, form_id, field_keys, filter_str='', limit=200, max_pages=10, order=''):
        """返回 [{字段: 值}]，字段名与 FieldKeys 顺序一一对应"""
        keys = [k.strip() for k in str(field_keys).split(',') if k.strip()]
        out, start = [], 0
        for _ in range(max_pages):
            payload = {'data': {
                'FormId': form_id, 'FieldKeys': field_keys, 'FilterString': filter_str or '',
                'OrderString': order, 'TopRowCount': 0, 'StartRow': start, 'Limit': limit,
            }}
            d = self._post(QUERY_PATH, payload)
            if isinstance(d, dict):
                msg = json.dumps(d, ensure_ascii=False)[:300]
                if 'ResponseStatus' in d or 'Result' in d:
                    raise KingdeeError('查询失败：%s' % msg, raw=msg)
                rows = d.get('data') or []
            else:
                rows = d or []
            if not rows:
                break
            for r in rows:
                if isinstance(r, list):
                    out.append({keys[i] if i < len(keys) else 'F%d' % i: (r[i] if i < len(r) else '')
                                for i in range(len(keys))})
                elif isinstance(r, dict):
                    out.append(r)
            if len(rows) < limit:
                break
            start += limit
        return out

    # ── 用户 ──────────────────────────────────────────────
    def users(self):
        """金蝶用户列表（人员绑定用）；字段不存在时自动降级"""
        tries = [
            ('SEC_User', 'FUserAccount,FName,FEmail,FMobile,FForbidden', ''),
            ('SEC_User', 'FUserAccount,FName', ''),
        ]
        last = None
        for form, keys, flt in tries:
            try:
                rows = self.bill_query(form, keys, flt)
                out = []
                for r in rows:
                    out.append({
                        'account': str(r.get('FUserAccount') or '').strip(),
                        'name': str(r.get('FName') or '').strip(),
                        'email': str(r.get('FEmail') or '').strip(),
                        'mobile': str(r.get('FMobile') or '').strip(),
                        'forbidden': str(r.get('FForbidden') or ''),
                    })
                return [u for u in out if u['account']]
            except KingdeeError as e:
                last = e
        if last:
            raise last
        return []

    def pending_bills(self, user_account, form_ids, days=180):
        """某审批人名下待审单据（状态 A/B）"""
        out = []
        since = time.strftime('%Y-%m-%d', time.localtime(time.time() - days * 86400))
        for form in form_ids:
            keys = 'FBillNo,FDate,FDocumentStatus,FCreatorId.FName,FAuditor.FName,FApproveDate'
            flt = ("FAuditor.FNumber='%s' and FDocumentStatus in ('A','B') and FDate>='%s'"
                   % (user_account.replace("'", ''), since))
            try:
                for r in self.bill_query(form, keys, flt):
                    out.append(normalize_bill(r, form))
            except KingdeeError:
                continue
        return out


def normalize_bill(r, form_id=''):
    st = str(r.get('FDocumentStatus') or '')
    no = str(r.get('FBillNo') or '')
    return {
        'id': '%s:%s' % (form_id, no),
        'title': '%s %s' % (form_id, no),
        'bill_no': no,
        'form': form_id,
        'status': STATUS_MAP.get(st, '进行中'),
        'status_text': STATUS_TEXT.get(st, st),
        'date': str(r.get('FDate') or '')[:10],
        'creator': str(r.get('FCreatorId.FName') or r.get('FCreatorId') or ''),
        'auditor': str(r.get('FAuditor.FName') or r.get('FAuditor') or ''),
        'approve_date': str(r.get('FApproveDate') or '')[:10],
    }
