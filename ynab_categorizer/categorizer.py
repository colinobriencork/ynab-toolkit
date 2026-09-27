"""LLM-assisted transaction categorizer using the claude CLI."""

import os
import re
import subprocess
from datetime import datetime

# How many days either side of a transaction count as "around the same time" —
# the window the second pass uses to spot incidentals (e.g. a meal next to a
# lodging charge on the same trip).
NEARBY_WINDOW_DAYS = 3


def _parse_date(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _day_context(transaction: dict) -> str:
    """A '- Day: Saturday (weekend)' line, or '' if the date can't be parsed."""
    d = _parse_date(transaction.get("date", ""))
    if not d:
        return ""
    day = d.strftime("%A")
    kind = "weekend" if d.weekday() >= 5 else "weekday"
    return f"- Day: {day} ({kind})\n"


def _nearby_transactions(transaction: dict, past_transactions: list[dict]) -> list[dict]:
    """Charges within NEARBY_WINDOW_DAYS of this one (excluding itself), date-sorted."""
    txn_date = _parse_date(transaction.get("date", ""))
    if not txn_date:
        return []

    txn_id = transaction.get("id")
    nearby = []
    seen = set()
    for t in past_transactions:
        if t.get("deleted") or (t.get("id") and t["id"] in seen):
            continue
        if t.get("id"):
            seen.add(t["id"])
        if txn_id and t.get("id") == txn_id:
            continue
        d = _parse_date(t.get("date", ""))
        if d and abs((d - txn_date).days) <= NEARBY_WINDOW_DAYS:
            nearby.append((d, t))
    nearby.sort(key=lambda pair: pair[0])
    return [t for _, t in nearby]


def build_categorization_prompt(
    transaction: dict,
    categories: list[dict],
    past_transactions: list[dict],
) -> str:
    cat_list = "\n".join(
        f"- {c['group_name']}: {c['name']}" for c in categories
    )

    similar = [
        t
        for t in past_transactions
        if t.get("payee_name") and transaction.get("payee_name")
        and t["payee_name"].lower() == transaction["payee_name"].lower()
        and t.get("category_name")
        and t.get("approved") is True
        and not t.get("deleted")
        and (not transaction.get("id") or t.get("id") != transaction["id"])
    ]
    recent_similar = sorted(similar, key=lambda t: t["date"], reverse=True)[:10]

    history_lines = ""
    if recent_similar:
        history_lines = "\n".join(
            f"- {t['date']}: {t['payee_name']} -> {t['category_name']} "
            f"(${t['amount'] / -1000:.2f})"
            for t in recent_similar
        )
    else:
        history_lines = "(no matching past transactions)"

    amount = transaction["amount"] / -1000

    items_line = ""
    if transaction.get("amazon_items"):
        items_line = f"\n- Items: {transaction['amazon_items']}"

    candidates_line = ""
    if transaction.get("amazon_candidates"):
        listed = "\n".join(f"  - {c}" for c in transaction["amazon_candidates"])
        candidates_line = (
            "\n- Possible Amazon orders near this date (no single order matched the "
            "amount exactly, but one of these is likely this charge — amounts can "
            "differ due to tax, partial shipments, or orders charged together):\n"
            f"{listed}"
        )

    return f"""You are helping categorize a YNAB bank transaction.

## Transaction to categorize
- Payee: {transaction.get('payee_name', 'Unknown')}
- Amount: ${amount:.2f}
- Date: {transaction['date']}
- Memo: {transaction.get('memo', '')}
- Account: {transaction.get('account_name', '')}{items_line}{candidates_line}

## Past transactions for this payee
{history_lines}

## Available categories
{cat_list}

Based on the history and context, suggest the best category for this transaction.

If you are confident (same payee always goes to the same category), respond with EXACTLY:
CATEGORY: <category name>

The category name must exactly match one from the list above.

If you need more context to decide (e.g., the user might be travelling, or this payee has been categorized differently before), respond with:
QUESTION: <your question to the user>

Be concise. Only ask a question if there is genuine ambiguity."""


def build_guess_prompt(
    transaction: dict,
    categories: list[dict],
    past_transactions: list[dict],
) -> str:
    """The second-pass prompt: richer context + research, with a decline path.

    Reuses build_categorization_prompt's context, then adds the day-of-week and the
    surrounding charges, and tells the model to research opaque merchants and reason
    from the evidence (amount, history, nearby orders). Unlike the first pass it
    never asks the user a question: it either commits to a category or, when it
    genuinely can't tell, declines with UNCERTAIN so the charge is surfaced rather
    than blind-guessed.
    """
    base = build_categorization_prompt(transaction, categories, past_transactions)
    # Drop the original instructions (everything from the first directive onward)
    # and splice in the surrounding-context sections + forced-but-declinable guess.
    context, _, _ = base.partition("Based on the history and context")

    day_line = _day_context(transaction)
    when_block = f"\n## When\n{day_line}\n" if day_line else ""

    nearby = _nearby_transactions(transaction, past_transactions)
    if nearby:
        nearby_lines = "\n".join(
            f"- {t['date']}: {t.get('payee_name', 'Unknown')} -> "
            f"{t.get('category_name') if t.get('approved') is True else 'unreviewed'} (${t['amount'] / -1000:.2f})"
            for t in nearby
        )
        nearby_block = (
            f"## Other charges around the same time (within "
            f"{NEARBY_WINDOW_DAYS} days)\n{nearby_lines}\n\n"
        )
    else:
        nearby_block = ""

    return f"""{context}{when_block}{nearby_block}This transaction could not be confidently categorized on the first pass. Make your single best determination — do NOT ask the user a question.

Really dig into what this charge is, using whatever signals fit:
- If the payee is unfamiliar, abbreviated, or opaque (a processor prefix like "SQ *" / "TST*", an unknown subscription, a name you don't recognize), use web search and fetch pages to identify the actual merchant (the real business) and what it sells.
- Weigh the surrounding charges and the day of week. A charge that sits alongside related ones often takes its meaning from them — e.g. a smaller charge next to a much larger one on the same trip is more likely an incidental (a meal or activity) than a repeat of the big-ticket item.
- Use the amount as a signal (e.g. a small Uber charge is likely a meal; a larger one a ride).
- Lean on the transaction history and any item details above — that is the user's own data.

These are illustrations, not rules: reason from the actual evidence in front of you.

Crucially, do NOT guess from the payee name alone. An opaque merchant — for example a bare "Amazon" charge with no item details — could be almost anything, so a category chosen only because it "sounds like" the payee (e.g. defaulting every Amazon charge to streaming) is worse than admitting you don't know.

Make a confident call ONLY when real evidence supports one: item details, what web research reveals about the merchant, the surrounding charges, or a reliable amount-based signal for a known service. If you have none of those — you cannot enrich it, research it, or reason it from context — you MUST decline rather than pick a plausible-sounding category. Respond with EXACTLY:
UNCERTAIN: <one line: what you'd need to know to decide>

When real evidence does support a category, respond with EXACTLY one final line:
CATEGORY: <category name>

The category name must exactly match one from the list above."""


DEFAULT_MODEL = "haiku"
GUESS_MODEL = "opus"
# Tools the second-pass guess may use to research an unfamiliar merchant.
# Limit built-in tools to web research. This does not sandbox inherited CLI hooks
# or configuration; run only with trusted Claude configuration.
GUESS_TOOLS = ["WebSearch", "WebFetch"]
DEFAULT_TIMEOUT = 60
# Web research is multi-step (search -> fetch -> reason), so it needs more headroom.
GUESS_TIMEOUT = 180


def _run_claude(
    prompt: str,
    model: str = DEFAULT_MODEL,
    allowed_tools: list[str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Call the claude CLI with --print flag and return the response text.

    When allowed_tools is given, those tools are pre-approved so the CLI can run
    agentically (search the web, fetch pages) before producing its final answer.
    """
    cmd = ["claude", "--print", "--model", model, "--tools", ",".join(allowed_tools or [])]
    if allowed_tools:
        cmd += ["--allowedTools", *allowed_tools]

    # Feed the prompt over stdin, never as a trailing arg: --allowedTools is a
    # variadic flag and would otherwise swallow the prompt as a tool name, leaving
    # the CLI to hang waiting on stdin until the timeout fires.
    sensitive = {"YNAB_API_TOKEN", "AMAZON_PASSWORD", "AMAZON_USERNAME",
                 "AMAZON_OTP_SECRET_KEY", "GMAIL_APP_PASSWORD"}
    env = {key: value for key, value in os.environ.items() if key not in sensitive}
    result = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {result.stderr}")
    return result.stdout.strip()


def _normalize(s: str) -> str:
    """Normalize a string for fuzzy comparison: lowercase, alphanumeric + spaces only."""
    return re.sub(r"[^\w\s]", "", s, flags=re.UNICODE).strip().lower()


def resolve_category(name: str, categories: list[dict]) -> dict | None:
    """Find a category by name.

    Handles plain names, group-prefixed names ("Wants: 🪴 Hobbies"),
    and emoji/special char mismatches from LLM output.
    """
    name = name.strip()
    # IDs are useful for deterministic rules; model prompts use display names.
    ids = [cat for cat in categories if cat.get("id") == name]
    if len(ids) == 1:
        return ids[0]
    group = None
    if ": " in name:
        group, _, name = name.partition(": ")
    pool = categories
    if group is not None:
        pool = [cat for cat in pool if _normalize(cat.get("group_name", "")) == _normalize(group)]
    for normalize in (lambda s: s.strip().casefold(), _normalize):
        matches = [cat for cat in pool if normalize(cat["name"]) == normalize(name)]
        if matches:
            return matches[0] if len(matches) == 1 else None
    return None


class Categorizer:
    def __init__(self, run_llm=None, guess_llm=None, *, model=DEFAULT_MODEL,
                 guess_model=GUESS_MODEL, timeout=DEFAULT_TIMEOUT,
                 guess_timeout=GUESS_TIMEOUT):
        if timeout <= 0 or guess_timeout <= 0:
            raise ValueError("Model timeouts must be positive")
        self._run_llm = run_llm or (lambda p: _run_claude(p, model=model, timeout=timeout))
        self._guess_llm = guess_llm or (
            lambda p: _run_claude(
                p,
                model=guess_model,
                allowed_tools=GUESS_TOOLS,
                timeout=guess_timeout,
            )
        )

    def suggest(
        self,
        transaction: dict,
        categories: list[dict],
        past_transactions: list[dict],
    ) -> dict:
        """Returns {'action': 'categorize', 'category_name': ...} or {'action': 'ask', 'question': ...}."""
        prompt = build_categorization_prompt(
            transaction, categories, past_transactions
        )
        text = self._run_llm(prompt)
        return parse_llm_response(text)

    def guess(
        self,
        transaction: dict,
        categories: list[dict],
        past_transactions: list[dict],
    ) -> dict:
        """Second-pass forced best guess, using the smarter guess_llm (Opus)."""
        prompt = build_guess_prompt(transaction, categories, past_transactions)
        text = self._guess_llm(prompt)
        return parse_llm_response(text)


def parse_llm_response(text: str) -> dict:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("CATEGORY:"):
            return {"action": "categorize", "category_name": line.split(":", 1)[1].strip()}
        if line.startswith("QUESTION:"):
            return {"action": "ask", "question": line.split(":", 1)[1].strip()}
        if line.startswith("UNCERTAIN:"):
            return {"action": "uncertain", "reason": line.split(":", 1)[1].strip()}
    # Fallback — treat entire response as needing manual review
    return {"action": "unclear", "raw": text}
