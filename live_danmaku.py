#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实时弹幕抓取模块 - 通过B站WebSocket协议获取直播弹幕
"""

import json
import time
import struct
import threading
import logging
from datetime import datetime
from collections import defaultdict

logger = logging.getLogger("LiveDanmaku")

# B站WebSocket常量
WS_URL = "wss://broadcastlv.chat.bilibili.com:2245/sub"
OP_HEARTBEAT = 2       # 心跳
OP_HEARTBEAT_REPLY = 3 # 心跳回复
OP_MESSAGE = 5         # 普通消息
OP_AUTH = 7            # 认证
OP_AUTH_REPLY = 8      # 认证回复

HEADER_LEN = 16

# 尝试导入brotli
brotli_available = False
try:
    import brotli
    brotli_available = True
except ImportError:
    try:
        import brotlicffi as brotli
        brotli_available = True
    except ImportError:
        logger.warning("brotli 未安装，压缩弹幕无法解析。尝试安装: pip install brotli")


def create_packet(data, opcode):
    """创建B站WebSocket数据包"""
    if isinstance(data, (dict, list)):
        data = json.dumps(data, ensure_ascii=False).encode("utf-8")
    elif isinstance(data, str):
        data = data.encode("utf-8")
    elif data is None:
        data = b""

    packet_len = HEADER_LEN + len(data)
    header = struct.pack(">I", packet_len)      # 总长度
    header += struct.pack(">H", HEADER_LEN)      # 头部长度
    header += struct.pack(">H", 0)               # 协议版本: 0=JSON
    header += struct.pack(">I", opcode)          # 操作码
    header += struct.pack(">I", 1)               # 序列号
    return header + data


def decode_packets(data):
    """解码B站WebSocket数据包"""
    packets = []
    offset = 0
    while offset + HEADER_LEN <= len(data):
        total_len = struct.unpack(">I", data[offset:offset+4])[0]
        header_len = struct.unpack(">H", data[offset+4:offset+6])[0]
        ver = struct.unpack(">H", data[offset+6:offset+8])[0]
        opcode = struct.unpack(">I", data[offset+8:offset+12])[0]
        seq = struct.unpack(">I", data[offset+12:offset+16])[0]

        if offset + total_len > len(data):
            break  # 数据不完整

        body = data[offset+header_len:offset+total_len] if header_len < total_len else b""
        packets.append({"ver": ver, "opcode": opcode, "seq": seq, "body": body})
        offset += total_len

    return packets


class LiveDanmaku:
    """B站直播弹幕抓取器"""

    def __init__(self):
        self._ws = None
        self._thread = None
        self._running = False
        self._room_id = None
        self._on_danmaku = None
        self._on_gift = None
        self._on_enter = None
        self._on_sc = None
        self._connected = False

        # 弹幕统计（用于高能时刻检测）
        self.danmaku_density = defaultdict(int)  # 时间窗口(秒) -> 弹幕数
        self.density_window = 5  # 统计窗口秒数
        self._start_time = None
        self.danmaku_records = []  # 原始记录 [(timestamp, username, content), ...]

    def _make_auth_packet(self, room_id):
        """创建认证包"""
        auth_data = {
            "roomid": room_id,
            "uid": 0,
            "protover": 0,
            "platform": "web",
            "type": 2,
        }
        return create_packet(auth_data, OP_AUTH)

    def _make_heartbeat_packet(self):
        """创建心跳包"""
        return create_packet("", OP_HEARTBEAT)

    def _handle_message(self, body, ver):
        """处理收到的消息"""
        # 如果是brotli压缩的，先解压
        if ver == 2 and brotli_available:
            try:
                decompressed = brotli.decompress(body)
            except Exception as e:
                logger.error(f"brotli解压失败: {e}")
                return
            # 解压后的数据可能是多个包粘在一起
            packets = decode_packets(decompressed)
            for pkt in packets:
                self._handle_message(pkt["body"], pkt["ver"])
            return

        # 如果是 JSON 格式
        if ver == 0 or ver == 1:
            try:
                text = body.decode("utf-8", errors="replace")
                msg = json.loads(text)
                self._parse_message(msg)
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

    def _parse_message(self, msg):
        """解析消息内容"""
        if not isinstance(msg, dict):
            return

        cmd = msg.get("cmd", "")
        now = datetime.now()
        elapsed = (now - self._start_time).total_seconds() if self._start_time else 0

        if cmd == "DANMU_MSG":
            info = msg.get("info", [])
            if len(info) >= 2:
                content = info[1]  # 弹幕内容
                user_info = info[0] if isinstance(info[0], list) else []
                username = user_info[15].get("uname", "未知用户") if len(user_info) > 15 and isinstance(user_info[15], dict) else str(user_info[2] if len(user_info) > 2 else "未知")

                # 记录弹幕
                record = (elapsed, username, content)
                self.danmaku_records.append(record)

                # 更新密度统计
                window = int(elapsed // self.density_window) * self.density_window
                self.danmaku_density[window] += 1

                # 回调
                if self._on_danmaku:
                    try:
                        self._on_danmaku(self._room_id, username, content, int(time.time() * 1000))
                    except Exception as e:
                        logger.error(f"弹幕回调出错: {e}")

        elif cmd == "SEND_GIFT":
            data = msg.get("data", {})
            username = data.get("uname", "未知")
            gift_name = data.get("giftName", "未知")
            num = data.get("num", 1)
            if self._on_gift:
                try:
                    self._on_gift(self._room_id, username, gift_name, num)
                except Exception as e:
                    logger.error(f"礼物回调出错: {e}")

        elif cmd == "SUPER_CHAT_MESSAGE":
            data = msg.get("data", {})
            if not isinstance(data, dict):
                data = msg
            username = data.get("uname", data.get("user_info", {}).get("uname", "未知"))
            text = data.get("message", data.get("text", ""))
            price = data.get("price", 0)
            if self._on_sc:
                try:
                    self._on_sc(self._room_id, username, text, price)
                except Exception as e:
                    logger.error(f"SC回调出错: {e}")

        elif cmd == "SUPER_CHAT_MESSAGE_JPN":
            data = msg.get("data", {})
            username = data.get("uname", "未知")
            text = data.get("message", "")
            price = data.get("price", 0)
            if self._on_sc:
                try:
                    self._on_sc(self._room_id, username, text, price)
                except Exception as e:
                    logger.error(f"SC(JP)回调出错: {e}")

        elif cmd in ("INTERACT_WORD", "ENTRY_EFFECT"):
            data = msg.get("data", {})
            username = data.get("uname", "未知")
            if self._on_enter:
                try:
                    self._on_enter(self._room_id, username)
                except Exception as e:
                    logger.error(f"进场回调出错: {e}")

    def connect(self, room_id, on_danmaku=None, on_gift=None, on_enter=None, on_sc=None):
        """
        连接B站弹幕WebSocket
        :param room_id: 直播间ID
        :param on_danmaku: 弹幕回调(room_id, username, content, timestamp_ms)
        :param on_gift: 礼物回调(room_id, username, gift_name, num)
        :param on_enter: 进场回调(room_id, username)
        :param on_sc: SC回调(room_id, username, text, price)
        """
        if self._running:
            logger.warning("弹幕抓取已在运行中")
            return

        self._room_id = room_id
        self._on_danmaku = on_danmaku
        self._on_gift = on_gift
        self._on_enter = on_enter
        self._on_sc = on_sc
        self._start_time = datetime.now()
        self.danmaku_density.clear()
        self.danmaku_records.clear()

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="DanmakuThread")
        self._thread.start()
        logger.info(f"弹幕抓取线程已启动: room_id={room_id}")

    def _run(self):
        """WebSocket运行循环（带自动重连）"""
        retry_count = 0
        max_retries = 5

        while self._running and retry_count < max_retries:
            try:
                import websocket as ws_client
                self._ws = ws_client.WebSocket()
                self._ws.settimeout(10)
                self._ws.connect(WS_URL, timeout=10)
                logger.info(f"WebSocket已连接: {WS_URL}")

                # 发送认证包
                auth_packet = self._make_auth_packet(self._room_id)
                self._ws.send(auth_packet, opcode=0x2)  # 二进制

                # 等待认证回复
                raw = self._ws.recv()
                if raw:
                    pkts = decode_packets(raw)
                    for pkt in pkts:
                        if pkt["opcode"] == OP_AUTH_REPLY:
                            body_str = pkt["body"].decode("utf-8", errors="replace")
                            auth_result = json.loads(body_str)
                            if auth_result.get("code") == 0:
                                logger.info(f"✅ 弹幕认证成功! room_id={self._room_id}")
                                self._connected = True
                                retry_count = 0
                            else:
                                logger.error(f"❌ 认证失败: {auth_result}")
                                self._running = False
                                return

                # 心跳和消息接收循环
                last_heartbeat = time.time()
                while self._running:
                    # 每30秒发心跳
                    now = time.time()
                    if now - last_heartbeat >= 30:
                        try:
                            self._ws.send(self._make_heartbeat_packet(), opcode=0x2)
                            last_heartbeat = now
                        except Exception:
                            logger.warning("发送心跳失败")
                            break

                    # 接收消息（带短超时）
                    try:
                        self._ws.settimeout(0.5)
                        raw = self._ws.recv()
                        if raw:
                            pkts = decode_packets(raw)
                            for pkt in pkts:
                                if pkt["opcode"] == OP_MESSAGE:
                                    self._handle_message(pkt["body"], pkt["ver"])
                                elif pkt["opcode"] == OP_HEARTBEAT_REPLY:
                                    # 心跳回复，可以获取热度等数据
                                    try:
                                        popularity = struct.unpack(">I", pkt["body"][:4])[0] if len(pkt["body"]) >= 4 else 0
                                        logger.debug(f"心跳回复 - 人气: {popularity}")
                                    except Exception:
                                        pass
                    except (websocket.WebSocketTimeoutException, Exception):
                        pass

                # 正常退出循环
                self._ws.close()
                self._connected = False
                break

            except Exception as e:
                retry_count += 1
                logger.warning(f"WebSocket连接异常 ({retry_count}/{max_retries}): {e}")
                self._connected = False
                if self._running:
                    wait = min(2 ** retry_count, 30)
                    logger.info(f"{wait}秒后重连...")
                    time.sleep(wait)

        if retry_count >= max_retries:
            logger.error("WebSocket重连次数达到上限，停止弹幕抓取")

        self._connected = False

    def disconnect(self):
        """断开弹幕连接"""
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._connected = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("弹幕抓取已停止")

    def is_connected(self):
        return self._connected

    def get_hot_moments(self, min_density=8):
        """
        获取高能时刻
        :param min_density: 最低弹幕密度阈值
        :return: [(window_start_time, density), ...] 按密度降序
        """
        moments = [(t, c) for t, c in self.danmaku_density.items() if c >= min_density]
        moments.sort(key=lambda x: x[1], reverse=True)
        return moments

    def get_statistics(self):
        """获取弹幕统计"""
        stats = {
            "total_danmaku": len(self.danmaku_records),
            "duration": (datetime.now() - self._start_time).total_seconds() if self._start_time else 0,
            "density_windows": len(self.danmaku_density),
            "is_connected": self._connected,
        }
        if self.danmaku_density:
            stats["max_density"] = max(self.danmaku_density.values())
            stats["avg_density"] = sum(self.danmaku_density.values()) / max(len(self.danmaku_density), 1)
        return stats


# 测试
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )

    live_dm = LiveDanmaku()

    def on_danmaku(room_id, username, content, ts):
        print(f"💬 {username}: {content}")

    def on_gift(room_id, username, gift_name, num):
        print(f"🎁 {username} 送了 {num}x {gift_name}")

    print("测试弹幕连接...")
    live_dm.connect(21669525, on_danmaku=on_danmaku, on_gift=on_gift)

    try:
        for i in range(120):
            time.sleep(1)
            if i % 10 == 0:
                stats = live_dm.get_statistics()
                print(f"📊 统计: {stats}")
    except KeyboardInterrupt:
        pass
    finally:
        live_dm.disconnect()
        print("断开连接")
