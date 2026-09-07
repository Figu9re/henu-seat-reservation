# -*- coding: utf-8 -*-
# henu_client.py —— 请求模块：HTTP 收发 + 预约请求加密 + cookie 管理
# 被 henu_login.py（登录模块）和 henu_main.py（主程序）复用，自身不依赖其他模块
import os
import urllib.request
import http.cookiejar
import json
import datetime
import time
import base64
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

api_base = "https://zwyy.henu.edu.cn"

# 凭证文件夹（token/cookie 集中存放，已被 .gitignore 排除）
CRED_DIR = ".credentials"
os.makedirs(CRED_DIR, exist_ok=True)

COOKIE_FILE = os.path.join(CRED_DIR, "henu_cookies.txt")

# cookie 设置（尽量加载已保存的）
cj = http.cookiejar.LWPCookieJar()
try:
    cj.load(COOKIE_FILE, ignore_discard=True, ignore_expires=True)
except Exception:
    pass
cookie_support = urllib.request.HTTPCookieProcessor(cj)
opener = urllib.request.build_opener(cookie_support, urllib.request.HTTPHandler)
urllib.request.install_opener(opener)

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def setup_account_files(username):
    """按学号切换 cookie 文件并重建 opener（多账号凭证互不覆盖）"""
    global COOKIE_FILE, cj, cookie_support, opener
    COOKIE_FILE = os.path.join(CRED_DIR, "henu_cookies_%s.txt" % username)
    cj = http.cookiejar.LWPCookieJar()
    try:
        cj.load(COOKIE_FILE, ignore_discard=True, ignore_expires=True)
    except Exception:
        pass
    cookie_support = urllib.request.HTTPCookieProcessor(cj)
    opener = urllib.request.build_opener(cookie_support, urllib.request.HTTPHandler)
    urllib.request.install_opener(opener)


# 通用的 POST JSON 请求封装（带 token 时加 authorization 头）
# 网络瞬时抖动（超时/断连）会自动重试，避免抢座过程中因一次抖动崩溃
def post_json(url, data, token=None, retries=3):
    body = json.dumps(data).encode('utf-8')
    h = dict(headers)
    h["Content-Type"] = "application/json"
    # 与预约站点前端 axios 请求保持一致，部分登录接口会校验该 AJAX 标记。
    h["Accept"] = "application/json, text/plain, */*"
    h["X-Requested-With"] = "XMLHttpRequest"
    if token:
        h["authorization"] = "bearer" + token
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=h, method="POST")
            resp = urllib.request.urlopen(req, timeout=10)
            return json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                print("请求失败(%s) %s，%.1f 秒后重试..." % (
                    type(e).__name__, url.rsplit("/", 1)[-1], attempt + 1))
                time.sleep(attempt + 1)
    raise last_err


# 通用的带重试 HTTP 请求：登录流程的裸 urlopen 也走它，避免瞬时网络抖动时崩溃
def http_request(url, data=None, retries=3, timeout=10):
    """发送 HTTP 请求并返回 response 对象。data 为 dict → JSON POST；bytes → form POST；None → GET。"""
    h = dict(headers)
    body = None
    if isinstance(data, dict):
        h["Content-Type"] = "application/json"
        body = json.dumps(data).encode('utf-8')
    elif data is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
        body = data
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, data=body, headers=h, method="POST" if data is not None else "GET")
            return urllib.request.urlopen(req, timeout=timeout)
        except Exception as e:
            last_err = e
            reason = getattr(e, "reason", None)
            print(
                "请求失败(%s) %s，原因: %r，底层原因: %r，%.1f 秒后重试..."
                % (
                    type(e).__name__,
                    url.rsplit("/", 1)[-1],
                    e,
                    reason,
                    attempt + 1,
                )
            )
            if attempt < retries - 1:
                time.sleep(attempt + 1)
    raise last_err


# ============ 预约请求体加密（对应前端 iz + RC.encrypt） ============
# 关键：az 的 case 41 返回 "YYYYMMDD"（无中文），key = 日期+反转 = 16 字节 → 标准 AES-128-CBC

def make_encrypt_key():
    d = datetime.date.today()
    today = "%04d%02d%02d" % (d.year, d.month, d.day)
    return today + today[::-1]


def encrypt_aesjson(data):
    key = make_encrypt_key().encode('utf-8')          # 16 字节 → AES-128
    iv = "ZZWBKJ_ZHIHUAWEI".encode('utf-8')           # 固定 16 字节 IV
    plaintext = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode('utf-8')
    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(pad(plaintext, AES.block_size))
    return base64.b64encode(encrypted).decode('utf-8')
