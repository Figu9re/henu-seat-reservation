# 河大图书馆座位自动预约项目

## 项目目标
每天 6:30 开放"明天"预约时，为 `henu_accounts.json` 里配置的账号自动抢约指定座位。

## 模块结构
| 文件 | 职责 |
|---|---|
| `henu_client.py` | 请求模块：HTTP 收发、预约请求 AES 加密、cookie 管理 |
| `henu_login.py` | 登录模块：CAS 登录、token 凭证持久化/校验、keyring 密码 |
| `henu_main.py` | 主程序：抢座时序、调用链、连续重试 |
| `henu_accounts.json` | 账号与座位配置（name / username / area / seat_no） |

依赖方向单向：`henu_main` → `henu_login` → `henu_client`。

## 运行方式
```
C:\ProgramData\Anaconda3\python.exe henu_main.py --account <学号>                # 正式模式：等到 6:30 抢座
C:\ProgramData\Anaconda3\python.exe henu_main.py --account <学号> --now          # 测试模式：立即发一次
```
依赖：Anaconda python + `pycryptodome keyring beautifulsoup4`（`Crypto` 来自 pycryptodome）。

## 凭证安全
- 密码存 Windows 凭据管理器（keyring，DPAPI 加密），不落明文。
- token/cookie 按学号隔离（`henu_token_<学号>.json`、`henu_cookies_<学号>.txt`），已被 `.gitignore` 排除，绝不提交。

## 约定
- 代码注释用中文，变量/函数名用英文。
- 网络瞬时抖动会自动重试（`post_json` / `http_request` 各重试 3 次），登录与抢座全流程覆盖。
- 服务端接口未公开，可能随时变动，维护时注意响应结构。
