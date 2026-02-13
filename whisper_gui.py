#!/usr/bin/env python3
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext


class WhisperApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Whisper 文字起こしワークフロー")
        self.root.geometry("820x620")
        self.root.minsize(760, 520)

        self.script_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "transcribe_workflow.sh",
        )

        self.process = None
        self.is_running = False
        self.cancel_requested = False
        self.active_output_file = None

        self.preset_var = tk.StringVar(value="x1")
        self.parallel_mode_var = tk.BooleanVar(value=False)
        self.jobs_var = tk.StringVar(value="2")
        self.auto_suffix_var = tk.BooleanVar(value=True)
        self.max_parallel_jobs = 4

        self.preset_accuracy = {
            "x1": "精度: 最高（large-v3）",
            "x4": "精度: 高（medium）",
            "x8": "精度: 中（small）",
            "x16": "精度: 低〜中（tiny）",
        }

        self.create_widgets()
        self.check_dependencies()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(120, self.activate_window)

    def create_widgets(self):
        input_frame = tk.Frame(self.root, pady=8)
        input_frame.pack(fill=tk.X, padx=10)

        tk.Label(input_frame, text="音声ファイル:").pack(side=tk.LEFT)
        self.input_entry = tk.Entry(input_frame)
        self.input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.input_btn = tk.Button(input_frame, text="選択...", command=self.select_input)
        self.input_btn.pack(side=tk.LEFT)

        output_frame = tk.Frame(self.root, pady=6)
        output_frame.pack(fill=tk.X, padx=10)

        tk.Label(output_frame, text="出力ファイル:").pack(side=tk.LEFT)
        self.output_entry = tk.Entry(output_frame)
        self.output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.output_entry.insert(0, os.path.join(os.getcwd(), "transcription_result.txt"))
        self.output_btn = tk.Button(output_frame, text="名前を付けて保存...", command=self.select_output)
        self.output_btn.pack(side=tk.LEFT)

        ctrl_frame = tk.Frame(self.root, pady=8)
        ctrl_frame.pack(fill=tk.X, padx=10)

        self.start_btn = tk.Button(ctrl_frame, text="文字起こし開始", command=self.start_transcription)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.cancel_btn = tk.Button(ctrl_frame, text="キャンセル", command=self.cancel_transcription, state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT)

        tk.Label(ctrl_frame, text="速度:").pack(side=tk.LEFT, padx=(14, 4))
        speed_frame = tk.Frame(ctrl_frame)
        speed_frame.pack(side=tk.LEFT)
        self.preset_radios = []
        for preset in ("x1", "x4", "x8", "x16"):
            rb = tk.Radiobutton(
                speed_frame,
                text=preset,
                value=preset,
                variable=self.preset_var,
                indicatoron=False,
                width=4,
                command=self.on_preset_changed,
            )
            rb.pack(side=tk.LEFT, padx=(0, 2))
            self.preset_radios.append(rb)

        self.status_lbl = tk.Label(ctrl_frame, text="準備完了", fg="gray")
        self.status_lbl.pack(side=tk.LEFT, padx=14)

        self.accuracy_lbl = tk.Label(self.root, text=self.preset_accuracy[self.preset_var.get()], fg="gray")
        self.accuracy_lbl.pack(anchor=tk.W, padx=10)

        advanced = tk.LabelFrame(self.root, text="詳細設定", padx=8, pady=6)
        advanced.pack(fill=tk.X, padx=10, pady=(6, 4))

        self.parallel_chk = tk.Checkbutton(
            advanced,
            text="分割並列モードを有効化",
            variable=self.parallel_mode_var,
            command=self.on_parallel_mode_changed,
        )
        self.parallel_chk.pack(anchor=tk.W)

        jobs_row = tk.Frame(advanced)
        jobs_row.pack(anchor=tk.W, pady=(4, 0))
        tk.Label(jobs_row, text="並列ジョブ数:").pack(side=tk.LEFT)
        self.jobs_spin = tk.Spinbox(
            jobs_row,
            from_=1,
            to=self.max_parallel_jobs,
            textvariable=self.jobs_var,
            width=4,
            state=tk.DISABLED,
        )
        self.jobs_spin.pack(side=tk.LEFT, padx=(6, 6))
        tk.Label(jobs_row, text=f"(1-{self.max_parallel_jobs})", fg="gray").pack(side=tk.LEFT)

        self.auto_suffix_chk = tk.Checkbutton(
            advanced,
            text="出力ファイル名に設定サフィックスを自動付与",
            variable=self.auto_suffix_var,
        )
        self.auto_suffix_chk.pack(anchor=tk.W, pady=(4, 0))

        tk.Label(self.root, text="ログ出力:").pack(anchor=tk.W, padx=10)
        self.log_area = scrolledtext.ScrolledText(self.root, height=16, state=tk.DISABLED, font=("Monaco", 10))
        self.log_area.pack(fill=tk.BOTH, expand=True, padx=10, pady=(4, 10))

    def check_dependencies(self):
        missing = []
        if not shutil.which("ffmpeg"):
            missing.append("ffmpeg")
        if not shutil.which("whisper-cli"):
            missing.append("whisper-cli")
        if not os.path.isfile(self.script_path):
            missing.append("transcribe_workflow.sh")

        if missing:
            self.status_lbl.config(text="依存関係不足", fg="red")
            self.start_btn.config(state=tk.DISABLED)
            messagebox.showerror(
                "セットアップが必要です",
                "不足している依存関係/ファイル:\n\n" + "\n".join(f"- {item}" for item in missing),
                parent=self.root,
            )

    def activate_window(self):
        # Finder/app-launch can leave the window visible but not key.
        # Bring it to front and set initial focus so first click works.
        try:
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(80, lambda: self.root.attributes("-topmost", False))
            self.root.focus_force()
            self.start_btn.focus_set()
        except tk.TclError:
            pass

    def log(self, message):
        self.log_area.config(state=tk.NORMAL)
        self.log_area.insert(tk.END, message)
        self.log_area.see(tk.END)
        self.log_area.config(state=tk.DISABLED)

    def on_preset_changed(self, _event=None):
        self.accuracy_lbl.config(text=self.preset_accuracy.get(self.preset_var.get(), "精度: 不明"))

    def on_parallel_mode_changed(self):
        self.jobs_spin.config(state=tk.NORMAL if self.parallel_mode_var.get() else tk.DISABLED)

    def select_input(self):
        path = filedialog.askopenfilename(
            filetypes=[
                ("音声ファイル", "*.m4a *.mp3 *.wav *.mp4 *.aac *.flac *.ogg *.webm"),
                ("すべてのファイル", "*.*"),
            ],
            parent=self.root,
        )
        if path:
            self.input_entry.delete(0, tk.END)
            self.input_entry.insert(0, path)

    def select_output(self):
        current = self.output_entry.get()
        initial_dir = os.path.dirname(current) if current else os.getcwd()
        initial_file = os.path.basename(current) if current else "transcription_result.txt"
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            initialdir=initial_dir,
            initialfile=initial_file,
            filetypes=[("テキストファイル", "*.txt"), ("すべてのファイル", "*.*")],
            parent=self.root,
        )
        if path:
            self.output_entry.delete(0, tk.END)
            self.output_entry.insert(0, path)

    def default_model_for_preset(self, preset):
        base = os.path.expanduser("~/.cache/whisper-cpp")
        model_map = {
            "x1": "ggml-large-v3.bin",
            "x4": "ggml-medium.bin",
            "x8": "ggml-small.bin",
            "x16": "ggml-tiny.bin",
        }
        name = model_map.get(preset)
        if not name:
            return None
        return os.path.join(base, name)

    def build_output_file_with_suffix(self, output_file, preset, parallel_enabled, jobs):
        root, ext = os.path.splitext(output_file)
        mode_tag = "parallel" if parallel_enabled else "single"
        suffix = f"{preset}_{mode_tag}"
        if parallel_enabled:
            suffix += f"_j{jobs}"

        candidate = f"{root}_{suffix}{ext}"
        counter = 2
        while os.path.exists(candidate):
            candidate = f"{root}_{suffix}_{counter}{ext}"
            counter += 1
        return candidate

    def set_processing_state(self, running):
        self.is_running = running

        if running:
            self.start_btn.config(state=tk.DISABLED)
            self.cancel_btn.config(state=tk.NORMAL)
            self.input_entry.config(state=tk.DISABLED)
            self.output_entry.config(state=tk.DISABLED)
            self.input_btn.config(state=tk.DISABLED)
            self.output_btn.config(state=tk.DISABLED)
            for rb in self.preset_radios:
                rb.config(state=tk.DISABLED)
            self.parallel_chk.config(state=tk.DISABLED)
            self.jobs_spin.config(state=tk.DISABLED)
            self.auto_suffix_chk.config(state=tk.DISABLED)
            self.status_lbl.config(text="実行中...", fg="blue")
        else:
            self.start_btn.config(state=tk.NORMAL)
            self.cancel_btn.config(state=tk.DISABLED)
            self.input_entry.config(state=tk.NORMAL)
            self.output_entry.config(state=tk.NORMAL)
            self.input_btn.config(state=tk.NORMAL)
            self.output_btn.config(state=tk.NORMAL)
            for rb in self.preset_radios:
                rb.config(state=tk.NORMAL)
            self.parallel_chk.config(state=tk.NORMAL)
            self.auto_suffix_chk.config(state=tk.NORMAL)
            self.on_parallel_mode_changed()
            self.status_lbl.config(text="準備完了", fg="gray")

    def start_transcription(self):
        if self.is_running:
            return

        input_file = self.input_entry.get().strip()
        output_file = self.output_entry.get().strip()

        if not input_file or not os.path.exists(input_file):
            messagebox.showerror("入力エラー", "有効な音声ファイルを選択してください。", parent=self.root)
            return

        if not output_file:
            messagebox.showerror("出力エラー", "出力ファイルを指定してください。", parent=self.root)
            return

        env = os.environ.copy()
        preset = self.preset_var.get()
        model_path = self.default_model_for_preset(preset)

        if "WHISPER_MODEL" not in env and model_path and not os.path.isfile(model_path):
            script_dir = os.path.dirname(os.path.abspath(__file__))
            installer_path = os.path.join(script_dir, "install_models.sh")
            install_cmd = f'cd "{script_dir}" && ./install_models.sh {preset}'
            if not os.path.isfile(installer_path):
                model_name = os.path.basename(model_path)
                install_cmd = (
                    f"curl -L -o {model_path} "
                    f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{model_name}"
                )
            messagebox.showerror(
                "モデルが見つかりません",
                f"選択したプリセット '{preset}' には次のモデルが必要です:\n{model_path}\n\nインストールコマンド:\n{install_cmd}",
                parent=self.root,
            )
            return

        parallel_enabled = self.parallel_mode_var.get()
        jobs = 1
        if parallel_enabled:
            try:
                jobs = int(self.jobs_var.get())
            except ValueError:
                messagebox.showerror("ジョブ数エラー", "並列ジョブ数は整数で指定してください。", parent=self.root)
                return

            if jobs < 1 or jobs > self.max_parallel_jobs:
                messagebox.showerror(
                    "ジョブ数エラー",
                    f"並列ジョブ数は 1 から {self.max_parallel_jobs} の範囲で指定してください。",
                    parent=self.root,
                )
                return

            if preset == "x1":
                proceed = messagebox.askyesno(
                    "x1 並列実行の警告",
                    "x1（large-v3）を分割並列で使うと、メモリ消費が増え、遅くなる場合があります。続行しますか？",
                    parent=self.root,
                )
                if not proceed:
                    return

        run_output = output_file
        if self.auto_suffix_var.get():
            run_output = self.build_output_file_with_suffix(output_file, preset, parallel_enabled, jobs)
        self.active_output_file = run_output

        if parallel_enabled:
            env["WHISPER_PRESET"] = "custom"
            if "WHISPER_MODEL" not in env and model_path:
                env["WHISPER_MODEL"] = model_path
            env["WHISPER_JOBS"] = str(jobs)
            if "WHISPER_THREADS" not in env:
                cpu_threads = os.cpu_count() or 4
                env["WHISPER_THREADS"] = str(max(1, cpu_threads // jobs))
        else:
            env["WHISPER_PRESET"] = preset

        self.log_area.config(state=tk.NORMAL)
        self.log_area.delete("1.0", tk.END)
        self.log_area.config(state=tk.DISABLED)

        self.log("=== Whisper 文字起こし ===\n")
        self.log(f"入力: {input_file}\n")
        self.log(f"出力: {run_output}\n")
        self.log(f"プリセット: {preset}\n")
        self.log(f"モード: {'分割並列' if parallel_enabled else 'シングルパス'}\n")
        if parallel_enabled:
            self.log(f"ジョブ数: {jobs}\n")
            self.log(f"ジョブあたりスレッド数: {env.get('WHISPER_THREADS', 'auto')}\n")
        self.log("-----------------------------\n")

        self.cancel_requested = False
        self.set_processing_state(True)

        threading.Thread(
            target=self.run_process,
            args=(input_file, run_output, env),
            daemon=True,
        ).start()

    def run_process(self, input_file, output_file, env):
        try:
            self.process = subprocess.Popen(
                [self.script_path, input_file, output_file],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
                env=env,
            )

            if self.process.stdout is not None:
                for line in self.process.stdout:
                    self.root.after(0, self.log, line)

            retcode = self.process.wait()
            self.root.after(0, self.on_process_finished, retcode)
        except Exception as exc:
            self.root.after(0, self.log, f"\nエラー: {exc}\n")
            self.root.after(0, self.on_process_finished, -1)

    def on_process_finished(self, retcode):
        self.process = None
        self.set_processing_state(False)

        if self.cancel_requested:
            self.status_lbl.config(text="キャンセル済み", fg="orange")
            self.log("\n--- 処理をキャンセルしました ---\n")
        elif retcode == 0:
            self.status_lbl.config(text="完了", fg="green")
            self.log("\n--- 処理が完了しました ---\n")
            output_path = self.active_output_file or "（不明）"
            messagebox.showinfo("成功", f"文字起こしが完了しました。\n\n出力:\n{output_path}", parent=self.root)
        else:
            self.status_lbl.config(text="失敗", fg="red")
            self.log(f"\n--- 処理に失敗しました（コード: {retcode}）---\n")
            messagebox.showerror("エラー", "文字起こしに失敗しました。ログを確認してください。", parent=self.root)

        self.active_output_file = None
        self.cancel_requested = False

    def cancel_transcription(self):
        if not (self.process and self.is_running):
            return

        self.cancel_requested = True
        self.log("\nキャンセルシグナルを送信します...\n")
        try:
            pgid = os.getpgid(self.process.pid)
            os.killpg(pgid, signal.SIGTERM)

            deadline = time.time() + 5
            while time.time() < deadline:
                if self.process.poll() is not None:
                    return
                time.sleep(0.1)

            if self.process.poll() is None:
                self.log("プロセスが終了しないため SIGKILL を送信します...\n")
                os.killpg(pgid, signal.SIGKILL)
        except Exception as exc:
            self.log(f"キャンセルエラー: {exc}\n")

    def on_close(self):
        if self.is_running:
            should_stop = messagebox.askyesno(
                "終了確認",
                "文字起こしを実行中です。停止して終了しますか？",
                parent=self.root,
            )
            if should_stop:
                self.cancel_transcription()
                self.wait_for_shutdown_then_close()
            return

        self.root.destroy()

    def wait_for_shutdown_then_close(self):
        if self.process and self.process.poll() is None:
            self.root.after(200, self.wait_for_shutdown_then_close)
        else:
            self.root.destroy()


def main():
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("エラー: tkinter がインストールされていません。brew install python-tk を実行してください。")
        sys.exit(1)

    root = tk.Tk()
    WhisperApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
