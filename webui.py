#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站直播全自动切片系统 - Web管理界面
支持目录结构: <storage_root>/<主播名>/<直播开始时间>/record/ + clips/
"""

import os
import sys
import json
import time
import yaml
import logging
import threading
import shutil
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file, send_from_directory

BASE_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(BASE_DIR))

# FFmpeg 自动查找：1) 项目内的便携版  2) 系统 PATH 中的 ffmpeg  3) 环境变量
_ffmpeg_candidates = [
    BASE_DIR / "ffmpeg-portable" / "ffmpeg-master-latest-win64-gpl" / "bin" / "ffmpeg.exe",
    BASE_DIR / "ffmpeg.exe",
    Path(__file__).resolve().parent.parent / "ffmpeg-portable" / "ffmpeg-master-latest-win64-gpl" / "bin" / "ffmpeg.exe",
]
FFMPEG_PATH = "ffmpeg"  # 默认：系统 PATH
for c in _ffmpeg_candidates:
    if c.exists():
        FFMPEG_PATH = str(c)
        break
# 环境变量覆盖
_env_ffmpeg = os.environ.get("FFMPEG_PATH", "")
if _env_ffmpeg and Path(_env_ffmpeg).exists():
    FFMPEG_PATH = _env_ffmpeg

log_dir = BASE_DIR / "logs"
log_dir.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(log_dir / "webui.log", encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger("WebUI")
logger.info(f"FFmpeg路径: {FFMPEG_PATH}")

app = Flask(__name__)


# ====== 目录结构管理 ======
def get_storage_root():
    """获取存储根目录"""
    cfg_root = state.config.get("storage_root", "")
    if cfg_root:
        p = Path(cfg_root)
        if not p.is_absolute():
            p = BASE_DIR / p
    else:
        p = BASE_DIR / "storage"
    p.mkdir(parents=True, exist_ok=True)
    return p


class LiveSession:
    """一场直播的会话，管理目录结构"""
    def __init__(self, room_id, room_name, title=""):
        self.room_id = room_id
        self.room_name = room_name
        self.title = title
        self.start_time = datetime.now()
        self.start_str = self.start_time.strftime("%Y_%m_%d_%H_%M_%S")

        safe_name = room_name.replace(" ", "_").replace("/", "_").replace("\\", "_")
        # 目录名: 开始时间 + 直播标题简写（取前20字符，去掉非法字符）
        dir_title = ""
        if title:
            safe_title = title.replace(" ", "_").replace("/", "_").replace("\\", "_").replace(":", "：").replace("?", "？")[:20]
            dir_title = f"_{safe_title}"
        root = get_storage_root()
        self.dir_root = root / safe_name / f"{self.start_str}{dir_title}"
        self.dir_record = self.dir_root / "record"
        self.dir_clips = self.dir_root / "clips"
        self.dir_record.mkdir(parents=True, exist_ok=True)
        self.dir_clips.mkdir(parents=True, exist_ok=True)

    def get_record_path(self):
        return str(self.dir_record / f"{self.room_name}_{self.start_str}.mp4")

    def get_clip_name(self, start_sec, end_sec):
        """切片文件名格式: xxhxxmxxs_xxhxxmxxs.mp4"""
        def fmt(s):
            h = int(s) // 3600
            m = (int(s) % 3600) // 60
            sec = int(s) % 60
            return f"{h:02d}h{m:02d}m{sec:02d}s"
        return f"{fmt(start_sec)}_{fmt(end_sec)}.mp4"

    def get_clip_path(self, start_sec, end_sec):
        return str(self.dir_clips / self.get_clip_name(start_sec, end_sec))


# ====== 必填参数校验 ======
REQUIRED_CONFIG = {
    "rooms": {
        "label": "直播间",
        "fields": [("room_id", "房间号", "int"), ("anchor_name", "主播名称", "str")],
        "desc": "至少添加一个直播间（room_id + anchor_name）",
    },
    "clipping": {
        "label": "剪辑参数",
        "fields": [("target_duration", "目标剪辑时长", "int")],
        "desc": "clipping.target_duration 目标剪辑时长(秒)",
    },
    "danmaku_analysis": {
        "label": "弹幕分析",
        "fields": [("density_threshold", "弹幕密度阈值", "int")],
        "desc": "danmaku_analysis.density_threshold 弹幕密度阈值(条/秒)",
    },
    "automation": {
        "label": "自动化配置",
        "fields": [("check_interval", "检查间隔", "int")],
        "desc": "automation.check_interval 检查直播状态间隔(秒)",
    },
}


def validate_config_strict(cfg):
    errors = []
    for section_key, section_info in REQUIRED_CONFIG.items():
        section = cfg.get(section_key, {})
        for field_key, field_label, field_type in section_info["fields"]:
            if section_key == "rooms":
                rooms = cfg.get("rooms", [])
                enabled = [r for r in rooms if r.get("enable", True)]
                if not enabled:
                    errors.append(f"[*{field_label}*] 未配置任何直播间，请至少添加一个")
                    break
                for ri, r in enumerate(enabled):
                    val = r.get(field_key)
                    if val is None or (isinstance(val, str) and val.strip() == ""):
                        errors.append(f"[*{field_label}*] 直播间#{ri+1} 缺少 '{field_key}'")
            else:
                val = section.get(field_key)
                if val is None:
                    errors.append(f"[*{field_label}*] {section_info['desc']}，当前未设置")
                elif field_type == "int" and not isinstance(val, int):
                    errors.append(f"[*{field_label}*] 必须为数字，当前值: {val}")
    return errors


# ====== 全局状态 ======
class AppState:
    def __init__(self):
        self.monitor = None
        self.recorder = None
        self.danmaku = {}
        self.clipper = None
        self.analyzer = None
        self._running = False
        self.config = self._load_config()
        self.logs = []
        self._max_logs = 500
        self.hot_moments = {}
        self.high_energy = {}    # room_id -> HighEnergyDetector
        self.sessions = {}       # room_id -> LiveSession
        self.current_stream = {}

    def _load_config(self):
        for p in [BASE_DIR / "config" / "config.yaml", BASE_DIR / "config.yaml"]:
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f)
        return {}

    def start_status_poller(self):
        """启动后台状态轮询（仅检查开播状态，不录制）"""
        from live_monitor import LiveMonitor
        self._status_monitor = LiveMonitor(check_interval=30)
        rooms = self.get_rooms()
        for room in rooms:
            rid = room["room_id"]
            name = room["anchor_name"]
            self._status_monitor.add_room(rid, name)
        self._status_monitor.start()

    def get_rooms(self):
        rooms = self.config.get("rooms", [])
        if not rooms and "live_room" in self.config:
            lr = self.config["live_room"]
            rooms = [{"room_id": lr.get("room_id", 0), "anchor_name": lr.get("anchor_name", "主播"), "room_url": lr.get("room_url", ""), "enable": True}]
        return [r for r in rooms if r.get("enable", True)]

    def add_log(self, msg, level="info"):
        self.logs.append({"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level})
        if len(self.logs) > self._max_logs:
            self.logs = self.logs[-self._max_logs:]

    def get_status(self):
        info = {"running": self._running, "rooms": self.get_rooms(), "monitor": {}, "recording": {}, "danmaku": {}, "storage_root": str(get_storage_root())}
        # 始终返回状态监测信息（即使未启动录制）
        if hasattr(self, '_status_monitor') and self._status_monitor:
            info["monitor"] = self._status_monitor.get_status_summary()
        # 录制中的额外信息
        if self.monitor:
            info["monitor"] = self.monitor.get_status_summary()
        if self.recorder:
            info["recording"] = self.recorder.get_all_recording()
        for rid, dm in self.danmaku.items():
            info["danmaku"][str(rid)] = {"connected": dm.is_connected()}
            try:
                stats = dm.get_statistics()
                if stats["total_danmaku"] > 0:
                    info["danmaku"][str(rid)]["stats"] = stats
            except:
                pass
        # 当前直播session信息
        info["sessions"] = {}
        for rid, sess in self.sessions.items():
            info["sessions"][str(rid)] = {"start_str": sess.start_str, "dir": str(sess.dir_root)}
        return info


state = AppState()


# ====== 后台线程 ======
def _monitor_loop():
    from live_monitor import LiveMonitor
    from live_recorder import LiveRecorder
    from auto_clipper import AutoClipper

    state.monitor = LiveMonitor(check_interval=30)
    # recorder和clipper直接用FFMPEG_PATH，目录由LiveSession管理
    from live_recorder import LiveRecorder
    state.recorder = LiveRecorder(FFMPEG_PATH, output_dir=str(BASE_DIR / "tmp_record"))
    (BASE_DIR / "tmp_record").mkdir(exist_ok=True)
    state.clipper = AutoClipper(FFMPEG_PATH, output_dir=str(BASE_DIR / "tmp_clips"))
    (BASE_DIR / "tmp_clips").mkdir(exist_ok=True)

    try:
        from scripts.danmaku_analyzer_fixed import DanmakuAnalyzer
        state.analyzer = DanmakuAnalyzer()
    except:
        state.analyzer = None

    rooms = state.get_rooms()
    if not rooms:
        state.add_log("未配置直播间", "error")
        state._running = False
        return

    for room in rooms:
        rid = room["room_id"]
        name = room["anchor_name"]
        state.hot_moments[rid] = []

        def make_callbacks(rid, name):
            def on_start(r, n, info):
                state.add_log(f"[{n}] 开播了!", "success")
                title = info.get("title", "") if info else ""
                _start_record(r, n, title)
            def on_end(r, n):
                state.add_log(f"[{n}] 下播了", "warn")
                _stop_record(r, n)
            return on_start, on_end

        on_start, on_end = make_callbacks(rid, name)
        state.monitor.add_room(rid, name, on_live_start=on_start, on_live_end=on_end)
        state.add_log(f"添加直播间: {name}({rid})", "info")

    state.monitor.start()
    state.add_log(f"监控已启动, {len(rooms)} 个直播间", "success")


def _start_record(room_id, room_name, title=""):
    # 创建直播会话目录结构
    sess = LiveSession(room_id, room_name, title)
    state.sessions[room_id] = sess
    record_path = sess.get_record_path()
    state.add_log(f"存储目录: {sess.dir_root}", "info")

    from live_recorder import LiveRecorder
    recorder = LiveRecorder(FFMPEG_PATH, output_dir=str(sess.dir_record))
    state.recorder = recorder

    # 检查是否已在录制或健康检查中（类级别共享字典）
    if room_id in recorder._recording:
        state.add_log(f"[{room_name}] 已在录制中，跳过", "warn")
        return
    if room_id in recorder._pending_health_checks:
        state.add_log(f"[{room_name}] 后台健康检查中，跳过", "warn")
        return

    success, result = recorder.start_recording(room_id, room_name)
    if success:
        state.add_log(f"开始录制: {os.path.basename(result)}", "info")
    else:
        state.add_log(f"录制失败: {result}", "error")
        return

    from live_danmaku import LiveDanmaku
    dm = LiveDanmaku()

    # 创建高能检测器
    from high_energy import HighEnergyDetector
    detector = HighEnergyDetector(room_id, density_threshold=state.config.get("danmaku_analysis", {}).get("density_threshold", 10))
    detector.start()
    state.high_energy[room_id] = detector

    def on_danmaku(rid, username, content, ts):
        detector.on_danmaku(rid, username, content, ts)
    def on_gift(rid, username, gift_name, num):
        detector.on_gift(rid, username, gift_name, num)
    def on_enter(rid, username):
        detector.on_enter(rid, username)
    def on_sc(rid, username, text, price):
        detector.on_sc(rid, username, text, price)

    dm.connect(room_id, on_danmaku=on_danmaku, on_gift=on_gift, on_enter=on_enter, on_sc=on_sc)
    state.danmaku[room_id] = dm

    t = threading.Thread(target=_hot_detector, args=(room_id,), daemon=True)
    t.start()


def _stop_record(room_id, room_name):
    # 停止高能检测器
    detector = state.high_energy.pop(room_id, None)
    if detector:
        try:
            detector.stop()
        except:
            pass

    if room_id in state.danmaku:
        try:
            state.danmaku[room_id].disconnect()
        except:
            pass
        del state.danmaku[room_id]

    recorded = None
    if state.recorder:
        success, result = state.recorder.stop_recording(room_id)
        if success:
            recorded = result
            state.add_log(f"录制完成: {os.path.basename(result)}", "success")

            # 把文件移到session目录（如果recorder没直接存那的话）
            if room_id in state.sessions:
                sess = state.sessions[room_id]
                target = sess.get_record_path()
                if os.path.abspath(result) != os.path.abspath(target):
                    shutil.move(result, target)
                    recorded = target
                    state.add_log(f"文件移至: {sess.dir_record}", "info")
        else:
            state.add_log(f"{result}", "warn")

    if recorded and os.path.exists(recorded):
        _process_recording(room_id, room_name, recorded)


def _hot_detector(room_id):
    """使用高能检测器定期检查高能时刻"""
    min_interval = state.config.get("danmaku_analysis", {}).get("min_peak_interval", 30)
    last_peak_time = 0
    while state._running:
        try:
            detector = state.high_energy.get(room_id)
            if detector:
                is_high, score = detector.check_high_energy()
                if is_high and (time.time() - last_peak_time >= min_interval):
                    segments = detector.get_high_energy_segments()
                    logs = []
                    breakdown = score.get("breakdown", score) if isinstance(score, dict) else {}
                    if breakdown.get("danmaku"):
                        logs.append(f"弹幕({score.get('danmaku_count',0)}条)")
                    if breakdown.get("gift"):
                        logs.append(f"礼物({score.get('gift_count',0)}件)")
                    if breakdown.get("sc"):
                        logs.append(f"SC({score.get('sc_count',0)}条)")
                    if breakdown.get("enter"):
                        logs.append(f"涌入({score.get('enter_count',0)}人)")
                    desc = "+".join(logs) if logs else f"总分{score.get('total',0):.0f}"
                    state.add_log(f"⚡ [{room_id}] 高能! {desc}", "warn")
                    last_peak_time = time.time()
                    # 保存到hot_moments
                    for s, e, ms, d in segments:
                        state.hot_moments.setdefault(room_id, []).append((s, e, min(1.0, ms/30), d))
            time.sleep(5)
        except Exception:
            time.sleep(5)


def _process_recording(room_id, room_name, recorded_file):
    sess = state.sessions.get(room_id)
    hot = state.hot_moments.get(room_id, [])

    # 尝试从高能检测器获取最后的分段
    detector = state.high_energy.get(room_id)
    if detector:
        try:
            segs = detector.get_high_energy_segments()
            min_interval = state.config.get("danmaku_analysis", {}).get("min_peak_interval", 30)
            last_peak_time = 0
            for s, e, ms, desc in segs:
                if s - last_peak_time >= min_interval:
                    hot.append((s, e, min(1.0, ms/30), desc))
                    last_peak_time = s
        except:
            pass

    if not hot:
        state.add_log("无高能时刻，跳过切片（可在弹幕关键词或密度阈值中调高敏感度）", "warn")
        return

    deduped = []
    for s in sorted(hot, key=lambda x: x[0]):
        if not deduped:
            deduped.append(s)
        elif s[0] - deduped[-1][1] < 5:
            l = deduped[-1]
            deduped[-1] = (l[0], max(l[1], s[1]), max(l[2], s[2]), f"{l[3]}; {s[3]}")
        else:
            deduped.append(s)
    deduped = deduped[:10]

    if not deduped:
        state.add_log("无高能时刻, 跳过切片", "warn")
        return

    if sess:
        # 按时间段命名切片，存到session的clips目录
        from auto_clipper import AutoClipper
        clipper = AutoClipper(FFMPEG_PATH, output_dir=str(sess.dir_clips))
        for seg in deduped:
            start_s, end_s, conf, reason = seg
            clip_path = sess.get_clip_path(start_s, end_s)
            # 截取单个片段
            import subprocess as sp
            sp.run([FFMPEG_PATH, "-y", "-ss", str(start_s), "-to", str(end_s), "-i", recorded_file,
                    "-c", "copy", clip_path], capture_output=True, timeout=120)
            if os.path.exists(clip_path):
                state.add_log(f"切片: {sess.get_clip_name(start_s, end_s)}", "success")
            else:
                state.add_log(f"切片失败: {start_s}-{end_s}s", "error")
    else:
        state.add_log("无session信息, 跳过切片", "warn")


def _replay_task(bvid):
    try:
        import requests as req
        h = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com"}
        state.add_log(f"获取视频: {bvid}", "info")
        cid_r = req.get(f"https://api.bilibili.com/x/player/pagelist?bvid={bvid}", headers=h, timeout=10).json()
        if cid_r["code"] != 0 or not cid_r.get("data"):
            state.add_log("获取CID失败", "error"); return
        cid = cid_r["data"][0]["cid"]
        dm_r = req.get(f"https://api.bilibili.com/x/v1/dm/list.so?oid={cid}", headers=h, timeout=15)
        if dm_r.status_code != 200:
            state.add_log("弹幕获取失败", "error"); return
        from scripts.danmaku_analyzer_fixed import DanmakuAnalyzer
        sugs = DanmakuAnalyzer().generate_clip_suggestions(dm_r.content.decode("utf-8", errors="replace"), 3600)
        if not sugs:
            state.add_log("无精彩片段", "warn"); return
        state.add_log(f"弹幕分析: {len(sugs)}个片段", "info")

        # 回放用replay目录
        replay_root = get_storage_root() / "回放" / bvid
        replay_root.mkdir(parents=True, exist_ok=True)
        video_path = str(replay_root / f"{bvid}.mp4")

        state.add_log("下载视频...", "info")
        import subprocess as sp
        sp.run(["yt-dlp", "-f", "best[height<=1080]", "-o", video_path, "--no-playlist", "--quiet",
                f"https://www.bilibili.com/video/{bvid}"], capture_output=True, timeout=300)

        if not os.path.exists(video_path):
            state.add_log("下载失败", "error"); return

        from auto_clipper import AutoClipper
        clipper = AutoClipper(FFMPEG_PATH, output_dir=str(replay_root))
        clip_dir = replay_root / "clips"
        clip_dir.mkdir(exist_ok=True)

        for sug in sugs:
            start_s = sug["start"]
            end_s = sug["end"]
            clip_name = f"{int(start_s):06d}_{int(end_s):06d}.mp4"
            clip_path = str(clip_dir / clip_name)
            sp.run([FFMPEG_PATH, "-y", "-ss", str(start_s), "-to", str(end_s), "-i", video_path,
                    "-c", "copy", clip_path], capture_output=True, timeout=120)
            if os.path.exists(clip_path):
                state.add_log(f"切片: {clip_name}", "success")

        state.add_log(f"回放完成, 共{len(sugs)}个切片", "success")
    except Exception as e:
        state.add_log(f"回放出错: {e}", "error")


# ====== Routes ======

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify(state.get_status())


@app.route("/api/logs")
def api_logs():
    return jsonify(state.logs[-100:])


@app.route("/api/start", methods=["POST"])
def api_start():
    if state._running:
        return jsonify({"ok": False, "msg": "监控已在运行中"})
    for p in [BASE_DIR / "config" / "config.yaml", BASE_DIR / "config.yaml"]:
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            state.config = cfg
            break
    errors = validate_config_strict(state.config)
    if errors:
        for e in errors:
            state.add_log("配置错误: " + e, "error")
        return jsonify({"ok": False, "msg": "配置不完整", "errors": errors})
    state._running = True
    threading.Thread(target=_monitor_loop, daemon=True).start()
    state.add_log("启动监控...", "info")
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    state._running = False
    if state.monitor:
        state.monitor.stop()
    for rid in list(state.danmaku.keys()):
        try:
            state.danmaku[rid].disconnect()
        except:
            pass
    state.danmaku.clear()
    state.add_log("监控已停止，正在处理已录制的直播...", "warn")
    threading.Thread(target=_finish_recording_on_stop, daemon=True).start()
    return jsonify({"ok": True})


def _finish_recording_on_stop():
    """
    手动停止时：停止所有录制 → 移动文件 → 切片
    同时保底清理所有残留FFmpeg进程
    """
    import subprocess as sp
    
    # 保底：杀掉所有残留的FFmpeg进程
    try:
        sp.run(["taskkill", "/F", "/IM", "ffmpeg.exe"], capture_output=True, timeout=5)
    except:
        pass
    
    rooms = state.get_rooms()
    
    def _process_one_room(room):
        rid = room["room_id"]
        name = room["anchor_name"]
        try:
            if rid not in state.sessions:
                return
            state.add_log(f"[{name}] 正在处理录制文件...", "info")
            if state.recorder:
                success, result = state.recorder.stop_recording(rid)
                if success and result and os.path.exists(result):
                    sess = state.sessions[rid]
                    target = sess.get_record_path()
                    if os.path.abspath(result) != os.path.abspath(target):
                        import shutil as sh
                        sh.move(result, target)
                        state.add_log(f"[{name}] 录制文件已保存: {os.path.basename(target)}", "success")
                    # 先停高能检测器
                    detector = state.high_energy.pop(rid, None)
                    if detector:
                        try: detector.stop()
                        except: pass
                    _process_recording(rid, name, target)
                else:
                    state.add_log(f"[{name}] 没有录制文件", "warn")
        except Exception as e:
            state.add_log(f"[{name}] 处理出错: {e}", "error")
            import traceback
            logger.error(f"stop processing error: {traceback.format_exc()}")
    
    # 并发处理每个房间
    import threading as _th
    threads = []
    for room in rooms:
        t = _th.Thread(target=_process_one_room, args=(room,), daemon=True)
        t.start()
        threads.append(t)
    
    # 等待所有完成（最多等5分钟）
    for t in threads:
        t.join(timeout=300)


@app.route("/api/replay", methods=["POST"])
def api_replay():
    d = request.json
    bv = (d.get("bvid", "") or "").strip()
    if not bv: return jsonify({"ok": False, "msg": "输入BV号"})
    if not bv.startswith("BV"): bv = "BV" + bv
    threading.Thread(target=_replay_task, args=(bv,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/validate")
def api_validate():
    cfg = state.config
    errors = validate_config_strict(cfg)
    return jsonify({"ok": len(errors) == 0, "errors": errors})


@app.route("/api/rooms", methods=["GET"])
def api_get_rooms():
    return jsonify(state.get_rooms())


@app.route("/api/rooms", methods=["POST"])
def api_add_room():
    d = request.json
    rid = d.get("room_id")
    name = d.get("anchor_name", "")
    if not rid: return jsonify({"ok": False, "msg": "[*房间号*] 必填，请输入B站直播间数字ID"})
    if not name or name.strip() == "" or name.startswith("主播"):
        return jsonify({"ok": False, "msg": "[*主播名称*] 必填，请输入主播昵称"})
    rid = int(rid)
    rooms = state.config.setdefault("rooms", [])
    for r in rooms:
        if r["room_id"] == rid:
            r["anchor_name"], r["enable"] = name, True; break
    else:
        rooms.append({"room_id": rid, "anchor_name": name, "room_url": f"https://live.bilibili.com/{rid}", "enable": True})
    _save_cfg()
    # 同步添加到状态监测
    if hasattr(state, '_status_monitor') and state._status_monitor:
        state._status_monitor.add_room(rid, name)
    state.add_log(f"添加: {name}({rid})", "info")
    return jsonify({"ok": True})


@app.route("/api/rooms/<int:rid>", methods=["DELETE"])
def api_rm_room(rid):
    state.config["rooms"] = [r for r in state.config.get("rooms", []) if r["room_id"] != rid]
    _save_cfg()
    # 从状态监测移除
    if hasattr(state, '_status_monitor') and state._status_monitor:
        state._status_monitor.remove_room(rid)
    return jsonify({"ok": True})


@app.route("/api/rooms/<int:rid>", methods=["PATCH"])
def api_patch_room(rid):
    d = request.json
    rooms = state.config.get("rooms", [])
    for r in rooms:
        if r["room_id"] == rid:
            if "hot_keywords" in d: r["hot_keywords"] = d["hot_keywords"]
            if "anchor_name" in d and d["anchor_name"].strip(): r["anchor_name"] = d["anchor_name"].strip()
            if "enable" in d: r["enable"] = d["enable"]
            _save_cfg()
            state.add_log(f"更新直播间 {rid} 配置", "info")
            return jsonify({"ok": True})
    return jsonify({"ok": False, "msg": "未找到该直播间"})


def _save_cfg():
    with open(BASE_DIR / "config" / "config.yaml", "w", encoding="utf-8") as f:
        yaml.dump(state.config, f, allow_unicode=True, default_flow_style=False)


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        d = request.json
        c = state.config
        if "check_interval" in d: c.setdefault("automation", {})["check_interval"] = int(d["check_interval"])
        if "density_threshold" in d: c.setdefault("danmaku_analysis", {})["density_threshold"] = int(d["density_threshold"])
        if "storage_root" in d: c["storage_root"] = d["storage_root"]
        _save_cfg()
        state.add_log("配置已更新", "info")
        return jsonify({"ok": True})
    a = state.config.get("automation", {})
    dm = state.config.get("danmaku_analysis", {})
    return jsonify({
        "check_interval": a.get("check_interval", 30),
        "density_threshold": dm.get("density_threshold", 10),
        "storage_root": state.config.get("storage_root", ""),
    })


# ====== 文件浏览API（支持新目录结构） ======
@app.route("/api/files")
def api_files():
    """浏览存储目录下的文件结构"""
    root = get_storage_root()
    result = []
    if not root.exists():
        return jsonify(result)

    for anchor_dir in sorted(root.iterdir(), key=os.path.getmtime, reverse=True):
        if not anchor_dir.is_dir():
            continue
        anchor = anchor_dir.name
        sessions_list = []
        for session_dir in sorted(anchor_dir.iterdir(), key=os.path.getmtime, reverse=True)[:20]:
            if not session_dir.is_dir():
                continue
            rec_dir = session_dir / "record"
            clips_dir = session_dir / "clips"
            records = [f.name for f in rec_dir.iterdir() if f.suffix in (".mp4", ".ts", ".mkv")] if rec_dir.exists() else []
            clips = []
            if clips_dir.exists():
                for f in sorted(clips_dir.iterdir(), key=os.path.getmtime, reverse=True)[:50]:
                    if f.suffix in (".mp4", ".ts", ".mkv"):
                        ts = datetime.fromtimestamp(f.stat().st_mtime).strftime("%m-%d %H:%M")
                        sz = f.stat().st_size
                        # 从文件名解析时间段
                        name = f.stem
                        parts = name.split("_")
                        time_range = name
                        # 新的命名格式: xxhxxmxxs_xxhxxmxxs
                        if len(parts) >= 2 and "h" in parts[0]:
                            try:
                                def parse_hmx(s):
                                    h = int(s.split("h")[0])
                                    rest = s.split("h")[1]
                                    m = int(rest.split("m")[0])
                                    sec = int(rest.split("m")[1].replace("s",""))
                                    return h,m,sec
                                h1,m1,s1 = parse_hmx(parts[0])
                                h2,m2,s2 = parse_hmx(parts[1])
                                time_range = f"{h1:02d}:{m1:02d}:{s1:02d} ~ {h2:02d}:{m2:02d}:{s2:02d}"
                            except:
                                pass
                        clips.append({"name": f.name, "time_range": time_range, "size": sz, "mtime": ts})
            sessions_list.append({
                "session": session_dir.name,
                "path": str(session_dir.relative_to(root)),
                "records": records,
                "clips": clips,
                "clip_count": len(clips),
            })
        result.append({
            "anchor": anchor,
            "path": str(anchor_dir.relative_to(root)),
            "sessions": sessions_list,
            "total_sessions": len(sessions_list),
        })
    return jsonify(result)


@app.route("/api/file/<path:filepath>")
def api_file_download(filepath):
    """下载存储目录下的任意文件"""
    root = get_storage_root()
    full = root / filepath
    if full.exists() and full.is_file():
        return send_file(str(full), as_attachment=True, download_name=full.name)
    return jsonify({"ok": False}), 404


@app.route("/api/file/delete/<path:filepath>", methods=["POST"])
def api_file_delete(filepath):
    root = get_storage_root()
    full = root / filepath
    if full.exists():
        if full.is_file():
            os.remove(full)
        elif full.is_dir():
            shutil.rmtree(full)
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 404


# ====== 兼容旧版clips API（只读storage根目录下所有clips） ======
@app.route("/api/clips")
def api_clips():
    # 新版优先返回storage结构
    root = get_storage_root()
    clips = []
    if root.exists():
        for f in root.rglob("*"):
            if f.is_file() and f.suffix in (".mp4", ".ts", ".mkv") and "clips" in str(f):
                clips.append({
                    "name": str(f.relative_to(root)),
                    "size": f.stat().st_size,
                    "time": datetime.fromtimestamp(f.stat().st_mtime).strftime("%m-%d %H:%M"),
                })
    clips.sort(key=lambda x: x["time"], reverse=True)
    return jsonify(clips[:50])


# ====== 启动 ======
if __name__ == "__main__":
    cfg_port = state.config.get("web_port", 5000)
    if not isinstance(cfg_port, int) or cfg_port < 1 or cfg_port > 65535:
        cfg_port = 5000
    state.config["web_port"] = cfg_port
    _save_cfg()

    # 启动后台状态轮询（Web页面加载时就能看到开播状态）
    state.start_status_poller()

    print(f"\n[B站直播切片系统 Web UI]")
    print(f"  http://localhost:{cfg_port}")
    print(f"  存储目录: {get_storage_root()}")
    print()
    app.run(host="127.0.0.1", port=cfg_port, debug=False, threaded=True)
