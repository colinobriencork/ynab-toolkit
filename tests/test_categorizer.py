"""Tests for the categorizer.

These test the behavioural contract:
- Prompt building includes the right context
- LLM response parsing correctly extracts category or question
- Category name resolution works
- The suggest flow works end-to-end with an injected LLM function
"""

from ynab_categorizer.categorizer import (
    build_categorization_prompt,
    build_guess_prompt,
    parse_llm_response,
    resolve_category,
    Categorizer,
    _run_claude,
)


# -- Fixtures --

SAMPLE_TRANSACTION = {
    "id": "t1",
    "payee_name": "Example Cafe",
    "amount": -4500,
    "date": "2014-03-10",
    "memo": "",
    "account_name": "Checking",
}

SAMPLE_CATEGORIES = [
    {"id": "c-dining", "name": "Dining Out", "group_name": "Lifestyle"},
    {"id": "c-groceries", "name": "Groceries", "group_name": "Essentials"},
    {"id": "c-vacation", "name": "Vacation", "group_name": "Travel"},
]

PAST_TRANSACTIONS = [
    {
        "payee_name": "Example Cafe",
        "category_name": "Dining Out",
        "approved": True,
        "date": "2014-02-15",
        "amount": -5000,
    },
    {
        "payee_name": "Example Cafe",
        "category_name": "Dining Out",
        "approved": True,
        "date": "2014-01-20",
        "amount": -3500,
    },
    {
        "payee_name": "Example Market",
        "category_name": "Groceries",
        "approved": True,
        "date": "2014-02-10",
        "amount": -45000,
    },
]


# -- Prompt building --


def test_prompt_includes_transaction_details():
    prompt = build_categorization_prompt(
        SAMPLE_TRANSACTION, SAMPLE_CATEGORIES, PAST_TRANSACTIONS
    )
    assert "Example Cafe" in prompt
    assert "$4.50" in prompt
    assert "2014-03-10" in prompt


def test_prompt_includes_available_categories():
    prompt = build_categorization_prompt(
        SAMPLE_TRANSACTION, SAMPLE_CATEGORIES, PAST_TRANSACTIONS
    )
    assert "Dining Out" in prompt
    assert "Groceries" in prompt
    assert "Vacation" in prompt


def test_prompt_does_not_include_category_ids():
    prompt = build_categorization_prompt(
        SAMPLE_TRANSACTION, SAMPLE_CATEGORIES, PAST_TRANSACTIONS
    )
    assert "c-dining" not in prompt


def test_prompt_includes_matching_history_only():
    prompt = build_categorization_prompt(
        SAMPLE_TRANSACTION, SAMPLE_CATEGORIES, PAST_TRANSACTIONS
    )
    assert "2014-02-15" in prompt
    assert "Example Market" not in prompt


def test_prompt_handles_no_matching_history():
    txn = {**SAMPLE_TRANSACTION, "payee_name": "New Place"}
    prompt = build_categorization_prompt(txn, SAMPLE_CATEGORIES, PAST_TRANSACTIONS)
    assert "no matching past transactions" in prompt


def test_prompt_includes_amazon_items_when_present():
    """Enriched Amazon transactions carry item names the LLM should see."""
    txn = {**SAMPLE_TRANSACTION, "amazon_items": "USB-C cable; Dish soap"}
    prompt = build_categorization_prompt(txn, SAMPLE_CATEGORIES, PAST_TRANSACTIONS)
    assert "USB-C cable" in prompt
    assert "Dish soap" in prompt


def test_prompt_includes_amazon_order_candidates_when_present():
    """When the exact order match failed, the near-miss candidates are shown so
    the model can still reason about what was bought."""
    txn = {
        **SAMPLE_TRANSACTION,
        "amazon_candidates": ["2015-05-13: $8.40 — Socks; Belt"],
    }
    prompt = build_categorization_prompt(txn, SAMPLE_CATEGORIES, PAST_TRANSACTIONS)
    assert "Socks" in prompt
    assert "2015-05-13: $8.40" in prompt


# -- Response parsing --


def test_parse_category_response():
    result = parse_llm_response("CATEGORY: Dining Out")
    assert result == {"action": "categorize", "category_name": "Dining Out"}


def test_parse_question_response():
    result = parse_llm_response("QUESTION: Are you currently travelling?")
    assert result == {"action": "ask", "question": "Are you currently travelling?"}


def test_parse_category_with_surrounding_text():
    text = "Based on history, this should be dining.\nCATEGORY: Dining Out\nHope that helps!"
    result = parse_llm_response(text)
    assert result == {"action": "categorize", "category_name": "Dining Out"}


def test_parse_unclear_response():
    result = parse_llm_response("I'm not sure what to do with this one.")
    assert result["action"] == "unclear"
    assert "not sure" in result["raw"]


def test_parse_uncertain_response():
    """The second pass can explicitly decline with a reason to surface."""
    result = parse_llm_response(
        "UNCERTAIN: could be lodging or dining; no nearby trip charges to tell"
    )
    assert result["action"] == "uncertain"
    assert "lodging or dining" in result["reason"]


# -- Category resolution --


def test_resolve_category_exact_match():
    cat = resolve_category("Dining Out", SAMPLE_CATEGORIES)
    assert cat["id"] == "c-dining"


def test_resolve_category_case_insensitive():
    cat = resolve_category("dining out", SAMPLE_CATEGORIES)
    assert cat["id"] == "c-dining"


def test_resolve_category_strips_group_prefix():
    cat = resolve_category("Lifestyle: Dining Out", SAMPLE_CATEGORIES)
    assert cat["id"] == "c-dining"


def test_resolve_category_strips_group_prefix_with_emoji():
    categories = [{"id": "c1", "name": "🪴 Hobbies", "group_name": "Wants"}]
    cat = resolve_category("Wants: 🪴 Hobbies", categories)
    assert cat["id"] == "c1"


def test_resolve_category_preserves_leading_special_chars():
    """Category names like ' Example Subscriptions' have a leading special char."""
    categories = [{"id": "c1", "name": " Example Subscriptions", "group_name": "Bills"}]
    cat = resolve_category("Bills:  Example Subscriptions", categories)
    assert cat["id"] == "c1"


def test_resolve_category_falls_back_to_normalized_match():
    """LLM can't reproduce special emoji — falls back to text-only match."""
    categories = [{"id": "c1", "name": "🍽️ Dining out", "group_name": "Wants"}]
    # LLM drops the emoji entirely
    cat = resolve_category("Wants: Dining out", categories)
    assert cat["id"] == "c1"


def test_resolve_category_normalized_match_with_different_emoji():
    """LLM outputs wrong/missing emoji — still matches on text."""
    categories = [{"id": "c1", "name": " Example Subscriptions", "group_name": "Bills"}]
    # LLM can't reproduce the Apple logo char, just outputs "Example Subscriptions"
    cat = resolve_category("Bills: Example Subscriptions", categories)
    assert cat["id"] == "c1"


def test_resolve_category_returns_none_for_unknown():
    assert resolve_category("Nonexistent", SAMPLE_CATEGORIES) is None


# -- Categorizer integration (injected LLM function) --


def test_suggest_returns_category_when_llm_is_confident():
    fake_llm = lambda prompt: "CATEGORY: Dining Out"
    categorizer = Categorizer(run_llm=fake_llm)

    result = categorizer.suggest(
        SAMPLE_TRANSACTION, SAMPLE_CATEGORIES, PAST_TRANSACTIONS
    )
    assert result == {"action": "categorize", "category_name": "Dining Out"}


def test_suggest_returns_question_when_llm_is_uncertain():
    fake_llm = lambda prompt: "QUESTION: Are you on vacation right now?"
    categorizer = Categorizer(run_llm=fake_llm)

    result = categorizer.suggest(
        SAMPLE_TRANSACTION, SAMPLE_CATEGORIES, PAST_TRANSACTIONS
    )
    assert result == {"action": "ask", "question": "Are you on vacation right now?"}


def test_guess_prompt_forces_a_choice_and_includes_amount_signal():
    """The second-pass prompt must demand a category and lean on the amount."""
    txn = {**SAMPLE_TRANSACTION, "payee_name": "Uber", "amount": -8500}
    prompt = build_guess_prompt(txn, SAMPLE_CATEGORIES, PAST_TRANSACTIONS)

    assert "$8.50" in prompt
    assert "must" in prompt.lower()  # forced to pick
    assert "amount" in prompt.lower()  # told to use price as a signal
    assert "QUESTION:" not in prompt  # no escape hatch to defer


def test_guess_prompt_includes_nearby_charges_and_day_context():
    """Opus should see surrounding charges + day-of-week to spot incidentals."""
    txn = {
        "id": "t1",
        "payee_name": "Example Inn",
        "amount": -3750,
        "date": "2015-06-06",  # a Saturday
        "memo": "",
        "account_name": "Checking",
    }
    past = [
        {  # a big lodging charge two days earlier — the incidental's context
            "id": "t0",
            "payee_name": "Example Inn",
            "amount": -400000,  # $400.00
            "category_name": "Vacation",
            "date": "2015-06-04",
        },
        {  # far away in time — must NOT show up as "nearby"
            "id": "tx",
            "payee_name": "Example Market",
            "amount": -3000,
            "category_name": "Groceries",
        "approved": True,
            "date": "2015-04-01",
        },
    ]
    prompt = build_guess_prompt(txn, SAMPLE_CATEGORIES, past)

    assert "Saturday" in prompt  # day-of-week surfaced
    assert "weekend" in prompt.lower()  # ...flagged as a weekend
    assert "$400.00" in prompt  # the nearby big lodging charge is shown
    assert "Example Market" not in prompt  # the far-away charge is excluded


def test_guess_prompt_allows_declining_when_genuinely_stuck():
    """The prompt must offer an explicit decline path (UNCERTAIN) as a last resort."""
    txn = {**SAMPLE_TRANSACTION, "payee_name": "Mystery Co"}
    prompt = build_guess_prompt(txn, SAMPLE_CATEGORIES, PAST_TRANSACTIONS)
    assert "UNCERTAIN:" in prompt


def test_guess_prompt_forbids_guessing_from_payee_name_alone():
    """An opaque charge with no usable signal must be declined, not guessed from
    the payee. A bare marketplace name is not enough evidence."""
    txn = {**SAMPLE_TRANSACTION, "payee_name": "Amazon", "amount": -6250}
    prompt = build_guess_prompt(txn, SAMPLE_CATEGORIES, PAST_TRANSACTIONS)
    assert "payee name alone" in prompt.lower()


def test_guess_prompt_instructs_web_research_for_unknown_merchants():
    """The second pass should tell the model to look up unfamiliar merchants."""
    txn = {**SAMPLE_TRANSACTION, "payee_name": "SQ *EXAMPLE SHOP", "amount": -2250}
    prompt = build_guess_prompt(txn, SAMPLE_CATEGORIES, PAST_TRANSACTIONS)

    lower = prompt.lower()
    assert "search" in lower  # told to search
    assert "web" in lower  # ...the web
    assert "merchant" in lower  # ...to identify an unknown merchant


def test_run_claude_enables_requested_tools(monkeypatch):
    """_run_claude wires --allowedTools and timeout through to the CLI."""
    import subprocess as sp

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["timeout"] = kwargs.get("timeout")
        return sp.CompletedProcess(cmd, 0, stdout="CATEGORY: Dining Out", stderr="")

    monkeypatch.setattr(
        "ynab_categorizer.categorizer.subprocess.run", fake_run
    )

    out = _run_claude(
        "hi", model="opus", allowed_tools=["WebSearch", "WebFetch"], timeout=180
    )

    assert out == "CATEGORY: Dining Out"
    assert "--allowedTools" in captured["cmd"]
    assert "WebSearch" in captured["cmd"]
    assert "WebFetch" in captured["cmd"]
    assert "opus" in captured["cmd"]
    assert captured["timeout"] == 180


def test_run_claude_passes_prompt_via_stdin_not_argv(monkeypatch):
    """The prompt must go through stdin: --allowedTools is variadic and would
    otherwise swallow a trailing prompt argument as a tool name."""
    import subprocess as sp

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return sp.CompletedProcess(cmd, 0, stdout="CATEGORY: Dining Out", stderr="")

    monkeypatch.setattr(
        "ynab_categorizer.categorizer.subprocess.run", fake_run
    )

    _run_claude("PROMPT-TEXT", allowed_tools=["WebSearch", "WebFetch"])

    assert captured["input"] == "PROMPT-TEXT"  # passed via stdin
    assert "PROMPT-TEXT" not in captured["cmd"]  # never as a trailing arg


def test_default_second_pass_researches_web_with_opus(monkeypatch):
    """The out-of-the-box second pass calls Opus with web tools enabled."""
    import subprocess as sp

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return sp.CompletedProcess(cmd, 0, stdout="CATEGORY: Dining Out", stderr="")

    monkeypatch.setattr(
        "ynab_categorizer.categorizer.subprocess.run", fake_run
    )

    txn = {**SAMPLE_TRANSACTION, "payee_name": "SQ *EXAMPLE SHOP", "amount": -2250}
    categorizer = Categorizer()  # default guess_llm — no injection

    result = categorizer.guess(txn, SAMPLE_CATEGORIES, PAST_TRANSACTIONS)

    assert result == {"action": "categorize", "category_name": "Dining Out"}
    assert "opus" in captured["cmd"]
    assert "WebSearch" in captured["cmd"]
    assert "WebFetch" in captured["cmd"]


def test_guess_returns_category_from_guess_llm():
    """guess() uses the (smarter) guess_llm, not the default run_llm."""
    txn = {**SAMPLE_TRANSACTION, "payee_name": "Uber", "amount": -8500}
    guess_llm = lambda prompt: "CATEGORY: Dining Out"
    categorizer = Categorizer(
        run_llm=lambda p: "QUESTION: should not be used", guess_llm=guess_llm
    )

    result = categorizer.guess(txn, SAMPLE_CATEGORIES, PAST_TRANSACTIONS)

    assert result == {"action": "categorize", "category_name": "Dining Out"}


def test_history_excludes_self_unapproved_and_deleted():
    history = [
        {**SAMPLE_TRANSACTION, "approved": True, "category_name": "Self"},
        {**SAMPLE_TRANSACTION, "id": "t2", "approved": False, "category_name": "Unreviewed"},
        {**SAMPLE_TRANSACTION, "id": "t3", "approved": True, "deleted": True, "category_name": "Deleted"},
    ]
    prompt = build_categorization_prompt(SAMPLE_TRANSACTION, SAMPLE_CATEGORIES, history)
    assert "no matching past transactions" in prompt


def test_category_resolution_uses_group_and_rejects_ambiguity():
    cats = [{"id": "a", "group_name": "Needs", "name": "Travel"},
            {"id": "b", "group_name": "Wants", "name": "Travel"}]
    assert resolve_category("Wants: Travel", cats)["id"] == "b"
    assert resolve_category("Travel", cats) is None
    assert resolve_category("Unknown: Travel", cats) is None
    assert resolve_category("b", cats)["id"] == "b"
    assert resolve_category("Travel", [dict(cats[0], name="🚆 Travel"), dict(cats[1], name="✈ Travel")]) is None


def test_model_and_timeout_overrides(monkeypatch):
    calls = []
    monkeypatch.setattr("ynab_categorizer.categorizer._run_claude",
                        lambda prompt, **kw: calls.append(kw) or "CATEGORY: Dining Out")
    c = Categorizer(model="fast", guess_model="slow", timeout=12, guess_timeout=34)
    c.suggest(SAMPLE_TRANSACTION, SAMPLE_CATEGORIES, [])
    c.guess(SAMPLE_TRANSACTION, SAMPLE_CATEGORIES, [])
    assert calls[0] == {"model": "fast", "timeout": 12}
    assert calls[1]["model"] == "slow"
    assert calls[1]["timeout"] == 34


def test_builtin_tools_are_explicitly_limited(monkeypatch):
    import subprocess
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="CATEGORY: Dining Out", stderr="")
    monkeypatch.setattr("ynab_categorizer.categorizer.subprocess.run", run)
    _run_claude("test")
    _run_claude("test", allowed_tools=["WebSearch", "WebFetch"])
    assert calls[0][calls[0].index("--tools") + 1] == ""
    assert calls[1][calls[1].index("--tools") + 1] == "WebSearch,WebFetch"


def test_claude_environment_excludes_unrelated_credentials(monkeypatch):
    import subprocess
    secret_keys = ["YNAB_API_TOKEN", "AMAZON_PASSWORD", "AMAZON_USERNAME",
                   "AMAZON_OTP_SECRET_KEY", "GMAIL_APP_PASSWORD"]
    for key in secret_keys:
        monkeypatch.setenv(key, "test-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    def run(cmd, **kwargs):
        assert not set(secret_keys).intersection(kwargs["env"])
        assert kwargs["env"]["PATH"] == "/usr/bin"
        return subprocess.CompletedProcess(cmd, 0, stdout="CATEGORY: Dining Out", stderr="")
    monkeypatch.setattr("ynab_categorizer.categorizer.subprocess.run", run)
    _run_claude("test")
