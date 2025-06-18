import os
import re
import json
import time
import logging
import requests
import schedule
from typing import Optional
from openai import OpenAI
import xml.etree.ElementTree as ET
from datetime import datetime
from html import unescape
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone, timedelta
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path
#import emoji
#from io import BytesIO
from html2image import Html2Image
from jinja2 import Template
from jinja2 import Environment, FileSystemLoader
import subprocess
import base64
import win32gui
import win32con


# -------------------
# 配置区
# -------------------


# 加载配置文件
CONFIG_PATH = './config.json'
with open(CONFIG_PATH, 'r', encoding='utf-8') as config_file:
    config = json.load(config_file)

# 从配置文件中获取参数
MIRAI_API_URL = config.get("MIRAI_API_URL")
VERIFY_KEY = config.get("VERIFY_KEY")
QQ_ID = config.get("QQ_ID")
TARGET_IDs = config.get("TARGET_IDs")
TARGET_IDs_list = config.get("TARGET_IDs_list")
USERNAME_LIST = config.get("USERNAME_LIST")
RSS_URLS = config.get("RSS_URLS")
RSSHUB_BAT_PATH = config.get("RSSHUB_BAT_PATH")

# Deepseek API 配置
DEEPSEEK_API_KEY = config.get("api_key")
DEEPSEEK_BASE_URL = config.get("base_url")



#RSS_BASE_URL = "http://rsshub.app/twitter/user/"
#RSS_URLS = [f"{RSS_BASE_URL}{username}" for username in USERNAME_LIST]

SEEN_FILE = 'seen.json'


USER_FOLDER_MAP = {username: username for username in USERNAME_LIST}

proxies = {
    #'http': 'http://127.0.0.1:7897',
    #'https': 'http://127.0.0.1:7897',
}
AVATAR_DIR = './avatar'  # <--- 新增头像目录

rt_pattern = re.compile(r'^(.+?)\s+RT\s*<br>', re.IGNORECASE)


# -------------------
# 工具函数
# -------------------


# 初始化客户端（建议放在全局配置区）

def translate_text(text: str) -> str:
    """
    使用 Deepseek API 进行文本翻译（日语 -> 简体中文）
    返回格式: 翻译后的文本字符串，失败时返回原文本
    """
    # 独立客户端实例（配置直连）
    DEEPSEEK_CLIENT = OpenAI(
        #api_key=,  # 建议改为从环境变量获取
        #base_url="https://api.deepseek.com",
        timeout=30,
        # 正确配置无代理的方式
    )

    if not DEEPSEEK_CLIENT.api_key:
        logging.warning("Deepseek API key 未配置，跳过翻译")
        return text

    system_prompt = (
        "你是一名专业翻译，请将日语内容精准翻译为简体中文。要求：\n"
        "1. 保持原有换行和格式\n"
        "2. 保留#话题标签和@提及,不进行翻译，同时#话题标签后必须保留空格\n"
        "3. 禁止添加解释内容\n"
        "4. 保留URL链接不变\n"
        "5. 处理日式颜文字不翻译\n"
        "6. 人名保留原文不翻译"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"请翻译以下内容：\n{text}"}
    ]

    max_retries = 3
    for attempt in range(max_retries):
        try:
            start_time = time.time()
            
            response = DEEPSEEK_CLIENT.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                temperature=0.2,
                top_p=0.3,
                max_tokens=8000,
                stream=False
            )

            if response.choices and response.choices[0].message.content:
                translated = response.choices[0].message.content.strip()
                translated = re.sub(r'^翻译[：:]?\s*', '', translated)
                translated = re.sub(r'\n{3,}', '\n\n', translated)
                
                logging.info(
                    f"翻译成功 | 耗时 {time.time()-start_time:.2f}s | "
                    f"Token使用: {response.usage.total_tokens}"
                )
                return translated

        except Exception as e:
            wait_time = 2 ** attempt
            logging.warning(f"翻译尝试 {attempt+1}/{max_retries} 失败: {str(e)}")
            time.sleep(wait_time)
    
    logging.error(f"所有重试失败 | 原文: {text[:100]}...")
    return text

def merge_consecutive_br(text: str) -> str:
    """合并文本中连续的<br>标签为单个<br>"""
    if not text:
        return text
    # 使用正则表达式将连续的<br>替换为单个<br>
    return re.sub(r'(?:<br>){2,}', '<br>', text)


def clean_html(text: str, author: str = None) -> str:
    if author:
        # 使用RSS中的作者名称，查找并移除description中该作者名之前的所有内容
        pattern = re.compile(fr'.*?{re.escape(author)}[:：]\s*(.*)', re.DOTALL)  
        match = pattern.match(text)
        if match:
            text = f"{author}: {match.group(1)}"
        else:
            pattern = re.compile(r'.*?([^>]+?)[:：]\s*(.*)', re.DOTALL)
            match = pattern.match(text)
            if match:
                text = f"{author}: {match.group(2)}"
        text = re.sub(fr'^{re.escape(author)}[:：]\s*', '', text)

    text = unescape(text)
    # 清理其他HTML标签，保留<br>
    text = re.sub(r'<(?!br).*?>', '', text)
    # 合并连续的<br>标签
    text = merge_consecutive_br(text)
    return text

# 在工具函数区添加
def extract_user_id(link: str) -> str:
    """从推特URL提取用户ID"""
    parsed = urlparse(link)
    path_segments = [s for s in parsed.path.split('/') if s]
    return path_segments[0] if path_segments else 'unknown_user'


# -------------------
# 下载和上传部分
# -------------------

UPLOAD_RECORD_FILE = 'uploaded_files.json'
DOWNLOAD_DIR = '.\女声优图库'

def load_seen():
    return set(json.load(open('seen.json', 'r')) if os.path.exists('seen.json') else [])

def save_seen(seen):
    with open('seen.json', 'w') as f:
        json.dump(list(seen), f, indent=2)

def load_uploaded():
    return set(json.load(open(UPLOAD_RECORD_FILE, 'r')) if os.path.exists(UPLOAD_RECORD_FILE) else [])

def save_uploaded(uploaded):
    with open(UPLOAD_RECORD_FILE, 'w') as f:
        json.dump(list(uploaded), f, indent=2)

# 特殊返回值，用于标识跳过了推特头像
SKIPPED_PROFILE_IMAGE_FLAG = "SKIPPED_PROFILE_IMAGE"

# 新增函数：根据作者名获取本地头像
def get_avatar_by_author(author: str) -> str:
    """通过作者名查找本地已存在的头像文件"""
    safe_author = re.sub(r'[\\/:*?"<>|]', '_', author)
    for ext in ['.jpg', '.jpeg', '.png', '.gif']:
        filename = f"{safe_author}{ext}"
        path = os.path.join(AVATAR_DIR, filename)
        if os.path.exists(path):
            logging.info(f"找到本地头像：{filename}")
            return path
    logging.warning(f"未找到本地头像：{author}")
    return None


def download_media(url: str, author: str) -> str:
    """
    下载媒体文件到指定目录，使用时间戳+原始文件名，自动检查重复。
    如果 URL 是推特头像，则跳过下载并返回 SKIPPED_PROFILE_IMAGE_FLAG。
    """
    # 新增：强制过滤非法字符
    author = re.sub(r'[\\/:*?"<>|]', '_', author)  # 确保目录名合法
    user_dir = os.path.join(DOWNLOAD_DIR, author)
    # 新增：检查是否为推特头像 URL
    if url.startswith("https://pbs.twimg.com/profile_images/"):
        logging.info(f"检测到推特头像链接，跳过下载和后续上传：{url}")
        return SKIPPED_PROFILE_IMAGE_FLAG
    user_dir = os.path.join(DOWNLOAD_DIR, author)
    os.makedirs(user_dir, exist_ok=True)

    # 修复URL编码问题
    url = unescape(url.replace('&amp;', '&'))
    parsed_url = urlparse(url)
    
    # 提取原始文件名（带扩展名）
    original_filename = os.path.basename(parsed_url.path)
    
    # 特殊处理推特图片格式
    if "pbs.twimg.com/media" in url:
        query_params = parse_qs(parsed_url.query)
        if 'format' in query_params and '.' not in original_filename:
            original_filename += f".{query_params['format'][0]}"
    
    # 生成时间戳前缀
    timestamp = datetime.now().strftime("%Y%m%d")
    
    # 构建完整文件名
    filename = f"{timestamp}_{original_filename}"
    path = os.path.join(user_dir, filename)
    
    # 检查文件是否已存在（基于完整文件名）
    if os.path.exists(path):
        logging.info(f"文件已存在，跳过下载：{filename}")
        return path

    # 下载重试逻辑
    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = requests.get(
                url,
                proxies=proxies,
                stream=True,
                timeout=15,
                verify=False,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            resp.raise_for_status()

            # 写入文件
            with open(path, 'wb') as f:
                for chunk in resp.iter_content(1024):
                    f.write(chunk)
            
            logging.info(f"下载成功：{filename}")
            return path

        except Exception as e:
            logging.warning(f"尝试 {attempt+1}/{max_retries} 失败：{str(e)[:100]}")
            time.sleep(1)
    
    logging.error(f"下载失败：{url}")
    return None


def upload_image(file_path, session_key):
    with open(file_path, 'rb') as img_file:
        files = {'img': img_file}
        data = {'sessionKey': session_key, 'type': 'group'}
        res = requests.post(f"{MIRAI_API_URL}/uploadImage", data=data, files=files)
        return res.json().get("imageId") if res.status_code == 200 else None

def upload_video(file_path, session_key, target_id):
    with open(file_path, 'rb') as video_file:
        files = {'file': video_file}
        data = {
            'sessionKey': session_key,
            'type': 'group',
            'target': str(target_id),
            'path': ''
        }
        res = requests.post(f"{MIRAI_API_URL}/file/upload", data=data, files=files)
        return res.json().get("data", {}).get("id") if res.status_code == 200 else None

def send_message(session_key, target, message_chain):
    return requests.post(f"{MIRAI_API_URL}/sendGroupMessage", json={
        "sessionKey": session_key,
        "target": target,
        "messageChain": message_chain
    })

# -------------------
# 用于重启rsshub的函数，若在docker部署，此部分需要重构
# -------------------
import socket

def is_rsshub_running(host='localhost', port=14607):
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except Exception:
        return False
    

WINDOW_TITLE = "RSSHUB_CMD_WINDOW"

def close_rsshub_window():
    def callback(hwnd, extra):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if WINDOW_TITLE in title:
                print(f"找到窗口：{title}，正在关闭")
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)

    win32gui.EnumWindows(callback, None)

def restart_rsshub():
    close_rsshub_window()
    time.sleep(2)
    # 用新的 cmd 窗口运行 rsshub
    subprocess.Popen(
        ["cmd.exe", "/c", "start", "", RSSHUB_BAT_PATH],
        creationflags=subprocess.CREATE_NEW_CONSOLE
    )
    print("已在新窗口中重启 RSSHub")

# -------------------
# html转图片的主函数
# -------------------

def Twitter_seiyuu():
    seen = load_seen()
    uploaded = load_uploaded()

    session_res = requests.post(f"{MIRAI_API_URL}/verify", json={"verifyKey": VERIFY_KEY})
    session_key = session_res.json().get("session")
    if not session_key:
        print("认证失败")
        return
    requests.post(f"{MIRAI_API_URL}/bind", json={"sessionKey": session_key, "qq": QQ_ID})

    if not is_rsshub_running():
        logging.warning("RSSHub 未运行，正在启动...")
        restart_rsshub()
        # 可选：等待启动完成
        time.sleep(10)


    for url in RSS_URLS:
        try:
            resp = requests.get(url, timeout=40)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            logging.info(f"抓取成功：{url}")
        except Exception as e:
            error_msg = str(e)
            logging.error(f"抓取失败：{url} -> {error_msg}")

            if ("503 Server Error" in error_msg) or \
               ("HTTPConnectionPool(host='localhost', port=14607): Read timed out." in error_msg):
                logging.warning("检测到 RSSHub 异常，尝试重启服务...")
                restart_rsshub()

            continue

        for item in root.findall('.//item'):
            # 在每个item开始时重置引用相关变量
            quoted_username = ''
            quote_block = ''
            quote_clean = ''
            quote_zh = ''
            avatar_quote = None

            link = item.findtext('link')
            if not link or link in seen:
                continue

            # 新增用户ID提取
            author_id = extract_user_id(link)
            title = item.findtext('title') or ''
            desc  = item.findtext('description') or ''
            # 修改后
            author_text = item.findtext('author') or 'unknown'  # 直接获取author标签内容
            author_clean = re.sub(r'<[^>]+>', '', author_text)
            # 清理HTML标签和特殊标记
            author = re.sub(r'<[^>]+>', '', author_text)
            #author = re.sub(r'\s*(?:OFFICIAL|のこと。?|[:：])\s*$', '', author)  # 移除结尾的OFFICIAL等标记和冒号
            author = re.sub(r'[\\/:*?"<>|\s]', '_', author.strip()).strip('_')

            pub_dt = item.findtext('pubDate') or ''
            categories = ['#' + c.text.strip() for c in item.findall('category') if c.text]
        
            
            # --- Step 1: 检测直接RT模式 ---
            # 检测是否为直接转发模式
            rt_match = rt_pattern.search(desc)

            if rt_match:  # 直接转发无评论的情况
                # 主推内容置空
                desc_main_only = ''
                # 添加清理逻辑
                author = re.sub(r'<[^>]+>', '', author)
                author = re.sub(r'[\\/:*?"<>|\s]', '_', author.strip()).strip('_')

                # 修改：直接从RT后的内容提取转推块的内容
                rt_content = desc[rt_match.end():]
                quote_block = rt_content

                    
                # 提取引用内容中的头像和用户名 (和普通推文使用相同的提取逻辑)
                quote_author_match = re.search(r'<img[^>]+?src="([^"]+?)"[^>]*?>([^:<]+?):', quote_block)
                if quote_author_match:
                    quote_avatar_url = unescape(quote_author_match.group(1))
                    quoted_username = quote_author_match.group(2).strip()
                    # 清理用户名，移除末尾的标记和冒号
                    quoted_username = re.sub(r'<[^>]+>', '', quoted_username)
                    #quoted_username = re.sub(r'\s*(?:OFFICIAL|のこと。?|[:：])\s*$', '', quoted_username)
                    quoted_username = re.sub(r'[\\/:*?"<>|\s]', '_', quoted_username.strip()).strip('_')
                    
                    logging.info(f"提取到RT用户信息 - 用户名: {quoted_username}")
                    logging.info(f"提取到RT用户信息 - 头像URL: {quote_avatar_url}")
                    
                    # 清理开头的作者名和冒号
                    quote_block = re.sub(fr'{re.escape(quoted_username)}[:：]\s*', '', quote_block)  # 移除用户名和冒号
                    quote_block = re.sub(r'^(?:<img[^>]+>)', '', quote_block)  # 只移除头像标签
                
                quote_clean = clean_html(quote_block)  # 直接清理RT后的内容
                quote_zh = translate_text(quote_clean) if quote_clean else ''  # 翻译RT内容
                logging.info(f"[{author}] 检测到直接转发模式，RT内容已清理并翻译。")
                
                # 下载引用用户头像
                if quote_avatar_url and quoted_username and quote_avatar_url.startswith("https://pbs.twimg.com/profile_images/"):
                    avatar_quote = get_avatar_by_author(quoted_username)
                    if not avatar_quote:
                        avatar_quote = download_avatar(quote_avatar_url, quoted_username)
                    avatar_quote = get_avatar_by_author(quoted_username)

                # 直接转发模式下，使用 author 下载用户头像
                avatar_path = get_avatar_by_author(author)
                if not avatar_path:  # 如果本地没有头像
                    # 尝试从desc中提取原作者头像URL
                    author_avatar_match = re.search(r'<img[^>]+?src="([^"]+?/profile_images/[^"]+?)"[^>]*?>', desc)
                    if author_avatar_match:
                        author_avatar_url = unescape(author_avatar_match.group(1))
                        logging.info(f"尝试下载原作者头像 - URL: {author_avatar_url}")
                        avatar_path = download_avatar(author_avatar_url, author)
                        if avatar_path:
                            logging.info(f"成功下载原作者头像: {avatar_path}")
                        else:
                            logging.warning(f"下载原作者头像失败: {author}")
                    else:
                        logging.warning(f"未找到原作者头像URL: {author}")

            else:
                # 1. 首先分离主推文内容和引用内容
                desc_main_only = re.split(r'<div class="rsshub-quote">', desc)[0]
                
                # 2. 使用更激进的清理方式 - 查找作者名和冒号后的位置，只保留之后的内容
                author_pattern = fr'{re.escape(author)}[:：]\s*'
                match = re.search(author_pattern, desc_main_only)
                if match:
                    desc_main_only = desc_main_only[match.end():]
                
                # 3. 移除所有可能的开头HTML标签和空白
                #desc_main_only = re.sub(r'^.*?<br>', '', desc_main_only, flags=re.DOTALL)  # 移除开头到第一个<br>之间的所有内容
                #desc_main_only = re.sub(r'^<[^>]+>', '', desc_main_only)  # 移除剩余的开头HTML标签
                desc_main_only = re.sub(fr'^.*?{re.escape(author_clean)}[:：]\s*', '', desc_main_only)
                desc_main_only = desc_main_only.lstrip()  # 移除开头空白

                # 从desc_main_only中提取头像URL
                m = re.search(r'<img[^>]+?src="([^"]+?/profile_images/[^"]+?)"[^>]*?>', desc)
                if m:
                    avatar_url = unescape(m.group(1))
                    # 先尝试获取本地头像
                    avatar_path = get_avatar_by_author(author)
                    # 如果本地没有,则下载
                    if not avatar_path:
                        avatar_path = download_avatar(avatar_url, author)
                else:
                    # 如果没有找到URL,仍然尝试从本地获取
                    avatar_path = get_avatar_by_author(author)

                # 优化引用块的提取
                quote_block_match = re.search(r'<div class="rsshub-quote">(.*?)</div>', desc, re.DOTALL)
                if quote_block_match:
                    quote_block = quote_block_match.group(1)
                    # 提取引用内容中的头像和用户名
                    quote_author_match = re.search(r'<img[^>]+?src="([^"]+?)"[^>]*?>([^:<]+?):', quote_block)
                    if quote_author_match:
                        quote_avatar_url = unescape(quote_author_match.group(1))
                        quoted_username = quote_author_match.group(2).strip()
                        # 清理用户名，移除末尾的标记和冒号
                        quoted_username = re.sub(r'<[^>]+>', '', quoted_username)
                        #quoted_username = re.sub(r'\s*(?:OFFICIAL|のこと。?|[:：])\s*$', '', quoted_username)
                        quoted_username = re.sub(r'[\\/:*?"<>|\s]', '_', quoted_username.strip()).strip('_')
                        
                        logging.info(f"提取到引用用户信息 - 用户名: {quoted_username}")
                        logging.info(f"提取到引用用户信息 - 头像URL: {quote_avatar_url}")
                        # 直接清理引用内容，包括用户名前缀
                        quote_block = re.sub(fr'{re.escape(quoted_username)}[:：]\s*', '', quote_block)  # 移除用户名和冒号
                        quote_block = re.sub(r'^<img[^>]+>', '', quote_block)  # 只移除头像标签
                        quote_clean = clean_html(quote_block)
                        quote_zh = translate_text(quote_clean) if quote_clean else ''
                             
                        # 下载引用用户头像
                        if quote_avatar_url.startswith("https://pbs.twimg.com/profile_images/"):
                            if not avatar_quote:
                                avatar_quote = download_avatar(quote_avatar_url, quoted_username)
                            avatar_quote = get_avatar_by_author(quoted_username)

                    
                    # 清理引用内容，使用更严格的正则表达式去除作者信息
                    quote_block = re.sub(r'^<img[^>]+>', '', quote_block)
                    quote_clean = clean_html(quote_block)
                    quote_zh = translate_text(quote_clean) if quote_clean else ''
                else:
                    quote_block = ''
                    quote_clean = ''
                    quote_zh = ''
                    quoted_username = ''
                    avatar_quote = None


            # 提取主推文字
            logging.info(f"[{author}] 开始翻译...")
            # --- 后续处理保持原有逻辑，但需确保desc_clean为空时处理 ---
            desc_clean = clean_html(desc_main_only) if desc_main_only else ''
            desc_zh = translate_text(desc_clean) if desc_clean else ''

            # --- Step 2: 提取转推块 html ---
            #quote_block_match = re.search(r'<div class="rsshub-quote">(.*?)</div>', desc, re.DOTALL)
            #quote_block = quote_block_match.group(1) if quote_block_match else ''


            logging.info(f"[{author}] 翻译完成.")


            # 提取转推用户头像（从 quote_block 中）
            logging.info("开始尝试提取转推头像链接")
            #avatar_quote = None
            #quoted_username = ''  # 添加此行，设置默认值
            matches = re.findall(r'<img[^>]+src="([^"]+pbs\.twimg\.com/profile_images/[^"]+)"[^>]*>\s*([^:<\n]+)', quote_block)
            

            if matches:
                avatar_quote_url = matches[0][0]  # 只获取头像URL，不覆盖用户名
                #quoted_username = re.sub(r'<[^>]+>', '', quoted_username)
                #quoted_username = re.sub(r'[\\/:*?"<>|\s]', '_', quoted_username.strip()).strip('_')

                logging.info(f"提取到转推头像链接：{avatar_quote_url}")
                logging.info(f"转推用户名用于命名头像文件：{quoted_username}")

                if quoted_username:  # 使用之前提取的用户名
                    avatar_quote = download_avatar(avatar_quote_url, quoted_username)

                if avatar_quote and avatar_quote != SKIPPED_PROFILE_IMAGE_FLAG and os.path.exists(avatar_quote):
                    logging.info(f"转推头像已存在或者下载成功：{avatar_quote}")
                else:
                    logging.warning(f"转推头像下载失败或无效路径：{avatar_quote}")
            else:
                logging.warning("未找到转推头像 <img> 标签或用户名")

            # 提取媒体链接，跳过隐藏元素
            media_urls = []
            seen_urls = set()
            for tag in re.findall(r'(<img[^>]+>)', desc):
                if tag.startswith('<img width="0" height="0" hidden="true"'):
                    continue
                u = re.search(r'src="([^"]+)"', tag).group(1)
                u = unescape(u)
                if u.startswith("https://pbs.twimg.com/profile_images/"):
                    continue  # 跳过头像
                if u not in seen_urls:
                    media_urls.append(u)
                    seen_urls.add(u)

            
            # 同理处理 <video>
            for tag in re.findall(r'(<video[^>]+>.*?</video>)', desc, re.DOTALL):
                if 'hidden="true"' in tag or 'style="display:none"' in tag:
                    continue
                src_match = re.search(r'src="([^"]+)"', tag)
                if src_match:
                    u = src_match.group(1).replace('&amp;', '&')
                    if u not in seen_urls:
                        media_urls.append(u)
                        seen_urls.add(u)

            media_paths = [p for u in media_urls if (p := download_media(u, author))]

    
            # 处理时间转换
            try:
                pub_dt_gmt = datetime.strptime(pub_dt, '%a, %d %b %Y %H:%M:%S %Z')
                pub_dt_utc = pub_dt_gmt.replace(tzinfo=timezone.utc)
            except ValueError:
                pub_dt_gmt = datetime.strptime(pub_dt.rstrip(' GMT'), '%a, %d %b %Y %H:%M:%S')
                pub_dt_utc = pub_dt_gmt.replace(tzinfo=timezone.utc)

            beijing_tz = timezone(timedelta(hours=8))
            pub_dt_beijing = pub_dt_utc.astimezone(beijing_tz)
            
            WEEKDAY_MAP = {
                0: "星期一", 1: "星期二", 2: "星期三", 3: "星期四",
                4: "星期五", 5: "星期六", 6: "星期天"
            }
            weekday_cn = WEEKDAY_MAP[pub_dt_beijing.weekday()]
            beijing_time_str = f"{weekday_cn}，{pub_dt_beijing.year}.{pub_dt_beijing.month:02}.{pub_dt_beijing.day:02} {pub_dt_beijing.strftime('%H:%M:%S')}"
            

            # # 生成消息文本
            # message_text = '\n'.join([
            #     f"👤 来自用户：{author}",                
            #     f"🌈 原文内容：\n{desc_clean}",
            #     f"🌈 翻译内容：\n{desc_zh}",
            #     '',
            #     f"🕒 发布时间：{beijing_time_str}",
            #     ' '.join(categories)
            # ])

            logging.info(f"[{author}] 开始生成消息图片...")
            # 生成图片并发送
            img_path = text_to_image_html(
                author=author,
                author_id=author_id,  # 新增
                desc_clean=desc_clean,
                desc_zh=desc_zh,
                quote_clean=quote_clean,
                quote_zh=quote_zh,
                categories=categories,
                beijing_time_str=beijing_time_str,
                avatar_path=avatar_path,
                avatar_quote=avatar_quote,
                quoted_username=quoted_username,
                is_retweet=bool(rt_pattern.search(desc))  # 新增参数
            )
            logging.info(f"[{author}] 消息图片生成完成: {img_path}")

            for target_id in TARGET_IDs_list:
                logging.info(f"[{author}] 开始向群 {target_id} 发送...")                
                # 优先发送图片消息
                if img_path:
                    img_message = upload_image(img_path, session_key)
                    if img_message:
                        # 立即发送图片和链接
                        send_message(session_key, target_id, [{"type": "Image", "imageId": img_message}])
                        send_message(session_key, target_id, [{"type": "Plain", "text": f"🔗 原文链接：{link}"}])
                        print(f"立即发送翻译后图片到群 {target_id} | 用户：{author}")
                    else:
                        logging.error(f"[{author}] 上传翻译图片到群 {target_id} 失败")

                # 处理媒体文件
                for media_path in media_paths:
                    filename = os.path.basename(media_path)
                    if filename in uploaded:
                        logging.info(f"[{author}] 媒体文件 {filename} 已上传过，跳过")
                        continue
                    if media_path == SKIPPED_PROFILE_IMAGE_FLAG:
                        continue

                    if media_path.lower().endswith(('.jpg', '.png', '.jpeg', '.gif')):
                        img_id = upload_image(media_path, session_key)
                        if img_id:
                            send_message(session_key, target_id, [{"type": "Image", "imageId": img_id}])
                    elif media_path.lower().endswith(('.mp4', '.mkv', '.avi', '.mov')):
                        vid_id = upload_video(media_path, session_key, target_id)
                        if vid_id:
                            send_message(session_key, target_id, [{"type": "File", "id": vid_id}])
                    else:
                        logging.warning(f"未识别的媒体类型：{media_path}")
            seen.add(link)
            logging.info(f"[{author}] 项目 {link} 处理完毕，标记为已看。")
            save_seen(seen)
            save_uploaded(uploaded)

# 消息图片化
def text_to_image_html(
    author: str,
    author_id: str,  # 新增参数
    desc_clean: str,
    desc_zh: str,
    quote_clean: str = '',
    quote_zh: str = '',
    categories: list = None,
    output_path: str = "./output",
    beijing_time_str=None,
    avatar_path: str = None,
    avatar_quote: str = None,
    quoted_username: str = '',
    is_retweet: bool = False  # 新增参数
):
    # 生成以时间戳命名的文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}.png"
    filepath = os.path.join(output_path, filename)
    output_path = os.path.abspath(output_path)
    filepath = os.path.join(output_path, filename)
    os.makedirs(output_path, exist_ok=True)

    # 移除相对路径转换函数，直接使用传入的绝对路径
    if avatar_path:
        avatar_path = os.path.abspath(avatar_path)
    if avatar_quote:
        avatar_quote = os.path.abspath(avatar_quote)


    # 内嵌 hashtag 高亮逻辑（#tag → 蓝色）
    def highlight_hashtags(text: str) -> str:
        # 修改正则表达式，排除 < 字符，这样就不会匹配到 <br>
        return re.sub(r'#([^<\s]+)', r'<span style="color:#1da1f2;">#\1</span>', text)




    # 对所有文本内容进行处理
    desc_clean = merge_consecutive_br(desc_clean)
    desc_zh = merge_consecutive_br(desc_zh)
    quote_clean = merge_consecutive_br(quote_clean)
    quote_zh = merge_consecutive_br(quote_zh)
    
    # 渲染 hashtag
    desc_clean = highlight_hashtags(desc_clean)
    desc_zh = highlight_hashtags(desc_zh)
    quote_clean = highlight_hashtags(quote_clean)
    quote_zh = highlight_hashtags(quote_zh)


    # 加载外部 HTML 模板

    # 获取当前脚本所在目录
    script_dir = Path(__file__).resolve().parent


    # 设置模板文件夹的绝对路径
    template_dir = os.path.join(script_dir, "html")

    # 新增：读取CSS和JS文件内容
    def load_resource(filename):
        resource_path = os.path.join(template_dir, filename)
        try:
            with open(resource_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            logging.error(f"加载资源失败: {resource_path} - {str(e)}")
            return ""
        
    # 加载CSS和JS内容
    css_content = load_resource("css2.css")
    js_content = load_resource("browser@4.js")

    # 加载模板
    env = Environment(loader=FileSystemLoader(template_dir))
    template_name = 'seiyuu.html' if quote_clean.strip() or quote_zh.strip() else 'no-quote.html'
    template = env.get_template(template_name)
    #avatar_base64 = image_to_base64(avatar_path) if avatar_path else None

    # 添加调试信息
    logging.info(f"生成图片参数 | author_id={author_id} | avatar_path={avatar_path}")



    # 准备翻译来源提示样式
    if not desc_clean.strip() and not desc_zh.strip() and not quote_clean.strip() and not quote_zh.strip():
        # 如果没有任何文字内容,则传入空白的 translate_source
        translate_source = ''
    elif is_retweet:
        translate_source = "已转推"
    else:
        translate_source = '''
    <div class="text-blue-500 text-sm flex items-center">
        由  
        <div class="flex flex-row items-center gap-2">
            <div class="h-9 w-9 text-gray-500" style="flex: none">
                <svg viewBox="0 0 30 30" width="30" height="30" xmlns="http://www.w3.org/2000/svg" class="fill-current">
                    <path id="path" d="M27.501 8.46875C27.249 8.3457 27.1406 8.58008 26.9932 8.69922C26.9434 8.73828 26.9004 8.78906 26.8584 8.83398C26.4902 9.22852 26.0605 9.48633 25.5 9.45508C24.6787 9.41016 23.9785 9.66797 23.3594 10.2969C23.2275 9.52148 22.79 9.05859 22.125 8.76172C21.7764 8.60742 21.4238 8.45312 21.1807 8.11719C21.0098 7.87891 20.9639 7.61328 20.8779 7.35156C20.8242 7.19336 20.7695 7.03125 20.5879 7.00391C20.3906 6.97266 20.3135 7.13867 20.2363 7.27734C19.9258 7.84375 19.8066 8.46875 19.8174 9.10156C19.8447 10.5234 20.4453 11.6562 21.6367 12.4629C21.7725 12.5547 21.8076 12.6484 21.7646 12.7832C21.6836 13.0605 21.5869 13.3301 21.501 13.6074C21.4473 13.7852 21.3662 13.8242 21.1768 13.7461C20.5225 13.4727 19.957 13.0684 19.458 12.5781C18.6104 11.7578 17.8438 10.8516 16.8877 10.1426C16.6631 9.97656 16.4395 9.82227 16.207 9.67578C15.2314 8.72656 16.335 7.94727 16.5898 7.85547C16.8574 7.75977 16.6826 7.42773 15.8193 7.43164C14.957 7.43555 14.167 7.72461 13.1611 8.10938C13.0137 8.16797 12.8594 8.21094 12.7002 8.24414C11.7871 8.07227 10.8389 8.0332 9.84766 8.14453C7.98242 8.35352 6.49219 9.23633 5.39648 10.7441C4.08105 12.5547 3.77148 14.6133 4.15039 16.7617C4.54883 19.0234 5.70215 20.8984 7.47559 22.3633C9.31348 23.8809 11.4307 24.625 13.8457 24.4824C15.3125 24.3984 16.9463 24.2012 18.7881 22.6406C19.2529 22.8711 19.7402 22.9629 20.5498 23.0332C21.1729 23.0918 21.7725 23.002 22.2373 22.9062C22.9648 22.752 22.9141 22.0781 22.6514 21.9531C20.5186 20.959 20.9863 21.3633 20.5605 21.0371C21.6445 19.752 23.2783 18.418 23.917 14.0977C23.9668 13.7539 23.9238 13.5391 23.917 13.2598C23.9131 13.0918 23.9512 13.0254 24.1445 13.0059C24.6787 12.9453 25.1973 12.7988 25.6738 12.5352C27.0557 11.7793 27.6123 10.5391 27.7441 9.05078C27.7637 8.82422 27.7402 8.58789 27.501 8.46875ZM15.46 21.8613C13.3926 20.2344 12.3906 19.6992 11.9766 19.7227C11.5898 19.7441 11.6592 20.1875 11.7441 20.4766C11.833 20.7617 11.9492 20.959 12.1123 21.209C12.2246 21.375 12.3018 21.623 12 21.8066C11.334 22.2207 10.1768 21.668 10.1221 21.6406C8.77539 20.8477 7.64941 19.7988 6.85547 18.3652C6.08984 16.9844 5.64453 15.5039 5.57129 13.9238C5.55176 13.541 5.66406 13.4062 6.04297 13.3379C6.54199 13.2461 7.05762 13.2266 7.55664 13.2988C9.66602 13.6074 11.4619 14.5527 12.9668 16.0469C13.8262 16.9004 14.4766 17.918 15.1465 18.9121C15.8584 19.9688 16.625 20.9746 17.6006 21.7988C17.9443 22.0879 18.2197 22.3086 18.4824 22.4707C17.6895 22.5586 16.3652 22.5781 15.46 21.8613ZM16.4502 15.4805C16.4502 15.3105 16.5859 15.1758 16.7568 15.1758C16.7949 15.1758 16.8301 15.1836 16.8613 15.1953C16.9033 15.2109 16.9424 15.2344 16.9727 15.2695C17.0273 15.3223 17.0586 15.4004 17.0586 15.4805C17.0586 15.6504 16.9229 15.7852 16.7529 15.7852C16.582 15.7852 16.4502 15.6504 16.4502 15.4805ZM19.5273 17.0625C19.3301 17.1426 19.1328 17.2129 18.9434 17.2207C18.6494 17.2344 18.3281 17.1152 18.1533 16.9688C17.8828 16.7422 17.6895 16.6152 17.6074 16.2168C17.5732 16.0469 17.5928 15.7852 17.623 15.6348C17.6934 15.3105 17.6152 15.1035 17.3877 14.9141C17.2012 14.7598 16.9658 14.7188 16.7061 14.7188C16.6094 14.7188 16.5205 14.6758 16.4541 14.6406C16.3457 14.5859 16.2568 14.4512 16.3418 14.2852C16.3691 14.2324 16.501 14.1016 16.5322 14.0781C16.8838 13.877 17.29 13.9434 17.666 14.0938C18.0146 14.2363 18.2773 14.498 18.6562 14.8672C19.0439 15.3145 19.1133 15.4395 19.334 15.7734C19.5078 16.0371 19.667 16.3066 19.7754 16.6152C19.8408 16.8066 19.7559 16.9648 19.5273 17.0625Z" fill-rule="nonzero" fill="#4D6BFE"></path>
                </svg>
            </div>
        </div>
        翻译自日语
    </div>
    '''

    # 渲染 HTML
    rendered_html = template.render(
        author=author,
        author_id=author_id,  # 新增
        desc_clean=desc_clean,
        desc_zh=desc_zh,
        quote_clean=quote_clean,
        quote_zh=quote_zh,
        beijing_time_str=beijing_time_str,
        categories=categories,
        avatar_path=avatar_path,
        avatar_quote=avatar_quote,
        quoted_username=quoted_username,
        translate_source=translate_source,
        css_content=css_content,
        js_content=js_content  # 新增参数
    )

    # Step 1: 生成大尺寸截图
    hti = Html2Image(output_path=output_path, size=(2160, 8000))
    try:
        hti.screenshot(html_str=rendered_html, save_as=filename)
    except Exception as e:
        logging.error(f"HTML 转图片失败：{e}")
        return None

    # Step 2: 裁剪白色空白区域
    img = Image.open(filepath)
    gray = img.convert('L')
    bbox = gray.point(lambda x: 0 if x == 255 else 255).getbbox()

    if bbox:
        img = img.crop(bbox)
        img.save(filepath)

    return filepath




# -------------------
# 新增：下载头像函数
# -------------------
def modify_avatar_url(url: str) -> str:
    """将头像URL中的 _normal 替换为 _400x400 以获取高清版本"""
    if not url:
        return url
    return url.replace('_normal.', '_400x400.')

def download_avatar(url: str, author: str) -> str:
    if not url:
        logging.warning(f"头像URL为空，无法下载：{author}")
        return None

    # 修改URL获取高清版本
    url = modify_avatar_url(url)

    os.makedirs(AVATAR_DIR, exist_ok=True)  # 确保 avatar 目录存在

    url = unescape(url.replace('&amp;', '&'))
    parsed_url = urlparse(url)
    ext = os.path.splitext(parsed_url.path)[1] or ".png"  # 保留扩展名或默认为.png
    filename = f"{author}{ext}"
    path = os.path.join(AVATAR_DIR, filename)

    # 如果头像文件已存在，则直接返回路径，不重新下载
    if os.path.exists(path):
        logging.info(f"头像已存在，跳过下载：{filename}")
        return path

    try:
        resp = requests.get(
            url,
            proxies=proxies,
            stream=True,
            timeout=15,
            verify=False,
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        resp.raise_for_status()
        with open(path, 'wb') as f:
            for chunk in resp.iter_content(1024):
                f.write(chunk)
        logging.info(f"头像下载成功：{filename}")
        return path
    except Exception as e:
        logging.warning(f"头像下载失败：{str(e)}")
        return None

def image_to_base64(image_path: str) -> Optional[str]:
    if not image_path or not os.path.exists(image_path):
        return None
    try:
        with open(image_path, "rb") as img_file:
            encoded = base64.b64encode(img_file.read()).decode("utf-8")
            ext = os.path.splitext(image_path)[1][1:].lower()  # jpg/png/svg 等
            return f"data:image/{ext};base64,{encoded}"
    except Exception as e:
        logging.warning(f"Base64 编码失败：{e}")
        return None


def start_immediate_tasks():
# 启动时立即扫描更新
    Twitter_seiyuu()

# 调度入口
if __name__ == '__main__':
    schedule.every(1).minutes.do(Twitter_seiyuu)
    # 启动时立即执行任务
    start_immediate_tasks()
    while True:
        schedule.run_pending()
        time.sleep(1)
