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

### 自動文字起こし（並列処理・一括実行）

作成した `transcribe_workflow.sh` を使用することで、変換から文字起こしまでを一括で行えます。

```bash
# 基本的な使い方
./transcribe_workflow.sh <音声ファイル> [出力ファイル名]

# 例
./transcribe_workflow.sh interview.m4a result.txt
```

このスクリプトは以下の処理を自動で行います：

1. 音声ファイルを wav (16kHz) に変換
2. 60秒ごとに分割
3. 並列処理で高速に文字起こし（4プロセス同時実行）
4. 結果を結合して出力
5. 中間ファイルの自動削除

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

### GUI アプリで実行

`tkinter` が使えるか先に確認します。

```bash
python3 -m tkinter
```

GUI を起動します。

```bash
python3 whisper_gui.py
```

機能:

1. 音声ファイル選択
2. 出力先（Save As）選択
3. 実行中ログ表示（stdout/stderr）
4. 実行中の二重起動防止
5. Cancel ボタンで処理中断（子プロセスも停止）
6. 速度プリセット選択（`x1/x4/x8/x16`）と精度目安の表示
7. Advanced で `single-pass` / `分割並列` を切替、並列ジョブ数（`JOBS`）を指定可能（上限4）
8. Advanced の自動サフィックス保存で、比較実験時に設定別ファイル（例: `_x4_parallel_j2`）を自動作成

補足:

- `whisper_gui.py` は自身の場所を基準に `transcribe_workflow.sh` を解決するため、別の作業ディレクトリから起動しても動作します。
- 実行ログ末尾に性能指標が表示されます（`Convert/Transcribe/Concat/Total` 秒、`RTF`、`x realtime`）。

## 手動実行（トラブルシューティング用）

スクリプトが動かない場合などは、手動で実行することも可能です（`transcribe.sh` などを参照）。

## ファイル構成

```
~/whisper-workflow/
├── README.md              # この文書
├── install_models.sh      # モデル一括インストール
├── whisper_gui.py         # GUI アプリ
├── transcribe_workflow.sh # 自動一括処理スクリプト (推奨)
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
