# -*- coding: utf-8 -*-
"""
Fir-Fetch (fixed & cleaned)
- 搜索 Bing（site:domain + 关键词 + filetype）
- 下载 pdf/xlsx/html
- 提取身份证/学号/联系方式
- 将疑似敏感文件归档并生成域名级报告

主要修复点：
1) 修复反编译导致的逻辑反转、提前 return、缩进错乱与异常链中断
2) 完整实现下载 -> 保存 -> 内容解析 -> 归档/报告 的闭环
3) Playwright 搜索流程最小可用实现 + 分页
4) 更稳健的正则与表格解析逻辑；减少误判
5) UI：目标解析、日志显示、结果目录打开、可取消的扫描线程

依赖：
pip install ttkbootstrap pdfplumber playwright httpx pandas lxml openpyxl html5lib
playwright install msedge   # 或使用 chromium 默认通道

作者：firefly（本文件为在其基础上的修复与精简）
"""

import asyncio
import hashlib
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pandas as pd
import pdfplumber
import tkinter as tk
from bs4 import BeautifulSoup
from tkinter import filedialog, messagebox, scrolledtext
import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from playwright.async_api import async_playwright, Error as PlaywrightError

# --------------------------- 日志器 ---------------------------

logger = logging.getLogger(__name__)
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s",
                                      datefmt="%H:%M:%S"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)

# --------------------------- 全局配置 ---------------------------

SEARCH_KEYWORDS = ['身份证', '奖学金', '公示', '名单', '学号', '联系方式']
BLACKLIST = ['课表', '选课', '培养方案', '招聘', '引进', '聘用', '应聘', '面试', '采购', '招标',
             '中标', '预算', '决算', '项目', '会议', '供应商', '教材', '统一身份认证平台', '智慧校园', '登录']
TITLE_FILTER_KEYWORDS = ['奖学金', '助学金', '名单', '公示', '评审表', '毕业生', '拟录取', '联系', '通讯录']

SENSITIVE_KEYWORDS = {'学号', '身份证', '邮箱', '手机'}
GENERAL_KEYWORDS = {'名单', '信息', '表', '通讯录', '公示'}

CHINESE_NAME_REGEX = re.compile(r'[\u4e00-\u9fa5]{2,4}')
ID_LIKE_NUMBER_REGEX = re.compile(r'(?<!\d)(?!\d{11}(?!\d))\d{8,}(?!\d)')

# 身份证号相关
_ID_CARD_PATTERN = r'[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])' \
                   r'(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx]'
PDF_ID_CARD_CONTEXT_REGEX = re.compile(
    rf'(?:居民身份证号|居民身份证|身份证号码|身份证号|身份证)\s*[:：]?\s*({_ID_CARD_PATTERN})'
)
ID_CARD_FORMAT_REGEX = re.compile(rf'^{_ID_CARD_PATTERN}$')
ID_LAST6_REGEX = re.compile(r'^\d{5}[\dxX]$')

# 学号
STUDENT_ID_FORMAT_REGEX = re.compile(r'^\d{4,20}$')
PDF_STUDENT_ID_CONTEXT_REGEX = re.compile(r'(?:学号|学生证号)\s*[:：]?\s*(\d{4,20})')
PDF_NAME_STUDENT_ID_REGEX = re.compile(r'([\u4e00-\u9fa5]{2,4})\s+(\d{8,20})')

# 联系方式
PHONE_REGEX = re.compile(r'1[3-9]\d{9}')
EMAIL_REGEX = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
NAME_PHONE_REGEX = re.compile(r'([\u4e00-\u9fa5]{2,4})\s*[:：]?\s*(1[3-9]\d{9})')
NAME_EMAIL_REGEX = re.compile(
    r'([\u4e00-\u9fa5]{2,4})\s*[:：]?\s*([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})'
)

MIN_FILE_SIZE = 256                 # 字节
MAX_PDF_PAGES = 10
MAX_XLS_SHEETS = 4
HEADER_RANGE = 5

ID_HEADERS = {'身份证号', '身份证号码', '身份证', '居民身份证号', '居民身份证'}
STUDENT_ID_HEADERS = {'学号', '学生证号'}
PHONE_EMAIL_HEADERS = {'e-mail', '联系方式', 'email', '电话', '手机', '邮箱'}
NAME_HEADERS = {'姓名', '联系人'}

MAX_CONCURRENT_TASKS = 25
semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

# 跨线程/协程共享
domain_reports: dict[str, list] = {}
report_lock = threading.Lock()
ssl_failed_domains: set[str] = set()

# --------------------------- 工具函数 ---------------------------

def sanitize_name(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', '_', name).strip()[:150]


def hash_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def get_filename_from_url(url: str, default_ext: str) -> str:
    path = urlparse(url).path
    base = Path(path).name or "downloaded"
    if '.' not in base and default_ext:
        base += f".{default_ext.lstrip('.')}"
    return sanitize_name(base)


def check_title_is_relevant(title: str) -> bool:
    return any(kw in title for kw in TITLE_FILTER_KEYWORDS)


def check_content_is_relevant(snippet: str) -> bool:
    """粗略相关性：含敏感词 + (人名 或 长数字)，或含通用词。"""
    snippet = snippet or ""
    contains_sensitive = any(kw in snippet for kw in SENSITIVE_KEYWORDS)
    contains_general = any(kw in snippet for kw in GENERAL_KEYWORDS)
    contains_name = bool(CHINESE_NAME_REGEX.search(snippet))
    contains_long_num = bool(ID_LIKE_NUMBER_REGEX.search(snippet))
    return (contains_sensitive and (contains_name or contains_long_num)) or contains_general


def check_blacklist(title: str, content: str) -> bool:
    t = (title or "") + " " + (content or "")
    t = t.lower()
    return any(word.lower() in t for word in BLACKLIST)


def is_valid_id_full(id_str: str) -> bool:
    return isinstance(id_str, str) and bool(ID_CARD_FORMAT_REGEX.fullmatch(id_str))


def is_valid_id_last6(id_str: str) -> bool:
    return isinstance(id_str, str) and bool(ID_LAST6_REGEX.fullmatch(id_str))


def is_valid_student_id_format(id_str: str) -> bool:
    return isinstance(id_str, str) and bool(STUDENT_ID_FORMAT_REGEX.fullmatch(id_str)) and ('*' not in id_str)


# --------------------------- 内容提取 ---------------------------

def extract_pdf_ids(file_path: Path, collect_contacts: bool) -> tuple[dict, dict, dict, list[str]]:
    """
    返回:
      full_ids:    {'第X页-表格'/'第X页-文本': 次数}
      last6_ids:   同上
      student_ids: 同上
      phone_email: 形如 '姓名: 联系方式' 的列表（collect_contacts=True 时）
    """
    full_ids, last6_ids, student_ids = {}, {}, {}
    phone_email_results: list[str] = []

    try:
        with pdfplumber.open(file_path) as pdf:
            for page_num, page in enumerate(pdf.pages[:MAX_PDF_PAGES], 1):
                # 表格
                tables = page.extract_tables() or []
                for table in tables:
                    if not table or len(table) < 2:
                        continue
                    header = [str(c).strip() if c else '' for c in table[0]]

                    id_cols = [i for i, col in enumerate(header) if any(h in col for h in ID_HEADERS)]
                    student_id_cols = [i for i, col in enumerate(header) if any(h in col for h in STUDENT_ID_HEADERS)]
                    name_cols = [i for i, col in enumerate(header) if any(h in col for h in NAME_HEADERS)]
                    pe_cols = [i for i, col in enumerate(header) if any(h in col for h in PHONE_EMAIL_HEADERS)]

                    # 身份证列
                    for col_idx in id_cols:
                        for row in table[1:]:
                            if len(row) <= col_idx:
                                continue
                            cell = str(row[col_idx] or '').strip().split('.')[0]
                            if not cell:
                                continue
                            if is_valid_id_full(cell):
                                key = f'第{page_num}页-表格'
                                full_ids[key] = full_ids.get(key, 0) + 1
                            elif len(cell) >= 6 and is_valid_id_last6(cell[-6:].upper()):
                                key = f'第{page_num}页-表格'
                                last6_ids[key] = last6_ids.get(key, 0) + 1

                    # 学号列
                    for col_idx in student_id_cols:
                        for row in table[1:]:
                            if len(row) <= col_idx:
                                continue
                            cell = str(row[col_idx] or '').strip().split('.')[0]
                            if not cell:
                                continue
                            if is_valid_student_id_format(cell):
                                key = f'第{page_num}页-表格'
                                student_ids[key] = student_ids.get(key, 0) + 1

                    # 联系方式列
                    if collect_contacts and name_cols and pe_cols:
                        name_col = name_cols[0]
                        for pe_col in pe_cols:
                            for row in table[1:]:
                                if len(row) <= max(name_col, pe_col):
                                    continue
                                name = str(row[name_col] or '').strip()
                                contact_info = str(row[pe_col] or '').strip()
                                if not name or not contact_info:
                                    continue
                                if PHONE_REGEX.search(contact_info) or EMAIL_REGEX.search(contact_info):
                                    phone_email_results.append(f'{name}: {contact_info}')

                # 文本
                text = page.extract_text() or ''
                for m in PDF_ID_CARD_CONTEXT_REGEX.finditer(text):
                    if is_valid_id_full(m.group(1)):
                        key = f'第{page_num}页-文本'
                        full_ids[key] = full_ids.get(key, 0) + 1
                for m in PDF_STUDENT_ID_CONTEXT_REGEX.finditer(text):
                    if is_valid_student_id_format(m.group(1)):
                        key = f'第{page_num}页-文本'
                        student_ids[key] = student_ids.get(key, 0) + 1
                for m in PDF_NAME_STUDENT_ID_REGEX.finditer(text):
                    if is_valid_student_id_format(m.group(2)):
                        key = f'第{page_num}页-文本(姓名+学号)'
                        student_ids[key] = student_ids.get(key, 0) + 1

                if collect_contacts:
                    for m in NAME_PHONE_REGEX.finditer(text):
                        phone_email_results.append(f'{m.group(1)}: {m.group(2)}')
                    for m in NAME_EMAIL_REGEX.finditer(text):
                        phone_email_results.append(f'{m.group(1)}: {m.group(2)}')
                    for phone in PHONE_REGEX.findall(text):
                        phone_email_results.append(f'单独手机号: {phone}')
                    for email in EMAIL_REGEX.findall(text):
                        phone_email_results.append(f'单独邮箱: {email}')

        return full_ids, last6_ids, student_ids, phone_email_results
    except Exception as e:
        logger.error(f'PDF 解析失败: {file_path.name} - {e}')
        return full_ids, last6_ids, student_ids, phone_email_results


def extract_xlsx_ids(file_path: Path, collect_contacts: bool) -> tuple[dict, dict, dict, list[str]]:
    full_ids, last6_ids, student_ids = {}, {}, {}
    phone_email_results: list[str] = []

    try:
        with pd.ExcelFile(file_path) as xls:
            for sheet_name in xls.sheet_names[:MAX_XLS_SHEETS]:
                df = pd.read_excel(xls, sheet_name=sheet_name, header=None)
                if df.empty:
                    continue

                # 粗扫：遍历单元格（比猜表头更鲁棒）
                for _, row in df.iterrows():
                    for cell in row.tolist():
                        s = str(cell or '').strip()
                        if not s:
                            continue
                        # 身份证
                        if is_valid_id_full(s):
                            full_ids[sheet_name] = full_ids.get(sheet_name, 0) + 1
                        elif len(s) >= 6 and is_valid_id_last6(s[-6:].upper()):
                            last6_ids[sheet_name] = last6_ids.get(sheet_name, 0) + 1
                        # 学号
                        if is_valid_student_id_format(s):
                            student_ids[sheet_name] = student_ids.get(sheet_name, 0) + 1
                        # 联系方式
                        if collect_contacts:
                            if PHONE_REGEX.search(s):
                                phone_email_results.append(f'单独手机号: {PHONE_REGEX.search(s).group(0)}')
                            for em in EMAIL_REGEX.findall(s):
                                phone_email_results.append(f'单独邮箱: {em}')

        return full_ids, last6_ids, student_ids, phone_email_results
    except Exception as e:
        logger.error(f'Excel 解析失败: {file_path.name} - {e}')
        return full_ids, last6_ids, student_ids, phone_email_results


def extract_html_ids(file_path: Path, collect_contacts: bool) -> tuple[dict, dict, dict, list[str]]:
    full_ids, last6_ids, student_ids = {}, {}, {}
    phone_email_results: list[str] = []

    try:
        # 先尝试表格
        try:
            dfs = pd.read_html(str(file_path), encoding='utf-8', flavor='html5lib')
        except ValueError:
            dfs = []

        for i, df in enumerate(dfs):
            if df.empty:
                continue
            sheet_name = f'表格-{i + 1}'
            for _, row in df.iterrows():
                for cell in row.tolist():
                    s = str(cell or '').strip()
                    if not s:
                        continue
                    if is_valid_id_full(s):
                        full_ids[sheet_name] = full_ids.get(sheet_name, 0) + 1
                    elif len(s) >= 6 and is_valid_id_last6(s[-6:].upper()):
                        last6_ids[sheet_name] = last6_ids.get(sheet_name, 0) + 1
                    if is_valid_student_id_format(s):
                        student_ids[sheet_name] = student_ids.get(sheet_name, 0) + 1
                    if collect_contacts:
                        if PHONE_REGEX.search(s):
                            phone_email_results.append(f'单独手机号: {PHONE_REGEX.search(s).group(0)}')
                        for em in EMAIL_REGEX.findall(s):
                            phone_email_results.append(f'单独邮箱: {em}')

        # 再解析全文文本
        html_content = file_path.read_text(encoding='utf-8', errors='ignore')
        soup = BeautifulSoup(html_content, 'lxml')
        text = soup.get_text(separator=' ') if soup else html_content

        for m in PDF_ID_CARD_CONTEXT_REGEX.finditer(text):
            if is_valid_id_full(m.group(1)):
                full_ids['页面文本'] = full_ids.get('页面文本', 0) + 1
        for m in PDF_STUDENT_ID_CONTEXT_REGEX.finditer(text):
            if is_valid_student_id_format(m.group(1)):
                student_ids['页面文本'] = student_ids.get('页面文本', 0) + 1

        for name, sid in PDF_NAME_STUDENT_ID_REGEX.findall(text):
            if is_valid_student_id_format(sid):
                student_ids['页面文本(姓名+学号)'] = student_ids.get('页面文本(姓名+学号)', 0) + 1

        if collect_contacts:
            for m in NAME_PHONE_REGEX.finditer(text):
                phone_email_results.append(f'{m.group(1)}: {m.group(2)}')
            for m in NAME_EMAIL_REGEX.finditer(text):
                phone_email_results.append(f'{m.group(1)}: {m.group(2)}')
            for phone in PHONE_REGEX.findall(text):
                phone_email_results.append(f'单独手机号: {phone}')
            for email in EMAIL_REGEX.findall(text):
                phone_email_results.append(f'单独邮箱: {email}')

        return full_ids, last6_ids, student_ids, phone_email_results
    except Exception as e:
        logger.error(f'HTML 解析失败: {file_path.name} - {e}')
        return full_ids, last6_ids, student_ids, phone_email_results


# --------------------------- 下载与归档 ---------------------------

async def download_and_analyze(
    url: str,
    title: str,
    domain: str,
    download_dir: Path,
    id_card_dir: Path,
    student_id_dir: Path,
    phone_email_dir: Path,
    file_counter_state: dict,
    processed_phone_email: set[str],
    processed_content_hashes: set[str],
    collect_contacts: bool,
    expected_ext: str | None = None,
) -> None:
    """下载 URL，保存并解析。根据结果将敏感文件移动到对应目录，并记录报告。"""
    async with semaphore:
        parsed_url = urlparse(url)
        headers = {
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0'),
            'Accept': ('text/html,application/xhtml+xml,application/xml;'
                       'q=0.9,image/webp,image/apng,*/*;q=0.8,'
                       'application/signed-exchange;v=b3;q=0.7'),
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Referer': f'{parsed_url.scheme}://{parsed_url.netloc}/',
        }

        with report_lock:
            verify_param = domain not in ssl_failed_domains

        content = None
        last_exc = None

        for attempt in range(2):
            try:
                async with httpx.AsyncClient(verify=verify_param, follow_redirects=True, timeout=30) as client:
                    resp = await client.get(url, headers=headers)
                    resp.raise_for_status()
                    content = resp.content
                    break
            except httpx.ConnectError as e:
                if 'CERTIFICATE_VERIFY_FAILED' in str(e) and verify_param:
                    logger.warning(f'🚨 域名 \'{domain}\' 首次 SSL 验证失败，将在后续请求禁用验证。')
                    with report_lock:
                        ssl_failed_domains.add(domain)
                    verify_param = False
                    last_exc = e
                    continue
                last_exc = e
                break
            except httpx.HTTPStatusError as e:
                last_exc = e
                break
            except Exception as e:
                last_exc = e
                break

        if content is None:
            logger.error(f'下载失败: {url} - {last_exc}')
            return

        if len(content) < MIN_FILE_SIZE:
            logger.debug(f'忽略过小文件 ({len(content)} B): {url}')
            return

        # 去重
        h = hash_bytes(content)
        if h in processed_content_hashes:
            logger.debug(f'重复内容，跳过: {url}')
            return
        processed_content_hashes.add(h)

        # 保存
        ext = (expected_ext or '').lower().lstrip('.')
        if not ext:
            # 从路径猜扩展
            guess = Path(urlparse(url).path).suffix.lower().lstrip('.')
            ext = guess or 'html'
        fname_base = f"{file_counter_state['count']:04d} - {sanitize_name(title or get_filename_from_url(url, ext))}"
        fname = f"{fname_base}.{ext}" if not fname_base.lower().endswith(f".{ext}") else fname_base
        file_counter_state['count'] += 1

        save_path = download_dir / fname
        try:
            save_path.write_bytes(content)
        except Exception as e:
            logger.error(f'保存失败: {save_path.name} - {e}')
            return

        # 解析
        full_ids, last6_ids, student_ids, phone_emails = {}, {}, {}, []
        try:
            if ext == 'pdf':
                full_ids, last6_ids, student_ids, phone_emails = extract_pdf_ids(save_path, collect_contacts)
            elif ext in ('xlsx', 'xls'):
                full_ids, last6_ids, student_ids, phone_emails = extract_xlsx_ids(save_path, collect_contacts)
            else:
                # 默认当作 html
                full_ids, last6_ids, student_ids, phone_emails = extract_html_ids(save_path, collect_contacts)
        except Exception as e:
            logger.error(f'解析失败: {save_path.name} - {e}')

        # 联系方式合并去重
        if collect_contacts and phone_emails:
            with report_lock:
                processed_phone_email.update(phone_emails)

        # 敏感归档 + 报告
        def _move_unique(dst_dir: Path, path: Path) -> Path:
            dst = dst_dir / path.name
            if dst.exists():
                dst = dst_dir / f"{path.stem}_{int(time.time())}{path.suffix}"
            try:
                path.replace(dst)
                return dst
            except Exception as move_e:
                logger.error(f'移动敏感文件失败: {path.name} -> {dst_dir} - {move_e}')
                return path

        def _append_report(_type: str, details: str):
            with report_lock:
                domain_reports.setdefault(domain, []).append({
                    'file': save_path.name,
                    'title': title,
                    'url': url,
                    'type': _type,
                    'details': details
                })

        # 身份证
        if full_ids or last6_ids:
            parts = []
            if full_ids:
                parts.append("完整身份证号: " + "; ".join(f"{k}:{v}" for k, v in full_ids.items()))
            if last6_ids:
                parts.append("身份证后6位: " + "; ".join(f"{k}:{v}" for k, v in last6_ids.items()))
            details = "; ".join(parts) if parts else "疑似身份证信息"
            logger.warning(f'🚨 敏感文件(身份证) [{domain}]: {save_path.name} → {details}')
            _append_report('身份证', details)
            _ = _move_unique(id_card_dir, save_path)
            return

        # 学号
        if student_ids:
            details = "学号: " + "; ".join(f"{k}:{v}" for k, v in student_ids.items())
            logger.warning(f'🚨 敏感文件(学号) [{domain}]: {save_path.name} → {details}')
            _append_report('学号', details)
            _ = _move_unique(student_id_dir, save_path)
            return

        # 仅联系方式（可选）
        if collect_contacts and processed_phone_email:
            # 为了避免大量重复，只记录一次“手机号和邮箱”
            _append_report('手机号和邮箱', f'累计 {len(processed_phone_email)} 条（去重后）')
            # 移动文件到目录（非必须）
            _ = _move_unique(phone_email_dir, save_path)
            return

        # 非敏感保留在下载目录
        logger.info(f'已解析（无敏感命中）: {save_path.name}')


# --------------------------- 搜索与抓取 ---------------------------

async def extract_results_from_page(page):
    """解析 Bing 搜索结果，返回 [(title, url, snippet)]"""
    results = []
    try:
        await page.wait_for_selector('li.b_algo h2 a', timeout=15000)
        items = await page.locator('li.b_algo').all()
        for it in items:
            try:
                a = it.locator('h2 a')
                title = (await a.text_content()) or ''
                url = await a.get_attribute('href')
                snippet = await it.locator('.b_caption p').text_content() if await it.locator('.b_caption p').count() else ''
                if url:
                    results.append((title.strip(), url.strip(), (snippet or '').strip()))
            except Exception:
                continue
    except PlaywrightError:
        logger.info('页面上未找到任何结果 (b_algo)。可能出现人机验证或无结果。')
    return results


async def search_worker(
    context,
    file_type: str,                  # 'pdf' / 'xlsx' / 'html'
    domain: str,
    pages: int,
    download_dir: Path,
    id_card_dir: Path,
    student_id_dir: Path,
    phone_email_dir: Path,
    file_counter_state: dict,
    processed_urls: set[str],
    processed_phone_email: set[str],
    processed_content_hashes: set[str],
    collect_contacts: bool,
    search_keywords: list[str],
):
    page = await context.new_page()
    # 屏蔽图片/样式等
    await page.route('**/*', lambda route: route.abort()
                     if route.request.resource_type not in ['document', 'script', 'xhr', 'fetch']
                     else route.continue_())

    try:
        await page.goto('https://www.bing.com', wait_until='domcontentloaded', timeout=30000)

        # 逐关键词检索
        for kw in search_keywords:
            # Bing 支持 filetype:pdf / filetype:xlsx；html 不加 filetype
            if file_type in ('pdf', 'xlsx'):
                query = f'site:{domain} {kw} filetype:{file_type}'
                expected_ext = file_type
            else:
                query = f'site:{domain} {kw}'
                expected_ext = None

            logger.info(f'🔎 [{domain}] 搜索: {query}')
            sb = page.locator('#sb_form_q')
            await sb.fill(query)
            await sb.press('Enter')
            await page.wait_for_load_state('domcontentloaded')

            for p in range(pages):
                results = await extract_results_from_page(page)
                if not results:
                    break

                for title, url, snippet in results:
                    if domain not in url:
                        continue
                    if check_blacklist(title, snippet):
                        continue
                    # 对 html，优先要求标题相关性；对文档类型放宽
                    if file_type == 'html' and (not check_title_is_relevant(title)) and (not check_content_is_relevant(snippet)):
                        continue

                    if url in processed_urls:
                        continue
                    processed_urls.add(url)

                    # 启动下载任务
                    await download_and_analyze(
                        url=url,
                        title=title,
                        domain=domain,
                        download_dir=download_dir,
                        id_card_dir=id_card_dir,
                        student_id_dir=student_id_dir,
                        phone_email_dir=phone_email_dir,
                        file_counter_state=file_counter_state,
                        processed_phone_email=processed_phone_email,
                        processed_content_hashes=processed_content_hashes,
                        collect_contacts=collect_contacts,
                        expected_ext=expected_ext,
                    )

                # 下一页
                try:
                    # 适配多种分页选择器
                    if await page.locator('a.sb_pagN').count():
                        await page.locator('a.sb_pagN').first.click()
                    elif await page.locator('a[title="Next page"]').count():
                        await page.locator('a[title="Next page"]').first.click()
                    else:
                        break
                    await page.wait_for_load_state('domcontentloaded')
                except Exception:
                    break

    except Exception as e:
        logger.error(f'搜索任务失败 (类型: {file_type}): {e}')
    finally:
        await page.close()


async def scan_domain(browser, domain: str, pages: int, show_browser: bool, proxy: str, collect_contacts: bool):
    logger.info(f"\n==================== 开始扫描域名: {domain} ====================")

    base_dir = Path(__file__).parent.resolve() / 'data'
    download_dir = base_dir / 'downloads' / domain
    sensitive_base_dir = base_dir / 'sensitive_files' / domain
    id_card_dir = sensitive_base_dir / '身份证'
    student_id_dir = sensitive_base_dir / '学号'
    phone_email_dir = sensitive_base_dir / '手机号和邮箱'
    for d in (download_dir, id_card_dir, student_id_dir, phone_email_dir):
        d.mkdir(parents=True, exist_ok=True)

    with report_lock:
        domain_reports[domain] = []

    processed_urls: set[str] = set()
    processed_content_hashes: set[str] = set()
    processed_phone_email: set[str] = set()

    file_counter_state = {'count': 1}
    current_search_keywords = list(SEARCH_KEYWORDS)

    if not collect_contacts and '联系方式' in current_search_keywords:
        current_search_keywords.remove('联系方式')
        logger.info('ℹ️ 已禁用联系方式收集，将跳过相关关键字。')

    try:
        context = await browser.new_context(
            user_agent=('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                        'AppleWebKit/537.36 (KHTML, like Gecko) '
                        'Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0'),
            java_script_enabled=True,
        )

        tasks = [
            search_worker(context, 'pdf', domain, pages, download_dir, id_card_dir, student_id_dir,
                          phone_email_dir, file_counter_state, processed_urls, processed_phone_email,
                          processed_content_hashes, collect_contacts, current_search_keywords),
            search_worker(context, 'xlsx', domain, pages, download_dir, id_card_dir, student_id_dir,
                          phone_email_dir, file_counter_state, processed_urls, processed_phone_email,
                          processed_content_hashes, collect_contacts, current_search_keywords),
            search_worker(context, 'html', domain, pages, download_dir, id_card_dir, student_id_dir,
                          phone_email_dir, file_counter_state, processed_urls, processed_phone_email,
                          processed_content_hashes, collect_contacts, current_search_keywords),
        ]
        await asyncio.gather(*tasks)
        await context.close()
    except PlaywrightError as e:
        logger.critical(f'浏览器操作失败 [{domain}]: {e}')


def print_final_report():
    logger.info("\n========================= 扫描完成 - 检测报告 =========================")
    found_any = False

    for domain, report in domain_reports.items():
        if not report:
            logger.info(f'\n✅ 域名: {domain} → 未发现敏感文件！')
            continue

        found_any = True
        logger.warning(f'\n🚨 域名: {domain} → 发现 {len(report)} 个敏感文件/记录:')

        # 尝试按文件前缀编号排序
        try:
            sorted_report = sorted(report, key=lambda x: int((x["file"].split(" - ")[0]).lstrip("0") or "0"))
        except Exception:
            sorted_report = report

        phone_email_reported = False
        for i, item in enumerate(sorted_report, 1):
            if item['type'] == '手机号和邮箱' and phone_email_reported:
                # 避免重复刷屏
                continue

            log_message = (
                f"\n  --- [{i}] 文件名/来源: {item['file']}"
                f"\n      类型: {item['type']}"
                f"\n      标题: {item['title']}"
                f"\n      URL: {item['url']}"
                f"\n      详情: {item['details']}"
            )
            logger.warning(log_message)

            if item['type'] == '手机号和邮箱':
                phone_email_reported = True

    if not found_any:
        logger.info('\n🎉 未发现任何敏感文件！')


async def async_main_logic(target_domains, pages, show_browser, proxy, collect_contacts):
    async with async_playwright() as p:
        browser_opts = {'headless': not show_browser, 'args': ['--no-sandbox', '--disable-gpu']}
        if proxy:
            browser_opts['proxy'] = {'server': proxy}

        logger.info('正在启动 Edge/Chromium 浏览器...')
        try:
            # 优先使用 Edge 通道；失败则退回默认 Chromium
            try:
                browser = await p.chromium.launch(channel='msedge', **browser_opts)
            except PlaywrightError:
                browser = await p.chromium.launch(**browser_opts)

            logger.info(f'开始扫描（每个关键词最多扫描 {pages} 页）...')
            for domain in target_domains:
                await asyncio.sleep(0)
                await scan_domain(browser, domain, pages, show_browser, proxy, collect_contacts)

            await browser.close()
            logger.info('浏览器已关闭')
            print_final_report()
        except PlaywrightError:
            logger.critical('浏览器启动失败，可能需要首次安装。')
            logger.info('尝试自动安装浏览器依赖...')
            try:
                subprocess.run([sys.executable, '-m', 'playwright', 'install', 'msedge'],
                               check=True, capture_output=True, text=True, encoding='utf-8')
                subprocess.run([sys.executable, '-m', 'playwright', 'install-deps', 'msedge'],
                               check=True, capture_output=True, text=True, encoding='utf-8')
                logger.info('依赖安装成功！请重新启动程序并开始扫描。')
            except subprocess.CalledProcessError as e:
                logger.critical(f'自动安装失败: {e}\nOutput: {e.stdout}\nError: {e.stderr}')
                logger.info("可手动执行：'playwright install msedge' 与 'playwright install-deps msedge'")


# --------------------------- GUI 日志队列 ---------------------------

class QueueHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        self.log_queue.put(self.format(record))


# --------------------------- 设置窗口 ---------------------------

class SettingsWindow(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title('设置')
        self.transient(parent)
        self.grab_set()
        self.bg_color = parent.style.colors.get('bg')
        self.configure(bg=self.bg_color)
        self.create_widgets()
        self.center_window()

    def center_window(self):
        self.update_idletasks()
        width, height = 700, 600
        parent = self.master
        x = parent.winfo_x() + parent.winfo_width() // 2 - width // 2
        y = parent.winfo_y() + parent.winfo_height() // 2 - height // 2
        self.geometry(f'{width}x{height}+{x}+{y}')

    def create_widgets(self):
        main = ttk.Frame(self, padding=10)
        main.pack(fill=BOTH, expand=True)

        notebook = ttk.Notebook(main)
        notebook.pack(fill=BOTH, expand=True, pady=(0, 10))

        # 搜索关键词
        tab_search = ttk.Frame(notebook, padding=10)
        notebook.add(tab_search, text='搜索关键词')
        ttk.Label(tab_search, text='搜索关键词 (每行一个):', style='White.TLabel').pack(fill=X, pady=(5, 5), anchor='w')
        self.search_keywords_text = scrolledtext.ScrolledText(tab_search, height=15, relief='solid', borderwidth=1,
                                                              font=('Microsoft YaHei UI', 10),
                                                              background='#2C3E50', foreground='white',
                                                              insertbackground='white')
        self.search_keywords_text.pack(fill=BOTH, expand=True)
        self.search_keywords_text.insert(tk.END, '\n'.join(SEARCH_KEYWORDS))

        # 标题黑名单
        tab_blacklist = ttk.Frame(notebook, padding=10)
        notebook.add(tab_blacklist, text='标题黑名单')
        ttk.Label(tab_blacklist, text='URL/标题黑名单关键词 (每行一个):', style='White.TLabel')\
            .pack(fill=X, pady=(5, 5), anchor='w')
        self.blacklist_text = scrolledtext.ScrolledText(tab_blacklist, height=15, relief='solid', borderwidth=1,
                                                        font=('Microsoft YaHei UI', 10),
                                                        background='#2C3E50', foreground='white',
                                                        insertbackground='white')
        self.blacklist_text.pack(fill=BOTH, expand=True)
        self.blacklist_text.insert(tk.END, '\n'.join(BLACKLIST))

        # 标题白名单
        tab_title_whitelist = ttk.Frame(notebook, padding=10)
        notebook.add(tab_title_whitelist, text='标题白名单')
        ttk.Label(tab_title_whitelist, text='标题白名单关键词 (每行一个):', style='White.TLabel')\
            .pack(fill=X, pady=(5, 5), anchor='w')
        self.title_filter_text = scrolledtext.ScrolledText(tab_title_whitelist, height=15, relief='solid',
                                                           borderwidth=1, font=('Microsoft YaHei UI', 10),
                                                           background='#2C3E50', foreground='white',
                                                           insertbackground='white')
        self.title_filter_text.pack(fill=BOTH, expand=True)
        self.title_filter_text.insert(tk.END, '\n'.join(TITLE_FILTER_KEYWORDS))

        # 内容检测关键字
        tab_content_keywords = ttk.Frame(notebook, padding=10)
        tab_content_keywords.columnconfigure(0, weight=1)
        tab_content_keywords.rowconfigure(1, weight=1)
        tab_content_keywords.rowconfigure(3, weight=1)
        notebook.add(tab_content_keywords, text='内容检测关键字')

        ttk.Label(tab_content_keywords, text='内容敏感关键词 (每行一个):', style='White.TLabel')\
            .grid(row=0, column=0, sticky='w', pady=(5, 5))
        self.sensitive_keywords_text = scrolledtext.ScrolledText(tab_content_keywords, height=6, relief='solid',
                                                                 borderwidth=1, font=('Microsoft YaHei UI', 10),
                                                                 background='#2C3E50', foreground='white',
                                                                 insertbackground='white')
        self.sensitive_keywords_text.grid(row=1, column=0, sticky='nsew')
        self.sensitive_keywords_text.insert(tk.END, '\n'.join(SENSITIVE_KEYWORDS))

        ttk.Label(tab_content_keywords, text='内容通用关键词 (每行一个):', style='White.TLabel')\
            .grid(row=2, column=0, sticky='w', pady=(15, 5))
        self.general_keywords_text = scrolledtext.ScrolledText(tab_content_keywords, height=6, relief='solid',
                                                               borderwidth=1, font=('Microsoft YaHei UI', 10),
                                                               background='#2C3E50', foreground='white',
                                                               insertbackground='white')
        self.general_keywords_text.grid(row=3, column=0, sticky='nsew')
        self.general_keywords_text.insert(tk.END, '\n'.join(GENERAL_KEYWORDS))

        # 按钮
        btn_frame = ttk.Frame(main)
        btn_frame.pack(side=BOTTOM, fill=X, pady=(10, 0))
        ttk.Button(btn_frame, text='保存', command=self.save_settings, bootstyle='success').pack(side=RIGHT)
        ttk.Button(btn_frame, text='取消', command=self.destroy, bootstyle='secondary-outline').pack(side=RIGHT, padx=5)

    def save_settings(self):
        global SEARCH_KEYWORDS, BLACKLIST, TITLE_FILTER_KEYWORDS, SENSITIVE_KEYWORDS, GENERAL_KEYWORDS
        SEARCH_KEYWORDS = [line.strip() for line in self.search_keywords_text.get('1.0', tk.END).splitlines() if line.strip()]
        BLACKLIST = [line.strip() for line in self.blacklist_text.get('1.0', tk.END).splitlines() if line.strip()]
        TITLE_FILTER_KEYWORDS = [line.strip() for line in self.title_filter_text.get('1.0', tk.END).splitlines() if line.strip()]
        SENSITIVE_KEYWORDS = {line.strip() for line in self.sensitive_keywords_text.get('1.0', tk.END).splitlines() if line.strip()}
        GENERAL_KEYWORDS = {line.strip() for line in self.general_keywords_text.get('1.0', tk.END).splitlines() if line.strip()}
        messagebox.showinfo('成功', '设置已保存。', parent=self)
        self.destroy()


# --------------------------- UI 组件 ---------------------------

class GradientFrame(tk.Canvas):
    def __init__(self, parent, colors=('gray20', 'gray10'), **kwargs):
        tk.Canvas.__init__(self, parent, **kwargs)
        self.colors = colors
        self.bind('<Configure>', self.draw_gradient)

    def draw_gradient(self, event=None):
        self.delete('gradient')
        width = self.winfo_width()
        height = self.winfo_height()
        r1, g1, b1 = self.winfo_rgb(self.colors[0])
        r2, g2, b2 = self.winfo_rgb(self.colors[1])
        r_ratio = float(r2 - r1) / max(height, 1)
        g_ratio = float(g2 - g1) / max(height, 1)
        b_ratio = float(b2 - b1) / max(height, 1)
        for i in range(height):
            nr = int(r1 + r_ratio * i)
            ng = int(g1 + g_ratio * i)
            nb = int(b1 + b_ratio * i)
            color = f'#{nr // 256:02x}{ng // 256:02x}{nb // 256:02x}'
            self.create_line(0, i, width, i, tags=('gradient',), fill=color)


class App(ttk.Window):
    def __init__(self, themename='darkly'):
        super().__init__(themename=themename)
        self.title('Fir-Fetch Plus')
        self.geometry('960x600')

        self.bg_color = self.style.colors.get('bg')
        self.style.configure('Transparent.TFrame', background=self.bg_color)
        self.style.configure('White.TLabel', foreground=self.style.colors.get('fg'), background=self.bg_color,
                             font=('Microsoft YaHei UI', 10))
        self.style.configure('White.TLabelframe.Label', foreground=self.style.colors.get('fg'), background=self.bg_color,
                             font=('Microsoft YaHei UI', 10))

        self.placeholder_text = '输入单个域名或浏览文件...'
        self.placeholder_color = 'grey'
        self.default_fg_color = self.style.lookup('TEntry', 'foreground')

        self.create_widgets()
        self.setup_logging()

        self.scan_thread: threading.Thread | None = None
        self.scan_loop: asyncio.AbstractEventLoop | None = None

    # ----- UI 构建 -----
    def create_widgets(self):
        bg_frame = GradientFrame(self, colors=('#2E3B55', '#1C2833'))
        bg_frame.pack(fill=BOTH, expand=True)

        main_frame = ttk.Frame(bg_frame, padding='15', style='Transparent.TFrame')
        main_frame.pack(fill=BOTH, expand=True)
        main_frame.grid_rowconfigure(1, weight=1)
        main_frame.grid_columnconfigure(0, weight=1)

        controls = ttk.Labelframe(main_frame, text='扫描配置', padding='10', style='White.TLabelframe')
        controls.grid(row=0, column=0, sticky='ew', pady=(0, 10))
        controls.grid_columnconfigure(1, weight=1)

        ttk.Label(controls, text='目标:', style='White.TLabel').grid(row=0, column=0, sticky='w', padx=5, pady=5)
        self.target_var = tk.StringVar()
        self.target_entry = ttk.Entry(controls, textvariable=self.target_var, font=('Microsoft YaHei UI', 10))
        self.target_entry.grid(row=0, column=1, sticky='ew', padx=(0, 5), pady=5)
        self.target_entry.insert(0, self.placeholder_text)
        self.target_entry.config(foreground=self.placeholder_color)
        self.target_entry.bind('<FocusIn>', self.on_target_focus_in)
        self.target_entry.bind('<FocusOut>', self.on_target_focus_out)

        ttk.Button(controls, text='浏览文件', command=self.browse_file, bootstyle='light-outline')\
            .grid(row=0, column=2, padx=5, pady=5)
        self.start_button = ttk.Button(controls, text='开始扫描', command=self.start_scan, bootstyle='success')
        self.start_button.grid(row=0, column=3, padx=5, pady=5)
        ttk.Button(controls, text='设置', command=self.open_settings, bootstyle='secondary')\
            .grid(row=0, column=4, padx=5, pady=5)
        ttk.Button(controls, text='打开结果文件夹', command=self.open_results_folder, bootstyle='info')\
            .grid(row=0, column=5, padx=5, pady=5)

        # 选项
        ttk.Label(controls, text='选项:', style='White.TLabel').grid(row=1, column=0, sticky='w', padx=5, pady=5)
        options = ttk.Frame(controls, style='Transparent.TFrame')
        options.grid(row=1, column=1, columnspan=5, sticky='ew', padx=0, pady=5)
        options.grid_columnconfigure(3, weight=1)

        ttk.Label(options, text='搜索页数:', style='White.TLabel').grid(row=0, column=0, sticky='w')
        self.pages_var = tk.IntVar(value=3)
        ttk.Spinbox(options, from_=1, to=20, textvariable=self.pages_var, width=5)\
            .grid(row=0, column=1, padx=(5, 15), sticky='w')

        ttk.Label(options, text='代理:', style='White.TLabel').grid(row=0, column=2, sticky='w')
        self.proxy_var = tk.StringVar(value='')
        ttk.Entry(options, textvariable=self.proxy_var).grid(row=0, column=3, padx=(5, 15), sticky='ew')

        self.show_browser_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text='显示浏览器', variable=self.show_browser_var, bootstyle='round-toggle')\
            .grid(row=0, column=4, padx=(0, 5))

        self.verbose_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text='显示详细信息', variable=self.verbose_var, bootstyle='round-toggle')\
            .grid(row=0, column=5, padx=(0, 15))

        self.collect_contacts_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text='收集联系方式', variable=self.collect_contacts_var, bootstyle='round-toggle')\
            .grid(row=0, column=6, padx=(0, 15))

        # 日志
        log_frame = ttk.Labelframe(main_frame, text='日志输出', padding='10', style='White.TLabelframe')
        log_frame.grid(row=1, column=0, sticky='nsew', pady=(10, 0))
        log_frame.grid_rowconfigure(0, weight=1)
        log_frame.grid_columnconfigure(0, weight=1)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, state='disabled', wrap=tk.WORD, font=('Courier New', 10),
            relief='solid', borderwidth=1, bg='#1C2833', fg='white', insertbackground='white'
        )
        self.log_text.grid(row=0, column=0, sticky='nsew')
        self.log_text.tag_config('INFO', foreground='white')
        self.log_text.tag_config('WARNING', foreground='#F39C12')
        self.log_text.tag_config('ERROR', foreground='#E74C3C')
        self.log_text.tag_config('CRITICAL', foreground='#C0392B', font=('Courier New', 10, 'bold'))
        self.log_text.tag_config('DEBUG', foreground='#7F8C8D')

    # ----- UI 回调 -----
    def on_target_focus_in(self, _):
        if self.target_entry.get() == self.placeholder_text:
            self.target_entry.delete(0, 'end')
            self.target_entry.config(foreground=self.default_fg_color)

    def on_target_focus_out(self, _):
        if not self.target_entry.get():
            self.target_entry.insert(0, self.placeholder_text)
            self.target_entry.config(foreground=self.placeholder_color)

    def open_settings(self):
        SettingsWindow(self)

    def open_results_folder(self):
        results_path = Path.home() / 'Desktop' / 'sfz_scan' / 'sensitive_files'
        results_path.mkdir(parents=True, exist_ok=True)
        messagebox.showinfo('提示', f'结果文件夹位于:\n{results_path}', parent=self)
        try:
            if sys.platform.startswith('win'):
                os.startfile(results_path)  # type: ignore[attr-defined]
            elif sys.platform == 'darwin':
                subprocess.run(['open', str(results_path)], check=False)
            else:
                subprocess.run(['xdg-open', str(results_path)], check=False)
        except Exception as e:
            messagebox.showerror('错误', f'无法打开文件夹: {e}', parent=self)

    def browse_file(self):
        filepath = filedialog.askopenfilename(
            title='选择域名文件', filetypes=(('Text files', '*.txt'), ('All files', '*.*')), parent=self
        )
        if filepath:
            self.on_target_focus_in(None)
            self.target_var.set(f'file://{filepath}')

    def setup_logging(self):
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.queue_handler = QueueHandler(self.log_queue)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')
        self.queue_handler.setFormatter(formatter)
        logger.addHandler(self.queue_handler)

        # 根据“详细信息”开关动态调整日志级别
        def _refresh_level():
            logger.setLevel(logging.DEBUG if self.verbose_var.get() else logging.INFO)
            self.after(1000, _refresh_level)

        _refresh_level()
        self.after(100, self.poll_log_queue)

    def poll_log_queue(self):
        try:
            while True:
                record = self.log_queue.get(block=False)
                self.display_log(record)
        except queue.Empty:
            pass
        self.after(100, self.poll_log_queue)

    def display_log(self, record: str):
        self.log_text.configure(state='normal')
        level_tag = 'INFO'
        if 'CRITICAL' in record:
            level_tag = 'CRITICAL'
        elif 'ERROR' in record:
            level_tag = 'ERROR'
        elif 'WARNING' in record or '🚨' in record or '🎯' in record or '🔎' in record:
            level_tag = 'WARNING'
        self.log_text.insert(tk.END, record + '\n', level_tag)
        self.log_text.configure(state='disabled')
        self.log_text.yview(tk.END)

    def _parse_targets_from_file(self, fp: str) -> list[str]:
        p = fp.replace('file://', '')
        path = Path(p)
        if not path.exists():
            messagebox.showerror('错误', f'文件不存在: {path}', parent=self)
            return []
        domains: list[str] = []
        for line in path.read_text(encoding='utf-8', errors='ignore').splitlines():
            line = line.strip()
            if not line:
                continue
            # 允许 http(s):// 形式
            if '://' in line:
                netloc = urlparse(line).netloc or line
                domains.append(netloc.split('/')[0])
            else:
                domains.append(line)
        # 去重/清洗
        cleaned = []
        for d in domains:
            d = d.replace('http://', '').replace('https://', '').strip('/')
            if d and d not in cleaned:
                cleaned.append(d)
        return cleaned

    def get_targets(self) -> list[str]:
        s = self.target_var.get().strip()
        if not s or s == self.placeholder_text:
            return []
        if s.startswith('file://') or (s.endswith('.txt') and Path(s).exists()):
            return self._parse_targets_from_file(s)
        # 单个域名/URL
        if '://' in s:
            d = urlparse(s).netloc or s
        else:
            d = s
        d = d.replace('http://', '').replace('https://', '').strip('/')
        return [d] if d else []

    def start_scan(self):
        global ssl_failed_domains, domain_reports
        target_domains = self.get_targets()
        if not target_domains:
            messagebox.showwarning('输入错误', '请输入一个域名或选择一个目标文件。', parent=self)
            return

        # 清状态
        ssl_failed_domains = set()
        domain_reports = {}

        pages = max(1, int(self.pages_var.get()))
        show_browser = bool(self.show_browser_var.get())
        proxy = self.proxy_var.get().strip()
        collect_contacts = bool(self.collect_contacts_var.get())

        self.start_button.config(text='取消扫描', command=self.cancel_scan, bootstyle='danger')
        logger.info(f'🎯 目标域名: {", ".join(target_domains)}')

        # 启动线程运行异步任务
        def _runner():
            self.scan_loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(self.scan_loop)
                self.scan_loop.run_until_complete(
                    async_main_logic(target_domains, pages, show_browser, proxy, collect_contacts)
                )
            except asyncio.CancelledError:
                logger.info('扫描任务已被取消。')
            except Exception as e:
                logger.error(f'扫描异常: {e}')
            finally:
                try:
                    self.scan_loop.close()
                except Exception:
                    pass
                self.scan_loop = None
                self.after(0, self.on_scan_complete)

        self.scan_thread = threading.Thread(target=_runner, daemon=True)
        self.scan_thread.start()

    def cancel_scan(self):
        self.start_button.config(text='正在取消...', state='disabled')
        logger.info('用户请求取消扫描...')
        try:
            if self.scan_loop and self.scan_loop.is_running():
                self.scan_loop.call_soon_threadsafe(self.scan_loop.stop)
        finally:
            # UI 恢复在 on_scan_complete
            pass

    def on_scan_complete(self):
        self.start_button.config(text='开始扫描', command=self.start_scan, state='normal', bootstyle='success')
        logger.info('==================== 扫描任务已结束 ====================')

    def show_playwright_install_prompt(self):
        response = messagebox.askyesno('Playwright 依赖缺失',
                                       'Playwright Edge 浏览器依赖似乎未安装。\n是否要尝试自动安装？',
                                       parent=self)
        if response:
            self.start_button.config(text='正在安装...', state='disabled')
            self.update()
            threading.Thread(target=self.run_playwright_install, daemon=True).start()

    def run_playwright_install(self):
        try:
            logger.info('执行: playwright install msedge')
            subprocess.run([sys.executable, '-m', 'playwright', 'install', 'msedge'],
                           check=True, capture_output=True, text=True, encoding='utf-8')
            logger.info('执行: playwright install-deps msedge')
            subprocess.run([sys.executable, '-m', 'playwright', 'install-deps', 'msedge'],
                           check=True, capture_output=True, text=True, encoding='utf-8')
            logger.info('依赖安装成功！请重新启动程序并开始扫描。')
            self.after(0, lambda: messagebox.showinfo('成功', '依赖安装成功！\n请重新启动程序。'))
            self.after(0, lambda: self.start_button.config(text='开始扫描', state='normal'))
        except subprocess.CalledProcessError as e:
            logger.error(f'自动安装失败: {e}\nOutput: {e.stdout}\nError: {e.stderr}')
            self.after(0, lambda: messagebox.showerror('安装失败', '自动安装失败，请查看日志或手动执行安装命令。'))


# --------------------------- 入口 ---------------------------

if __name__ == '__main__':
    try:
        app = App()
        globals()['app_instance'] = app
        app.mainloop()
    except KeyboardInterrupt:
        logger.info('\n用户手动退出程序。')
