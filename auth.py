# -*- coding: utf-8 -*-
"""鉴权：管理员密码（PBKDF2）+ 会话 token + 钉钉免登

规则
- 管理密码：部署时环境变量 WB_ADMIN_PASS 初始化；未设置则用 admin123 并要求尽快修改
- 会话：随机 token 存 sessions 表，7 天有效；Authorization: Bearer <token>
- 员工身份：钉钉 JSAPI authCode → 服务端换 userid → 必须存在于 persons/accounts 白名单
"""
import hashlib
import hmac
import os
import secrets

import db
import settings
from dingtalk import Client as DingClient, DingError

ITER = 120000


# ── 密码 ──────────────────────────────────────────────────────────────
def hash_password(pw):
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), bytes.fromhex(salt), ITER)
    return 'pbkdf2$%d$%s$%s' % (ITER, salt, dk.hex())


def verify_password(pw, stored):
    try:
        algo, it, salt, hexdig = (stored or '').split('$')
        if algo != 'pbkdf2':
            return False
        dk = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), hexdig)
    except Exception:
        return False


def ensure_admin():
    """首次启动初始化管理密码"""
    cfg = db.cfg_all()
    if (cfg.get('admin:pass_hash') or '').strip():
        return False
    pw = os.environ.get('WB_ADMIN_PASS', '') or 'admin123'
    db.cfg_set({'admin:pass_hash': hash_password(pw)})
    if not os.environ.get('WB_ADMIN_PASS'):
        print('[SECURITY] 未设置 WB_ADMIN_PASS，管理密码已初始化为 admin123，请登录后立即修改！')
    return True


def admin_login(password):
    cfg = db.cfg_all()
    stored = (cfg.get('admin:pass_hash') or '').strip()
    if not stored or not verify_password(password or '', stored):
        return None
    tok, exp = db.session_create('admin', name='管理员')
    return {'token': tok, 'expires': exp, 'kind': 'admin', 'name': '管理员'}


def admin_password_valid(password):
    cfg = db.cfg_all()
    return verify_password(password or '', (cfg.get('admin:pass_hash') or '').strip())


def admin_change_password(old, new):
    if not admin_password_valid(old):
        return False, '原密码不正确'
    if len(new or '') < 6:
        return False, '新密码至少 6 位'
    db.cfg_set({'admin:pass_hash': hash_password(new)})
    db.sessions_clear('admin')  # 改密后强制重新登录
    return True, '已修改'


# ── 会话 ──────────────────────────────────────────────────────────────
def _mint(cfg, person, userid):
    """决定会话角色。

    规则（默认）：钉钉单点登录 / 扫码登录 **一律是员工会话**，
    配置中心只在「管理员密码登录」时可见。
    若确实需要「管理员钉钉账号登录即管理员」，在平台配置里把
    dingtalk:sso_admin 设为 1 才会走直通。
    """
    admin_uid = (cfg.get('admin_userid') or '').strip()
    sso_admin = str(cfg.get('sso_admin') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    name = person.get('name') or ''
    if sso_admin and admin_uid and admin_uid == (userid or '').strip():
        tok, exp = db.session_create('admin', name=name or '管理员')
        return {'token': tok, 'expires': exp, 'kind': 'admin', 'name': name or '管理员'}
    tok, exp = db.session_create('user', person['person_id'], name)
    # 盖登录时间戳（同步统计用），并把这个人标成「本轮优先同步」：
    # 定时器 30 秒内会给他单独跑一轮，登录后很快就能看到自己的数据
    try:
        db.person_set(person['person_id'], last_login=db.now())
        db.cfg_set({'dingtalk:sync_focus': person['person_id']})
    except Exception:
        pass
    return {'token': tok, 'expires': exp, 'kind': 'user',
            'name': name, 'person_id': person['person_id']}


def token_of(headers, query_token=''):
    auth = (headers.get('Authorization') or '') if headers else ''
    if auth.lower().startswith('bearer '):
        return auth[7:].strip()
    return (query_token or '').strip()


def current(headers, query_token=''):
    """返回 {'kind','person_id','name'} 或 None"""
    sess = db.session_get(token_of(headers, query_token))
    if not sess:
        return None
    out = {'kind': sess['kind'], 'person_id': sess.get('person_id') or '', 'name': sess.get('name') or ''}
    if sess['kind'] == 'user':
        p = db.person_get(sess.get('person_id') or '')
        if not p or p.get('status') == 'disabled':
            return None
        out['person'] = p
        out['name'] = p.get('name') or out['name']
    return out


# ── 钉钉免登 ──────────────────────────────────────────────────────────
def dingtalk_sso(code):
    """authCode → userid → person；返回 (session, person) 或抛 DingError"""
    code = (code or '').strip()
    if not code:
        raise DingError('缺少 authCode（请在钉钉客户端内打开）')
    cfg = settings.get_platform('dingtalk')
    if not (cfg.get('app_key') and cfg.get('app_secret')):
        raise DingError('管理员尚未配置钉钉应用凭证')
    cli = DingClient(cfg['app_key'], cfg['app_secret'], cfg.get('corp_id', ''))
    info = cli.userinfo_by_code(code)  # {'userid','name'}
    userid = info.get('userid') or ''
    if not userid:
        raise DingError('未能从 authCode 解析出 userid')
    acc = db.account_find('dingtalk', userid)
    if not acc:
        # 容错：按姓名唯一匹配一次
        cands = [p for p in db.persons_all() if p.get('name') == (info.get('name') or '')]
        if len(cands) == 1:
            db.account_add(cands[0]['person_id'], 'dingtalk', userid, info.get('name') or '')
            acc = db.account_find('dingtalk', userid)
    if not acc:
        raise DingError('账号 %s 不在工作台白名单内，请联系管理员开通' % (info.get('name') or userid))
    person = db.person_get(acc['person_id'])
    if not person:
        raise DingError('人员记录缺失，请联系管理员')
    return _mint(cfg, person, userid), person


# ── 钉钉扫码登录（PC 浏览器，无钉钉容器） ──────────────────────────────
def dingtalk_qr_login(code):
    """OAuth2 授权码 → unionId → userid → person；返回 (session, person)"""
    cfg = settings.get_platform('dingtalk')
    if not (cfg.get('app_key') and cfg.get('app_secret')):
        raise DingError('管理员尚未配置钉钉应用凭证（AppKey/AppSecret）')
    cli = DingClient(cfg['app_key'], cfg['app_secret'], cfg.get('corp_id', ''))
    info = cli.oauth_user(code)
    userid = info.get('userid') or ''
    unionid = info.get('unionid') or ''
    name = info.get('name') or ''

    def _key():
        # 优先 userid（与同步/免登同一键），退化为 unionId 键，保证同一人多次登录稳定
        if userid:
            return userid
        return ('union:' + unionid) if unionid else ''

    key = _key()
    acc = db.account_find('dingtalk', key) if key else None
    if not acc and unionid:
        acc = db.account_find('dingtalk', 'union:' + unionid)
    if not acc:
        # 手机号 / 邮箱任一命中即视为同一人（与同步阶段同一套规则）
        p = db.person_find_by_contact(info.get('mobile') or '', info.get('email') or '')
        if p and key:
            db.account_add(p['person_id'], 'dingtalk', key, name)
            acc = db.account_find('dingtalk', key)
    if not acc:
        raise DingError('钉钉账号 %s 不在工作台白名单内，请联系管理员开通' % (name or userid or unionid or '未知'))
    person = db.person_get(acc['person_id'])
    if not person:
        raise DingError('人员记录缺失，请联系管理员')
    return _mint(cfg, person, userid), person
