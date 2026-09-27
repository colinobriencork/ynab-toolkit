"""Spend-watch: warn before a month's spending blows past monthly income.

For every budget with a configured income (``MONTHLY_INCOME_<NAME>`` in .env,
in dollars), this looks at the current month's category activity and reports:

- total spending vs the income ceiling (warning at 80% by default, over at 100%)
- categories already overspent
- categories that have burned through most of their assigned amount
- savings contribution targets compared across budgets using configured keywords

The analysis is pure; the driver prints the report and only emails it (via
Gmail SMTP) when called with send=True and GMAIL_ADDRESS/GMAIL_APP_PASSWORD are
configured.
"""

import html as html_mod
import re
import smtplib
from datetime import date
from decimal import Decimal

from .config import budget_options
from email.message import EmailMessage

GMAIL_SMTP = "smtp.gmail.com"
DEFAULT_WARN_RATIO = 0.8
DEFAULT_NEAR_LIMIT_RATIO = 0.9

# Groups that never count as spending: card payments are transfers (the spend
# already hit a category) and the internal group holds the income inflow.
NON_SPENDING_GROUPS = {"Credit Card Payments", "Internal Master Category"}

# Comparisons are opt-in: category choices belong in private configuration.
SAVINGS_PAIRS = []


# --- analysis (pure) -------------------------------------------------------


def _spending_cats(categories):
    return [c for c in categories if c.get("group_name") not in NON_SPENDING_GROUPS]


def total_spent(categories) -> int:
    """Net spending this month in milliunits (positive = money out)."""
    return -sum(c.get("activity") or 0 for c in _spending_cats(categories))


def spending_status(spent: int, income: int, warn_ratio: float = DEFAULT_WARN_RATIO):
    """Where this month's spending sits against the income ceiling."""
    if spent > income:
        level = "over"
    elif spent >= income * warn_ratio:
        level = "warning"
    else:
        level = "ok"
    return {"spent": spent, "income": income, "level": level,
            "headroom": income - spent}


def overspent_categories(categories):
    """Categories already in the red this month, worst first."""
    over = [c for c in _spending_cats(categories) if (c.get("balance") or 0) < 0]
    return sorted(over, key=lambda c: c["balance"])


def approaching_categories(categories, ratio: float = DEFAULT_NEAR_LIMIT_RATIO):
    """Categories with money still left but most of it spent. Overspent ones are
    reported separately, and a balance of exactly zero is a bill paid in full,
    not a category in danger."""
    near = []
    for c in _spending_cats(categories):
        budgeted = c.get("budgeted") or 0
        spent = -(c.get("activity") or 0)
        if budgeted > 0 and (c.get("balance") or 0) > 0 and spent >= budgeted * ratio:
            near.append(c)
    return near


def _matching(categories, keywords):
    return [c for c in categories
            if any(k in c["name"].lower() for k in keywords)
            and not (c.get("hidden") or c.get("deleted"))]


def ytd_contributions(months_categories, keywords):
    """What went into (and out of) matching categories this year.

    ``months_categories`` is one category list per month so far, oldest first.
    "spent" is money that left the category on the thing it's for — the balance
    alone would undercount whoever already used their savings. Returns None
    when no month has a matching category.
    """
    assigned = spent = 0
    balance = None
    for categories in months_categories:
        matches = _matching(categories, keywords)
        if not matches:
            continue
        assigned += sum(c.get("budgeted") or 0 for c in matches)
        spent += -sum(c.get("activity") or 0 for c in matches)
        balance = sum(c.get("balance") or 0 for c in matches)
    if balance is None:
        return None
    return {"assigned": assigned, "spent": spent, "balance": balance}


def savings_parity(budgets, pairs=SAVINGS_PAIRS):
    """Compare monthly savings targets across budgets.

    ``budgets`` is a list of (budget_label, categories). Each row gives the
    summed ``goal_target`` of matching categories per budget, or None when a
    budget has no matching category at all.
    """
    rows = []
    for label, keywords in pairs:
        targets = {}
        for budget_label, categories in budgets:
            matches = _matching(categories, keywords)
            targets[budget_label] = (
                sum(c.get("goal_target") or 0 for c in matches) if matches else None
            )
        rows.append({"label": label, "targets": targets})
    return rows


# --- report ----------------------------------------------------------------


def _dollars(milliunits) -> str:
    return f"${milliunits / 1000:,.2f}"


def _budget_section(name, status, overspent, near) -> list[str]:
    pct = status["spent"] / status["income"] * 100 if status["income"] else 0
    tag = {"ok": "OK", "warning": "WARNING", "over": "OVER INCOME"}[status["level"]]
    lines = [
        f"{name} — {tag}",
        f"  spent {_dollars(status['spent'])} of {_dollars(status['income'])} "
        f"monthly income ({pct:.0f}%), {_dollars(status['headroom'])} headroom",
    ]
    if overspent:
        lines.append("  overspent:")
        lines += [f"    {_dollars(c['balance'])}  {c['name']}" for c in overspent]
    if near:
        lines.append("  near their limit:")
        lines += [
            f"    {c['name']}: spent {_dollars(-c['activity'])} "
            f"of {_dollars(c['budgeted'])} assigned"
            for c in near
        ]
    return lines


def _parity_section(target_rows, ytd_by_budget, budget_names, year) -> list[str]:
    lines = [f"Savings parity ({year} so far):"]
    for row in target_rows:
        lines.append(f"  {row['label']}:")
        width = max(len(n) for n in budget_names)
        for name in budget_names:
            target = row["targets"][name]
            ytd = ytd_by_budget[name].get(row["label"])
            if target is None and ytd is None:
                lines.append(f"    {name:<{width}}: — no category")
                continue
            target_part = (f"{_dollars(target)}/mo target"
                           if target is not None else "no monthly target")
            ytd_part = (
                f"put in {_dollars(ytd['assigned'])}, "
                f"spent {_dollars(ytd['spent'])}, left {_dollars(ytd['balance'])}"
                if ytd else "nothing this year"
            )
            lines.append(f"    {name:<{width}}: {target_part} | {ytd_part}")
    return lines


# --- html report -----------------------------------------------------------

LEVEL_COLORS = {"ok": "#15803d", "warning": "#b45309", "over": "#b91c1c"}
LEVEL_TAGS = {"ok": "OK", "warning": "WARNING", "over": "OVER INCOME"}


def _esc(text) -> str:
    return html_mod.escape(str(text))


def render_html(headline, budgets, parity) -> str:
    """The report as a self-contained HTML email body (inline styles only —
    Gmail strips style sheets)."""
    out = ['<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,'
           'Arial,sans-serif;color:#1f2430;max-width:680px;line-height:1.5">',
           f'<h2 style="margin:0 0 16px">{_esc(headline)}</h2>']

    for b in budgets:
        status, color = b["status"], LEVEL_COLORS[b["status"]["level"]]
        pct = status["spent"] / status["income"] * 100 if status["income"] else 0
        out.append(
            f'<h3 style="margin:18px 0 4px">{_esc(b["name"])} '
            f'<span style="color:{color};font-size:0.85em">'
            f'{LEVEL_TAGS[status["level"]]}</span></h3>'
            f'<p style="margin:0 0 6px">spent <b>{_dollars(status["spent"])}</b> of '
            f'{_dollars(status["income"])} monthly income ({pct:.0f}%), '
            f'{_dollars(status["headroom"])} headroom</p>'
        )
        if b["overspent"]:
            items = "".join(
                f'<li>{_dollars(c["balance"])} &nbsp;{_esc(c["name"])}</li>'
                for c in b["overspent"]
            )
            out.append(f'<p style="margin:6px 0 2px">overspent:</p>'
                       f'<ul style="margin:0;color:{LEVEL_COLORS["over"]}">{items}</ul>')
        if b["near"]:
            items = "".join(
                f'<li>{_esc(c["name"])}: spent {_dollars(-c["activity"])} '
                f'of {_dollars(c["budgeted"])} assigned</li>'
                for c in b["near"]
            )
            out.append(f'<p style="margin:6px 0 2px">near their limit:</p>'
                       f'<ul style="margin:0;color:{LEVEL_COLORS["warning"]}">{items}</ul>')

    if parity:
        cell = 'style="padding:5px 10px;border-bottom:1px solid #e5e7eb;text-align:right"'
        left = 'style="padding:5px 10px;border-bottom:1px solid #e5e7eb;text-align:left"'
        head = ("<tr>" + f"<th {left}></th>"
                + "".join(f'<th {cell}>{_esc(n)}: target/mo</th>'
                          f'<th {cell}>put in</th><th {cell}>spent</th>'
                          f'<th {cell}>left</th>' for n in parity["names"])
                + "</tr>")
        rows = []
        for row in parity["target_rows"]:
            cells = [f'<td {left}><b>{_esc(row["label"])}</b></td>']
            for name in parity["names"]:
                target = row["targets"][name]
                ytd = parity["ytd_by_budget"][name].get(row["label"])
                if target is None and ytd is None:
                    cells.append(f'<td {cell} colspan="4">— no category</td>')
                    continue
                cells.append(f'<td {cell}>'
                             f'{_dollars(target) if target is not None else "—"}</td>')
                if ytd:
                    cells += [f'<td {cell}>{_dollars(ytd["assigned"])}</td>',
                              f'<td {cell}>{_dollars(ytd["spent"])}</td>',
                              f'<td {cell}>{_dollars(ytd["balance"])}</td>']
                else:
                    cells.append(f'<td {cell} colspan="3">nothing this year</td>')
            rows.append("<tr>" + "".join(cells) + "</tr>")
        out.append(
            f'<h3 style="margin:22px 0 6px">Savings parity ({parity["year"]} so far)</h3>'
            '<table style="border-collapse:collapse;font-size:0.92em">'
            + head + "".join(rows) + "</table>"
        )

    out.append("</div>")
    return "".join(out)


# --- email -----------------------------------------------------------------


def send_email(subject: str, body: str, config: dict, html: str = None) -> bool:
    """Email the report via Gmail. Returns False (with a hint) if unconfigured."""
    address = config.get("GMAIL_ADDRESS")
    password = config.get("GMAIL_APP_PASSWORD")
    if not address or not password:
        print("Email not sent: set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env "
              "(app password from myaccount.google.com/apppasswords).")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = address
    msg["To"] = config.get("SPEND_WATCH_RECIPIENTS", address)
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")

    with smtplib.SMTP_SSL(GMAIL_SMTP) as server:
        server.login(address, password)
        server.send_message(msg)
    return True


# --- driver ----------------------------------------------------------------


def _income_key(budget_name: str) -> str:
    words = re.findall(r"[A-Za-z]+", budget_name)
    first = words[0] if words else budget_name
    return f"MONTHLY_INCOME_{first.upper()}"


def spend_watch(client, config: dict, send: bool = False,
                warn_ratio: float = DEFAULT_WARN_RATIO, today: date = None, budget_selector=None):
    """Build the spending report for every budget with a configured income.

    Prints the report; emails it too when send=True.
    """
    today = today or date.today()
    budgets = []
    watched = []  # (short_name, budget_id, categories) for the parity comparison
    worst = "ok"
    severity = {"ok": 0, "warning": 1, "over": 2}

    pairs_config = config.get("savings_pairs")
    pairs = [(label, tuple(words)) for label, words in pairs_config.items()] if pairs_config is not None else SAVINGS_PAIRS
    all_budgets = client.get_budgets()
    if budget_selector:
        all_budgets = [b for b in all_budgets if b['id'] == budget_selector or b['name'].casefold() == budget_selector.casefold()]
        if len(all_budgets) != 1:
            raise ValueError("Budget selector must match exactly one budget")
    for budget in all_budgets:
        options = budget_options(config, budget)
        income_dollars = config.get(_income_key(budget["name"]))
        income = options.get("monthly_income")
        if income is None and income_dollars:
            income = int(Decimal(str(income_dollars)) * 1000)
        if not income:
            continue
        if income < 0:
            raise ValueError("Monthly income must be positive")
        short_name = budget["name"]
        categories = client.get_categories(budget["id"])
        status = spending_status(
            total_spent(categories), income, options.get("warn_ratio", warn_ratio)
        )
        if severity[status["level"]] > severity[worst]:
            worst = status["level"]
        budgets.append({
            "name": short_name,
            "status": status,
            "overspent": overspent_categories(categories),
            "near": approaching_categories(categories, options.get("near_limit_ratio", DEFAULT_NEAR_LIMIT_RATIO)),
        })
        watched.append((short_name, budget["id"], categories))

    if not watched:
        print("No budgets watched — set MONTHLY_INCOME_<NAME> in .env "
              "or monthly_income in your budget configuration.")
        return

    parity = None
    if len(watched) > 1 and pairs:
        names = [n for n, _, _ in watched]
        ytd_by_budget = {}
        for name, budget_id, _ in watched:
            year_months = [
                client.get_month(budget_id, f"{today.year}-{m:02d}-01")["categories"]
                for m in range(1, today.month + 1)
            ]
            ytd_by_budget[name] = {
                label: ytd_contributions(year_months, keywords)
                for label, keywords in pairs
            }
        parity = {
            "names": names,
            "year": today.year,
            "target_rows": savings_parity([(n, cats) for n, _, cats in watched], pairs=pairs),
            "ytd_by_budget": ytd_by_budget,
        }

    sections = [
        _budget_section(b["name"], b["status"], b["overspent"], b["near"])
        for b in budgets
    ]
    if parity:
        sections.append(_parity_section(
            parity["target_rows"], parity["ytd_by_budget"],
            parity["names"], parity["year"],
        ))
    body = "\n\n".join("\n".join(s) for s in sections)
    headline = {"ok": "OK: spending within income",
                "warning": "WARNING: spending approaching income",
                "over": "OVER: spending exceeds income"}[worst]
    subject = f"[spend-watch] {headline}"

    print(subject)
    print()
    print(body)

    if send:
        html = render_html(headline, budgets, parity)
        if send_email(subject, body, config, html=html):
            print("\nReport emailed.")
