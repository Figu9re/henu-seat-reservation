# -*- coding: utf-8 -*-
# henu_login.py —— 登录模块：CAS 登录、token 凭证持久化/校验、keyring 密码管理
# 依赖 henu_client.py（请求模块），被 henu_main.py（主程序）调用
import os
import urllib.parse
import getpass
import random
import json
import datetime
import re
import base64
import keyring
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

import henu_client

KEYRING_SERVICE = "henu_seat_reservation"
login_page_url = "https://ids.henu.edu.cn/authserver/login?service=https%3A%2F%2Fzwyy.henu.edu.cn%2Fv4%2Flogin%2Fcas"
# 登录表单会把 service 保留在 action 查询参数中；缺少它时 CAS 不会签发业务 ticket。
login_post_url = login_page_url
TOKEN_FILE = os.path.join(henu_client.CRED_DIR, "henu_token.json")

# AES 加密的字符集（对应 JS 里的 $aes_chars）
AES_CHARS = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"


def random_string(n):
    return "".join(random.choice(AES_CHARS) for _ in range(n))


def encrypt_password(password, salt):
    """登录密码加密（对应前端 encryptPassword）：AES-CBC + PKCS7，key=salt，明文=64随机+密码"""
    key = salt.encode('utf-8')
    iv = random_string(16).encode('utf-8')
    plaintext = (random_string(64) + password).encode('utf-8')
    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(pad(plaintext, AES.block_size))
    return base64.b64encode(encrypted).decode('utf-8')


def get_password(username):
    """取该学号的密码：keyring 按学号读；读不到则交互输入并存。"""
    pwd = keyring.get_password(KEYRING_SERVICE, username)
    if pwd:
        return pwd
    pwd = getpass.getpass("请输入学号 %s 的密码（输入时不显示）: " % username)
    keyring.set_password(KEYRING_SERVICE, username, pwd)
    print("密码已存入 Windows 凭据管理器，下次自动读取")
    return pwd


def setup_account(username):
    """按学号切换 token/cookie 文件（多账号凭证互不覆盖），主程序启动时调用"""
    global TOKEN_FILE
    TOKEN_FILE = os.path.join(henu_client.CRED_DIR, "henu_token_%s.json" % username)
    henu_client.setup_account_files(username)


def save_captcha_image(username):
    """下载当前 CAS 会话验证码，返回本地临时图片路径。"""
    captcha_url = "https://ids.henu.edu.cn/authserver/getCaptcha.htl?_=%s" % datetime.datetime.now().timestamp()
    response = henu_client.http_request(captcha_url)
    path = os.path.join(henu_client.CRED_DIR, "henu_captcha_%s.png" % username)
    with open(path, "wb") as f:
        f.write(response.read())
    return path


def load_token():
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return json.load(f).get("token")
    except Exception:
        return None


def save_token(token, username):
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "username": username,
            "token": token,
            "saved_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }, f, ensure_ascii=False, indent=2)


def token_valid(token):
    # space/index 和 pcTopFor 对过期 token 也返回 code=0，不校验登录态；
    # 用 space/pick 严格验证：未登录时返回 code=10001
    try:
        today = str(datetime.date.today())
        top = henu_client.post_json(henu_client.api_base + "/v4/space/pcTopFor", {"day": today}, token)
        if top.get("code") != 0 or not top.get("data", {}).get("list"):
            return False
        campus = top["data"]["list"][0]["id"]
        r = henu_client.post_json(henu_client.api_base + "/v4/space/pick", {
            "premisesIds": [campus], "categoryIds": [],
            "storeyIds": [], "boutiqueIds": [], "date": today,
        }, token)
        return r.get("code") == 0
    except Exception:
        return False


# ============ 登录（CAS 完整流程） ============

def login(username=None, password=None):
    response = henu_client.http_request(login_page_url)
    result = response.read().decode('utf-8')
    print("GET 登录页 状态码:", response.status)
    final_url = response.url

    cas = None
    m = re.search(r"[?&]cas=([0-9a-fA-F]+)", final_url)
    if m:
        # cookie 会话仍有效：CAS 直接重定向到业务系统并附 cas ticket，无需密码
        cas = m.group(1)
        print("已登录，直接从重定向 URL 取得 cas ticket:", cas)

    if cas is None:
        # 会话失效，走完整的登录表单流程
        soup = BeautifulSoup(result, "html.parser")

        def find_input_value(name):
            inp = soup.find("input", {"name": name})
            return inp.get("value", "") if inp else ""

        execution = find_input_value("execution")
        lt = find_input_value("lt")
        salt_inp = soup.find("input", {"id": "pwdEncryptSalt"})
        pwd_encrypt_salt = salt_inp.get("value", "") if salt_inp else ""

        if username is None:
            username = input("请输入学号: ")
        if password is None:
            password = getpass.getpass("请输入密码（输入时不显示）: ")

        need_captcha = False
        try:
            check_data = urllib.parse.urlencode({"username": username}).encode('utf-8')
            check_resp = henu_client.http_request(
                "https://ids.henu.edu.cn/authserver/checkNeedCaptcha.htl",
                data=check_data).read().decode('utf-8')
            print("\ncheckNeedCaptcha 响应:", check_resp)
            try:
                need_captcha = bool(json.loads(check_resp).get("isNeed"))
            except (TypeError, ValueError):
                need_captcha = '"isNeed":true' in check_resp.replace(" ", "")
        except Exception as e:
            print("checkNeedCaptcha 调用失败:", e)

        captcha = ""
        if need_captcha:
            try:
                print("验证码图片:", save_captcha_image(username))
            except Exception as e:
                print("验证码图片下载失败:", e)
            captcha = input("需要验证码，请输入验证码: ")

        encrypted_pwd = encrypt_password(password, pwd_encrypt_salt)
        post_data = {
            'username': username,
            # 页面上的 id 是 saltPassword，但提交字段名仍是 password。
            'password': encrypted_pwd,
            'captcha': captcha,
            'execution': execution,
            '_eventId': 'submit',
            'lt': lt,
            'dllt': 'generalLogin',
            'cllt': 'userNameLogin',
        }
        encoded = urllib.parse.urlencode(post_data).encode('utf-8')
        response = henu_client.http_request(login_post_url, data=encoded)
        final_url = response.url
        result = response.read().decode('utf-8')
        print("\n=== 登录响应 ===")
        print("状态码:", response.status)
        print("最终URL:", final_url)

        m = re.search(r"[?&]cas=([0-9a-fA-F]+)", final_url)
        if not m:
            print("未找到 cas ticket，请确认登录是否成功")
            raise SystemExit(1)
        cas = m.group(1)
        print("cas ticket:", cas)

    res = henu_client.post_json(henu_client.api_base + "/v4/login/user", {"cas": cas})
    print("\n=== login/user 返回 ===")
    print(json.dumps(res, ensure_ascii=False, indent=2)[:800])
    if res.get("code") != 0:
        print("换取 token 失败:", res.get("msg"))
        raise SystemExit(1)
    token = res["data"]["member"]["token"]
    if username is None:
        username = res["data"]["member"].get("id")

    save_token(token, username)
    try:
        henu_client.cj.save(henu_client.COOKIE_FILE, ignore_discard=True, ignore_expires=True)
    except Exception:
        pass
    print("已保存登录凭证到:", TOKEN_FILE)
    return token
