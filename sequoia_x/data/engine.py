"""数据引擎模块：负责 SQLite 行情数据存储与 baostock 增量同步。"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol   TEXT    NOT NULL,
    date     TEXT    NOT NULL,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    turnover REAL,
    UNIQUE (symbol, date)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_symbol_date ON stock_daily (symbol, date);
"""

# 股票名称本地缓存表：推送阶段只读这张表，彻底摆脱对 baostock 的实时依赖
_CREATE_NAME_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_name (
    symbol     TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# 键值表：存放运行时状态（如数据不新鲜告警去重标记）
_CREATE_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# 持仓表：手工记录的实盘持仓，用于每日评估止损/止盈并推送卖出提醒
_CREATE_POSITION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS position (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,
    strategy     TEXT    NOT NULL DEFAULT '',
    buy_date     TEXT    NOT NULL,
    buy_price    REAL    NOT NULL,
    qty          INTEGER NOT NULL,
    stop_price   REAL,
    status       TEXT    NOT NULL DEFAULT 'open',
    close_date   TEXT,
    close_price  REAL,
    close_reason TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_POSITION_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_position_status ON position (status);
"""


@contextmanager
def _connect(db_path: str, timeout: float = 5.0) -> Iterator[sqlite3.Connection]:
    """打开 SQLite 连接，并保证退出时真正关闭连接。

    ``with sqlite3.connect(...) as conn`` 只负责提交/回滚事务，**不会关闭连接**；
    连接一旦泄漏就会一直持有数据库文件句柄，在 Windows 上会导致文件无法
    删除/移动（``PermissionError [WinError 32]``），也会积压文件描述符。
    """
    conn = sqlite3.connect(db_path, timeout=timeout)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _bs_fetch_batch(tasks: list) -> list:
    """多进程 worker：独立 login，批量拉取 baostock 数据。

    登录失败立即返回空列表：baostock 宕机时若继续逐只 query，
    每只都会各自阻塞到超时（653 只/worker × 10s ≈ 小时级卡死进程池）。
    """
    import contextlib
    import io

    import baostock as bs

    # baostock 会直接 print 报错文案，重定向以保持日志干净
    with contextlib.redirect_stdout(io.StringIO()):
        lg = bs.login()
    if lg.error_code != "0":
        return []
    results = []
    for symbol, bs_code, start, end in tasks:
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume,amount",
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="1",  # 后复权
        )
        if rs.error_code != "0":
            continue
        while rs.next():
            results.append([symbol] + rs.get_row_data())
    bs.logout()
    return results

# 子进程内的 baostock 登录状态（每个 worker 进程只登录一次）
_BS_STATE: dict = {}


def _bs_login() -> bool:
    """在当前 worker 进程内登录 baostock，带重试。"""
    import time

    import baostock as bs

    for _ in range(3):
        lg = bs.login()
        if lg.error_code == "0":
            _BS_STATE["logged_in"] = True
            return True
        time.sleep(2)
    _BS_STATE["logged_in"] = False
    return False


def _bs_worker_init() -> None:
    """进程池 initializer：每个 worker 进程启动时只登录一次。

    避免按批 login/logout 造成的连接抖动
    （baostock 频繁重连会返回“服务器连接失败”并拖慢速度）。
    """
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        _bs_login()


def _bs_backfill_worker(tasks: list) -> tuple[int, list, list]:
    """多进程 worker：复用进程内长连接，逐只带重试地拉取历史 K 线。

    返回 (本批处理的股票数, 数据行列表, 失败股票代码列表)。
    网络抖动（超时/乱码响应）时自动重连并指数退避重试，尽量不丢数据。
    baostock 自带的 print 输出会被重定向丢弃，避免污染主进程日志。
    """
    import contextlib
    import io
    import time

    import baostock as bs

    rows: list = []
    failed: list = []

    # baostock 会直接 print("login success!") 等，重定向以保持日志干净
    with contextlib.redirect_stdout(io.StringIO()):
        if not _BS_STATE.get("logged_in") and not _bs_login():
            return len(tasks), [], [t[0] for t in tasks]

        for symbol, bs_code, start, end in tasks:
            ok = False
            # 逐只重试：失败后重连 baostock，指数退避 2s/4s/8s
            for attempt in range(3):
                symbol_rows: list = []
                try:
                    rs = bs.query_history_k_data_plus(
                        bs_code,
                        "date,open,high,low,close,volume,amount",
                        start_date=start,
                        end_date=end,
                        frequency="d",
                        adjustflag="1",  # 后复权
                    )
                    if rs.error_code != "0":
                        raise RuntimeError(rs.error_msg)
                    while rs.next():
                        symbol_rows.append([symbol] + rs.get_row_data())
                    rows.extend(symbol_rows)
                    ok = True
                    break
                except Exception:
                    if attempt < 3 - 1:
                        time.sleep(2 ** (attempt + 1))
                        try:
                            bs.logout()
                        except Exception:
                            pass
                        _BS_STATE["logged_in"] = False
                        _bs_login()

            if not ok:
                failed.append(symbol)

    return len(tasks), rows, failed




def _bs_query_stock_basic(max_retries: int = 5, label: str = "股票列表") -> list:
    """登录 baostock 并全量拉取 stock_basic，返回原始行列表；失败返回空列表。

    容错：query_stock_basic 全量下载较慢，偶发网络超时会使单次请求返回空，
    调用方若据此判空就会静默终止（回填误判为 0 只股票），故此处带指数退避重试。

    baostock 字段顺序：code, code_name, ipoDate, outDate, type, status
    （``type == "1"`` 为股票，``status == "1"`` 为上市）。
    """
    import time

    import baostock as bs

    for attempt in range(max_retries):
        lg = bs.login()
        if lg.error_code != "0":
            logger.warning(f"{label}：登录失败（第 {attempt + 1}/{max_retries} 次）: {lg.error_msg}")
            time.sleep(2 ** (attempt + 1))
            continue

        try:
            rs = bs.query_stock_basic(code_name="", code="")
            if rs.error_code != "0":
                raise RuntimeError(rs.error_msg)
            rows: list = []
            while rs.next():
                rows.append(rs.get_row_data())
            if rows:
                return rows
            raise RuntimeError("query_stock_basic 返回 0 条记录（疑似网络抖动）")
        except Exception as e:
            logger.warning(f"{label}：获取失败（第 {attempt + 1}/{max_retries} 次）: {e}")
            time.sleep(2 ** (attempt + 1))
        finally:
            bs.logout()

    logger.error(f"{label}：已重试 {max_retries} 次仍无法获取")
    return []


def _rows_to_names(rows: list) -> dict[str, str]:
    """把 stock_basic 行列表转为 {纯数字代码: 名称}。"""
    mapping: dict[str, str] = {}
    for row in rows:
        code, name, stock_type, status = row[0], row[1], row[4], row[5]
        if stock_type == "1" and status == "1" and name:
            mapping[code.split(".")[1]] = name
    return mapping


def fetch_stock_names_from_baostock(max_retries: int = 2) -> dict[str, str]:
    """一次性拉取全市场股票名称 {纯数字代码: 名称}；失败返回空字典（不抛异常）。

    相比逐只 ``query_stock_basic(code=...)``，全量下载只需一次请求，
    118 只股票从 118 次请求降到 1 次。
    """
    return _rows_to_names(_bs_query_stock_basic(max_retries=max_retries, label="股票名称"))


class DataEngine:
    """行情数据引擎，负责 SQLite 存储和 baostock 数据同步。"""

    def __init__(self, settings: Settings) -> None:
        self.db_path: str = settings.db_path
        self.start_date: str = settings.start_date
        self._init_db()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.execute(_CREATE_NAME_TABLE_SQL)
            conn.execute(_CREATE_META_TABLE_SQL)
            conn.execute(_CREATE_POSITION_TABLE_SQL)
            conn.execute(_CREATE_POSITION_INDEX_SQL)
            conn.commit()
        logger.info(f"数据库初始化完成：{self.db_path}")

    def get_max_date(self) -> str | None:
        """返回 stock_daily 中的最新K线日期（ISO 字符串），库为空时返回 None。"""
        with _connect(self.db_path) as conn:
            row = conn.execute("SELECT MAX(date) FROM stock_daily").fetchone()
        return row[0] if row and row[0] else None

    def get_meta(self, key: str) -> str | None:
        """读取 meta 键值表中的值，不存在时返回 None。"""
        with _connect(self.db_path) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        """写入 meta 键值表（upsert）。"""
        with _connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            conn.commit()

    def _get_last_date(self, symbol: str) -> str | None:
        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM stock_daily WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return row[0] if row and row[0] else None

    def get_ohlcv(self, symbol: str) -> pd.DataFrame:
        with _connect(self.db_path) as conn:
            df = pd.read_sql(
                "SELECT * FROM stock_daily WHERE symbol = ? ORDER BY date",
                conn,
                params=(symbol,),
            )
        return df

    @staticmethod
    def _to_baostock_code(symbol: str) -> str:
        """将纯数字代码转为 baostock 格式：6/9开头 -> sh，其余 -> sz。"""
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

    # ── 数据同步 ──

    def sync_today_bulk(self) -> int:
        """多进程并行通过 baostock 拉取增量数据（后复权），写入 SQLite。"""
        from datetime import date, timedelta
        from multiprocessing import Pool

        from sequoia_x.core.trading_calendar import TradingCalendar

        today_str = date.today().strftime("%Y-%m-%d")

        # 守卫：本地已覆盖「最新交易日」时直接跳过（周末/节假日零开销，
        # 也避免重复运行时的全市场重拉）。数据是否落后由调用方的新鲜度
        # 断言兜底——交易日当天缺数据时 latest > max_date，仍会正常进入同步。
        latest = TradingCalendar(self.db_path).latest_trade_date()
        max_date = self.get_max_date()
        if max_date and max_date >= latest.isoformat():
            logger.info(f"本地数据已是最新交易日（{max_date}），跳过同步")
            return 0

        tasks = []
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol"
            ).fetchall()

        if not rows:
            logger.warning("本地无股票数据，请先执行 --backfill")
            return 0

        for symbol, last_date in rows:
            if last_date and last_date >= today_str:
                continue
            start = today_str
            if last_date:
                start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")
            tasks.append((symbol, self._to_baostock_code(symbol), start, today_str))

        if not tasks:
            logger.info("所有股票已是最新，无需更新")
            return 0

        logger.info(f"需要更新 {len(tasks)} 只股票，启动多进程并行拉取...")

        n_workers = min(8, len(tasks))
        chunks = [tasks[i::n_workers] for i in range(n_workers)]

        with Pool(n_workers) as pool:
            batch_results = pool.map(_bs_fetch_batch, chunks)

        all_rows = []
        for batch in batch_results:
            all_rows.extend(batch)

        if not all_rows:
            logger.info("无新数据（可能非交易日）")
            return 0

        df = pd.DataFrame(all_rows, columns=["symbol", "date", "open", "high", "low", "close", "volume", "turnover"])
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0]

        count = len(df)
        with _connect(self.db_path) as conn:
            for d in df["date"].unique().tolist():
                conn.execute("DELETE FROM stock_daily WHERE date = ?", (d,))
            df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi", chunksize=500)
            conn.commit()

        logger.info(f"sync_today_bulk: 写入 {count} 条数据")
        return count

    def backfill(self, symbols: list[str], n_workers: int = 8) -> None:
        """多进程并行通过 baostock 回填历史日 K 线数据（后复权）。

        按股票切分为小块，用进程池并行拉取；主进程作为唯一写入方，
        流式将结果落库（WAL + INSERT OR IGNORE），避免多进程写锁竞争。

        容错机制：
        - 每个 worker 进程启动时只 login 一次并复用长连接，避免频繁重连
        - 单只股票失败自动重试 3 次（2s/4s/8s 退避），失败股票汇总告警
        - 每只股票按本地 MAX(date) 计算增量起点，已是最新的自动跳过
        - 已入库数据用 INSERT OR IGNORE，中断后可重跑续传
        """
        import sqlite3
        from datetime import date, timedelta
        from multiprocessing import Pool

        today_str = date.today().strftime("%Y-%m-%d")

        # 一次性读取现有进度，避免逐只查库
        with _connect(self.db_path, timeout=60.0) as conn:
            last_dates = dict(
                conn.execute(
                    "SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol"
                ).fetchall()
            )

        tasks: list = []
        skipped_up_to_date = 0
        for symbol in symbols:
            last_date = last_dates.get(symbol)
            if last_date and last_date >= today_str:
                skipped_up_to_date += 1
                continue
            if last_date:
                start = (
                    date.fromisoformat(last_date) + timedelta(days=1)
                ).strftime("%Y-%m-%d")
            else:
                start = self.start_date
            tasks.append((symbol, self._to_baostock_code(symbol), start, today_str))

        if not tasks:
            logger.info(f"全部 {len(symbols)} 只股票已是最新，无需回填")
            return

        n_workers = max(1, min(n_workers, len(tasks)))
        logger.info(
            f"需回填 {len(tasks)} 只股票（已是最新跳过 {skipped_up_to_date}），"
            f"启动 {n_workers} 进程并行拉取..."
        )

        # 小块切分：既能让结果流式返回，也能让失败只影响一小撮股票
        chunk_size = 10
        chunks = [tasks[i : i + chunk_size] for i in range(0, len(tasks), chunk_size)]

        processed = 0
        total_rows = 0
        failed_symbols: list[str] = []
        value_cols = ["open", "high", "low", "close", "volume", "turnover"]
        insert_cols = ["symbol", "date"] + value_cols

        with _connect(self.db_path, timeout=60.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=60000")

            with Pool(n_workers, initializer=_bs_worker_init) as pool:
                for chunk_idx, (n_syms, rows, failed) in enumerate(
                    pool.imap_unordered(_bs_backfill_worker, chunks), start=1
                ):
                    processed += n_syms
                    failed_symbols.extend(failed)

                    if rows:
                        df = pd.DataFrame(
                            rows,
                            columns=[
                                "symbol", "date", "open", "high", "low",
                                "close", "volume", "turnover",
                            ],
                        )
                        for col in value_cols:
                            df[col] = pd.to_numeric(df[col], errors="coerce")
                        df = df.dropna(subset=["close"])
                        df = df[df["volume"] > 0]

                        if not df.empty:
                            conn.executemany(
                                "INSERT OR IGNORE INTO stock_daily "
                                "(symbol, date, open, high, low, close, volume, turnover) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                df[insert_cols]
                                .astype(object)
                                .itertuples(index=False, name=None),
                            )
                            conn.commit()
                            total_rows += len(df)

                    if chunk_idx % 20 == 0 or processed >= len(tasks):
                        logger.info(
                            f"进度 {processed}/{len(tasks)}，"
                            f"已写入 {total_rows} 行，失败 {len(failed_symbols)} 只"
                        )

        logger.info(
            f"回填完成 — 处理 {processed} 只 | 写入 {total_rows} 行 | "
            f"失败 {len(failed_symbols)} 只"
        )
        if failed_symbols:
            preview = ", ".join(failed_symbols[:20])
            logger.warning(f"以下股票拉取失败（可重跑续传补齐）: {preview} ...")

    # ── 股票列表 ──

    def get_all_symbols(self) -> list[str]:
        """通过 baostock 获取全市场 A 股代码列表（上市状态的股票）。

        同一次全量下载里已经带回股票名称，因此顺便写入本地名称缓存表，
        不额外产生任何网络请求（缓存刷新在回填/日常模式中自动完成）。
        """
        rows = _bs_query_stock_basic()
        symbols = [
            row[0].split(".")[1]
            for row in rows
            if row[4] == "1" and row[5] == "1"  # type=股票 且 status=上市
        ]
        if not symbols:
            return []

        logger.info(f"获取股票列表完成，共 {len(symbols)} 只")
        self.save_stock_names(_rows_to_names(rows))
        return symbols

    def get_local_symbols(self) -> list[str]:
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM stock_daily"
            ).fetchall()
        return [row[0] for row in rows]

    # ── 股票名称本地缓存 ──

    # 名称缓存有效期：超过该天数才会在同步阶段重新联网拉取
    NAME_CACHE_TTL_DAYS: int = 7

    def save_stock_names(self, mapping: dict[str, str]) -> int:
        """把 {纯数字代码: 名称} 写入本地名称缓存表（覆盖更新），返回写入条数。"""
        if not mapping:
            return 0

        updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = [
            (code, name, updated_at)
            for code, name in mapping.items()
            if code and name
        ]
        if not rows:
            return 0

        with _connect(self.db_path) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO stock_name (symbol, name, updated_at) "
                "VALUES (?, ?, ?)",
                rows,
            )
            conn.commit()
        logger.info(f"股票名称缓存已更新，共 {len(rows)} 只")
        return len(rows)

    def get_cached_stock_names(self, symbols: list[str] | None = None) -> dict[str, str]:
        """只读本地名称缓存表，不发起任何网络请求。

        Args:
            symbols: 需要查询的代码列表；为 None 时返回全部缓存。
        """
        rows: list = []
        with _connect(self.db_path) as conn:
            if not symbols:
                rows = conn.execute("SELECT symbol, name FROM stock_name").fetchall()
            else:
                # SQLite 的 IN 参数上限约 999，分批查询避免超限
                for start in range(0, len(symbols), 900):
                    chunk = symbols[start : start + 900]
                    placeholders = ",".join("?" * len(chunk))
                    rows.extend(
                        conn.execute(
                            f"SELECT symbol, name FROM stock_name "
                            f"WHERE symbol IN ({placeholders})",
                            chunk,
                        ).fetchall()
                    )
        return {row[0]: row[1] for row in rows}

    def _name_cache_updated_at(self) -> str | None:
        with _connect(self.db_path) as conn:
            row = conn.execute("SELECT MAX(updated_at) FROM stock_name").fetchone()
        return row[0] if row and row[0] else None

    def _name_cache_is_fresh(self) -> bool:
        last = self._name_cache_updated_at()
        if not last:
            return False
        try:
            age = (datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).days
        except ValueError:
            return False
        return age < self.NAME_CACHE_TTL_DAYS

    def sync_stock_names(self, force: bool = False) -> int:
        """从 baostock 全量刷新名称缓存，返回本次写入条数（跳过刷新时为 0）。

        缓存新鲜（< NAME_CACHE_TTL_DAYS 天）时直接跳过，不联网；
        联网失败只记日志并保留旧缓存，绝不抛异常中断主流程。
        """
        if not force and self._name_cache_is_fresh():
            logger.info(f"股票名称缓存仍新鲜（{self._name_cache_updated_at()}），跳过刷新")
            return 0

        mapping = fetch_stock_names_from_baostock()
        if not mapping:
            logger.warning("股票名称刷新失败，继续沿用本地缓存（可能为空，届时仅展示代码）")
            return 0
        return self.save_stock_names(mapping)

    def get_stock_names(self, symbols: list[str]) -> dict[str, str]:
        """获取 {代码: 名称} 映射，只读本地缓存表（推送阶段零网络依赖）。

        缺失的代码不会出现在返回值里，调用方按「只展示代码」降级即可；
        缓存的联网刷新由 ``sync_stock_names()`` 在数据同步阶段完成。
        """
        return self.get_cached_stock_names(symbols)
