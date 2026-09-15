# 河大图书馆座位自动预约项目

## 项目目标
每天 6:30 开放"明天"预约时，为 `henu_accounts.json` 里配置的账号自动抢约指定座位。

## 模块结构
| 文件 | 职责 |
|---|---|
| `henu_client.py` | 请求模块：HTTP 收发、预约请求 AES 加密、cookie 管理、确认长连接 |
| `henu_login.py` | 登录模块：CAS 登录、token 凭证持久化/校验、keyring 密码 |
| `henu_main.py` | 主程序：抢座时序、并发确认、座位切换、日志线程 |
| `henu_accounts.json` | 账号与座位配置（name / username / area / seat_no） |
| `tests/` | 单元测试。只放这里，项目根目录不放。不入库 |
| `logs/` | 每次抢座一份 jsonl。不入库 |
| `tmp/` | 临时文件。不入库 |

依赖方向单向：`henu_main` → `henu_login` → `henu_client`。

## 运行方式
```
C:\ProgramData\Anaconda3\python.exe henu_main.py --account <学号>                # 正式模式：等到 6:30 抢座
C:\ProgramData\Anaconda3\python.exe henu_main.py --account <学号> --now          # 测试模式：查座并构造 confirm 后等 30 秒再抢座
```
依赖：Anaconda python + `pycryptodome keyring beautifulsoup4`（`Crypto` 来自 pycryptodome）。

## 座位配置
- `seat_no` 可以是单个整数，也可以是按优先级排列的列表，例如 `[20, 19, 18]`。
- 开放前对同一区域只拉一次座位列表，为每个 `seat_no` 预构造 confirm 请求体。
- 抢座过程中切座位只换已构造好的 `seat_id`，不再查座位接口。

## 抢座策略
- 开放后 0.1 秒开火。确认请求 `retries=1`、`timeout=3`，内部不 sleep、不退避。
- `--now` 在查完座位、构造好 confirm 请求体之后等待 `NOW_FIRE_DELAY=30` 秒再开火。正式 6:30 模式不加这 30 秒，它本身已经隔开。
- 有用的并发是重叠 RTT，不是无界轰击：`CONCURRENCY=10`。十个 worker 同时 start，仅各自第一枪错开 `WORKER_STAGGER=0.08` 秒；回包后的循环仍立即再发，不 sleep。
- 每个 grab worker 独占一条到 `zwyy.henu.edu.cn` 的 HTTPS 连接（`ConfirmHttpsConn` / `http.client.HTTPSConnection`）。不是连接池，上限等于 worker 数。
- `_run_grab_workers` 启动线程前用 `cookie_header` 从 jar 快照一次 Cookie。每个 worker 自己持有 `ConfirmHttpsConn`，热路径 `send_confirm(..., conn=该连接)`。无 conn 时（单测）仍走全局 `post_json`。
- 连接在该 worker 第一次 `send_confirm` 时创建，第一枪完成 TLS 握手；之后 keep-alive 复用，不再新建 TLS。登录、查座位、postmortem 仍走全局 urllib opener。
- 确认请求不走全局 opener，也不在 urlopen 外包全局锁。10 个 worker 并行发 confirm。
- 确认连接在 handshake/read timeout、远端断开等传输错误或响应 `will_close` 时关闭，下一枪再新建；worker `finally` 关闭。坏连接不得留给下一枪。
- 停止规则看业务 code 和 message：
  - `code == 0`：预约成功，停止。
  - `code == 1` 且 message 含「已存在座位预约」：账号已有预约，立刻停止（reason=already_reserved）。不切座位，不当成 `code == 0` 成功。
  - `code == 1` 其它文案（含「该空间当前状态不可预约」）：当前座位不可预约，立刻改抢下一个 `seat_no`；没有下一个则停止。
  - 其它结果（615 / 627 / 超时 / 网络错误等）继续抢当前座位。
- 成功只认 `code == 0`，不把「已存在预约」当成功。
- 总窗口仍是开火后 60 秒。在途请求无法取消，最多再等一个 3 秒超时。

## 运行日志
- 目录：`logs/`，文件名 `grab_<学号>_<YYYYMMDD_HHMMSS>.jsonl`。
- 抢座线程只 `queue.put`；独立日志线程写 jsonl 并打印。热路径不 write/flush/print。
- 不打印 token/cookie/密码。
- 抢座停止后（成功除外）才再查一次座位状态。

## 凭证安全
- 密码存 Windows 凭据管理器（keyring，DPAPI 加密），不落明文。
- token/cookie 按学号隔离（`henu_token_<学号>.json`、`henu_cookies_<学号>.txt`），已被 `.gitignore` 排除，绝不提交。
- 确认请求 authorization 仍是 `"bearer"+token`，无空格。Cookie 在开火前从 jar 快照，不经 opener 发 confirm。

## 约定
- 代码注释用中文，变量/函数名用英文。Python 3.7，字符串格式化用 `%`。
- 单元测试只放 `tests/`，命名 `test_*.py`。项目根目录不放测试文件。
- 临时文件只放 `tmp/`，用完可删。项目根目录不放临时文件。
- 登录等非确认请求仍由 `post_json` / `http_request` 重试 3 次。
- 确认请求禁止走内部重试退避；失败由外层 worker 立即再发。
- 服务端接口未公开，可能随时变动，维护时注意响应结构。