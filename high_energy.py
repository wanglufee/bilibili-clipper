"""
高能时刻检测器
综合弹幕密度、礼物、SC、进场速度等多个指标计算高能分数

依赖: live_danmaku.py (回调接收)
"""

import time
import logging
import threading
from collections import defaultdict
from datetime import datetime

logger = logging.getLogger("high_energy")

class HighEnergyDetector:
    """
    多维度高能时刻检测器
    
    检测维度:
    - 弹幕密度 (条/秒)
    - 礼物事件 (权重 * 礼物金额)
    - SC (Super Chat) 留言 (权重 * 金额)
    - 进场速度 (人/秒) — 短时间内大量涌入通常是高能信号
    - 大额综合事件 (礼物+SC+弹幕同时爆发)
    """

    # 各维度权重配置
    WEIGHTS = {
        "danmaku": 1.0,          # 弹幕密度基础权重
        "gift_small": 2.0,       # 小礼物（辣条/小心心等 ≤10元）
        "gift_medium": 5.0,      # 中礼物（10-100元）
        "gift_large": 10.0,      # 大礼物（>100元）
        "sc_small": 8.0,         # SC 小额 (≤50元)
        "sc_medium": 15.0,       # SC 中额 (50-500元)
        "sc_large": 30.0,        # SC 大额 (>500元)
        "enter_burst": 3.0,      # 进场爆发
    }

    # 高能判定阈值（总分）
    DEFAULT_THRESHOLD = 15.0

    def __init__(self, room_id, density_threshold=10, score_threshold=None):
        """
        :param room_id: 直播间ID
        :param density_threshold: 弹幕密度阈值（备用，保留兼容）
        :param score_threshold: 综合得分阈值（超过则标记高能）
        """
        self.room_id = room_id
        self.density_threshold = density_threshold
        self.score_threshold = score_threshold or self.DEFAULT_THRESHOLD

        # 时间窗口（秒）
        self.window_size = 5          # 检测窗口宽度
        self.density_window = 5       # 弹幕密度统计窗口

        # 数据存储
        self._lock = threading.Lock()
        self._start_time = None
        self._danmaku_count = 0       # 当前窗口弹幕数
        self._enter_count = 0         # 当前窗口进场数
        self._gift_events = []        # 当前窗口礼物事件 [(score, timestamp)]
        self._sc_events = []          # 当前窗口SC事件 [(score, timestamp)]
        self._window_start = 0        # 当前窗口开始时间

        # 历史数据
        self.energy_records = []      # [(timestamp, score, reasons)]
        self.high_energy_points = []  # [(timestamp, score, description)]

        # 统计
        self.total_danmaku = 0
        self.total_gifts = 0
        self.total_sc = 0
        self.total_enters = 0

        # 实时滑动窗口
        self._sliding_lock = threading.Lock()
        self._sliding_events = []     # [(timestamp, type, value)]

        # 启动滑动窗口清理线程
        self._running = False
        self._cleaner_thread = None

    def start(self):
        """启动检测器"""
        self._start_time = time.time()
        self._window_start = int(self._start_time)
        self._running = True
        self._cleaner_thread = threading.Thread(
            target=self._cleaner_loop,
            daemon=True,
            name=f"energy-cleaner-{self.room_id}"
        )
        self._cleaner_thread.start()
        logger.info(f"高能检测器已启动: {self.room_id}")

    def stop(self):
        """停止检测器"""
        self._running = False
        logger.info(f"高能检测器已停止: {self.room_id}")

    def _cleaner_loop(self):
        """定期清理过期事件（保留最近60秒）"""
        while self._running:
            time.sleep(1)
            now = time.time()
            cutoff = now - 60
            with self._sliding_lock:
                self._sliding_events = [
                    e for e in self._sliding_events if e[0] > cutoff
                ]

    # ========== 事件接收 ==========

    def on_danmaku(self, room_id, username, content, timestamp_ms):
        """收到弹幕"""
        with self._lock:
            self._danmaku_count += 1
            self.total_danmaku += 1
        with self._sliding_lock:
            self._sliding_events.append((time.time(), "danmaku", 1))

    def on_gift(self, room_id, username, gift_name, num):
        """收到礼物"""
        score = self._calc_gift_score(gift_name, num)
        now = time.time()
        with self._lock:
            self.total_gifts += 1
            self._gift_events.append((score, now))
        with self._sliding_lock:
            self._sliding_events.append((now, "gift", score))
        logger.info(f"🎁 礼物 [{self.room_id}]: {username} x{num} ({gift_name}) → 权重 {score:.0f}")

    def on_sc(self, room_id, username, text, price):
        """收到SC (Super Chat) — 需要通过扩展命令识别"""
        score = self._calc_sc_score(price)
        now = time.time()
        with self._lock:
            self.total_sc += 1
            self._sc_events.append((score, now))
        with self._sliding_lock:
            self._sliding_events.append((now, "sc", score))
        logger.info(f"💰 SC [{self.room_id}]: {username} ¥{price} → 权重 {score:.0f}")

    def on_enter(self, room_id, username):
        """观众进场"""
        with self._lock:
            self._enter_count += 1
            self.total_enters += 1
        with self._sliding_lock:
            self._sliding_events.append((time.time(), "enter", 1))

    # ========== 权重计算 ==========

    def _calc_gift_score(self, gift_name, num):
        """估算礼物价值权重"""
        gift_name_lower = gift_name.lower() if gift_name else ""
        # 小礼物
        small_gifts = ["辣条", "小心心", "免费礼物", "荧光棒", "人气票", "lollipop"]
        # 中礼物
        medium_gifts = ["小电视", "摩天楼", "飞机", "舰长", "提督", "办卡", "ca"]
        # 大礼物
        large_gifts = ["总督", "宇宙飞船", "城堡", "飞船", "梦幻城堡", "嘉年华", "space"]

        is_small = any(k in gift_name_lower or k in gift_name for k in small_gifts)
        is_medium = any(k in gift_name_lower or k in gift_name for k in medium_gifts)
        is_large = any(k in gift_name_lower or k in gift_name for k in large_gifts)

        if is_large:
            return self.WEIGHTS["gift_large"] * num
        elif is_medium:
            return self.WEIGHTS["gift_medium"] * num
        else:
            return self.WEIGHTS["gift_small"] * num

    def _calc_sc_score(self, price_yuan):
        """SC金额权重"""
        if price_yuan <= 50:
            return self.WEIGHTS["sc_small"]
        elif price_yuan <= 500:
            return self.WEIGHTS["sc_medium"]
        else:
            return self.WEIGHTS["sc_large"]

    # ========== 当前窗口评分 ==========

    def get_current_score(self):
        """
        计算当前窗口的综合高能分数
        
        返回: (total_score, breakdown)
            total_score: float 总分
            breakdown: dict 各维度分数明细
        """
        now = time.time()
        
        with self._lock:
            window_danmaku = self._danmaku_count
            window_enter = self._enter_count
            # 清理过期礼物和SC（超过10秒）
            cutoff = now - self.window_size
            active_gifts = [(s, t) for s, t in self._gift_events if t > cutoff]
            active_sc = [(s, t) for s, t in self._sc_events if t > cutoff]
            self._gift_events = active_gifts
            self._sc_events = active_sc
            # 重置计数（在下次调用时重新累积）
            self._danmaku_count = 0
            self._enter_count = 0

        # 弹幕密度分数
        danmaku_density = window_danmaku / self.window_size if self.window_size > 0 else 0
        # 密度阈值基准: threshold条/秒 = 1分，低于此不计分
        danmaku_score = max(0, (danmaku_density - self.density_threshold / self.window_size)) * self.WEIGHTS["danmaku"]

        # 礼物分数
        gift_score = sum(s for s, _ in active_gifts)

        # SC分数
        sc_score = sum(s for s, _ in active_sc)

        # 进场爆发分数（超过5人/秒算爆发）
        enter_rate = window_enter / self.window_size if self.window_size > 0 else 0
        enter_score = max(0, (enter_rate - 1)) * self.WEIGHTS["enter_burst"]

        total = danmaku_score + gift_score + sc_score + enter_score

        breakdown = {
            "danmaku": round(danmaku_score, 1),
            "gift": round(gift_score, 1),
            "sc": round(sc_score, 1),
            "enter": round(enter_score, 1),
            "total": round(total, 1),
            "is_high_energy": total >= self.score_threshold,
            "danmaku_count": window_danmaku,
            "gift_count": len(active_gifts),
            "sc_count": len(active_sc),
            "enter_count": window_enter,
        }

        return breakdown

    def check_high_energy(self):
        """
        检查当前是否高能时刻
        返回: (is_high, score_dict)
        """
        score = self.get_current_score()
        elapsed = int(time.time() - (self._start_time or time.time()))

        if score["is_high_energy"]:
            desc = self._build_description(score)
            self.high_energy_points.append({
                "elapsed": elapsed,
                "timestamp": time.time(),
                "score": score["total"],
                "description": desc,
                "breakdown": score,
            })
            logger.info(f"⚡ 高能时刻! [{self.room_id}] +{elapsed}s 总分={score['total']:.1f} | {desc}")

        return score["is_high_energy"], score

    def _build_description(self, score):
        """生成高能描述"""
        parts = []
        if score["danmaku"] >= 5:
            parts.append(f"弹幕爆发({score['danmaku_count']}条)")
        if score["gift"] >= 10:
            parts.append(f"礼物({score['gift_count']}件)")
        if score["sc"] >= 10:
            parts.append(f"SC({score['sc_count']}条)")
        if score["enter"] >= 5:
            parts.append(f"涌入({score['enter_count']}人)")
        return "+".join(parts) if parts else f"总分{score['total']}"

    def get_summary(self):
        """获取统计数据摘要"""
        return {
            "room_id": self.room_id,
            "total_danmaku": self.total_danmaku,
            "total_gifts": self.total_gifts,
            "total_sc": self.total_sc,
            "total_enters": self.total_enters,
            "high_energy_count": len(self.high_energy_points),
            "threshold": self.score_threshold,
        }

    def get_high_energy_segments(self):
        """
        获取所有高能时段（合并相邻点）
        返回: [(start_elapsed, end_elapsed, max_score, description)]
        """
        if not self.high_energy_points:
            return []

        sorted_points = sorted(self.high_energy_points, key=lambda x: x["elapsed"])
        
        segments = []
        current = {
            "start": sorted_points[0]["elapsed"],
            "end": sorted_points[0]["elapsed"],
            "max_score": sorted_points[0]["score"],
            "desc": sorted_points[0]["description"],
        }

        for p in sorted_points[1:]:
            gap = p["elapsed"] - current["end"]
            if gap <= 5:  # 5秒内合并
                current["end"] = p["elapsed"]
                if p["score"] > current["max_score"]:
                    current["max_score"] = p["score"]
                    current["desc"] = p["description"]
            else:
                segments.append((
                    current["start"],
                    current["end"],
                    current["max_score"],
                    current["desc"],
                ))
                current = {
                    "start": p["elapsed"],
                    "end": p["elapsed"],
                    "max_score": p["score"],
                    "desc": p["description"],
                }

        segments.append((
            current["start"],
            current["end"],
            current["max_score"],
            current["desc"],
        ))

        return segments


# ========== 兼容函数（对接旧代码） ==========

def create_detector(room_id, density_threshold=10):
    """创建高能检测器"""
    return HighEnergyDetector(room_id, density_threshold)


if __name__ == "__main__":
    # 简单测试
    logging.basicConfig(level=logging.INFO)
    
    d = HighEnergyDetector(21669525, density_threshold=5)
    d.start()
    
    # 模拟弹幕爆发
    for i in range(30):
        d.on_danmaku(21669525, "user", "666", int(time.time() * 1000))
    
    # 模拟礼物
    d.on_gift(21669525, "老板", "火箭", 1)
    
    # 模拟进场
    for i in range(10):
        d.on_enter(21669525, f"user{i}")
    
    time.sleep(1)
    score = d.check_high_energy()
    print(f"高能检测: {score}")
    print(f"摘要: {d.get_summary()}")
    
    d.stop()
