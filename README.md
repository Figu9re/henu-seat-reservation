# 河大图书馆座位自动预约

每天 6:30 开放"明天"预约时，自动为配置的账号抢约指定座位。密码存 Windows 凭据管理器，无需人工介入。

## 功能

- **CAS 自动登录**：逆向河大统一认证，支持 cookie 会话复用与密码加密登录
- **多账号抢座**：`henu_accounts.json` 配置多个账号与目标座位，按学号隔离凭证
- **提前准备并并发抢座**：开放前为 `seat_no` 列表预构造请求体，6:30:00.1 后 4 个请求重叠 RTT，仅第一枪错开 0.20 秒；`code=1` 立刻改抢下一个座位
- **响应耗时记录**：记录从 6:30:00 目标时间到每次预约响应返回的耗时
- **网络抗抖动**：请求超时/断连自动重试 3 次，不因瞬时网络问题崩溃
- **按日期推进**：脚本始终抢"明天"的座位

## 快速开始

**1. 安装依赖**（用 Anaconda 的 python）：

```
C:\ProgramData\Anaconda3\python.exe -m pip install pycryptodome keyring beautifulsoup4
```

**2. 配置账号**：编辑 `henu_accounts.json`

```json
{
  "accounts": [
    {"name": "姓名", "username": "学号", "area": "二楼大厅走廊", "seat_no": [20, 19, 18]}
  ]
}
```

**3. 首次运行存密码**（会交互输入密码，存入凭据管理器）：

```
C:\ProgramData\Anaconda3\python.exe henu_main.py --account <学号> --now
```

**4. 每天定时抢座**：Windows 任务计划程序 06:29 启动正式模式

```
C:\ProgramData\Anaconda3\python.exe henu_main.py --account <学号>
```

## 使用说明

| 模式 | 命令 | 说明 |
|---|---|---|
| 正式 | `henu_main.py --account <学号>` | 提前完成登录和定位，开放前最后刷新参数，等到 6:30:00 后立即连续抢座 |
| 测试 | `henu_main.py --account <学号> --now` | 查座并构造 confirm 后等 30 秒再按正式规则抢座，不等待 6:30 |

> `--now` 会真实发送预约请求，只在想立刻抢或验证流程时用。

## 文件结构

```
henu_main.py     主程序：抢座时序、调用链、连续重试
henu_login.py    登录模块：CAS 登录、token 凭证、keyring 密码
henu_client.py   请求模块：HTTP 收发、预约请求加密、cookie 管理
henu_accounts.example.json  账号与座位配置模板（复制为 henu_accounts.json 后填写）
```

## 登录页诊断

重新检查 CAS 登录页面和 JavaScript 时，使用只读诊断脚本：

```text
C:\ProgramData\Anaconda3\python.exe cas_login_diagnose.py --summary-only
```

该脚本只 GET 登录页及同源 JavaScript，静态检查 `execution`、`lt`、`pwdEncryptSalt`、密码加密、验证码检查和登录提交字段；不会读取密码、提交登录表单、换取 token 或发送预约请求。外部 JavaScript 不会被执行。

输出 JSON 报告：

```text
C:\ProgramData\Anaconda3\python.exe cas_login_diagnose.py --format json --output cas-login-report.json
```

不要用 `henu_main.py --now` 代替诊断，它会真实发送预约请求。


- token/cookie 为会话凭证，已被 `.gitignore` 排除，不会进入版本库
- 服务端接口未公开，可能随时变动
- 抢座以"能约到"为前提。当前座位返回 `code=1` 时立刻改抢下一个；列表耗尽或 60 秒超时才停
