#!/usr/bin/env python3
"""
Cotygodniowy raport czasu z Asany.

Dla zdefiniowanego zespołu liczy DOKŁADNY czas zalogowany w natywnym
śledzeniu czasu Asany ("Rzeczywisty czas") w dwóch okresach:
  * bieżący tydzień (poniedziałek -> dzień uruchomienia),
  * bieżący miesiąc (1. dzień miesiąca -> dzień uruchomienia).

Dokładność bierze się stąd, że NIE sumujemy zagregowanego pola
`actual_time_minutes` (które nie ma daty i przez wyszukiwarkę jest
ograniczone do 100 wyników), tylko pobieramy pojedyncze wpisy czasu
(`/tasks/{gid}/time_tracking_entries`). Każdy wpis ma `entered_on`
(datę) oraz `created_by` (autora), więc czas przypisujemy precyzyjnie
do właściwej osoby i właściwego dnia. Listę zadań pobieramy przez
stronicowany endpoint `/tasks` (bez limitu 100).

Skrypt korzysta wyłącznie z biblioteki standardowej Pythona (>=3.9),
więc nie wymaga instalacji zależności.

Wymagane zmienne środowiskowe:
  ASANA_TOKEN        - Personal Access Token do Asany (sekret).

Opcjonalne (mają sensowne wartości domyślne dla tego workspace):
  ASANA_WORKSPACE_GID  - GID workspace (domyślnie Harbingers).
  ASANA_USER_GIDS      - lista GID osób po przecinku.
  ASANA_USER_NAMES     - lista nazw osób po przecinku (równolegle do GID).
  REPORT_TZ            - strefa czasowa (domyślnie Europe/Warsaw).

Wysyłka e-mail (jeśli chcesz realnie wysłać):
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_FROM, MAIL_TO
  SMTP_STARTTLS ("1"/"0", domyślnie 1)

Sterowanie:
  DRY_RUN=1          - nie wysyłaj maila, wypisz raport na stdout.
  FORCE_SEND=1       - pomiń kontrolę godziny (wyślij niezależnie od pory).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    print("Wymagany Python 3.9+ (moduł zoneinfo).", file=sys.stderr)
    raise

ASANA_API = "https://app.asana.com/api/1.0"

# --- Konfiguracja domyślna (workspace Harbingers) --------------------------

DEFAULT_WORKSPACE_GID = "580416856505742"

# Kolejność tej listy = kolejność w raporcie.
DEFAULT_TEAM = [
    ("1208538292216163", "Tomasz Welc"),
    ("1208566294908723", "Aleksandra Ster"),
    ("1209246692737515", "Marcel Szydło"),
    ("1214133790907177", "Adrian Horowski"),
]


# --- Klient Asana API ------------------------------------------------------


class AsanaError(RuntimeError):
    pass


class Asana:
    def __init__(self, token: str):
        self._token = token

    def _request(self, path: str, params: dict | None = None) -> dict:
        url = f"{ASANA_API}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        for attempt in range(6):
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Bearer {self._token}")
            req.add_header("Accept", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                # 429 = rate limit, 5xx = przejściowy błąd -> retry z backoffem.
                if exc.code == 429 or 500 <= exc.code < 600:
                    retry_after = exc.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else min(2 ** attempt, 30)
                    time.sleep(wait)
                    continue
                body = exc.read().decode("utf-8", "replace")
                raise AsanaError(f"HTTP {exc.code} dla {url}: {body}") from exc
            except urllib.error.URLError as exc:
                time.sleep(min(2 ** attempt, 30))
                last = exc
        raise AsanaError(f"Nie udało się pobrać {url} po kilku próbach: {last}")

    def paginate(self, path: str, params: dict) -> list[dict]:
        """Zwraca wszystkie rekordy z listy, obsługując offset-pagination."""
        out: list[dict] = []
        params = dict(params)
        params.setdefault("limit", 100)
        offset: str | None = None
        while True:
            if offset:
                params["offset"] = offset
            payload = self._request(path, params)
            out.extend(payload.get("data", []))
            nxt = payload.get("next_page")
            if not nxt or not nxt.get("offset"):
                break
            offset = nxt["offset"]
        return out

    def tasks_for_assignee(self, workspace: str, assignee: str, modified_since_iso: str) -> list[dict]:
        return self.paginate(
            "/tasks",
            {
                "workspace": workspace,
                "assignee": assignee,
                "modified_since": modified_since_iso,
                "opt_fields": "gid,name",
            },
        )

    def time_entries(self, task_gid: str) -> list[dict]:
        return self.paginate(
            f"/tasks/{task_gid}/time_tracking_entries",
            {"opt_fields": "duration_minutes,entered_on,created_by.gid,created_by.name"},
        )


# --- Logika okresów --------------------------------------------------------


def period_bounds(now_local: dt.datetime) -> tuple[dt.date, dt.date, dt.date]:
    """Zwraca (poniedziałek_tygodnia, pierwszy_dzień_miesiąca, dziś)."""
    today = now_local.date()
    monday = today - dt.timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    return monday, month_start, today


def collect_minutes(
    asana: Asana,
    workspace: str,
    team: list[tuple[str, str]],
    week_start: dt.date,
    month_start: dt.date,
    today: dt.date,
) -> dict[str, dict[str, int]]:
    """Zwraca {gid: {"week": min, "month": min}} na podstawie wpisów czasu."""
    modified_since_iso = (
        dt.datetime.combine(month_start, dt.time.min)
        .replace(tzinfo=dt.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.000Z")
    )
    result: dict[str, dict[str, int]] = {gid: {"week": 0, "month": 0} for gid, _ in team}

    for gid, name in team:
        tasks = asana.tasks_for_assignee(workspace, gid, modified_since_iso)
        for task in tasks:
            for entry in asana.time_entries(task["gid"]):
                created_by = entry.get("created_by") or {}
                if created_by.get("gid") != gid:
                    continue  # licz tylko czas zalogowany przez tę osobę
                entered_on = entry.get("entered_on")
                minutes = entry.get("duration_minutes") or 0
                if not entered_on or not minutes:
                    continue
                try:
                    day = dt.date.fromisoformat(entered_on)
                except ValueError:
                    continue
                if month_start <= day <= today:
                    result[gid]["month"] += minutes
                    if week_start <= day <= today:
                        result[gid]["week"] += minutes
        print(f"  * {name}: {result[gid]['week']} min (tydzień) / "
              f"{result[gid]['month']} min (miesiąc)", file=sys.stderr)
    return result


# --- Formatowanie ----------------------------------------------------------


def fmt_hm(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h} h {m:02d} min"


def build_report(
    team: list[tuple[str, str]],
    data: dict[str, dict[str, int]],
    week_start: dt.date,
    month_start: dt.date,
    today: dt.date,
) -> tuple[str, str, str]:
    """Zwraca (temat, treść_tekstowa, treść_html)."""
    pl_months = [
        "", "stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca",
        "lipca", "sierpnia", "września", "października", "listopada", "grudnia",
    ]
    date_h = f"{today.day} {pl_months[today.month]} {today.year}"
    week_h = f"{week_start.strftime('%d.%m')} – {today.strftime('%d.%m')}"
    month_h = f"{month_start.strftime('%d.%m')} – {today.strftime('%d.%m.%Y')}"

    subject = f"⏱️ Raport czasu z Asany – {date_h}"

    week_total = sum(data[g]["week"] for g, _ in team)
    month_total = sum(data[g]["month"] for g, _ in team)

    # --- wersja tekstowa ---
    lines = [subject, ""]
    lines.append(f"TYDZIEŃ ({week_h}):")
    for gid, name in team:
        lines.append(f"  - {name}: {fmt_hm(data[gid]['week'])}")
    lines.append(f"  = RAZEM: {fmt_hm(week_total)}")
    lines.append("")
    lines.append(f"MIESIĄC ({month_h}):")
    for gid, name in team:
        lines.append(f"  - {name}: {fmt_hm(data[gid]['month'])}")
    lines.append(f"  = RAZEM: {fmt_hm(month_total)}")
    lines.append("")
    lines.append("Źródło: natywne śledzenie czasu w Asanie (time tracking entries).")
    text = "\n".join(lines)

    # --- wersja HTML ---
    def rows(period: str) -> str:
        r = ""
        for gid, name in team:
            r += (
                f'<tr><td style="padding:8px 14px;border-bottom:1px solid #eee">{name}</td>'
                f'<td style="padding:8px 14px;border-bottom:1px solid #eee;text-align:right;'
                f'font-variant-numeric:tabular-nums">{fmt_hm(data[gid][period])}</td></tr>'
            )
        return r

    html = f"""\
<!doctype html><html lang="pl"><body style="margin:0;background:#f6f7f9;
font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1a1a1a">
<div style="max-width:560px;margin:0 auto;padding:24px">
  <h1 style="font-size:20px;margin:0 0 4px">⏱️ Raport czasu z Asany</h1>
  <p style="margin:0 0 20px;color:#666">{date_h}</p>

  <h2 style="font-size:15px;margin:20px 0 6px">Ten tydzień <span style="color:#888;font-weight:400">({week_h})</span></h2>
  <table style="width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden">
    {rows('week')}
    <tr><td style="padding:10px 14px;font-weight:700">RAZEM</td>
        <td style="padding:10px 14px;text-align:right;font-weight:700;font-variant-numeric:tabular-nums">{fmt_hm(week_total)}</td></tr>
  </table>

  <h2 style="font-size:15px;margin:24px 0 6px">Cały miesiąc <span style="color:#888;font-weight:400">({month_h})</span></h2>
  <table style="width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden">
    {rows('month')}
    <tr><td style="padding:10px 14px;font-weight:700">RAZEM</td>
        <td style="padding:10px 14px;text-align:right;font-weight:700;font-variant-numeric:tabular-nums">{fmt_hm(month_total)}</td></tr>
  </table>

  <p style="margin:22px 0 0;color:#999;font-size:12px">
    Źródło: natywne śledzenie czasu w Asanie (wpisy time tracking).
    Zliczany jest czas zalogowany przez daną osobę, wg daty wpisu.
  </p>
</div></body></html>"""
    return subject, text, html


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
        server.sendmail(mail_from, [addr.strip() for addr in mail_to.split(",")], msg.as_string())
    print(f"E-mail wysłany do: {mail_to}", file=sys.stderr)


# --- Główna logika ---------------------------------------------------------


def load_team() -> list[tuple[str, str]]:
    gids = os.environ.get("ASANA_USER_GIDS")
    names = os.environ.get("ASANA_USER_NAMES")
    if gids and names:
        gid_list = [g.strip() for g in gids.split(",") if g.strip()]
        name_list = [n.strip() for n in names.split(",")]
        if len(gid_list) != len(name_list):
            raise SystemExit("ASANA_USER_GIDS i ASANA_USER_NAMES muszą mieć tyle samo elementów.")
        return list(zip(gid_list, name_list))
    return DEFAULT_TEAM


def main() -> int:
    token = os.environ.get("ASANA_TOKEN")
    if not token:
        raise SystemExit("Brak ASANA_TOKEN w środowisku.")

    tz = ZoneInfo(os.environ.get("REPORT_TZ", "Europe/Warsaw"))
    now_local = dt.datetime.now(tz)
    dry_run = os.environ.get("DRY_RUN", "0") == "1"
    force_send = os.environ.get("FORCE_SEND", "0") == "1"

    # Zabezpieczenie godziny: cron w GitHub Actions jest w UTC i nie zna DST,
    # więc uruchamiamy o 08:00 i 09:00 UTC, a realnie działamy tylko o 10:00
    # czasu lokalnego (chyba że DRY_RUN/FORCE_SEND).
    if not dry_run and not force_send and now_local.hour != 10:
        print(f"Pora lokalna {now_local:%H:%M} != 10:00 – pomijam ten przebieg.", file=sys.stderr)
        return 0

    workspace = os.environ.get("ASANA_WORKSPACE_GID", DEFAULT_WORKSPACE_GID)
    team = load_team()
    week_start, month_start, today = period_bounds(now_local)

    print(f"Okres tygodnia: {week_start} .. {today}", file=sys.stderr)
    print(f"Okres miesiąca: {month_start} .. {today}", file=sys.stderr)

    asana = Asana(token)
    data = collect_minutes(asana, workspace, team, week_start, month_start, today)
    subject, text, html = build_report(team, data, week_start, month_start, today)

    if dry_run:
        print("\n===== DRY RUN – raport (bez wysyłki) =====\n")
        print(text)
        return 0

    send_email(subject, text, html)
    return 0


if __name__ == "__main__":
    sys.exit(main())
