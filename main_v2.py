import asyncio
import time
import json
import ssl
import aiohttp
import os
import importlib
from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from bilibili_api import live, sync, Credential, user, select_client
from bilibili_api.live import LiveDanmaku
from data import SESSDATA, BILI_JCT, BUVID3
import random
from datetime import datetime
import re
from pydantic import BaseModel
import subprocess
import sys
from typing import Optional, Union
import signal, atexit, traceback

from ids import *
from logger import add_log, log_buffer
from memory_store import *
from constants import *
from json_handle import load_json_files, save_json, append_to_jsonl
from send_reply import reply_worker, name_to_uid
from box_bot import call_box, call_all_box, call_at_box, call_month_box, call_month_all_box, call_month_at_box
from qq_bot import QQBot, dynamic_monitor
from mail import send_email

import gift_bot
from gift_bot import *
import eggs
from eggs import *
from hotreload_config import HOT_RELOAD_CONFIG

# request_settings.set("impersonate", "chrome131")
select_client("aiohttp")

# ===== 运行配置（集中管理，便于部署调整）=====
# 刷新礼物映射表的脚本路径（默认与本项目同目录，原硬编码 /root/bili/bili_gift_map.py）
BILI_GIFT_MAP_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bili_gift_map.py")
# 推进"你看又⭕️"档位的随机步长（电池数）
GEAR_STEP_MIN, GEAR_STEP_MAX = 4000, 5000
# 支持从 .env 注入环境变量（与 data.py 一致；缺失则忽略）。data 已在上方 import 时加载过，
# 此处再加载一次保证幂等，使本入口在 data 导入顺序变化后仍可自洽。
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass
# 热更新接口鉴权：设置环境变量 BOT_HOT_API_TOKEN 后，调用 /api/* 需带 Authorization: Bearer <token>；
# 未设置则不校验，兼容仅本机访问的旧部署
HOT_API_TOKEN = os.getenv("BOT_HOT_API_TOKEN")

def _check_hot_auth(authorization: str = Header(None)):
    if not HOT_API_TOKEN:
        return
    if authorization != f"Bearer {HOT_API_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")

STATUS = 1
LIVE_STATUS = 0

# code hot update
_hot_reload_state = {}

def check_hot_reload():
    for filename, state in _hot_reload_state.items():
        current_mtime = os.path.getmtime(state['path'])
        if current_mtime == state['mtime']:
            continue

        mod = sys.modules.get(state['module'])
        if mod is None:
            continue

        try:
            importlib.reload(mod)
            g = globals()
            for func_name in state['functions']:
                if hasattr(mod, func_name):
                    g[func_name] = getattr(mod, func_name)
            
            # constant
            constants_to_update = state.get('constants', [])
            if constants_to_update:
                new_values = {name: getattr(mod, name) for name in constants_to_update if hasattr(mod, name)}
            
                for name, val in new_values.items():
                    g[name] = val
                
                for other_mod_name, other_mod in sys.modules.items():
                    if not other_mod_name or other_mod_name == '__main__':
                        continue
                    if other_mod is None:
                        continue
                    
                    for name, val in new_values.items():
                        if hasattr(other_mod, name):
                            try:
                                setattr(other_mod, name, val)
                            except (AttributeError, TypeError):
                                pass
                
                add_log(f"[HOT RELOAD] {filename} updated ({len(state['functions'])} funcs, {len(constants_to_update)} consts)")
            else:
                add_log(f"[HOT RELOAD] {filename} updated ({len(state['functions'])} funcs)")
            
            state['mtime'] = current_mtime
            
        except Exception as e:
            add_log(f"[HOT RELOAD ERROR] {filename}: {e}")

# 用于danmu_egg的所有时间戳（已弃用）
last_gachi_danmu_trigger = 0
last_question_mark_trigger = 0
last_circle_trigger = 0
last_good_night_trigger = 0
last_haha_trigger = 0

# total_battery = 0
# is_loss_warning_sent = False
# current_gear = 0

async def init_get_room_status():
    global LIVE_STATUS
    room1 = live.LiveRoom(ROOM_ID)
    try:
        info = await room1.get_room_info()
        status = info["room_info"]["live_status"]
        LIVE_STATUS = 1 if status == 1 else 0
        add_log(f"Initial LIVE_STATUS: {LIVE_STATUS}")
    except Exception as e:
        add_log(f"[ERROR] Failed to get initial LIVE_STATUS: {e}")

# 计算大航海连开数量
def calc_guard_combo(gift_name, total_battery):
    base = COMBO_GUARD_PRICE.get(gift_name, 1680)
    remainder = total_battery % base
    first_map = GUARD_FIRST_PRICE.get(gift_name, {})

    if remainder in first_map:
        first_price = first_map[remainder]
        if total_battery <= first_price:
            return 1, first_price, base
        cnt = (total_battery - first_price) // base + 1
        return cnt, first_price, base

    cnt = max(1, round(total_battery / base))
    avg = total_battery // cnt
    return cnt, avg, avg

# 内存更新
def update_all_log(uid, uname, gift_name, battery):
    if gift_name in COMBO_GUARD_PRICE:
        cnt, first_price, follow_price = calc_guard_combo(gift_name, battery)
        for i in range(cnt):
            single = first_price if i == 0 else follow_price
            MEMORY["all"].append({
                "uid": int(uid),
                "uname": uname,
                "time": int(time.time()),
                "gift_name": gift_name,
                "gift_price": single
            })
    else:
        MEMORY["all"].append({
            "uid": int(uid),
            "uname": uname,
            "time": int(time.time()),
            "gift_name": gift_name,
            "gift_price": battery
        })

def update_danmu_log(uid, uname, msg):
    MEMORY["danmu"].append({
        "uid": int(uid), "uname": uname, "time": int(time.time()), "danmu": msg
    })

def update_gift_summary(uid, uname, gift_name, num, battery):
    uid_str = str(uid)
    if uid_str not in MEMORY["gift"]:
        MEMORY["gift"][uid_str] = {"uid": int(uid), "uname": uname, "gift_list": {}, "profit": 0}
    user = MEMORY["gift"][uid_str]
    user["uname"] = uname
    user["profit"] += battery
    if gift_name:
        user["gift_list"][gift_name] = user["gift_list"].get(gift_name, 0) + num

def update_box_summary(uid, uname, count, cost, profit, original_box_name):
    original_box_name = BOX_MEMORY_MAP.get(original_box_name, original_box_name)
    uid_str = str(uid)
    if uid_str not in MEMORY["box"]:
        MEMORY["box"][uid_str] = {
            "uid": int(uid),
            "uname": uname,
            "count": 0,
            "cost": 0,
            "profit": 0,
            "info": {},
            "cost_detail": {},
            "profit_detail": {},
            "is_personal_loss_egg_sent": False
        }
    user = MEMORY["box"][uid_str]
    user["uname"] = uname
    user["count"] += count
    user["cost"] += cost
    user["profit"] += profit
    user["info"][original_box_name] = user["info"].get(original_box_name, 0) + count
    user.setdefault("cost_detail", {})
    user.setdefault("profit_detail", {})
    user["cost_detail"][original_box_name] = user["cost_detail"].get(original_box_name, 0.0) + cost
    user["profit_detail"][original_box_name] = user["profit_detail"].get(original_box_name, 0.0) + profit

# 大航海特殊判断
async def record_to_guard_log(uid, uname, price, guard_level, start_time, source="GUARD"):
    if any(p_uid == uid and p_time == start_time for p_uid, p_time in processed_records):
        return

    price = int(price)

    if price <= 0:
        price = {1: 199980, 2: 19980, 3: 1980}.get(guard_level, 1980)

    guard_name = {1: "总督", 2: "提督", 3: "舰长"}.get(guard_level, "大航海")

    cnt, _, _ = calc_guard_combo(guard_name, price)

    if guard_name == "舰长":
        if price == 198: guard_name = "舰长*3天"
        elif price == 330: guard_name = "舰长*5天"
        elif price == 528: guard_name = "舰长*8天"
    elif guard_name == "提督":
        if price == 1998: guard_name = "提督*3天"
        elif price == 6660: guard_name = "提督*10天"
    elif guard_name == "总督":
        if price == 33330: guard_name = "总督*5天"

    processed_records.append((uid, start_time))
    if len(processed_records) > 200: processed_records.pop(0)
    
    if guard_name in ["舰长*3天", "舰长*5天", "舰长*8天", "提督*3天", "提督*10天", "总督*5天"]:
        update_box_summary(uid, uname, 1, 500, price, "大航海盲盒")
        save_json("files/box.json", MEMORY["box"])
        await check_global_loss_warning(uid, uname)
    update_gift_summary(uid, uname, guard_name, cnt, price)
    update_all_log(uid, uname, guard_name, price)
    add_log(f"[{source}] {uname} {guard_name}x{cnt} ({price} 电池)")

    reply = thank_gift(uid, uname, guard_name, price, cnt)
    if reply:
        await handle_thank_reply(uid, uname, reply)

    MEMORY["meta"]["total_battery"] += price
    save_json("files/meta.json", MEMORY["meta"])
    await handle_total_gift_reply(YQZ_ID, MEMORY["meta"]["total_battery"])
    check_hot_reload()
    await huli_egg(uid, guard_name)
    await guard_egg(uid, uname, guard_name, price, cnt)
    await check_gachi_egg(uid, guard_name, price)

def on_gift_saved():
    add_log("HTML refresh triggered")

# 周期性任务
async def periodic_tasks():
    global last_save_time, last_log_save, LIVE_STATUS
    try:
        while True:
            now = int(time.time())
            if now - last_save_time > 30:
                print(f"LIVE_STATUS: {LIVE_STATUS}")
                # save_json("files/box.json", MEMORY["box"])
                save_json("files/gift.json", MEMORY["gift"])
                save_json("files/all.json", MEMORY["all"])
                save_json("files/meta.json", MEMORY["meta"])

                if MEMORY["danmu"]:
                    append_to_jsonl("files/danmu.jsonl", MEMORY["danmu"])
                    MEMORY["meta"]["total_danmu_cnt_from_start"] += len(MEMORY["danmu"])
                    MEMORY["danmu"].clear()

                MEMORY["audience"]["interact_cache"] = list(interact_cache)
                save_json("files/audience.json", MEMORY["audience"])

                try:
                    room_info = live.LiveRoom(ROOM_ID)
                    info = await room_info.get_room_info()
                    real_status = 1 if info["room_info"]["live_status"] == 1 else 0
                    
                    if real_status == 1 and LIVE_STATUS == 0:
                        add_log(f"[轮询兜底] 检测到已开播但 LIVE_STATUS=0，自动修正")
                        LIVE_STATUS = 1
                        MEMORY["meta"]["live_time"] = now
                        MEMORY["meta"]["title"] = info["room_info"].get("title", "")
                        save_json("files/meta.json", MEMORY["meta"])
                        try:
                            process = subprocess.Popen(
                                [sys.executable, BILI_GIFT_MAP_SCRIPT],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE
                            )
                            add_log("bili_gift_map.json loaded")
                        except Exception as e:
                            add_log(f"Failed to run bili_gift_map.py: {e}")

                    elif real_status == 0 and LIVE_STATUS == 1:
                        add_log(f"[轮询兜底] 检测到下播但 LIVE_STATUS=1，自动修正")
                        LIVE_STATUS = 0
                        prepare_time = datetime.now().strftime("%H:%M")
                        prepare_timestamp = int(time.time())
                        live_start_ts = MEMORY["meta"].get("live_time", prepare_timestamp)
                        live_length = (prepare_timestamp - live_start_ts) // 60
                        live_hours = live_length // 60
                        live_mins = live_length % 60
                        
                        if live_hours == 0 and live_mins == 0:
                            time_length = "不足1分钟"
                        elif live_hours == 0:
                            time_length = f"{live_mins}分钟"
                        elif live_mins == 0:
                            time_length = f"{live_hours}小时"
                        else:
                            time_length = f"{live_hours}小时{live_mins}分钟"
                        
                        live_start_time = time.strftime("%H:%M", time.localtime(live_start_ts))
                        title = MEMORY["meta"].get("title", "天  才  主  播  ！")
                        
                        try:
                            await send_email(live_start_time, prepare_time, time_length, title)
                            add_log(f"[轮询兜底] 已补发下播邮件，时长: {time_length}")
                        except Exception as e:
                            add_log(f"[轮询兜底] 补发下播邮件失败: {e}")

                except Exception as e:
                    add_log(f"[轮询检测] 获取房间信息失败: {e}")

                last_save_time = now
                add_log("All json files saved")
                on_gift_saved()
            if now - last_log_save > 5:
                save_json("files/log.json", log_buffer.buffer)
                last_log_save = now
            await asyncio.sleep(2)
    except asyncio.CancelledError:
        add_log(f"main.py cancelled. saving json files.")
        if MEMORY["gift"]:
            save_json("files/gift.json", MEMORY["gift"])
        if MEMORY["all"]:
            save_json("files/all.json", MEMORY["all"])
        if MEMORY["meta"]:
            save_json("files/meta.json", MEMORY["meta"])
        if MEMORY["audience"]:
            MEMORY["audience"]["interact_cache"] = list(interact_cache)
            save_json("files/audience.json", MEMORY["audience"])
        if MEMORY["danmu"]:
            append_to_jsonl("files/danmu.jsonl", MEMORY["danmu"])

        add_log("Emergency data save completed successfully.")
        raise

# 监听
room = live.LiveDanmaku(ROOM_ID, credential=credential)

box_names_pattern = '|'.join(re.escape(name) for name in BOX_NAME_LIST)

@room.on('DANMU_MSG')
async def on_danmaku(event):
    global LIVE_STATUS
    data = event['data']['info']
    msg, uid, uname = data[1], data[2][0], data[2][1]
    

    if msg == "呼叫礼物姬":
        await call_gift(uid, uname)
    elif "呼叫礼物姬@" in msg:
        await call_at_gift(uid, uname, msg)
    
    # 月度全局盲盒姬
    elif re.search(rf'^呼叫(?:\d{{1,2}}|一|二|三|四|五|六|七|八|九|十|十一|十二)月(?:{box_names_pattern})?盲盒姬总部$', msg):
        await call_month_all_box(uid, uname, msg)
    
    # 指定uid月度盲盒姬
    elif re.search(rf'^呼叫(?:\d{{1,2}}|一|二|三|四|五|六|七|八|九|十|十一|十二)月(?:{box_names_pattern})?盲盒姬@(\d+)$', msg):
        await call_month_at_box(uid, uname, msg)
    
    # 月度盲盒姬
    elif re.search(rf'^呼叫(?:\d{{1,2}}|一|二|三|四|五|六|七|八|九|十|十一|十二)月(?:{box_names_pattern})?盲盒姬$', msg):
        await call_month_box(uid, uname, msg)
    
    # 全局盲盒姬
    elif re.search(rf'^呼叫(?:{box_names_pattern})?盲盒姬总部$', msg):
        await call_all_box(uid, uname, msg)
    
    # 指定uid盲盒姬
    elif re.search(rf'^呼叫(?:{box_names_pattern})?盲盒姬@(\d+)$', msg):
        await call_at_box(uid, uname, msg)
    
    # 盲盒姬
    elif re.search(rf'^呼叫(?:{box_names_pattern})?盲盒姬$', msg):
        await call_box(uid, uname, msg)

    if LIVE_STATUS == 1:
        # check_hot_reload()
        await danmu_egg(DANMU_COUNT)
        update_danmu_log(uid, uname, msg)

@room.on('SEND_GIFT')
async def on_gift(event):
    global LIVE_STATUS
    # print(f"SEND_GIFT: {event}")
    if LIVE_STATUS != 1:
        return
    data = event['data']['data']
    uid, gift_name, num = data.get('uid'), data.get('giftName'), data.get('num', 1)
    uname = data.get('sender_uinfo', {}).get('base', {}).get('name', '用户')
    blind_data = data.get('blind_gift') or (data.get('batch_combo_send') and data['batch_combo_send'].get('blind_gift'))

    if blind_data:
        original_box_name = blind_data.get('original_gift_name', "盲盒")
        bg_cost_battery = blind_data.get('original_gift_price', 0) / 100
        g_profit_battery = blind_data.get('gift_tip_price', 0) / 100
        update_box_summary(uid, uname, num, bg_cost_battery*num, g_profit_battery*num, original_box_name)
        save_json("files/box.json", MEMORY["box"])
        update_gift_summary(uid, uname, gift_name, num, g_profit_battery*num)
        update_all_log(uid, uname, gift_name, g_profit_battery*num)
        add_log(f"[盲盒] {uname} x{num} ({g_profit_battery*num:.1f} 电池)")

        check_hot_reload()
        await box_egg(uid, uname, gift_name, num, bg_cost_battery, g_profit_battery)
        await huli_egg(uid, gift_name)
        await check_gachi_egg(uid, None, g_profit_battery)
        await check_global_loss_warning(uid, uname)

        if g_profit_battery >= 1000:
            reply = thank_gift(uid, uname, gift_name, g_profit_battery)
            if reply:
                await handle_thank_reply(uid, uname, reply)
        MEMORY["meta"]["total_battery"] += g_profit_battery*num
        await handle_total_gift_reply(YQZ_ID, MEMORY["meta"]["total_battery"])

    else:

        total_coin = data.get('total_coin', 0)
        if total_coin > 0:
            battery = total_coin / 100
        else:
            battery = (data.get('price', 0) * num) / 100
        
        single_battery = battery / num if num > 0 else battery
        
        update_gift_summary(uid, uname, gift_name, num, battery)
        update_all_log(uid, uname, gift_name, single_battery)
        add_log(f"[礼物] {uname} {gift_name}x{num} ({battery:.1f} 电池)")

        if battery >= 1000:
            reply = thank_gift(uid, uname, gift_name, single_battery)
            if reply:
                await handle_thank_reply(uid, uname, reply)
        MEMORY["meta"]["total_battery"] += battery
        check_hot_reload()
        await gift_egg(uid, uname, gift_name, num, single_battery)
        await huli_egg(uid, gift_name)
        await handle_total_gift_reply(YQZ_ID, MEMORY["meta"]["total_battery"])
        await check_gachi_egg(uid, None, single_battery)

@room.on('SUPER_CHAT_MESSAGE')
async def on_sc(event):
    # print(f"SUPER_CHAT_MESSAGE: {event}")
    global LIVE_STATUS
    if LIVE_STATUS != 1:
        return
    data = event['data']['data']
    uid, price, uname, content = data.get('uid'), data.get('price', 0), data.get('user_info', {}).get('uname', '用户'), data.get('message', '')
    battery = price * 10
    
    start_time = int(time.time())
    if any(p_uid == uid and p_battery == battery and p_time == start_time for p_uid, p_battery, p_time in processed_sc_records):
        return
    processed_sc_records.append((uid, battery, start_time))
    if len(processed_sc_records) > 10:
        processed_sc_records.pop(0)

    append_to_jsonl("files/superchat.jsonl", [{
        "uid": uid,
        "uname": uname,
        "time": start_time,
        "battery": battery,
        "content": content
    }])

    update_gift_summary(uid, uname, "SuperChat", 1, battery)
    update_all_log(uid, uname, "SuperChat", battery)
    add_log(f"[SuperChat] {uname} ({price}元)")
    
    if battery >= 1000:
        reply = thank_gift(uid, uname, "SuperChat", battery)
        if reply:
            await handle_thank_reply(uid, uname, reply)
    MEMORY["meta"]["total_battery"] += battery
    await handle_total_gift_reply(YQZ_ID, MEMORY["meta"]["total_battery"])
    check_hot_reload()
    await huli_egg(uid, "SuperChat")
    await sc_egg(uid, uname, battery, content)
    await check_gachi_egg(uid, None, battery)

@room.on('USER_TOAST_MSG')
async def handle_toast(event):
    global LIVE_STATUS
    # print(f"USER_TOAST_MSG: {event}")
    if LIVE_STATUS != 1:
        return
    data = event['data']['data']
    guard_level = data.get('guard_level')
    if guard_level and guard_level in [1, 2, 3]:
        await record_to_guard_log(
            uid=data.get('uid'), uname=data.get('username'),
            price=data.get('price', 0) / 100, guard_level=data.get('guard_level'),
            start_time=data.get('start_time'), source="大航海"
        )
    else:
        pass


@room.on('INTERACT_WORD_V2')
async def interact_word(event):
    global interact_cache, LIVE_STATUS, STATUS
    if LIVE_STATUS != 1:
        return

    data = event['data']['data']
    pb_decoded = data.get('pb_decoded', {})
    if not pb_decoded:
        return
    uname = pb_decoded.get('uname')
    user_info = pb_decoded.get('user_info', {})
    medal = user_info.get('medal') if user_info else None
    uid = user_info.get('uid', 0) if user_info else 0
    reply = None
    
    if uid in interact_cache and uid != ADMIN_ID:
        if uid == YQZ_ID:
            reply = f"[欢迎姬]报告！发现云崎早_haya同学进入直播间！"
            add_log("[欢迎姬] 云崎早同学又一次进入直播间")
            await reply_queue.put((YQZ_ID, reply))
        return

    if uid == ADMIN_ID:
        if STATUS != 0:
            if STATUS == 2: STATUS = 1
            if uid in interact_cache:
                return
            target_date = datetime(2026, 3, 20)
            today = datetime.now().date()
            days_passed = abs((target_date.date() - today).days) + 1
            if today.month == 5 and today.day == 3:
                reply = "[欢迎姬]今天是云宝的生日哎！卡米宝宝祝全世界最最最可爱的云宝生日快乐！"
            elif today.month == 10 and today.day == 3:
                reply = "[欢迎姬]今天是卡米宝宝的生日哎！"
            elif days_passed % 100 == 0 or days_passed == 50:
                reply = f"[欢迎姬]哇！今天是卡米宝宝和云宝相遇的{days_passed}天哎！{days_passed}天快乐！"
            elif today.month == 3 and today.day == 20:
                years_passed = today.year - 2026
                reply = f"[欢迎姬]哇！今天是卡米宝宝和云宝相遇的{years_passed}周年哎！{years_passed}周年快乐！"
            else:
                reply = f"[欢迎姬]报告！发现{days_passed}个卡米宝宝进入云宝的直播间！"
        else:
            reply = "[欢迎姬]哎？卡米宝宝不是睡了吗？怎么又回来了？"
            STATUS = 1
            if uid in interact_cache:
                await reply_queue.put((YQZ_ID, reply))
                add_log(f"[欢迎姬] 卡米宝宝没有睡觉")
                return

    elif uid == ASPK_ID:
        reply = f"[欢迎姬]欢迎帅神！！"
    elif medal and uid not in REFUSE_WELCOME_LIST:
        medal_name = medal.get('name', None)
        medal_level = medal.get('level', 0)
        if medal_name == "早崎鸭":
            if uid == YQZ_ID:
                if int(time.time()) - MEMORY["meta"]["live_time"] <= 120:
                    reply = "[欢迎姬]欢迎云宝！今天也要认真工作哦！"
                else:
                    reply = "[欢迎姬]报告！发现云崎早_haya同学进入直播间！"
            elif uid in WELCOME_MAP:
                reply = WELCOME_MAP[uid].format(uname=uname)
            elif medal_level > 30:
                if len(uname) > 16:
                    uname = uname[:13] + "..."
                reply = f"[欢迎姬]报告！一只叫{uname}的早崎鸭偷偷进入了直播间！" 

    MEMORY["audience"]["total_audience"] = MEMORY["audience"].get("total_audience", 0) + 1
    interact_cache.add(uid)
    MEMORY["audience"]["interact_cache"] = list(interact_cache)
    # save_json("files/audience.json", MEMORY["audience"])
            
    if reply is not None:
        if uid in GACHI_GACHI_ID or uid == GACHI_ID[3] or uid == ADMIN_ID:
            await reply_queue.put((YQZ_ID, reply))
        else:
            await reply_queue.put((uid, reply))
        add_log(f"[欢迎姬] 欢迎{uname}")

@room.on('COMMON_NOTICE_DANMAKU')
async def on_common_notice_danmaku(event):
    global LIVE_STATUS
    if LIVE_STATUS != 1:
        return

    data = event.get('data', {}).get('data', [])
    if data == [] or data == {}:
        return

    content_segments = data.get('content_segments', [])
    
    if content_segments[1].get('text', '') == "投喂":
        uname, gift_name = content_segments[0]['text'], content_segments[2]['text']
        if gift_name == "大航海盲盒":
             return
        try:
            uid = await name_to_uid(uname, SESSDATA, BILI_JCT, BUVID3) or None
        except Exception as e:
            uid = None
            add_log(f"[ERROR] name_to_uid failed: {e}")

        if uid is None:
            with open('error.json', 'a', encoding="utf-8") as f:
                to_dump = {
                    "time": int(time.time()),
                    "uname": uname,
                    "gift_name": gift_name
                }
                json.dump(to_dump, f, ensure_ascii=False, indent=2)
                f.write('\n')
            return

        with open('bili_gift_map.json', 'r', encoding="utf-8") as f:
            gift_map = json.load(f)
            gift_value = 0
            for gift_id, gift_details in gift_map.items():
                if gift_name == gift_details["name"]:
                    gift_value = gift_details["price"] * 10
                    break
            else:
                with open('error.json', 'a', encoding="utf-8") as ff:
                    to_dump = {
                        "time": int(time.time()),
                        "uid": uid,
                        "uname": uname,
                        "gift_name": gift_name
                    }
                    json.dump(to_dump, ff, ensure_ascii=False, indent=2)
                    ff.write('\n')
                return

        update_gift_summary(uid, uname, gift_name, 1, gift_value)
        update_all_log(uid, uname, gift_name, gift_value)
        add_log(f"[礼物 (COMMON_NOTICE_DANMAKU)] {uname} {gift_name} ({gift_value:.1f} 电池)")

        if gift_value >= 1000:
            reply = thank_gift(uid, uname, gift_name, gift_value)
            if reply:
                await handle_thank_reply(uid, uname, reply)
        MEMORY['meta']['total_battery'] += gift_value
        check_hot_reload()
        await huli_egg(uid, gift_name)
        await handle_total_gift_reply(YQZ_ID, MEMORY["meta"]["total_battery"])
        await check_gachi_egg(uid, None, gift_value)

@room.on('LIVE')
async def on_live(event):
    global LIVE_STATUS
    LIVE_STATUS = 1
    add_log("[LOG] LIVE")
    live_time = datetime.now().strftime("%H:%M")
    live_timestamp = int(time.time())
    if live_timestamp - MEMORY["meta"]["live_time"] <= 5:
        return
    MEMORY["meta"]["live_time"] = live_timestamp
    # save_json("files/meta.json", MEMORY["meta"])

    title = MEMORY["meta"].get("title", "")
    room_data = {}
    try:
        room_info = live.LiveRoom(ROOM_ID)
        info = await room_info.get_room_info()
        room_data = info.get("room_info", {})
        title = room_data.get("title", "")
        MEMORY["meta"]["title"] = title
    except Exception as e:
        add_log(f"[ERROR] 获取直播间标题失败: {e}")
    
    save_json("files/meta.json", MEMORY["meta"])

    if not qq:
        return

    try:    
        cover = room_data.get("cover", "")
        segments = [
            {"type": "text", "data": {"text": "【推送姬】开播提醒\n云崎早_haya 开播啦！\n"}},
        ]
        if cover:
            segments.append({"type": "image", "data": {"file": cover}})
            segments.append({"type": "text", "data": {"text": "\n"}})
        segments.append({
            "type": "text",
            "data": {"text": f"标题：{title}\n房间号：{ROOM_ID}\n开播时间：{live_time}\n直播间：https://live.bilibili.com/{ROOM_ID}\n快来一起观看吧~！"}
        })

        # for gid in TARGET_GROUP_LIST:
        #     await qq.send_mixed(segments, at_all=True, group_id=gid)
        #     await asyncio.sleep(3)
        tasks = [qq.send_mixed(segments, at_all=True, group_id=gid) for gid in TARGET_GROUP_LIST]
        await asyncio.gather(*tasks, return_exceptions=True)
        # await qq.send_mixed(segments, at_all=True, group_id=TARGET_GROUP)
        # await asyncio.sleep(5)
        # await qq.send_mixed(segments, at_all=True, group_id=TARGET_GROUP_FANS)

    except Exception:
        segments = [{
            "type": "text",
            "data": {"text": f"【推送姬】开播提醒\n云崎早_haya 开播啦！\n标题：{title}\n房间号：{ROOM_ID}\n开播时间：{live_time}\n直播间：https://live.bilibili.com/{ROOM_ID}\n快来一起观看吧~！"}
        }]

        # for gid in TARGET_GROUP_LIST:
        #     await qq.send_mixed(segments, at_all=True, group_id=gid)
        #     await asyncio.sleep(3)
        tasks = [qq.send_mixed(segments, at_all=True, group_id=gid) for gid in TARGET_GROUP_LIST]
        await asyncio.gather(*tasks, return_exceptions=True)
        # await qq.send_mixed(segments, at_all=True, group_id=TARGET_GROUP)
        # await asyncio.sleep(5)
        # await qq.send_mixed(segments, at_all=True, group_id=TARGET_GROUP_FANS)

    try:
        process = subprocess.Popen(
            [sys.executable, BILI_GIFT_MAP_SCRIPT],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        add_log("bili_gift_map.json loaded")
    except Exception as e:
        add_log(f"Failed to run bili_gift_map.py: {e}")

@room.on('PREPARING')
async def on_preparing(event):
    global LIVE_STATUS
    LIVE_STATUS = 0
    add_log("[LOG] PREPARING")
    prepare_time = datetime.now().strftime("%H:%M")
    prepare_timestamp = int(time.time())
    live_length = int(prepare_timestamp - MEMORY["meta"]["live_time"]) // 60
    live_hours = int(live_length) // 60
    live_mins = int(live_length) % 60
    live_start_time = time.strftime("%H:%M", time.localtime(MEMORY["meta"]["live_time"]))
    if qq and MEMORY["meta"]["live_time"] != 0:
        if live_hours == 0 and live_mins == 0:
            time_length = "不足1分钟"
        elif live_hours == 0:
            time_length = f"{live_mins}分钟"
        elif live_mins == 0:
            time_length = f"{live_hours}小时"
        else:
            time_length = f"{live_hours}小时{live_mins}分钟"

        # for gid in TARGET_GROUP_LIST:
        #     await qq.text(f"【推送姬】下播提醒\n云崎早_haya 下播啦！\n直播时间：{live_start_time}-{prepare_time}（{time_length}）\n感谢大家观看~", at_all=True, group_id=gid)
        #     await asyncio.sleep(3)
        tasks = [qq.text(f"【推送姬】下播提醒\n云崎早_haya 下播啦！\n直播时间：{live_start_time}-{prepare_time}（{time_length}）\n感谢大家观看~", at_all=True, group_id=gid) for gid in TARGET_GROUP_LIST]
        await asyncio.gather(*tasks, return_exceptions=True)
        # await qq.text(f"【推送姬】下播提醒\n云崎早_haya 下播啦！\n直播时间：{live_start_time}-{prepare_time}（{time_length}）\n感谢大家观看~", at_all=True, group_id=TARGET_GROUP)
        # await asyncio.sleep(5)
        # await qq.text(f"【推送姬】下播提醒\n云崎早_haya 下播啦！\n直播时间：{live_start_time}-{prepare_time}（{time_length}）\n感谢大家观看~", at_all=True, group_id=TARGET_GROUP_FANS)
        add_log("[推送姬] 下播提醒")

    await asyncio.sleep(5)
    title = MEMORY["meta"]["title"] or "天  才  主  播  ！"
    await send_email(live_start_time, prepare_time, time_length, title)


# FastAPI & SSL Patch
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # 前端仅通过 GET 读取礼物/盲盒等统计数据，不涉及 Cookie 凭据，故关闭 credentials
    # （原 allow_credentials=True 与 allow_origins=["*"] 是浏览器拒绝的无效组合）
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/gift")
def get_gift():
    return MEMORY["gift"]

@app.get("/box")
def get_box():
    return MEMORY["box"]

@app.get("/all")
def get_all():
    return MEMORY["all"]

@app.get("/log")
def get_log():
    return log_buffer.buffer

@app.get("/data")
def get_data():
    leaderboard = sorted(MEMORY["gift"].values(), key=lambda x: x.get('profit', 0), reverse=True)
    return {
        "status": STATUS,
        "list": leaderboard
    }

@app.get("/superchat")
def get_superchat():
    data = []
    if os.path.exists("files/superchat.jsonl"):
        with open("files/superchat.jsonl", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, list):
                        data.extend(obj)
                    else:
                        data.append(obj)
                except json.JSONDecodeError:
                    continue
    data.sort(key=lambda x: x.get("time", 0), reverse=True)
    return {
        "status": STATUS,
        "list": data
    }

#### 热更新辅助函数
def _update_meta_gear(profit):
    next_t = MEMORY["meta"]["next_threshold"]
    gear = MEMORY["meta"]["current_gear"]
    triggered = False

    while profit >= next_t:
        gear += 1
        random_step = random.randint(GEAR_STEP_MIN, GEAR_STEP_MAX)
        next_t += random_step
        triggered = True
        # add_log(f"[礼物姬] 下一电池阈值: {next_t}")
    
    if triggered:
        MEMORY["meta"]["current_gear"] = gear
        MEMORY["meta"]["next_threshold"] = next_t
        add_log(f"[HOT GEAR UPDATE] gear={gear}, next_threshold={next_t}")

def _rollback_meta_gear(profit):
    next_t = MEMORY["meta"]["next_threshold"]
    gear = MEMORY["meta"]["current_gear"]
    triggered = False

    while gear > 0:
        min_threshold_for_gear = gear * GEAR_STEP_MIN

        if profit < min_threshold_for_gear:
            gear -= 1
            step = random.randint(GEAR_STEP_MIN, GEAR_STEP_MAX)
            next_t -= step

            if gear == 0:
                min_next = random.randint(GEAR_STEP_MIN, GEAR_STEP_MAX)
            else:
                min_next = gear * GEAR_STEP_MIN
            if next_t < min_next:
                next_t = min_next
            triggered = True
        else:
            break

    if triggered:
        MEMORY["meta"]["current_gear"] = gear
        MEMORY["meta"]["next_threshold"] = next_t
        add_log(f"[HOT GEAR ROLLBACK] gear={gear}, next_threshold={next_t}")


#### 热更新，gift_price, blind_cost, blind_profit传单个成本与收益电池数
class HotGiftInput(BaseModel):
    uid: int
    uname: str
    gift_name: str
    gift_price: int
    timestamp: int
    count: int
    is_blind_box: bool = False
    blind_cost: Optional[float] = None
    original_box_name: Optional[str] = None
@app.post("/api/hot_gift", dependencies=[Depends(_check_hot_auth)])
def api_hot_gift(data: HotGiftInput):
    '''
    curl -X POST "http://127.0.0.1:8000/api/hot_gift" \
      -H "Content-Type: application/json" \
      -d '{
        "uid": ,
        "uname": ,
        "gift_name": ,
        "gift_price": ,
        "timestamp": ,
        "count": ,
        "is_blind_box": ,
        "blind_cost": ,
        "original_box_name": 
      }'
    '''
    uid_str = str(data.uid)
    total_price = data.gift_price * data.count

    if uid_str in MEMORY["gift"]:
        if data.gift_name in MEMORY["gift"][uid_str]["gift_list"]:
            MEMORY["gift"][uid_str]["gift_list"][data.gift_name] += data.count
        else:
            MEMORY["gift"][uid_str]["gift_list"][data.gift_name] = data.count
        MEMORY["gift"][uid_str]["profit"] += total_price
    else:
        MEMORY["gift"][uid_str] = {
            "uid": data.uid,
            "uname": data.uname,
            "gift_list": {data.gift_name: data.count},
            "profit": total_price
        }

    for _ in range(data.count):
        MEMORY["all"].append({
            "uid": data.uid,
            "uname": data.uname,
            "time": data.timestamp,
            "gift_name": data.gift_name,
            "gift_price": data.gift_price
        })

    if data.is_blind_box:
        profit = data.gift_price
        cost = data.blind_cost if data.blind_cost is not None else 0.0
        count = data.count
        box_name = data.original_box_name or data.gift_name
        update_box_summary(data.uid, data.uname, count, cost*count, profit*count, box_name)
        save_json("files/box.json", MEMORY["box"])

    MEMORY["meta"]["total_battery"] += total_price
    _update_meta_gear(MEMORY["meta"]["total_battery"])
    save_json("files/meta.json", MEMORY["meta"])

    log_suffix = f" [HOT UPDATE (BOX)] {box_name}x{count}]" if data.is_blind_box else ""
    add_log(f"[HOT UPDATE] {data.uname}: {data.gift_name} x {data.count}" + log_suffix)
    return {"status": "success", "message": "Hot update inject completed."}

# 热删除
class DeleteSpecificGiftInput(BaseModel):
    uid: int
    uname: str
    gift_name: str
    gift_price: Union[int, float]
    timestamp: int
    count: int
    is_blind_box: bool = False
    blind_cost: Optional[float] = None
    original_box_name: Optional[str] = None
    
@app.post("/api/delete_specific_gift", dependencies=[Depends(_check_hot_auth)])
def api_delete_specific_gift(data: DeleteSpecificGiftInput):
    r"""
    curl -X POST "http://127.0.0.1:8000/api/delete_specific_gift" \
      -H "Content-Type: application/json" \
      -d '{
        "uid": ,
        "uname": ,
        "gift_name": ,
        "gift_price": ,
        "timestamp": ,
        "count": ,
        "is_blind_box": ,
        "blind_cost": ,
        "original_box_name": 
      }'
    """
    uid_str = str(data.uid)
    
    target_indices = []
    for i, record in enumerate(MEMORY["all"]):
        rp = record.get("gift_price", 0)
        price_match = (rp == data.gift_price) or (isinstance(rp, (int, float)) and abs(rp - data.gift_price) < 0.01)
        
        if (record.get("uid") == data.uid and
            record.get("gift_name") == data.gift_name and
            price_match and
            record.get("time") == data.timestamp):
            target_indices.append(i)
            if len(target_indices) >= data.count:
                break
    
    if len(target_indices) < data.count:
        return {"status": "not_found", "message": f"仅找到{len(target_indices)}条记录，需要删除{data.count}条"}
    
    deleted_records = []
    for idx in reversed(target_indices):
        deleted = MEMORY["all"].pop(idx)
        deleted_records.append(deleted)
    
    actual_price = deleted_records[0].get("gift_price", 0)
    total_deduct = actual_price * data.count
    
    if uid_str in MEMORY["gift"]:
        user_data = MEMORY["gift"][uid_str]
        current_profit = user_data.get("profit", 0)
        
        new_profit = current_profit - total_deduct
        user_data["profit"] = new_profit if new_profit > 0 else 0
        
        gift_list = user_data.get("gift_list", {})
        if data.gift_name in gift_list:
            gift_list[data.gift_name] -= data.count
            if gift_list[data.gift_name] <= 0:
                del gift_list[data.gift_name]
        
        if not gift_list:
            del MEMORY["gift"][uid_str]
        else:
            user_data["gift_list"] = gift_list

        if data.is_blind_box:
            profit = actual_price
            cost = data.blind_cost if data.blind_cost is not None else 0.0
            box_name = data.original_box_name or data.gift_name
            box_name = BOX_MEMORY_MAP.get(box_name, box_name)

            if uid_str in MEMORY["box"]:
                box_user = MEMORY["box"][uid_str]
                box_user["count"] = max(0, box_user.get("count", 0) - data.count)
                box_user["cost"] = max(0, box_user.get("cost", 0) - cost * data.count)
                box_user["profit"] = max(0, box_user.get("profit", 0) - profit * data.count)

                info = box_user.get("info", {})
                if box_name in info:
                    info[box_name] -= data.count
                    if info[box_name] <= 0:
                        del info[box_name]
                box_user["info"] = info

                cost_detail = box_user.setdefault("cost_detail", {})
                if box_name in cost_detail:
                    cost_detail[box_name] -= cost * data.count
                    if cost_detail[box_name] <= 0:
                        del cost_detail[box_name]
                box_user["cost_detail"] = cost_detail

                profit_detail = box_user.setdefault("profit_detail", {})
                if box_name in profit_detail:
                    profit_detail[box_name] -= profit * data.count
                    if profit_detail[box_name] <= 0:
                        del profit_detail[box_name]
                box_user["profit_detail"] = profit_detail

                if box_user["count"] <= 0 or not box_user.get("info", {}):
                    del MEMORY["box"][uid_str]
            
            save_json("files/box.json", MEMORY["box"])

    MEMORY["meta"]["total_battery"] -= total_deduct
    if MEMORY["meta"]["total_battery"] < 0:
        MEMORY["meta"]["total_battery"] = 0
    _rollback_meta_gear(MEMORY["meta"]["total_battery"])
    
    save_json("files/all.json", MEMORY["all"])
    save_json("files/gift.json", MEMORY["gift"])
    save_json("files/meta.json", MEMORY["meta"])
    
    add_log(f"[HOT DELETE] {data.uname}({data.uid}) {data.gift_name}x{data.count}: "
            f"battery:{total_deduct}, time:{data.timestamp}")
    
    return {
        "status": "success",
        "deleted_count": data.count,
        "deleted_records": deleted_records,
        "gift_summary_remaining": MEMORY["gift"].get(uid_str, "该用户已无记录")
    }

# 热发送弹幕
class SendDanmu(BaseModel):
    msg: str
    at_uid: Optional[int] = None
@app.post("/api/send_danmu", dependencies=[Depends(_check_hot_auth)])
async def api_send_danmu(data: SendDanmu):
    r'''
    curl -X POST "http://127.0.0.1:8000/api/send_danmu" \
      -H "Content-Type: application/json" \
      -d '{"msg":"", "at_uid": 3493074573461871}'
    '''
    msg = data.msg.strip()
    uid = data.at_uid

    if uid == 0:
        await reply_queue.put((None, msg))
    elif uid == -1:
        await reply_queue.put((YQZ_ID, msg))
    else:
        await reply_queue.put((uid, msg))

    add_log(f"[SEND DANMU] {msg}" + (f" @{uid}" if uid else ""))
    return {"status": "success", "sent": msg, "at_uid": uid}

# 更改STATUS状态
class StatusInput(BaseModel):
    status: int
@app.post("/api/set_status", dependencies=[Depends(_check_hot_auth)])
def api_set_status(data: StatusInput):
    '''
    curl -X POST "http://127.0.0.1:8000/api/set_status" \
        -H "Content-Type: application/json" \
        -d '{"status": 0}'
    '''
    global STATUS
    STATUS = data.status
    add_log(f"[HOT UPDATE] STATUS set to {STATUS}")
    return {"status": "success", "current_status": STATUS}

# 部分运行环境下 bilibili_api 请求需要跳过证书校验；如网络环境可信，可改为 False 以恢复校验
DISABLE_SSL_VERIFY = True

def patch_ssl():
    if not DISABLE_SSL_VERIFY:
        return
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    orig_init = aiohttp.TCPConnector.__init__
    def new_init(self, *args, **kwargs):
        kwargs['ssl'] = ssl_context
        orig_init(self, *args, **kwargs)
    aiohttp.TCPConnector.__init__ = new_init

## 紧急保存逻辑
def _do_emergency_save(source="unknown"):
    add_log(f"[EMERGENCY SAVE] Source: {source}")
    try:
        if MEMORY.get("gift"):
            save_json("files/gift.json", MEMORY["gift"])
        if MEMORY.get("all"):
            save_json("files/all.json", MEMORY["all"])
        if MEMORY.get("meta"):
            save_json("files/meta.json", MEMORY["meta"])
        if MEMORY.get("audience"):
            MEMORY["audience"]["interact_cache"] = list(interact_cache)
            save_json("files/audience.json", MEMORY["audience"])
        if MEMORY.get("danmu"):
            append_to_jsonl("files/danmu.jsonl", MEMORY["danmu"])
        add_log("[EMERGENCY SAVE] Completed successfully.")
    except Exception as e:
        add_log(f"[EMERGENCY SAVE FAILED] {e}")
        traceback.print_exc()

atexit.register(lambda: _do_emergency_save("atexit"))

# __main__
async def main():
    global qq
    
    add_log("=== Start ===")
    await init_get_room_status()
    load_json_files()

    for filename, config in HOT_RELOAD_CONFIG.items():
        filepath = os.path.join(os.path.dirname(__file__), filename)
        if os.path.exists(filepath):
            _hot_reload_state[filename] = {
                'path': filepath,
                'mtime': os.path.getmtime(filepath),
                'module': config['module'],
                'functions': config['functions']
            }
        else:
            add_log(f"[HOT RELOAD] Warning: {filename} not found, skipping")
            
    patch_ssl()

    qq = QQBot(NAPCAT_API, TARGET_GROUP, TOKEN)
    
    tasks = [
        asyncio.create_task(periodic_tasks(), name="periodic"),
        asyncio.create_task(reply_worker(), name="reply"),
        asyncio.create_task(room.connect(), name="room"),
        asyncio.create_task(dynamic_monitor(qq), name="dynamic"),
    ]
    
    import uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=8000, loop="asyncio")
    server = uvicorn.Server(config)
    tasks.append(asyncio.create_task(server.serve(), name="uvicorn"))

    def _on_signal(sig):
        sig_name = signal.Signals(sig).name
        add_log(f"[SIGNAL] Received {sig_name}, cancelling tasks...")
        for t in tasks:
            if not t.done():
                t.cancel()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: _on_signal(s))

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        add_log(f"[MAIN ERROR] {e}")
        traceback.print_exc()
        raise
    finally:
        _do_emergency_save("finally")
        add_log("=== Shutdown Complete ===")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _do_emergency_save("KeyboardInterrupt")
    except Exception as e:
        add_log(f"[UNCAUGHT] {e}")
        traceback.print_exc()
        _do_emergency_save("uncaught_exception")
        raise