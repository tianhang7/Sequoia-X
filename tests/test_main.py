"""主程序入口属性测试。"""

import json
import sys
from datetime import date
from unittest.mock import patch

import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

# 预先导入 main 模块，避免在 @given 循环中重复导入
import main as main_module


# Feature: sequoia-x-v2, Property 13: 主程序异常以非零退出码终止
@given(error_msg=st.text(min_size=1, max_size=100))
@h_settings(max_examples=30, deadline=None)
def test_main_exits_nonzero_on_exception(error_msg: str) -> None:
    """属性 13：main() 中任意未捕获异常应导致 sys.exit(1)。"""
    # patch main 模块中直接引用的 get_settings
    with patch.object(main_module, "get_settings", side_effect=RuntimeError(error_msg)):
        with pytest.raises(SystemExit) as exc_info:
            main_module.main()
        assert exc_info.value.code != 0


def test_archive_selections_writes_json(tmp_path) -> None:
    """归档：选股结果应写入 selections_YYYYMMDD.json 且结构完整。"""
    from sequoia_x.core.config import Settings

    settings = Settings(
        db_path=str(tmp_path / "t.db"),
        start_date="2024-01-01",
        selections_dir=str(tmp_path / "sel"),
    )
    selections = {
        "TurtleTradeStrategy": {
            "raw_count": 2,
            "count": 1,
            "dropped": {"limit_up": 1},
            "symbols": ["600001"],
        }
    }
    main_module._archive_selections(settings, "2026-09-24", "2026-09-24", selections)

    out = tmp_path / "sel" / f"selections_{date.today():%Y%m%d}.json"
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["as_of"] == "2026-09-24"
    assert data["latest_trade_date"] == "2026-09-24"
    assert data["strategies"]["TurtleTradeStrategy"]["symbols"] == ["600001"]
    assert data["strategies"]["TurtleTradeStrategy"]["dropped"] == {"limit_up": 1}
