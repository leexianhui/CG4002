"""structlog -> JSONL: one JSON object per line, written to logs/<component>-<epoch_ms>.jsonl,
plus a human-readable mirror on the console.

Every process (relay, engine, mock sims) calls configure_logging() once at startup so that
tools/latency_report.py has a uniform log format to parse (COMMS_IMPLEMENTATION_PLAN.md §12).

The file and the console deliberately get *different renderings of the same events*: the file stays
machine-readable JSONL for latency_report.py, while the console gets structlog's ConsoleRenderer so
a human watching a live demo can actually read it. Before this split the console only ever showed
WARNING+ (and showed it as raw JSON), which meant a perfectly healthy run — `timesync_seeded`,
`window_ready`, `action_classified` are all INFO — looked completely silent on screen and every
question about "is it working?" had to be answered by grepping a log file after the fact.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog

from cg_comms.clock import wall_clock_unix_ms

# Foreign (non-structlog) records — websockets' "server listening on ...", asyncio's "Task
# exception was never retrieved" — pass through these before hitting either renderer, so a
# third-party log line lands in the JSONL file as a real JSON object rather than as a bare string
# that latency_report.py has to skip.
_FOREIGN_PRE_CHAIN = [
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.stdlib.ExtraAdder(),
]


def configure_logging(
    component: str,
    logs_dir: Path,
    level: int = logging.INFO,
    console_level: int | None = None,
) -> Path:
    """Configures structlog + stdlib logging to emit one JSON object per line to a fresh log file
    and a human-readable stream to stderr. Returns the log file path.

    `level` gates the JSONL file; `console_level` gates the console mirror and defaults to `level`,
    so `--log-level INFO` shows the full happy path on screen. Pass a higher `console_level` to
    quieten the terminal without losing detail in the file.
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{component}-{wall_clock_unix_ms()}.jsonl"

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=_FOREIGN_PRE_CHAIN,
        )
    )

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(level if console_level is None else console_level)
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
            foreign_pre_chain=_FOREIGN_PRE_CHAIN,
        )
    )

    root = logging.getLogger()
    root.setLevel(min(level, console_handler.level))
    root.handlers = [file_handler, console_handler]

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Hands the event dict to whichever ProcessorFormatter the receiving handler owns,
            # instead of rendering here — that's what lets one event be JSON in the file and
            # human-readable on the console.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(min(level, console_handler.level)),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(component=component)
    return log_path
