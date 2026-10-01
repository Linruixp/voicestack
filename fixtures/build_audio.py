#!/usr/bin/env python3
"""Build the simple audio fixtures: 30 s EN/ZH speech, a 3 s clip, silence,
and 10-20 s reference clips.

Voices (macOS built-ins): Samantha = en_US, Tingting = zh_CN.
Output: 16 kHz mono 16-bit PCM WAV under fixtures/audio and fixtures/refs.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (
    AUDIO,
    REFS,
    SR,
    probe_duration,
    silence_frames,  # noqa: E402
    synth_wav,
    write_wav,
)

EN_30S = (
    "The library opened its doors at eight in the morning, and within an hour "
    "the reading room was full of students preparing for their final "
    "examinations. A quiet energy filled the space as pages turned and pencils "
    "moved across paper. Near the tall windows, a small group discussed a "
    "research project about renewable energy, comparing notes from three "
    "different cities. The librarian reminded everyone that the archive would "
    "close early on Friday, and that any borrowed materials needed to be "
    "returned before the weekend. Outside, the first signs of autumn were "
    "beginning to show in the trees along the avenue."
)

ZH_30S = (
    "图书馆每天早上八点开门，到了九点，阅览室里已经坐满了准备考试的学生。"
    "安静的空气里，只有翻书声和铅笔在纸上沙沙作响。靠窗的桌子旁，"
    "几个同学正在讨论一个关于可再生能源的研究项目。管理员提醒大家，"
    "周五会提前闭馆，借出的资料要在周末之前归还。窗外，"
    "大道两旁的树叶已经露出秋天的颜色。"
)

CLIP_3S = "Thank you for listening to this short sample."

REF_EN = (
    "The quick brown fox jumps over the lazy dog. This recording is a clean "
    "reference sample used for voice enrollment. Please stand by while the "
    "system measures the acoustic characteristics of the speaker. One, two, "
    "three, four, five, six, seven, eight, nine, ten."
)

REF_ZH = (
    "这是一个用于声音注册的参考录音样本。请保持安静，"
    "系统正在测量说话人的声音特征。今天天气很好，我们一起去公园散步吧。"
    "一二三四五六七八九十。"
)


def to_exact_3s(path: Path) -> None:
    """Pad/trim an existing WAV to exactly 3.000 s."""
    from _common import read_pcm

    frames, _ = read_pcm(path)
    target = 3 * SR * 2
    frames = frames[:target].ljust(target, b"\x00")
    write_wav(path, frames)


def main() -> int:
    AUDIO.mkdir(parents=True, exist_ok=True)
    REFS.mkdir(parents=True, exist_ok=True)

    # Pure silence, 5 s.
    write_wav(AUDIO / "silence_5s.wav", silence_frames(5.0))

    # 30 s EN / ZH speech.
    synth_wav(EN_30S, "Samantha", AUDIO / "en_30s.wav")
    synth_wav(ZH_30S, "Tingting", AUDIO / "zh_30s.wav")

    # 3 s clip (built short, then hard-padded to exactly 3.000 s).
    synth_wav(CLIP_3S, "Samantha", AUDIO / "clip_3s.wav")
    to_exact_3s(AUDIO / "clip_3s.wav")

    # Reference clips for voice enrollment (task 8).
    synth_wav(REF_EN, "Samantha", REFS / "ref_en.wav")
    synth_wav(REF_ZH, "Tingting", REFS / "ref_zh.wav")

    for p in sorted(list(AUDIO.glob("*.wav")) + list(REFS.glob("*.wav"))):
        print(f"  {p.relative_to(AUDIO.parent)}  {probe_duration(p):.3f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
