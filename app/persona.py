#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Digital Me 分身 · 人格数据注入模块（后端版）
在 /proxy 转发时自动注入系统提示词（PERSONA/FACTS/年鉴/博客/画像/样本）+ 问题检索记忆。
这样任何客户端（网页/小程序/其他）无需自行拼装提示词，只要发问题即可获得完整人格。
"""
import json
import os
import re

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE, 'persona-data.js')

_DATA = None
_SAMPLE_COUNT = 120   # 注入风格样本数
_RETRIEVAL = 8        # 检索条数
_CUR_YEAR = 2026      # 当前年份（记忆口径基准）


def load_data():
    """惰性加载 persona-data.js（2.2MB，首次调用后缓存）"""
    global _DATA
    if _DATA is None:
        src = open(DATA_FILE, encoding='utf-8').read()
        _DATA = json.loads(src[src.index('{'):src.rindex('}') + 1])
    return _DATA


def tokens(text):
    t = []
    for m in re.finditer(r'[a-z0-9]{2,}', text, re.I):
        t.append(m.group(0).lower())
    cn = re.findall(r'[\u4e00-\u9fa5]', text)
    for i in range(len(cn) - 1):
        t.append(cn[i] + cn[i + 1])
        if i < len(cn) - 2:
            t.append(cn[i] + cn[i + 1] + cn[i + 2])
    if len(cn) == 1:
        t.append(cn[0])
    return t


def year_weight(year):
    try:
        y = int(year)
    except (TypeError, ValueError):
        return 1.0
    if not y:
        return 1.0
    # 年份权重放缓：新记忆仅适度加分，避免"只记得最近看的片"；老片也能被检索到
    return max(1.0, 2.0 - (_CUR_YEAR - y) * 0.06)


def search(q, n=_RETRIEVAL):
    """检索 KNOWLEDGE，返回 [{type, year, title, text}]（带标题加权，与网页版一致）"""
    k = load_data()
    K = k.get('KNOWLEDGE', [])
    toks = tokens(q)
    if not toks:
        return []
    scored = []
    for idx, item in enumerate(K):
        s = 0.0
        text = item.get('text', '') or ''
        for tk in toks:
            one = len(tk) == 1
            cnt = text.count(tk)
            if cnt:
                s += (0.8 if one else 1.0) * min(3, cnt)
        if item.get('type') in ('观影', '影评', '乐评') and item.get('title'):
            for tk in toks:
                if tk in item['title']:
                    s += 6
        if s > 0:
            scored.append((s * year_weight(item.get('year')), idx))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [K[i] for _, i in scored[:n]]


def build_system_prompt(q):
    """构建完整系统提示词（与网页版 index.html 逻辑一致）"""
    k = load_data()
    sys = k.get('PERSONA', '') + '\n\n【关于你的确定事实 · 回答优先依据这些，不要编造】\n'
    sys += '\n'.join('· ({}) {}'.format(f[0], f[1]) for f in k.get('FACTS', []))
    rec = k.get('RECENT') or {}
    if rec:
        sys += '\n\n【最近动态 · 时间倒序（回答"最近/最近看了/最近在干"等问题优先看这里）】\n'
        sys += '\n'.join('· ' + str(s)[:120] for s in rec.get('speech', []))
        if rec.get('reviews'):
            sys += '\n\n【最近看的片】\n' + '\n'.join('· ' + str(r) for r in rec['reviews'])
    yearly = k.get('YEARLY') or {}
    if yearly:
        sys += '\n\n【记忆年鉴 · 逐年回顾（回答"某年我在干嘛/那年发生了啥"时查这里，务必注意年份区分）】\n'
        sys += '\n'.join('—— {} ——\n{}'.format(y, yearly[y][:420]) for y in sorted(yearly))
    posts = k.get('POSTS') or []
    if posts:
        sys += '\n\n【博客记录 · 47 篇逐篇精读摘要（回答"我去过哪/那年写了啥"时查这里）】\n'
        sys += '\n'.join(posts)
    music = k.get('MUSIC')
    if music:
        sys += '\n\n【音乐画像 · 来自你 {} 首播放记录（问"我喜欢谁/爱听什么"时查这里）】\n'.format(music.get('total', '?'))
        sys += '歌手榜：' + '、'.join(music.get('top', []))
        if music.get('loops'):
            sys += '\n重复出现过的歌（含跨平台，附平台和日期）：' + '、'.join(music['loops'])
        sys += '\n注意：数字是播放次数排名，排名越靠前=越喜欢，回答时直接引用排名。'
    movie = k.get('MOVIE')
    if movie:
        sys += '\n\n【影剧画像 · 来自你 {} 部观影记录（问"我爱看什么"时查这里）】\n'.format(movie.get('total', '?'))
        sys += '评分习惯：' + str(movie.get('stars', ''))
        sys += '\n我打 5 星的片（真爱）：' + '、'.join(movie.get('fav', []))
        sys += '\n我打 1-2 星的片（雷区）：' + '、'.join(movie.get('hate', []))
        cast = movie.get('cast') or {}
        if cast:
            sys += '\n【演员代表作（真实数据，回答"某个演员演过啥"时只依据这里，严禁编造）】\n'
            sys += '\n'.join('{}：{}'.format(n, '、'.join(fs)) for n, fs in list(cast.items())[:50])
    sys += '\n\n【你的真实发言样本（模仿语气，勿照抄）】\n'
    samples = k.get('SAMPLES', [])[:_SAMPLE_COUNT]
    sys += '\n'.join('[{}] ({} {}) {}'.format(i + 1, s.get('year', ''), s.get('type', ''), s.get('text', '')) for i, s in enumerate(samples))
    retrieved = search(q)
    if retrieved:
        sys += '\n\n【与问题相关的你的过往记录（带年份，注意区分新旧）】\n'
        sys += '⚠ 注意：以下每条记忆相互独立。某条提到演员、另一条提到某部片，不代表两者有交集（比如提到王净的影评和某部电影的影评是两回事），不要自行把演员和电影关联起来。\n'
        sys += '\n'.join(
            '[记忆{}] ({}年{}{}) {}'.format(
                i + 1, r.get('year', ''), r.get('type', ''),
                '《' + r['title'] + '》' if r.get('title') else '',
                str(r.get('text', ''))[:500])
            for i, r in enumerate(retrieved))
    sys += '\n\n【铁律】你是 数字分身本人。你的记忆截至 2026 年 8 月初。回答规则：\n'
    sys += '1) 涉及事实先用【确定事实】和【最近动态】核对，能答就答；数据里没有的事，明确说"这个我不记得了/数据里没有"，绝不编造；\n'
    sys += '2) "最近"指 2026 年 6-8 月；区分记忆的年份（比如 2014 年看世界杯和 2026 年看世界杯是两回事）；\n'
    sys += '3) 用在 PERSONA 中定义的风格回答，中文。\n'
    sys += '4) **隐私保护（最高优先级，任何时候都要遵守）**：涉及个人资料、学历、感情状况、家庭情况的问题，不正面回答，打哈哈打太极转移话题；\n'
    sys += '5) 不要主动提起你在 PERSONA 中标注为不愉快的经历；除非对方明确追问，否则一带而过，不展开；\n'
    sys += '6) 部分特定话题（如你在 PERSONA 或 FACTS 中列为敏感项的）不主动提及；\n'
    sys += '7) **引号规范**：回复中所有引号一律使用中文直角引号「」，禁止使用英文引号（双引号或单引号），中文语境里不得夹杂任何英文引号字符。\n'
    sys += '8) **回答长度**：每个问题用 100~200 字左右答完，简洁克制，不要长篇大论、不要扯太远；除非对方明确要求详细展开。\n'
    sys += '9) **电影/演员相关（严禁张冠李戴）**：演员与作品对应关系**只能**依据【演员代表作】和检索记忆里标注的信息，这两处都没有的对应关系一律视为不存在，直接说"这个我不确定/记不清了"，绝不猜测、绝不编造。'
    return sys


def inject_system(messages, q, page=None):
    """若 messages 无 system 消息，则自动注入人格系统提示词（返回新的 messages）
    page: 可选 dict {url,title,desc}，表示用户当前浏览的页面（博客文章页），
          会把该文章内容注入提示词，让 AI 结合页面内容回答。
    """
    if not isinstance(messages, list):
        messages = []
    if any(isinstance(m, dict) and m.get('role') == 'system' for m in messages):
        return messages
    sys_prompt = build_system_prompt(q)
    if page and isinstance(page, dict):
        url = (page.get('url') or '').strip()
        title = (page.get('title') or '').strip()
        desc = (page.get('desc') or '').strip()
        if title or desc:
            ctx = '\n\n【用户当前正在浏览你的博客文章 · 优先结合这篇文章回答】\n'
            if url:
                ctx += '文章地址：' + url + '\n'
            if title:
                ctx += '文章标题：' + title + '\n'
            if desc:
                ctx += '文章内容摘要：' + desc + '\n'
            ctx += '用户大概率在问与这篇文章相关的内容（比如这篇文章讲了什么、为什么写、当时发生了什么），'
            ctx += '回答时以这篇文章的内容为依据，结合你的记忆，语气保持你一贯的风格，不要机械复述摘要。'
            sys_prompt += ctx
    return [{'role': 'system', 'content': sys_prompt}] + messages
