#!/usr/bin/env python3
"""
Codzienny raport opóźnionych tasków na Slacka.

Dla zdefiniowanego zespołu znajduje w Asanie zadania, które:
  * są NIEUKOŃCZONE (`completed = false`),
  * mają termin (`due date`) PRZED dzisiejszym dniem (czyli po terminie).

Wynik grupowany jest po osobach i wysyłany jako wiadomość na Slacka przez
Incoming Webhook. Zadania pobierane są jednym zapytaniem do wyszukiwarki
Asany (`/workspaces/{gid}/tasks/search`), więc raport jest szybki.

Skrypt korzysta wyłącznie z biblioteki standardowej Pythona (>=3.9),
więc nie wymaga instalacji zależności.

Wymagane zmienne środowiskowe:
  ASANA_TOKEN        - Personal Access Token do Asany (sekret).
  SLACK_WEBHOOK_URL  - URL Incoming Webhooka Slacka (sekret) - do realnej wysyłki.

Opcjonalne (mają sensowne wartości domyślne dla tego workspace):
  ASANA_WORKSPACE_GID  - GID workspace (domyślnie Harbingers).
  ASANA_USER_GIDS      - lista GID osób po przecinku.
  ASANA_USER_NAMES     - lista nazw osób po przecinku (równolegle do GID).
  REPORT_TZ            - strefa czasowa (domyślnie Europe/Warsaw).

Sterowanie:
  DRY_RUN=1          - nie wysyłaj na Slacka, wypisz wiadomość na stdout.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

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
    ("1208566294908723", "Aleksandra Ster"),
    ("1209246692737515", "Marcel Szydło"),
    ("1214133790907177", "Adrian Horowski"),
    ("1211287680277826", "Przemysław Żywicki"),
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
        last: Exception | None = None
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

    def overdue_tasks(self, workspace: str, gids: list[str], today: dt.date) -> list[dict]:
        """Niezakończone zadania PO TERMINIE (due < today), dla podanych osób.

        Używa wyszukiwarki Asany (wymaga planu z zaawansowanym wyszukiwaniem).
        Filtr `due_on.before` w API bywa inkluzywny (zwraca też zadania z
        terminem na dziś), dlatego dokładne odcięcie robimy po stronie skryptu:
        zostawiamy tylko zadania, których termin minął (co najmniej 1 dzień).
        Zwraca zadania z polami: name, due_on, assignee.gid/name, permalink_url.
        """
        payload = self._request(
            f"/workspaces/{workspace}/tasks/search",
            {
                "assignee.any": ",".join(gids),
                "completed": "false",
                "due_on.before": today.isoformat(),
                "opt_fields": "name,due_on,due_at,assignee.gid,assignee.name,permalink_url",
                "sort_by": "due_date",
                "sort_ascending": "true",
                "limit": 100,
            },
        )
        overdue: list[dict] = []
        for task in payload.get("data", []):
            due_on = task.get("due_on")
            if not due_on:
                continue  # bez terminu nie liczymy jako "po terminie"
            try:
                due = dt.date.fromisoformat(due_on)
            except ValueError:
                continue
            if (today - due).days >= 1:  # ściśle przed dzisiaj = po terminie
                overdue.append(task)
        return overdue


# --- Formatowanie ----------------------------------------------------------

PL_MONTHS = [
    "", "stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca",
    "lipca", "sierpnia", "września", "października", "listopada", "grudnia",
]


def pl_days(n: int) -> str:
    """Poprawna polska odmiana: 1 dzień / 2 dni / 5 dni po terminie."""
    return "1 dzień po terminie" if n == 1 else f"{n} dni po terminie"


def slack_text(s: str) -> str:
    """Przygotowuje tekst do wstawienia w wiadomość/link Slacka.

    Escape'uje znaki specjalne mrkdwn (`&`, `<`, `>`), zamienia `|` (które jest
    separatorem w linkach `<url|tekst>`) oraz zbija znaki nowej linii i nadmiar
    spacji, żeby nazwa zadania nie rozwaliła formatowania.
    """
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace("|", "/")
    return " ".join(s.split())


def build_message(
    team: list[tuple[str, str]],
    tasks: list[dict],
    today: dt.date,
) -> str:
    """Składa treść wiadomości Slacka (mrkdwn)."""
    # Pogrupuj zadania po osobie (gid).
    by_person: dict[str, list[dict]] = {gid: [] for gid, _ in team}
    for task in tasks:
        assignee = task.get("assignee") or {}
        gid = assignee.get("gid")
        if gid in by_person:
            by_person[gid].append(task)

    date_h = f"{today.day} {PL_MONTHS[today.month]} {today.year}"
    total = sum(len(v) for v in by_person.values())

    if total == 0:
        return (f":white_check_mark: *Opóźnione taski — {date_h}*\n"
                "Brak zaległości — cały zespół ma zadania w terminie. :tada:")

    lines = [f":alarm_clock: *Opóźnione taski — {date_h}* (łącznie: {total})", ""]
    for gid, name in team:
        items = by_person[gid]
        if not items:
            lines.append(f"*{name}* — :white_check_mark: brak")
            continue
        lines.append(f"*{name}* ({len(items)}):")
        for task in items:
            title = slack_text(task.get("name") or "(bez nazwy)")
            url = task.get("permalink_url")
            due_on = task.get("due_on")
            label = f"<{url}|{title}>" if url else title
            if due_on:
                try:
                    due = dt.date.fromisoformat(due_on)
                    days = (today - due).days
                    lines.append(f"   • {label} — termin {due_on} ({pl_days(days)})")
                except ValueError:
                    lines.append(f"   • {label} — termin {due_on}")
            else:
                lines.append(f"   • {label}")
        lines.append("")
    return "\n".join(lines).rstrip()


# --- Wysyłka na Slacka -----------------------------------------------------


def send_slack(webhook_url: str, text: str) -> None:
    data = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(webhook_url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", "replace")
                if body.strip() != "ok":
                    raise AsanaError(f"Slack odpowiedział nietypowo: {body!r}")
                print("Wiadomość wysłana na Slacka.", file=sys.stderr)
                return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if exc.code == 429 or 500 <= exc.code < 600:
                time.sleep(min(2 ** attempt, 30))
                continue
            raise AsanaError(f"Slack HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            time.sleep(min(2 ** attempt, 30))
            last = exc
    raise AsanaError(f"Nie udało się wysłać na Slacka: {last}")


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
    today = dt.datetime.now(tz).date()
    dry_run = os.environ.get("DRY_RUN", "0") == "1"

    workspace = os.environ.get("ASANA_WORKSPACE_GID", DEFAULT_WORKSPACE_GID)
    team = load_team()

    print(f"Dzień raportu: {today}", file=sys.stderr)
    asana = Asana(token)
    tasks = asana.overdue_tasks(workspace, [gid for gid, _ in team], today)
    print(f"Znaleziono opóźnionych zadań: {len(tasks)}", file=sys.stderr)

    message = build_message(team, tasks, today)

    if dry_run:
        print("\n===== DRY RUN – wiadomość (bez wysyłki na Slacka) =====\n")
        print(message)
        return 0

    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        raise SystemExit("Brak SLACK_WEBHOOK_URL w środowisku.")
    send_slack(webhook, message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
