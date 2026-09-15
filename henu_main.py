# -*- coding: utf-8 -*-
# henu_main.py —— 主程序：每天 6:30 开放"明天"预约时，为指定账号抢约指定座位
#
# 用法：
#   python henu_main.py --account <学号>              正常模式：登录后等到开放后 0.1 秒，连续抢座
#   python henu_main.py --account <学号> --now        测试模式：查座并构造 confirm 后等 30 秒再抢座
#
# 账号配置在 henu_accounts.json：每个账号一行（name / username / area / seat_no）
# seat_no 可以是整数，也可以是按优先级排列的列表，例如 [20, 19, 18]
# 密码存 Windows 凭据管理器（keyring），某账号首次运行时交互输入一次
#
# 抢座策略：开放前为所有目标座位构造 confirm 请求体。开放后 0.1 秒开火，
#   10 个 worker 重叠 RTT；仅第一枪按 WORKER_STAGGER 错开，回包后立即再发。
#   每个 worker 复用一条 HTTPS 长连接。确认请求 retries=1、timeout=3。
#   code=0 成功停止；code=1 且已存在预约则停止；code=1 当前座位不可约则立刻改抢下一个；
#   其它结果继续抢当前座位。开火后 60 秒仍未成功则停止。
#
# 模块分工：
#   henu_client.py  请求模块：HTTP 收发 + 预约请求加密
#   henu_login.py   登录模块：CAS 登录 + token 凭证 + keyring 密码
import argparse
import datetime
import json
import os
import queue
import threading
import time

# 无论从哪个目录启动，都先切到脚本所在目录，保证相对路径（配置文件、token）正确
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import henu_client
import henu_login

SEND_HOUR, SEND_MIN, SEND_SEC = 6, 30, 0      # 每天 6:30:00 开放"明天"预约
RETRY_START_OFFSET = 0.1      # 开放后 0.1 秒发送首个请求
RETRY_TIMEOUT = 60             # 最多持续到开放后 60 秒
CONCURRENCY = 10                # 同时在途的确认请求数，用来重叠 RTT
WORKER_STAGGER = 0.08          # 仅第一枪错开：worker i 等待 (i-1)*WORKER_STAGGER 秒
CONFIRM_TIMEOUT = 3            # 确认请求超时；挂死的 worker 不能占满窗口
LOG_DIR = "logs"               # 每次运行一份 jsonl，热路径只入队
NOW_FIRE_DELAY = 30            # 仅 --now：构造 confirm 后等待再开火


def load_config():
    with open("henu_accounts.json", encoding="utf-8") as f:
        return json.load(f)["accounts"]


def pick_account(accounts, username):
    for a in accounts:
        if a["username"] == username:
            return a
    raise SystemExit("配置文件中没有学号 %s，请先填入 henu_accounts.json" % username)


def normalize_seat_nos(account):
    """seat_no 接受单个整数或按优先级排列的列表。"""
    raw = account.get("seat_no")
    if raw is None:
        raise SystemExit("账号未配置 seat_no")
    if isinstance(raw, list):
        nos = raw
    else:
        nos = [raw]
    out = []
    for n in nos:
        try:
            out.append(int(n))
        except (TypeError, ValueError):
            raise SystemExit("seat_no 非法: %r" % (n,))
    if not out:
        raise SystemExit("seat_no 为空")
    return out


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


def wait_now_fire(log_fp=None):
    """仅测试模式：构造 confirm 后等待 NOW_FIRE_DELAY 秒。正式模式不调用。"""
    started_at = datetime.datetime.now()
    until = started_at + datetime.timedelta(seconds=NOW_FIRE_DELAY)
    print("[测试模式] 开始等待开火: %s，等到 %s" % (fmt_ts(started_at), fmt_ts(until)))
    wait_until(until)
    fired_at = datetime.datetime.now()
    print("[测试模式] 开始等 %s，等到 %s，实际开火 %s" % (
        fmt_ts(started_at), fmt_ts(until), fmt_ts(fired_at)))
    write_log(log_fp, {
        "event": "now_wait",
        "started_at": fmt_ts(started_at),
        "until": fmt_ts(until),
        "fired_at": fmt_ts(fired_at),
        "delay_sec": NOW_FIRE_DELAY,
    })
    return fired_at


def fmt_ts(dt=None):
    if dt is None:
        dt = datetime.datetime.now()
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def open_run_log(account):
    """打开本次运行日志。热路径不写这个文件。"""
    os.makedirs(LOG_DIR, exist_ok=True)
    name = "grab_%s_%s.jsonl" % (
        account.get("username", "unknown"),
        datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
    )
    path = os.path.join(LOG_DIR, name)
    fp = open(path, "a", encoding="utf-8")
    print("运行日志:", path)
    return path, fp


def write_log(fp, event):
    if fp is None:
        return
    try:
        event.setdefault("ts", fmt_ts())
        fp.write(json.dumps(event, ensure_ascii=False) + "\n")
        fp.flush()
    except Exception:
        pass


def close_run_log(fp):
    if fp is None:
        return
    try:
        fp.close()
    except Exception:
        pass


def print_grab_event(event):
    """控制台输出从日志线程走，不进抢座 worker。"""
    name = event.get("event")
    if name == "confirm":
        print("code: %s | msg: %s | seat: %s" % (
            event.get("code"), event.get("msg"), event.get("seat_no")))
        elapsed_ms = event.get("elapsed_from_target_ms")
        if elapsed_ms is not None:
            print("响应耗时（相对开放时间）: %.3f 秒" % (elapsed_ms / 1000.0))
    elif name == "confirm_error":
        print("预约请求异常(%s)，继续重试..." % event.get("error_type"))
    elif name == "seat_switch":
        nxt = event.get("to")
        if nxt is None:
            print("座位 %s 不可预约，没有下一个座位" % event.get("from"))
        else:
            print("座位 %s 不可预约，切换到 %s" % (event.get("from"), nxt))


def emit_log(log_queue, event, log_fp=None):
    event.setdefault("ts", fmt_ts())
    if log_queue is not None:
        log_queue.put(event)
        return
    write_log(log_fp, event)
    print_grab_event(event)


def start_logger(fp):
    if fp is None:
        return None, None, None
    log_queue = queue.Queue()
    stop_evt = threading.Event()

    def run():
        while True:
            try:
                event = log_queue.get(timeout=0.1)
            except queue.Empty:
                if stop_evt.is_set():
                    break
                continue
            if event is None:
                break
            write_log(fp, event)
            print_grab_event(event)
        while True:
            try:
                event = log_queue.get_nowait()
            except queue.Empty:
                break
            if event is None:
                continue
            write_log(fp, event)
            print_grab_event(event)

    t = threading.Thread(target=run, name="grab-logger")
    t.daemon = True
    t.start()
    return log_queue, stop_evt, t


def stop_logger(log_queue, stop_evt, thread):
    if log_queue is None:
        return
    stop_evt.set()
    log_queue.put(None)
    thread.join(2)


def _make_confirm_data(day, chosen, reserve_type, t0, d_conf):
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
    return confirm_data


def build_confirm_targets(token, account, day, quiet=False):
    """定位区域 + 一次拉座位列表，为配置的每个 seat_no 构造 confirm 请求体。"""
    area_name = account["area"]
    seat_nos = normalize_seat_nos(account)
    if not quiet:
        print("\n=== 目标: %s（明天）| 区域: %s | 座位号: %s ===" % (
            day, area_name, seat_nos))

    top = henu_client.post_json(henu_client.api_base + "/v4/space/pcTopFor", {"day": day}, token)
    campus_id = None
    floor_ids = []
    for c in top["data"]["list"]:
        if c["name"] == "金明校区":
            campus_id = c["id"]
            floor_ids = [ch["id"] for ch in c.get("children", [])]
            break
    if not (campus_id and floor_ids):
        if not quiet:
            print("定位校区/楼层失败")
        return []

    pick = henu_client.post_json(henu_client.api_base + "/v4/space/pick", {
        "premisesIds": [campus_id], "categoryIds": [],
        # 账号配置只指定区域名；跨校区楼层目录查找，避免楼层写死导致找不到目标区域。
        "storeyIds": floor_ids, "boutiqueIds": [], "date": day,
    }, token)
    area_id = None
    area_free = None
    for a in pick.get("data", {}).get("area", []):
        if a["name"] == area_name:
            area_id = a["id"]
            area_free = a.get("free_num")
            if not quiet:
                print("区域: %s id=%s 空闲=%s" % (area_name, area_id, area_free))
            break
    if not area_id:
        if not quiet:
            print("未找到区域:", area_name)
        return []

    mp = henu_client.post_json(henu_client.api_base + "/v4/Space/map", {"id": area_id}, token)
    d_conf = next((d for d in mp.get("data", {}).get("date", {}).get("list", [])
                   if d.get("day") == day), None)
    if not d_conf:
        if not quiet:
            print("未找到 date 配置")
        return []
    t0 = d_conf["times"][0]
    reserve_type = mp["data"]["date"].get("reserveType")
    if not quiet:
        print("reserveType:", reserve_type, "| 时段:", t0["start"], "-", t0["end"])

    seat_resp = henu_client.post_json(henu_client.api_base + "/v4/Space/seat", {
        "id": area_id, "day": day, "label_id": "",
        "start_time": t0["start"], "end_time": t0["end"],
        "begdate": "", "enddate": "",
    }, token)
    lst = seat_resp.get("data", {}).get("list", [])
    by_no = {}
    for s in lst:
        try:
            by_no[int(s["no"])] = s
        except (TypeError, ValueError):
            continue

    targets = []
    missing = []
    for seat_no in seat_nos:
        chosen = by_no.get(seat_no)
        if not chosen:
            missing.append(seat_no)
            continue
        confirm_data = _make_confirm_data(day, chosen, reserve_type, t0, d_conf)
        if not quiet:
            print("目标座位:", chosen["id"], chosen["no"], chosen.get("status_name"))
            print("请求体:", json.dumps(confirm_data, ensure_ascii=False))
        targets.append({
            "seat_no": seat_no,
            "request_data": confirm_data,
            "info": {
                "day": day,
                "area_name": area_name,
                "area_id": area_id,
                "area_free": area_free,
                "seat_no": seat_no,
                "seat_id": chosen["id"],
                "seat_label": chosen.get("no"),
                "status_name": chosen.get("status_name"),
                "reserve_type": reserve_type,
                "start": t0.get("start"),
                "end": t0.get("end"),
                "segment": confirm_data.get("segment"),
                "start_time": confirm_data.get("start_time"),
                "end_time": confirm_data.get("end_time"),
            },
        })
    if missing and not quiet:
        print("未找到座位:", missing)
    return targets


def send_confirm(token, confirm_data, on_http_attempt=None,
                 retries=1, timeout=CONFIRM_TIMEOUT, conn=None):
    """发送预约请求，并返回响应及接收时间。确认路径不做内部退避。"""
    aesjson = henu_client.encrypt_aesjson(confirm_data)
    kwargs = {"retries": retries, "timeout": timeout}
    if on_http_attempt is not None:
        kwargs["on_attempt"] = on_http_attempt
    payload = {"aesjson": aesjson}
    if conn is not None:
        # worker 长连接：不走全局 opener，retries 仍传进去但连接层只打一枪
        res = conn.post_json("/v4/space/confirm", payload, token, **kwargs)
    else:
        res = henu_client.post_json(
            henu_client.api_base + "/v4/space/confirm",
            payload,
            token,
            **kwargs)
    received_at = datetime.datetime.now()
    return res, received_at


def log_postmortem(fp, token, account, day):
    """抢座结束后再查一次座位，不进入热路径。"""
    try:
        targets = build_confirm_targets(token, account, day, quiet=True)
        write_log(fp, {
            "event": "postmortem",
            "targets": [{
                "seat_no": t["seat_no"],
                "info": t.get("info"),
                "request_data": t.get("request_data"),
            } for t in targets],
        })
    except Exception as e:
        write_log(fp, {
            "event": "postmortem_error",
            "error_type": type(e).__name__,
            "error": str(e),
        })


def already_reserved_message(msg):
    """code=1 里账号已有预约。包含匹配，避免整句标点差异。"""
    return "已存在座位预约" in (msg or "")


def _run_grab_workers(token, targets, target_time, deadline, log_queue, log_fp=None):
    lock = threading.Lock()
    state = {
        "seat_index": 0,
        "stop": False,
        "reason": None,
        "attempts": 0,
        "last_res": None,
        "last_seat_no": None,
    }
    cookie = henu_client.cookie_header(henu_client.api_base + "/v4/space/confirm")

    def worker(worker_id):
        delay = (worker_id - 1) * WORKER_STAGGER
        if delay > 0:
            time.sleep(delay)
        # 对象在这里创建；真正的 TCP/TLS 等到第一枪 send_confirm
        conn = henu_client.ConfirmHttpsConn(cookie=cookie)
        try:
            while True:
                now = datetime.datetime.now()
                with lock:
                    if state["stop"]:
                        return
                    if now >= deadline:
                        state["stop"] = True
                        state["reason"] = "timeout"
                        return
                    idx = state["seat_index"]
                    if idx >= len(targets):
                        state["stop"] = True
                        state["reason"] = "no_seats"
                        return
                    target = targets[idx]
                    state["attempts"] += 1
                    attempt = state["attempts"]
                http_trace = []
                sent_at = datetime.datetime.now()
                try:
                    res, received_at = send_confirm(
                        token, target["request_data"],
                        on_http_attempt=http_trace.append, conn=conn)
                except Exception as e:
                    emit_log(log_queue, {
                        "event": "confirm_error",
                        "worker": worker_id,
                        "attempt": attempt,
                        "seat_no": target["seat_no"],
                        "sent_at": fmt_ts(sent_at),
                        "received_at": fmt_ts(),
                        "error_type": type(e).__name__,
                        "error": str(e),
                        "http": http_trace,
                    }, log_fp)
                    continue
                emit_log(log_queue, {
                    "event": "confirm",
                    "worker": worker_id,
                    "attempt": attempt,
                    "seat_no": target["seat_no"],
                    "seat_id": target["request_data"].get("seat_id"),
                    "sent_at": fmt_ts(sent_at),
                    "received_at": fmt_ts(received_at),
                    "rtt_ms": int(round((received_at - sent_at).total_seconds() * 1000)),
                    "elapsed_from_target_ms": int(round(
                        (received_at - target_time).total_seconds() * 1000)),
                    "code": res.get("code"),
                    "msg": res.get("message") or res.get("msg") or "",
                    "response": res,
                    "http": http_trace,
                }, log_fp)
                code = res.get("code")
                msg = res.get("message") or res.get("msg") or ""
                switch_from = None
                switch_to = None
                with lock:
                    state["last_res"] = res
                    state["last_seat_no"] = target["seat_no"]
                    if state["stop"]:
                        return
                    if code == 0:
                        state["stop"] = True
                        state["reason"] = "success"
                        return
                    if code == 1 and already_reserved_message(msg):
                        state["stop"] = True
                        state["reason"] = "already_reserved"
                        return
                    if code == 1:
                        if (state["seat_index"] < len(targets)
                                and targets[state["seat_index"]] is target):
                            switch_from = target["seat_no"]
                            state["seat_index"] += 1
                            if state["seat_index"] >= len(targets):
                                state["stop"] = True
                                state["reason"] = "no_seats"
                            else:
                                switch_to = targets[state["seat_index"]]["seat_no"]
                if switch_from is not None:
                    emit_log(log_queue, {
                        "event": "seat_switch",
                        "from": switch_from,
                        "to": switch_to,
                    }, log_fp)
        finally:
            conn.close()

    threads = []
    n = max(1, int(CONCURRENCY))
    for i in range(n):
        t = threading.Thread(target=worker, args=(i + 1,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return state


def reserve_with_retry(token, account, targets=None, target_time=None, log_fp=None, fire_delay=0):
    """开放瞬间并发抢座。code=1 已存在预约则停；其它 code=1 按配置顺序切座。"""
    day = str(datetime.date.today() + datetime.timedelta(days=1))
    if targets is None:
        while True:
            try:
                targets = build_confirm_targets(token, account, day)
            except Exception as e:
                print("定位请求异常(%s)，1 秒后重试..." % type(e).__name__)
                write_log(log_fp, {
                    "event": "build_error",
                    "error_type": type(e).__name__,
                    "error": str(e),
                })
                targets = []
            if targets:
                write_log(log_fp, {
                    "event": "build",
                    "ok": True,
                    "targets": [{
                        "seat_no": t["seat_no"],
                        "info": t.get("info"),
                        "request_data": t.get("request_data"),
                    } for t in targets],
                })
                break
            print("定位/构造失败，1 秒后重试...")
            write_log(log_fp, {"event": "build", "ok": False})
            time.sleep(1)

    if fire_delay:
        fired_at = wait_now_fire(log_fp)
        if target_time is None:
            target_time = fired_at
    if target_time is None:
        target_time = datetime.datetime.now()
    deadline = target_time + datetime.timedelta(seconds=RETRY_TIMEOUT)
    log_queue, stop_evt, log_thread = start_logger(log_fp)
    try:
        state = _run_grab_workers(
            token, targets, target_time, deadline, log_queue, log_fp=log_fp)
    finally:
        stop_logger(log_queue, stop_evt, log_thread)

    last_res = state.get("last_res")
    last_code = None if last_res is None else last_res.get("code")
    last_msg = "" if last_res is None else (last_res.get("message") or last_res.get("msg") or "")
    reason = state.get("reason") or "unknown"
    if reason == "success":
        print("=== 预约成功 ===")
    elif reason == "already_reserved":
        print("=== 当前用户已存在座位预约，停止 ===")
    elif reason == "timeout":
        print("=== 开放后 %s 秒仍未成功，停止 ===" % RETRY_TIMEOUT)
    elif reason == "no_seats":
        print("=== 没有可继续抢的座位，停止 ===")
    write_log(log_fp, {
        "event": "stop",
        "reason": reason,
        "attempts": state.get("attempts"),
        "last_seat_no": state.get("last_seat_no"),
        "last_code": last_code,
        "last_msg": last_msg,
    })
    if reason != "success":
        log_postmortem(log_fp, token, account, day)
    return state


def main(account, username, log_fp=None):
    now = datetime.datetime.now()
    open_at = now.replace(hour=SEND_HOUR, minute=SEND_MIN, second=SEND_SEC, microsecond=0)
    if open_at <= now:
        open_at += datetime.timedelta(days=1)
    login_at = open_at - datetime.timedelta(seconds=60)
    seat_nos = normalize_seat_nos(account)
    print("当前时间:", now.strftime("%Y-%m-%d %H:%M:%S"))
    print("开放时刻:", open_at.strftime("%Y-%m-%d %H:%M:%S"))
    print("抢座账号:", account["name"], "| 座位:", seat_nos)
    write_log(log_fp, {
        "event": "schedule",
        "now": fmt_ts(now),
        "open_at": fmt_ts(open_at),
        "login_at": fmt_ts(login_at),
        "name": account.get("name"),
        "seat_no": seat_nos,
        "area": account.get("area"),
    })

    wait_until(login_at)
    print("\n时间到，开始登录...")
    token = ensure_login(username)
    day = str(open_at.date() + datetime.timedelta(days=1))
    # 座位信息和 confirm 参数必须在开放前准备好；开放后的关键路径只做加密和 POST。
    # 开放前服务器可能尚未放出"明天"数据，给参数获取本身留到 6:30:00.1 的短重试窗口。
    build_deadline = open_at + datetime.timedelta(seconds=RETRY_START_OFFSET)
    targets = []
    while datetime.datetime.now() < build_deadline:
        try:
            targets = build_confirm_targets(token, account, day)
        except Exception as e:
            print("参数获取异常(%s)，重试..." % type(e).__name__)
            write_log(log_fp, {
                "event": "build_error",
                "error_type": type(e).__name__,
                "error": str(e),
            })
            targets = []
        write_log(log_fp, {
            "event": "build",
            "ok": bool(targets),
            "targets": [{
                "seat_no": t["seat_no"],
                "info": t.get("info"),
                "request_data": t.get("request_data"),
            } for t in targets],
        })
        if targets:
            break
        time.sleep(1)
    if not targets:
        write_log(log_fp, {"event": "stop", "reason": "build_failed"})
        raise SystemExit("开放前未能构造预约请求")
    for t in targets:
        print("request_data:", json.dumps(t["request_data"], ensure_ascii=False))

    wait_until(build_deadline)
    print("到点，开始连续抢座")
    write_log(log_fp, {
        "event": "fire",
        "target_time": fmt_ts(open_at + datetime.timedelta(seconds=RETRY_START_OFFSET)),
        "now": fmt_ts(),
        "concurrency": CONCURRENCY,
        "seat_nos": [t["seat_no"] for t in targets],
    })
    reserve_with_retry(
        token, account, targets=targets,
        target_time=open_at + datetime.timedelta(seconds=RETRY_START_OFFSET),
        log_fp=log_fp)


def run_cli():
    parser = argparse.ArgumentParser(description="河大图书馆抢座脚本")
    parser.add_argument("--account", required=True, help="学号，必须在 henu_accounts.json 里")
    parser.add_argument("--now", action="store_true", help="测试模式：查座并构造 confirm 后等待 30 秒再抢座，不等待开放时刻")
    args = parser.parse_args()

    accounts = load_config()
    account = pick_account(accounts, args.account)
    henu_login.setup_account(args.account)
    _path, log_fp = open_run_log(account)
    try:
        write_log(log_fp, {
            "event": "start",
            "mode": "now" if args.now else "official",
            "username": args.account,
            "name": account.get("name"),
            "area": account.get("area"),
            "seat_no": normalize_seat_nos(account),
        })
        if args.now:
            print("[测试模式] 查座并构造请求后等待 %s 秒再开火" % NOW_FIRE_DELAY)
            token = ensure_login(args.account)
            reserve_with_retry(token, account, log_fp=log_fp, fire_delay=NOW_FIRE_DELAY)
        else:
            main(account, args.account, log_fp=log_fp)
    finally:
        close_run_log(log_fp)


if __name__ == "__main__":
    run_cli()