"""Start the assistant service with logging of model calls and events, for a head-to-head with pi.

The service sends usage only to Langfuse. This launcher wraps the actor adapter, the one-shot
client and the event sink, and appends what they do to SPIKE_LOG as JSONL.

Usage: SPIKE_LOG=... python pi/eval/assistant_server.py PORT
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ASSISTANT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ASSISTANT_ROOT))
os.chdir(ASSISTANT_ROOT)

import uvicorn  # noqa: E402

from agents.core import llm_adapter  # noqa: E402
from agents.services.events.jobs import EventSink  # noqa: E402
from agents.services.llm.llm_client import LLMClient  # noqa: E402

LOG = Path(os.environ["SPIKE_LOG"])


def write(record: dict) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.time(), **record}, ensure_ascii=False, default=str) + "\n")


def wrap_adapter(cls: type) -> None:
    original = cls.chat

    async def chat(self, *args, **kwargs):
        started = time.monotonic()
        response = await original(self, *args, **kwargs)
        write(
            {
                "kind": "actor_call",
                "model": getattr(self, "model", None),
                "usage": getattr(response, "usage", None),
                "seconds": round(time.monotonic() - started, 2),
            }
        )
        return response

    cls.chat = chat


def wrap_client() -> None:
    for name in ("call_async", "call_sync"):
        original = getattr(LLMClient, name)

        if name == "call_async":

            async def call(self, *args, _original=original, **kwargs):
                write(
                    {"kind": "client_call", "role": getattr(self, "role", None), "model": getattr(self, "model", None)}
                )
                return await _original(self, *args, **kwargs)

        else:

            def call(self, *args, _original=original, **kwargs):
                write(
                    {"kind": "client_call", "role": getattr(self, "role", None), "model": getattr(self, "model", None)}
                )
                return _original(self, *args, **kwargs)

        setattr(LLMClient, name, call)


def wrap_sink() -> None:
    original = EventSink.send

    def send(self, event, *args, **kwargs):
        payload = getattr(event, "model_dump", lambda: event)()
        write({"kind": "event", "event": payload})
        return original(self, event, *args, **kwargs)

    EventSink.send = send


wrap_adapter(llm_adapter.OpenAIAdapter)
wrap_adapter(llm_adapter.AnthropicAdapter)
wrap_client()
wrap_sink()

if __name__ == "__main__":
    uvicorn.run("api.main:app", host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
