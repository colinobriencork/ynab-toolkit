"""Backend contract tests use invented prompts; no account or network is needed."""

import subprocess
import sys
from pathlib import Path

import pytest

from ynab_categorizer.backends import (
    BackendError, ClaudeBackend, CodexBackend, CommandBackend, create_backend,
)
from ynab_categorizer.categorizer import Categorizer, build_guess_prompt


@pytest.fixture
def calls(monkeypatch):
    captured = []

    def run(cmd, **kwargs):
        captured.append((cmd, kwargs))
        if kwargs.get("cwd"):
            assert Path(kwargs["cwd"]).is_dir()
        return subprocess.CompletedProcess(cmd, 0, stdout="CATEGORY: Dining\n", stderr="diagnostics")

    monkeypatch.setattr("ynab_categorizer.backends.subprocess.run", run)
    return captured


def test_codex_uses_stdin_final_output_and_isolated_directory(calls, monkeypatch):
    monkeypatch.setenv("YNAB_API_TOKEN", "fake-private-token")
    monkeypatch.setenv("AMAZON_PASSWORD", "fake-private-password")
    monkeypatch.setenv("CODEX_API_KEY", "fake-provider-token")
    assert CodexBackend().generate("private test prompt", timeout=7) == "CATEGORY: Dining"
    cmd, kwargs = calls[0]
    assert cmd[:2] == ["codex", "exec"]
    assert cmd[-1] == "-"
    assert "private test prompt" not in cmd
    assert kwargs["input"] == "private test prompt"
    assert kwargs["timeout"] == 7
    assert "YNAB_API_TOKEN" not in kwargs["env"]
    assert "AMAZON_PASSWORD" not in kwargs["env"]
    assert kwargs["env"]["CODEX_API_KEY"] == "fake-provider-token"
    assert not Path(kwargs["cwd"]).exists()  # temporary instructions cleaned up
    assert "--ephemeral" in cmd and "--ignore-user-config" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert 'web_search="disabled"' in cmd
    assert "project_doc_max_bytes=0" in cmd
    for feature in ("shell_tool", "apps", "hooks", "plugins", "multi_agent"):
        assert cmd[cmd.index(feature) - 1] == "--disable"
    assert "--json" not in cmd
    assert "--model" not in cmd


def test_codex_second_pass_selects_model_and_web_research(calls):
    CodexBackend().generate("prompt", model="example-model", timeout=11, second_pass=True)
    cmd, kwargs = calls[0]
    assert cmd[cmd.index("--model") + 1] == "example-model"
    assert 'web_search="live"' in cmd
    assert kwargs["timeout"] == 11


def test_codex_never_inherits_claude_defaults(calls):
    c = Categorizer(backend="codex")
    txn = {"date": "2014-03-10", "amount": -2000, "payee_name": "Example Cafe"}
    c.suggest(txn, [], [])
    c.guess(txn, [], [])
    assert all("--model" not in cmd for cmd, _ in calls)


def test_codex_explicit_model_is_used_for_both_passes(calls):
    c = Categorizer(backend="codex", model="example-model")
    txn = {"date": "2014-03-10", "amount": -2000}
    c.suggest(txn, [], [])
    c.guess(txn, [], [])
    assert all(cmd[cmd.index("--model") + 1] == "example-model" for cmd, _ in calls)


@pytest.mark.parametrize("backend", [ClaudeBackend(), CodexBackend(), CommandBackend(["wrapper"])])
@pytest.mark.parametrize("failure", ["missing", "timeout", "exit", "empty"])
def test_backend_failures_are_actionable_and_do_not_echo_private_text(backend, failure, monkeypatch):
    def fail(cmd, **kwargs):
        if failure == "missing":
            raise FileNotFoundError("private-text")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, 3, output="private-text", stderr="private-text")
        return subprocess.CompletedProcess(cmd, 2 if failure == "exit" else 0,
                                           stdout="", stderr="private-text")
    monkeypatch.setattr("ynab_categorizer.backends.subprocess.run", fail)
    with pytest.raises(BackendError) as exc:
        backend.generate("private-text", timeout=3)
    assert "private-text" not in str(exc.value)
    assert {"missing": "not found", "timeout": "timed out", "exit": "code 2",
            "empty": "empty response"}[failure] in str(exc.value)


def test_custom_command_arguments_are_literal_and_second_pass_is_configurable(calls):
    backend = CommandBackend(["wrapper", "--model", "{model}", "$(example)"],
                             ["other-wrapper", "--slow"])
    backend.generate("sensitive prompt", model="test-model", timeout=3)
    backend.generate("second prompt", timeout=4, second_pass=True)
    assert calls[0][0] == ["wrapper", "--model", "test-model", "$(example)"]
    assert calls[0][1].get("shell", False) is False
    assert calls[1][0] == ["other-wrapper", "--slow"]
    assert calls[1][1]["input"] == "second prompt"


def test_custom_command_runs_a_real_stdin_stdout_process():
    backend = CommandBackend([sys.executable, "-c",
                              "import sys; print('CATEGORY: ' + sys.stdin.read().strip())"])
    assert backend.generate("Dining", timeout=3) == "CATEGORY: Dining"


@pytest.mark.parametrize("command", [None, [], "wrapper --model small", [""], [2], ["a\0b"], ["{model}"]])
def test_custom_command_requires_an_argument_array(command):
    with pytest.raises(ValueError):
        CommandBackend(command)


def test_missing_command_model_does_not_start_process(calls):
    with pytest.raises(ValueError, match="requires model"):
        CommandBackend(["wrapper", "{model}"]).generate("prompt", timeout=3)
    assert calls == []


def test_plain_text_backend_does_not_promise_web_research(calls):
    c = Categorizer(backend="command", backend_command=["wrapper"])
    c.guess({"date": "2014-03-10", "amount": -2000}, [], [])
    prompt = calls[0][1]["input"]
    assert "Web research is unavailable" in prompt
    assert "use web search and fetch" not in prompt
    assert "UNCERTAIN:" in prompt


def test_custom_python_backend_uses_the_same_categorization_contract():
    class Backend:
        supports_research = False
        default_model = "local"
        default_guess_model = None

        def generate(self, prompt, *, model, timeout, second_pass=False):
            assert model == "local"
            return "UNCERTAIN: item details needed" if second_pass else "QUESTION: What was purchased?"

    c = Categorizer(backend=Backend())
    txn = {"date": "2014-03-10", "amount": -2000}
    assert c.suggest(txn, [], [])["action"] == "ask"
    assert c.guess(txn, [], [])["action"] == "uncertain"


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError, match="Unknown model backend"):
        create_backend("misspelled")
