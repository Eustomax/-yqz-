# ids.py —— 用户 uid 映射（readme 注明存用户 uid）
# 以下均为占位 0，请替换为真实 uid（整数）。
# GACHI_ID / GACHI_GACHI_ID / REFUSE_WELCOME_LIST 为列表，被 `uid in X` 与 `X[索引]` 使用。

ADMIN_ID = 0            # 管理员（晚安卡米宝宝）uid
YQZ_ID = 0             # 云崎早_haya uid
HULI_ID = 0            # 狐狸狸 uid（huli_egg 彩蛋）
GACHI_ID = [0, 0, 0, 0]   # 某组合 uid 列表（注意 GACHI_ID[3] 被引用，长度至少 4）
XIAOZAO_ID = 0         # 小早生煎 uid
YANCHENGCHUAN_ID = 0   # 盐城船 uid
SHUANGSHUI_ID = 0      # 双水 uid
SHENNAI_ID = 0         # 神奈 uid
JUNBEN_ID = 0          # 闰本 uid
ASPK_ID = 0            # ASPK uid
GACHI_GACHI_ID = [0]   # 彩蛋组合 uid 列表（uid in GACHI_GACHI_ID）
REFUSE_WELCOME_LIST = []  # 拒绝触发欢迎姬的 uid 列表（uid not in REFUSE_WELCOME_LIST）
