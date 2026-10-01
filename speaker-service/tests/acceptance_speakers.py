"""Acceptance: VS-bridge speech/clone/translate + the speaker-identity protocol.

The protocol enrolls the recording-1 speaker, proves recording 2 is a NEW meeting
whose segments are auto-named, proves a never-enrolled stranger stays ``unknown``,
and proves a silent recording yields an empty transcript without crashing.
"""

from __future__ import annotations

from typing import Any

from acceptance_support import (
    FIXTURES,
    Reporter,
    Session,
    b64,
    delete_profile,
)


async def speech_capabilities(sess: dict[str, Session], rep: Reporter) -> None:
    vb = sess["vsbridge"]
    speech = await vb.call(
        "generate_speech",
        {
            "text": "Hello from the acceptance run of the on-demand VoiceStudio stack.",
            "language": "en",
        },
    )
    sp = speech.get("parsed") or {}
    rep.cap(
        "generate_speech",
        "vsbridge",
        "generate_speech",
        vb.ok(speech)
        and bool(sp.get("audio_id"))
        and sp.get("audio_duration_s", 0) > 0,
        {
            "audio_id": sp.get("audio_id"),
            "audio_duration_s": sp.get("audio_duration_s"),
            "wall_ms": speech["wall_ms"],
        },
    )

    clone = await vb.call(
        "clone_voice",
        {
            "name": "acceptance-clone",
            "ref_audio_base64": b64(FIXTURES / "refs" / "ref_en.wav"),
        },
    )
    cl = clone.get("parsed") or {}
    rep.cap(
        "clone_voice",
        "vsbridge",
        "clone_voice",
        vb.ok(clone) and bool(cl.get("profile_id")) and cl.get("kind") == "clone",
        {
            "profile_id": cl.get("profile_id"),
            "kind": cl.get("kind"),
            "wall_ms": clone["wall_ms"],
        },
    )
    if cl.get("profile_id"):
        delete_profile(str(cl["profile_id"]))

    tr: dict[str, Any] = {}
    for _ in range(2):  # argos cold-load flakes once; retry as the notepad records
        tr = await sess["voicebridge"].call(
            "translate_text",
            {
                "text": "The harbour light turned once more before the storm arrived.",
                "src": "en",
                "tgt": "zh",
            },
        )
        if sess["voicebridge"].ok(tr) and (tr.get("parsed") or {}).get("ok"):
            break
    tp = tr.get("parsed") or {}
    rep.cap(
        "translate_text",
        "voicebridge",
        "translate_text",
        sess["voicebridge"].ok(tr)
        and tp.get("ok") is True
        and bool(str(tp.get("translated") or "").strip()),
        {
            "provider": tp.get("provider"),
            "offline": tp.get("offline"),
            "translated": tp.get("translated"),
        },
    )


async def speaker_protocol(sess: dict[str, Session], rep: Reporter) -> None:
    svc = sess["vs-speaker"]
    ca = FIXTURES / "meetings"
    r1 = await svc.call("transcribe_meeting", {"file_path": str(ca / "spkA_rec1.wav")})
    m1 = r1.get("parsed") or {}
    segs1 = m1.get("segments") or []
    cids1 = sorted({s["cluster_id"] for s in segs1 if s.get("cluster_id") is not None})
    rep.cap(
        "transcribe_meeting",
        "vs-speaker",
        "transcribe_meeting",
        svc.ok(r1) and bool(segs1) and bool(cids1),
        {
            "meeting_id": (m1.get("meeting") or {}).get("id"),
            "segments": len(segs1),
            "clusters": cids1,
            "wall_ms": r1["wall_ms"],
        },
    )
    if not cids1:
        rep.check(
            "enroll_from_rec1",
            "speaker registry",
            "recording-1 cluster enrolled as a named speaker",
            False,
            {"reason": "no cluster id in recording 1"},
        )
        return

    enroll = await svc.call(
        "enroll_speaker", {"name": "Samantha", "cluster_id": cids1[0]}
    )
    ep = enroll.get("parsed") or {}
    rep.check(
        "enroll_from_rec1",
        "speaker registry",
        "recording-1 cluster enrolled as a named speaker",
        svc.ok(enroll)
        and ep.get("name") == "Samantha"
        and ep.get("speaker_id") is not None,
        {"cluster_id": cids1[0], "speaker_id": ep.get("speaker_id")},
    )

    r2 = await svc.call("transcribe_meeting", {"file_path": str(ca / "spkA_rec2.wav")})
    m2 = r2.get("parsed") or {}
    segs2 = m2.get("segments") or []
    unknown2 = m2.get("unknown_clusters")
    speakers2 = list(m2.get("speakers") or [])
    mid1 = (m1.get("meeting") or {}).get("id")
    mid2 = (m2.get("meeting") or {}).get("id")
    named = bool(segs2) and all(s.get("speaker_name") == "Samantha" for s in segs2)
    rep.check(
        "meeting_returning_speaker",
        "recording-2 transcript",
        "NEW meeting; every segment auto-named Samantha; no unknown clusters",
        svc.ok(r2)
        and mid2 is not None
        and mid2 != mid1
        and named
        and unknown2 == []
        and "Samantha" in speakers2,
        {
            "meeting_id": mid2,
            "segments": len(segs2),
            "speakers": speakers2,
            "unknown_clusters": unknown2,
            "names": sorted({s.get("speaker_name") for s in segs2}),
        },
    )

    stranger = await svc.call(
        "identify_speaker", {"audio_path": str(ca / "spkB_rec1.wav")}
    )
    st = (stranger.get("parsed") or {}).get("status")
    sim = (stranger.get("parsed") or {}).get("similarity")
    rep.cap(
        "identify_speaker",
        "vs-speaker",
        "identify_speaker",
        svc.ok(stranger) and st == "unknown",
        {"status": st, "similarity": sim, "wall_ms": stranger["wall_ms"]},
    )
    rep.check(
        "meeting_stranger_unknown",
        "spkB_rec1 identify",
        "a never-enrolled speaker is classified unknown",
        svc.ok(stranger) and st == "unknown",
        {"status": st, "similarity": sim},
    )

    quiet = await svc.call(
        "transcribe_meeting", {"file_path": str(FIXTURES / "audio" / "silence_5s.wav")}
    )
    mq = quiet.get("parsed") or {}
    qsegs = mq.get("segments")
    rep.check(
        "silent_meeting_no_crash",
        "silence_5s transcript",
        "pipeline does not crash on a silent recording; transcript is empty",
        svc.ok(quiet)
        and (mq.get("meeting") or {}).get("id") is not None
        and isinstance(qsegs, list)
        and len(qsegs) == 0,
        {
            "meeting_id": (mq.get("meeting") or {}).get("id"),
            "segments": qsegs,
            "is_error": quiet["is_error"],
            "text": quiet["text"][:300],
        },
    )
