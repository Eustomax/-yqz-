# yqz-liverobot 代码优化说明

对 B站直播机器人项目 `yqz-liverobot` 的保守原地优化（保持模块结构与对外行为不变，仅做低风险清理与提取）。

## 优化范围与改动

### 1. 安全加固（main_v2.py）
- CORS：`allow_credentials=True` → `False`（GET 读取不涉及 Cookie，收紧凭证暴露）。
- 热更新接口鉴权：4 个接口（`/api/hot_gift`、`/api/delete_specific_gift`、`/api/send_danmu`、`/api/set_status`）加 `Depends(_check_hot_auth)` Bearer Token 校验，Token 取自环境变量 `BOT_HOT_API_TOKEN`。
- SSL 校验可配置：`DISABLE_SSL_VERIFY` 开关（默认 True，保持原行为），可部署时关闭。

### 2. 配置集中化（常量提取）
- `main_v2.py`：新增 `BILI_GIFT_MAP_SCRIPT`（替代硬编码 `/root/bili/bili_gift_map.py`）、`GEAR_STEP_MIN/MAX`；开播提醒硬编码房间号 `27885573` → `{ROOM_ID}`。
- `gift_bot.py`：新增 `ADMIN_NAME = "卡米宝宝"`（替代 8 处硬编码昵称）、`GEAR_STEP_MIN/MAX`。
- `json_handle.py`：新增 `GEAR_STEP_MIN/MAX`，2 处 `random.randint(4000,5000)` 复用；删除重复注释行。
- `bili_gift_map.py`：`ROOM_ID = 27885573` 常量替代默认参数硬编码。

### 3. 重复逻辑提取
- `send_reply.py`：新增公共函数 `send_split_reply(uid, reply, prefix)`，统一「回复>40字拆分发送」逻辑。
- `box_bot.py` / `gift_bot.py`：9 处 + 3 处回复拆分块改用 `send_split_reply`；`box_bot.py` 新增 `BOX_NAMES_PATTERN`、`net_punctuation()` 净收益标点后缀函数（个人/全场/月度三处复用）。
- `qq_bot.py`：新增 `_abs_url()` 辅助函数，消除 `extract_info` 中 6 处相对链接补全的重复代码。

### 4. 去死代码 / 可读性
- `main_v2.py`：删除 4 处注释死代码块（@room.on 注释、on_danmu/on_gift/GUARD_BUY 注释）。
- `box_bot.py`：删除注释死代码（boxn_calculate 旧逻辑、零散注释）。
- `qq_bot.py`：删重复 `import json`；删除注释掉的 print 循环、send_mixed 调用、未知动态分支注释块；删除冗余 `pass`。
- `eggs.py`：删除约 90 行注释掉的 combo 函数块；`huli_egg` 裸 `except` 改为记录日志；4 处 `print` 异常 → `add_log`。
- `json_handle.py` / `qq_bot.py`：`print` 异常日志改为 `add_log`，统一日志出口。

## 修复的关键问题（本轮）
- `main_v2.py` 第 732 行附近 `try` 体缩进错误（12 空格）→ 修正为 8 空格，消除 `IndentationError`。
- `gift_bot.py` 中 `replace_all` 误将常量定义 `ADMIN_NAME = "卡米宝宝"` 改写为 `ADMIN_NAME = "{ADMIN_NAME}"`，已修正为正确值。

## 校验
- `python -m py_compile` 对全部 13 个 .py 模块（含 web/）通过，无语法错误。
- 注意：仓库缺 `memory_store.py` / `constants.py` / `ids.py` / `data.py` 等运行期文件，仅做语法校验，未做运行时验证。

## 环境变量注入规范化（.env.example）
- 新增 `.env.example`：列出 `BILI_SESSDATA`/`BILI_JCT`/`BILI_BUVID3`（必填凭证）+ `BOT_HOT_API_TOKEN`（可选热更新接口鉴权），附取值说明。
- `data.py` 与 `main_v2.py` 顶部加容错 `load_dotenv()`：有 python-dotenv 时自动加载 `.env`，缺失则静默忽略（仍可用真实环境变量），不强制依赖。
- 新增 `.gitignore`：忽略 `.env`（防凭证泄露）、`__pycache__/`、`*.pyc`、运行期 `*.json` 与 `log_*.txt`。
- 校验：`py_compile` 通过；在托管 venv 装 python-dotenv 后实测 `data.py` 经 `.env` 读到注入值（RESULT: .env 注入链路 OK），测后清理临时 `.env`。
- 部署提示：需 `pip install python-dotenv` 才能让 `.env` 自动生效；复制 `.env.example` 为 `.env` 填入真实值。

## 未改动
- `gift_egg`（eggs.py）保留为 `pass`：在 `main_v2.py:458` 与 `hotreload_config.py` 中均有引用，属已接线的空实现，未删除。

## 补充：缺失的运行期模板文件（2026-08-13 后续）
仓库原缺 `memory_store.py` / `constants.py` / `ids.py` / `data.py`（readme 注明这些文件未入库）。已补齐模板，使项目可 `import` 通过：

- **`memory_store.py`**：定义运行时内存 `MEMORY`（含 `all/audience/box/gift/danmu/meta` 全部 key，结构与 `json_handle.load_json_files` 访问路径一致）+ 全局 `interact_cache = set()`（已互动观众 uid 集合，支持 `.add()` / `in` / `list()`）。
- **`constants.py`**：配置类常量 `ROOM_ID`、推送配置（`TARGET_GROUP`/`TARGET_GROUP_LIST`/`TARGET_GROUP_FANS`/`NAPCAT_API`/`TOKEN`）、行为参数（`CHECK_INTERVAL`/`DANMU_COUNT`）、礼物姬价格阈值（`COMBO_GUARD_PRICE`/`GUARD_FIRST_PRICE`）、盲盒姬（`BOX_NAME_LIST`/`BOX_MEMORY_MAP`/`CN_MONTHS`）、欢迎姬（`WELCOME_MAP`）。
- **`ids.py`**：用户 uid 映射（`ADMIN_ID`/`YQZ_ID`/`HULI_ID`/`GACHI_ID`/`XIAOZAO_ID`/`YANCHENGCHUAN_ID`/`SHUANGSHUI_ID`/`SHENNAI_ID`/`JUNBEN_ID`/`ASPK_ID`/`GACHI_GACHI_ID`/`REFUSE_WELCOME_LIST`），均为占位 `0`/`[]`，需替换为真实 uid。
- **`data.py`**：凭证 `SESSDATA`/`BILI_JCT`/`BUVID3`，改为从环境变量 `BILI_SESSDATA`/`BILI_JCT`/`BILI_BUVID3` 读取（安全加固，避免明文入库）。

### 模板校验
- 4 文件 `py_compile` 通过；可正常 `import`。
- 符号覆盖校验：所有被 `import *` 引用的符号均已定义（`ROOM_ID` 因 `bili_gift_map.py` 同名定义被静态分析误判为「已存在」，实际 `main_v2` 经 `from constants import *` 取用，已在 `constants.py` 补齐）。
- `MEMORY` 初始结构逐级校验：所有 `json_handle` 实际访问路径均存在，无 `KeyError` 风险。
- 导入轻依赖模块 `json_handle` 成功，验证 `memory_store`/`constants` 经 `import *` 符号足够。

### 部署前需用户替换的占位
- `ids.py` 中所有 `*_ID` / 列表中的 `0` → 真实 uid。
- `constants.py` 中 `TARGET_GROUP*` 群号、`NAPCAT_API` 地址、`TOKEN`、`ROOM_ID`（如需改房间）。
- 运行前导出 `BILI_SESSDATA`/`BILI_JCT`/`BILI_BUVID3` 环境变量。
