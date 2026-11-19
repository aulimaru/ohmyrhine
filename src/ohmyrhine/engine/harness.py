"""The command-line agents a lookup runs in (catalog.HARNESSES): Claude Code, Codex and
Antigravity, each run headless in the lookup's working directory on the account it is logged in
with, so it uses that subscription, never an API key.

A run is given what it may touch (Access): the files it may write, directories it may read
besides its own, and whether it may go on the web. Each harness gets that in its own terms,
and is told nothing else may run. How far each one holds to it:
- Claude Code: reading, writing and the web exactly as given (`--permission-mode dontAsk`).
- Antigravity: the same, through per-run permission rules; an action they deny ends the run
  without an answer.
- Codex: its sandbox keeps writes to the working directory (all of it, not only the files
  given) and has no network but its own web search; it can read the whole disk.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..catalog import HARNESSES, MODELS
from ..config import AgentToolConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Found:
    """A lookup's outcome: what the agent wrote back, or why there is nothing."""

    text: str
    ok: bool
    ms: int = 0
    model: str | None = None
    # Token usage as the API counts it, and what the run would cost there: the run itself is on
    # the subscription.
    usage: dict[str, int] = field(default_factory=dict)
    api_cost: float | None = None
    turns: int | None = None


@dataclass(frozen=True)
class Access:
    # The only files it may write.
    write: tuple[Path, ...] = ()
    # Directories it may read besides its working directory.
    read: tuple[Path, ...] = ()
    web: bool = False


_FAILED = "这次没查成：查东西的程序出错了。"


def _api_cost(model: str | None, usage: dict[str, int]) -> float | None:
    """What the run would cost on the API, for a model the price table has (Antigravity's
    model names carry the effort: gemini-4-hydrogen-high)."""
    if model is None:
        return None
    spec = MODELS.get(model) or MODELS.get(re.sub(r"-(low|medium|high|xhigh|max)$", "", model))
    return round(spec.pricing.cost(usage), 6) if spec and spec.pricing else None


class Harness:
    """Runs one lookup tool's agent; at most `slots` runs at once across tools."""

    def __init__(self, tool: AgentToolConfig, timeout_s: float, slots: asyncio.Semaphore):
        self.spec = HARNESSES[tool.harness]
        self.command = tool.command or self.spec.command
        self.model = tool.model or self.spec.model
        self.effort = self.spec.efforts[tool.effort] if tool.effort else None
        self.timeout_s, self._slots = timeout_s, slots

    @property
    def instructions(self) -> str:
        return self.spec.instructions

    def installed(self) -> bool:
        return shutil.which(self.command) is not None

    async def _exec(self, args: list[str], workdir: Path, env: dict[str, str] | None = None) -> tuple[bytes, bytes, int | None]:
        async with self._slots:
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=workdir, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), self.timeout_s)
            except (TimeoutError, asyncio.CancelledError):
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
                raise
        return out, err, proc.returncode

    async def __call__(self, workdir: Path, prompt: str, access: Access) -> Found:
        raise NotImplementedError


class ClaudeCode(Harness):
    async def __call__(self, workdir: Path, prompt: str, access: Access) -> Found:
        tools = ["Read", "Grep", "Glob"]
        allowed = [f"Edit(/{p.resolve()})" for p in access.write]  # // is an absolute path
        if access.web:
            tools += ["WebSearch", "WebFetch"]
            allowed += ["WebSearch", "WebFetch"]
        if access.write:
            tools += ["Edit", "Write"]
        args = [
            self.command, "-p", prompt, "--output-format", "json", "--no-session-persistence",
            # Its own CLAUDE.md, none of the account's settings, hooks or MCP servers; asked about
            # nothing, it may use only what is allowed here.
            "--setting-sources", "project", "--strict-mcp-config", "--permission-mode", "dontAsk",
            "--tools", *tools,
        ]
        if allowed:
            args += ["--allowedTools", *allowed]
        for path in access.read:
            args += ["--add-dir", str(path)]
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            args += ["--effort", self.effort]
        # Never an API key, whatever the gateway was started with: the subscription login only.
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
        started = time.monotonic()
        out, err, code = await self._exec(args, workdir, env)
        ms = round((time.monotonic() - started) * 1000)
        try:
            result = json.loads(out)
        except ValueError:
            log.warning("Claude Code 没有正常结束（退出码 %s）：%s", code, err.decode(errors="replace")[-300:])
            return Found(_FAILED, False, ms)
        usage = result.get("usage") or {}
        found = Found(
            str(result.get("result") or "").strip(), not result.get("is_error") and result.get("subtype") == "success", ms,
            next(iter(result.get("modelUsage") or {}), None) or self.model,
            {
                "input": usage.get("input_tokens") or 0, "output": usage.get("output_tokens") or 0,
                "cache_read": usage.get("cache_read_input_tokens") or 0,
                "cache_write": usage.get("cache_creation_input_tokens") or 0,
            },
            result.get("total_cost_usd"), result.get("num_turns"),
        )
        if not found.ok:
            log.warning("Claude Code 报错：%s", (found.text or result.get("subtype") or "")[:300])
        return found


class Codex(Harness):
    async def __call__(self, workdir: Path, prompt: str, access: Access) -> Found:
        with tempfile.TemporaryDirectory() as tmp:
            last = Path(tmp) / "answer.txt"
            args = [self.command, *(["--search"] if access.web else []), "exec", "--json",
                    "--skip-git-repo-check", "--ephemeral",
                    # None of the account's config or rules: what it may do is set here.
                    "--ignore-user-config", "--ignore-rules",
                    "-s", "workspace-write" if access.write else "read-only",
                    "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
                    "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                    "-C", str(workdir), "-o", str(last)]
            if self.model:
                args += ["-m", self.model]
            if self.effort:
                args += ["-c", f'model_reasoning_effort="{self.effort}"']
            started = time.monotonic()
            out, err, code = await self._exec([*args, prompt], workdir)
            ms = round((time.monotonic() - started) * 1000)
            text = last.read_text(encoding="utf-8").strip() if last.is_file() else ""
        usage: dict[str, int] = {}
        failed = None
        for line in out.decode(errors="replace").splitlines():
            with contextlib.suppress(ValueError):
                event = json.loads(line)
                if event.get("type") == "turn.completed":
                    u = event.get("usage") or {}
                    cached, written = u.get("cached_input_tokens") or 0, u.get("cache_write_input_tokens") or 0
                    usage = {"input": (u.get("input_tokens") or 0) - cached - written, "cache_read": cached,
                             "cache_write": written, "output": u.get("output_tokens") or 0}
                elif event.get("type") in ("turn.failed", "error"):
                    failed = str(event.get("error") or event.get("message") or event)[:300]
        if code != 0 or failed or not text:
            log.warning("Codex 没有正常结束（退出码 %s）：%s", code, failed or err.decode(errors="replace")[-300:])
            return Found(text or _FAILED, False, ms, self.model, usage, _api_cost(self.model, usage))
        return Found(text, True, ms, self.model, usage, _api_cost(self.model, usage))


class Antigravity(Harness):
    async def __call__(self, workdir: Path, prompt: str, access: Access) -> Found:
        own = str(workdir.resolve())
        allow = [f"read_file({own})", *(f"read_file({p.resolve()})" for p in access.read),
                 *(f"write_file({p.resolve()})" for p in access.write)]
        if access.web:
            allow.append("read_url(*)")
        # For this run only: anything not allowed here is denied, never asked about.
        settings = {"permissions": {"allow": allow}, "toolPermission": "request-review",
                    "allowNonWorkspaceAccess": False, "trustedWorkspaces": [own]}
        args = [self.command, "-p", prompt, "--output-format", "json", "--disable-slash-commands",
                "--settings", json.dumps(settings, ensure_ascii=False)]
        for path in access.read:
            args += ["--add-dir", str(path)]
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            args += ["--effort", self.effort]
        started = time.monotonic()
        out, err, code = await self._exec(args, workdir)
        ms = round((time.monotonic() - started) * 1000)
        try:
            result = json.loads(out)
        except ValueError:
            log.warning("Antigravity 没有正常结束（退出码 %s）：%s", code, err.decode(errors="replace")[-300:])
            return Found(_FAILED, False, ms)
        u = result.get("usage") or {}
        cached = u.get("cache_read_tokens") or 0
        usage = {"input": (u.get("input_tokens") or 0) - cached, "cache_read": cached,
                 "output": u.get("output_tokens") or 0}
        text = str(result.get("response") or "").strip()
        ok = result.get("status") == "SUCCESS" and bool(text)
        if not ok:
            # A denied action ends the run without an answer; stderr says which.
            log.warning("Antigravity 没有查完（%s）：%s", result.get("status"), (result.get("error") or err.decode(errors="replace"))[-300:])
        return Found(text or _FAILED, ok, ms, self.model, usage, _api_cost(self.model, usage), result.get("num_turns"))


_KINDS: dict[str, type[Harness]] = {"claude": ClaudeCode, "codex": Codex, "agy": Antigravity}


def make_harness(tool: AgentToolConfig, timeout_s: float, slots: asyncio.Semaphore) -> Harness:
    return _KINDS[tool.harness](tool, timeout_s, slots)
