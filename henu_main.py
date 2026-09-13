# -*- coding: utf-8 -*-
# henu_main.py —— 主程序：每天 6:30 开放"明天"预约时，为指定账号抢约指定座位
#
# 用法：
#   python henu_main.py --account <学号>              正常模式：登录后等到开放后 0.1 秒，连续重试抢座
#   python henu_main.py --account <学号> --now        测试模式：立即发一次请求（不等待），用于验证流程
#
# 账号配置在 henu_accounts.json：每个账号一行（name / username / area / seat_no）
# 密码存 Windows 凭据管理器（keyring），某账号首次运行时交互输入一次
#
# 抢座策略：开放前完成请求参数获取，开放后 0.1 秒发送首个请求，之后每隔 0.25 秒
#   重试一次，直到收到明确结果（code=0 成功 / 已预约过）或开放后 6 秒仍未成功才停止。
#   每次重试复用同一份 request_data，仅重新加密并发送，不重新查询座位信息。
#
# 模块分工：
#   henu_client.py  请求模块：HTTP 收发 + 预约请求加密
#   henu_login.py   登录模块：CAS 登录 + token 凭证 + keyring 密码
import argparse
import datetime
import json
import os
import time

# 无论从哪个目录启动，都先切到脚本所在目录，保证相对路径（配置文件、token）正确
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import henu_client
import henu_login

SEND_HOUR, SEND_MIN, SEND_SEC = 6, 30, 0      # 每天 6:30:00 开放"明天"预约
RETRY_START_OFFSET = 0.1      # 开放后 0.1 秒发送首个请求
RETRY_INTERVAL = 0.25         # 每 0.25 秒重试一次
RETRY_TIMEOUT = 60             # 最多持续到开放后 60 秒


def load_config():
    with open("henu_accounts.json", encoding="utf-8") as f:
        return json.load(f)["accounts"]


def pick_account(accounts, username):
    for a in accounts:
        if a["username"] == username:
            return a
    raise SystemExit("配置文件中没有学号 %s，请先填入 henu_accounts.json" % username)


def ensure_login(username):
    token = henu_login.load_token()
    if token and henu_login.token_valid(token):
        print("使用已保存的凭证")
        return token
    print("凭证无效，从凭据管理器取密码自动登录")
    return henu_login.login(username, henu_login.get_password(username))


def wait_until(moment):
    """阻塞直到 moment 时刻（分段 sleep，能容忍长时间等待）"""
    while True:
        remain = (moment - datetime.datetime.now()).total_seconds()
        if remain <= 0:
            return
        time.sleep(min(3600, remain))


def build_confirm_request(token, account, day):
    """定位区域 + 找目标座位 + 构造 confirm 请求体。失败返回 None。"""
    area_name = account["area"]
    seat_no = account["seat_no"]
    print("\n=== 目标: %s（明天）| 区域: %s | 座位号: %s ===" % (day, area_name, seat_no))

    top = henu_client.post_json(henu_client.api_base + "/v4/space/pcTopFor", {"day": day}, token)
    campus_id = None
    floor_ids = []
    for c in top["data"]["list"]:
        if c["name"] == "金明校区":
            campus_id = c["id"]
            floor_ids = [ch["id"] for ch in c.get("children", [])]
            break
    if not (campus_id and floor_ids):
        print("定位校区/楼层失败")
        return None

    pick = henu_client.post_json(henu_client.api_base + "/v4/space/pick", {
        "premisesIds": [campus_id], "categoryIds": [],
        # 账号配置只指定区域名；跨校区楼层目录查找，避免楼层写死导致找不到目标区域。
        "storeyIds": floor_ids, "boutiqueIds": [], "date": day,
    }, token)
    area_id = None
    for a in pick.get("data", {}).get("area", []):
        if a["name"] == area_name:
            area_id = a["id"]
            print("区域: %s id=%s 空闲=%s" % (area_name, area_id, a.get("free_num")))
            break
    if not area_id:
        print("未找到区域:", area_name)
        return None

    mp = henu_client.post_json(henu_client.api_base + "/v4/Space/map", {"id": area_id}, token)
    d_conf = next((d for d in mp.get("data", {}).get("date", {}).get("list", [])
                   if d.get("day") == day), None)
    if not d_conf:
        print("未找到 date 配置")
        return None
    t0 = d_conf["times"][0]
    reserve_type = mp["data"]["date"].get("reserveType")
    print("reserveType:", reserve_type, "| 时段:", t0["start"], "-", t0["end"])

    seat_resp = henu_client.post_json(henu_client.api_base + "/v4/Space/seat", {
        "id": area_id, "day": day, "label_id": "",
        "start_time": t0["start"], "end_time": t0["end"],
        "begdate": "", "enddate": "",
    }, token)
    lst = seat_resp.get("data", {}).get("list", [])
    chosen = next((s for s in lst if int(s["no"]) == seat_no), None)
    if not chosen:
        print("未找到", seat_no, "号座位")
        return None
    print("目标座位:", chosen["id"], chosen["no"], chosen.get("status_name"))

    confirm_data = {
        "seat_id": chosen["id"], "segment": "", "day": day,
        "start_time": "", "end_time": "",
    }
    if reserve_type == "1":
        confirm_data["segment"] = t0["id"]
    elif reserve_type == "2":
        confirm_data["end_time"] = t0.get("end", "")
    elif reserve_type == "3":
        def fmt(t):
            return t[11:16] if len(t) >= 16 else t
        confirm_data["start_time"] = fmt(d_conf.get("def_start_time", ""))
        confirm_data["end_time"] = fmt(d_conf.get("def_end_time", ""))
    print("请求体:", json.dumps(confirm_data, ensure_ascii=False))
    return confirm_data


def send_confirm(token, confirm_data):
    """发送预约请求，并返回响应及接收时间。"""
    aesjson = henu_client.encrypt_aesjson(confirm_data)
    res = henu_client.post_json(henu_client.api_base + "/v4/space/confirm", {"aesjson": aesjson}, token)
    received_at = datetime.datetime.now()
    msg = res.get("message") or res.get("msg") or ""
    print("code:", res.get("code"), "| msg:", msg)
    return res, received_at


def log_response_elapsed(target_time, received_at):
    elapsed = (received_at - target_time).total_seconds()
    print("响应耗时（相对开放时间）: %.3f 秒" % elapsed)


def is_final(res):
    """是否已得到明确结果（成功或已预约过）"""
    code = res.get("code")
    msg = res.get("message") or res.get("msg") or ""
    if code == 0:
        print("=== 预约成功 ===")
        return True
    if ("已存在" in msg) or ("重复预约" in msg) or ("已预约" in msg):
        print("=== 该时段已预约过（视为达成） ===")
        return True
    return False


def reserve_with_retry(token, account, max_attempts=None, request_data=None,
                       target_time=None):
    """开放瞬间连续重试抢座。"""
    day = str(datetime.date.today() + datetime.timedelta(days=1))
    if request_data is None:
        while True:
            try:
                request_data = build_confirm_request(token, account, day)
            except Exception as e:
                print("定位请求异常(%s)，1 秒后重试..." % type(e).__name__)
                request_data = None
            if request_data:
                break
            print("定位/构造失败，1 秒后重试...")
            time.sleep(1)

    if target_time is None:
        target_time = datetime.datetime.now()
    deadline = target_time + datetime.timedelta(seconds=RETRY_TIMEOUT)
    attempts = 0
    while True:
        try:
            res, received_at = send_confirm(token, request_data)
            if target_time is not None:
                log_response_elapsed(target_time, received_at)
        except Exception as e:
            print("预约请求异常(%s)，继续重试..." % type(e).__name__)
            res = None
        if res is not None and is_final(res):
            return
        attempts += 1
        if max_attempts is not None and attempts >= max_attempts:
            print("（测试模式，只发 %s 次）" % max_attempts)
            return
        if datetime.datetime.now() >= deadline:
            print("=== 开放后 %s 秒仍未成功，停止 ===" % RETRY_TIMEOUT)
            return
        time.sleep(RETRY_INTERVAL)


def main(account, username):
    now = datetime.datetime.now()
    open_at = now.replace(hour=SEND_HOUR, minute=SEND_MIN, second=SEND_SEC, microsecond=0)
    if open_at <= now:
        open_at += datetime.timedelta(days=1)
    login_at = open_at - datetime.timedelta(seconds=60)
    print("当前时间:", now.strftime("%Y-%m-%d %H:%M:%S"))
    print("开放时刻:", open_at.strftime("%Y-%m-%d %H:%M:%S"))
    print("抢座账号:", account["name"], "| 座位:", account["seat_no"])

    wait_until(login_at)
    print("\n时间到，开始登录...")
    token = ensure_login(username)
    day = str(open_at.date() + datetime.timedelta(days=1))
    # 座位信息和 confirm 参数必须在开放前准备好；开放后的关键路径只做加密和 POST。
    # 开放前服务器可能尚未放出"明天"数据，给参数获取本身留到 6:30:00.1 的短重试窗口。
    build_deadline = open_at + datetime.timedelta(seconds=RETRY_START_OFFSET)
    request_data = None
    while datetime.datetime.now() < build_deadline:
        try:
            request_data = build_confirm_request(token, account, day)
        except Exception as e:
            print("参数获取异常(%s)，重试..." % type(e).__name__)
            request_data = None
        if request_data:
            break
        time.sleep(1)
    if not request_data:
        raise SystemExit("开放前未能构造预约请求")
    print("request_data:", json.dumps(request_data, ensure_ascii=False))

    wait_until(build_deadline)
    print("到点，开始连续抢座")
    reserve_with_retry(
        token, account, request_data=request_data,
        target_time=open_at + datetime.timedelta(seconds=RETRY_START_OFFSET))


def run_cli():
    parser = argparse.ArgumentParser(description="河大图书馆抢座脚本")
    parser.add_argument("--account", required=True, help="学号，必须在 henu_accounts.json 里")
    parser.add_argument("--now", action="store_true", help="测试模式：立即发一次请求，不等待开放时刻")
    args = parser.parse_args()

    accounts = load_config()
    account = pick_account(accounts, args.account)
    henu_login.setup_account(args.account)

    if args.now:
        print("[测试模式] 立即发送（跳过等待）")
        token = ensure_login(args.account)
        reserve_with_retry(token, account, max_attempts=1)
    else:
        main(account, args.account)


if __name__ == "__main__":
    run_cli()
