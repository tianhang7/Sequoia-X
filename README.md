# Sequoia-X: 王者回归 | The King Returns

> A 股量化选股系统 V2 | A-Share Quantitative Stock Selection System V2

---

## 简介 | Introduction

Sequoia-X V2 是面向 A 股市场的量化选股系统，基于现代 Python 工程化标准从零重构。
系统以 OOP 架构、向量化计算和增量数据更新为核心设计原则，每个交易日收盘后自动完成
**数据新鲜度断言 → 持仓卖出评估 → 策略选股 → 结果过滤 → 归档 → Telegram 推送** 的全流程。

数据层使用 [baostock](http://baostock.com)（免费、无需注册、无限流）拉取历史及增量日 K 数据（后复权），
存储于本地 SQLite，彻底规避东方财富反爬问题；交易日历来自新浪（本地缓存 + 断网降级）。
baostock 宕机/登录失败时，可一键切换 [Yahoo Finance](https://finance.yahoo.com) 备用数据源补齐数据
（`--yahoo`，见下文「数据源降级」）。

工程保障：数据过期拒绝推送、六道选股过滤器、持仓卖出提醒、每日选股 JSON 归档、
UTF-8 日志落盘、信号级回测（`--backtest`），测试套件基于 hypothesis 属性测试 + 单元测试。

---

## 运行模式 | Usage

```bash
python main.py                 # 日常模式：增量补数据 → 数据新鲜度断言 → 持仓卖出评估
                               #           → 策略选股 + 过滤 → 归档 + 当日操作手册 → Telegram 推送
python main.py --backfill      # 回填模式：8进程并行灌入全市场历史K线（可中断续传）
python main.py --backfill-raw  # 补拉模式：仅补不复权收盘价 raw_close（老库升级/缺口续传用）
python main.py --backfill --yahoo      # 备用数据源：用 Yahoo Finance 回填历史K线（baostock 不可用时）
python main.py --backfill-raw --yahoo  # 备用数据源：用 Yahoo Finance 补齐 raw_close
python main.py --backtest      # 回测模式：验证策略历史信号质量（不推送任何消息）
python main.py --position-list # 查看持仓与复盘统计
python main.py --position-exits# 仅评估持仓卖出条件（不推送）
python main.py --position-add 600519 --position-qty 100 --position-price 1500   # 登记买入
python main.py --position-close 600519 --close-price 1600                       # 登记平仓
```

完整参数见 `python main.py --help`。

---

## 内置策略 | Strategies

| 策略 | 说明 | 参与回测 |
|---|---|---|
| **TurtleTrade** | 海龟突破：20日新高 + 阳线防诱多，按流通市值降序排序（市值不可用时降级按20日均成交额） | ✅ |
| **MaVolume** | 均线+放量突破 | ✅ |
| **HighTightFlag** | 高而窄的旗形整理突破 | ✅ |
| **LimitUpShakeout** | 涨停洗盘回踩确认 | ✅ |
| **UptrendLimitDown** | 上升趋势中的跌停反包 | ✅ |
| **RpsBreakout** | 欧奈尔 RPS 相对强度突破（滚动新高基于 `shift(1)`，无前视偏差） | ✅ |
| **PrivatePlacement** | 定增/解禁事件公告（akshare，事件型，不依赖K线） | ❌ |

所有策略的 `run(as_of=...)` 均支持按历史日期切片，回测与实盘共用同一套信号逻辑。

---

## 快速开始 | Quick Start

### 环境要求

- Python >= 3.10

### 1. 安装依赖

推荐使用 [uv](https://docs.astral.sh/uv/)（快速包管理器，本仓库已提交 `uv.lock`）：

```bash
uv sync
```

或者 pip：

```bash
pip install .
```

> **Windows 激活虚拟环境报「无法加载文件…未数字签名」？**
> 这是 PowerShell 执行策略拦截了 `.venv\Scripts\activate.ps1`。任选其一：
>
> ```powershell
> # A. 对当前用户永久放开（推荐，不影响系统全局）
> Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
> .venv\Scripts\activate
>
> # B. 仅本次会话临时放开
> Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
> .venv\Scripts\activate
> ```
>
> ```cmd
> :: C. 彻底绕过 PowerShell：改用 CMD（不受执行策略影响）
> .venv\Scripts\activate.bat
> ```
>
> VS Code 中可将「终端默认配置」设为 Command Prompt，避免该问题。
> 也可以完全跳过激活，直接用 `.venv\Scripts\python.exe main.py`。

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env：配置 Telegram 推送（TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID）
# 及各项高级配置（新鲜度/过滤器/持仓规则），详见 .env.example 注释
```

> **Telegram 是唯一推送通道**：不配置 Bot Token / Chat ID 时，日常模式仍可正常
> 同步数据、选股与归档，但不会发出任何消息（启动时与消息触发时各告警一次）。

### 3. 首次回填历史数据

```bash
python main.py --backfill
```

约 12 分钟完成 ~5200 只 A 股历史后复权日 K 数据回填（可中断续传，已回填部分自动跳过）。
回填同时写入不复权收盘价 `raw_close`（下单用，`ENABLE_RAW_PRICES=false` 可关闭省一半请求）。
老库升级（一键补齐历史 `raw_close`，可重跑续传）：

```bash
python main.py --backfill-raw
```

### 4. 数据源降级：改用 Yahoo Finance 补齐数据

baostock 偶发登录失败或返回空数据时（日志出现「失败 NNNN 只」「更新 0 行」），
可用 Yahoo Finance 作为备用数据源补齐，无需改代码：

```bash
python main.py --backfill-raw --yahoo   # 补齐不复权收盘价 raw_close（只 UPDATE 已有行）
python main.py --backfill --yahoo       # 补齐/回填后复权历史K线（INSERT OR IGNORE，可续传）
```

两个模式都可反复重跑：已补齐的股票自动跳过，只处理仍缺数据的部分；
失败清单会写入日志（`以下股票 Yahoo 亦无数据: ...`），修复后重跑继续。

实现要点（`sequoia_x/data/yahoo_source.py` + `DataEngine.backfill_*_yahoo`）：

| 项目 | 说明 |
|---|---|
| 代码映射 | `6/9` 开头 → `.SS`（上交所），`0/2/3` → `.SZ`（深交所），`4/8` → `.BJ`（北交所） |
| 复权口径 | `auto_adjust=True` → 后复权（写 `close`）；`False` → 不复权（写 `raw_close`），与 baostock `adjustflag` 1/3 一致 |
| 断点续传 | 历史回填按各股票本地 `MAX(date)+1` 起步；raw 补齐按「最早缺 `raw_close` 的那一行」起步，不重拉整段历史 |
| 并发/限流 | 每批 50 只、线程并发下载；单批失败只记日志，不影响其余批次 |
| 缓存 | yfinance 缓存落在 `data/yf_cache/`，规避 Windows 默认缓存目录不可写导致的报错 |

> ⚠️ **成交额口径差异**：Yahoo 不提供成交额字段，`turnover` 以 `close × volume` 近似，
> 仅供流动性过滤器（`FILTER_MIN_AVG_AMOUNT`）做量级粗筛，不参与任何价格计算与下单。
>
> ⚠️ 代码表来自本地库（`get_local_symbols`），故 Yahoo 通道解决的是「数据缺口」而非「冷启动」：
> 全新库仍需先用 baostock 执行一次 `--backfill` 建立代码与名称缓存。
>
> ℹ️ 日志中的「**已最新 N 只**」表示这些股票的本地数据已追平最近交易日、窗口内无新增交易日
> （如国庆休市等），属正常完成；只有「**失败**」清单才代表 Yahoo 确实拉不到数据，需要重跑补齐。

### 5. 日常运行

```bash
python main.py
```

运行日志自动写入 `LOG_FILE`（默认 `log.txt`），无需重定向 stdout。
建议配合定时任务每个交易日收盘后自动执行：

```cron
# Linux crontab（工作日 19:15）
15 19 * * 1-5 cd /root/Sequoia-X && .venv/bin/python main.py
```

```powershell
# Windows 任务计划（工作日 19:15）
schtasks /Create /TN "SequoiaX-Daily" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 19:15 /TR "c:\path\to\.venv\Scripts\python.exe c:\path\to\Sequoia-X\main.py"
```

数据落后最新交易日时（`strict` 模式）任务会推送告警并以退出码 1 终止，不会发出过期信号。

### 6. 运行测试

```bash
python -m pytest tests -q          # 67 项测试：hypothesis 属性测试 + 单元测试
```

测试覆盖数据引擎、配置、日志、过滤器、策略信号、持仓、手册、回测、Telegram 与交易日历；
全部使用 fixture / mock，不访问网络或真实数据库。

---

## 运行保障与进阶功能

### 1. 数据新鲜度断言（防止按过期信号下单）

每次日常运行先比对「库内最新K线日期」与交易日历（`data/trade_calendar.json`，
新浪日历 + 本地缓存，断网降级为工作日推断）：

- `DATA_FRESHNESS=strict`（默认）：数据落后最新交易日 → 推送告警（同一标记只推一次）
  并以非零退出码终止，**不会**发送过期选股信号；
- `warn`：仅记录告警继续运行；`off`：不检查。

### 2. 选股结果过滤器（下单前强制清洗）

策略原始候选按以下规则硬剔除，日志输出剔除统计（`raw → kept`）：

| 规则 | 说明 | 开关/阈值 |
|---|---|---|
| 非主板/双创 | 北交所 4/8 开头、B 股等剔除 | 固定 |
| ST / 退 | 读本地名称缓存 | 固定 |
| 停牌/数据滞后 | 最后一根K线 ≠ 信号日或成交量为0 | 固定 |
| 次新股 | K线根数不足 | `FILTER_MIN_BARS=60` |
| 低流动性 | 20日均成交额低于阈值 | `FILTER_MIN_AVG_AMOUNT=100000000` |
| 信号日涨停 | 次日大概率难买 | `FILTER_DROP_LIMIT_UP=true` |

总开关 `ENABLE_SYMBOL_FILTERS=true`。

### 3. 持仓管理与卖出提醒

```bash
python main.py --position-add 600519 --position-qty 100 --position-price 1500 \
               --position-strategy turtle   # 登记买入（缺省自动生成 7% 止损价）
python main.py --position-close 600519 --close-price 1600   # 登记平仓（进复盘统计）
python main.py --position-list              # 持仓明细 + 胜率统计
python main.py --position-exits             # 手动评估卖出条件（不推送）
```

日常运行自动评估全部持仓，按优先级 **硬止损 → 跌破MA20 → 时间止损（10日不盈利）**
推送卖出提醒（Telegram）。
止损/时间参数：`POSITION_STOP_LOSS=0.07`、`POSITION_TIME_STOP_DAYS=10`。

### 4. 每日选股归档、操作手册与日志落盘

- 选股结果写入 `data/selections/selections_YYYYMMDD.json`
  （含 `as_of` 数据日期、原始/过滤后数量、剔除统计、股票清单）；
- **当日操作手册**落盘 `data/manuals/manual_YYYYMMDD.md`：把持仓离场信号整理成
  「建议卖出」（现价 / 目标卖出价 / 止损价 / 离场原因），把过滤后的策略候选整理成
  「建议买入」（收盘价参考 / 建议限价 / 止损价 / 止盈目标，跨策略命中自动去重），
  末尾附一页式执行清单，次日按图操作即可；同日重跑覆盖旧文件，无信号时也留档；
  止盈比例 `MANUAL_TAKE_PROFIT=0.20`（止损沿用 `POSITION_STOP_LOSS`）；
  价格口径一律用不复权原始价（`raw_close`，可直接参考下单），缺失时降级后复权并
  逐条打标「后复权」，顶部给出原始价覆盖率；
  日常运行时手册摘要（卖出/买入数量 + 各前 N 条，`MANUAL_PUSH_TOP_N=10`，0=不推送）
  一并推 Telegram，全文仍在 Markdown 文件中；
- 运行日志同步写入 `LOG_FILE`（默认 `log.txt`，UTF-8；置空则仅控制台）。

### 5. 历史回测（验证策略信号质量）

```bash
python main.py --backtest                          # 默认：近60交易日、每10日取1个信号日
python main.py --backtest --bt-days 250 --bt-step 20 \
               --bt-strategies ma_volume,turtle    # 自定义窗口/策略/步长
```

- **信号级**回测：信号日**次日开盘进场**（涨停开盘买不进则放弃该笔），
  等额资金、扣 0.1% 往返费，衡量信号质量而非净值曲线；
- 离场优先级：硬止损(-7%，盘中触发) → 收盘跌破MA20(次日开盘卖) →
  时间止损(10日不盈利) → 窗口结束按收盘强平；
- 输出各策略笔数/胜率/平均/中位/离场分布 + 全市场等权基准，
  JSON 报告落盘 `data/backtest/`；事件型策略（定增）不参与回测；
- 耗时参考：全市场内存加载约 1 分钟，之后每个信号日 × 每策略约 30~60 秒，
  默认参数约需 5~15 分钟（`--bt-days 250 --bt-step 20` 实测约 15 分钟）。策略均已支持 `as_of` 历史切片，无前视偏差。

#### 回测实测结论（重要，务必先读）

用 `--bt-days 250 --bt-step 20`（2025-09-18 ~ 2026-09-16，13 个信号日，基准 **-0.59%**）实测：

| 策略 | 笔数 | 胜率 | 均笔 | 盈利因子 | 保本胜率 | 总收益 |
|---|---|---|---|---|---|---|
| **RpsBreakout** | 662 | 27.2% | **+1.39%** | 1.30 | 22.3% | **+922%** |
| **TurtleTrade** | 560 | 24.5% | +0.17% | 1.04 | 23.7% | +97% |
| UptrendLimitDown | 12 | 41.7% | +6.00% | 3.83 | 15.7% | +72% |
| HighTightFlag | 13 | 38.5% | +0.80% | 1.45 | 30.1% | +10% |
| LimitUpShakeout | 9 | 22.2% | -2.78% | 0.51 | 35.9% | -25% |
| **MaVolume** | 98 | 16.3% | -1.90% | 0.49 | 28.4% | **-186%** |

**结论一：短窗口回测会严重误导。** 同一套策略在 2.5 个月窗口（基准 +3.88%）下 TurtleTrade 是
-472%、RPS 是 -285%；换一年窗口（基准 -0.59%）后分别变成 +97% 和 +922%。「跑输基准」很大程度上
是踩在了一个大盘上涨的短窗口上。**判断策略至少要用一年以上窗口。**

**结论二：日线信号上的固定 10 日时间止损过早。** 按持仓天数分组，两个窗口的规律高度一致：

| 持仓 | Turtle 胜率 | 均笔 | RPS 胜率 | 均笔 |
|---|---|---|---|---|
| 1-2 天 | **0.0%** | -7.34% | 2.7% | -6.82% |
| 3-5 天 | **0.0%** | -6.64% | 2.9% | -6.72% |
| 6-10 天 | 7.5% | -4.56% | 18.1% | -3.86% |
| 11-20 天 | 40.0% | +0.89% | 58.7% | +2.91% |
| 21+ 天 | **96.9%** | **+30.53%** | **97.6%** | **+41.01%** |

5 天内止损的 176 笔 Turtle 信号胜率**是 0%**；仅保留持仓 ≥11 天的交易，RPS 胜率升至 74%、
合计 +3715%，MaVolume 更是从 -186% 转为 +140%。**建议把 `POSITION_TIME_STOP_DAYS` 从 10 放宽到
15~20 天，或改用移动止损（跌破 MA10 离场）后重新回测对照。**

**结论三：警惕异常值依赖。** TurtleTrade 的 +97% 完全由单笔撑起——
`603115`（2026-02-26 以 36.78 买入，持有 69 天至 124.08，**+237%**）；剔除这一笔后变为 **-139.7%**。
UptrendLimitDown（+72% → 剔除后 +9.1%）同样如此。RPS 相对稳健（剔除后仍 +685%）。
**优先实盘验证 RpsBreakout。**

> ⚠️ **读数须知**
> - 「总收益」是等额资金下各笔收益**直接相加**，**不是净值曲线**，未考虑仓位重叠与资金占用；
> - 13 个信号日样本仍偏少，多笔交易来自同一信号日，**样本独立性弱**；
> - 「持仓 ≥11 天」是事后视角（只有看到结果才知道持有了多久），不能直接当作交易规则，
>   需改用移动止损等可实时判断的条件后再验证。

---

## 目录结构 | Project Structure

```
Sequoia-X/
├── main.py                      # 入口：argparse 分发日常/回填/回测/持仓模式
├── pyproject.toml               # 依赖声明 + ruff/pytest 配置
├── uv.lock                      # uv 锁定的依赖版本（推荐用 uv sync 安装）
├── log.txt                      # 运行日志（LOG_FILE 默认值，不入 git）
├── .env.example                 # 环境变量模板
├── data/                        # SQLite 数据库 + 交易日历 + 归档（运行时生成，不入 git）
│   ├── sequoia_v2.db            # 行情/名称缓存/持仓/键值表
│   ├── trade_calendar.json      # 交易日历缓存
│   ├── selections/              # 每日选股归档 selections_YYYYMMDD.json
│   ├── manuals/                 # 当日操作手册 manual_YYYYMMDD.md
│   ├── backtest/                # 回测报告 JSON
│   └── yf_cache/                # yfinance 缓存（--yahoo 备用通道使用）
├── sequoia_x/
│   ├── core/
│   │   ├── config.py            # Pydantic-settings 配置管理
│   │   ├── logger.py            # rich 结构化日志 + 文件落盘
│   │   └── trading_calendar.py  # 交易日历（新鲜度断言/回测取样）
│   ├── data/
│   │   ├── engine.py            # 数据引擎（baostock 回填 + 增量同步 + Yahoo 降级 + SQLite）
│   │   └── yahoo_source.py      # Yahoo Finance 备用数据源（代码映射/批量下载/口径对齐）
│   ├── strategy/
│   │   ├── base.py              # 策略抽象基类（支持 as_of 历史切片）
│   │   ├── filters.py           # 选股结果过滤器（ST/停牌/次新/流动性/涨停…）
│   │   ├── turtle_trade.py      # 海龟交易策略
│   │   ├── ma_volume.py         # 均线放量策略
│   │   ├── high_tight_flag.py   # 高窄旗形策略
│   │   ├── limit_up_shakeout.py # 涨停洗盘策略
│   │   ├── uptrend_limit_down.py # 上升跌停策略
│   │   ├── rps_breakout.py      # RPS 突破策略
│   │   └── private_placement.py # 定增公告策略（事件型，不参与回测）
│   ├── portfolio.py             # 持仓管理：登记/平仓/卖出条件评估
│   ├── manual.py                # 当日操作手册：建议买卖 + 目标价（Markdown 落盘）
│   ├── backtest/
│   │   └── engine.py            # 信号级回测引擎（次日开盘进场 + 规则离场）
│   └── notify/
│       └── telegram.py          # Telegram Bot 推送（分片 + 告警）
└── tests/                       # 属性测试（hypothesis）+ 单元测试
```

---

## 数据说明

- **数据源**：[baostock](http://baostock.com)（免费、无需注册、无限流）为主；
  [Yahoo Finance](https://finance.yahoo.com)（`--yahoo`）为降级备用通道；
  定增事件与交易日历来自 akshare/新浪
- **复权方式**：后复权（hfq）— 历史价格不变，适合增量存储，避免除权导致数据错乱；
  另存不复权收盘价 `raw_close` 专供下单/展示（持仓提醒、手册目标价均优先用它）
- **两源口径对齐**：Yahoo 通道 `auto_adjust` 开/关分别对应 baostock 的
  `adjustflag="1"`（后复权）与 `"3"`（不复权）；两者写入同一张 `stock_daily` 表，
  可混用续传（`INSERT OR IGNORE` / `UPDATE`，已存在的行不会被重复拉取覆盖）
- **存储**：本地 SQLite（`data/sequoia_v2.db`），可直接拷贝到其他机器使用
  - `stock_daily` 行情主表、`stock_name` 名称缓存（ST 判定）、
    `position` 持仓与复盘、`meta` 键值（新鲜度告警标记等）
- **交易日历**：`data/trade_calendar.json`（新浪日历，缓存 7 天自动刷新，断网降级为工作日推断）
- **日常增量**：8 进程并行通过 baostock 拉取，2~3 分钟完成全市场更新；
  baostock 不可用时任务会按新鲜度策略告警/终止，恢复后自动补数重跑

---

## 许可证 | License

MIT
