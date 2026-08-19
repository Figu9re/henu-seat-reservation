# 河大图书馆座位自动预约

每天 6:30 开放"明天"预约时，自动为配置的账号抢约指定座位。密码存 Windows 凭据管理器，无需人工介入。

## 功能

- **CAS 自动登录**：逆向河大统一认证，支持 cookie 会话复用与密码加密登录
- **多账号抢座**：`henu_accounts.json` 配置多个账号与目标座位，按学号隔离凭证
- **开放瞬间连续重试**：6:29:59.8 发出第一枪，每 0.25 秒重试，覆盖开放瞬间
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
    {"name": "姓名", "username": "学号", "area": "二楼大厅走廊", "seat_no": 15}
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
| 正式 | `henu_main.py --account <学号>` | 等到 6:30 开放瞬间连续抢座 |
| 测试 | `henu_main.py --account <学号> --now` | 立即发一次请求，用于验证流程 |

> `--now` 会真实发送预约请求，只在想立刻抢或验证流程时用。

## 文件结构

```
henu_main.py     主程序：抢座时序、调用链、连续重试
henu_login.py    登录模块：CAS 登录、token 凭证、keyring 密码
henu_client.py   请求模块：HTTP 收发、预约请求加密、cookie 管理
henu_accounts.example.json  账号与座位配置模板（复制为 henu_accounts.json 后填写）
```

## 注意事项

- token/cookie 为会话凭证，已被 `.gitignore` 排除，不会进入版本库
- 服务端接口未公开，可能随时变动
- 抢座以"能约到"为前提，若目标座位被他人先抢到，脚本会在超时后停止
