#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站直播全自动切片系统 v2.0 - 主控制器
整合：直播监控、直播录制、实时弹幕抓取、弹幕分析、自动切片
支持：直播模式 + 回放模式
"""

import os
import sys
import json
import time
import yaml
import logging
from datetime import datetime
from pathlib import Path

# 解决Windows控制台emoji打印问题
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# 添加项目根目录到路径
BASE_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(BASE_DIR))

# FFmpeg路径
FFMPEG_PATH = r"C:\Users\MECHREVO\.openclaw\workspace\ffmpeg-portable\ffmpeg-master-latest-win64-gpl\bin\ffmpeg.exe"

# 日志配置
log_dir = BASE_DIR / "logs"
log_dir.mkdir(exist_ok=True)
log_file = log_dir / f"clipper_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("Main")


class AutoLiveClipper:
    """自动直播切片主控制器"""

    def __init__(self, config_path=None):
        self.config = self._load_config(config_path)
        self._init_dirs()

        # 子模块（延迟初始化）
        self.monitor = None
        self.recorder = None
        self.danmaku = None
        self.clipper = None
        self.analyzer = None

        # 状态
        self._running = False
        self._live_status = {}        # room_id -> bool
        self._hot_moments = {}        # room_id -> [(start, end, conf, reason), ...]
        self._recording_files = {}    # room_id -> file_path (当前录制的文件)

        logger.info("=" * 60)
        logger.info("B站全自动直播切片系统 v2.0")
        logger.info(f"工作目录: {BASE_DIR}")
        logger.info(f"FFmpeg: {FFMPEG_PATH}")
        logger.info(f"配置文件: {self.config}")
        logger.info("=" * 60)

    def _load_config(self, config_path):
        """加载配置"""
        if config_path and os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)

        # 尝试默认配置路径
        default_paths = [
            config_path,
            str(BASE_DIR / "config" / "config.yaml"),
            str(BASE_DIR / "config.yaml"),
        ]
        for p in default_paths:
            if p and os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f)

        logger.warning("未找到配置文件，使用默认配置")
        return {}

    def _init_dirs(self):
        """初始化目录"""
        for d in ["download", "clips", "logs"]:
            (BASE_DIR / d).mkdir(exist_ok=True)
        for sub in ["pending", "processing", "completed", "failed"]:
            (BASE_DIR / "download" / sub).mkdir(exist_ok=True)
            (BASE_DIR / "clips" / sub).mkdir(exist_ok=True)

    def _init_modules(self):
        """初始化所有子模块"""
        if not self.monitor:
            from live_monitor import LiveMonitor
            interval = self.config.get("automation", {}).get("check_interval", 30)
            self.monitor = LiveMonitor(check_interval=interval)

        if not self.recorder:
            from live_recorder import LiveRecorder
            download_dir = str(BASE_DIR / self.config.get("paths", {}).get("download_dir", "download"))
            self.recorder = LiveRecorder(FFMPEG_PATH, output_dir=download_dir)

        if not self.danmaku:
            from live_danmaku import LiveDanmaku
            self.danmaku = LiveDanmaku()

        if not self.clipper:
            from auto_clipper import AutoClipper
            clips_dir = str(BASE_DIR / self.config.get("paths", {}).get("clips_dir", "clips"))
            self.clipper = AutoClipper(FFMPEG_PATH, output_dir=clips_dir)

        if not self.analyzer:
            try:
                from scripts.danmaku_analyzer_fixed import DanmakuAnalyzer
                self.analyzer = DanmakuAnalyzer()
            except ImportError:
                logger.warning("DanmakuAnalyzer 导入失败，部分功能将受限")
                self.analyzer = None

    def get_room_config(self, room_id):
        """获取指定直播间的配置"""
        default = {
            "room_id": room_id,
            "room_url": f"https://live.bilibili.com/{room_id}",
            "anchor_name": f"主播{room_id}",
        }
        live_room = self.config.get("live_room", {})
        if live_room.get("room_id") == room_id:
            return {**default, **live_room}
        return default

    # ========== 直播模式 ==========

    def _on_live_start(self, room_id, room_name, room_info):
        """开播回调"""
        logger.info(f"🎬 [{room_name}] 开播了！开始自动录制...")
        self._live_status[room_id] = True
        self._hot_moments[room_id] = []

        # 1. 开始录制
        success, result = self.recorder.start_recording(room_id, room_name)
        if success:
            self._recording_files[room_id] = result
            logger.info(f"✅ 录制开始: {result}")
        else:
            logger.error(f"❌ 录制启动失败: {result}")
            return

        # 2. 创建高能检测器
        from high_energy import HighEnergyDetector
        density_threshold = self.config.get("danmaku_analysis", {}).get("density_threshold", 10)
        detector = HighEnergyDetector(room_id, density_threshold)
        detector.start()
        if not hasattr(self, "_energy_detectors"):
            self._energy_detectors = {}
        self._energy_detectors[room_id] = detector

        # 3. 开始抓取实时弹幕，直接连接高能检测器的回调
        def on_danmaku(rid, username, content, ts):
            detector.on_danmaku(rid, username, content, ts)

        def on_gift(rid, username, gift_name, num):
            detector.on_gift(rid, username, gift_name, num)
            logger.info(f"🎁 [{room_name}] {username} 送了 {num}x {gift_name}")

        def on_enter(rid, username):
            detector.on_enter(rid, username)

        def on_sc(rid, username, text, price):
            detector.on_sc(rid, username, text, price)
            logger.info(f"💰 [{room_name}] {username} 发了SC: ¥{price}")

        self.danmaku.connect(
            room_id,
            on_danmaku=on_danmaku,
            on_gift=on_gift,
            on_enter=on_enter,
            on_sc=on_sc,
        )
        logger.info(f"💬 弹幕+高能检测已启动")

        # 4. 启动定期检测线程
        import threading
        t = threading.Thread(
            target=self._energy_check_loop,
            args=(room_id,),
            daemon=True,
            name=f"EnergyCheck-{room_id}",
        )
        t.start()

    def _energy_check_loop(self, room_id):
        """定期检查高能状态"""
        detectors = getattr(self, "_energy_detectors", {})
        min_interval = self.config.get("danmaku_analysis", {}).get("min_peak_interval", 30)
        last_peak_time = 0

        while self._running:
            time.sleep(3)
            detector = detectors.get(room_id)
            if not detector or room_id not in self._live_status or not self._live_status[room_id]:
                break

            try:
                is_high, score = detector.check_high_energy()
                now = time.time()
                if is_high and (now - last_peak_time >= min_interval):
                    # 获取合并后的时段
                    segments = detector.get_high_energy_segments()
                    if segments:
                        last_seg = segments[-1]
                        s, e, max_score, desc = last_seg
                        rec = (s, e, min(1.0, max_score / 30), desc)
                        if room_id not in self._hot_moments:
                            self._hot_moments[room_id] = []
                        self._hot_moments[room_id].append(rec)
                        last_peak_time = now
                        logger.info(f"⚡ 高能时刻! [{self.danmaku._room_name_map.get(room_id,room_id)}] "
                                     f"+{s}s 总分{score['total']:.1f} | {desc}")
            except Exception as e:
                logger.error(f"高能检查出错: {e}")

    def _on_live_end(self, room_id, room_name):
        """下播回调"""
        logger.info(f"🔴 [{room_name}] 下播了！开始处理录制内容...")
        self._live_status[room_id] = False

        # 1. 停止高能检测器
        detector = getattr(self, "_energy_detectors", {}).pop(room_id, None)
        if detector:
            detector.stop()
            logger.info(f"📊 高能检测统计: {detector.get_summary()}")
            # 获取最后未刷新的高能时段
            segments = detector.get_high_energy_segments()
            min_interval = self.config.get("danmaku_analysis", {}).get("min_peak_interval", 30)
            last_peak_time = 0
            for s, e, max_score, desc in segments:
                if s - last_peak_time >= min_interval:
                    rec = (s, e, min(1.0, max_score / 30), desc)
                    if room_id not in self._hot_moments:
                        self._hot_moments[room_id] = []
                    self._hot_moments[room_id].append(rec)
                    last_peak_time = s

        # 2. 停止弹幕抓取
        if self.danmaku and self.danmaku.is_connected():
            self.danmaku.disconnect()

        # 3. 停止录制
        recorded_file = None
        if self.recorder:
            success, result = self.recorder.stop_recording(room_id)
            if success:
                recorded_file = result
                logger.info(f"📁 录制文件: {result}")
            else:
                logger.warning(f"停止录制异常: {result}")

        # 清理录制状态
        if room_id in self._recording_files:
            del self._recording_files[room_id]

        # 4. 获取录制期间的弹幕数据
        hot_moments = self._hot_moments.get(room_id, [])
        logger.info(f"📊 直播期间检测到 {len(hot_moments)} 个高能时刻")
        for hm in hot_moments:
            s, e, c, desc = hm if len(hm) == 4 else (hm[0], hm[1], "-", "-")
            logger.info(f"   ⏱ {s:.0f}s ~ {e:.0f}s [{c:.2f}] {desc}")

        # 5. 如果有录制文件，基于高能时刻生成切片
        if recorded_file and os.path.exists(recorded_file):
            self._process_live_recording(room_id, room_name, recorded_file, hot_moments)
        else:
            logger.warning("没有有效的录制文件，无法生成切片")

        # 6. 检查是否有回放，尝试从回放生成补充切片
        self._check_replay_after_live(room_id, room_name)

    def _process_live_recording(self, room_id, room_name, recorded_file, hot_moments):
        """处理直播录制文件，生成切片"""
        if not hot_moments:
            logger.info("没有高能时刻，尝试从弹幕密度统计数据中提取...")
            # 从弹幕密度中取前5个峰值
            if self.danmaku:
                stats = self.danmaku.get_statistics()
                logger.info(f"弹幕统计: {stats}")

                # 如果有密度数据，取最高密度的窗口
                all_hot = self.danmaku.get_hot_moments(min_density=5)
                for window_start, density in all_hot[:5]:
                    hot_moments.append((window_start, window_start + 10, min(1.0, density / 50), f"弹幕: {density}/秒"))

        if not hot_moments:
            logger.warning("仍然没有高能时刻，基于时间均匀截取")
            # 基于时间均匀截取3个片段
            duration = self.clipper.get_video_duration(recorded_file)
            if duration and duration > 30:
                chunk = duration / 4
                for i in range(3):
                    mid = chunk * (i + 1)
                    hot_moments.append((mid - 10, mid + 10, 0.5, f"均匀采样 #{i+1}"))

        if hot_moments:
            # 去重、按时间排序
            deduped = self._deduplicate_segments(hot_moments)

            # 生成切片
            output_name = f"{room_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_live.mp4"
            output_path = str(BASE_DIR / "clips" / "completed" / output_name)

            logger.info(f"基于 {len(deduped)} 个高能时刻生成切片...")
            result = self.clipper.make_final_video(recorded_file, deduped, output_path, room_name)

            if result and os.path.exists(result):
                logger.info(f"✅ 直播切片生成成功: {result}")
            else:
                logger.error("❌ 直播切片生成失败")
        else:
            logger.warning("没有可用的高能时刻，跳过切片生成")

    def _deduplicate_segments(self, segments):
        """去重并排序片段"""
        # 转为统一格式
        parsed = []
        for seg in segments:
            if isinstance(seg, dict):
                s, e = seg.get("start", 0), seg.get("end", 0)
                c = seg.get("confidence", 1.0)
                r = seg.get("reason", "")
            else:
                s, e = seg[0], seg[1]
                c = seg[2] if len(seg) > 2 else 1.0
                r = seg[3] if len(seg) > 3 else ""
            parsed.append((s, e, c, r))

        # 按开始时间排序
        parsed.sort(key=lambda x: x[0])

        # 去重（重叠或过于接近的合并）
        deduped = []
        for seg in parsed:
            if not deduped:
                deduped.append(seg)
                continue

            last = deduped[-1]
            # 如果与上一个重叠或差距小于5秒，合并
            if seg[0] - last[1] < 5:
                merged = (last[0], max(last[1], seg[1]), max(last[2], seg[2]), f"{last[3]}; {seg[3]}")
                deduped[-1] = merged
            else:
                deduped.append(seg)

        # 限制片段数量
        max_segments = 10
        if len(deduped) > max_segments:
            deduped.sort(key=lambda x: x[2], reverse=True)
            deduped = deduped[:max_segments]
            deduped.sort(key=lambda x: x[0])

        return deduped

    def _check_replay_after_live(self, room_id, room_name):
        """直播结束后检查回放"""
        logger.info(f"等待回放就绪...")
        time.sleep(60)  # 等1分钟让B站生成回放

        try:
            import requests
            api = f"https://api.live.bilibili.com/x/web-interface/v2/getRecord"
            params = {"room_id": room_id}
            headers = {"User-Agent": "Mozilla/5.0", "Referer": f"https://live.bilibili.com/{room_id}"}
            resp = requests.get(api, params=params, headers=headers, timeout=15)
            data = resp.json()

            if data["code"] == 0 and data.get("data", {}).get("list"):
                records = data["data"]["list"]
                logger.info(f"找到 {len(records)} 个回放视频")
                # 处理最新的回放
                latest = records[0]
                bvid = latest.get("bvid") or latest.get("vid", "")
                if bvid:
                    logger.info(f"处理回放: BV{bvid}")
                    self._process_replay_video(room_id, room_name, bvid)
            else:
                logger.info("暂无可用回放")

        except Exception as e:
            logger.warning(f"获取回放列表失败: {e}")

    # ========== 回放模式 ==========

    def _process_replay_video(self, room_id, room_name, bvid):
        """处理回放视频"""
        logger.info(f"📺 处理回放: {bvid}")

        # 1. 获取弹幕
        from scripts.download_bilibili import get_bilibili_video_info
        from scripts.danmaku_analyzer_fixed import DanmakuAnalyzer

        try:
            # 获取弹幕XML
            import requests
            cid_api = f"https://api.bilibili.com/x/player/pagelist?bvid={bvid}"
            resp = requests.get(cid_api, timeout=10)
            cid_data = resp.json()

            if cid_data["code"] != 0 or not cid_data.get("data"):
                logger.error("获取视频CID失败")
                return

            cid = cid_data["data"][0]["cid"]
            dm_api = f"https://api.bilibili.com/x/v1/dm/list.so?oid={cid}"
            dm_resp = requests.get(dm_api, timeout=15)

            if dm_resp.status_code != 200:
                logger.error("获取弹幕失败")
                return

            xml_content = dm_resp.content.decode("utf-8", errors="replace")

        except Exception as e:
            logger.error(f"获取弹幕数据失败: {e}")
            return

        # 2. 分析弹幕
        analyzer = DanmakuAnalyzer()
        suggestions = analyzer.generate_clip_suggestions(xml_content, video_duration=3600)

        if not suggestions:
            logger.warning("回放弹幕分析未生成建议")
            return

        logger.info(f"弹幕分析完成，得到 {len(suggestions)} 个剪辑建议")

        # 3. 尝试下载回放视频
        video_url = f"https://www.bilibili.com/video/{bvid}"
        download_dir = str(BASE_DIR / "download")
        output_template = os.path.join(download_dir, f"replay_%(id)s.%(ext)s")

        video_path = None
        try:
            cmd = [
                "yt-dlp",
                "-f", "best[height<=1080]",
                "-o", output_template,
                "--no-playlist",
                "--quiet",
                video_url,
            ]
            logger.info(f"下载回放视频: {video_url}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

            # 寻找下载的文件
            for f in os.listdir(download_dir):
                if bvid in f and (f.endswith(".mp4") or f.endswith(".mkv")):
                    video_path = os.path.join(download_dir, f)
                    break

            if not video_path:
                logger.warning("找不到下载的回放视频文件")
                return

        except Exception as e:
            logger.error(f"下载回放视频失败: {e}")
            return

        # 4. 生成切片
        segments = []
        for rec in suggestions:
            segments.append((rec["start"], rec["end"], rec.get("confidence", 0.5), rec.get("reason", "")))

        if segments:
            output_name = f"{room_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_replay.mp4"
            output_path = str(BASE_DIR / "clips" / "completed" / output_name)

            deduped = self._deduplicate_segments(segments)
            result = self.clipper.make_final_video(video_path, deduped, output_path, room_name)

            if result:
                logger.info(f"✅ 回放切片完成: {result}")
            else:
                logger.error("❌ 回放切片失败")
        else:
            logger.warning("回放分析未得到可用片段")

    # ========== 纯回放处理（用户手动输入BV号） ==========

    def handle_replay_mode(self, bvid):
        """处理用户指定的回放视频"""
        logger.info(f"📺 用户指定回放: {bvid}")

        room_config = self.get_room_config(self.config.get("live_room", {}).get("room_id", 0))
        room_name = room_config.get("anchor_name", "未知主播")

        self._process_replay_video(room_config.get("room_id", 0), room_name, bvid)

    # ========== 启动/停止 ==========

    def start_live_monitor(self):
        """启动直播监控模式"""
        self._init_modules()
        self._running = True

        room_config = self.config.get("live_room", {})
        room_id = room_config.get("room_id")
        room_name = room_config.get("anchor_name", "未知主播")

        if not room_id:
            logger.error("配置中未设置 room_id")
            return

        logger.info(f"启动直播监控: {room_name}({room_id})")

        self.monitor.add_room(
            room_id,
            room_name,
            on_live_start=self._on_live_start,
            on_live_end=self._on_live_end,
        )
        self.monitor.start()

        logger.info(f"✅ 直播监控已启动，每{self.config.get('automation', {}).get('check_interval', 30)}秒检查一次")
        return True

    def stop(self):
        """停止所有服务"""
        logger.info("正在停止所有服务...")
        self._running = False

        if self.monitor:
            self.monitor.stop()

        if self.danmaku:
            self.danmaku.disconnect()

        if self.recorder:
            # 停止所有正在进行的录制
            for room_id in list(self.recorder._recording.keys()):
                self.recorder.stop_recording(room_id)

        logger.info("所有服务已停止")

    def get_status(self):
        """获取系统状态"""
        status = {
            "running": self._running,
            "live_status": {},
            "recordings": {},
        }

        if self.monitor:
            status["live_status"] = self.monitor.get_status_summary()

        if self.recorder:
            status["recordings"] = self.recorder.get_all_recording()

        if self.danmaku:
            status["danmaku"] = {
                "connected": self.danmaku.is_connected(),
            }
            stats = self.danmaku.get_statistics()
            if stats["total_danmaku"] > 0:
                status["danmaku"]["statistics"] = stats

        return status

    def check_ffmpeg(self):
        """检查FFmpeg是否可用"""
        import subprocess as _sp
        try:
            _r = _sp.run([FFMPEG_PATH, "-version"], capture_output=True, text=True, timeout=10)
            return _r.returncode == 0
        except Exception as _e:
            return False

    def quick_test(self):
        """快速测试"""
        self._init_modules()
        logger.info("系统测试...")

        tests = []
        # 1. FFmpeg
        tests.append(("FFmpeg", self.check_ffmpeg()))
        # 2. 目录
        tests.append(("目录结构", all((BASE_DIR / d).exists() or (BASE_DIR / d).mkdir(exist_ok=True) or True for d in ["download", "clips", "logs"])))
        # 3. 配置
        tests.append(("配置文件", bool(self.config)))

        all_ok = True
        for name, ok in tests:
            status = "✅" if ok else "❌"
            logger.info(f"  {status} {name}")
            if not ok:
                all_ok = False

        # FFmpeg检查重新做一次，因为之前可能没进入_init_modules
        ffmpeg_ok = self.check_ffmpeg()
        if not ffmpeg_ok:
            all_ok = False
            # 再试一次
            import time
            time.sleep(0.5)
            all_ok = self.check_ffmpeg()

        return all_ok


# ========== 命令行交互 ==========

def print_banner():
    print("""
    ╔═══════════════════════════════════════╗
    ║   🏴‍☠️ B站直播全自动切片系统 v2.0    ║
    ║       弹幕驱动 · 智能分析 · 自动剪辑    ║
    ╚═══════════════════════════════════════╝
    """)


def main():
    print_banner()

    system = AutoLiveClipper()

    if not system.quick_test():
        print("\n⚠️  部分测试失败，请检查配置")
        input("按回车继续...")

    while True:
        print("\n" + "=" * 50)
        print("主菜单:")
        print("  🟢 1. 启动直播监控模式 (自动录制+切片)")
        print("  📺 2. 处理回放视频 (输入BV号)")
        print("  📊 3. 查看系统状态")
        print("  ⚙️  4. 配置管理")
        print("  ❌ 5. 退出")
        print("=" * 50)

        choice = input("\n请选择 (1-5): ").strip()

        if choice == "1":
            print("\n启动直播监控模式...")
            if system.start_live_monitor():
                print("\n✅ 监控已启动! 按 Ctrl+C 停止监控并返回菜单")
                try:
                    while system._running:
                        time.sleep(5)
                        status = system.get_status()
                        lives = status.get("live_status", {})
                        for rid, info in lives.items():
                            is_live = "🟢 直播中" if info["is_live"] else "⚫ 未直播"
                            rec = f" | 录制中" if any(info["is_live"] for info in status.get("recordings", {}).values()) else ""
                            print(f"  {info['room_name']}: {is_live}{rec}")
                except KeyboardInterrupt:
                    print("\n\n停止监控...")
                    system.stop()
                    print("已停止")
            else:
                print("❌ 启动失败，请检查配置")

        elif choice == "2":
            print("\n处理回放视频")
            bvid = input("请输入BV号 (例如: BV1Mgd6BPEH8): ").strip()
            if bvid:
                # 自动补全BV前缀
                if not bvid.startswith("BV"):
                    bvid = "BV" + bvid
                print(f"开始处理: {bvid}")
                system.handle_replay_mode(bvid)
            else:
                print("❌ BV号无效")

        elif choice == "3":
            print("\n系统状态:")
            status = system.get_status()
            print(f"  运行中: {'是' if status['running'] else '否'}")

            for rid, info in status.get("live_status", {}).items():
                icon = "🟢" if info["is_live"] else "⚫"
                print(f"  {icon} {info['room_name']} ({rid})")
                print(f"     最后检查: {info['last_check']}")

            for rid, info in status.get("recordings", {}).items():
                print(f"  📹 录制中: {info['room_name']} ({info['duration']:.0f}秒)")
                print(f"     文件: {info['output_path']}")

            dm = status.get("danmaku", {})
            if dm.get("connected"):
                print(f"  💬 弹幕: 已连接")
                if "statistics" in dm:
                    s = dm["statistics"]
                    print(f"     总弹幕: {s['total_danmaku']}, 最高密度: {s.get('max_density', 0)}/秒")
            elif dm:
                print(f"  💬 弹幕: 未连接")

        elif choice == "4":
            print("\n配置管理")
            print(f"  主播: {system.config.get('live_room', {}).get('anchor_name', '未设置')}")
            print(f"  房间号: {system.config.get('live_room', {}).get('room_id', '未设置')}")
            print(f"  检查间隔: {system.config.get('automation', {}).get('check_interval', 30)}秒")
            print(f"  FFmpeg: {os.path.exists(FFMPEG_PATH)}")
            print("\n要修改配置，请编辑: config/config.yaml")

        elif choice == "5":
            print("\n退出系统...")
            system.stop()
            print("再见! 👋")
            break

        else:
            print("❌ 无效选择，请输入 1-5")

    return 0


if __name__ == "__main__":
    sys.exit(main())
