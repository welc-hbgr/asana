#!/usr/bin/env python3
"""Wysyła mailem raport KPI i budżetów per portfolio.

Dane raportu leżą w pliku JSON (katalog `reports/`) — ten skrypt tylko je
renderuje i wysyła. Rozdzielenie jest celowe: dane pochodzą z konektora
Optmyzr, do którego GitHub Actions nie ma dostępu, więc JSON powstaje po
stronie Claude, a Actions odpowiada wyłącznie za wysyłkę.

HTML jest pisany pod klientów pocztowych, nie pod przeglądarkę: style są
inline, bez zmiennych CSS, bez media queries i bez motywu ciemnego — Gmail
i Outlook wycinają arkusze stylów, a niezastąpiona zmienna CSS zostawia
tekst w kolorze tła.

Zmienne środowiskowe (te same, co pozostałe raporty w repo):
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_FROM, MAIL_TO
  SMTP_STARTTLS ("1"/"0", domyślnie 1)
  REPORT_JSON  – ścieżka do pliku z danymi (domyślnie reports/latest.json)
  DRY_RUN      – "1" wypisuje raport na stdout zamiast wysyłać
"""

from __future__ import annotations

import json
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# --- Formatowanie liczb ----------------------------------------------------

NBSP = " "


def money(value: float | None, currency: str = "zł") -> str:
    if value is None:
        return "—"
    return f"{int(round(value)):,}".replace(",", NBSP) + NBSP + currency


def pct(value: float | None, decimals: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{decimals}f}".replace(".", ",") + "%"


def delta(value: float | None) -> str:
    """Zmiana WoW ze znakiem; None = brak bazy do porównania."""
    if value is None:
        return "n/d"
    sign = "+" if value > 0 else ("−" if value < 0 else "")
    return sign + f"{abs(value):.1f}".replace(".", ",") + "%"


def delta_color(value: float | None) -> str:
    if value is None or abs(value) < 1:
        return "#6B7C86"
    return "#146B4C" if value > 0 else "#A32B22"


def count(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{int(round(value)):,}".replace(",", NBSP)


def roas(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}".replace(".", ",") + "x"


STATUS = {
    "ok": ("ok", "#146B4C", "#D5E9DF"),
    "over": ("przepalanie", "#A32B22", "#F5DCD8"),
    "under": ("niedowożenie", "#8A6206", "#F2E6C8"),
    "none": ("brak monitora", "#6B7C86", "#EDF1F2"),
}

SEV = {
    "crit": ("Budżet", "#A32B22", "#F5DCD8"),
    "warn": ("Wynik", "#8A6206", "#F2E6C8"),
    "info": ("Dane", "#0B6E7A", "#D3E8EA"),
}

# --- Render HTML -----------------------------------------------------------

CELL = "padding:7px 10px;border-bottom:1px solid #D8E0E3;white-space:nowrap;"
CELL_L = CELL + "text-align:left;white-space:normal;"
CELL_R = CELL + "text-align:right;font-variant-numeric:tabular-nums;"
HEAD = (
    "padding:7px 10px;border-bottom:2px solid #B6C4C9;background:#EDF1F2;"
    "font-size:10px;letter-spacing:.06em;text-transform:uppercase;"
    "color:#6B7C86;font-weight:700;"
)


def th(label: str, align: str = "right") -> str:
    return f'<th style="{HEAD}text-align:{align};">{label}</th>'


def pill(status: str) -> str:
    label, fg, bg = STATUS.get(status, STATUS["none"])
    return (
        f'<span style="display:inline-block;padding:2px 7px;border-radius:2px;'
        f"background:{bg};color:{fg};font-size:10px;font-weight:700;"
        f'letter-spacing:.05em;text-transform:uppercase;">{label}</span>'
    )


def acct_cell(row: dict) -> str:
    plat = row.get("plat", "")
    suffix = (
        f' <span style="color:#6B7C86;font-size:11px;">{plat}</span>' if plat else ""
    )
    return f'<td style="{CELL_L}">{row["acct"]}{suffix}</td>'


def budget_table(rows: list[dict], total: dict | None) -> str:
    out = [
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="border-collapse:collapse;width:100%;font-size:13px;">',
        "<tr>",
        th("Konto", "left"),
        th("Budżet mies."),
        th("Wydano MTD"),
        th("Wykorzyst."),
        th("Odchyl."),
        th("Prognoza"),
        th("Status", "left"),
        "</tr>",
    ]
    for r in rows:
        cur = r.get("cur", "zł")
        budget = money(r["budget"], cur) if r.get("budget") else "—"
        used = pct(r["used"]) if r.get("used") is not None else "—"
        dev = r.get("dev")
        dev_txt = (
            ("+" if dev > 0 else "−") + f"{abs(dev):.1f}".replace(".", ",") + NBSP + "pp"
            if dev is not None
            else "—"
        )
        out += [
            "<tr>",
            acct_cell(r),
            f'<td style="{CELL_R}">{budget}</td>',
            f'<td style="{CELL_R}">{money(r["spent"], cur)}</td>',
            f'<td style="{CELL_R}">{used}</td>',
            f'<td style="{CELL_R}color:{delta_color(dev)};">{dev_txt}</td>',
            f'<td style="{CELL_R}">{money(r["forecast"], cur)}</td>',
            f'<td style="{CELL}text-align:left;">{pill(r["status"])}</td>',
            "</tr>",
        ]
    if total:
        cur = total.get("cur", "zł")
        tstyle = "background:#EDF1F2;font-weight:700;border-top:2px solid #B6C4C9;"
        out += [
            f'<tr style="{tstyle}">',
            f'<td style="{CELL_L}">{total["acct"]}</td>',
            f'<td style="{CELL_R}">{money(total.get("budget"), cur) if total.get("budget") else "—"}</td>',
            f'<td style="{CELL_R}">{money(total["spent"], cur)}</td>',
            f'<td style="{CELL_R}">{pct(total["used"]) if total.get("used") is not None else "—"}</td>',
            f'<td style="{CELL_R}">—</td>',
            f'<td style="{CELL_R}">{money(total["forecast"], cur)}</td>',
            f'<td style="{CELL}"></td>',
            "</tr>",
        ]
    out.append("</table>")
    return "".join(out)


def kpi_table(rows: list[dict], total: dict | None) -> str:
    out = [
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="border-collapse:collapse;width:100%;font-size:13px;">',
        "<tr>",
        th("Konto", "left"),
        th("Koszt"),
        th("Δ"),
        th("Konw."),
        th("Δ"),
        th("Wartość konw."),
        th("Δ"),
        th("ROAS"),
        th("Δ"),
        th("CPC"),
        th("CTR"),
        "</tr>",
    ]
    for r in rows + ([total] if total else []):
        cur = r.get("cur", "zł")
        is_total = r is total
        style = (
            "background:#EDF1F2;font-weight:700;border-top:2px solid #B6C4C9;"
            if is_total
            else ""
        )
        cpc = money(r["cpc"], cur) if r.get("cpc") is not None else "—"
        if r.get("cpc") is not None:
            cpc = f'{r["cpc"]:.2f}'.replace(".", ",") + NBSP + cur
        ctr = pct(r["ctr"], 2) if r.get("ctr") is not None else "—"
        out += [
            f'<tr style="{style}">',
            acct_cell(r),
            f'<td style="{CELL_R}">{money(r["cost"], cur)}</td>',
            f'<td style="{CELL_R}color:{delta_color(r.get("dcost"))};">{delta(r.get("dcost"))}</td>',
            f'<td style="{CELL_R}">{count(r.get("conv"))}</td>',
            f'<td style="{CELL_R}color:{delta_color(r.get("dconv"))};">{delta(r.get("dconv"))}</td>',
            f'<td style="{CELL_R}">{money(r["val"], cur)}</td>',
            f'<td style="{CELL_R}color:{delta_color(r.get("dval"))};">{delta(r.get("dval"))}</td>',
            f'<td style="{CELL_R}">{roas(r.get("roas"))}</td>',
            f'<td style="{CELL_R}color:{delta_color(r.get("droas"))};">{delta(r.get("droas"))}</td>',
            f'<td style="{CELL_R}">{cpc}</td>',
            f'<td style="{CELL_R}">{ctr}</td>',
            "</tr>",
        ]
    out.append("</table>")
    return "".join(out)


def render_html(data: dict) -> str:
    m = data["meta"]
    parts = [
        '<!doctype html><html><body style="margin:0;padding:0;background:#F4F6F7;">',
        '<div style="max-width:900px;margin:0 auto;padding:28px 20px 56px;'
        'font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'color:#101B21;font-size:15px;line-height:1.55;">',
        # nagłówek
        '<div style="border-bottom:2px solid #101B21;padding-bottom:20px;margin-bottom:28px;">',
        '<div style="font-size:11px;letter-spacing:.15em;text-transform:uppercase;'
        'color:#0B6E7A;font-weight:700;">Optmyzr &middot; raport tygodniowy</div>',
        f'<h1 style="margin:8px 0 10px;font-size:28px;line-height:1.15;'
        f'letter-spacing:-.02em;font-weight:700;">{m["title"]}</h1>',
        f'<div style="color:#3A4A54;font-size:14px;">{m["subtitle"]}</div>',
        "</div>",
    ]

    # Na już
    if data.get("alerts"):
        parts.append(
            '<div style="font-size:11px;letter-spacing:.13em;text-transform:uppercase;'
            'color:#6B7C86;font-weight:700;border-bottom:1px solid #B6C4C9;'
            'padding-bottom:7px;margin-bottom:14px;">Na już</div>'
        )
        parts.append(
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            'style="border-collapse:collapse;width:100%;margin-bottom:32px;">'
        )
        for a in data["alerts"]:
            label, fg, bg = SEV.get(a.get("sev", "info"), SEV["info"])
            parts.append(
                f'<tr><td style="padding:11px 12px;border-bottom:1px solid #D8E0E3;'
                f'width:74px;vertical-align:top;">'
                f'<span style="display:inline-block;padding:2px 7px;border-radius:2px;'
                f"background:{bg};color:{fg};font-size:10px;font-weight:700;"
                f'letter-spacing:.05em;text-transform:uppercase;">{label}</span></td>'
                f'<td style="padding:11px 12px;border-bottom:1px solid #D8E0E3;'
                f'color:#3A4A54;">{a["text"]}</td></tr>'
            )
        parts.append("</table>")

    # Podsumowanie
    if data.get("summary"):
        parts.append(
            '<div style="background:#FFFFFF;border:1px solid #D8E0E3;border-radius:3px;'
            'padding:16px 18px;margin-bottom:32px;color:#3A4A54;font-size:14px;">'
            f'{data["summary"]}</div>'
        )

    # Portfolia
    for p in data["portfolios"]:
        parts.append(
            f'<div style="font-size:11px;letter-spacing:.13em;text-transform:uppercase;'
            f'color:#6B7C86;font-weight:700;border-bottom:1px solid #B6C4C9;'
            f'padding-bottom:7px;margin:0 0 6px;">Portfolio</div>'
            f'<h2 style="margin:0 0 16px;font-size:22px;font-weight:700;'
            f'letter-spacing:-.01em;">{p["name"]}</h2>'
        )
        parts.append(
            '<div style="font-size:10px;letter-spacing:.12em;text-transform:uppercase;'
            'color:#6B7C86;font-weight:700;margin-bottom:7px;">Budżet — wykorzystanie MTD</div>'
        )
        parts.append(budget_table(p["budget"], p.get("budget_total")))
        if p.get("budget_note"):
            parts.append(
                f'<p style="margin:9px 0 24px;font-size:12px;color:#6B7C86;">{p["budget_note"]}</p>'
            )
        parts.append(
            '<div style="font-size:10px;letter-spacing:.12em;text-transform:uppercase;'
            'color:#6B7C86;font-weight:700;margin-bottom:7px;">KPI — 7 dni vs poprzednie 7</div>'
        )
        parts.append(kpi_table(p["kpi"], p.get("kpi_total")))
        if p.get("kpi_note"):
            parts.append(
                f'<p style="margin:9px 0 36px;font-size:12px;color:#6B7C86;">{p["kpi_note"]}</p>'
            )
        else:
            parts.append('<div style="height:36px;"></div>')

    # Listy końcowe
    for block in data.get("blocks", []):
        parts.append(
            f'<div style="font-size:11px;letter-spacing:.13em;text-transform:uppercase;'
            f'color:#6B7C86;font-weight:700;border-bottom:1px solid #B6C4C9;'
            f'padding-bottom:7px;margin-bottom:12px;">{block["title"]}</div>'
        )
        parts.append(
            '<ul style="margin:0 0 30px;padding-left:18px;color:#3A4A54;font-size:14px;">'
        )
        for item in block["items"]:
            parts.append(f'<li style="margin-bottom:6px;">{item}</li>')
        parts.append("</ul>")

    parts.append(
        f'<div style="border-top:1px solid #D8E0E3;padding-top:16px;font-size:12px;'
        f'color:#6B7C86;">{m["footer"]}</div>'
    )
    parts.append("</div></body></html>")
    return "".join(parts)


def render_text(data: dict) -> str:
    m = data["meta"]
    lines = [m["title"], m["subtitle"], ""]
    if data.get("alerts"):
        lines += ["NA JUŻ", "-" * 60]
        for a in data["alerts"]:
            label = SEV.get(a.get("sev", "info"), SEV["info"])[0]
            text = a["text"].replace("<b>", "").replace("</b>", "")
            lines.append(f"[{label}] {text}")
        lines.append("")
    for p in data["portfolios"]:
        lines += [f"PORTFOLIO {p['name']}", "-" * 60, "Budżet:"]
        for r in p["budget"]:
            cur = r.get("cur", "zł")
            budget = money(r["budget"], cur) if r.get("budget") else "brak monitora"
            used = pct(r["used"]) if r.get("used") is not None else "—"
            lines.append(
                f"  {r['acct']} ({r.get('plat','')}) — budżet {budget}, "
                f"wydano {money(r['spent'], cur)} ({used}), "
                f"prognoza {money(r['forecast'], cur)}, {STATUS[r['status']][0]}"
            )
        lines.append("KPI (7 dni vs poprzednie 7):")
        for r in p["kpi"]:
            cur = r.get("cur", "zł")
            lines.append(
                f"  {r['acct']} ({r.get('plat','')}) — koszt {money(r['cost'], cur)} "
                f"({delta(r.get('dcost'))}), konw. {count(r.get('conv'))} "
                f"({delta(r.get('dconv'))}), wartość {money(r['val'], cur)} "
                f"({delta(r.get('dval'))}), ROAS {roas(r.get('roas'))} "
                f"({delta(r.get('droas'))})"
            )
        lines.append("")
    for block in data.get("blocks", []):
        lines += [block["title"].upper(), "-" * 60]
        for item in block["items"]:
            lines.append("  - " + item.replace("<b>", "").replace("</b>", ""))
        lines.append("")
    lines.append(m["footer"])
    return "\n".join(lines)


# --- Wysyłka ---------------------------------------------------------------


def send_email(subject: str, text: str, html: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    mail_from = os.environ.get("MAIL_FROM", user or "")
    mail_to = os.environ.get("MAIL_TO", "welc@harbingers.io")
    use_starttls = os.environ.get("SMTP_STARTTLS", "1") != "0"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP(host, port, timeout=60) as server:
        server.ehlo()
        if use_starttls:
            server.starttls()
            server.ehlo()
        if user and password:
            server.login(user, password)
        server.sendmail(
            mail_from, [a.strip() for a in mail_to.split(",")], msg.as_string()
        )
    print(f"E-mail wysłany do: {mail_to}", file=sys.stderr)


def main() -> int:
    path = Path(os.environ.get("REPORT_JSON", "reports/latest.json"))
    if not path.exists():
        print(f"Brak pliku z danymi: {path}", file=sys.stderr)
        return 1

    data = json.loads(path.read_text(encoding="utf-8"))
    html = render_html(data)
    text = render_text(data)
    subject = data["meta"]["subject"]

    if os.environ.get("DRY_RUN") == "1":
        print(text)
        print(f"\n[DRY_RUN] Nie wysłano. Temat: {subject}", file=sys.stderr)
        return 0

    send_email(subject, text, html)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
