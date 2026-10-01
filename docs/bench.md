# Meeting-Pipeline Benchmark — Pre-Committed Ceilings (task 31)

**Status:** ceilings fixed BEFORE measurement (this file was written, then committed
to disk, *before* `tests/bench.sh` was run for the first time). They are **not**
derived from any observed run — deriving a ceiling from its own measurement is
self-certifying and forbidden by the task.

**Host:** macOS 26.6.2 arm64, 24 GB RAM, **fanless** M-series SoC.
**Fixture:** `fixtures/meetings/meeting_20min.wav` — 1211.400375 s, 16 kHz mono,
two speakers, 100 turns (`meeting_20min.ground_truth.json`).
**Workload:** one full meeting through the service code path
`pipeline.transcribe_meeting` = ASR (mlx-whisper large-v3) → diarization
(pyannote community-1, MPS) → alignment → WeSpeaker embedding → identity match.

---

## 1. Pre-committed ceilings

| Metric | Ceiling | Derivation (no measurement involved) |
|--------|---------|--------------------------------------|
| **Peak RSS** (service process) | **5.0 GiB = 5,368,709,120 bytes** | `docs/budget.md` "Peak RAM — One Model at a Time" service components: mlx-whisper large-v3 `~3.0` + pyannote community-1 `~1.5` + WeSpeaker ResNet34 `~0.5` = **5.0**. The budget's "GB" figures are treated as **GiB** (strictly smaller ⇒ *stricter* ceiling). |
| **Wall time** (cold, end-to-end) | **3 × audio duration = 3 × 1211.400375 s = 3634.20 s** (≈ 60.6 min) | Fixed ratio mandated by the task: wall ≤ 3× audio duration. Concrete value for this fixture; the script recomputes `3 × ffprobe(duration)` so the *rule* is the commitment, not a hard-coded number. |
| Disk delta | *recorded, not gated* | Task requires measuring disk; no gated ceiling was budgeted. Reported for the record. |

VoiceStudio is **resident** (`127.0.0.1:3900`, `vs-ensure-up.sh` exit 0) for the
whole run — its ~1 GB runtime is part of the realistic fanless memory pressure.
The gate is the **service process RSS**, which excludes VoiceStudio's own RSS; the
budget's 6 GB "maximum simultaneous" (service + VoiceStudio TTS) is the system-level
figure, reported as a secondary observation only.

### Why 5.0 GiB is the service resident limit

`docs/budget.md` budgets the three models the service itself loads. The pipeline
runs them in one process, so the resident set of the service process is bounded by
their sum. This is the "budgeted resident-set limit for our service" the task names.
It was written in task 4, independent of this benchmark.

---

## 2. Measurement method

1. `bin/vs-ensure-up.sh` — VoiceStudio resident on `:3900` (hard requirement; the
   run aborts if it is not up).
2. `tests/bench_driver.py` runs `pipeline.transcribe_meeting(fixture)` against an
   **isolated** `VASTACK_DATA_DIR` (tempdir; never the operator's registry).
3. The driver is executed under **`/usr/bin/time -l`**, which reports the exact
   `maximum resident set size` (peak RSS, bytes) of the process. Sampling `ps` is
   not used because it can miss a transient peak; `time -l` cannot.
4. Wall time = `time -l` real seconds (cold: process start + imports + lazy model
   load + pipeline).
5. Disk = `du -sk` delta of the isolated data dir (uploads + SQLite) plus the
   transcript cache dir.
6. Thermal/throttle notes captured from `pmset -g therm` and load average.

## 3. Falsifiability

`bash tests/bench.sh` exits **0 only when both** `peak_rss ≤ RSS ceiling` **and**
`wall ≤ wall ceiling`. The comparison lives in `tests/bench_check.py` (one source
of truth). `bash tests/bench.sh --falsify` re-runs that **same comparator against the
real recorded measurement** with ceilings forced to floor values (1 byte / 1 s) and
asserts it **fails** — proving the gate is real, not a rubber stamp.

## 4. Documented remediation ladder (executed only on breach)

If a ceiling is breached, the script executes and re-measures, in order:
1. **Unload VoiceStudio TTS** — free the TTS engine's resident set.
2. **Serialize heavy stages** — ensure whisper weights are released before
   diarization/embedding loads (no overlapping model residency).
3. **Drop to a smaller whisper model** — via an explicit, documented override.
4. **Pin CPU** — `taskpolicy`/nice to stabilise fanless throttling.

The remediation applied and the re-measured numbers are printed and written to the
evidence file. No ceiling is ever moved to fit a measurement.

---

## 5. Measured result (2026-10-01, post-run)

Measured with VoiceStudio resident (`:3900` discovery HTTP 200, app RSS ~148–197 MB),
fixture `meeting_20min.wav` (1211.4 s), isolated `VASTACK_DATA_DIR`, offline flags on.

| Metric | Pre-committed ceiling | Measured | Verdict |
|--------|----------------------|----------|---------|
| Wall (cold, `/usr/bin/time -l` real) | 3634.20 s | **497.22 s** (0.41× audio) | ✅ PASS |
| Peak RSS (service process) | 5,368,709,120 B (5.0 GiB) | **4,013,146,112 B** (3.74 GiB) | ✅ PASS |
| Disk delta | recorded only | 128 kB (upload + SQLite) | — |
| Peak memory footprint (secondary) | — | 23,033,354,144 B (21.5 GiB, incl. unified/Metal) | observed |

Pipeline outcome: 320 segments, 2 unknown clusters, language `en`, no warnings
(the isolated registry had no enrolled speakers, so every cluster is `unknown` —
correct, not a failure). **No remediation was needed.** `bash tests/bench.sh --falsify`
re-ran the same comparator on this recorded measurement and proved the gate is real:
real ceilings → PASS (rc 0); ceilings forced to 1 B / 1 s → FAIL (rc 1).

**Thermal/throttle note (fanless):** `pmset -g therm` reported no thermal or
performance warning before or after; load average ~2.7–3.6. The host did not
throttle during this run.

### Blocking defect fixed while benchmarking

The first run **crashed** inside the pipeline (`embed._l2_normalize`: "cannot
normalize a zero or non-finite embedding") on a sub-window diarized segment. Root
cause: WeSpeaker ResNet34 returns a non-finite embedding for slices shorter than
its pooling window — measured, 0.1 s (1600 samples @16k) is NaN, ~0.105 s+ is
finite — but the pipeline guarded at `pipeline.MIN_EMBED_SECONDS = 0.1` (== the
NaN length). Raised to `0.2` (margin above the boundary); such clusters now fall
back to `unknown` with reason `cluster has no embeddable segment` instead of
killing the meeting. Regression test:
`tests/test_pipeline.py::test_sub_window_segment_is_not_embedded_cluster_becomes_unknown`.

Evidence: `~/.omo/evidence/voicestudio-omo-local-stack/task-31-bench.txt`
(+ `task-31-measurement.json`, `task-31-driver-baseline.json`, `task-31-time-baseline.log`).
