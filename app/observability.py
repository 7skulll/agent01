"""单进程、内存有界的指标：计数、活跃数和固定耗时分桶，不采集业务正文。"""

import asyncio
import json
import logging
import math
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from uuid import uuid4

logger = logging.getLogger(__name__)
BOUNDS_MS = (10, 50, 100, 250, 500, 1000, 2500, 5000, 10000)


class Metrics:
    def __init__(self):
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.counters = {}
        self.timings = {}
        self.active = {}

    def increment(self, name, value=1, **labels):
        """标签由内部固定枚举提供，不能使用问题、文件名、ID 或服务端错误正文。"""
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            return
        key = (name, tuple(sorted(labels.items())))
        with self.lock:
            self.counters[key] = self.counters.get(key, 0) + value

    def observe(self, stage, seconds, outcome="ok"):
        if not math.isfinite(seconds) or seconds < 0:
            return
        elapsed = seconds * 1000
        with self.lock:
            entry = self.timings.setdefault(
                (stage, outcome),
                {
                    "count": 0,
                    "total_ms": 0.0,
                    "max_ms": 0.0,
                    "buckets": [0] * (len(BOUNDS_MS) + 1),
                },
            )
            entry["count"] += 1
            entry["total_ms"] += elapsed
            entry["max_ms"] = max(entry["max_ms"], elapsed)
            for index, bound in enumerate((*BOUNDS_MS, math.inf)):
                entry["buckets"][index] += int(elapsed <= bound)

    @contextmanager
    def span(self, stage):
        started, outcome = time.perf_counter(), "ok"
        with self.lock:
            self.active[stage] = self.active.get(stage, 0) + 1
        try:
            yield
        except BaseException as exc:
            outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
            raise
        finally:
            self.observe(stage, time.perf_counter() - started, outcome)
            with self.lock:
                self.active[stage] -= 1

    def tokens(self, stage, *, estimated_input=None, usage=None):
        """实际用量与本地估算分开累计；缺少 provider usage 时不伪造实际用量。"""
        if estimated_input is not None:
            self.increment("tokens_estimated_input", estimated_input, stage=stage)
        if isinstance(usage, dict):
            for key, alternate in (
                ("input_tokens", "prompt_tokens"),
                ("output_tokens", "completion_tokens"),
            ):
                value = usage.get(key, usage.get(alternate))
                if value is not None:
                    self.increment(
                        "tokens_actual_" + key.removesuffix("_tokens"), value, stage=stage
                    )

    def snapshot(self):
        """累计值仅覆盖当前进程生命周期；桶为累计计数，不保存无限增长的样本。"""
        with self.lock:
            return {
                "started_at": self.started_at,
                "uptime_seconds": round(time.monotonic() - self.started, 3),
                "counters": [
                    {"name": name, "labels": dict(labels), "value": value}
                    for (name, labels), value in sorted(self.counters.items())
                ],
                "timings": [
                    {
                        "stage": stage,
                        "outcome": outcome,
                        "count": data["count"],
                        "total_ms": round(data["total_ms"], 3),
                        "mean_ms": round(data["total_ms"] / data["count"], 3),
                        "max_ms": round(data["max_ms"], 3),
                        "buckets": [
                            {"le_ms": bound, "count": count}
                            for bound, count in zip((*BOUNDS_MS, "+Inf"), data["buckets"])
                        ],
                    }
                    for (stage, outcome), data in sorted(self.timings.items())
                ],
                "active": dict(sorted(self.active.items())),
            }


def measured(stage):
    """给持有 metrics 的服务方法记录成功、异常与取消；不改变原异常语义。"""

    def decorate(method):
        @wraps(method)
        async def wrapped(self, *args, **kwargs):
            with self.metrics.span(stage):
                return await method(self, *args, **kwargs)

        return wrapped

    return decorate


class MetricsMiddleware:
    def __init__(self, app, metrics):
        self.app, self.metrics = app, metrics
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/api/v1/metrics":
            return await self.app(scope, receive, send)
        started, status, request_id = time.perf_counter(), 500, str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id

        async def capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode("ascii")),
                ]
            await send(message)

        try:
            with self.metrics.span("http"):
                await self.app(scope, receive, capture)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            method = (
                scope["method"]
                if scope["method"]
                in {
                    "GET",
                    "POST",
                    "PUT",
                    "PATCH",
                    "DELETE",
                    "HEAD",
                    "OPTIONS",
                }
                else "OTHER"
            )
            self.metrics.increment("http_requests", route=route, method=method, status=str(status))
            logger.info(
                json.dumps(
                    {
                        "event": "http_request",
                        "request_id": request_id,
                        "route": route,
                        "method": method,
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    }
                )
            )
