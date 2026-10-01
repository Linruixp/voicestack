# VoiceStack test fixtures

Deterministic, **offline**, regenerable test assets for the VoiceStack local
stack. Every audio file is **16 kHz mono 16-bit PCM WAV** so ASR, diarization,
voice enrollment and matching can all share one canonical format.

Speech is synthesised with the built-in macOS `say` voices:

| Speaker | Voice      | Language |
|---------|------------|----------|
| A (EN)  | `Samantha` | en_US    |
| B (EN)  | `Fred`     | en_US    |
| ZH      | `Tingting` | zh_CN    |

> **Audio is not committed.** The root `.gitignore` excludes `*.wav` (and
> `*.m4b`, `*.mp3`). Only the generators, this README, the ground-truth JSON,
> the EPUB and the PDFs are tracked. Run `./generate_all.sh` to recreate every
> audio file byte-for-byte.

## Layout

```
fixtures/
├── generate_all.sh              # ← regenerate everything (entry point)
├── _common.py                   # shared helpers (WAV I/O, say/ffmpeg)
├── build_audio.py               # 30 s EN/ZH, 3 s clip, silence, refs
├── build_meetings.py            # labeled 2×2 set + 20-min meeting + JSON
├── build_docs.py                # text PDFs, EPUB, scanned PDF, non-audio
├── verify.py                    # asserts format/duration/consistency
├── not_audio.txt                # a non-audio file
├── README.md                    # this file
├── audio/
│   ├── en_30s.wav               # ≈30 s English speech (Samantha)   32.5 s
│   ├── zh_30s.wav               # ≈30 s Mandarin speech (Tingting)  31.3 s
│   ├── clip_3s.wav              # exactly 3.000 s English clip
│   └── silence_5s.wav           # 5 s of pure digital silence
├── refs/
│   ├── ref_en.wav               # 17.0 s clean EN reference (task 8)
│   └── ref_zh.wav               # 15.2 s clean ZH reference (task 8)
├── docs/
│   ├── en_text.pdf              # selectable-text EN PDF, 3 headings
│   ├── zh_text.pdf              # selectable-text ZH PDF (STSong-Light)
│   ├── book.epub                # valid EPUB 3, 3 chapters + nav
│   └── scanned.pdf              # IMAGE-ONLY PDF, no text layer
└── meetings/
    ├── spkA_rec1.wav            # speaker A, recording 1  (33.4 s, 3 seg)
    ├── spkA_rec2.wav            # speaker A, recording 2  (34.1 s, 3 seg)
    ├── spkB_rec1.wav            # speaker B, recording 1  (40.7 s, 3 seg)
    ├── spkB_rec2.wav            # speaker B, recording 2  (36.9 s, 3 seg)
    ├── ground_truth.json        # labels for the four files above
    ├── meeting_20min.wav        # ≈20 min 2-speaker meeting (1211.4 s)
    └── meeting_20min.ground_truth.json   # labels for the meeting
```

`early/` (from task 38) is untouched and kept alongside for compatibility.

## What each item is for

| Fixture | Purpose | Consumed by |
|---------|---------|-------------|
| `audio/en_30s.wav`, `audio/zh_30s.wav` | ASR happy path in EN + ZH; EN word timestamps | tasks 15, 16, 17 |
| `audio/clip_3s.wav` | short-clip / min-length edge cases | tasks 18, 22, 24 |
| `audio/silence_5s.wav` | ASR + pipeline failure path (empty transcript, no crash) | tasks 15, 21 |
| `not_audio.txt` | non-audio upload must error cleanly | tasks 22, 25 |
| `docs/en_text.pdf`, `docs/zh_text.pdf` | text-based audiobook import → `.m4b` | tasks 10, 11, 12 |
| `docs/book.epub` | EPUB audiobook import (≥2 chapters) | tasks 10, 11 |
| `docs/scanned.pdf` | image-only PDF → MinerU OCR path | task 12 |
| `refs/ref_en.wav`, `refs/ref_zh.wav` | clean voice-enrollment references | tasks 8, 18 |
| `meetings/spk?_rec?.wav` + `ground_truth.json` | calibration (≥2 speakers × ≥2 recordings) + returning-speaker recognition | tasks 16, 17, 18, 20, 23, 24 |
| `meetings/meeting_20min.wav` + JSON | full diarization/alignment/pipeline meeting | tasks 16, 17, 21, 37 |

### The 2-speaker × 2-recording labeled set

Four files, **one speaker per file** (`per-file: speaker label, transcript,
segment timestamps`). Speaker **A** appears in `spkA_rec1` **and**
`spkA_rec2`; speaker **B** appears in `spkB_rec1` **and** `spkB_rec2`. This is
exactly the "each speaker in ≥2 recordings" shape that task 20's threshold
calibration and task 24's cross-recording recognition require.

Each file is built by concatenating that speaker's utterances with a fixed
0.4 s gap between utterances. Boundaries are derived from real sample offsets,
so ground truth matches the audio to the sample.

## Ground-truth JSON schema

### `meetings/ground_truth.json`

```jsonc
{
  "schema": "voicestack.fixtures.meetings/v1",
  "sample_rate": 16000, "channels": 1, "sample_format": "s16le",
  "gap_seconds": 0.4,
  "speakers": { "A": {"voice": "Samantha"}, "B": {"voice": "Fred"} },
  "files": {
    "spkA_rec1.wav": {
      "speaker": "A",              // single speaker for this file
      "voice": "Samantha",
      "language": "en",
      "duration": 33.406,          // seconds, matches the audio
      "transcript": "Good morning …",  // full concatenated text
      "segments": [
        { "speaker": "A", "start": 0.0, "end": 11.532, "text": "…" },
        { "speaker": "A", "start": 11.932, "end": 23.009, "text": "…" }
      ]
    }
    // … spkA_rec2.wav, spkB_rec1.wav, spkB_rec2.wav
  }
}
```

### `meetings/meeting_20min.ground_truth.json`

```jsonc
{
  "schema": "voicestack.fixtures.meeting20/v1",
  "sample_rate": 16000, "channels": 1, "sample_format": "s16le",
  "gap_seconds": 0.4,
  "speakers": { "A": {"voice": "Samantha"}, "B": {"voice": "Fred"} },
  "duration": 1211.4,
  "turn_count": 100,
  "segments": [                       // alternating A/B turns, in order
    { "speaker": "A", "start": 0.0,    "end": 11.532, "text": "…" },
    { "speaker": "B", "start": 11.932, "end": 26.173, "text": "…" }
  ]
}
```

## Regeneration

```bash
cd ~/voicestack/fixtures
./generate_all.sh
```

Requirements (all pre-installed on this host): macOS `say`, `ffmpeg`/`ffprobe`,
`python3` with `reportlab` + `Pillow`, `pdftoppm`, `pdftotext`. The script
fails fast if any is missing.

To also write the task evidence file:

```bash
FIXTURES_EVIDENCE=~/.omo/evidence/voicestudio-omo-local-stack/task-36-fixtures.txt \
  ./generate_all.sh
```

`verify.py` runs automatically and exits non-zero unless every file exists,
is the right duration/format, the text PDFs are extractable, `scanned.pdf`
has **no** text layer, the EPUB structure is valid, and the ground truth
matches the audio.

## Provenance

Generated for plan todo **#36** of `.omo/plans/voicestudio-omo-local-stack.md`.
Deterministic: no network is used and no user recording is required.
