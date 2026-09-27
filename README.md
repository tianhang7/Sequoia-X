# Sequoia-X: 王者回归 | The King Returns

> A 股量化选股系统 V2 | A-Share Quantitative Stock Selection System V2

---

## 简介 | Introduction

Sequoia-X V2 是面向 A 股市场的量化选股系统，基于现代 Python 工程化标准从零重构。
系统以 OOP 架构、向量化计算和增量数据更新为核心设计原则，每个交易日收盘后自动完成
**数据新鲜度断言 → 持仓卖出评估 → 策略选股 → 结果过滤 → 归档 → Telegram 推送** 的全流程。

数据层默认走搜狐历史接口（免费、免登录、无 key）拉取日 K 数据（实际成交价，
供下单/展示用），东财 push2 补充后复权口径（供策略用），失败股票自动降级
baostock 补拉（`PRICE_SOURCE=baostock` 可强制只用 baostock），存储于本地 SQLite；
交易日历来自新浪（本地缓存 + 断网降级）。

工程保障：数据过期拒绝推送、六道选股过滤器、持仓卖出提醒、每日选股 JSON 归档、
UTF-8 日志落盘、信号级回测（`--backtest`），测试套件基于 hypothesis 属性测试 + 单元测试。

---

## 五种运行模式

```bash
python main.py                 # 日常模式：增量补数据 → 数据新鲜度断言 → 持仓卖出评估
                               #           → 策略选股 + 过滤 → 归档 + 当日操作手册 → Telegram 推送
python main.py --backfill      # 回填模式：8进程并行灌入全市场历史K线（可中断续传）
python main.py --backfill-raw  # 补拉模式：仅补不复权收盘价 raw_close（老库升级/缺口续传用）
python main.py --backtest      # 回测模式：验证策略历史信号质量（不推送任何消息）
python main.py --position-list # 查看持仓与复盘统计
python main.py --position-exits# 仅评估持仓卖出条件（不推送）
```

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

```bash
# 推荐使用 uv（快速包管理器）
uv sync

# 或者 pip
pip install .
```

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

### 4. 日常运行

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

### 5. 运行测试

```bash
python -m pytest tests -q          # 51 项属性测试 + 单元测试
```

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
  默认参数约需 5~15 分钟。策略均已支持 `as_of` 历史切片，无前视偏差。

---

## 目录结构 | Project Structure

```
Sequoia-X/
├── main.py                      # 入口：argparse 分发日常/回填/回测/持仓模式
├── pyproject.toml               # 依赖声明 + ruff/pytest 配置
├── .env.example                 # 环境变量模板
├── data/                        # SQLite 数据库 + 交易日历 + 归档（运行时生成，不入 git）
│   ├── sequoia_v2.db            # 行情/名称缓存/持仓/键值表
│   ├── trade_calendar.json      # 交易日历缓存
│   ├── selections/              # 每日选股归档 selections_YYYYMMDD.json
│   ├── manuals/                 # 当日操作手册 manual_YYYYMMDD.md
│   └── backtest/                # 回测报告 JSON
├── sequoia_x/
│   ├── core/
│   │   ├── config.py            # Pydantic-settings 配置管理
│   │   ├── logger.py            # rich 结构化日志 + 文件落盘
│   │   └── trading_calendar.py  # 交易日历（新鲜度断言/回测取样）
│   ├── data/
│   │   └── engine.py            # 数据引擎（baostock 回填 + 增量同步 + SQLite）
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

- **数据源**：东方财富公开接口 push2his（免费、无需 key；失败股票自动降级 baostock 补拉）；定增事件与交易日历来自 akshare/新浪
- **复权方式**：后复权（hfq）— 历史价格不变，适合增量存储，避免除权导致数据错乱；
  另存不复权收盘价 `raw_close` 专供下单/展示（持仓提醒、手册目标价均优先用它）
- **存储**：本地 SQLite（`data/sequoia_v2.db`），可直接拷贝到其他机器使用
  - `stock_daily` 行情主表、`stock_name` 名称缓存（ST 判定）、
    `position` 持仓与复盘、`meta` 键值（新鲜度告警标记等）
- **交易日历**：`data/trade_calendar.json`（新浪日历，缓存 7 天自动刷新，断网降级为工作日推断）
- **日常增量**：8 线程并发经东财拉取（后复权 + 不复权同源），2~3 分钟完成全市场更新；
  东财失败股票自动降级 baostock 补拉，仍失败时任务按新鲜度策略告警/终止，恢复后自动补数重跑

---

## 许可证 | License

MIT
