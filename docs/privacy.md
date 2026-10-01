# Privacy & biometric data handling

The speaker-service stores **voiceprints** — embeddings derived from a
person's voice. A voiceprint is **biometric data**: it is a stable,
person-linked identifier and cannot be reissued like a password. This note
records what is stored, how it is protected, and the explicit reliance on
macOS disk encryption.

## What is stored

- `speakers` (name, organization, notes) — optional operator-entered PII.
- `voiceprints` (embedding vector + `{model_id, revision, dim}` provenance) —
  **biometric templates**, one row per enrolled cluster version.
- `meetings`, `segments`, `clusters`, `jobs` — transcript text and meeting
  titles, which may themselves contain personal data.
- Uploaded audio lives only under `<data_dir>/uploads/` while a job runs.

Everything above is inside the app-data root
`~/Library/Application Support/VoiceStudioStack/`, which the service keeps at
mode `0700`, as is the SQLite database itself (`voicestack.db`). The service is
single-user and loopback-only.

## FileVault reliance (explicit)

The service does **not** implement its own at-rest encryption. Biometric
templates, transcripts and audio are protected at rest **only by FileVault**
(full-disk encryption), which is the sole boundary if a device leaves the
operator's possession. This is an intentional baseline for a single-user local
tool: `fdesetup status` must report `FileVault is On.` Threats this does **not**
cover (an unlocked, running session, a compromised admin account, or backups
taken without encryption) are accepted residual risk and out of scope.

The designed threat boundary is: (a) no non-loopback bind, (b) bearer/token
auth on every mutation, (c) no CORS, (d) owner-only file modes, (e) secrets
only in the macOS Keychain, and (f) FileVault for at-rest data.

## Retention & deletion

- `DELETE /speakers/{id}` purges the speaker **and every voiceprint row** (and
  its vector-index row); linked clusters revert to `unknown` and segment/
  meeting references are nulled. Meetings and transcripts survive.
- `VASTACK_DELETE_AUDIO_AFTER_TRANSCRIBE=1` (default **off**) deletes the
  uploaded recording as soon as its job reaches a terminal state.
- Deleting a speaker row is irreversible; the UI requires typing the speaker
  name before a biometric purge.

## Secrets

- Service bearer token: Keychain `voicestack-service` / account `service`.
- Hugging Face token: Keychain `voicestack-hf`.
- No token is written to the repository, `.env`, or any served asset; the Web
  UI authenticates with a server-minted `HttpOnly` `SameSite=Lax` session
  cookie for the loopback origin only.

Verification: `speaker-service/tests/test_security.sh` asserts each of the
above; evidence `~/.omo/evidence/voicestudio-omo-local-stack/task-27-security.txt`.
