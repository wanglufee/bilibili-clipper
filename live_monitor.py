#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
直播监控模块 - 定时轮询直播间状态
"""

import time
import threading
import requests
import logging
from datetime import datetime

logger = logging.getLogger("LiveMonitor")


class LiveMonitor:
    """直播间状态监控器"""

    def __init__(self, check_interval=30):
        self.check_interval = check_interval
        self._rooms = {}       # room_id -> room_info
        self._running = False
        self._thread = None
        self._api_base = "https://api.live.bilibili.com/room/v1/Room/get_info"

    def add_room(self, room_id, room_name="", on_live_start=None, on_live_end=None):
        """
        添加要监控的直播间
        :param room_id: 房间号
        :param room_name: 房间名称
        :param on_live_start: 开播回调(room_id, room_name, room_info)
        :param on_live_end:   下播回调(room_id, room_name)
        """
        self._rooms[room_id] = {
            "room_id": room_id,
            "room_name": room_name,
            "last_status": None,  # None: 未知, False: 未直播, True: 直播中
            "on_live_start": on_live_start,
            "on_live_end": on_live_end,
            "last_check": None,
        }
        logger.info(f"已添加直播间: {room_name}({room_id})")

    def remove_room(self, room_id):
        if room_id in self._rooms:
            del self._rooms[room_id]
            logger.info(f"已移除直播间: {room_id}")

    def get_room_status(self, room_id):
        """查询单个直播间状态"""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": f"https://live.bilibili.com/{room_id}",
        }
        try:
            resp = requests.get(self._api_base, params={"room_id": room_id}, headers=headers, timeout=10)
            if resp.status_code != 200:
                logger.error(f"查询直播间 {room_id} 返回HTTP {resp.status_code}")
                return None, None
            data = resp.json()
            if data["code"] == 0:
                return data["data"]["live_status"] == 1, data["data"]
            logger.error(f"直播间API返回异常: {data.get('message')}")
            return False, None
        except Exception as e:
            logger.error(f"查询直播间 {room_id} 状态失败: {e}")
            return None, None

    def _check_all(self):
        """检查所有直播间"""
        for room_id, info in self._rooms.items():
            try:
                is_live, room_data = self.get_room_status(room_id)
                if is_live is None:
                    continue  # 查询失败，跳过

                prev = info["last_status"]
                info["last_check"] = datetime.now()

                if prev is None:
                    # 第一次检查，记录状态并触发回调（如果是直播中）
                    info["last_status"] = is_live
                    if is_live:
                        logger.info(f"[{info['room_name']}] 正在直播中 (初始状态，触发开播)")
                        if info["on_live_start"]:
                            try:
                                info["on_live_start"](room_id, info["room_name"], room_data)
                            except Exception as e:
                                logger.error(f"开播回调出错: {e}")
                    else:
                        logger.info(f"[{info['room_name']}] 未直播 (初始状态)")

                elif is_live and not prev:
                    # 开播了！
                    info["last_status"] = True
                    logger.info(f"[{info['room_name']}] 🟢 开播了!")
                    if info["on_live_start"]:
                        try:
                            info["on_live_start"](room_id, info["room_name"], room_data)
                        except Exception as e:
                            logger.error(f"开播回调出错: {e}")

                elif not is_live and prev:
                    # 下播了！
                    info["last_status"] = False
                    logger.info(f"[{info['room_name']}] 🔴 下播了")
                    if info["on_live_end"]:
                        try:
                            info["on_live_end"](room_id, info["room_name"])
                        except Exception as e:
                            logger.error(f"下播回调出错: {e}")

                # 状态没变，不触发回调
                logger.debug(f"[{info['room_name']}] 状态: {'直播中' if is_live else '未直播'}")

            except Exception as e:
                logger.error(f"检查直播间 {room_id} 时出错: {e}")

    def _loop(self):
        """监控循环"""
        logger.info(f"监控线程启动，检查间隔: {self.check_interval}秒")
        while self._running:
            self._check_all()
            # 等待下一个检查周期
            for _ in range(self.check_interval):
                if not self._running:
                    break
                time.sleep(1)

    def start(self):
        """启动监控"""
        if self._running:
            logger.warning("监控已在运行中")
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="LiveMonitor")
        self._thread.start()
        logger.info("直播监控已启动")

    def stop(self):
        """停止监控"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        logger.info("直播监控已停止")

    @property
    def is_running(self):
        return self._running

    def get_status_summary(self):
        """获取所有直播间状态摘要"""
        summary = {}
        for room_id, info in self._rooms.items():
            summary[room_id] = {
                "room_name": info["room_name"],
                "is_live": info["last_status"],
                "last_check": info["last_check"].strftime("%H:%M:%S") if info["last_check"] else "N/A",
            }
        return summary


# 测试
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    monitor = LiveMonitor(check_interval=10)

    def on_start(rid, name, info):
        print(f"\n🎬 {name} 开播了! 标题: {info.get('title')}")

    def on_end(rid, name):
        print(f"\n🔴 {name} 下播了")

    monitor.add_room(21669525, "雨说体育徐静雨", on_start, on_end)
    monitor.start()

    try:
        while True:
            time.sleep(3)
            print(f"  当前状态: {monitor.get_status_summary()}")
    except KeyboardInterrupt:
        monitor.stop()
        print("退出")
