#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Digital Me · 后台管理模块
提供：SQLite 会话/对话日志、管理员登录校验、UA 解析、统计查询、密码修改、模型配置读写。
被 server.py 引用（登录页 /login、后台 /admin、日志记录）。
"""
import json
import os
import re
import sqlite3
import hashlib
import secrets
import time
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE, 'admin_db.db')
CONFIG_FILE = os.path.join(BASE, 'server-config.json')

# 默认管理员账号（首次启动自动创建；可通过环境变量覆盖初始密码）
ADMIN_USER = os.environ.get('DM_ADMIN_USER', 'admin')
ADMIN_PASS = os.environ.get('DM_ADMIN_PASS', 'admin123')

SESSION_TTL = timedelta(hours=720)   # 登录会话有效期（30 天，持久化到 SQLite，重启不失效）
_sessions = {}                       # token -> {user, expire_ts}（内存缓存，加速校验；持久层在 DB）

DEFAULT_MODELS = ['deepseek-chat', 'deepseek-reasoner', 'deepseek-v4-flash', 'deepseek-v4']

# ---------------- IP 归属查询（在线 API + 内存缓存） ----------------
# 说明：对话记录时不查询（避免拖慢流式响应）；后台展示时按 IP 查一次并缓存。
# 免费 API: ip-api.com，无 key，45 次/分钟；缓存 24h，基本不会触发限流。
_GEO_CACHE = {}          # ip -> geo 字符串
_GEO_CACHE_TTL = 24 * 3600
_GEO_API = 'http://ip-api.com/json/{ip}?lang=zh-CN&fields=status,country,regionName,city,isp'


def lookup_geo(ip, force=False):
    """反查 IP 归属，返回如 '山东 济南 电信'；失败返回 ''。带 24h 内存缓存（失败也缓存 5 分钟防打爆 API）。"""
    if not ip:
        return ''
    now = time.time()
    cached = _GEO_CACHE.get(ip)
    if cached and (force or now - cached[1] < _GEO_CACHE_TTL):
        return cached[0]
    if len(_GEO_CACHE) > 5000:          # 防内存膨胀
        _GEO_CACHE.clear()
    geo = ''
    for attempt in range(2):            # 失败重试一次
        try:
            import urllib.request
            url = _GEO_API.format(ip=ip)
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            if data.get('status') == 'success':
                country = data.get('country', '')
                region = data.get('regionName', '')
                city = data.get('city', '')
                isp = data.get('isp', '')
                # 运营商简称：优先中文关键词，其次英文常见运营商名
                isp_short = ''
                isp_low = (isp or '').lower()
                for kw, label in (('telecom', '电信'), ('unicom', '联通'), ('mobile', '移动'),
                                  ('alibaba', '阿里云'), ('tencent', '腾讯云'), ('baidu', '百度'),
                                  ('huawei', '华为云'), ('aliyun', '阿里云'), ('cernet', '教育网')):
                    if kw in isp_low:
                        isp_short = label
                        break
                if not isp_short:
                    for kw in ('电信', '联通', '移动', '铁通', '鹏博士', '教育网', '阿里', '腾讯', '百度'):
                        if kw in (isp or ''):
                            isp_short = kw
                            break
                # 城市去后缀：'济南市' -> '济南'
                if city.endswith('市'):
                    city = city[:-1]
                if region.endswith('省') or region.endswith('市'):
                    region = region[:-1]
                parts = []
                for p in (region, city, isp_short):
                    if p and p not in parts:
                        parts.append(p)
                geo = ' '.join(parts)
                if not geo and country:
                    geo = country
                if not geo:
                    geo = isp or ''
                break
            # status != success：等待后重试
            time.sleep(1.5)
        except Exception:
            time.sleep(1.5)
    _GEO_CACHE[ip] = (geo, now)
    return geo


# ---------------------------------------------------------------- DB

def _conn():
    db = sqlite3.connect(DB_FILE)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    db = _conn()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            pass_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT DEFAULT '',
            ua TEXT DEFAULT '',
            device TEXT DEFAULT '',
            browser TEXT DEFAULT '',
            os TEXT DEFAULT '',
            geo TEXT DEFAULT '',
            question TEXT DEFAULT '',
            answer TEXT DEFAULT '',
            duration_ms INTEGER DEFAULT 0,
            ok INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            expire_ts REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_conv_time ON conversations(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_conv_ip ON conversations(ip);
        CREATE INDEX IF NOT EXISTS idx_conv_geo ON conversations(geo);
    ''')
    # 兼容旧库：conversations 缺 geo 列时补上
    try:
        cols = [r[1] for r in db.execute('PRAGMA table_info(conversations)')]
        if 'geo' not in cols:
            db.execute('ALTER TABLE conversations ADD COLUMN geo TEXT DEFAULT ""')
    except Exception:
        pass
    # 种子管理员
    row = db.execute('SELECT id FROM admins WHERE username=?', (ADMIN_USER,)).fetchone()
    if not row:
        db.execute('INSERT INTO admins (username, pass_hash) VALUES (?, ?)',
                   (ADMIN_USER, _hash_pass(ADMIN_PASS)))
    db.commit()
    db.close()


def _hash_pass(pw, salt=None):
    if salt is None:
        salt = secrets.token_hex(8)
    h = hashlib.sha256((salt + pw).encode('utf-8')).hexdigest()
    return '{}:{}'.format(salt, h)


def _verify_pass(pw, stored):
    try:
        salt, h = stored.split(':', 1)
        return _hash_pass(pw, salt) == stored
    except Exception:
        return False


# ---------------------------------------------------------------- 密码修改

def change_password(username, old_pw, new_pw):
    """修改管理员密码，返回 (ok, msg)"""
    if not username:
        return False, '未登录'
    db = _conn()
    try:
        row = db.execute('SELECT * FROM admins WHERE username=?', (username,)).fetchone()
        if not row or not _verify_pass(old_pw, row['pass_hash']):
            return False, '旧密码不正确'
        if not new_pw or len(new_pw) < 6:
            return False, '新密码至少 6 位'
        if new_pw == old_pw:
            return False, '新密码不能与旧密码相同'
        db.execute('UPDATE admins SET pass_hash=? WHERE id=?',
                   (_hash_pass(new_pw), row['id']))
        db.commit()
        return True, '密码已更新'
    finally:
        db.close()


# ---------------------------------------------------------------- 模型配置（多方案）

def _config_file_data():
    """读 server-config.json 原始内容；文件不存在/损坏返回 {}"""
    try:
        if os.path.exists(CONFIG_FILE):
            return json.load(open(CONFIG_FILE, encoding='utf-8'))
    except Exception:
        pass
    return {}


def read_config():
    """读取当前生效的模型配置。
    结构：{max_tokens, temp(全局) + presets:[{name,base,key,model}] + active}
    兼容旧版（方案内带 max_tokens/temp 或顶层直接配置）。
    """
    defaults = {
        'base': 'https://api.deepseek.com/v1',
        'key': '',
        'model': 'deepseek-chat',
        'max_tokens': 2000,
        'temp': 0.9,
    }
    raw = _config_file_data()
    cfg = dict(defaults)

    # 全局参数：优先顶层，其次旧方案内（兼容）
    if raw.get('max_tokens'):
        cfg['max_tokens'] = raw['max_tokens']
    if raw.get('temp') is not None:
        cfg['temp'] = raw['temp']

    presets = raw.get('presets')
    if isinstance(presets, list) and presets:
        active = raw.get('active', '') or presets[0].get('name', '')
        cur = next((p for p in presets if p.get('name') == active), presets[0])
        for k in ('base', 'key', 'model'):
            if cur.get(k):
                cfg[k] = cur[k]
        # 旧版方案内可能有 max_tokens/temp：无顶层全局时兜底
        if not raw.get('max_tokens') and cur.get('max_tokens'):
            cfg['max_tokens'] = cur['max_tokens']
        if raw.get('temp') is None and cur.get('temp') is not None:
            cfg['temp'] = cur['temp']
        cfg['preset_name'] = cur.get('name', '')
    else:
        # 旧版单方案格式（顶层直接 base/key/model/max_tokens/temp）
        for k in defaults:
            if raw.get(k):
                cfg[k] = raw[k]
    return cfg


def list_presets():
    """返回全部方案 [{name, base, model, has_key, active}]（key 脱敏）"""
    raw = _config_file_data()
    presets = raw.get('presets')
    active = raw.get('active', '')
    if not isinstance(presets, list) or not presets:
        # 无 presets 时，用旧格式兜底成一个方案（以当前模型名为方案名）
        cfg = read_config()
        default_name = cfg.get('model') or '默认方案'
        presets = [{
            'name': default_name,
            'base': cfg['base'],
            'key': cfg['key'],
            'model': cfg['model'],
        }]
        active = default_name
    out = []
    for p in presets:
        item = {
            'name': p.get('name', ''),
            'base': p.get('base', ''),
            'model': p.get('model', ''),
            'has_key': bool(p.get('key')),
            'key': '',
            'active': (p.get('name') == active),
        }
        out.append(item)
    return out


def save_config(cfg):
    """保存配置。
    - cfg 带 preset_name：更新对应方案（仅 base/key/model，支持 new_name 重命名）
    - cfg 带 global 标记：更新全局 max_tokens/temp
    返回 (ok, msg)
    """
    try:
        raw = _config_file_data()
        name = cfg.pop('preset_name', None) or ''
        is_global = cfg.pop('global', False)

        if is_global:
            # 全局参数
            if cfg.get('max_tokens') is not None:
                raw['max_tokens'] = max(256, int(cfg['max_tokens']))
            if cfg.get('temp') is not None:
                raw['temp'] = min(2.0, max(0.0, float(cfg['temp'])))
            json.dump(raw, open(CONFIG_FILE, 'w', encoding='utf-8'),
                      ensure_ascii=False, indent=2)
            return True, '全局参数已保存'

        # 方案字段（仅 base/key/model）
        new_name = (cfg.pop('new_name', None) or '').strip()
        data = {}
        if cfg.get('base'):
            data['base'] = str(cfg['base']).strip()
        if cfg.get('model'):
            data['model'] = str(cfg['model']).strip()
        if cfg.get('key'):
            data['key'] = str(cfg['key']).strip()

        presets = raw.get('presets')
        if isinstance(presets, list) and presets:
            if name:
                for p in presets:
                    if p.get('name') == name:
                        p.update(data)
                        if new_name and new_name != name:
                            if any(q.get('name') == new_name for q in presets):
                                return False, '方案名已存在: {}'.format(new_name)
                            p['name'] = new_name
                            if raw.get('active') == name:
                                raw['active'] = new_name
                        break
                else:
                    return False, '方案不存在: {}'.format(name)
            else:
                active = raw.get('active', '') or presets[0].get('name', '')
                for p in presets:
                    if p.get('name') == active:
                        p.update(data)
                        break
                else:
                    presets[0].update(data)
            raw['presets'] = presets
        else:
            # 旧版单方案：写入顶层（保持向后兼容）
            raw.update(data)
        json.dump(raw, open(CONFIG_FILE, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=2)
        return True, '配置已保存'
    except Exception as e:
        return False, '保存失败: {}'.format(e)


def add_preset(name, base, key, model):
    """新增方案（仅 name/base/key/model），返回 (ok, msg)。若当前无激活方案，新方案自动激活。"""
    try:
        if not key:
            return False, 'API Key 不能为空'
        if not base or not model:
            return False, 'Base URL 和模型名不能为空'
        raw = _config_file_data()
        presets = raw.get('presets')
        if not isinstance(presets, list):
            # 把旧格式迁移成 presets（以当前模型名为方案名）
            cfg = read_config()
            old_name = raw.get('preset_name') or (cfg.get('model') or '默认方案')
            presets = [{
                'name': old_name,
                'base': cfg['base'], 'key': cfg['key'], 'model': cfg['model'],
            }]
            if not raw.get('active'):
                raw['active'] = old_name
        if any(p.get('name') == name for p in presets):
            return False, '方案名已存在: {}'.format(name)
        presets.append({
            'name': name, 'base': base, 'key': key, 'model': model,
        })
        # 无激活方案时自动激活新方案
        if not raw.get('active') or not any(p.get('name') == raw.get('active') for p in presets):
            raw['active'] = name
        raw['presets'] = presets
        json.dump(raw, open(CONFIG_FILE, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=2)
        return True, '方案已添加'
    except Exception as e:
        return False, '添加失败: {}'.format(e)


def delete_preset(name):
    """删除方案（不允许删除最后一个；若删除当前激活方案则激活第一个），返回 (ok, msg)"""
    try:
        raw = _config_file_data()
        presets = raw.get('presets')
        if not isinstance(presets, list) or not presets:
            return False, '没有可删除的方案'
        if len(presets) <= 1:
            return False, '至少保留一个方案'
        if not any(p.get('name') == name for p in presets):
            return False, '方案不存在'
        presets = [p for p in presets if p.get('name') != name]
        if raw.get('active') == name:
            raw['active'] = presets[0].get('name', '')
        raw['presets'] = presets
        json.dump(raw, open(CONFIG_FILE, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=2)
        return True, '方案已删除'
    except Exception as e:
        return False, '删除失败: {}'.format(e)


def activate_preset(name):
    """切换激活方案，返回 (ok, msg)"""
    try:
        raw = _config_file_data()
        presets = raw.get('presets')
        if not isinstance(presets, list) or not any(p.get('name') == name for p in presets):
            return False, '方案不存在'
        raw['active'] = name
        json.dump(raw, open(CONFIG_FILE, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=2)
        return True, '已切换为「{}」'.format(name)
    except Exception as e:
        return False, '切换失败: {}'.format(e)


# ---------------------------------------------------------------- 登录 / 会话

def check_login(username, password):
    db = _conn()
    row = db.execute('SELECT * FROM admins WHERE username=?', (username,)).fetchone()
    db.close()
    if row and _verify_pass(password, row['pass_hash']):
        return True
    return False


def create_session(username):
    token = secrets.token_urlsafe(32)
    expire_ts = time.time() + SESSION_TTL.total_seconds()
    db = _conn()
    try:
        db.execute('INSERT INTO sessions (token, username, expire_ts) VALUES (?, ?, ?)',
                   (token, username, expire_ts))
        db.commit()
    finally:
        db.close()
    _sessions[token] = {'user': username, 'expire_ts': expire_ts}
    return token


def validate_session(token):
    if not token:
        return None
    s = _sessions.get(token)
    if s:
        if s['expire_ts'] < time.time():
            _sessions.pop(token, None)
            destroy_session(token)
            return None
        return s['user']
    # 内存无缓存 → 查 DB（服务重启后第一次访问走这里）
    db = _conn()
    try:
        row = db.execute('SELECT username, expire_ts FROM sessions WHERE token=?', (token,)).fetchone()
    finally:
        db.close()
    if not row:
        return None
    if row['expire_ts'] < time.time():
        destroy_session(token)
        return None
    _sessions[token] = {'user': row['username'], 'expire_ts': row['expire_ts']}
    return row['username']


def destroy_session(token):
    _sessions.pop(token, None)
    db = _conn()
    try:
        db.execute('DELETE FROM sessions WHERE token=?', (token,))
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------- 日志记录

def parse_ua(ua):
    """从 User-Agent 提取 设备/浏览器/系统（尽力而为）"""
    ua = ua or ''
    low = ua.lower()
    device = 'PC'
    if 'iphone' in low:
        device = 'iPhone'
    elif 'ipad' in low:
        device = 'iPad'
    elif 'android' in low:
        device = 'Android'
    elif 'mobile' in low:
        device = 'Mobile'

    browser = 'Other'
    if 'edg' in low:
        browser = 'Edge'
    elif 'chrome' in low and 'chromium' not in low:
        browser = 'Chrome'
    elif 'firefox' in low:
        browser = 'Firefox'
    elif 'safari' in low and 'chrome' not in low:
        browser = 'Safari'
    elif 'micromessenger' in low:
        browser = '微信内置'

    osys = 'Other'
    if 'iphone' in low or 'ipad' in low or 'ios' in low:
        osys = 'iOS'
    elif 'android' in low:
        osys = 'Android'
    elif 'windows' in low:
        osys = 'Windows'
    elif 'mac os' in low or 'macintosh' in low:
        osys = 'macOS'
    elif 'linux' in low:
        osys = 'Linux'
    return device, browser, osys


def get_client_ip(handler):
    """取客户端 IP：优先 X-Forwarded-For（反代/frp 场景）"""
    xff = handler.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    xri = handler.headers.get('X-Real-IP', '')
    if xri:
        return xri.strip()
    return handler.client_address[0] if handler.client_address else ''


def log_conversation(handler, question, answer='', duration_ms=0, ok=True):
    """记录一轮对话（流式回复完成后调用）"""
    ip = get_client_ip(handler)
    ua = handler.headers.get('User-Agent', '')
    device, browser, osys = parse_ua(ua)
    db = _conn()
    try:
        db.execute(
            'INSERT INTO conversations (ip, ua, device, browser, os, question, answer, duration_ms, ok) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (ip, ua, device, browser, osys, question, answer, duration_ms, 1 if ok else 0))
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------- 查询

def _save_geo_to_db(cid, geo):
    """把反查到的 geo 写回 DB，避免重复查询"""
    try:
        db = _conn()
        db.execute('UPDATE conversations SET geo=? WHERE id=?', (geo, cid))
        db.commit()
        db.close()
    except Exception:
        pass


def query_conversations(page=1, per_page=20, keyword='', province='', city=''):
    """查询并按 IP 分组返回。
    优化：SQL 层直接按 IP 分组分页，避免全表加载。
    返回：{total_groups, page, pages, groups:[{ip, geo, device, browser, os, first_time,
           questions:[{id, created_at, question, answer, duration_ms, ok}]}]}
    """
    db = _conn()
    where, args = [], []
    if keyword:
        where.append('(question LIKE ? OR answer LIKE ?)')
        like = '%{}%'.format(keyword)
        args += [like, like]
    if province:
        where.append('(geo LIKE ? OR ip IN (SELECT ip FROM conversations WHERE geo LIKE ?))')
        args += ['%{}%'.format(province)] * 2
    if city:
        where.append('(geo LIKE ? OR ip IN (SELECT ip FROM conversations WHERE geo LIKE ?))')
        args += ['%{}%'.format(city)] * 2
    wsql = ('WHERE ' + ' AND '.join(where)) if where else ''

    # 1. 先查总组数（按 DISTINCT ip）
    total = db.execute('SELECT COUNT(DISTINCT ip) c FROM conversations ' + wsql, args).fetchone()['c']

    # 2. SQL 层按 IP 分组分页，只取当前页的 IP
    offset = (page - 1) * per_page
    ip_rows = db.execute(
        'SELECT ip, MAX(geo) AS geo, MAX(device) AS device, MAX(browser) AS browser, '
        'MAX(os) AS os, MIN(datetime(created_at, "+8 hours")) AS first_time '
        'FROM conversations ' + wsql +
        ' GROUP BY ip ORDER BY MAX(id) DESC LIMIT ? OFFSET ?',
        args + [per_page, offset]
    ).fetchall()

    # 3. 查这些 IP 的详细对话记录
    groups = []
    if ip_rows:
        ips = [r['ip'] for r in ip_rows]
        placeholders = ','.join('?' * len(ips))
        detail_rows = db.execute(
            'SELECT id, ip, ua, device, browser, os, question, answer, duration_ms, ok, '
            'datetime(created_at, "+8 hours") AS created_at_bj, geo '
            'FROM conversations WHERE ip IN ({}) ORDER BY id DESC'.format(placeholders),
            ips
        ).fetchall()
        # 分组
        gmap = {}
        for r in ip_rows:
            gmap[r['ip']] = {
                'ip': r['ip'], 'geo': r['geo'] or '', 'device': r['device'] or '',
                'browser': r['browser'] or '', 'os': r['os'] or '',
                'first_time': r['first_time'] or '', 'questions': []
            }
            groups.append(gmap[r['ip']])
        for r in detail_rows:
            d = dict(r)
            d['created_at'] = d.pop('created_at_bj', d.get('created_at', ''))
            ip = d.get('ip') or ''
            # geo 缓存：DB 有值就用，没有才实时查（并写回）
            if not d.get('geo') and ip:
                d['geo'] = lookup_geo(ip)
                _save_geo_to_db(d['id'], d['geo'])
            if ip in gmap:
                gmap[ip]['questions'].append(d)
    db.close()

    return {
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': max(1, (total + per_page - 1) // per_page),
        'groups': groups,
    }


def query_regions():
    """返回省市层级（从已反查的 geo 提取）：{provinces:[...], cities:{省:[市...]}}"""
    db = _conn()
    rows = db.execute('SELECT DISTINCT geo FROM conversations WHERE geo != ""').fetchall()
    db.close()
    provinces, cities = [], {}
    seen_p, seen_c = set(), set()
    for r in rows:
        geo = r['geo'] or ''
        # geo 格式："省份 城市 运营商" 或 "直辖市 运营商"（如 "北京 联通"）
        parts = geo.split()
        if not parts:
            continue
        if parts[0] in ('北京', '上海', '天津', '重庆'):
            prov = parts[0]
            city = ''
        else:
            prov = parts[0] if len(parts) >= 1 else ''
            city = parts[1] if len(parts) >= 2 else ''
        if prov and prov not in seen_p:
            seen_p.add(prov)
            provinces.append(prov)
        if prov and city:
            cities.setdefault(prov, [])
            if city not in cities[prov]:
                cities[prov].append(city)
    return {'provinces': provinces, 'cities': cities}


def query_stats():
    db = _conn()
    total = db.execute('SELECT COUNT(*) c FROM conversations').fetchone()['c']
    # created_at 存的是 UTC，北京时间 = UTC + 8
    today = db.execute(
        "SELECT COUNT(*) c FROM conversations WHERE date(created_at, '+8 hours')=date('now', '+8 hours')"
    ).fetchone()['c']
    ips = db.execute('SELECT COUNT(DISTINCT ip) c FROM conversations').fetchone()['c']
    devices = db.execute(
        'SELECT device, COUNT(*) c FROM conversations GROUP BY device ORDER BY c DESC'
    ).fetchall()
    oss = db.execute(
        'SELECT os, COUNT(*) c FROM conversations GROUP BY os ORDER BY c DESC'
    ).fetchall()
    browsers = db.execute(
        'SELECT browser, COUNT(*) c FROM conversations GROUP BY browser ORDER BY c DESC'
    ).fetchall()
    last7 = db.execute(
        "SELECT date(created_at, '+8 hours') d, COUNT(*) c FROM conversations "
        "WHERE datetime(created_at, '+8 hours') >= datetime('now', '+8 hours', '-6 day') "
        "GROUP BY date(created_at, '+8 hours') ORDER BY d"
    ).fetchall()
    db.close()
    return {
        'total': total,          # 累计对话 = PV
        'today': today,
        'unique_ips': ips,
        'devices': [dict(r) for r in devices],
        'os': [dict(r) for r in oss],
        'browsers': [dict(r) for r in browsers],
        'last7': [dict(r) for r in last7],
    }


def delete_conversation(cid):
    db = _conn()
    db.execute('DELETE FROM conversations WHERE id=?', (cid,))
    db.commit()
    db.close()


init_db()
