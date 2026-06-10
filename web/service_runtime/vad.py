from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

import numpy as np

MIC_SAMPLE_RATE = 16000
VAD_BLOCK_SIZE = 512


class VadModelStore:
    """Lazy shared Silero VAD model holder."""

    def __init__(self, device: str):
        self.device = device
        self.model = None
        self.torch = None
        self.VADIterator = None
        self.lock = asyncio.Lock()

    async def load(self):
        async with self.lock:
            if self.model is not None:
                return self.torch, self.VADIterator, self.model, self.device

            import torch
            from silero_vad import VADIterator, load_silero_vad

            torch.set_num_threads(1)
            device = self.device
            if device == "auto":
                device = "cuda" if torch.cuda.is_available() else "cpu"

            model = load_silero_vad()
            if device != "cpu":
                model = model.to(device)

            self.device = device
            self.torch = torch
            self.VADIterator = VADIterator
            self.model = model
            return torch, VADIterator, model, device


class VoiceVadSession:
    """Per-client VAD state that emits vad_start/vad_end/vad_short events."""

    def __init__(self, torch, vad_iterator_cls, model, device: str, settings: Any):
        self.torch = torch
        self.device = device
        self.vad_iterator = vad_iterator_cls(
            model,
            threshold=settings.vad_threshold,
            sampling_rate=MIC_SAMPLE_RATE,
            min_silence_duration_ms=settings.vad_min_silence_ms,
            speech_pad_ms=settings.vad_speech_pad_ms,
        )
        self.pending = np.array([], dtype=np.float32)
        self.block_seconds = VAD_BLOCK_SIZE / MIC_SAMPLE_RATE
        self.pre_roll_chunks = max(1, int((settings.pre_speech_ms / 1000.0) / self.block_seconds))
        self.pre_roll: deque[np.ndarray] = deque(maxlen=self.pre_roll_chunks)
        self.speech_chunks: list[np.ndarray] = []
        self.in_speech = False
        self.max_utterance_sec = settings.max_utterance_sec
        self.min_utterance_ms = settings.min_utterance_ms

    def push_audio(self, audio: np.ndarray) -> list[dict]:
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        if not len(audio):
            return []

        self.pending = np.concatenate([self.pending, audio])
        events: list[dict] = []
        while len(self.pending) >= VAD_BLOCK_SIZE:
            chunk = self.pending[:VAD_BLOCK_SIZE].copy()
            self.pending = self.pending[VAD_BLOCK_SIZE:]
            events.extend(self._push_block(chunk))
        return events

    def reset(self) -> None:
        self.vad_iterator.reset_states()
        self.pending = np.array([], dtype=np.float32)
        self.pre_roll.clear()
        self.speech_chunks = []
        self.in_speech = False

    def _push_block(self, chunk: np.ndarray) -> list[dict]:
        chunk_tensor = self.torch.from_numpy(chunk)
        if self.device != "cpu":
            chunk_tensor = chunk_tensor.to(self.device)

        event = self.vad_iterator(chunk_tensor, return_seconds=True)
        started = isinstance(event, dict) and "start" in event
        ended = isinstance(event, dict) and "end" in event
        events: list[dict] = []

        if started and not self.in_speech:
            self.in_speech = True
            self.speech_chunks = list(self.pre_roll)
            self.pre_roll.clear()
            events.append({"type": "vad_start"})

        if self.in_speech:
            self.speech_chunks.append(chunk.copy())
            seconds = len(self.speech_chunks) * self.block_seconds
            if ended or seconds >= self.max_utterance_sec:
                audio = np.concatenate(self.speech_chunks) if self.speech_chunks else np.array([], dtype=np.float32)
                self.speech_chunks = []
                self.in_speech = False
                self.vad_iterator.reset_states()
                duration_ms = int(len(audio) / MIC_SAMPLE_RATE * 1000)
                if duration_ms >= self.min_utterance_ms:
                    events.append({"type": "vad_end", "duration_ms": duration_ms, "audio": audio})
                else:
                    events.append({"type": "vad_short", "duration_ms": duration_ms})
        else:
            self.pre_roll.append(chunk.copy())

        return events
