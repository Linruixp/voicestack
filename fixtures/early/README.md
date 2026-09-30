# Early Fixtures

Minimal unlabeled audio clips generated via macOS `say` to unblock Wave 1 transcription and voice-creation probes before the full labeled fixture set (task 36) exists.

## Files

| File | Duration | Voice | Purpose |
|------|----------|-------|---------|
| `en_3s.wav` | ~3.5 s | Samantha (en_US) | Short English probe clip |
| `zh_3s.wav` | ~3.0 s | Tingting (zh_CN) | Short Chinese probe clip |
| `ref_en.wav` | ~16.6 s | Samantha (en_US) | Longer English reference clip |
| `ref_zh.wav` | ~13.5 s | Tingting (zh_CN) | Longer Chinese reference clip |

## Format

- Sample rate: 16 000 Hz
- Channels: 1 (mono)
- Bit depth: 16-bit PCM (WAV)

## Regeneration

```bash
chmod +x generate.sh
./generate.sh
```

Requirements: macOS, `ffmpeg` (e.g. via Homebrew), `say` (built-in).

## Notes

- `.wav` files are gitignored (`*.wav`). Only `generate.sh` and `README.md` are committed.
- Voices used: `Samantha` (en_US) and `Tingting` (zh_CN) — both built into macOS.
- These fixtures are **not** labeled; they are for probing/development only.
