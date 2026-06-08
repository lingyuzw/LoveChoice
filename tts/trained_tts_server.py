from __future__ import annotations

import argparse
import random
import sys
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel


COSYVOICE3_PROMPT = "You are a helpful assistant"
DEFAULT_SEED = 42
DEFAULT_WARMUP_TEXT = "\u4f60\u597d\u3002"


class TTSRequest(BaseModel):
    text: str
    stream: bool = True
    speed: float = 1.12
    seed: int | None = DEFAULT_SEED


app = FastAPI(title="Trained CosyVoice3 TTS API")
cosyvoice = None
speaker = "hanser"


def setup_cosyvoice(
    repo_dir: str,
    model_dir: str,
    load_vllm: bool = False,
    load_trt: bool = False,
    fp16: bool = False,
    trt_concurrent: int = 1,
    strict_vllm: bool = False,
):
    repo = Path(repo_dir).resolve()
    sys.path.insert(0, str(repo))
    sys.path.append(str(repo / "third_party" / "Matcha-TTS"))

    from cosyvoice.cli.cosyvoice import AutoModel

    kwargs = {"model_dir": model_dir}
    if load_vllm:
        kwargs["load_vllm"] = True
    if load_trt:
        kwargs["load_trt"] = True
        kwargs["trt_concurrent"] = trt_concurrent
    if fp16:
        kwargs["fp16"] = True

    try:
        return AutoModel(**kwargs)
    except Exception as exc:
        if not load_vllm or strict_vllm:
            raise

        print(
            "[vllm fallback] CosyVoice3 vLLM initialization failed; "
            "retrying without --load_vllm so the TTS API can still start.",
            flush=True,
        )
        print(f"[vllm fallback] original error: {exc}", flush=True)
        fallback_kwargs = dict(kwargs)
        fallback_kwargs.pop("load_vllm", None)
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        return AutoModel(**fallback_kwargs)


def set_seed(seed: int | None) -> None:
    seed = DEFAULT_SEED if seed is None else seed
    try:
        from cosyvoice.utils.common import set_all_random_seed

        set_all_random_seed(seed)
    except Exception:
        random.seed(seed)
        np.random.seed(seed)


def tensor_to_pcm16_bytes(tts_speech) -> bytes:
    audio = tts_speech.detach().cpu().numpy().reshape(-1)
    audio = np.clip(audio, -1.0, 1.0)
    return (audio * 32767).astype(np.int16).tobytes()


def stream_pcm(chunks: Iterator[dict]) -> Iterator[bytes]:
    for item in chunks:
        yield tensor_to_pcm16_bytes(item["tts_speech"])


def format_cosyvoice3_text(text: str) -> str:
    text = text.strip()
    # 如果 LLM 输出已经带了 <|endofprompt|> 残留（prompt 泄漏），剪掉前缀保留后面的真实对话内容。
    if "<|endofprompt|>" in text:
        text = text.split("<|endofprompt|>", 1)[1].strip()
    # 如果有其他英文字符串残留（如 "You are a helpful assistant"），去掉。
    for marker in (
        "You are a helpful assistant",
        "You are a helpful",
        "A conversation between User and Assistant",
    ):
        text = text.replace(marker, "")
    text = text.strip()
    # CosyVoice3 内部强制要求 <|endofprompt|> 标记存在（作为 SFT 格式分隔符），
    # 但不能带可朗读的英文 prompt 文本，否则会被模型语音化输出。
    return f"<|endofprompt|>{text}"


def warmup_model(text: str = DEFAULT_WARMUP_TEXT) -> None:
    try:
        chunks = cosyvoice.inference_sft(
            format_cosyvoice3_text(text),
            speaker,
            stream=False,
            speed=1.0,
        )
        for _ in chunks:
            pass
    except Exception as exc:
        print(f"[warmup skipped] {exc}", flush=True)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "sample_rate": cosyvoice.sample_rate,
        "speaker": speaker,
        "prompt": COSYVOICE3_PROMPT,
        "default_seed": DEFAULT_SEED,
    }


@app.post("/tts")
def tts(req: TTSRequest):
    set_seed(req.seed)
    tts_text = format_cosyvoice3_text(req.text)
    print(f"TTS input: {tts_text}", flush=True)

    chunks = cosyvoice.inference_sft(
        tts_text,
        speaker,
        stream=req.stream,
        speed=req.speed,
    )
    return StreamingResponse(
        stream_pcm(chunks),
        media_type="application/octet-stream",
        headers={
            "X-Sample-Rate": str(cosyvoice.sample_rate),
            "X-Audio-Format": "pcm_s16le",
        },
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo_dir", required=True, help="CosyVoice repository directory")
    parser.add_argument("--model_dir", required=True, help="Your trained CosyVoice3 model directory")
    parser.add_argument("--speaker", default="hanser", help="Speaker id")
    parser.add_argument("--warmup_text", default=DEFAULT_WARMUP_TEXT)
    parser.add_argument("--no_warmup", action="store_true")
    parser.add_argument("--load_vllm", action="store_true", help="Enable CosyVoice3 internal vLLM acceleration")
    parser.add_argument("--strict_vllm", action="store_true", help="Exit if --load_vllm cannot initialize")
    parser.add_argument("--load_trt", action="store_true", help="Enable TensorRT acceleration if supported")
    parser.add_argument("--fp16", action="store_true", help="Enable fp16 where supported")
    parser.add_argument("--trt_concurrent", type=int, default=1)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=50000)
    args = parser.parse_args()

    global cosyvoice, speaker
    cosyvoice = setup_cosyvoice(
        repo_dir=args.repo_dir,
        model_dir=args.model_dir,
        load_vllm=args.load_vllm,
        load_trt=args.load_trt,
        fp16=args.fp16,
        trt_concurrent=args.trt_concurrent,
        strict_vllm=args.strict_vllm,
    )

    available_speakers = cosyvoice.list_available_spks()
    if args.speaker:
        speaker = args.speaker
    elif available_speakers:
        speaker = available_speakers[0]
    else:
        speaker = "hanser"

    print(f"Loaded model: {args.model_dir}", flush=True)
    print(f"Speaker: {speaker}", flush=True)
    print(f"Sample rate: {cosyvoice.sample_rate}", flush=True)
    print(f"Available speakers: {available_speakers}", flush=True)

    if not args.no_warmup:
        warmup_model(args.warmup_text)

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
