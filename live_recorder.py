#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
直播录制模块 - 获取直播流地址并用FFmpeg录制
"""

import os
import json
import time
import signal
import logging
import subprocess
import requests
from datetime import datetime

logger = logging.getLogger("LiveRecorder")


class LiveRecorder:
    """直播录制器"""
    _instance_registry = {}  # 类级别: room_id -> recording info，跨实例共享
    _pending_health_checks = set()  # 类级别: 正在后台健康检查的room_id

    def __init__(self, ffmpeg_path, output_dir="download"):
        self.ffmpeg_path = ffmpeg_path
        self.output_dir = output_dir
        self._recording = LiveRecorder._instance_registry  # 共享同一本字典
        os.makedirs(output_dir, exist_ok=True)

    def get_live_stream_url(self, room_id):
        """
        获取B站直播流地址 (v2 API)
        返回: (流地址列表, 流信息)
        """
        # 使用v2接口获取播放信息
        v2_api = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
        params = {
            "room_id": room_id,
            "protocol": "0,1",
            "format": "0,1,2",
            "codec": "0,1",
            "qn": 10000,
            "platform": "web",
            "ptype": 8,
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": f"https://live.bilibili.com/{room_id}",
        }

        try:
            resp = requests.get(v2_api, params=params, headers=headers, timeout=15)
            data = resp.json()

            if data.get("code") != 0:
                logger.error(f"获取直播流失败(v2): {data.get('message', '未知错误')}")
                logger.info("尝试旧版API...")
                return self._get_stream_url_legacy(room_id)

            playurl = data.get("data", {}).get("playurl_info", {}).get("playurl")
            if not playurl:
                logger.error("直播流返回数据为空")
                return None, None

            # 提取所有可用流地址
            stream_urls = []
            for stream in playurl.get("stream", []):
                for fmt in stream.get("format", []):
                    for codec in fmt.get("codec", []):
                        for url_info in codec.get("url_info", []):
                            full_url = url_info["host"] + codec["base_url"] + url_info["extra"]
                            if full_url not in stream_urls:
                                stream_urls.append(full_url)

            logger.info(f"获取到 {len(stream_urls)} 个流地址 (v2API)")
            return stream_urls, data["data"]

        except Exception as e:
            logger.error(f"获取直播流地址失败(v2): {e}")
            return self._get_stream_url_legacy(room_id)

    def _get_stream_url_legacy(self, room_id):
        """旧版API获取直播流 (备用)"""
        api_url = "https://api.live.bilibili.com/xlive/web-room/v1/playUrl/playUrl"

    def start_recording(self, room_id, room_name=""):
        """
        开始录制直播
        返回: (成功, 错误信息)
        """
        if room_id in self._recording:
            info = self._recording[room_id]
            if info.get("python_download"):
                if info["python_download"]["thread"].is_alive():
                    logger.warning(f"直播间 {room_name}({room_id}) 已在录制中(Python)")
                    return False, "已在录制中"
            elif info.get("process") and info["process"].poll() is None:
                logger.warning(f"直播间 {room_name}({room_id}) 已在录制中")
                return False, "已在录制中"

        # 获取直播流地址
        stream_urls, _ = self.get_live_stream_url(room_id)
        if not stream_urls:
            return False, "无法获取直播流地址，可能不在直播"

        # 使用第一个流地址
        stream_url = stream_urls[0]

        # 生成输出文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = room_name.replace(" ", "_").replace("/", "_") if room_name else f"room_{room_id}"
        output_filename = f"{safe_name}_{timestamp}.ts"
        output_path = os.path.join(self.output_dir, output_filename)

        # 构建FFmpeg命令 - 使用 user_agent + referer（比 -headers 兼容性更好）
        cmd = [
            self.ffmpeg_path,
            "-y",
            "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "-referer", f"https://live.bilibili.com/{room_id}",
            "-i", stream_url,
            "-c", "copy",
            "-f", "mpegts",
            "-bsf:a", "aac_adtstoasc",
            output_path,
        ]

        logger.info(f"开始录制: {room_name}({room_id})")
        logger.info(f"输出文件: {output_path}")
        logger.debug(f"FFmpeg命令: {' '.join(cmd)}")

        try:
            # stderr重定向到日志文件（而不是PIPE），避免管道缓冲区阻塞
            ffmpeg_log_dir = os.path.join(self.output_dir, "..", "logs")
            os.makedirs(ffmpeg_log_dir, exist_ok=True)
            ffmpeg_log_file = os.path.join(ffmpeg_log_dir, f"ffmpeg_{room_id}_{timestamp}.log")
            stderr_file = open(ffmpeg_log_file, "w", encoding="utf-8")
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
                creationflags=subprocess.CREATE_NO_WINDOW,  # Windows下不弹窗口
            )
            # 关闭文件句柄（子进程已持有副本）
            stderr_file.close()

            self._recording[room_id] = {
                "room_name": room_name,
                "process": process,
                "start_time": datetime.now(),
                "output_path": output_path,
                "stream_url": stream_url,
            }

            # 稍微等一下看进程有没有立即挂掉
            time.sleep(2)
            if process.poll() is not None:
                del self._recording[room_id]
                logger.error(f"FFmpeg启动后立即退出")
                return False, f"FFmpeg启动失败"

            # 稍微等一下看文件是否有数据写入
            time.sleep(3)
            has_data = os.path.exists(output_path) and os.path.getsize(output_path) > 0
            if not has_data:
                time.sleep(5)
                has_data = os.path.exists(output_path) and os.path.getsize(output_path) > 0
                if not has_data:
                    time.sleep(7)
                    has_data = os.path.exists(output_path) and os.path.getsize(output_path) > 0
                    if not has_data:
                        # 15秒还是0字节
                        if process.poll() is not None:
                            # 进程退出了，真失败
                            del self._recording[room_id]
                            logger.error(f"FFmpeg录制15秒后退出，文件为空")
                            return False, f"FFmpeg录制失败，进程退出"
                        else:
                            # 进程还在跑但文件0字节，后台健康检查线程继续监控
                            logger.warning(f"录制15秒仍为0字节但FFmpeg在运行，启动后台健康检查...")
                            self._start_health_check(room_id, room_name, process, stream_url, output_path, output_filename)
                            return False, "流启动较慢，后台健康检查中..."

            logger.info(f"✅ 录制中: {output_filename}")
            return True, output_path

        except Exception as e:
            logger.error(f"启动录制失败: {e}")
            return False, str(e)

    def _start_python_recording(self, room_id, room_name, stream_url, output_path, output_filename):
        """
        备用方案：用Python requests直接下载流（当FFmpeg无法获取数据时）
        """
        logger.info(f"使用Python流式下载: {room_name}({room_id})")
        try:
            import requests as req
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": f"https://live.bilibili.com/{room_id}",
            }
            resp = req.get(stream_url, headers=headers, stream=True, timeout=30)
            if resp.status_code != 200:
                logger.error(f"Python流式下载失败，HTTP {resp.status_code}")
                return False, f"HTTP {resp.status_code}"

            # 创建下载线程
            import threading
            stop_event = threading.Event()
            download_thread = threading.Thread(
                target=self._python_download_worker,
                args=(resp, output_path, stop_event),
                daemon=True,
            )
            download_thread.start()

            # 等几秒确认有数据
            time.sleep(5)
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                stop_event.set()
                download_thread.join(timeout=3)
                logger.error(f"Python流式下载5秒后仍无数据")
                return False, "Python流式下载无数据"

            self._recording[room_id] = {
                "room_name": room_name,
                "process": None,
                "start_time": datetime.now(),
                "output_path": output_path,
                "stream_url": stream_url,
                "python_download": {
                    "thread": download_thread,
                    "stop_event": stop_event,
                    "resp": resp,
                },
            }
            logger.info(f"✅ Python流式录制中: {output_filename}")
            return True, output_path

        except Exception as e:
            logger.error(f"Python流式下载启动失败: {e}")
            return False, str(e)

    def _start_health_check(self, room_id, room_name, process, stream_url, output_path, output_filename):
        """
        FFmpeg录制15秒后仍为0字节时的后台健康检查
        每10秒检查一次，最多等6分钟
        """
        LiveRecorder._pending_health_checks.add(room_id)
        import threading as _th
        _th.Thread(
            target=self._ffmpeg_health_worker,
            args=(room_id, room_name, process, stream_url, output_path, output_filename),
            daemon=True,
        ).start()
    
    def _ffmpeg_health_worker(self, room_id, room_name, process, stream_url, output_path, output_filename):
        """健康检查工作线程"""
        wait_cycles = 0
        while wait_cycles < 36:  # 最多等6分钟
            time.sleep(10)
            if process.poll() is not None:
                if os.path.exists(output_path) and os.path.getsize(output_path) == 0:
                    if room_id in self._recording:
                        del self._recording[room_id]
                    logger.error(f"FFmpeg [{room_name}] {wait_cycles*10}秒后退出，文件仍为空")
                LiveRecorder._pending_health_checks.discard(room_id)
                return
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                # FFmpeg开始写入数据了！注册到_recording
                LiveRecorder._pending_health_checks.discard(room_id)
                self._recording[room_id] = {
                    "room_name": room_name,
                    "process": process,
                    "start_time": datetime.now(),
                    "output_path": output_path,
                    "stream_url": stream_url,
                }
                logger.info(f"✅ FFmpeg [{room_name}] 在{(wait_cycles+1)*10}秒后开始写入数据!")
                return
            wait_cycles += 1
        
        # 6分钟超时，尝试Python下载
        LiveRecorder._pending_health_checks.discard(room_id)
        if os.path.exists(output_path) and os.path.getsize(output_path) == 0:
            logger.warning(f"FFmpeg [{room_name}] 6分钟仍无数据，尝试Python下载")
            process.terminate()
            try:
                process.wait(timeout=5)
            except:
                subprocess.run(["taskkill", "/F", "/PID", str(process.pid)], capture_output=True, timeout=5)
            if room_id in self._recording:
                del self._recording[room_id]
            # 获取新流地址
            try:
                urls, _ = self.get_live_stream_url(room_id)
                if urls:
                    self._start_python_recording(room_id, room_name, urls[0], output_path, output_filename)
            except Exception as e:
                logger.error(f"Python下载fallback失败: {e}")

    def _python_download_worker(self, resp, output_path, stop_event):
        """Python流式下载工作线程"""
        try:
            with open(output_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if stop_event.is_set():
                        break
                    if chunk:
                        f.write(chunk)
        except Exception as e:
            logger.error(f"Python流式下载出错: {e}")
        finally:
            resp.close()

    def stop_recording(self, room_id):
        """
        停止录制
        返回: (成功, 录制文件路径 或 错误信息)
        """
        if room_id not in self._recording:
            logger.warning(f"直播间 {room_id} 没有正在进行的录制")
            return False, "没有正在录制的任务"

        info = self._recording[room_id]
        output_path = info["output_path"]

        logger.info(f"停止录制: {info['room_name']}({room_id})")

        # 判断是否为Python流式下载模式
        if info.get("python_download"):
            logger.info("停止Python流式下载...")
            pd_info = info["python_download"]
            pd_info["stop_event"].set()
            pd_info["thread"].join(timeout=5)
            try:
                pd_info["resp"].close()
            except:
                pass
            del self._recording[room_id]
            return self._check_output(output_path, info['room_name'], room_id)

        process = info["process"]
        try:
            # 先发 SIGTERM
            if process.poll() is None:
                if os.name == "nt":
                    # Windows用taskkill
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                        capture_output=True,
                        timeout=5,
                    )
                else:
                    process.terminate()

                # 等待进程退出
                process.wait(timeout=10)

        except subprocess.TimeoutExpired:
            logger.warning("FFmpeg进程未响应，强制结束")
            if process.poll() is None:
                process.kill()
        except Exception as e:
            logger.error(f"停止录制时出错: {e}")

        # 检查输出文件是否存在且有大小
        file_size = 0
        if os.path.exists(output_path):
            file_size = os.path.getsize(output_path)
            duration = (datetime.now() - info["start_time"]).total_seconds()
            logger.info(f"📁 录制文件: {output_path} ({file_size/1024/1024:.1f}MB, {duration:.0f}秒)")

        del self._recording[room_id]

        if file_size > 1024:  # 至少1KB才算有效
            return True, output_path
        else:
            return False, "录制文件为空或太小"

    def _check_output(self, output_path, room_name, room_id):
        """检查录制输出文件"""
        file_size = 0
        if os.path.exists(output_path):
            file_size = os.path.getsize(output_path)
        if file_size > 1024:
            return True, output_path
        else:
            return False, "录制文件为空或太小"

    def is_recording(self, room_id):
        """检查是否正在录制"""
        if room_id not in self._recording:
            return False
        process = self._recording[room_id]["process"]
        if process.poll() is not None:
            # 进程结束了，清理
            del self._recording[room_id]
            return False
        return True

    def get_all_recording(self):
        """获取所有录制任务状态"""
        result = {}
        for room_id, info in list(self._recording.items()):
            # Python下载模式 - process为None
            if info.get("python_download"):
                pd = info["python_download"]
                if pd["thread"].is_alive():
                    result[room_id] = {
                        "room_name": info["room_name"],
                        "start_time": info["start_time"].strftime("%H:%M:%S"),
                        "duration": (datetime.now() - info["start_time"]).total_seconds(),
                        "output_path": info["output_path"],
                        "method": "python",
                    }
                else:
                    del self._recording[room_id]
                continue
            # FFmpeg模式
            process = info.get("process")
            if process is None or process.poll() is not None:
                del self._recording[room_id]
                continue
            result[room_id] = {
                "room_name": info["room_name"],
                "start_time": info["start_time"].strftime("%H:%M:%S"),
                "duration": (datetime.now() - info["start_time"]).total_seconds(),
                "output_path": info["output_path"],
                "method": "ffmpeg",
            }
        return result


# 测试
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    ffmpeg = r"C:\Users\MECHREVO\.openclaw\workspace\ffmpeg-portable\ffmpeg-master-latest-win64-gpl\bin\ffmpeg.exe"
    recorder = LiveRecorder(ffmpeg)

    # 测试获取流地址
    print("测试获取直播流地址...")
    urls, info = recorder.get_live_stream_url(21669525)
    if urls:
        print(f"获取到 {len(urls)} 个流地址")
        print(f"第一个: {urls[0][:80]}...")
    else:
        print("当前可能不在直播中")
