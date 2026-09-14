# 河大图书馆座位自动预约项目

## 项目目标
每天 6:30 开放"明天"预约时，为 `henu_accounts.json` 里配置的账号自动抢约指定座位。

## 模块结构
| 文件 | 职责 |
|---|---|
| `henu_client.py` | 请求模块：HTTP 收发、预约请求 AES 加密、cookie 管理 |
| `henu_login.py` | 登录模块：CAS 登录、token 凭证持久化/校验、keyring 密码 |
| `henu_main.py` | 主程序：抢座时序、并发确认、座位切换、日志线程 |
| `henu_accounts.json` | 账号与座位配置（name / username / area / seat_no） |
| `logs/` | 每次抢座一份 jsonl。不入库 |

依赖方向单向：`henu_main` → `henu_login` → `henu_client`。

## 运行方式
```
C:\ProgramData\Anaconda3\python.exe henu_main.py --account <学号>                # 正式模式：等到 6:30 抢座
C:\ProgramData\Anaconda3\python.exe henu_main.py --account <学号> --now          # 测试模式：立即按同一套停止规则抢座
```
依赖：Anaconda python + `pycryptodome keyring beautifulsoup4`（`Crypto` 来自 pycryptodome）。

## 座位配置
- `seat_no` 可以是单个整数，也可以是按优先级排列的列表，例如 `[20, 19, 18]`。
- 开放前对同一区域只拉一次座位列表，为每个 `seat_no` 预构造 confirm 请求体。
- 抢座过程中切座位只换已构造好的 `seat_id`，不再查座位接口。

## 抢座策略
- 开放后 0.1 秒开火。确认请求 `retries=1`、`timeout=3`，内部不 sleep、不退避。
- 有用的并发是重叠 RTT，不是无界轰击：`CONCURRENCY=4`。四个 worker 同时 start，仅各自第一枪错开 0.20 秒（0 / 0.20 / 0.40 / 0.60）；回包后的循环仍立即再发，不 sleep。
- 停止规则只看业务 code：
  - `code == 0`：预约成功，停止。
  - `code == 1`：当前座位不可预约，立刻改抢下一个 `seat_no`；没有下一个则停止。
  - 其它结果（615 / 627 / 已预约 / 超时 / 网络错误等）继续抢当前座位。
- 成功只认 `code == 0`，不把"已预约"当成功。
- 总窗口仍是开火后 60 秒。在途 urllib 请求无法取消，最多再等一个 3 秒超时。

## 运行日志
- 目录：`logs/`，文件名 `grab_<学号>_<YYYYMMDD_HHMMSS>.jsonl`。
- 抢座线程只 `queue.put`；独立日志线程写 jsonl 并打印。热路径不 write/flush/print。
- 不打印 token/cookie/密码。
- 抢座停止后（成功除外）才再查一次座位状态。

## 凭证安全
- 密码存 Windows 凭据管理器（keyring，DPAPI 加密），不落明文。
- token/cookie 按学号隔离（`henu_token_<学号>.json`、`henu_cookies_<学号>.txt`），已被 `.gitignore` 排除，绝不提交。

## 约定
- 代码注释用中文，变量/函数名用英文。Python 3.7，字符串格式化用 `%`。
- 登录等非确认请求仍由 `post_json` / `http_request` 重试 3 次。
- 确认请求禁止走内部重试退避；失败由外层 worker 立即再发。
- 服务端接口未公开，可能随时变动，维护时注意响应结构。