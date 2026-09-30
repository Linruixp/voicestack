#!/bin/bash
# generate.sh — Regenerate early fixtures using macOS `say`
# Voices: Samantha (en_US), Tingting (zh_CN)
# Output: 16 kHz mono WAV (16-bit PCM)

set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

# English 3-second clip (~3.5s)
say -v "Samantha" -o "$DIR/en_3s.aiff" "The quick brown fox jumps over the lazy dog near the riverbank."
ffmpeg -y -i "$DIR/en_3s.aiff" -ar 16000 -ac 1 "$DIR/en_3s.wav" 2>/dev/null
rm -f "$DIR/en_3s.aiff"

# Chinese 3-second clip (~3.0s)
say -v "Tingting" -o "$DIR/zh_3s.aiff" "你好婷婷，今天天气真好。"
ffmpeg -y -i "$DIR/zh_3s.aiff" -ar 16000 -ac 1 "$DIR/zh_3s.wav" 2>/dev/null
rm -f "$DIR/zh_3s.aiff"

# English reference clip (~16.6s)
say -v "Samantha" -o "$DIR/ref_en.aiff" "The quick brown fox jumps over the lazy dog. This is a test of a longer speech segment. Let me count from one to ten. One, two, three, four, five, six, seven, eight, nine, ten. Birds sing in the morning light. This is the end of our sample."
ffmpeg -y -i "$DIR/ref_en.aiff" -ar 16000 -ac 1 "$DIR/ref_en.wav" 2>/dev/null
rm -f "$DIR/ref_en.aiff"

# Chinese reference clip (~13.5s)
say -v "Tingting" -o "$DIR/ref_zh.aiff" "婷婷：今天天气真好，我们去公园散步吧。琳达：好啊，公园里有很多花都开了。婷婷：是啊，春天来了，花儿都开了，非常美丽。"
ffmpeg -y -i "$DIR/ref_zh.aiff" -ar 16000 -ac 1 "$DIR/ref_zh.wav" 2>/dev/null
rm -f "$DIR/ref_zh.aiff"

echo "Done. Generated files:"
ls -la "$DIR"/*.wav
