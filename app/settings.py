"""Static source definitions and application defaults."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from .models import SourceConfig


PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DATABASE_PATH = DATA_DIR / "notices.db"
HOST = "127.0.0.1"
PORT = 8765
REQUEST_CONNECT_TIMEOUT = 4.0
REQUEST_READ_TIMEOUT = 12.0
REQUEST_INTERVAL_SECONDS = 0.8
KNOWN_STREAK_STOP = 20
LAST_PAGE_GRACE_SECONDS = 10.0
# 浏览器可能把后台标签页冻结数小时，短期心跳缺失不能作为关闭依据。正常关闭由
# 每标签页 pagehide 信标上报；该值只用于清理没有报告暂停状态的超长期孤儿。
HEARTBEAT_ORPHAN_SECONDS = 24 * 60 * 60.0
# 主循环两次轮询相隔超过这个时间，说明系统睡眠/休眠过，这段时间不算心跳过期。
SUSPEND_GAP_SECONDS = 30.0

SOURCES: tuple[SourceConfig, ...] = (
    SourceConfig(
        key="future_notices",
        name="未来科学与工程学院·通知公告",
        base_url="https://future.suda.edu.cn",
        list_path="/30836/list.htm",
        category="通知公告",
        site_key="future_suda",
        history_start=date(2025, 7, 1),
    ),
    SourceConfig(
        key="future_aid",
        name="未来科学与工程学院·奖惩助贷",
        base_url="https://future.suda.edu.cn",
        list_path="/30819/list.htm",
        category="奖惩助贷",
        site_key="future_suda",
        history_start=date(2025, 7, 1),
    ),
    SourceConfig(
        key="jwc_notices",
        name="苏州大学教务处·通知公告",
        base_url="https://jwc.suda.edu.cn",
        list_path="/8620/list.htm",
        category="通知公告",
        site_key="jwc_suda",
        history_start=date(2025, 7, 1),
    ),
)
