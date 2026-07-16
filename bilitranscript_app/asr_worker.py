"""Standalone local-ASR worker used by the desktop application.

The worker deliberately has no PySide dependency. A packaged GUI can invoke it
with an existing Python installation that already contains Faster-Whisper,
FunASR or OpenAI Whisper.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any


def configure_output() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)


def emit(event: str, **payload: Any) -> None:
    print(json.dumps({"event": event, **payload}, ensure_ascii=False), flush=True)


def available_backends() -> list[str]:
    candidates = (
        ("faster_whisper", "faster-whisper"),
        ("funasr", "funasr"),
        ("whisper", "openai-whisper"),
    )
    return [backend for module, backend in candidates if importlib.util.find_spec(module)]


def resolve_device(device: str) -> str:
    if device != "auto":
        return device
    try:
        import torch  # type: ignore

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        try:
            import ctranslate2  # type: ignore

            return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            return "cpu"


def clean_sensevoice_text(text: str) -> str:
    cleaned = re.sub(r"<\|[^|>]+\|>", " ", text)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return cleaned.strip()


def transcribe_faster_whisper(args: argparse.Namespace) -> dict[str, Any]:
    from faster_whisper import WhisperModel  # type: ignore

    device = resolve_device(args.device)
    compute_type = args.compute_type
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"
    emit("status", message=f"正在加载 Faster-Whisper {args.model}（{device}）")
    model = WhisperModel(args.model, device=device, compute_type=compute_type)
    segments_iter, info = model.transcribe(
        args.input,
        language=None if args.language == "auto" else args.language,
        task="transcribe",
        vad_filter=True,
    )
    duration = float(getattr(info, "duration", None) or args.duration or 0)
    segments: list[dict[str, Any]] = []
    for segment in segments_iter:
        item = {
            "start": float(getattr(segment, "start", 0) or 0),
            "end": float(getattr(segment, "end", 0) or 0),
            "text": str(getattr(segment, "text", "") or "").strip(),
        }
        if item["text"]:
            segments.append(item)
        if duration:
            emit("progress", value=min(98, int(item["end"] / duration * 100)), message="正在识别语音")
    return {
        "backend": "faster-whisper",
        "model": args.model,
        "device": device,
        "language": str(getattr(info, "language", args.language) or args.language),
        "duration": duration,
        "segments": segments,
        "text": "\n".join(item["text"] for item in segments),
    }


def transcribe_funasr(args: argparse.Namespace) -> dict[str, Any]:
    from funasr import AutoModel  # type: ignore

    device = resolve_device(args.device)
    emit("status", message=f"正在加载 FunASR {args.model}（{device}）")
    model = AutoModel(model=args.model, vad_model="fsmn-vad", device=device)
    emit("progress", value=5, message="正在识别语音")
    raw = model.generate(input=args.input, language=args.language)
    pieces: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                pieces.append(str(item.get("text") or ""))
    elif isinstance(raw, dict):
        pieces.append(str(raw.get("text") or ""))
    else:
        pieces.append(str(raw or ""))
    text = clean_sensevoice_text("\n".join(piece for piece in pieces if piece))
    duration = float(args.duration or 0)
    segments = [{"start": 0.0, "end": max(1.0, duration), "text": text}] if text else []
    emit("progress", value=98, message="正在整理识别结果")
    return {
        "backend": "funasr",
        "model": args.model,
        "device": device,
        "language": args.language,
        "duration": duration,
        "segments": segments,
        "text": text,
    }


def transcribe_openai_whisper(args: argparse.Namespace) -> dict[str, Any]:
    import torch  # type: ignore
    import whisper  # type: ignore

    device = resolve_device(args.device)
    emit("status", message=f"正在加载 OpenAI Whisper {args.model}（{device}）")
    model = whisper.load_model(args.model, device=device)
    emit("progress", value=5, message="正在识别语音")
    raw = model.transcribe(
        args.input,
        language=None if args.language == "auto" else args.language,
        task="transcribe",
        fp16=bool(device == "cuda" and torch.cuda.is_available()),
        verbose=False,
    )
    segments = [
        {
            "start": float(item.get("start") or 0),
            "end": float(item.get("end") or 0),
            "text": str(item.get("text") or "").strip(),
        }
        for item in raw.get("segments") or []
        if str(item.get("text") or "").strip()
    ]
    emit("progress", value=98, message="正在整理识别结果")
    return {
        "backend": "openai-whisper",
        "model": args.model,
        "device": device,
        "language": str(raw.get("language") or args.language),
        "duration": float(args.duration or 0),
        "segments": segments,
        "text": str(raw.get("text") or "").strip(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--input")
    parser.add_argument("--output")
    parser.add_argument("--backend", default="auto")
    parser.add_argument("--model", default="")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--compute-type", default="auto")
    parser.add_argument("--duration", type=float, default=0)
    return parser


def main() -> int:
    configure_output()
    args = build_parser().parse_args()
    available = available_backends()
    if args.probe:
        print(json.dumps({"backends": available}, ensure_ascii=False))
        return 0
    if not args.input or not args.output:
        emit("error", message="缺少输入或输出路径")
        return 2
    backend = args.backend
    if backend == "auto":
        backend = next((item for item in ("faster-whisper", "funasr", "openai-whisper") if item in available), "")
    if not backend or backend not in available:
        emit("error", message="没有可用的本地 ASR。请安装 faster-whisper 或 funasr。")
        return 3
    defaults = {
        "faster-whisper": "small",
        "funasr": "iic/SenseVoiceSmall",
        "openai-whisper": "base",
    }
    args.backend = backend
    args.model = args.model or defaults[backend]
    try:
        if backend == "faster-whisper":
            result = transcribe_faster_whisper(args)
        elif backend == "funasr":
            result = transcribe_funasr(args)
        else:
            result = transcribe_openai_whisper(args)
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        emit("complete", message="识别完成", output=args.output)
        return 0
    except Exception as exc:
        emit("error", message=f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

