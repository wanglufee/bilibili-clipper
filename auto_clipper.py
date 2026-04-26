#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动切片模块 - 基于高能时刻用FFmpeg生成视频片段
"""

import os
import re
import subprocess
import logging
from datetime import datetime

logger = logging.getLogger("AutoClipper")


class AutoClipper:
    """自动视频切片生成器"""

    def __init__(self, ffmpeg_path, output_dir="clips"):
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = os.path.join(os.path.dirname(ffmpeg_path), "ffprobe.exe")
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def get_video_duration(self, video_path):
        """获取视频时长（秒）"""
        cmd = [
            self.ffprobe_path,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                return float(result.stdout.strip())
        except Exception as e:
            logger.warning(f"获取视频时长失败: {e}")
        return None

    def clip_segment(self, video_path, start_time, duration, output_path):
        """
        从视频中截取一个片段
        先用 -ss 快速seek，再精确切割
        """
        safe_name = os.path.basename(output_path)
        logger.info(f"切片: {safe_name} ({start_time:.1f}s ~ {start_time+duration:.1f}s)")

        # 第一步：快速截取（使用copy，不重新编码）
        temp_file = output_path + ".temp.ts"
        cmd = [
            self.ffmpeg_path,
            "-y",
            "-ss", str(start_time),
            "-i", video_path,
            "-t", str(duration),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            temp_file,
        ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=duration + 60
            )
            if result.returncode != 0:
                logger.error(f"快速截取失败: {result.stderr[:300]}")
                return False

            # 第二步：重新编码到精确位置（修正关键帧对齐问题）
            cmd_exact = [
                self.ffmpeg_path,
                "-y",
                "-i", temp_file,
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-c:a", "aac",
                "-b:a", "128k",
                "-pix_fmt", "yuv420p",
                output_path,
            ]

            result = subprocess.run(
                cmd_exact, capture_output=True, text=True, timeout=duration + 60
            )

            # 清理临时文件
            if os.path.exists(temp_file):
                os.remove(temp_file)

            if result.returncode == 0:
                file_size = os.path.getsize(output_path)
                logger.info(f"✅ 切片完成: {safe_name} ({file_size/1024/1024:.1f}MB)")
                return True
            else:
                logger.error(f"精确截取失败: {result.stderr[:300]}")
                # 删掉不完整的输出
                if os.path.exists(output_path):
                    os.remove(output_path)
                return False

        except subprocess.TimeoutExpired:
            logger.error(f"切片超时: {safe_name}")
            for f in [temp_file, output_path]:
                if os.path.exists(f):
                    os.remove(f)
            return False
        except Exception as e:
            logger.error(f"切片出错: {e}")
            return False

    def clip_segments(self, video_path, segments, room_name=""):
        """
        从视频中截取多个片段
        :param video_path: 源视频路径
        :param segments: [(start_sec, end_sec, confidence, reason), ...]
        :param room_name: 直播间名称（用于文件名）
        :return: 生成的clip文件路径列表
        """
        if not os.path.exists(video_path):
            logger.error(f"视频文件不存在: {video_path}")
            return []

        video_duration = self.get_video_duration(video_path)
        if video_duration is None:
            logger.warning("无法获取视频时长，使用文件大小作为参考")

        clip_paths = []
        safe_name = room_name.replace(" ", "_").replace("/", "_") if room_name else "clip"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        for i, seg in enumerate(segments, 1):
            # segments可能是tuple或dict
            if isinstance(seg, dict):
                start = seg.get("start", 0)
                end = seg.get("end", start + 15)
                confidence = seg.get("confidence", 1.0)
                reason = seg.get("reason", "")
            else:
                start, end = seg[0], seg[1]
                confidence = seg[2] if len(seg) > 2 else 1.0
                reason = seg[3] if len(seg) > 3 else ""

            duration = end - start
            if duration < 3:
                logger.warning(f"片段 #{i} 太短 ({duration:.1f}s)，跳过")
                continue
            if duration > 300:
                logger.warning(f"片段 #{i} 太长 ({duration:.1f}s)，限制为60s")
                duration = 60

            # 生成文件名
            output_name = f"{safe_name}_{timestamp}_{i:02d}.mp4"
            output_path = os.path.join(self.output_dir, output_name)

            if self.clip_segment(video_path, start, duration, output_path):
                clip_paths.append({
                    "path": output_path,
                    "start": start,
                    "end": end,
                    "duration": duration,
                    "confidence": confidence,
                    "reason": reason,
                })

        logger.info(f"共生成 {len(clip_paths)}/{len(segments)} 个切片")
        return clip_paths

    def merge_clips(self, clip_paths, output_path):
        """
        合并多个视频片段
        :param clip_paths: [{"path": "...", ...}, ...] 或 路径字符串列表
        :param output_path: 输出视频路径
        :return: 合并后的文件路径，失败返回None
        """
        # 规范化输入
        paths = []
        for cp in clip_paths:
            if isinstance(cp, dict):
                p = cp.get("path", "")
            else:
                p = str(cp)
            if os.path.exists(p):
                paths.append(p)

        if len(paths) == 0:
            logger.error("没有可合并的视频片段")
            return None

        if len(paths) == 1:
            # 只有一个文件，直接复制
            import shutil
            shutil.copy2(paths[0], output_path)
            logger.info(f"只有一个片段，直接复制到: {output_path}")
            return output_path

        # 创建concat用文件列表
        concat_dir = os.path.dirname(output_path) or self.output_dir
        concat_file = os.path.join(concat_dir, "concat_list.txt")

        try:
            with open(concat_file, "w", encoding="utf-8") as f:
                for p in paths:
                    # 转义Windows路径
                    escaped = p.replace("\\", "/").replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")

            cmd = [
                self.ffmpeg_path,
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_file,
                "-c", "copy",
                "-movflags", "+faststart",
                output_path,
            ]

            logger.info(f"合并 {len(paths)} 个片段...")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

            # 清理临时文件
            if os.path.exists(concat_file):
                os.remove(concat_file)

            if result.returncode == 0:
                file_size = os.path.getsize(output_path)
                logger.info(f"✅ 合并完成: {output_path} ({file_size/1024/1024:.1f}MB)")
                return output_path
            else:
                logger.error(f"合并失败: {result.stderr[:300]}")
                return None

        except Exception as e:
            logger.error(f"合并过程出错: {e}")
            if os.path.exists(concat_file):
                os.remove(concat_file)
            return None

    def make_final_video(self, video_path, segments, output_path, room_name=""):
        """
        一站式：截取片段 + 合并
        :return: 最终视频路径 或 None
        """
        clips = self.clip_segments(video_path, segments, room_name)
        if not clips:
            logger.error("没有生成任何切片")
            return None

        if len(clips) == 1:
            final_path = clips[0]["path"]
            # 如果是单个文件，直接改名到目标路径
            if final_path != output_path:
                os.rename(final_path, output_path)
            return output_path

        result = self.merge_clips(clips, output_path)
        return result


# 测试
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    ffmpeg = r"C:\Users\MECHREVO\.openclaw\workspace\ffmpeg-portable\ffmpeg-master-latest-win64-gpl\bin\ffmpeg.exe"
    clipper = AutoClipper(ffmpeg)

    # 测试获取视频时长
    test_video = r"C:\Users\MECHREVO\.openclaw\workspace\bilibili-clipper\download\sample_video.f30064.mp4"
    if os.path.exists(test_video):
        duration = clipper.get_video_duration(test_video)
        print(f"视频时长: {duration:.1f}秒")

        # 测试截取
        segments = [(10, 20, 0.9, "测试片段")]
        result = clipper.clip_segments(test_video, segments, "测试")
        if result:
            print(f"生成切片: {result[0]['path']}")
    else:
        print(f"测试视频不存在: {test_video}")
        print("跳过测试")
