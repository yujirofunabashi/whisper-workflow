#!/usr/bin/env python3
"""Simple, robust browser GUI for whisper workflow.

Design goal:
- Prioritize reliability over rich client-side interactions.
- Start flow works with plain HTML form submit (minimal JS).
"""

from __future__ import annotations

import argparse
import cgi
import datetime as dt
import html
import json
import os
import re
import shutil
import signal
import socketserver
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlsplit

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.expanduser(os.environ.get("WHISPER_GUI_CACHE_DIR", "~/Library/Caches/WhisperGUI"))
UPLOADS_DIR = os.path.join(CACHE_DIR, "uploads")
LOGS_DIR = os.path.join(CACHE_DIR, "logs")
OUTPUTS_DIR = os.path.expanduser(os.environ.get("WHISPER_GUI_OUTPUT_DIR", "~/Downloads/WhisperGUI"))
TRANSCRIBE_SCRIPT = os.path.join(SCRIPT_DIR, "transcribe_workflow.sh")
PREFLIGHT_SCRIPT = os.path.join(SCRIPT_DIR, "preflight.sh")
RETRY_SCRIPT = os.path.join(SCRIPT_DIR, "retry_failed_segments.sh")

PRESETS = {"x1", "x4", "x8", "x16"}
PRIORITIES = {"accuracy", "balanced", "speed"}
MODE_STRATEGIES = {"auto", "custom"}
CUSTOM_MODES = {"single-pass", "segmented"}
RECOVERY_MODES = {"failed", "time_range"}
UPLOAD_RETENTION_SEC = 24 * 60 * 60
LOG_RETENTION_SEC = 7 * 24 * 60 * 60
LOG_HEARTBEAT_SEC = 10
SEGMENTS_TOTAL_RE = re.compile(r"^Segments:\s*(\d+)\s*$")
SEGMENT_COMPLETED_RE = re.compile(r"^\[3/4\]\s+Completed\s+segment_(\d+)\s*$")


def now_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sanitize_filename(name: str, fallback: str) -> str:
    base = os.path.basename((name or "").strip()) or fallback
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return (cleaned or fallback)[:120]


def tail_lines(path: str, limit: int = 500) -> str:
    if not path or not os.path.isfile(path):
        return ""
    out = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            out.append(line.rstrip("\n"))
            if len(out) > limit:
                out.pop(0)
    return "\n".join(out)


def localize_log_line(line: str) -> str:
    if not line:
        return line

    table = [
        ("=== Whisper Transcription Workflow ===", "=== Whisper文字起こしワークフロー ==="),
        ("=== Performance Summary ===", "=== 処理サマリー ==="),
        ("=== Completed Successfully ===", "=== 正常終了 ==="),
        ("=== Completed With Warnings ===", "=== 警告付き完了 ==="),
        ("Cleaning up intermediate files...", "中間ファイルをクリーンアップ中..."),
        ("Done! Output saved to:", "完了: 出力先"),
        ("Input:", "入力:"),
        ("Output:", "出力:"),
        ("Model:", "モデル:"),
        ("Preset:", "プリセット:"),
        ("Accuracy:", "精度目安:"),
        ("Profile:", "プロファイル:"),
        ("Mode:", "モード:"),
        ("Language:", "言語:"),
        ("Jobs:", "並列ジョブ数:"),
        ("Threads per job:", "ジョブあたりスレッド数:"),
        ("Segment length:", "分割長:"),
        ("Retry count:", "リトライ回数:"),
        ("Retry backoff:", "リトライ間隔:"),
        ("Preflight normalize:", "Preflight正規化:"),
        ("Mode override:", "モード上書き:"),
        ("WorkDir:", "作業ディレクトリ:"),
        ("Convert time:", "変換時間:"),
        ("Transcribe time:", "文字起こし時間:"),
        ("Concat time:", "結合時間:"),
        ("Total time:", "合計時間:"),
        ("Audio duration:", "音声長:"),
        ("RTF (transcribe only):", "RTF (文字起こしのみ):"),
        ("RTF (end-to-end):", "RTF (全体):"),
        ("Speed (transcribe only):", "処理速度 (文字起こしのみ):"),
        ("Speed (end-to-end):", "処理速度 (全体):"),
        ("Applying loudnorm correction", "音量正規化を適用"),
        ("[1/4] Converting to 16kHz WAV...", "[1/4] 16kHz WAVへ変換中..."),
        ("[2/2] Transcribing full audio in single pass...", "[2/2] 音声全体を文字起こし中 (単一パス)..."),
        ("[2/4] Segmenting into", "[2/4] 分割中"),
        ("[3/4] Transcribing segments (Parallel)...", "[3/4] 分割音声を文字起こし中 (並列)..."),
        ("[3/4] Completed", "[3/4] セグメント完了"),
        ("[SEGMENT_FAILED]", "[セグメント失敗]"),
        ("[4/4] Concatenating results...", "[4/4] 結果を結合中..."),
        ("Segments:", "分割数:"),
        ("[recover] input:", "[追補] 入力:"),
        ("[recover] partial output:", "[追補] 部分結果:"),
        ("[recover] failed list:", "[追補] 対象セグメント一覧:"),
        ("[recover] segment_time:", "[追補] 分割秒数:"),
        ("[recover] converting input to 16kHz wav...", "[追補] 入力を16kHz wavへ変換中..."),
        ("[recover] regenerating segments...", "[追補] セグメント再生成中..."),
        ("[recover] transcribing", "[追補] 文字起こし中"),
        ("[recover] failed:", "[追補] 失敗:"),
        ("[recover] missing segment wav:", "[追補] セグメント欠落:"),
        ("[recover] merging recovered segments into partial output...", "[追補] 結果を部分出力へマージ中..."),
        ("[recover] recovered segments:", "[追補] 回復セグメント数:"),
        ("[recover] remaining failed segments:", "[追補] 未回復セグメント数:"),
        ("[recover] output:", "[追補] 出力:"),
        ("[recover] remaining list:", "[追補] 未回復一覧:"),
        ("Force CPU mode:", "CPU固定モード:"),
        ("started", "開始"),
        ("completed", "完了"),
        ("failed", "失敗"),
        ("canceled", "キャンセル"),
    ]

    out = line
    for old, new in table:
        out = out.replace(old, new)
    return out


def localize_log_text(text: str) -> str:
    if not text:
        return text
    return "\n".join(localize_log_line(line) for line in text.splitlines())


def parse_int_clamped(value: Any, default: int, min_value: int, max_value: int) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        n = default
    return max(min_value, min(max_value, n))


def recommend_auto_mode(duration_sec: Any, preset: str, priority: str, cpu_count: int) -> dict[str, Any]:
    duration = to_float(duration_sec) or 0.0
    cpu = max(2, cpu_count or 4)
    half_cpu = max(2, cpu // 2)
    prio = priority if priority in PRIORITIES else "balanced"
    model_preset = preset if preset in PRESETS else "x4"

    # Aggressive auto policy: 15+ min uses segmented mode by default.
    if duration >= 120 * 60:
        jobs = min(8, max(4, half_cpu))
        seg_t = 90
    elif duration >= 60 * 60:
        jobs = min(6, max(4, half_cpu))
        seg_t = 120
    elif duration >= 30 * 60:
        jobs = min(5, max(3, half_cpu))
        seg_t = 150
    elif duration >= 15 * 60:
        jobs = min(4, max(2, half_cpu))
        seg_t = 180
    else:
        return {"mode": "single-pass", "jobs": 1, "segment_time": 240, "reason": "15分未満のため単一パスが最適"}

    if prio == "accuracy":
        jobs = max(2, jobs - 1)
        seg_t = min(300, seg_t + 30)
    elif prio == "speed":
        jobs = min(8, jobs + 1)
        seg_t = max(60, seg_t - 30)

    if model_preset == "x1":
        jobs = max(2, jobs - 1)
        seg_t = min(300, seg_t + 30)
    elif model_preset == "x16":
        jobs = min(8, jobs + 1)
        seg_t = max(60, seg_t - 30)

    reason_suffix = {"accuracy": "精度寄り", "balanced": "バランス", "speed": "速度寄り"}[prio]
    return {
        "mode": "segmented",
        "jobs": jobs,
        "segment_time": seg_t,
        "reason": f"{int(duration // 60)}分音声のため分割並列 ({reason_suffix}, jobs={jobs}, {seg_t}秒分割)",
    }


def to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def format_duration_ja(seconds: Any) -> str:
    sec = to_float(seconds)
    if sec is None or sec <= 0:
        return "0秒"

    total = int(round(sec))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60

    if h > 0:
        return f"{h}時間{m}分{s}秒"
    if m > 0:
        return f"{m}分{s}秒"
    return f"{s}秒"


def format_db(value: Any) -> str:
    f = to_float(value)
    if f is None:
        return "不明"
    return f"{f:.1f}dB"


def format_percent(value: Any) -> str:
    f = to_float(value)
    if f is None:
        return "不明"
    p = max(0.0, min(100.0, f * 100.0))
    return f"{p:.0f}%"


def parse_time_to_seconds(value: str) -> Optional[int]:
    text = (value or "").strip()
    if not text:
        return None

    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        try:
            return max(0, int(float(text)))
        except ValueError:
            return None

    parts = text.split(":")
    if len(parts) not in {2, 3}:
        return None

    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None

    if len(nums) == 2:
        mm, ss = nums
        if mm < 0 or ss < 0:
            return None
        return int(mm * 60 + ss)

    hh, mm, ss = nums
    if hh < 0 or mm < 0 or ss < 0:
        return None
    return int(hh * 3600 + mm * 60 + ss)


def build_segment_ids_from_ranges(ranges_text: str, segment_time: int) -> tuple[list[str], Optional[str]]:
    if segment_time <= 0:
        return [], "segment_time が不正です。"

    tokens = [t.strip() for t in re.split(r"[,\n]+", ranges_text or "") if t.strip()]
    if not tokens:
        return [], "時刻範囲が指定されていません。"

    ids: set[int] = set()
    for token in tokens:
        if "-" in token:
            left, right = token.split("-", 1)
            start_sec = parse_time_to_seconds(left)
            end_sec = parse_time_to_seconds(right)
            if start_sec is None or end_sec is None:
                return [], f"時刻範囲の形式が不正です: {token}"
            if end_sec < start_sec:
                start_sec, end_sec = end_sec, start_sec
            if end_sec == start_sec:
                end_sec = start_sec + 1
        else:
            start_sec = parse_time_to_seconds(token)
            if start_sec is None:
                return [], f"時刻範囲の形式が不正です: {token}"
            end_sec = start_sec + 1

        start_idx = max(0, start_sec // segment_time)
        end_idx = max(start_idx, max(0, (end_sec - 1) // segment_time))
        for idx in range(start_idx, end_idx + 1):
            ids.add(idx)

    if not ids:
        return [], "対象セグメントを算出できませんでした。"
    if len(ids) > 5000:
        return [], "対象セグメント数が多すぎます。範囲を絞ってください。"

    return [f"segment_{idx:03d}" for idx in sorted(ids)], None


def default_recovered_output_name(partial_output_path: str) -> str:
    base = os.path.basename(partial_output_path or "") or "transcription_result.txt"
    if base.endswith(".txt"):
        return base[:-4] + ".recovered.txt"
    return base + ".recovered.txt"


def resolve_output_path(name_or_path: str, fallback_name: str) -> str:
    raw = (name_or_path or "").strip()
    if raw and os.path.isabs(raw):
        return raw

    safe = sanitize_filename(raw or fallback_name, fallback_name)
    if not safe.endswith(".txt"):
        safe += ".txt"
    return os.path.join(OUTPUTS_DIR, safe)


def read_recovery_meta(partial_output_path: str) -> dict[str, str]:
    meta_path = f"{partial_output_path}.recovery_meta"
    meta: dict[str, str] = {}
    if not os.path.isfile(meta_path):
        return meta

    try:
        with open(meta_path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                if key:
                    meta[key] = val.strip()
    except OSError:
        return {}
    return meta


def estimate_runtime_window_sec(
    duration_sec: Any,
    preset: str,
    mode: str,
    jobs: int,
    use_vad: bool,
    force_cpu_mode: bool,
) -> Optional[tuple[int, int, int]]:
    duration = to_float(duration_sec)
    if duration is None or duration <= 0:
        return None

    p = preset if preset in PRESETS else "x4"
    m = mode if mode in {"single-pass", "segmented"} else "single-pass"
    j = max(1, min(8, int(jobs)))

    # Rough RTF model (lower is faster). This is only a planning hint.
    base_rtf = {
        "x1": 0.95,
        "x4": 0.55,
        "x8": 0.30,
        "x16": 0.18,
    }.get(p, 0.55)

    if m == "segmented":
        gain = 1.0 + (j - 1) * 0.55  # diminishing returns for parallel jobs
        rtf = max(0.08, (base_rtf / gain) + 0.04)
    else:
        rtf = base_rtf

    if use_vad:
        rtf *= 0.90
    if force_cpu_mode:
        rtf *= 1.15

    transcribe_sec = duration * rtf
    convert_sec = max(2.0, min(45.0, duration * 0.008))
    concat_sec = 0.0 if m == "single-pass" else max(1.0, min(20.0, duration / 600.0))
    total_sec = transcribe_sec + convert_sec + concat_sec

    # Keep a visible uncertainty range for real-world variance.
    low = int(max(1, total_sec * 0.8))
    high = int(max(low + 1, total_sec * 1.4))
    center = int(max(1, round(total_sec)))
    return center, low, high


def cleanup_dir(path: str, max_age_sec: int, skip_paths: set[str]) -> None:
    if not os.path.isdir(path):
        return

    now = time.time()
    for name in os.listdir(path):
        full = os.path.join(path, name)
        if not os.path.isfile(full):
            continue
        real = os.path.realpath(full)
        if real in skip_paths:
            continue

        try:
            age = now - os.path.getmtime(full)
        except OSError:
            continue

        if age <= max_age_sec:
            continue

        try:
            os.remove(full)
        except OSError:
            pass


def find_vad_model_path() -> str:
    candidates = [
        os.path.expanduser("~/.cache/whisper-cpp/ggml-silero-v6.2.0.bin"),
        os.path.expanduser("~/.cache/whisper-cpp/ggml-silero-v5.1.2.bin"),
        os.path.join(SCRIPT_DIR, "models", "ggml-silero-v6.2.0.bin"),
        os.path.join(SCRIPT_DIR, "models", "ggml-silero-v5.1.2.bin"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return ""


class AppState:
    def __init__(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        os.makedirs(UPLOADS_DIR, exist_ok=True)
        os.makedirs(LOGS_DIR, exist_ok=True)
        os.makedirs(OUTPUTS_DIR, exist_ok=True)

        self.lock = threading.Lock()
        self.process: Optional[subprocess.Popen[str]] = None

        self.status = "idle"  # idle|running|completed|failed|canceled
        self.running = False
        self.started_at = ""
        self.finished_at = ""
        self.last_error = ""
        self.ui_message = ""

        self.input_file = ""
        self.input_name = ""
        self.preset = "x4"
        self.priority = "balanced"
        self.mode_strategy = "auto"
        self.custom_mode = "segmented"
        self.custom_jobs = max(2, min(8, (os.cpu_count() or 4) // 2))
        self.custom_segment_time = 60
        self.resolved_mode = ""
        self.resolved_jobs = 0
        self.resolved_segment_time = 0
        self.resolved_mode_reason = ""
        self.use_vad = False
        self.use_vad_effective = False
        self.vad_model_path = ""
        self.force_cpu_mode = False
        self.auto_correction = True
        self.output_name = "transcription_result.txt"
        self.output_file = os.path.join(OUTPUTS_DIR, self.output_name)
        self.log_file = ""

        self.preflight_result: Optional[dict[str, Any]] = None
        self.preflight_error = ""
        self.preflight_at = ""
        self.preflight_skipped = False

        self.failure_reason = ""
        self.failure_action = ""
        self.applied_corrections: list[str] = []
        self.run_started_monotonic = 0.0
        self.last_heartbeat_sec = 0
        self.progress_total_segments = 0
        self.progress_completed_segments = 0
        self.progress_completed_ids: set[str] = set()
        self.estimated_total_sec = 0
        self.estimated_low_sec = 0
        self.estimated_high_sec = 0
        self.recovery_mode = "failed"
        self.recovery_ranges = ""
        self.recovery_segment_time = ""
        self.recovery_retry_count = 1
        self.recovery_preset = "keep"
        self.recovery_partial_output = ""
        self.recovery_output_name = ""

        self.cleanup_stale_files()

    def dependencies(self) -> list[str]:
        missing = []
        if not os.path.isfile(TRANSCRIBE_SCRIPT):
            missing.append("transcribe_workflow.sh")
        if not os.path.isfile(PREFLIGHT_SCRIPT):
            missing.append("preflight.sh")
        if not shutil.which("ffmpeg"):
            missing.append("ffmpeg")
        if not shutil.which("ffprobe"):
            missing.append("ffprobe")
        if not shutil.which("whisper-cli"):
            missing.append("whisper-cli")
        return missing

    def cleanup_stale_files(self) -> None:
        with self.lock:
            skip_paths = {
                p
                for p in (
                    os.path.realpath(self.input_file) if self.input_file else "",
                    os.path.realpath(self.log_file) if self.log_file else "",
                )
                if p
            }
        cleanup_dir(UPLOADS_DIR, UPLOAD_RETENTION_SEC, skip_paths)
        cleanup_dir(LOGS_DIR, LOG_RETENTION_SEC, skip_paths)

    def _write_log(self, line: str) -> None:
        if not self.log_file:
            return
        with open(self.log_file, "a", encoding="utf-8", errors="replace") as f:
            f.write(line)

    def _update_progress_from_log_line(self, line: str) -> None:
        text = (line or "").strip()
        if not text:
            return

        m_total = SEGMENTS_TOTAL_RE.match(text)
        if m_total:
            self.progress_total_segments = parse_int_clamped(m_total.group(1), 0, 0, 999999)
            return

        m_done = SEGMENT_COMPLETED_RE.match(text)
        if m_done:
            seg_id = m_done.group(1)
            if seg_id not in self.progress_completed_ids:
                self.progress_completed_ids.add(seg_id)
                self.progress_completed_segments = len(self.progress_completed_ids)
            return

    def _save_upload(self, item: cgi.FieldStorage) -> tuple[Optional[str], Optional[str], Optional[str]]:
        if not getattr(item, "file", None):
            return None, None, "音声ファイルが選択されていません。"

        original = getattr(item, "filename", "") or "audio_input"
        safe = sanitize_filename(original, "audio_input")
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(UPLOADS_DIR, f"{ts}_{safe}")

        try:
            with open(dst, "wb") as out:
                shutil.copyfileobj(item.file, out, length=1024 * 1024)
        except OSError as e:
            return None, None, f"アップロード保存失敗: {e}"

        if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
            try:
                os.remove(dst)
            except OSError:
                pass
            return None, None, "アップロードされたファイルが空です。"

        return dst, original, None

    def _run_preflight(self, input_path: str, priority: str) -> dict[str, Any]:
        cmd = ["bash", PREFLIGHT_SCRIPT, input_path, priority]
        completed = subprocess.run(
            cmd,
            cwd=SCRIPT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            raise RuntimeError(f"preflight.sh failed (exit={completed.returncode}): {stderr[-300:]}")

        raw = (completed.stdout or "").strip()
        if not raw:
            raise RuntimeError("preflight.sh returned empty output")

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"preflight JSON parse failed: {e}") from e

        if not isinstance(data, dict):
            raise RuntimeError("preflight output is not a JSON object")

        return data

    def _resolve_mode_config(
        self,
        mode_strategy: str,
        custom_mode: str,
        custom_jobs: int,
        custom_segment_time: int,
        preflight_result: Optional[dict[str, Any]],
        preset: str,
        priority: str,
    ) -> dict[str, Any]:
        duration_sec = None
        if isinstance(preflight_result, dict):
            in_data = preflight_result.get("input")
            if isinstance(in_data, dict):
                duration_sec = in_data.get("duration_sec")

        if mode_strategy == "custom":
            if custom_mode == "segmented":
                return {
                    "mode": "segmented",
                    "jobs": custom_jobs,
                    "segment_time": custom_segment_time,
                    "reason": f"カスタム指定: 分割並列 (jobs={custom_jobs}, {custom_segment_time}秒分割)",
                }
            return {"mode": "single-pass", "jobs": 1, "segment_time": 240, "reason": "カスタム指定: 単一パス"}

        auto = recommend_auto_mode(duration_sec, preset, priority, os.cpu_count() or 4)
        auto["reason"] = "自動最適化: " + str(auto.get("reason") or "")
        return auto

    def preflight_with_form(self, form: cgi.FieldStorage) -> tuple[bool, str]:
        with self.lock:
            if self.running:
                return False, "実行中は診断できません。"

        priority = "balanced"
        if "priority" in form and form.getfirst("priority"):
            priority = str(form.getfirst("priority")).strip()
        if priority not in PRIORITIES:
            priority = "balanced"

        selected_preset = str(form.getfirst("selected_preset") or "").strip()
        if selected_preset not in PRESETS:
            selected_preset = ""

        input_path = ""
        input_name = ""
        uploaded_new = False

        if "audio_file" in form:
            item = form["audio_file"]
            if isinstance(item, list):
                item = item[0]

            # New upload is optional for re-diagnosis.
            if getattr(item, "filename", ""):
                uploaded_new = True
                saved, original, err = self._save_upload(item)
                if err:
                    return False, err
                input_path = saved or ""
                input_name = original or "audio_input"

        with self.lock:
            if not input_path:
                if self.input_file and os.path.isfile(self.input_file):
                    input_path = self.input_file
                    input_name = self.input_name or os.path.basename(self.input_file)
                else:
                    return False, "音声ファイルを選択してください。"

        preflight_result: Optional[dict[str, Any]] = None
        preflight_err = ""
        try:
            preflight_result = self._run_preflight(input_path, priority)
        except Exception as e:  # noqa: BLE001
            preflight_err = str(e)

        with self.lock:
            self.input_file = input_path
            self.input_name = input_name
            self.priority = priority
            if selected_preset:
                self.preset = selected_preset
            self.preflight_at = now_text()
            self.preflight_skipped = False

            if preflight_result is not None:
                self.preflight_result = preflight_result
                self.preflight_error = ""
                if uploaded_new and not selected_preset:
                    recommended = str(preflight_result.get("recommended_preset") or "").strip()
                    if recommended in PRESETS:
                        self.preset = recommended
                self.ui_message = "Preflight診断が完了しました。"
            else:
                self.preflight_result = None
                self.preflight_error = f"Preflight失敗: {preflight_err}"
                self.preflight_skipped = True
                self.ui_message = "Preflightに失敗したため診断をスキップします。"

        return True, "preflight"

    def start_with_form(self, form: cgi.FieldStorage) -> tuple[bool, str]:
        with self.lock:
            if self.running:
                return False, "すでに実行中です。"

        missing = self.dependencies()
        if missing:
            return False, "依存不足: " + ", ".join(missing)

        preset = "x4"
        if "preset" in form and form.getfirst("preset"):
            preset = str(form.getfirst("preset")).strip()
        if preset not in PRESETS:
            preset = "x4"

        auto_correction = "auto_correction" in form
        use_vad = "use_vad" in form
        mode_strategy = str(form.getfirst("mode_strategy") or "").strip()
        if mode_strategy not in MODE_STRATEGIES:
            mode_strategy = "auto"

        custom_mode = str(form.getfirst("custom_mode") or "").strip()
        if custom_mode not in CUSTOM_MODES:
            custom_mode = "segmented"

        with self.lock:
            prev_custom_jobs = self.custom_jobs
            prev_custom_segment = self.custom_segment_time

        custom_jobs = parse_int_clamped(form.getfirst("custom_jobs"), prev_custom_jobs, 1, 8)
        custom_segment_time = parse_int_clamped(form.getfirst("custom_segment_time"), prev_custom_segment, 30, 600)

        output_name = "transcription_result.txt"
        if "output_name" in form and form.getfirst("output_name"):
            output_name = str(form.getfirst("output_name")).strip()
        output_name = sanitize_filename(output_name, "transcription_result.txt")
        if not output_name.endswith(".txt"):
            output_name += ".txt"
        output_file = os.path.join(OUTPUTS_DIR, output_name)

        with self.lock:
            input_path = self.input_file
            input_name = self.input_name
            preflight_result = self.preflight_result
            preflight_skipped = self.preflight_skipped
            selected_priority = self.priority
            current_force_cpu_mode = self.force_cpu_mode

        if not input_path or not os.path.isfile(input_path):
            return False, "先に音声ファイルをアップロードして診断してください。"

        if preflight_result is None and not preflight_skipped:
            return False, "先にPreflight診断を実行してください。"

        verdict = ""
        corrections: list[str] = []
        if isinstance(preflight_result, dict):
            verdict = str(preflight_result.get("verdict") or "")
            raw_corrections = preflight_result.get("corrections")
            if isinstance(raw_corrections, list):
                corrections = [str(x) for x in raw_corrections]

        if verdict == "NOT_RECOMMENDED":
            return False, "診断結果が非推奨のため開始できません。別形式で保存し直してください。"

        normalize_enabled = auto_correction and ("volume_normalize" in corrections)
        mode_config = self._resolve_mode_config(
            mode_strategy=mode_strategy,
            custom_mode=custom_mode,
            custom_jobs=custom_jobs,
            custom_segment_time=custom_segment_time,
            preflight_result=preflight_result,
            preset=preset,
            priority=selected_priority,
        )
        resolved_mode = str(mode_config.get("mode") or "single-pass")
        resolved_jobs = parse_int_clamped(mode_config.get("jobs"), 1, 1, 8)
        resolved_segment_time = parse_int_clamped(mode_config.get("segment_time"), 240, 30, 600)
        resolved_reason = str(mode_config.get("reason") or "")
        use_vad_effective = use_vad and resolved_mode == "single-pass"
        vad_model_path = ""
        if use_vad and resolved_mode == "segmented":
            resolved_reason = resolved_reason + " / VADは安定性のため単一パス時のみ有効"
        if use_vad_effective:
            vad_model_path = find_vad_model_path()
            if not vad_model_path:
                use_vad_effective = False
                resolved_reason = resolved_reason + " / VADモデル未検出のため無効"

        duration_for_estimate = None
        if isinstance(preflight_result, dict):
            in_data = preflight_result.get("input")
            if isinstance(in_data, dict):
                duration_for_estimate = in_data.get("duration_sec")
        estimate_window = estimate_runtime_window_sec(
            duration_for_estimate,
            preset,
            resolved_mode,
            resolved_jobs if resolved_mode == "segmented" else 1,
            use_vad_effective,
            current_force_cpu_mode,
        )

        run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = os.path.join(LOGS_DIR, f"run_{run_id}.log")

        env = os.environ.copy()
        env["WHISPER_PRESET"] = preset
        env["WHISPER_MODE"] = resolved_mode
        if current_force_cpu_mode:
            env["WHISPER_FORCE_CPU"] = "1"
        else:
            env.pop("WHISPER_FORCE_CPU", None)
        if use_vad_effective:
            env["WHISPER_USE_VAD"] = "1"
            env["WHISPER_VAD_MODEL"] = vad_model_path
        else:
            env.pop("WHISPER_USE_VAD", None)
            env.pop("WHISPER_VAD_MODEL", None)
        if resolved_mode == "segmented":
            env["WHISPER_JOBS"] = str(resolved_jobs)
            env["WHISPER_SEGMENT_TIME"] = str(resolved_segment_time)
        else:
            env.pop("WHISPER_JOBS", None)
            env.pop("WHISPER_SEGMENT_TIME", None)
        if normalize_enabled:
            env["WHISPER_PREFLIGHT_NORMALIZE"] = "1"
        else:
            env.pop("WHISPER_PREFLIGHT_NORMALIZE", None)

        cmd = [TRANSCRIBE_SCRIPT, input_path, output_file]
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=SCRIPT_DIR,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except Exception as e:  # noqa: BLE001
            return False, f"起動失敗: {e}"

        with self.lock:
            self.process = proc
            self.running = True
            self.status = "running"
            self.started_at = now_text()
            self.run_started_monotonic = time.time()
            self.last_heartbeat_sec = 0
            self.progress_total_segments = 0
            self.progress_completed_segments = 0
            self.progress_completed_ids = set()
            self.estimated_total_sec = estimate_window[0] if estimate_window is not None else 0
            self.estimated_low_sec = estimate_window[1] if estimate_window is not None else 0
            self.estimated_high_sec = estimate_window[2] if estimate_window is not None else 0
            self.finished_at = ""
            self.last_error = ""
            self.failure_reason = ""
            self.failure_action = ""
            self.ui_message = "実行を開始しました。"

            self.input_file = input_path
            self.input_name = input_name
            self.preset = preset
            self.mode_strategy = mode_strategy
            self.custom_mode = custom_mode
            self.custom_jobs = custom_jobs
            self.custom_segment_time = custom_segment_time
            self.resolved_mode = resolved_mode
            self.resolved_jobs = resolved_jobs if resolved_mode == "segmented" else 1
            self.resolved_segment_time = resolved_segment_time if resolved_mode == "segmented" else 240
            self.resolved_mode_reason = resolved_reason
            self.use_vad = use_vad
            self.use_vad_effective = use_vad_effective
            self.vad_model_path = vad_model_path
            self.auto_correction = auto_correction
            self.applied_corrections = ["volume_normalize"] if normalize_enabled else []
            self.output_name = output_name
            self.output_file = output_file
            self.log_file = log_file

            self._write_log(f"[{self.started_at}] 開始\n")
            self._write_log(f"入力={input_path}\n")
            self._write_log(f"出力={output_file}\n")
            self._write_log(f"プリセット={preset}\n")
            self._write_log(f"優先度={self.priority}\n")
            self._write_log(f"モード戦略={mode_strategy}\n")
            self._write_log(f"実行モード={self.resolved_mode}\n")
            if self.resolved_mode == "segmented":
                self._write_log(f"分割設定=jobs:{self.resolved_jobs}, segment_time:{self.resolved_segment_time}s\n")
            self._write_log(f"モード理由={self.resolved_mode_reason}\n")
            self._write_log(f"VAD要求={use_vad}\n")
            self._write_log(f"VAD実効={use_vad_effective}\n")
            if vad_model_path:
                self._write_log(f"VADモデル={vad_model_path}\n")
            self._write_log(f"CPU固定モード={self.force_cpu_mode}\n")
            self._write_log(f"自動補正={auto_correction}\n")
            self._write_log(f"適用補正={','.join(self.applied_corrections) if self.applied_corrections else 'なし'}\n")
            if self.preflight_result is not None:
                self._write_log("preflight_result=" + json.dumps(self.preflight_result, ensure_ascii=False) + "\n")
            elif self.preflight_skipped:
                self._write_log("preflight_result=skipped\n")
            self._write_log("-----------------------------\n")

            threading.Thread(target=self._pump_output, args=(proc,), daemon=True).start()
            threading.Thread(target=self._wait_finish, args=(proc,), daemon=True).start()
            threading.Thread(target=self._heartbeat_progress, args=(proc,), daemon=True).start()

        return True, "started"

    def retry_failed_with_form(self, form: cgi.FieldStorage) -> tuple[bool, str]:
        with self.lock:
            if self.running:
                return False, "すでに実行中です。"
            input_path = self.input_file
            current_output = self.output_file
            prev_retry_count = self.recovery_retry_count
            prev_mode = self.recovery_mode
            prev_ranges = self.recovery_ranges
            prev_preset = self.recovery_preset
            prev_partial_output = self.recovery_partial_output
            prev_recovery_output = self.recovery_output_name

        if not input_path or not os.path.isfile(input_path):
            return False, "入力音声が見つかりません。先に通常実行を行ってください。"

        if not os.path.isfile(RETRY_SCRIPT):
            return False, "retry_failed_segments.sh が見つかりません。"

        mode = str(form.getfirst("recovery_mode") or prev_mode or "failed").strip()
        if mode not in RECOVERY_MODES:
            mode = "failed"

        ranges_text = str(form.getfirst("recovery_ranges") or prev_ranges or "").strip()
        retry_count = parse_int_clamped(form.getfirst("recovery_retry_count"), prev_retry_count, 1, 5)
        preset_opt = str(form.getfirst("recovery_preset") or prev_preset or "keep").strip()
        if preset_opt != "keep" and preset_opt not in PRESETS:
            preset_opt = "keep"

        partial_default_name = os.path.basename(current_output or "") or "transcription_result.txt"
        partial_input = str(form.getfirst("recovery_partial_output") or prev_partial_output or partial_default_name).strip()
        partial_output = resolve_output_path(partial_input, partial_default_name)
        if not os.path.isfile(partial_output):
            return False, f"部分結果ファイルが見つかりません: {partial_output}"

        recovered_default_name = default_recovered_output_name(partial_output)
        recovered_input = str(form.getfirst("recovery_output_name") or prev_recovery_output or recovered_default_name).strip()
        recovered_output = resolve_output_path(recovered_input, recovered_default_name)

        segment_time_override_text = str(form.getfirst("recovery_segment_time") or "").strip()
        segment_time_override = 0
        if segment_time_override_text:
            try:
                segment_time_override = int(segment_time_override_text)
            except ValueError:
                return False, f"分割秒は整数で指定してください: {segment_time_override_text}"
            if segment_time_override < 30 or segment_time_override > 3600:
                return False, "分割秒は 30〜3600 の範囲で指定してください。"

        meta = read_recovery_meta(partial_output)
        seg_from_meta = parse_int_clamped(meta.get("SEGMENT_TIME"), 0, 0, 3600)
        seg_from_state = 0
        with self.lock:
            if self.resolved_mode == "segmented":
                seg_from_state = parse_int_clamped(self.resolved_segment_time, 0, 0, 3600)
        segment_time_effective = segment_time_override or seg_from_meta or seg_from_state

        failed_file = f"{partial_output}.failed_segments.txt"
        target_count = 0
        if mode == "failed":
            if not os.path.isfile(failed_file):
                return False, f"失敗セグメント一覧が見つかりません: {failed_file}"
            try:
                with open(failed_file, "r", encoding="utf-8", errors="replace") as f:
                    target_count = sum(
                        1
                        for line in f
                        if re.fullmatch(r"\s*segment_[0-9]+\s*", line or "")
                    )
            except OSError:
                target_count = 0
            if target_count <= 0:
                return False, "失敗セグメント一覧が空です。"
        else:
            if not ranges_text:
                return False, "時刻範囲を入力してください。例: 00:10:00-00:12:30, 00:31:00"
            if segment_time_effective <= 0:
                return False, "分割秒を特定できません。`分割秒` を入力して再実行してください。"

            ids, err = build_segment_ids_from_ranges(ranges_text, segment_time_effective)
            if err:
                return False, err
            target_count = len(ids)
            run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            failed_file = os.path.join(LOGS_DIR, f"retry_targets_{run_id}.txt")
            try:
                with open(failed_file, "w", encoding="utf-8") as f:
                    for seg_id in ids:
                        f.write(seg_id + "\n")
            except OSError as e:
                return False, f"時刻範囲の保存に失敗しました: {e}"

        env = os.environ.copy()
        env["WHISPER_RETRY_COUNT"] = str(retry_count)
        if segment_time_effective > 0:
            env["WHISPER_SEGMENT_TIME"] = str(segment_time_effective)
        else:
            env.pop("WHISPER_SEGMENT_TIME", None)
        if preset_opt in PRESETS:
            env["WHISPER_PRESET"] = preset_opt
        else:
            env.pop("WHISPER_PRESET", None)

        run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = os.path.join(LOGS_DIR, f"retry_{run_id}.log")
        cmd = [RETRY_SCRIPT, input_path, partial_output, failed_file, recovered_output]
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=SCRIPT_DIR,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except Exception as e:  # noqa: BLE001
            return False, f"追補起動に失敗しました: {e}"

        if segment_time_effective > 0 and target_count > 0:
            per_seg_sec = max(8, int(round(segment_time_effective * 0.35)))
            est_total = int(target_count * per_seg_sec + 10)
            est_low = max(1, int(round(est_total * 0.7)))
            est_high = max(est_low + 1, int(round(est_total * 1.6)))
        else:
            est_total = 0
            est_low = 0
            est_high = 0

        with self.lock:
            self.process = proc
            self.running = True
            self.status = "running"
            self.started_at = now_text()
            self.run_started_monotonic = time.time()
            self.last_heartbeat_sec = 0
            self.progress_total_segments = 0
            self.progress_completed_segments = 0
            self.progress_completed_ids = set()
            self.estimated_total_sec = est_total
            self.estimated_low_sec = est_low
            self.estimated_high_sec = est_high
            self.finished_at = ""
            self.last_error = ""
            self.failure_reason = ""
            self.failure_action = ""
            self.ui_message = "失敗区間の追補を開始しました。"

            self.recovery_mode = mode
            self.recovery_ranges = ranges_text
            self.recovery_segment_time = str(segment_time_override_text or "")
            self.recovery_retry_count = retry_count
            self.recovery_preset = preset_opt
            self.recovery_partial_output = os.path.basename(partial_output)
            self.recovery_output_name = os.path.basename(recovered_output)

            self.resolved_mode = "recovery"
            self.resolved_mode_reason = "失敗区間追補"
            self.use_vad_effective = False
            self.vad_model_path = ""
            self.output_name = os.path.basename(recovered_output)
            self.output_file = recovered_output
            self.log_file = log_file

            self._write_log(f"[{self.started_at}] 追補開始\n")
            self._write_log(f"入力={input_path}\n")
            self._write_log(f"部分結果={partial_output}\n")
            self._write_log(f"追補出力={recovered_output}\n")
            self._write_log(f"追補モード={mode}\n")
            if ranges_text:
                self._write_log(f"時刻範囲={ranges_text}\n")
            self._write_log(f"対象一覧={failed_file}\n")
            self._write_log(f"対象件数={target_count}\n")
            self._write_log(f"分割秒={segment_time_effective or '-'}\n")
            self._write_log(f"追補プリセット={preset_opt}\n")
            self._write_log(f"リトライ回数={retry_count}\n")
            self._write_log("-----------------------------\n")

            threading.Thread(target=self._pump_output, args=(proc,), daemon=True).start()
            threading.Thread(target=self._wait_finish, args=(proc,), daemon=True).start()
            threading.Thread(target=self._heartbeat_progress, args=(proc,), daemon=True).start()

        return True, "retry-started"

    def _pump_output(self, proc: subprocess.Popen[str]) -> None:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            with self.lock:
                if proc is self.process:
                    self._write_log(line)
                    self._update_progress_from_log_line(line)

    def _heartbeat_progress(self, proc: subprocess.Popen[str]) -> None:
        while True:
            time.sleep(LOG_HEARTBEAT_SEC)
            with self.lock:
                if proc is not self.process or not self.running:
                    return

                now_sec = int(max(0.0, time.time() - self.run_started_monotonic))
                if now_sec - self.last_heartbeat_sec < LOG_HEARTBEAT_SEC:
                    continue

                self.last_heartbeat_sec = now_sec
                if self.resolved_mode == "segmented" and self.progress_total_segments > 0:
                    done = max(0, min(self.progress_completed_segments, self.progress_total_segments))
                    total = self.progress_total_segments
                    remaining = max(0, total - done)
                    eta_text = "算出中"
                    speed_text = "-"
                    if done > 0 and now_sec > 0:
                        speed = done / max(1, now_sec)
                        speed_text = f"{speed:.3f} seg/秒"
                        eta_sec = int(round(remaining / speed)) if speed > 0 else 0
                        eta_text = format_duration_ja(eta_sec)
                    self.ui_message = f"処理中... {now_sec}秒経過 / 完了 {done}/{total} / 残り目安 {eta_text}"
                    self._write_log(
                        f"[進行中] {now_sec}秒経過: 完了 {done}/{total}, 速度 {speed_text}, 推定残り {eta_text}\n"
                    )
                elif self.estimated_total_sec > 0:
                    rem = max(0, self.estimated_total_sec - now_sec)
                    rem_text = format_duration_ja(rem)
                    self.ui_message = f"処理中... {now_sec}秒経過 / 残り目安 {rem_text}"
                    self._write_log(f"[進行中] {now_sec}秒経過: 推定残り {rem_text}\n")
                else:
                    self.ui_message = f"処理中... {now_sec}秒経過"
                    self._write_log(
                        f"[進行中] {now_sec}秒経過: 処理は継続中です（単一パス中はログが少なく見えることがあります）\n"
                    )

    def _failure_guidance(self, log_text: str) -> tuple[str, str]:
        lower = (log_text or "").lower()

        if "ggml-metal-device.m" in lower or ("failed to process audio" in lower and "vad" in lower):
            return (
                "VAD実行時に内部エラーが発生しました。",
                "VADをOFFにして再試行してください（今回の実装では失敗時に自動でVAD OFF再試行します）。",
            )

        if "model file not found" in lower or "download example:" in lower:
            return "モデルファイルが見つかりません。", "`./install_models.sh` を実行してください。"

        if "no space left on device" in lower:
            return "ディスク容量が不足しています。", "不要ファイルを削除してから再実行してください。"

        if "failed after" in lower and "attempt" in lower:
            return "whisper-cli のリトライがすべて失敗しました。", "軽量プリセット (x8/x16) で再試行してください。"

        if "converting to 16khz wav" in lower and (
            "invalid data" in lower
            or "error while" in lower
            or "could not" in lower
            or "failed" in lower
        ):
            return "音声ファイルの変換に失敗しました。", "別形式 (m4a/mp3/wav) で保存し直して再実行してください。"

        return "処理に失敗しました。", "ログを確認し、必要に応じて軽量プリセットで再試行してください。"

    def _wait_finish(self, proc: subprocess.Popen[str]) -> None:
        rc = proc.wait()
        with self.lock:
            if proc is not self.process:
                return

            self.running = False
            self.finished_at = now_text()
            self.process = None
            self.run_started_monotonic = 0.0
            self.last_heartbeat_sec = 0

            if self.status == "canceled":
                self.status = "canceled"
                self.ui_message = "キャンセルしました。"
                self._write_log(f"[{self.finished_at}] キャンセル\n")
            elif rc == 0:
                self.status = "completed"
                done_log = tail_lines(self.log_file, 200).lower()
                if "completed with warnings" in done_log:
                    self.ui_message = "一部失敗ありで完了しました。"
                else:
                    self.ui_message = "完了しました。"
                self._write_log(f"[{self.finished_at}] 完了\n")
            else:
                self.status = "failed"
                self.last_error = f"exit={rc}"
                log_text = tail_lines(self.log_file, 800)
                if "ggml-metal-device.m" in log_text.lower():
                    self.force_cpu_mode = True
                reason, action = self._failure_guidance(log_text)
                self.failure_reason = reason
                self.failure_action = action
                self.ui_message = f"失敗しました ({self.last_error})"
                self._write_log(f"[{self.finished_at}] 失敗 (exit={rc})\n")

                if os.path.isfile(self.output_file) and os.path.getsize(self.output_file) == 0:
                    try:
                        os.remove(self.output_file)
                    except OSError:
                        pass

        self.cleanup_stale_files()

    def cancel(self) -> tuple[bool, str]:
        with self.lock:
            proc = self.process
            if not proc or not self.running:
                return False, "実行中ジョブがありません。"
            self.status = "canceled"

        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            with self.lock:
                self.ui_message = "キャンセル要求を送信しました。"
            return True, "canceled"
        except Exception as e:  # noqa: BLE001
            with self.lock:
                self.status = "running"
            return False, f"キャンセル失敗: {e}"

    def snapshot(self) -> dict:
        with self.lock:
            st = {
                "running": self.running,
                "status": self.status,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "last_error": self.last_error,
                "ui_message": self.ui_message,
                "input_file": self.input_file,
                "input_name": self.input_name,
                "preset": self.preset,
                "priority": self.priority,
                "mode_strategy": self.mode_strategy,
                "custom_mode": self.custom_mode,
                "custom_jobs": self.custom_jobs,
                "custom_segment_time": self.custom_segment_time,
                "resolved_mode": self.resolved_mode,
                "resolved_jobs": self.resolved_jobs,
                "resolved_segment_time": self.resolved_segment_time,
                "resolved_mode_reason": self.resolved_mode_reason,
                "use_vad": self.use_vad,
                "use_vad_effective": self.use_vad_effective,
                "vad_model_path": self.vad_model_path,
                "force_cpu_mode": self.force_cpu_mode,
                "auto_correction": self.auto_correction,
                "output_name": self.output_name,
                "output_file": self.output_file,
                "log_file": self.log_file,
                "preflight_result": self.preflight_result,
                "preflight_error": self.preflight_error,
                "preflight_at": self.preflight_at,
                "preflight_skipped": self.preflight_skipped,
                "failure_reason": self.failure_reason,
                "failure_action": self.failure_action,
                "applied_corrections": list(self.applied_corrections),
                "recovery_mode": self.recovery_mode,
                "recovery_ranges": self.recovery_ranges,
                "recovery_segment_time": self.recovery_segment_time,
                "recovery_retry_count": self.recovery_retry_count,
                "recovery_preset": self.recovery_preset,
                "recovery_partial_output": self.recovery_partial_output,
                "recovery_output_name": self.recovery_output_name,
            }

        raw_log_tail = tail_lines(st["log_file"], 600)
        st["log_tail"] = raw_log_tail
        st["log_tail_ja"] = localize_log_text(raw_log_tail)
        st["missing_dependencies"] = self.dependencies()
        return st


HTML_TEMPLATE = """<!doctype html>
<html lang=\"ja\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />
  <title>Whisper GUI</title>
  <style>
    body { margin:0; padding:16px; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif; background:#0b1220; color:#e5e7eb; }
    .wrap { max-width:1080px; margin:0 auto; }
    .card { border:1px solid #334155; border-radius:10px; background:#111827; padding:12px; margin-bottom:12px; }
    .row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:8px; }
    .row:last-child { margin-bottom:0; }
    label { min-width:100px; color:#94a3b8; font-size:12px; }
    input[type=text], input[type=number], select, input[type=file] { flex:1; min-width:240px; background:#0b1325; color:#e5e7eb; border:1px solid #334155; border-radius:8px; padding:8px; }
    input[type=checkbox] { transform: translateY(1px); }
    button { border:1px solid #475569; background:#1f2937; color:#e5e7eb; border-radius:8px; padding:8px 12px; cursor:pointer; }
    button:hover { background:#334155; }
    button:disabled { opacity:0.5; cursor:not-allowed; }
    .muted { color:#94a3b8; font-size:12px; }
    .ok { color:#22c55e; }
    .warn { color:#f59e0b; }
    .err { color:#f87171; }
    pre { margin:0; white-space:pre-wrap; min-height:48vh; max-height:68vh; overflow:auto; background:#020617; border:1px solid #1e293b; border-radius:8px; padding:10px; font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
    a { color:#93c5fd; }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <div class=\"card\">
      <div><strong>Whisper GUI (Preflight v1)</strong></div>
      <div class=\"muted\">音声を選択すると自動でPreflight診断します。</div>
      <div class=\"row\" style=\"margin-top:8px\">
        <span>status:</span>
        <strong class=\"{status_class}\">{status}</strong>
        <span class=\"muted\">started: {started_at} / finished: {finished_at}</span>
      </div>
      <div class=\"row\"><span class=\"muted\">message: {ui_message}</span></div>
      {deps_html}
      {error_html}
      {failure_html}
    </div>

    <div class=\"card\">
      <form id=\"preflight-form\" action=\"/preflight\" method=\"post\" enctype=\"multipart/form-data\">
        <div class=\"row\">
          <label>音声</label>
          <input id=\"audio_file\" type=\"file\" name=\"audio_file\" accept=\".m4a,.mp3,.wav,.mp4,.aac,.flac,.ogg,.webm\" />
        </div>
        <input id=\"preflight_selected_preset\" type=\"hidden\" name=\"selected_preset\" value=\"\" />
        {current_input_html}
        <div class=\"row\">
          <label>優先度</label>
          <select name=\"priority\">
            <option value=\"accuracy\" {prio_accuracy}>精度優先</option>
            <option value=\"balanced\" {prio_balanced}>バランス</option>
            <option value=\"speed\" {prio_speed}>速度優先</option>
          </select>
          <button id=\"preflight-submit\" type=\"submit\" {preflight_disabled}>診断実行</button>
        </div>
        <div id=\"preflight-loading\" class=\"muted\" style=\"display:none\">診断中...（長尺音声は最大60秒ほどかかります）</div>
      </form>
      {preflight_html}
    </div>

    <div class=\"card\">
      <form action=\"/start\" method=\"post\">
        <div class=\"row\">
          <label>プリセット</label>
          <select id=\"preset_select\" name=\"preset\">
            <option value=\"x1\" {sel_x1}>x1 (最高精度)</option>
            <option value=\"x4\" {sel_x4}>x4 (高精度)</option>
            <option value=\"x8\" {sel_x8}>x8 (中精度)</option>
            <option value=\"x16\" {sel_x16}>x16 (軽量)</option>
          </select>
        </div>
        <div class=\"row\">
          <label>実行モード</label>
          <select id=\"mode_strategy\" name=\"mode_strategy\">
            <option value=\"auto\" {mode_auto}>最適自動選択</option>
            <option value=\"custom\" {mode_custom}>カスタム指定</option>
          </select>
          <select id=\"custom_mode\" name=\"custom_mode\">
            <option value=\"single-pass\" {custom_single}>単一パス</option>
            <option value=\"segmented\" {custom_segmented}>分割並列</option>
          </select>
        </div>
        <div class=\"row\">
          <label>分割設定</label>
          <input id=\"custom_jobs\" type=\"number\" min=\"1\" max=\"8\" name=\"custom_jobs\" value=\"{custom_jobs}\" />
          <input id=\"custom_segment_time\" type=\"number\" min=\"30\" max=\"600\" name=\"custom_segment_time\" value=\"{custom_segment_time}\" />
          <span class=\"muted\">jobs / segment秒（カスタム分割時のみ有効）</span>
        </div>
        <div class=\"row\">
          <label>VAD</label>
          <input type=\"checkbox\" name=\"use_vad\" value=\"1\" {use_vad_checked} />
          <span class=\"muted\">音声区間検出（必要時のみON。既定OFF）</span>
        </div>
        <div class=\"row\">
          <label>自動補正</label>
          <input type=\"checkbox\" name=\"auto_correction\" value=\"1\" {auto_correction_checked} />
          <span class=\"muted\">ON時、必要な場合のみ loudnorm を適用</span>
        </div>
        <div class=\"row\">
          <label>出力名</label>
          <input type=\"text\" name=\"output_name\" value=\"{output_name}\" />
          <button type=\"submit\" {start_disabled}>開始</button>
        </div>
      </form>
      <div class=\"row\" style=\"margin-top:8px\">
        <form action=\"/cancel\" method=\"post\">
          <button type=\"submit\" {cancel_disabled}>キャンセル</button>
        </form>
        <a href=\"/\">更新</a>
        <a href=\"/download\">結果をダウンロード</a>
        <a href=\"/reveal-output\">出力をFinder表示</a>
      </div>
      <div class=\"row\"><span class=\"muted\">出力保存先: {output_file}</span></div>
      <div class=\"row\"><span class=\"muted\">入力(現在): {input_file}</span></div>
      {run_info_html}
    </div>

    <div class=\"card\">
      <div><strong>失敗区間の追補実行</strong></div>
      <div class=\"muted\">一部失敗時の残り区間だけ再実行します。時刻範囲指定にも対応しています。</div>
      {recovery_status_html}
      <form action=\"/retry-failed\" method=\"post\">
        <div class=\"row\">
          <label>部分結果</label>
          <input type=\"text\" name=\"recovery_partial_output\" value=\"{recovery_partial_output}\" />
          <span class=\"muted\">既定は現在の出力名（必要なら絶対パス指定可）</span>
        </div>
        <div class=\"row\">
          <label>追補方式</label>
          <select id=\"recovery_mode\" name=\"recovery_mode\">
            <option value=\"failed\" {recovery_mode_failed}>失敗一覧から再実行</option>
            <option value=\"time_range\" {recovery_mode_time_range}>時刻範囲を指定</option>
          </select>
        </div>
        <div class=\"row\">
          <label>時刻範囲</label>
          <input id=\"recovery_ranges\" type=\"text\" name=\"recovery_ranges\" value=\"{recovery_ranges}\" />
          <span class=\"muted\">例: 00:10:00-00:12:30, 00:31:00</span>
        </div>
        <div class=\"row\">
          <label>分割秒(任意)</label>
          <input id=\"recovery_segment_time\" type=\"number\" min=\"30\" max=\"3600\" name=\"recovery_segment_time\" value=\"{recovery_segment_time}\" />
          <span class=\"muted\">空欄時は .recovery_meta から自動読込</span>
        </div>
        <div class=\"row\">
          <label>追補プリセット</label>
          <select name=\"recovery_preset\">
            <option value=\"keep\" {recovery_preset_keep}>元設定を引継ぎ</option>
            <option value=\"x1\" {recovery_preset_x1}>x1</option>
            <option value=\"x4\" {recovery_preset_x4}>x4</option>
            <option value=\"x8\" {recovery_preset_x8}>x8</option>
            <option value=\"x16\" {recovery_preset_x16}>x16</option>
          </select>
          <input type=\"number\" min=\"1\" max=\"5\" name=\"recovery_retry_count\" value=\"{recovery_retry_count}\" />
          <span class=\"muted\">リトライ回数</span>
        </div>
        <div class=\"row\">
          <label>追補出力名</label>
          <input type=\"text\" name=\"recovery_output_name\" value=\"{recovery_output_name}\" />
          <button type=\"submit\" {retry_disabled}>追補実行</button>
        </div>
      </form>
    </div>

    <div class=\"card\">
      <div class=\"row\" style=\"justify-content:space-between\">
        <strong>ログ</strong>
        <span class=\"muted\">{log_file}</span>
      </div>
      <pre id=\"log-view\">{log_tail}</pre>
    </div>
  </div>

  <script>
    const logViewEl = document.getElementById('log-view');
    const scrollStateKey = 'whisper_gui_scroll_state_v1';

    const saveScrollState = () => {
      const payload = {
        pageY: window.scrollY || 0,
        logTop: logViewEl ? logViewEl.scrollTop : 0,
      };
      try {
        sessionStorage.setItem(scrollStateKey, JSON.stringify(payload));
      } catch (_) {}
    };

    const restoreScrollState = () => {
      let payload = null;
      try {
        payload = JSON.parse(sessionStorage.getItem(scrollStateKey) || 'null');
      } catch (_) {
        payload = null;
      }
      if (!payload) return;
      window.scrollTo(0, Number(payload.pageY || 0));
      if (logViewEl) logViewEl.scrollTop = Number(payload.logTop || 0);
    };

    restoreScrollState();

    // Lightweight auto-refresh only while running.
    const running = {running_js};
    if (running) {
      setTimeout(() => {
        saveScrollState();
        location.reload();
      }, 2000);
    }

    // Auto-run preflight when new file is selected.
    const uploadEl = document.getElementById('audio_file');
    const preflightForm = document.getElementById('preflight-form');
    const preflightLoadingEl = document.getElementById('preflight-loading');
    const preflightSubmitEl = document.getElementById('preflight-submit');
    const preflightSelectedPresetEl = document.getElementById('preflight_selected_preset');
    const presetEl = document.getElementById('preset_select');
    const modeStrategyEl = document.getElementById('mode_strategy');
    const customModeEl = document.getElementById('custom_mode');
    const customJobsEl = document.getElementById('custom_jobs');
    const customSegmentEl = document.getElementById('custom_segment_time');
    const recoveryModeEl = document.getElementById('recovery_mode');
    const recoveryRangesEl = document.getElementById('recovery_ranges');
    const recoverySegmentTimeEl = document.getElementById('recovery_segment_time');
    const hasInput = {has_input_js};

    const showPreflightLoading = () => {
      if (preflightLoadingEl) preflightLoadingEl.style.display = 'block';
      if (preflightSubmitEl) preflightSubmitEl.disabled = true;
    };

    const syncModeInputs = () => {
      if (!modeStrategyEl || !customModeEl || !customJobsEl || !customSegmentEl) return;
      const custom = modeStrategyEl.value === 'custom';
      const segmented = custom && customModeEl.value === 'segmented';
      customModeEl.disabled = !custom;
      customJobsEl.disabled = !segmented;
      customSegmentEl.disabled = !segmented;
    };

    if (modeStrategyEl && customModeEl) {
      modeStrategyEl.addEventListener('change', syncModeInputs);
      customModeEl.addEventListener('change', syncModeInputs);
      syncModeInputs();
    }

    if (uploadEl && preflightForm) {
      preflightForm.addEventListener('submit', showPreflightLoading);
      uploadEl.addEventListener('change', () => {
        if (uploadEl.files && uploadEl.files.length > 0) {
          if (preflightSelectedPresetEl) preflightSelectedPresetEl.value = '';
          showPreflightLoading();
          preflightForm.submit();
        }
      });
    }

    if (presetEl && preflightForm) {
      presetEl.addEventListener('change', () => {
        if (!hasInput || running) return;
        if (preflightSelectedPresetEl) preflightSelectedPresetEl.value = presetEl.value;
        showPreflightLoading();
        if (typeof preflightForm.requestSubmit === 'function') preflightForm.requestSubmit();
        else preflightForm.submit();
      });
    }

    const syncRecoveryInputs = () => {
      if (!recoveryModeEl || !recoveryRangesEl || !recoverySegmentTimeEl) return;
      const useTimeRange = recoveryModeEl.value === 'time_range';
      recoveryRangesEl.disabled = !useTimeRange;
      recoverySegmentTimeEl.disabled = !useTimeRange;
    };

    if (recoveryModeEl) {
      recoveryModeEl.addEventListener('change', syncRecoveryInputs);
      syncRecoveryInputs();
    }
  </script>
</body>
</html>
"""


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server: "WhisperServer"

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def _redirect_error(self, message: str) -> None:
        self._redirect("/?error=" + quote(message, safe=""))

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_form(self) -> cgi.FieldStorage:
        return cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
            },
            keep_blank_values=True,
        )

    def _status_class(self, status: str) -> str:
        if status == "completed":
            return "ok"
        if status == "running":
            return "warn"
        if status in {"failed", "canceled"}:
            return "err"
        return "muted"

    def _render_preflight_html(self, st: dict[str, Any]) -> str:
        preflight_error = st.get("preflight_error") or ""
        preflight_result = st.get("preflight_result")

        if preflight_error:
            return (
                '<div class="row"><span class="warn">診断結果: スキップ</span></div>'
                '<div class="row"><span class="err">'
                + html.escape(preflight_error)
                + "</span></div>"
            )

        if not isinstance(preflight_result, dict):
            return '<div class="row"><span class="muted">診断結果: 未実行</span></div>'

        verdict = str(preflight_result.get("verdict") or "")
        reasons = preflight_result.get("reasons") if isinstance(preflight_result.get("reasons"), list) else []
        corrections = preflight_result.get("corrections") if isinstance(preflight_result.get("corrections"), list) else []
        in_data = preflight_result.get("input") if isinstance(preflight_result.get("input"), dict) else {}

        verdict_label = {
            "OK": "OK",
            "NEEDS_CORRECTION": "要補正 ⚠",
            "NOT_RECOMMENDED": "非推奨 ✕",
        }.get(verdict, "不明")
        verdict_class = {
            "OK": "ok",
            "NEEDS_CORRECTION": "warn",
            "NOT_RECOMMENDED": "err",
        }.get(verdict, "muted")

        container = str(in_data.get("container") or "unknown")
        codec = str(in_data.get("codec") or "unknown")
        duration = format_duration_ja(in_data.get("duration_sec"))
        mean_volume = format_db(in_data.get("mean_volume_db"))
        silence_ratio = format_percent(in_data.get("silence_ratio"))
        recommended = str(preflight_result.get("recommended_preset") or "-")
        preset_reason = str(preflight_result.get("preset_reason") or "")
        current_preset = str(st.get("preset") or "-")

        lines = [
            f'<div class="row"><span class="{verdict_class}"><strong>診断結果: {html.escape(verdict_label)}</strong></span></div>',
            f'<div class="row"><span class="muted">形式: {html.escape(container)} ({html.escape(codec)}) -> wav 変換予定</span></div>',
            f'<div class="row"><span class="muted">音声長: {html.escape(duration)} / 音量: {html.escape(mean_volume)} / 無音: {html.escape(silence_ratio)}</span></div>',
            f'<div class="row"><span class="muted">推薦プリセット: {html.escape(recommended)} ({html.escape(st.get("priority") or "balanced")})</span></div>',
        ]
        if current_preset in PRESETS and current_preset != recommended:
            lines.append(
                f'<div class="row"><span class="warn">現在選択プリセット: {html.escape(current_preset)}（手動）</span></div>'
            )
        elif current_preset in PRESETS:
            lines.append(f'<div class="row"><span class="muted">現在選択プリセット: {html.escape(current_preset)}</span></div>')

        if preset_reason:
            lines.append(f'<div class="row"><span class="muted">推薦理由: {html.escape(preset_reason)}</span></div>')

        if reasons:
            reason_text = " / ".join(str(r) for r in reasons)
            lines.append(f'<div class="row"><span class="muted">理由: {html.escape(reason_text)}</span></div>')

        if corrections:
            corr_text = ", ".join(str(c) for c in corrections)
            lines.append(f'<div class="row"><span class="muted">補正内容: {html.escape(corr_text)}</span></div>')

        mode_strategy = str(st.get("mode_strategy") or "auto")
        estimate_mode = "single-pass"
        estimate_jobs = 1
        if mode_strategy == "custom":
            custom_mode = str(st.get("custom_mode") or "segmented")
            if custom_mode == "segmented":
                jobs = parse_int_clamped(st.get("custom_jobs"), 2, 1, 8)
                seg_t = parse_int_clamped(st.get("custom_segment_time"), 60, 30, 600)
                estimate_mode = "segmented"
                estimate_jobs = jobs
                lines.append(
                    f'<div class="row"><span class="muted">実行モード: カスタム分割並列 (jobs={jobs}, {seg_t}秒分割)</span></div>'
                )
            else:
                estimate_mode = "single-pass"
                estimate_jobs = 1
                lines.append('<div class="row"><span class="muted">実行モード: カスタム単一パス</span></div>')
        else:
            auto_mode = recommend_auto_mode(
                in_data.get("duration_sec"),
                str(st.get("preset") or "x4"),
                str(st.get("priority") or "balanced"),
                os.cpu_count() or 4,
            )
            if str(auto_mode.get("mode")) == "segmented":
                estimate_mode = "segmented"
                estimate_jobs = parse_int_clamped(auto_mode.get("jobs"), 2, 1, 8)
                lines.append(
                    '<div class="row"><span class="muted">実行モード(自動): 分割並列 '
                    + html.escape(f"(jobs={auto_mode.get('jobs')}, {auto_mode.get('segment_time')}秒分割)")
                    + "</span></div>"
                )
            else:
                estimate_mode = "single-pass"
                estimate_jobs = 1
                lines.append('<div class="row"><span class="muted">実行モード(自動): 単一パス</span></div>')

        estimate = estimate_runtime_window_sec(
            in_data.get("duration_sec"),
            str(st.get("preset") or "x4"),
            estimate_mode,
            estimate_jobs,
            bool(st.get("use_vad", False)),
            bool(st.get("force_cpu_mode", False)),
        )
        if estimate is not None:
            center, low, high = estimate
            lines.append(
                '<div class="row"><span class="muted">推定処理時間(目安): 約'
                + html.escape(format_duration_ja(center))
                + "（"
                + html.escape(format_duration_ja(low))
                + "〜"
                + html.escape(format_duration_ja(high))
                + "）</span></div>"
            )

        if verdict == "NOT_RECOMMENDED":
            lines.append('<div class="row"><span class="err">次のアクション: 別形式 (m4a/mp3/wav) で保存し直してください。</span></div>')

        return "".join(lines)

    def _render_run_info_html(self, st: dict[str, Any]) -> str:
        preflight_result = st.get("preflight_result") if isinstance(st.get("preflight_result"), dict) else {}
        in_data = preflight_result.get("input") if isinstance(preflight_result.get("input"), dict) else {}
        applied = st.get("applied_corrections") if isinstance(st.get("applied_corrections"), list) else []

        before_text = (
            f"{in_data.get('container', 'unknown')} ({in_data.get('codec', 'unknown')}) / "
            f"{format_duration_ja(in_data.get('duration_sec'))} / "
            f"{in_data.get('sample_rate', 'unknown')}Hz / "
            f"{in_data.get('channels', 'unknown')}ch"
        )

        corr_text = ", ".join(str(c) for c in applied) if applied else "なし"
        mode_text = str(st.get("resolved_mode") or "-")
        mode_reason = str(st.get("resolved_mode_reason") or "")
        vad_effective = bool(st.get("use_vad_effective", st.get("use_vad", False)))
        vad_requested = bool(st.get("use_vad", False))
        vad_model_path = str(st.get("vad_model_path") or "")
        force_cpu_mode = bool(st.get("force_cpu_mode", False))
        vad_text = "ON" if vad_effective else "OFF"
        vad_note = ""
        if vad_requested and not vad_effective:
            vad_note = "（単一パス時のみ有効）"
        if mode_text == "segmented":
            mode_text = (
                "分割並列 "
                + f"(jobs={parse_int_clamped(st.get('resolved_jobs'), 1, 1, 8)}, "
                + f"{parse_int_clamped(st.get('resolved_segment_time'), 240, 30, 600)}秒分割)"
            )
        elif mode_text == "single-pass":
            mode_text = "単一パス"
        elif mode_text == "recovery":
            mode_text = "失敗区間追補"
        estimate = estimate_runtime_window_sec(
            in_data.get("duration_sec"),
            str(st.get("preset") or "x4"),
            str(st.get("resolved_mode") or "single-pass"),
            parse_int_clamped(st.get("resolved_jobs"), 1, 1, 8),
            vad_effective,
            force_cpu_mode,
        )
        eta_html = ""
        if estimate is not None:
            center, low, high = estimate
            eta_html = (
                '<div class="row"><span class="muted">推定処理時間(目安): 約'
                + html.escape(format_duration_ja(center))
                + "（"
                + html.escape(format_duration_ja(low))
                + "〜"
                + html.escape(format_duration_ja(high))
                + "）</span></div>"
            )
        return (
            '<div class="row"><span class="muted">補正一覧: '
            + html.escape(corr_text)
            + "</span></div>"
            '<div class="row"><span class="muted">実行モード: '
            + html.escape(mode_text)
            + "</span></div>"
            '<div class="row"><span class="muted">モード理由: '
            + html.escape(mode_reason or "-")
            + "</span></div>"
            '<div class="row"><span class="muted">VAD: '
            + html.escape(vad_text + vad_note)
            + "</span></div>"
            '<div class="row"><span class="muted">CPU固定モード: '
            + html.escape("ON" if force_cpu_mode else "OFF")
            + "</span></div>"
            + (
                '<div class="row"><span class="muted">VADモデル: '
                + html.escape(vad_model_path)
                + "</span></div>"
                if vad_model_path
                else ""
            )
            + eta_html
            +
            '<div class="row"><span class="muted">処理前入力: '
            + html.escape(before_text)
            + "</span></div>"
            '<div class="row"><span class="muted">処理後出力: 16kHz mono WAV -> テキスト</span></div>'
        )

    def _render_recovery_status_html(self, st: dict[str, Any], partial_output: str) -> str:
        if not partial_output:
            return '<div class="row"><span class="muted">対象: なし</span></div>'

        lines = []
        lines.append(f'<div class="row"><span class="muted">対象部分結果: {html.escape(partial_output)}</span></div>')

        partial_ok = os.path.isfile(partial_output)
        failed_file = f"{partial_output}.failed_segments.txt"
        failed_ok = os.path.isfile(failed_file)
        meta = read_recovery_meta(partial_output)
        seg_meta = parse_int_clamped(meta.get("SEGMENT_TIME"), 0, 0, 3600)

        if partial_ok:
            lines.append('<div class="row"><span class="ok">部分結果ファイル: OK</span></div>')
        else:
            lines.append('<div class="row"><span class="warn">部分結果ファイル: なし（先に通常実行の出力を指定）</span></div>')

        if failed_ok:
            lines.append('<div class="row"><span class="ok">失敗一覧: OK（`*.failed_segments.txt`）</span></div>')
        else:
            lines.append('<div class="row"><span class="muted">失敗一覧: 未検出（時刻範囲指定モードなら実行可能）</span></div>')

        if seg_meta > 0:
            lines.append(
                '<div class="row"><span class="muted">分割秒メタ: '
                + html.escape(str(seg_meta))
                + "秒（自動読込可）</span></div>"
            )
        else:
            lines.append('<div class="row"><span class="muted">分割秒メタ: なし（時刻範囲指定時は手入力推奨）</span></div>')

        return "".join(lines)

    def _render_home(self, error: str = "") -> bytes:
        st = self.server.state.snapshot()

        deps = st.get("missing_dependencies", [])
        deps_html = ""
        if deps:
            deps_html = (
                '<div class="row"><span class="err">依存不足: '
                + html.escape(", ".join(deps))
                + "</span></div>"
            )

        error_html = ""
        if error:
            error_html = '<div class="row"><span class="err">' + html.escape(error) + "</span></div>"

        failure_html = ""
        if st.get("status") == "failed" and st.get("failure_reason"):
            failure_html = (
                '<div class="row"><span class="err">失敗理由: '
                + html.escape(st.get("failure_reason") or "")
                + "</span></div>"
                '<div class="row"><span class="warn">次のアクション: '
                + html.escape(st.get("failure_action") or "")
                + "</span></div>"
            )

        verdict = ""
        preflight_result = st.get("preflight_result")
        if isinstance(preflight_result, dict):
            verdict = str(preflight_result.get("verdict") or "")

        has_input = bool(st.get("input_file")) and os.path.isfile(st.get("input_file") or "")
        preflight_ready = isinstance(preflight_result, dict) or bool(st.get("preflight_skipped"))
        current_input_html = '<div class="row"><span class="muted">現在セット中: なし</span></div>'
        if has_input:
            current_name = str(st.get("input_name") or os.path.basename(str(st.get("input_file") or "")) or "audio")
            current_path = str(st.get("input_file") or "")
            current_input_html = (
                '<div class="row"><span class="ok">現在セット中: '
                + html.escape(current_name)
                + '</span></div><div class="row"><span class="muted">（ファイル選択欄が空表示でも、このファイルで開始できます）<br>'
                + html.escape(current_path)
                + "</span></div>"
            )

        start_disabled = ""
        if st.get("running"):
            start_disabled = "disabled"
        elif not has_input:
            start_disabled = "disabled"
        elif not preflight_ready:
            start_disabled = "disabled"
        elif verdict == "NOT_RECOMMENDED":
            start_disabled = "disabled"

        recovery_partial_default = os.path.basename(st.get("output_file") or "") or "transcription_result.txt"
        recovery_partial = str(st.get("recovery_partial_output") or recovery_partial_default).strip()
        recovery_partial_path = resolve_output_path(recovery_partial, recovery_partial_default)
        recovery_output_default = default_recovered_output_name(recovery_partial_path)
        recovery_output = str(st.get("recovery_output_name") or recovery_output_default).strip()
        recovery_mode = str(st.get("recovery_mode") or "failed")
        if recovery_mode not in RECOVERY_MODES:
            recovery_mode = "failed"
        retry_disabled = "disabled" if st.get("running") or not has_input else ""

        values = {
            "status": html.escape(st.get("status", "idle")),
            "status_class": self._status_class(st.get("status", "idle")),
            "started_at": html.escape(st.get("started_at") or "-"),
            "finished_at": html.escape(st.get("finished_at") or "-"),
            "ui_message": html.escape(st.get("ui_message") or "-"),
            "deps_html": deps_html,
            "error_html": error_html,
            "failure_html": failure_html,
            "preflight_html": self._render_preflight_html(st),
            "run_info_html": self._render_run_info_html(st),
            "recovery_status_html": self._render_recovery_status_html(st, recovery_partial_path),
            "sel_x1": "selected" if st.get("preset") == "x1" else "",
            "sel_x4": "selected" if st.get("preset") == "x4" else "",
            "sel_x8": "selected" if st.get("preset") == "x8" else "",
            "sel_x16": "selected" if st.get("preset") == "x16" else "",
            "mode_auto": "selected" if st.get("mode_strategy") == "auto" else "",
            "mode_custom": "selected" if st.get("mode_strategy") == "custom" else "",
            "custom_single": "selected" if st.get("custom_mode") == "single-pass" else "",
            "custom_segmented": "selected" if st.get("custom_mode") == "segmented" else "",
            "custom_jobs": html.escape(str(st.get("custom_jobs") or 2)),
            "custom_segment_time": html.escape(str(st.get("custom_segment_time") or 60)),
            "prio_accuracy": "selected" if st.get("priority") == "accuracy" else "",
            "prio_balanced": "selected" if st.get("priority") == "balanced" else "",
            "prio_speed": "selected" if st.get("priority") == "speed" else "",
            "use_vad_checked": "checked" if st.get("use_vad", False) else "",
            "auto_correction_checked": "checked" if st.get("auto_correction", True) else "",
            "output_name": html.escape(st.get("output_name") or "transcription_result.txt"),
            "preflight_disabled": "disabled" if st.get("running") else "",
            "start_disabled": start_disabled,
            "cancel_disabled": "" if st.get("running") else "disabled",
            "output_file": html.escape(st.get("output_file") or "-"),
            "input_file": html.escape(st.get("input_file") or "-"),
            "current_input_html": current_input_html,
            "recovery_partial_output": html.escape(recovery_partial),
            "recovery_output_name": html.escape(recovery_output),
            "recovery_mode_failed": "selected" if recovery_mode == "failed" else "",
            "recovery_mode_time_range": "selected" if recovery_mode == "time_range" else "",
            "recovery_ranges": html.escape(str(st.get("recovery_ranges") or "")),
            "recovery_segment_time": html.escape(str(st.get("recovery_segment_time") or "")),
            "recovery_retry_count": html.escape(str(st.get("recovery_retry_count") or 1)),
            "recovery_preset_keep": "selected" if st.get("recovery_preset") == "keep" else "",
            "recovery_preset_x1": "selected" if st.get("recovery_preset") == "x1" else "",
            "recovery_preset_x4": "selected" if st.get("recovery_preset") == "x4" else "",
            "recovery_preset_x8": "selected" if st.get("recovery_preset") == "x8" else "",
            "recovery_preset_x16": "selected" if st.get("recovery_preset") == "x16" else "",
            "retry_disabled": retry_disabled,
            "log_file": html.escape(st.get("log_file") or "-"),
            "log_tail": html.escape(st.get("log_tail_ja") or st.get("log_tail") or ""),
            "has_input_js": "true" if has_input else "false",
            "running_js": "true" if st.get("running") else "false",
        }

        page = HTML_TEMPLATE
        for key, value in values.items():
            page = page.replace("{" + key + "}", value)
        return page.encode("utf-8")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)

        if path == "/":
            error = q.get("error", [""])[0]
            body = self._render_home(error=error)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/state":
            self._send_json(self.server.state.snapshot())
            return

        if path == "/download":
            st = self.server.state.snapshot()
            out = st.get("output_file") or ""
            if not out or not os.path.isfile(out):
                self._redirect_error("出力ファイルがありません")
                return

            with open(out, "rb") as f:
                body = f.read()

            fname = os.path.basename(out) or "transcription_result.txt"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/reveal-output":
            st = self.server.state.snapshot()
            out = st.get("output_file") or ""
            if out and os.path.exists(out):
                subprocess.run(["open", "-R", out], check=False)
            self._redirect("/")
            return

        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/preflight":
            form = self._read_form()
            ok, msg = self.server.state.preflight_with_form(form)
            if not ok:
                self._redirect_error(msg)
                return
            self._redirect("/")
            return

        if self.path == "/start":
            form = self._read_form()
            ok, msg = self.server.state.start_with_form(form)
            if not ok:
                self._redirect_error(msg)
                return
            self._redirect("/")
            return

        if self.path == "/cancel":
            ok, msg = self.server.state.cancel()
            if not ok:
                self._redirect_error(msg)
                return
            self._redirect("/")
            return

        if self.path == "/retry-failed":
            form = self._read_form()
            ok, msg = self.server.state.retry_failed_with_form(form)
            if not ok:
                self._redirect_error(msg)
                return
            self._redirect("/")
            return

        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:
        return


class WhisperServer(ThreadingHTTPServer):
    def __init__(self, addr):
        self.state = AppState()
        super().__init__(addr, Handler)


def start_server(host: str, start_port: int) -> tuple[WhisperServer, int]:
    last_exc: Optional[Exception] = None
    for p in range(start_port, start_port + 50):
        try:
            srv = WhisperServer((host, p))
            return srv, p
        except OSError as e:
            last_exc = e
    raise RuntimeError(f"failed to bind server: {last_exc}")


def open_url(url: str) -> None:
    for cmd in (["open", url], ["open", "-a", "Google Chrome", url], ["open", "-a", "Safari", url]):
        rc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode
        if rc == 0:
            return


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8876)
    ap.add_argument("--url-file", default="")
    ap.add_argument("--open-browser", action="store_true")
    args = ap.parse_args()

    server, port = start_server(args.host, args.port)
    url = f"http://{args.host}:{port}/"

    if args.url_file:
        try:
            with open(args.url_file, "w", encoding="utf-8") as f:
                f.write(url)
        except OSError:
            pass

    if args.open_browser:
        open_url(url)

    try:
        server.serve_forever(poll_interval=0.8)
    except KeyboardInterrupt:
        pass
    finally:
        with server.state.lock:
            if server.state.process and server.state.running:
                try:
                    pgid = os.getpgid(server.state.process.pid)
                    os.killpg(pgid, signal.SIGTERM)
                except Exception:
                    pass
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
