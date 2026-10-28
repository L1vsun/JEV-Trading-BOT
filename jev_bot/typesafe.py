"""Minimal, dependency-free client for Jev (TypeSafe's System One model).

Implements the documented HTTP API (https://docs.typesafe.ai/api):

    POST https://api.typesafe.ai/v1/systemone
    Authorization: Bearer <TYPESAFE_API_KEY>
    {"state": ..., "model": "jev-latest", "questions": {id: Question}}

Question types: ``Noul`` (yes/no probability), ``Choice`` (one option +
distribution + confidence) and ``Score`` (probability-weighted level +
distribution + confidence). All questions in one call are evaluated in
parallel, so the bot asks everything it needs in a single request.

Also here: retries with exponential backoff on 429/529 (honouring
``retry-after``), token/cost accounting, ``GET /v1/models``, and a JSONL
response cache so a live run can be replayed later without a key.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

BASE_URL = os.environ.get("TYPESAFE_ENDPOINT", "https://api.typesafe.ai")
DEFAULT_MODEL = "jev-latest"
PRICE_PER_MTOK = 0.042            # USD per million input tokens; output is free


# ---------------------------------------------------------------- questions
@dataclass
class Noul:
    instructions: Any
    criteria: dict | None = None          # {"true": ..., "false": ...}

    def to_json(self) -> dict:
        d = {"type": "noul", "instructions": self.instructions}
        if self.criteria:
            d["criteria"] = self.criteria
        return d


@dataclass
class Choice:
    instructions: Any
    criteria: dict[str, Any]              # option -> description (or None)

    def to_json(self) -> dict:
        if not 1 < len(self.criteria) <= 255:
            raise ValueError("a Choice needs 2..255 options")
        return {"type": "choice", "instructions": self.instructions, "criteria": self.criteria}


@dataclass
class Score:
    instructions: Any
    criteria: list[Any]                   # ordered level descriptions

    def to_json(self) -> dict:
        if not 2 <= len(self.criteria) <= 10:
            raise ValueError("a Score needs 2..10 levels")
        return {"type": "score", "instructions": self.instructions, "criteria": self.criteria}


Question = Noul | Choice | Score


# ---------------------------------------------------------------- answers
@dataclass
class Answer:
    type: str
    choice: str | None = None
    score: float | None = None
    noul: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None
    legend: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_json(cls, d: dict) -> "Answer":
        return cls(type=d["type"], choice=d.get("choice"), score=d.get("score"), noul=d.get("noul"),
                   probabilities={str(k): float(v) for k, v in (d.get("probabilities") or {}).items()},
                   confidence=d.get("confidence"), legend=d.get("legend") or {})

    def to_json(self) -> dict:
        d: dict[str, Any] = {"type": self.type}
        for k in ("choice", "score", "noul", "confidence"):
            if getattr(self, k) is not None:
                d[k] = getattr(self, k)
        if self.probabilities:
            d["probabilities"] = self.probabilities
        if self.legend:
            d["legend"] = self.legend
        return d

    def p(self, option: str) -> float:
        return self.probabilities.get(option, 0.0)


@dataclass
class Response:
    model: str
    answers: dict[str, Answer]
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    cached: bool = False

    def __getitem__(self, k: str) -> Answer:
        return self.answers[k]


class SystemOne(Protocol):
    """Anything that answers typed questions about a state: the real Jev
    client or a replay cache."""
    model: str

    def system_one(self, state: Any, questions: dict[str, Question]) -> Response: ...


# ---------------------------------------------------------------- errors
class JevError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def confidence_from(probs: dict[str, float]) -> float:
    """TypeSafe's documented confidence shape: (k * p_max - 1) / (k - 1)."""
    k = len(probs)
    if k < 2:
        return 1.0
    return max(0.0, min(1.0, (k * max(probs.values()) - 1) / (k - 1)))


# ---------------------------------------------------------------- cache
class ResponseCache:
    """JSONL cache keyed by the sha256 of the request body."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.mem: dict[str, dict] = {}
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.mem[row["key"]] = row["response"]

    @staticmethod
    def key(body: dict) -> str:
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def get(self, body: dict) -> dict | None:
        return self.mem.get(self.key(body))

    def put(self, body: dict, response: dict) -> None:
        k = self.key(body)
        self.mem[k] = response
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps({"key": k, "response": response}) + "\n")


# ---------------------------------------------------------------- client
@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: list[float] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return self.input_tokens / 1e6 * PRICE_PER_MTOK


class JevClient:
    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL, base_url: str = BASE_URL,
                 timeout: float = 10.0, max_retries: int = 4, cache: ResponseCache | None = None,
                 cache_only: bool = False):
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.cache = cache
        self.cache_only = cache_only
        self.usage = Usage()
        self.last_payload: dict | None = None
        if not self.api_key and not cache_only:
            raise JevError(
                "TYPESAFE_API_KEY is not set. Export it before calling ask, models, or live "
                "(or pass --replay-cache FILE for offline replay)."
            )

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        delay = 0.5
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(self.base_url + path, data=data, method=method, headers={
                "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                "User-Agent": "jev-trading-bot/0.1"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code in (429, 529) and attempt < self.max_retries:
                    ra = e.headers.get("retry-after")
                    time.sleep(float(ra) if ra and ra.replace(".", "", 1).isdigit() else delay + random.random() * delay)
                    delay = min(delay * 2, 8.0)
                    continue
                detail = e.read().decode(errors="replace")[:500]
                raise JevError(f"HTTP {e.code}: {detail}", e.code) from None
            except urllib.error.URLError as e:
                if attempt < self.max_retries:
                    time.sleep(delay)
                    delay = min(delay * 2, 8.0)
                    continue
                raise JevError(f"network error: {e.reason}") from None
        raise JevError("retries exhausted")

    def system_one(self, state: Any, questions: dict[str, Question]) -> Response:
        body = {"state": state, "model": self.model, "questions": {k: q.to_json() for k, q in questions.items()}}
        # The body is the request. The key stays on the Authorization header only.
        self.last_payload = body
        t0 = time.perf_counter()
        raw = self.cache.get(body) if self.cache else None
        cached = raw is not None
        if raw is None:
            if self.cache_only:
                raise JevError("request not in cache (cache_only mode)")
            raw = self._request("POST", "/v1/systemone", body)
            if self.cache:
                self.cache.put(body, raw)
        ms = (time.perf_counter() - t0) * 1000
        u = raw.get("usage") or {}
        self.usage.calls += 1
        if not cached:
            self.usage.input_tokens += int(u.get("input_tokens", 0))
            self.usage.output_tokens += int(u.get("output_tokens", 0))
            self.usage.latency_ms.append(ms)
        return Response(raw.get("model", self.model), {k: Answer.from_json(v) for k, v in raw["answers"].items()},
                        int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0)), ms, cached)

    def models(self) -> list[dict]:
        return self._request("GET", "/v1/models").get("models", [])


def payload_text(body: dict) -> str:
    """The exact request JSON. It has no Authorization header and no API key."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
