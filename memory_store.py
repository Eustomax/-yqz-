# memory_store.py
# 运行时内存状态：被 json_handle / main_v2 / box_bot 等通过 `from memory_store import *` 共享。
# 本文件只定义初始结构，真实数据在启动时由 json_handle.load_json_files() 从 files/*.json 载入。

# 已进入直播间互动过的观众 uid 集合（全局去重）：
# 支持 .add(uid) / `uid in interact_cache` / list(interact_cache) 互转
interact_cache = set()

# 程序核心内存字典
MEMORY = {
    "all": {},
    "audience": {
        "total_audience": 0,
        "interact_cache": [],
    },
    "box": {},    # uid(str) -> {"count": int, "is_personal_loss_egg_sent": bool, ...}
    "gift": {},   # uid(str) -> {"gift_list": {name: battery}, "profit": int}
    "danmu": {},
    "meta": {
        "current_gear": 0,
        "dog": 0,
        "is_birthday_msg_sent": False,
        "is_castle_msg_sent": False,
        "is_huli_egg_sent": False,
        "is_kfc_msg_sent": False,
        "is_loss_warning_sent": False,
        "is_oil_msg_sent": False,
        "is_whole_profit_msg_sent": False,
        "live_time": 0,
        "next_threshold": 0,
        "title": "",
        "total_battery": 0,
        "total_danmu_cnt_from_start": 0,
    },
}
