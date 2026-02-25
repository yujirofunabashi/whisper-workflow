# Whisper 文字起こしワークフロー

whisper-cpp を使った音声文字起こしの手順書。

## 環境

- macOS (Apple Silicon)
- whisper-cpp (Homebrew)
- ffmpeg

## セットアップ

### 1. whisper-cpp のインストール

```bash
brew install whisper-cpp
```

### 2. モデルのダウンロード

推奨: ワークフロー同梱スクリプトで必要モデルをまとめて取得します。

```bash
cd ~/whisper-workflow
./install_models.sh
```

個別に入れる場合:

```bash
mkdir -p ~/.cache/whisper-cpp

# large-v3 モデル（推奨、約3GB）
curl -L -o ~/.cache/whisper-cpp/ggml-large-v3.bin \
  "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin"
```

他のモデルサイズ:

- `ggml-medium.bin` (~1.5GB) - バランス型
- `ggml-small.bin` (~500MB) - 軽量だが精度低め

## 使い方

### 自動文字起こし（一括実行）

作成した `transcribe_workflow.sh` を使用することで、変換から文字起こしまでを一括で行えます。

```bash
# 基本的な使い方
./transcribe_workflow.sh <音声ファイル> [出力ファイル名]

# 例
./transcribe_workflow.sh interview.m4a result.txt
```

このスクリプトは以下の処理を自動で行います：

1. 音声ファイルを wav (16kHz) に変換
2. （必要時のみ）`WHISPER_PREFLIGHT_NORMALIZE=1` で `loudnorm` を適用
3. Whisper で文字起こし（プリセット `x1/x4/x8/x16` は single-pass）
4. 結果を出力
5. 中間ファイルを自動削除

### 速度プリセット（x1 / x4 / x8 / x16）

`WHISPER_PRESET` で速度プリセットを選べます（デフォルト `x1`）。

```bash
WHISPER_PRESET=x4 ./transcribe_workflow.sh <音声ファイル> [出力ファイル名]
```

精度の目安:

- `x1`: 最高精度（`ggml-large-v3.bin`）
- `x4`: 高精度（`ggml-medium.bin`）
- `x8`: 中精度（`ggml-small.bin`）
- `x16`: 低〜中精度（`ggml-tiny.bin`）

必要モデルの例:

```bash
./install_models.sh x4 x8 x16
```

必要に応じて以下を上書きできます。

- `WHISPER_MODEL`（モデルファイルを手動指定）
- `WHISPER_THREADS`（スレッド数）
- `WHISPER_LANGUAGE`（言語、例: `ja`）
- `WHISPER_PREFLIGHT_NORMALIZE`（`1` のとき変換時に loudnorm を適用）
- `WHISPER_MODE`（`single-pass` / `segmented` を強制）
- `WHISPER_JOBS`（分割並列時の並列数）
- `WHISPER_SEGMENT_TIME`（分割秒数）
- `WHISPER_FORCE_CPU`（`1` のとき GPU を使わず `-ng` で実行）
- `WHISPER_THREADS` 未指定時:
  - `single-pass`: CPUコア数を使用
  - `segmented`: `CPUコア数 / JOBS` を各ジョブに自動割当
- `WHISPER_USE_VAD`（`1` のとき VAD 有効）
- `WHISPER_VAD_ALLOW_SEGMENTED`（`1` で分割並列でもVADを強制。既定は安定性優先で無効化）
- `WHISPER_VAD_MODEL`（VADモデルのパス。例: `~/.cache/whisper-cpp/ggml-silero-v6.2.0.bin`）
- `WHISPER_VAD_CPU_ONLY`（既定 `1`。VAD有効時は `-ng` でCPU実行し安定性優先。`0` でGPU試行）

### Preflight 診断（CLI）

アップロード前の入力診断を単体で実行できます。

```bash
./preflight.sh <音声ファイル> [accuracy|balanced|speed]
```

出力は JSON です。主な項目:

- `verdict`（`OK` / `NEEDS_CORRECTION` / `NOT_RECOMMENDED`）
- `reasons`
- `corrections`（例: `volume_normalize`）
- `input`（container/codec/duration/mean_volume/silence_ratio/convertible など）
- `recommended_preset`

注記:
- `mean_volume_db` / `silence_ratio` は先頭サンプル（既定60秒）から算出します。
- サンプル長は `WHISPER_PREFLIGHT_SCAN_SEC` で変更できます（1〜300秒）。

### Web GUI で実行

推奨起動:

```bash
./WhisperDialog.command
```

直接起動:

```bash
python3 whisper_gui_web.py
```

機能:

1. 音声アップロード時の自動 Preflight 診断
2. 診断結果カード表示（OK / 要補正 / 非推奨）
3. 優先度セレクタ（精度優先 / バランス / 速度優先）
4. 推薦プリセットの自動設定（手動上書き可）
5. 実行モード選択（`最適自動選択` / `カスタム指定`）
6. カスタム指定時に `単一パス` / `分割並列` を選択可能
7. 分割並列の `jobs` / `segment秒` をカスタム可能
8. 自動補正チェック（必要時 `loudnorm` を適用）
9. 失敗理由と次アクションの表示
10. 実行中ログ表示（10秒ごとに、経過時間・推定残り時間を追記）
11. Cancel ボタンで処理中断（子プロセスも停止）
12. 診断カードと実行情報に推定処理時間（目安レンジ）を表示
13. 一度セットした入力ファイルを保持し、優先度/プリセット変更時も再選択なしで再診断可能
14. 分割並列で一部セグメント失敗時も、成功分を結合した部分結果を保存（`*.failed_segments.txt` を併記）
15. GUI から「失敗区間の追補実行」が可能（失敗一覧モード / 時刻範囲モード）
16. 時刻範囲モードでは `00:10:00-00:12:30, 00:31:00` のように指定すると、対象セグメントを自動算出して追補

部分結果からの追補（失敗区間のみ再実行）:

```bash
./retry_failed_segments.sh <元音声> <部分結果.txt> [失敗一覧.txt] [追補後出力.txt]
```

- 既定の失敗一覧は `<部分結果.txt>.failed_segments.txt`
- 追補後出力の既定は `<部分結果>.recovered.txt`
- 元実行時の `SEGMENT_TIME` は `<部分結果.txt>.recovery_meta` から自動読込
- 自動読込できない場合は `WHISPER_SEGMENT_TIME=...` を指定

GUI 追補の使い方:

1. 通常実行後、`失敗区間の追補実行` カードを開く
2. 追補方式を選ぶ
3. `失敗一覧から再実行`:
   - `<部分結果>.failed_segments.txt` を使って残りだけ再実行
4. `時刻範囲を指定`:
   - 例: `00:10:00-00:12:30, 00:31:00`
   - 分割秒は空欄なら `.recovery_meta` から自動読込（必要時は手入力）
5. `追補実行` を押すと、`<部分結果>.recovered.txt` に追補統合結果を出力

モード自動選択の既定:

- 15分未満: 単一パス
- 15〜30分: 分割並列（180秒分割ベース）
- 30〜60分: 分割並列（150秒分割ベース）
- 60〜120分: 分割並列（120秒分割ベース）
- 120分以上: 分割並列（90秒分割ベース）
- `priority` と `preset` に応じて `jobs` / `segment秒` は自動補正されます（`accuracy` は保守的、`speed` は積極的）

VAD:

- Web GUI では既定で OFF（必要時のみチェックでON）
- 既定では単一パス時のみ有効（分割並列では安定性優先で自動無効）
- CLI では `WHISPER_USE_VAD=1` で有効化（必要なら `WHISPER_VAD_ALLOW_SEGMENTED=1` で分割並列にも強制）
- VADモデルが見つからない場合は自動で無効化されます
- VAD有効時に `whisper-cli` が失敗した場合は、安定性のため VADを自動OFFにして再試行します
- Metal(GPU) 由来のエラー検知時は、その後の実行を CPU 固定モードに切替えて安定性を優先します

補足:

- `WhisperDialog.command` は既存サーバが動作中でも、`whisper_gui_web.py` 更新を検知すると自動で再起動します（必要なら `WHISPER_GUI_FORCE_RESTART=1 ./WhisperDialog.command` で強制再起動）。
- デフォルト保存先: 入力キャッシュ `~/Library/Caches/WhisperGUI`、出力 `~/Downloads/WhisperGUI`
- 変更する場合は `WHISPER_GUI_CACHE_DIR` / `WHISPER_GUI_OUTPUT_DIR` を指定できます。

### 自動比較ベンチマーク（CSV出力）

同じ音声を `x1/x4/x8/x16` と `single/parallel` の組み合わせで連続実行し、結果を1フォルダに保存します。

```bash
./benchmark_matrix.sh <音声ファイル> [出力フォルダ]
```

出力:

- `benchmark.csv`（各回の計測結果）
- `benchmark_median.csv`（設定ごとの中央値）
- 設定別の実行ログと文字起こし結果テキスト

デフォルトの計測手順:

- ウォームアップ: 1回（`BENCHMARK_WARMUP_RUNS`）
- 本計測: 3回（`BENCHMARK_MEASURE_RUNS`）

## 手動実行（トラブルシューティング用）

スクリプトが動かない場合などは、手動で実行することも可能です（`transcribe.sh` などを参照）。

## ファイル構成

```
~/whisper-workflow/
├── README.md              # この文書
├── WhisperDialog.command  # macOS起動ランチャー
├── install_models.sh      # モデル一括インストール
├── models/                # 追加モデル（VADなど）
│   └── ggml-silero-v6.2.0.bin  # VADモデル
├── preflight.sh           # Preflight診断（JSON出力）
├── whisper_gui_web.py     # Web GUI
├── transcribe_workflow.sh # 自動一括処理スクリプト (推奨)
├── benchmark_matrix.sh    # 比較ベンチマーク実行 + CSV出力
└── transcribe.sh          # 旧・手動用スクリプト

~/.cache/whisper-cpp/
├── ggml-large-v3.bin      # x1
├── ggml-medium.bin        # x4
├── ggml-small.bin         # x8
└── ggml-tiny.bin          # x16
```

## 参考

- [whisper.cpp GitHub](https://github.com/ggerganov/whisper.cpp)
- [Hugging Face モデル](https://huggingface.co/ggerganov/whisper.cpp)
