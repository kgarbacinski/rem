"""Run one model call through the user's own `claude` CLI, headless and isolated.

No API key, no SDK, no extra dependency: whatever account Claude Code is logged
into does the thinking. Each call is a fresh process with no hooks, no plugins,
no MCP servers, no memory and no saved transcript, so a dream never dreams about itself.
"""
import json
import os
import shutil
import subprocess
import threading

DEFAULT_MODELS = {"analyze": "haiku", "audit": "sonnet", "synthesize": "sonnet", "skeptic": "sonnet"}


class LLMError(RuntimeError):
    pass


class Usage:
    """Thread-safe running total of what a dream cost."""

    def __init__(self):
        self._lock = threading.Lock()
        self.calls = 0
        self.cost_usd = 0.0
        self.input_tokens = 0
        self.output_tokens = 0
        self.seconds = 0.0

    def add(self, result: dict):
        usage = result.get("usage") or {}
        with self._lock:
            self.calls += 1
            self.cost_usd += float(result.get("total_cost_usd") or 0)
            self.input_tokens += int(usage.get("input_tokens") or 0) + int(
                usage.get("cache_creation_input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0)
            self.output_tokens += int(usage.get("output_tokens") or 0)
            self.seconds += float(result.get("duration_ms") or 0) / 1000

    def as_dict(self):
        return {
            "calls": self.calls,
            "cost_usd": round(self.cost_usd, 4),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model_seconds": round(self.seconds, 1),
        }


def model_for(stage: str) -> str:
    return os.environ.get(f"REM_MODEL_{stage.upper()}") or os.environ.get("REM_MODEL") or DEFAULT_MODELS[stage]


def call(stage, system, prompt, schema, usage=None, tools=None, cwd=None, timeout=900, budget_usd=None):
    """Return the structured output of one call. Raises LLMError on failure."""
    mock = os.environ.get("REM_LLM_CMD")
    if mock:
        payload = json.dumps({"stage": stage, "system": system, "prompt": prompt, "schema": schema})
        out = subprocess.run(mock, shell=True, input=payload, capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            raise LLMError(f"mock backend failed: {out.stderr[:400]}")
        return json.loads(out.stdout)

    binary = os.environ.get("REM_CLAUDE_BIN") or shutil.which("claude")
    if not binary:
        raise LLMError("the `claude` CLI is not on PATH")
    cmd = [
        binary, "-p",
        "--model", model_for(stage),
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
        "--system-prompt", system,
        "--no-session-persistence",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--setting-sources", "",
    ]
    if tools:
        cmd += ["--tools", ",".join(tools), "--allowedTools", ",".join(tools)]
    else:
        cmd += ["--tools", ""]
    if budget_usd:
        cmd += ["--max-budget-usd", str(budget_usd)]
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    env["REM_CHILD"] = "1"
    try:
        out = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
                             cwd=str(cwd) if cwd else None, env=env)
    except subprocess.TimeoutExpired:
        raise LLMError(f"{stage}: timed out after {timeout}s")
    try:
        result = json.loads(out.stdout)
    except ValueError:
        raise LLMError(f"{stage}: unreadable output: {(out.stdout or out.stderr)[:400]}")
    if usage is not None:
        usage.add(result)
    if result.get("is_error"):
        raise LLMError(f"{stage}: {str(result.get('result'))[:400]}")
    data = result.get("structured_output")
    if data is None:
        try:
            data = json.loads(result.get("result") or "")
        except ValueError:
            raise LLMError(f"{stage}: the model returned no structured output")
    return data
