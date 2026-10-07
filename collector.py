#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上岸打卡系统 · 工作台数据采集脚本 collector.py
================================================================
用途：
    点击工作台任意板块「刷新/爬取」按钮 → 运行本脚本 → 同步采集全部 7 个来源 →
    写入统一 JSON（工作台读取该文件重新渲染）。

用法：
    python3 collector.py                       # 默认输出到脚本同目录 workbench_data.json
    python3 collector.py -o 指定路径.json       # 自定义输出路径
    python3 collector.py --limit 3             # 每个来源最多采集 N 篇（调试用）
    python3 collector.py --fast                # 缩短请求间隔（调试用，默认随机 1~3 秒）
    环境变量 WB_DATA_PATH 也可指定输出路径。

依赖：
    pip3 install requests beautifulsoup4 lxml
    可选（Playwright 兜底/优先渲染，强烈建议安装）：
        pip3 install playwright && python3 -m playwright install chromium

采集来源：
    1. 央视网 https://www.cctv.com/                       「要闻」→ 最新链接（Playwright 优先）
    2. 人民网 http://www.people.com.cn/GB/59476/...        「今日头条」+「今日要闻」合并去重（静态）
    3. 学习强国 头条新闻页                                  「头条新闻」→ 最近 72 小时（Playwright 优先）
    4. 新华网 https://www.news.cn/politics/index.html      「时政关注」→ 最新链接（Playwright 优先）
    5. 半月谈 http://www.banyuetan.org/byt/jinritan/...    「要闻top10」→ 全部 10 条（静态）
    6. 今日重庆 https://www.cq.gov.cn/ywdt/jrcq/index.html 「今日重庆」→ 最近 72 小时（静态）
    7. 学习强国 习近平文汇页                                「习近平文汇」+「学习重点」→ 全部可见链接
                                                            （数据含全部分页；金句写入 golden_quotes）

输出 JSON 结构（工作台约定）：
    {
      "updated_at": "YYYY-MM-DD HH:MM",
      "articles": [ {source, section, title, url, publish_time, summary, content, golden_quotes[]}, ... ],
      "sources_status": [ {source, status: ok|partial|failed, count, error, note}, ... ]   # 附加字段
    }
"""

import argparse
import ast
import datetime as dt
import json
import os
import random
import re
import sys
import time
import traceback
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 全局配置
# ---------------------------------------------------------------------------

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.environ.get("WB_DATA_PATH") or os.path.join(SCRIPT_DIR, "workbench_data.json")

DELAY_RANGE = (1.0, 3.0)          # 用户要求：请求间隔随机 1~3 秒
DETAIL_TIMEOUT = 25
LIST_TIMEOUT = 30

# 各来源采集篇数上限（"最新链接"的合理取量，可按需调整）
CAPS = {
    "cctv": 20,        # 央视网要闻
    "people": 14,      # 人民网今日头条+今日要闻
    "xuexi_headline": 20,  # 学习强国头条新闻（72h 内）
    "xinhua": 15,      # 新华网时政关注
    "banyuetan": 10,   # 半月谈要闻top10（固定 10 条）
    "jrcq": 15,        # 今日重庆（72h 内）
    "xuexi_wenhui": 59,  # 学习强国习近平文汇（2026-01-01 以来有更新的节）
    "xuexi_six": 300,  # 学习强国六板块（2026-01-01 以来全部，当前约 250 条）
}

# 来源编号 ↔ 内部 key（--only 用）
ONLY_MAP = {"1": "cctv", "2": "people", "3": "xuexi_headline", "4": "xinhua",
            "5": "banyuetan", "6": "jrcq", "7": "xuexi_wenhui", "8": "xuexi_six",
            "cctv": "cctv", "people": "people", "xuexi_headline": "xuexi_headline",
            "xinhua": "xinhua", "banyuetan": "banyuetan", "jrcq": "jrcq",
            "xuexi_wenhui": "xuexi_wenhui", "xuexi_six": "xuexi_six"}
KEY2NAME = {"cctv": "央视网·要闻", "people": "人民网", "xuexi_headline": "学习强国·头条新闻",
            "xinhua": "新华网·时政关注", "banyuetan": "半月谈·要闻top10", "jrcq": "今日重庆",
            "xuexi_wenhui": "学习强国·习近平文汇", "xuexi_six": "学习强国"}

HOURS_72 = dt.timedelta(hours=72)

# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

LOG_LINES = []          # 运行日志缓存（--serve 的 /status 进度展示用）


def log(msg):
    line = f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}"
    LOG_LINES.append(line)
    if len(LOG_LINES) > 400:
        del LOG_LINES[:200]
    print(line, flush=True)


def jitter():
    """请求间隔：默认随机 1~3 秒；--fast 模式缩短。"""
    lo, hi = DELAY_RANGE
    if FAST:
        lo, hi = 0.2, 0.6
    time.sleep(random.uniform(lo, hi))


def now_str():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def fmt_time(d):
    if not d:
        return ""
    return d.strftime("%Y-%m-%d %H:%M")


def norm_url(u):
    """URL 归一化（去 utm 等跟踪参数、去尾部 &），用于去重。"""
    if not u:
        return ""
    u = u.strip()
    p = urlparse(u)
    qs = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_")]
    q = urlencode(qs)
    core = f"{p.scheme}://{p.netloc}{p.path}"
    return core + (f"?{q}" if q else "")


_SESSION = requests.Session()
_SESSION.headers.update(BASE_HEADERS)


def fetch_static(url, referer=None, timeout=DETAIL_TIMEOUT):
    """静态抓取；自动修正编码（人民网/央视网/今日重庆/学习强国等多为 GBK 或误判 ISO-8859-1）。"""
    headers = {}
    if referer:
        headers["Referer"] = referer
    last_err = None
    for _ in range(2):  # 网络抖动最多重试 1 次（非来源级反复重试）
        try:
            jitter()
            r = _SESSION.get(url, headers=headers, timeout=timeout)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                continue
            if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
                r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:
            last_err = str(e)[:120]
    raise RuntimeError(f"静态抓取失败 {url} : {last_err}")


def fetch_json(url, referer=None, timeout=DETAIL_TIMEOUT):
    """抓取并解析 JSON（自动处理 JSONP 包裹）。"""
    headers = {"Referer": referer or "https://www.xuexi.cn/"}
    text = fetch_static(url, referer=referer, timeout=timeout)
    text = text.strip()
    m = re.match(r"^[\w$]+\((.*)\)\s*;?\s*$", text, re.S)
    if m:
        text = m.group(1)
    return json.loads(text)


# ---------------------------------------------------------------------------
# Playwright（可选；学习强国两个页面、央视网首页、新华网时政关注优先使用）
# ---------------------------------------------------------------------------

_PW_holder = {"pw": None, "browser": None}

def _get_browser():
    if _PW_holder["browser"] is not None:
        return _PW_holder["browser"]
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    try:
        _PW_holder["pw"] = sync_playwright().start()
        _PW_holder["browser"] = _PW_holder["pw"].chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"])
        return _PW_holder["browser"]
    except Exception as e:
        log(f"  [Playwright] 启动失败：{str(e)[:120]}（将回退静态抓取）")
        return None


def close_browser():
    try:
        if _PW_holder["browser"] is not None:
            _PW_holder["browser"].close()
            _PW_holder["pw"].stop()
    except Exception:
        pass
    _PW_holder["pw"] = _PW_holder["browser"] = None


def render_page(url, wait_ms=9000, json_pattern=None, referer=None):
    """用浏览器渲染页面；返回 (渲染后 HTML, 捕获到的 XHR JSON 文本列表)。
    json_pattern: 正则，命中该模式的响应体会被全文捕获（用于学习强国 lgdata 数据接口）。"""
    browser = _get_browser()
    if browser is None:
        return "", []
    ctx = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900},
                              locale="zh-CN")
    if referer:
        ctx.set_extra_http_headers({"Referer": referer})
    page = ctx.new_page()
    captured = []

    def _on_response(resp):
        try:
            if json_pattern and re.search(json_pattern, resp.url):
                captured.append(resp.text())
        except Exception:
            pass

    page.on("response", _on_response)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(wait_ms)
        html = page.content()
    except Exception as e:
        log(f"  [Playwright] 渲染异常 {url}: {str(e)[:100]}")
        html = ""
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    return html, captured


def render_page_text(url, wait_ms=7000, referer=None):
    """渲染详情页并返回 body 纯文本（兜底提取正文用）。"""
    browser = _get_browser()
    if browser is None:
        return ""
    ctx = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900},
                              locale="zh-CN")
    page = ctx.new_page()
    try:
        if referer:
            ctx.set_extra_http_headers({"Referer": referer})
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(wait_ms)
        return page.evaluate("()=>document.body?document.body.innerText:''")
    except Exception:
        return ""
    finally:
        try:
            ctx.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 时间解析（多格式 → datetime）
# ---------------------------------------------------------------------------

TIME_PATTERNS = [
    (r"(20\d{2})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?", (0, 1, 2, 3, 4)),   # 2026-10-04 15:20[:38]
    (r"(20\d{2})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})(?::(\d{2}))?", (0, 1, 2, 3, 4)),  # 2026年10月04日 15:20[:38]
    (r"(20\d{2})-(\d{1,2})-(\d{1,2})", (0, 1, 2)),                                            # 2026-10-04
    (r"(20\d{2})年(\d{1,2})月(\d{1,2})日", (0, 1, 2)),                                        # 2026年10月04日
    (r"(20\d{2})(\d{2})(\d{2})(?!\d)", (0, 1, 2)),                                            # 20261004（URL 日期）
]


def parse_time_any(text):
    """从任意文本解析发布时间，返回 datetime 或 None。"""
    if not text:
        return None
    text = str(text)
    for patt, groups in TIME_PATTERNS:
        m = re.search(patt, text)
        if m:
            try:
                vals = [int(m.group(g + 1)) for g in groups]
                y, mo, d = vals[0], vals[1], vals[2]
                h = vals[3] if len(vals) > 3 else 0
                mi = vals[4] if len(vals) > 4 else 0
                if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31 and h < 24 and mi < 60:
                    return dt.datetime(y, mo, d, h, mi)
            except Exception:
                continue
    # 时间戳（毫秒/秒）
    m = re.search(r"\b(1[6-9]\d{12})\b", text)
    if m:
        try:
            return dt.datetime.fromtimestamp(int(m.group(1)) / 1000)
        except Exception:
            pass
    return None


def url_date(url):
    """从 URL 中提取日期（央视网 /2026/10/04/、新华网 /20261004/、cq.gov t20261004_）。"""
    m = re.search(r"/(20\d{2})/(\d{2})/(\d{2})/", url)
    if m:
        try:
            return dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            return None
    m = re.search(r"/(20\d{2})(\d{2})(\d{2})/", url)
    if m:
        try:
            return dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            return None
    m = re.search(r"[t_](20\d{2})(\d{2})(\d{2})[_\.]", url)
    if m:
        try:
            return dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# 正文清洗 / 提取
# ---------------------------------------------------------------------------

DROP_PATTERNS = re.compile(
    r"^\s*(责任编辑|编辑|责编|作者单位|【纠错】|纠错|打印|字号|分享(?:到|至)|扫描|扫一扫|二维码|"
    r"相关阅读|推荐阅读|延伸阅读|延伸阅读|点击进入|上一篇|下一篇|返回(?:首页|顶部)|正在加载|"
    r"(?:免责|版权)声明|版权所有|ICP备|京公网安备|友情链接|评论\(|条评论|"
    r"更多推荐|热点推荐|猜你喜欢|为您推荐|广告|举报|新闻信息服务许可证).{0,80}$"
)


def html_to_text(el):
    """容器 → 段落式纯文本。"""
    if el is None:
        return ""
    for tag in el.find_all(["script", "style", "iframe", "noscript", "svg", "form",
                            "button", "input", "select", "video", "audio"]):
        tag.decompose()
    # 去掉短小的导航/推荐/版权类节点
    for node in el.find_all(["div", "p", "span", "li", "a", "h3", "h4", "section", "aside", "footer"]):
        t = node.get_text(" ", strip=True)
        if t and len(t) < 120 and (DROP_PATTERNS.match(t) or
                                   re.search(r"(责任编辑|【纠错】|友情链接|版权所有|ICP备|扫码|分享到|相关阅读|推荐阅读|打印本页|字号：)", t)):
            node.decompose()
    # 图片 → 占位说明
    for img in el.find_all("img"):
        alt = (img.get("alt") or "").strip()
        img.replace_with(f"[图：{alt}] " if alt else "")
    # 段落
    paras = []
    for p in el.find_all(["p", "div", "li", "h1", "h2", "h3"]):
        t = p.get_text(" ", strip=True)
        if t:
            paras.append(t)
    text = "\n".join(paras) if paras else el.get_text("\n", strip=True)
    # 清理连续空行与空白
    lines = [re.sub(r"[ \t\u3000]+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    out, prev = [], None
    for ln in lines:
        if ln != prev:
            out.append(ln)
        prev = ln
    return "\n".join(out)


SIGNATURE_LINE = re.compile(
    r"^(央视网|央视新闻|央视财经|新华社|人民日报|人民网|人民网-人民日报|新华网|半月谈|半月谈网|"
    r"中国新闻网|中新网|视觉中国|澎湃新闻|环球网|环球时报|解放日报|上观新闻)$")

NAV_LINE_DATE = re.compile(r"^\S{0,6}\s*20\d{2}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}(:\d{2})?\s*$")
NAV_LINE_PIPE = re.compile(r"^\S{0,12}\s*\|\s*20\d{2}年")
NAV_LINE_SOURCE = re.compile(r"^来源[:：]")


JUNK_INLINE = [
    re.compile(r"[\w\u4e00-\u9fa5]{0,8}(\s*[>＞]\s*[\w\u4e00-\u9fa5]{1,10})+"),      # 面包屑串：新闻频道 > 国际新闻
    re.compile(r"来源[:：]\s*\S{0,20}?\s*\|\s*20\d{2}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}(:\d{2})?"),
    re.compile(r"\S{0,12}\s*\|\s*20\d{2}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}(:\d{2})?"),   # 央视新闻 | 2026年10月04日 17:39
    re.compile(r"^[\w\u4e00-\u9fa5]{0,6}\s*20\d{2}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}(:\d{2})?\s*$"),
    re.compile(r"(最新推荐|加载更多|精彩图集|望海热线|热门推荐|相关推荐|[\w.+-]+@[\w.-]+\.cntv\.cn)"),
]

JUNK_MARKERS = re.compile(r"(加载更多|精彩图集|最新推荐|望海热线|版权所有)", re.I)


def clean_inline_junk(text):
    """整段文本的行内杂质清除（面包屑/来源行/时间戳行），适用于无换行的混排正文。"""
    if not text:
        return text
    for p in JUNK_INLINE:
        text = p.sub(" ", text)
    lines = [re.sub(r"[ \t\u3000]{2,}", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join([ln for ln in lines if ln])


def strip_nav_lines(text):
    """行级清洗：去掉面包屑（含 > 的短行）、带日期的来源行、独立日期行、纯署名行。"""
    if not text:
        return text
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        if len(s) < 60:
            if " > " in s or "＞" in s:                       # 面包屑：新闻频道 > 国际新闻
                continue
            if NAV_LINE_SOURCE.search(s) and re.search(r"20\d{2}", s):
                continue                                     # 来源：xxx | 2026年10月04日 …
            if NAV_LINE_PIPE.match(s):
                continue                                     # 央视新闻 | 2026年10月04日 17:39
            if NAV_LINE_DATE.match(s):
                continue                                     # 央视网 2026年10月02日 19:46
            if SIGNATURE_LINE.match(s):
                continue                                     # 纯署名行
        out.append(s)
    return "\n".join(out)


def cut_tail(text):
    """截掉正文尾部常见杂质（责任编辑/纠错/相关阅读之后的残留）。"""
    for mark in ("责任编辑", "【纠错】", "相关阅读", "推荐阅读", "延伸阅读", "点击进入专题",
                 "更多精彩", "（责编", "(责编", "原文链接", "【编辑:"):
        i = text.rfind(mark)
        if i > 0 and i > len(text) * 0.5:   # 只在真正的尾部裁剪
            text = text[:i]
    return text.strip()


TITLE_SPLITS = (" -- ", " --", "--人民网", "_新华网", "_央视网", "_新闻频道_央视网",
                "_重庆市人民政府网", "-半月谈", "-新华网", "_时政", "_网易", "-中新网")


def clean_title(t):
    if not t:
        return ""
    t = re.sub(r"\s+", " ", t).strip()
    for sp in TITLE_SPLITS:
        i = t.find(sp)
        if i > 8:
            t = t[:i]
            break
    return t.strip(" |　")


# 各站详情页选择器规则（按域名匹配；按顺序尝试）
DETAIL_RULES = [
    {"host": r"(news\.)?cctv\.com",
     "content": ["#content_area", "#contentarea", ".cnt_bd", "#page_body"],
     "time_sel": [".info", ".source", "#content_area .info"],
     "time_re": r"(20\d{2})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})(?::(\d{2}))?"},
    {"host": r"people\.com\.cn",
     "content": ["#rm_txt_zw", ".rm_txt_con", ".show_text", ".rm_txt_con p"],
     "time_sel": [".show-info", ".show_info", ".author"],
     "time_re": r"(20\d{2})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})(?::(\d{2}))?"},
    {"host": r"news\.cn",
     "content": ["#detail", ".detail", "#detailContent"],
     "time_sel": [".info", ".time", ".source"],
     "time_re": r"(20\d{2})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?"},
    {"host": r"banyuetan\.org",
     "content": ["#detail_content", ".detail_content", ".article-content"],
     "time_sel": [".detail_time", ".time", ".detail_top", ".source-time"],
     "time_re": r"(20\d{2})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?"},
    {"host": r"cq\.gov\.cn",
     "content": [".article-box", ".cwx-main", "#content-box1", ".common-main", "#zoom"],
     "time_sel": [".article-time", ".article-date", ".time", ".date"],
     "time_re": r"(20\d{2})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?"},
]

GENERIC_CONTENT_SELS = ["article", ".article", "#article", ".content", "#content",
                        ".main-content", ".text", ".TRS_Editor", ".pages_content"]


def extract_detail(url, html=None, prefer_text=None):
    """详情页提取：返回 (title, publish_time(datetime|None), content)。
    html 传 None 时自动静态抓取。prefer_text 为浏览器渲染文本兜底。"""
    if html is None:
        html = fetch_static(url, referer="https://www.xuexi.cn/")
    soup = BeautifulSoup(html, "lxml")
    host = urlparse(url).netloc

    # ---- 标题
    title = ""
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        title = og["content"].strip()
    if not title:
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            title = h1.get_text(strip=True)
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    title = clean_title(title)

    # ---- 规则匹配
    rule = None
    for r in DETAIL_RULES:
        if re.search(r["host"], host):
            rule = r
            break

    # ---- 时间
    pub = None
    if rule:
        for sel in rule["time_sel"]:
            for el in soup.select(sel)[:3]:
                pub = parse_time_any(el.get_text(" ", strip=True) if el.name != "meta" else (el.get("content") or ""))
                if pub:
                    break
            if pub:
                break
        if not pub:
            pub = parse_time_any_from_re(html, rule["time_re"])
    if not pub:
        # meta 兜底
        for m in soup.find_all("meta"):
            k = (m.get("name") or m.get("property") or m.get("itemprop") or "").lower()
            if any(x in k for x in ("time", "date", "publish")):
                pub = parse_time_any(m.get("content") or "")
                if pub:
                    break
    if not pub:
        pub = parse_time_any(soup.get_text(" ", strip=True)[:3000])
    if not pub:
        pub = url_date(url)

    # ---- 正文
    content = ""
    node = None
    if rule:
        for sel in rule["content"]:
            node = soup.select_one(sel)
            if node:
                content = html_to_text(node)
                if len(content) >= 40:
                    break
                node = None
    if not node:
        for sel in GENERIC_CONTENT_SELS:
            n2 = soup.select_one(sel)
            if n2:
                c2 = html_to_text(n2)
                if len(c2) > len(content):
                    content, node = c2, n2
        if len(content) < 40:
            # 通用兜底：取 <p> 文本量最大的容器
            best, best_len = None, 0
            for d in soup.find_all(["div", "section", "article"]):
                ps = d.find_all("p")
                ln = sum(len(p.get_text(strip=True)) for p in ps)
                if ln > best_len:
                    best, best_len = d, ln
            if best:
                c3 = html_to_text(best)
                if len(c3) > len(content):
                    content = c3
    if len(content) < 40 and prefer_text:
        content = prefer_text
    content = clean_inline_junk(content)
    content = strip_nav_lines(content)
    content = re.sub(r"原标题：", " ", content)
    # 判定为导航/推荐位杂烩（标题重复多次、含推荐位标记的短文本）→ 弃用，走 meta description
    if content and len(content) < 600:
        if JUNK_MARKERS.search(content) or (title and content.count(title[:20]) >= 2):
            content = ""
    if len(content) < 40:
        # meta description 兜底（央视网新版模板/视频页正文由 JS 注入，静态只有摘要可用）
        for md in soup.find_all("meta"):
            k = (md.get("name") or md.get("property") or "").lower()
            if k in ("description", "og:description"):
                c = (md.get("content") or "").strip()
                if len(c) > len(content):
                    content = c
    content = cut_tail(content)
    return title, pub, content


def parse_time_any_from_re(html, patt):
    m = re.search(patt, html or "")
    if not m:
        return None
    try:
        g = m.groups()
        y, mo, d = int(g[0]), int(g[1]), int(g[2])
        h = int(g[3]) if len(g) > 3 and g[3] else 0
        mi = int(g[4]) if len(g) > 4 and g[4] else 0
        return dt.datetime(y, mo, d, h, mi)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 摘要（抽取式，2~4 句，基于正文）与金句（来源 7）
# ---------------------------------------------------------------------------

SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])")


def split_sentences(text):
    if not text:
        return []
    sents = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        for s in SENT_SPLIT.split(ln):
            s = s.strip()
            if s:
                sents.append(s)
    return sents


def make_summary(content):
    """抽取式摘要：优先取前部、含关键数字、长度适中的 2~4 个原句。"""
    sents = [s for s in split_sentences(content) if 12 <= len(s) <= 130]
    if not sents:
        text = re.sub(r"\s+", "", content or "")
        return text[:110] + ("…" if len(text) > 110 else "")
    def score(i, s):
        sc = 0.0
        n = len(sents)
        sc += max(0.0, 1.5 * (1 - i / max(n, 1)))          # 越靠前越重要
        if re.search(r"\d", s):
            sc += 1.2                                       # 保留关键数字
        if re.search(r"[，、：]", s):
            sc += 0.3
        L = len(s)
        sc += 1.0 - abs(L - 60) / 90.0                      # 长度适中
        if re.search(r"(记者|报道|从.*获悉|日前|近日|今天|昨日)", s):
            sc += 0.4
        return sc
    k = 3 if len(sents) >= 8 else (2 if len(sents) >= 3 else len(sents))
    idxs = sorted(sorted(range(len(sents)), key=lambda i: -score(i, sents[i]))[:k])
    # 去掉相邻重复
    picked = []
    for i in idxs:
        if picked and sents[i][:18] == sents[picked[-1]][:18]:
            continue
        picked.append(i)
    return "".join(sents[i] for i in picked)


GOLDEN_HINT = re.compile(r"(强调|指出|必须|要\s|坚持|人民|初心|使命|复兴|伟大|团结|奋斗|青春|理想|信念|实干|改革|发展|安全|治国|治党|强军)")


def extract_golden_quotes(content, max_n=2):
    """来源 7 金句：原文完整句子，不改写不拼接，取 1~2 句。"""
    sents = [s.strip() for s in split_sentences(content)]
    cand = []
    for s in sents:
        if not (16 <= len(s) <= 120):
            continue
        if DROP_PATTERNS.match(s):
            continue
        sc = 0.0
        if re.search(r"[“\"「‘'].*[”\"」’']", s):
            sc += 2.0
        if GOLDEN_HINT.search(s):
            sc += 1.5
        if re.search(r"^习近平", s):
            sc += 0.5
        if len(s) >= 28:
            sc += 0.5
        if sc >= 2.0:
            cand.append((sc, s))
    cand.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for _, s in cand:
        key = s[:20]
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= max_n:
            break
    return out


# ---------------------------------------------------------------------------
# 来源 1：央视网「要闻」（Playwright 优先）
# ---------------------------------------------------------------------------

CCTV_URL = "https://www.cctv.com/"

CCTV_JS = r"""
() => {
  const spans = Array.from(document.querySelectorAll('span.title'));
  const node = spans.find(s => s.textContent.trim() === '要闻');
  if (!node) return [];
  const wrap = node.closest('.wrapper_1200') || node.closest('div');
  let box = wrap && wrap.nextElementSibling;
  if (!box) return [];
  const seen = new Set(); const out = [];
  box.querySelectorAll('a[href]').forEach(a => {
    const h = a.href;
    if (!/\/20\d{2}\/\d{2}\/\d{2}\/[A-Za-z0-9]+\.shtml/.test(h)) return;
    if (h.indexOf('tv.cctv.com') !== -1) return;      // 跳过视频页
    if (seen.has(h)) return;
    seen.add(h);
    const t = (a.getAttribute('title') || a.innerText || '').replace(/\s+/g, ' ').trim();
    out.push({url: h, title: t});
  });
  return out;
}
"""


def collect_cctv(limit):
    """返回 [{section,title,url}]。央视网首页要闻区为 JS 渲染，Playwright 优先。"""
    items, note = [], ""
    browser = _get_browser()
    if browser:
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900},
                                  locale="zh-CN")
        page = ctx.new_page()
        try:
            page.goto(CCTV_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(9000)
            items = page.evaluate(CCTV_JS)
        except Exception as e:
            note = f"页面内提取失败: {str(e)[:80]}"
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    if not items:
        # 静态兜底（首页要闻区为动态注入，静态通常为空）
        try:
            html2 = fetch_static(CCTV_URL, referer=CCTV_URL)
            soup = BeautifulSoup(html2, "lxml")
            for a in soup.select("a[href]"):
                h = a.get("href", "")
                if re.search(r"news\.cctv\.com/20\d{2}/\d{2}/\d{2}/[A-Za-z0-9]+\.shtml", h):
                    items.append({"url": urljoin("https://news.cctv.com/", h),
                                  "title": a.get_text(" ", strip=True)})
            note = note or "静态兜底提取"
        except Exception as e:
            note = (note + "；" if note else "") + f"静态兜底失败: {str(e)[:80]}"
    out = []
    for it in items[:limit]:
        out.append({"section": "要闻", "title": it.get("title", ""), "url": it["url"]})
    return out, note


# ---------------------------------------------------------------------------
# 来源 2：人民网「今日头条」+「今日要闻」（静态）
# ---------------------------------------------------------------------------

PEOPLE_URL = "http://www.people.com.cn/GB/59476/index.html"


def collect_people(limit):
    html = fetch_static(PEOPLE_URL, referer="http://www.people.com.cn/")
    # 按「今日头条」「今日要闻」标题在原文中的位置切片
    pos_toutiao = html.find("今日头条")
    pos_yaowen = html.find("今日要闻")
    out, seen = [], set()

    def links_between(a, b):
        seg = html[a:b if b > a else a + 60000]
        res = []
        for m in re.finditer(r'href="(https?://politics\.people\.com\.cn/n1/20\d{2}/\d{4}/c[\d\-]+\.html|'
                             r'https?://[a-z]+\.people\.com\.cn/n1/20\d{2}/\d{4}/c[\d\-]+\.html)"[^>]*>([^<]{6,80})', seg):
            u, t = m.group(1), re.sub(r"\s+", "", m.group(2))
            if u in seen:
                continue
            seen.add(u)
            res.append((u, t))
        return res

    if pos_toutiao >= 0:
        seg_end = pos_yaowen if pos_yaowen > pos_toutiao else pos_toutiao + 40000
        for u, t in links_between(pos_toutiao, seg_end)[:limit]:
            out.append({"section": "今日头条", "title": t, "url": u})
    if pos_yaowen >= 0:
        for u, t in links_between(pos_yaowen, pos_yaowen + 50000):
            if len(out) >= limit:
                break
            out.append({"section": "今日要闻", "title": t, "url": u})
    return out, ""


# ---------------------------------------------------------------------------
# 来源 3：学习强国「头条新闻」（Playwright 优先 → lgdata JSON；72 小时）
# ---------------------------------------------------------------------------

XXQG_HEADLINE_PAGE = "https://www.xuexi.cn/72ac54163d26d6677a80b8e21a776cfa/9a3668c13f6e303932b5e0e100fc248b.html"
XXQG_HEADLINE_JSON = "https://www.xuexi.cn/lgdata/1crqb964p71.json"   # 头条新闻频道数据


def xuexi_list_items(page_url, json_url, json_pattern, wait_ms=9000):
    """Playwright 渲染页面并拦截 lgdata JSON；失败则直接请求 JSON。返回 list[dict]。"""
    _, captured = render_page(page_url, wait_ms=wait_ms, json_pattern=json_pattern,
                              referer="https://www.xuexi.cn/")
    for body in captured:
        try:
            data = json.loads(body)
            if isinstance(data, list) and data:
                return data
        except Exception:
            continue
    # 兜底：直接请求 JSON 接口
    try:
        data = fetch_json(json_url)
        if isinstance(data, list) and data:
            return data
    except Exception:
        pass
    return []


def collect_xuexi_headline(limit):
    cutoff = dt.datetime.now() - HOURS_72
    items = xuexi_list_items(XXQG_HEADLINE_PAGE, XXQG_HEADLINE_JSON,
                             r"lgdata/1crqb964p71\.json")
    out, candidates = [], 0
    newest = ""
    for it in items:
        try:
            if str(it.get("dataValid")) not in ("True", "true", "1"):
                continue
            if "头条新闻" not in str(it.get("channelNames") or ""):
                continue
            candidates += 1
            if not newest:
                newest = str(it.get("publishTime") or "")
            pub = parse_time_any(it.get("publishTime") or "")
            if not pub or pub < cutoff:      # 仅保留最近 72 小时
                continue
            out.append({"section": "头条新闻", "title": (it.get("title") or "").strip(),
                        "url": (it.get("url") or "").strip(), "_pub": pub})
            if len(out) >= limit:
                break
        except Exception:
            continue
    note = ""
    if not out:
        note = f"72小时内无新文章（站点最新：{newest or '未知'}），按规则不保留"
    return out, note


# ---------------------------------------------------------------------------
# 来源 4：新华网「时政关注」（Playwright 优先，静态兜底）
# ---------------------------------------------------------------------------

XINHUA_URL = "https://www.news.cn/politics/index.html"


def collect_xinhua(limit):
    items, note = [], ""
    html, _ = render_page(XINHUA_URL, wait_ms=8000, referer="https://www.news.cn/")
    seen = set()
    if html:
        soup = BeautifulSoup(html, "lxml")
        for a in soup.select("a[href]"):
            h = a.get("href", "")
            m = re.match(r"https?://www\.news\.cn/politics/20\d{6}/[0-9a-f]+/[ch]\.html", h)
            if m and h not in seen:
                seen.add(h)
                t = a.get_text(" ", strip=True)
                items.append({"url": h, "title": t})
    if len(items) < 5:
        note = "浏览器渲染不足，静态兜底"
        html2 = fetch_static(XINHUA_URL, referer="https://www.news.cn/")
        soup = BeautifulSoup(html2, "lxml")
        for a in soup.select("a[href]"):
            h = urljoin(XINHUA_URL, a.get("href", ""))
            m = re.match(r"https?://www\.news\.cn/politics/20\d{6}/[0-9a-f]+/[ch]\.html", h)
            if m and h not in seen:
                seen.add(h)
                items.append({"url": h, "title": a.get_text(" ", strip=True)})
    out = []
    for it in items[:limit]:
        out.append({"section": "时政关注", "title": it.get("title", ""), "url": it["url"]})
    return out, note


# ---------------------------------------------------------------------------
# 来源 5：半月谈「要闻top10」（静态，ul.title2_box 固定 10 条）
# ---------------------------------------------------------------------------

BANYUETAN_URL = "http://www.banyuetan.org/byt/jinritan/index.html"


def collect_banyuetan(limit):
    html = fetch_static(BANYUETAN_URL, referer="http://www.banyuetan.org/")
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    # 定位「要闻top10」标题块 → 其后的 ul.title2_box
    box = soup.select_one("ul.title2_box")
    if box is None:
        for tit in soup.find_all(["div", "span", "a"]):
            if "要闻top10" in tit.get_text():
                box = tit.find_parent().find_next("ul")
                break
    if box:
        for a in box.select("a[href]"):
            h = urljoin(BANYUETAN_URL, a.get("href", ""))
            if not re.search(r"banyuetan\.org/\w+/detail/20\d{6}/", h) or h in seen:
                continue
            seen.add(h)
            t = re.sub(r"[.\s…]+$", "", a.get_text(strip=True))   # 去掉截断省略号
            out.append({"section": "要闻top10", "title": t, "url": h})
    return out[:limit], ("" if box else "未定位到要闻top10容器")


# ---------------------------------------------------------------------------
# 来源 6：今日重庆「今日重庆」（静态，li+日期；72 小时）
# ---------------------------------------------------------------------------

JRCQ_URL = "https://www.cq.gov.cn/ywdt/jrcq/index.html"


def collect_jrcq(limit):
    html = fetch_static(JRCQ_URL, referer="https://www.cq.gov.cn/")
    soup = BeautifulSoup(html, "lxml")
    cutoff = dt.datetime.now() - HOURS_72
    slack = dt.timedelta(hours=24)     # 列表页只有日期（无时分），预筛放宽 24h，详情页再精确
    out = []
    for li in soup.find_all("li"):
        a = li.find("a", href=True)
        if not a:
            continue
        m = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", li.get_text(" ", strip=True))
        if not m:
            continue
        try:
            d = dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            continue
        if d < cutoff - slack:
            continue
        h = urljoin(JRCQ_URL, a["href"])
        if "cq.gov.cn" not in h:
            continue
        t = re.sub(r"20\d{2}-\d{1,2}-\d{1,2}\s*$", "", a.get_text(" ", strip=True)).strip()
        out.append({"section": "今日重庆", "title": t, "url": h, "_pub_list": d})
        if len(out) >= limit + 8:
            break
    return out, ""


# ---------------------------------------------------------------------------
# 来源 7：学习强国「习近平文汇」+「学习重点」（数据含全部分页；金句）
# ---------------------------------------------------------------------------

XXQG_WENHUI_PAGE = "https://www.xuexi.cn/5c90534c80d14c060d6683fa960e3676/82573c005c024095037d2186a02244cb.html"
XXQG_WENHUI_JSON = "https://www.xuexi.cn/lgdata/5c90534c80d14c060d6683fa960e3676/82573c005c024095037d2186a02244cb.json"

# 来源 8：学习强国六个板块（与来源 3/7 同页面体系；频道 ID 来自页面网络请求）
XXQG_SIX_CHANNELS = {
    "重要活动": ["1jpuhp6fn73"],
    "重要会议": ["19vhj0omh73"],
    "重要讲话": ["132gdqo7l73"],
    "重要文章": ["1ahjpjgb4n3"],
    "指示批示": ["1kvrj9vvv73"],
    "函电贺词": ["17qonfb74n3"],
}
XXQG_SIX_SINCE = dt.datetime(2026, 1, 1)   # 来源 8 时间范围：2026-01-01 至今
XXQG_WENHUI_SINCE = dt.datetime(2026, 1, 1)  # 来源 7 习近平文汇：最新年卷发布时间 ≥ 2026-01-01


def xuexi_page_data_items(pid1, pid2):
    """用户提示的 URL 变形技巧：{pid1}/data{pid2}.js → globalCache 数据。
    返回 (模块名, 列表) 列表；失败返回 []。实测该缓存为站点旧数据（可能滞后），仅作首选尝试。"""
    url = f"https://www.xuexi.cn/{pid1}/data{pid2}.js"
    try:
        t = fetch_static(url, referer="https://www.xuexi.cn/")
        m = re.search(r"globalCache\s*=\s*(\{.*\})\s*;?\s*$", t.strip(), re.S)
        if not m:
            return []
        gc = json.loads(m.group(1))
        return [(mid, (mod or {}).get("list") or []) for mid, mod in gc.items()
                if isinstance(mod, dict) and mod.get("list")]
    except Exception:
        return []


def collect_xuexi_six(limit):
    """来源 8：重要活动/重要会议/重要讲话/重要文章/指示批示/函电贺词。
    按用户降级阶梯：① 页面 data JS 变形 → ② lgdata/{频道ID}.json → ③ Playwright 渲染兜底。
    列表层先筛 publishTime >= 2026-01-01，再进详情，避免无效请求。"""
    out, seen_item = [], set()
    note_parts = []
    # ① 页面 data JS（实测为 2017-2019 旧缓存且不含六板块，命中即用，否则走 lgdata）
    page_items = xuexi_page_data_items("72ac54163d26d6677a80b8e21a776cfa",
                                       "9a3668c13f6e303932b5e0e100fc248b")
    datajs_by_section = {}
    for _mid, lst in page_items:
        for it in lst:
            try:
                cates = set()
                for f in ("cate_id", "programa_id"):
                    v = it.get(f)
                    if isinstance(v, str) and v.startswith("["):
                        cates |= set(json.loads(v))
                    elif v:
                        cates.add(str(v))
            except Exception:
                continue
            for sec in XXQG_SIX_CHANNELS:
                if sec in cates:
                    datajs_by_section.setdefault(sec, []).append(it)
    # ② lgdata 频道 JSON（主数据源，参数取自页面网络请求）
    for section, ids in XXQG_SIX_CHANNELS.items():
        n_sec = 0
        merged = list(datajs_by_section.get(section, []))
        for cid in ids:
            try:
                merged.extend(fetch_json(f"https://www.xuexi.cn/lgdata/{cid}.json") or [])
            except Exception as e:
                note_parts.append(f"{section} lgdata/{cid} 失败")
                continue
        for it in merged:
            iid = str(it.get("itemId") or it.get("_id") or "")
            u = (it.get("url") or it.get("static_page_url") or "").strip()
            if not u or (iid and iid in seen_item) or norm_url(u) in {x.get("_nu") for x in out}:
                continue
            pub = parse_time_any(it.get("publishTime") or it.get("original_time") or "")
            if not pub or pub < XXQG_SIX_SINCE:      # 仅保留 2026-01-01 至今
                continue
            seen_item.add(iid)
            out.append({"section": section, "title": (it.get("title") or it.get("frst_name") or "").strip(),
                        "url": u, "_pub": pub, "_nu": norm_url(u)})
            n_sec += 1
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    note = "；".join(note_parts)
    # ③ lgdata 全失败时 Playwright 渲染兜底（页面会加载 35il6fpn0ohq.json——六板块数据全集）
    if not out:
        _, captured = render_page("https://www.xuexi.cn/72ac54163d26d6677a80b8e21a776cfa/9a3668c13f6e303932b5e0e100fc248b.html",
                                  wait_ms=9000, json_pattern=r"lgdata/(35il6fpn0ohq|1jpuhp6fn73|19vhj0omh73|132gdqo7l73|1ahjpjgb4n3|1kvrj9vvv73|17qonfb74n3)\.json",
                                  referer="https://www.xuexi.cn/")
        for body in captured:
            try:
                data = json.loads(body)
            except Exception:
                continue
            if not isinstance(data, list):
                continue
            for it in data:
                names = it.get("channelNames")
                if isinstance(names, str):
                    try:
                        import ast as _ast
                        names = _ast.literal_eval(names)
                    except Exception:
                        names = []
                sec = next((s for s in XXQG_SIX_CHANNELS if s in [str(x) for x in (names or [])]), None)
                if not sec:
                    continue
                pub = parse_time_any(it.get("publishTime") or "")
                if not pub or pub < XXQG_SIX_SINCE:
                    continue
                u = (it.get("url") or "").strip()
                if not u or norm_url(u) in {x.get("_nu") for x in out}:
                    continue
                out.append({"section": sec, "title": (it.get("title") or "").strip(),
                            "url": u, "_pub": pub, "_nu": norm_url(u)})
                if len(out) >= limit:
                    break
            if len(out) >= limit:
                break
        if not out:
            note = (note + "；" if note else "") + "lgdata 失败，已降级 Playwright 渲染"
    return out, note


def collect_xuexi_wenhui(limit):
    """页面数据 JSON 内嵌全部 59 节（分页仅为前端 UI，JSON 全量）。
    「习近平文汇」只保留「最新年卷 publishTime >= 2026-01-01」的节（不再全量抓取）；
    「学习重点」保持原全量扫描逻辑（当前页面未发现该板块可见条目）。
    返回节列表（含 _vol=最新年卷，供 enrich 复用，避免详情阶段重复请求）；
    每节详情在 enrich 阶段取「最新年卷」正文（金句来源）。"""
    html, captured = render_page(XXQG_WENHUI_PAGE, wait_ms=8000,
                                 json_pattern=r"lgdata/5c90534c80d14c060d6683fa960e3676/82573c005c024095037d2186a02244cb\.json",
                                 referer="https://www.xuexi.cn/")
    data_text = None
    for body in captured:
        if "xxqg.html?id=" in body:
            data_text = body
            break
    if data_text is None:
        try:
            data_text = fetch_static(XXQG_WENHUI_JSON, referer="https://www.xuexi.cn/")
        except Exception:
            data_text = html or ""
    pairs = re.findall(r'"link":"(https://www\.xuexi\.cn/xxqg\.html\?id=[^"]+)","text":"([^"]+)"',
                       data_text or "")
    # 渲染后的 DOM 兜底（text-link-item）
    if not pairs and html:
        soup = BeautifulSoup(html, "lxml")
        for a in soup.select("a[href*='xxqg.html?id=']"):
            pairs.append((a["href"], a.get_text(strip=True)))
    seen, out, skipped = set(), [], 0
    for link, text in pairs:
        if link in seen:
            continue
        seen.add(link)
        text = text.strip()
        m = re.match(r"^(\d{3})(.+)$", text)
        title = (m.group(1) + " " + m.group(2)) if m else text
        item = {"section": "习近平文汇", "title": title, "url": link, "_qid": link.split("id=")[-1]}
        # 「习近平文汇」2026 筛选：取该节最新年卷（lgdata JSON 首条），
        # publishTime >= 2026-01-01 才保留；JSON 全失败时渲染节页面提取首个日期兜底
        vol = xuexi_section_latest_volume(item["_qid"]) if item["_qid"] else None
        pub = parse_time_any((vol or {}).get("publishTime") or "")
        if pub is None:
            pub = xuexi_section_date_from_page(link)
        if not pub or pub < XXQG_WENHUI_SINCE:
            skipped += 1
            continue
        if vol is not None:
            item["_vol"] = vol                # 复用最新年卷，enrich 不再重复请求
        else:
            item["_skip_vol_lookup"] = True   # JSON 已确认不可用，enrich 阶段不再重试
        out.append(item)
        if len(out) >= limit:
            break
    note = "" if not skipped else f"2026 筛选：跳过 {skipped} 个最新年卷早于 2026-01-01 的节"
    return out, note


def xuexi_section_latest_volume(qid):
    """文汇节 → 最新年卷 item（lgdata/{频道ID}.json 首条）。
    节 ID 有三种形态：短 ID 重复 2~3 次（如 u9l6qkucn4×3、17m502ocin4×2.5）或 32 位 hex，
    频道 JSON 的 key 为其中的「重复单元」或 hex 本身——按周期检测 + 前缀候选依次尝试。"""
    cands = []
    n = len(qid)
    for p in range(6, 14):                       # 周期检测：qid 是否由前 p 位重复构成
        unit = qid[:p]
        if n // p >= 2 and (unit * (n // p + 1))[:n] == qid:
            cands.append(unit)
            break
    for p in (10, 11, 12):
        if len(qid) > p:
            cands.append(qid[:p])
    cands.append(qid)
    seen = set()
    for short in cands:
        if short in seen:
            continue
        seen.add(short)
        try:
            data = fetch_json(f"https://www.xuexi.cn/lgdata/{short}.json")
            if isinstance(data, list) and data and (data[0].get("publishTime") or data[0].get("url")):
                return data[0]
        except Exception:
            continue
    return None


def xuexi_section_date_from_page(url):
    """文汇节 lgdata JSON 全失败时：渲染节页面，从正文提取首个日期作 2026 判断。
    提取不到日期返回 None（视为不满足「2026-01-01 至今」，宁缺毋滥）。"""
    try:
        txt = render_page_text(url, wait_ms=6000, referer="https://www.xuexi.cn/")
    except Exception:
        return None
    m = re.search(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})", txt or "")
    if not m:
        return None
    try:
        return dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 详情富化（进入详情页取 title/publish_time/content + 摘要/金句）
# ---------------------------------------------------------------------------

def xuexi_item_id(url):
    m = re.search(r"[?&]item_id=(\d+)", url) or re.search(r"[?&]id=(\d+)", url)
    return m.group(1) if m else None


def enrich_xuexi(item, src_name):
    """学习强国详情：boot-source JSONP 接口（快），失败则 Playwright 渲染兜底。"""
    url = item["url"]
    iid = xuexi_item_id(url) or xuexi_item_id(item.get("_qurl", ""))
    content, pub, title = "", None, item.get("title", "")
    if iid:
        try:
            data = fetch_json(f"https://boot-source.xuexi.cn/data/app/{iid}.js?callback=callback")
            title = (data.get("title") or title).strip()
            content = html_to_text(BeautifulSoup(data.get("content") or "", "lxml"))
            if not content:
                content = (data.get("normalized_content") or "").strip()
            pub = parse_time_any(data.get("publish_time") or "")
        except Exception:
            pass
    if len(content) < 40:
        txt = render_page_text(url, wait_ms=6000, referer="https://www.xuexi.cn/")
        if txt:
            # 去掉站点导航与页脚
            lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
            keep = []
            for ln in lines:
                if re.match(r"^(思\s*想|二十大时间|习近平文汇|学习理论|红色中国|学习科学|国\s*际|五个一工程|学习电视台|学习电台|强军兴军|学习文化|学习强国)", ln):
                    continue
                if re.search(r"(友情链接|服务电话|版权所有|ICP备|互联网新闻信息服务许可证)", ln):
                    break
                keep.append(ln)
            content = "\n".join(keep)
    return title, pub, content


def enrich_common(item, src_name):
    """通用详情：静态抓取 + 规则提取；内容过短时 Playwright 渲染兜底。"""
    url = item["url"]
    try:
        html = fetch_static(url)
    except Exception:
        html = ""
    title, pub, content = extract_detail(url, html=html or None)
    if len(content) < 40:
        txt = render_page_text(url, wait_ms=6000)
        if txt:
            _, _, content2 = extract_detail(url, html=None, prefer_text=txt)
            if len(content2) > len(content):
                content = content2
    if not title:
        title = item.get("title", "")
    return title, pub, content


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

FAST = False


def run(limit=None, out_path=None, only=None):
    """only: None=全量采集全部来源；set(key)=--only 合并模式，只重采指定来源，
    其余来源沿用现有 JSON 数据（不重跑）。"""
    global FAST
    started = dt.datetime.now()
    articles, seen_urls = [], set()
    sources_status = []

    # --only 合并模式：加载现有 JSON，保留未重采来源的 articles/sources_status
    if only:
        out_path = out_path or OUTPUT_PATH
        redo = {KEY2NAME[k] for k in only}
        try:
            with open(out_path, encoding="utf-8") as f:
                old = json.load(f)
            articles = [a for a in old.get("articles", []) if a.get("source") not in redo]
            sources_status = [s for s in old.get("sources_status", []) if s.get("source") not in redo]
            for a in articles:
                u = norm_url(a.get("url", ""))
                if u:
                    seen_urls.add(u)
            log(f"--only 合并模式：保留其它来源 {len(articles)} 篇既有数据，本次重采 {sorted(redo)}")
        except FileNotFoundError:
            log(f"--only 合并模式：未找到现有 {out_path}，仅采集 {sorted(redo)}")
        except Exception as e:
            log(f"--only 合并模式：读取现有 JSON 失败（{str(e)[:80]}），仅采集 {sorted(redo)}")

    def add_articles(src_name, raw_items, enrich_fn, status_note=""):
        """raw_items: [{section,title,url,( extras )}] → 富化 → 去重 → 追加。"""
        count = 0
        if not raw_items:
            # 有 note 说明是按规则正常过滤为空（如 72 小时窗口），不算失败
            sources_status.append({"source": src_name,
                                   "status": "ok" if status_note else "failed",
                                   "count": 0, "error": "" if status_note else "列表为空",
                                   "note": status_note})
            return
        try:
            for it in raw_items:
                u = norm_url(it["url"])
                if not u or u in seen_urls:
                    continue
                seen_urls.add(u)
                try:
                    title, pub, content = enrich_fn(it, src_name)
                except Exception as e:
                    log(f"  [{src_name}] 详情失败 {it['url'][:70]}: {str(e)[:80]}")
                    continue
                if not title:
                    title = it.get("title", "")
                # 72h 过滤（来源 3、6）
                if src_name == "学习强国·头条新闻":
                    p = pub or it.get("_pub")
                    if not p or p < dt.datetime.now() - HOURS_72:
                        continue
                    pub = p
                if src_name == "今日重庆":
                    p = pub or it.get("_pub_list")
                    if not p or p < dt.datetime.now() - HOURS_72:
                        continue
                    pub = p
                golden = []
                if src_name == "学习强国·习近平文汇":
                    golden = extract_golden_quotes(content)
                articles.append({
                    "source": src_name,
                    "section": it.get("section", ""),
                    "title": title,
                    "url": it["url"],
                    "publish_time": fmt_time(pub),
                    "summary": make_summary(content),
                    "content": content,
                    "golden_quotes": golden,
                })
                count += 1
                if limit and count >= limit:
                    break
            sources_status.append({"source": src_name, "status": "ok", "count": count,
                                   "error": "", "note": status_note})
        except Exception as e:
            sources_status.append({"source": src_name, "status": "failed",
                                   "count": count, "error": str(e)[:160],
                                   "note": traceback.format_exc()[-200:]})

    def should_run(key):
        """--only 模式下只跑指定来源；None 表示全部来源都跑。"""
        return only is None or key in only

    caps = dict(CAPS)
    if limit:
        for k in caps:
            caps[k] = min(caps[k], limit)

    # ---- 来源 1：央视网
    if should_run("cctv"):
        log("来源1 央视网·要闻 采集中…")
        try:
            items, note = collect_cctv(caps["cctv"])
            add_articles("央视网·要闻", items, enrich_common, note)
        except Exception as e:
            sources_status.append({"source": "央视网·要闻", "status": "failed", "count": 0,
                                   "error": str(e)[:160], "note": ""})

    # ---- 来源 2：人民网
    if should_run("people"):
        log("来源2 人民网·今日头条+今日要闻 采集中…")
        try:
            items, note = collect_people(caps["people"])
            add_articles("人民网", items, enrich_common, note)
        except Exception as e:
            sources_status.append({"source": "人民网", "status": "failed", "count": 0,
                                   "error": str(e)[:160], "note": ""})

    # ---- 来源 3：学习强国·头条新闻
    if should_run("xuexi_headline"):
        log("来源3 学习强国·头条新闻 采集中…")
        try:
            items, note = collect_xuexi_headline(caps["xuexi_headline"])
            add_articles("学习强国·头条新闻", items, enrich_xuexi, note)
        except Exception as e:
            sources_status.append({"source": "学习强国·头条新闻", "status": "failed", "count": 0,
                                   "error": str(e)[:160], "note": ""})

    # ---- 来源 4：新华网·时政关注
    if should_run("xinhua"):
        log("来源4 新华网·时政关注 采集中…")
        try:
            items, note = collect_xinhua(caps["xinhua"])
            add_articles("新华网·时政关注", items, enrich_common, note)
        except Exception as e:
            sources_status.append({"source": "新华网·时政关注", "status": "failed", "count": 0,
                                   "error": str(e)[:160], "note": ""})

    # ---- 来源 5：半月谈·要闻top10
    if should_run("banyuetan"):
        log("来源5 半月谈·要闻top10 采集中…")
        try:
            items, note = collect_banyuetan(caps["banyuetan"])
            add_articles("半月谈·要闻top10", items, enrich_common, note)
        except Exception as e:
            sources_status.append({"source": "半月谈·要闻top10", "status": "failed", "count": 0,
                                   "error": str(e)[:160], "note": ""})

    # ---- 来源 6：今日重庆
    if should_run("jrcq"):
        log("来源6 今日重庆 采集中…")
        try:
            items, note = collect_jrcq(caps["jrcq"])
            add_articles("今日重庆", items, enrich_common, note)
        except Exception as e:
            sources_status.append({"source": "今日重庆", "status": "failed", "count": 0,
                                   "error": str(e)[:160], "note": ""})

    # ---- 来源 7：学习强国·习近平文汇（2026-01-01 以来更新的节）+ 学习重点
    if should_run("xuexi_wenhui"):
        log("来源7 学习强国·习近平文汇 采集中…")

        def enrich_wenhui(item, src_name):
            qid = item.get("_qid", "")
            vol = item.get("_vol")
            if vol is None and not item.get("_skip_vol_lookup"):
                # collect 阶段未取到（异常兜底路径），再试一次 lgdata
                vol = xuexi_section_latest_volume(qid) if qid else None
            if vol:
                vurl = vol.get("url", "")
                viid = xuexi_item_id(vurl)
                content, pub = "", parse_time_any(vol.get("publishTime") or "")
                if viid:
                    try:
                        data = fetch_json(f"https://boot-source.xuexi.cn/data/app/{viid}.js?callback=callback")
                        content = html_to_text(BeautifulSoup(data.get("content") or "", "lxml"))
                        pub = pub or parse_time_any(data.get("publish_time") or "")
                    except Exception:
                        pass
                if len(content) < 40:
                    content = render_page_text(vurl or item["url"], wait_ms=6000, referer="https://www.xuexi.cn/")
                # 清理文汇正文中内嵌的编号（如 VW001.0 01 .202 60120 .00 1）
                content = re.sub(r"VW[\d\s.]{5,}", " ", content)
                content = re.sub(r"\n{2,}", "\n", content).strip()
                return item["title"], pub, content
            # 兜底：直接渲染节页面（顺便提取首个日期作 publish_time，避免为空）
            txt = render_page_text(item["url"], wait_ms=6000, referer="https://www.xuexi.cn/")
            pub = None
            m = re.search(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})", txt or "")
            if m:
                try:
                    pub = dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                except ValueError:
                    pub = None
            # 清理文汇正文中内嵌的编号（如 VW001.0 01 .202 60120 .00 1）
            txt = re.sub(r"VW[\d\s.]{5,}", " ", txt or "")
            txt = re.sub(r"\n{2,}", "\n", txt).strip()
            return item["title"], pub, txt

        try:
            items, note = collect_xuexi_wenhui(caps["xuexi_wenhui"])
            note = ("「习近平文汇」仅保留最新年卷发布时间 ≥ 2026-01-01 的节；"
                    "「学习重点」保持原全量逻辑（页面当前未发现该板块链接）；数据含全部分页") \
                + (f"；{note}" if note else "")
            add_articles("学习强国·习近平文汇", items, enrich_wenhui, note)
        except Exception as e:
            sources_status.append({"source": "学习强国·习近平文汇", "status": "failed", "count": 0,
                                   "error": str(e)[:160], "note": ""})

    # ---- 来源 8：学习强国·六个板块（重要活动/会议/讲话/文章/指示批示/函电贺词，2026-01-01 至今）
    if should_run("xuexi_six"):
        log("来源8 学习强国·六个板块 采集中…")
        try:
            items, note = collect_xuexi_six(caps["xuexi_six"])
            add_articles("学习强国", items, enrich_xuexi, note)
        except Exception as e:
            sources_status.append({"source": "学习强国", "status": "failed", "count": 0,
                                   "error": str(e)[:160], "note": ""})

    close_browser()

    # ---- 输出
    result = {
        "updated_at": now_str(),
        "articles": articles,
        "sources_status": sources_status,
        "meta": {
            "elapsed_seconds": int((dt.datetime.now() - started).total_seconds()),
            "total": len(articles),
        },
    }
    if only:
        result["meta"]["only"] = sorted(only)
    out_path = out_path or OUTPUT_PATH
    tmp = out_path + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    os.replace(tmp, out_path)

    # 同步产出 workbench_data.js（精简版，去正文 content）：
    # 放在上岸打卡系统 HTML 同目录，双击打开（file://）时 <script src> 即可注入 window.WB_DATA
    try:
        js_data = {
            "updated_at": result["updated_at"],
            "articles": [{k: a.get(k, "") for k in
                          ("source", "section", "title", "url", "publish_time", "summary", "golden_quotes")}
                         for a in result["articles"]],
        }
        js_path = os.path.splitext(out_path)[0] + ".js"
        tmp_js = js_path + ".tmp"
        with open(tmp_js, "w", encoding="utf-8") as f:
            f.write("/* 本文件由 collector.py 自动生成（JSON 精简版），"
                    "供上岸打卡系统 HTML 以 <script src=\"workbench_data.js\"> 方式读取。请勿手改。 */\n")
            f.write("window.WB_DATA = ")
            f.write(json.dumps(js_data, ensure_ascii=False))
            f.write(";\n")
        os.replace(tmp_js, js_path)
        log(f"已同步生成浏览器数据文件 → {js_path}")
    except Exception as e:
        log(f"workbench_data.js 生成失败（不影响 JSON）: {str(e)[:120]}")

    log("=" * 60)
    for s in sources_status:
        log(f"  {s['source']:<18} {s['status']:<7} {s['count']:>3} 篇  {s.get('error','') or s.get('note','')}")
    log(f"共采集 {len(articles)} 篇 → {out_path}")
    return result


def serve(addr="127.0.0.1:8765"):
    """本地数据服务：供上岸打卡系统 HTML 一键调用采集。
    端点（均带 CORS 头，支持 file:// 页面 fetch）：
      GET /status    → {running, updated_at, total, log: 最近日志}
      GET /data.json → 返回当前 workbench_data.json
      GET /collect   → 后台线程触发全量采集（带锁防重入；可选 ?limit=N ?only=7,8 调试参数）
    启动：python collector.py --serve            （默认 127.0.0.1:8765）
          python collector.py --serve 0.0.0.0:9000"""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    host, _, port = addr.partition(":")
    port = int(port or 8765)
    state = {"running": False, "lock": threading.Lock()}

    def worker(only=None, limit=None):
        try:
            run(limit=limit, out_path=OUTPUT_PATH, only=only)
        except Exception as e:
            log(f"采集线程异常: {str(e)[:200]}")
            log(traceback.format_exc()[-300:])
        finally:
            state["running"] = False

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qsl(u.query)
            if u.path == "/status":
                total = 0
                updated_at = ""
                try:
                    with open(OUTPUT_PATH, encoding="utf-8") as f:
                        old = json.load(f)
                    total = old.get("meta", {}).get("total", len(old.get("articles", [])))
                    updated_at = old.get("updated_at", "")
                except Exception:
                    pass
                self._send(200, json.dumps({
                    "running": state["running"], "updated_at": updated_at,
                    "total": total, "log": LOG_LINES[-25:],
                }, ensure_ascii=False))
            elif u.path == "/data.json":
                try:
                    with open(OUTPUT_PATH, encoding="utf-8") as f:
                        self._send(200, f.read())
                except FileNotFoundError:
                    self._send(404, json.dumps({"error": "workbench_data.json 不存在，先运行一次采集"},
                                               ensure_ascii=False))
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)[:160]}, ensure_ascii=False))
            elif u.path == "/collect":
                if state["running"]:
                    self._send(200, json.dumps({"running": True, "note": "采集已在进行中，请轮询 /status"},
                                               ensure_ascii=False))
                    return
                state["running"] = True
                only = None
                limit = None
                for k, v in qs:
                    if k == "only" and v.strip():
                        only = set()
                        for part in re.split(r"[,\s]+", v.strip()):
                            kk = ONLY_MAP.get(part)
                            if kk:
                                only.add(kk)
                    elif k == "limit":
                        try:
                            limit = int(v)
                        except ValueError:
                            limit = None
                threading.Thread(target=worker, args=(only, limit), daemon=True).start()
                log(f"收到 /collect 请求（only={sorted(only) if only else '全部来源'}, limit={limit}），后台采集已启动")
                self._send(200, json.dumps({
                    "started": True,
                    "note": "全量约 15~25 分钟，请轮询 /status 至 running=false 后拉取 /data.json",
                }, ensure_ascii=False))
            else:
                self._send(404, json.dumps({"error": "not found"}, ensure_ascii=False))

        def log_message(self, *args):   # 静默访问日志
            pass

    log(f"本地数据服务已启动: http://{host}:{port}  （/status /data.json /collect）")
    log("请保持本窗口开启；上岸打卡系统 HTML 的「立即爬取」按钮会自动连接此服务")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def main():
    global FAST
    ap = argparse.ArgumentParser(description="上岸打卡 · 工作台数据采集")
    ap.add_argument("-o", "--output", default=None, help="输出 JSON 路径（默认脚本同目录 workbench_data.json）")
    ap.add_argument("--limit", type=int, default=None, help="每个来源最多采集 N 篇（调试用）")
    ap.add_argument("--fast", action="store_true", help="缩短请求间隔（调试用）")
    ap.add_argument("--serve", nargs="?", const="127.0.0.1:8765", default=None,
                    help="启动本地数据服务（供上岸打卡系统 HTML 一键爬取），可选地址 如 --serve 127.0.0.1:8765")
    ap.add_argument("--only", default=None,
                    help="只重采指定来源并合并其余来源现有数据（逗号分隔编号 1-8，如 --only 7,8；"
                         "1央视网 2人民网 3学习强国头条 4新华网 5半月谈 6今日重庆 "
                         "7学习强国文汇 8学习强国六板块）")
    # 【新增】自定义输出文件名前缀：不传 = 沿用原有默认名 workbench_data
    #   例：python collector.py --out-name=我的数据   → 我的数据.json + 我的数据.js
    #   注意：若同时指定了 -o/--output，以 -o 的路径为准，本参数不生效。
    ap.add_argument("--out-name", dest="out_name", default=None,
                    help="自定义输出文件名前缀（默认 workbench_data；会输出 <前缀>.json 与 <前缀>.js）")
    args = ap.parse_args()
    FAST = args.fast
    if args.serve:
        serve(args.serve)          # 本地数据服务模式（常驻，不执行一次性采集）
        return
    only = None
    if args.only:
        only = set()
        for part in re.split(r"[,\s]+", args.only.strip()):
            if not part:
                continue
            k = ONLY_MAP.get(part)
            if not k:
                ap.error(f"--only 未知来源「{part}」，可选编号 1-8")
            only.add(k)
    # 【新增】--out-name：仅在未指定 -o/--output 时生效（保证原有 -o 语义完全不变）
    out_path = args.output
    if not out_path and args.out_name:
        name = str(args.out_name).strip()
        if name:
            # 只取文件名部分，防止误传路径；扩展名统一由脚本补 .json / .js
            if name.lower().endswith(".json") or name.lower().endswith(".js"):
                name = os.path.splitext(name)[0]
            name = os.path.basename(name)
            if name:
                out_path = os.path.join(SCRIPT_DIR, name + ".json")
                log(f"--out-name 已启用：输出文件名前缀「{name}」")
    run(limit=args.limit, out_path=out_path, only=only)


if __name__ == "__main__":
    main()
