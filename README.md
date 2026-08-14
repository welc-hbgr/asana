# Cotygodniowy raport czasu z Asany

Automatyzacja, która **w każdy piątek o 10:00 (czasu polskiego)** wysyła
e-mailem raport zalogowanego czasu z Asany dla zdefiniowanego zespołu, w
dwóch okresach: **bieżący tydzień** i **bieżący miesiąc** (od 1. dnia
miesiąca do dnia wysyłki).

Domyślny zespół:

| Osoba | Rola |
|---|---|
| Tomasz Welc | właściciel raportu |
| Aleksandra Ster | pracownik |
| Marcel Szydło | pracownik |
| Adrian Horowski | pracownik |

## Jak to działa

- `scripts/asana_weekly_report.py` – pobiera dane z REST API Asany i składa raport.
- `.github/workflows/asana-weekly-report.yml` – uruchamia skrypt co piątek (cron) oraz na żądanie.

Skrypt liczy **dokładny** czas, bo sięga po pojedyncze wpisy czasu
(`/tasks/{gid}/time_tracking_entries`). Każdy wpis ma datę (`entered_on`)
i autora (`created_by`), więc czas jest przypisywany do właściwej osoby i
właściwego dnia. Dzięki temu tydzień i pełny miesiąc są policzone rzetelnie,
bez ograniczenia 100 wyników, które ma wyszukiwarka Asany.

> **Założenie:** liczony jest czas zalogowany przez daną osobę na zadaniach
> jej przypisanych. Jeśli ktoś loguje czas na zadaniu przypisanym do kogoś
> innego, ten wpis nie zostanie doliczony (można to rozszerzyć – patrz sekcja
> „Dostosowanie”).

## Konfiguracja (jednorazowo)

Wszystko ustawiasz jako **GitHub Secrets** w repozytorium:
`Settings → Secrets and variables → Actions → New repository secret`.

### 1. Token do Asany

| Sekret | Wartość |
|---|---|
| `ASANA_TOKEN` | Personal Access Token z Asany |

Token wygenerujesz tu: **Asana → Settings → Apps → Developer apps →
Personal access tokens → Create new token**
(lub bezpośrednio: <https://app.asana.com/0/my-apps>).
Token musi należeć do konta, które widzi zadania całego zespołu.

### 2. Wysyłka e-mail (SMTP)

| Sekret | Przykład / opis |
|---|---|
| `SMTP_HOST` | np. `smtp.gmail.com`, `smtp.office365.com` |
| `SMTP_PORT` | zwykle `587` |
| `SMTP_USER` | login SMTP (adres nadawcy) |
| `SMTP_PASS` | hasło aplikacji / hasło SMTP |
| `MAIL_FROM` | adres „od" (domyślnie = `SMTP_USER`) |
| `MAIL_TO` | odbiorca (domyślnie `welc@harbingers.io`; kilku po przecinku) |

> **Gmail / Google Workspace:** użyj **hasła aplikacji** (App Password), nie
> zwykłego hasła. Wymaga włączonej weryfikacji dwuetapowej.
>
> **Nie masz własnego SMTP?** Napisz – podmienię wysyłkę na usługę typu
> Resend/SendGrid (jeden klucz API zamiast danych SMTP).

## Test przed pierwszym piątkiem

1. Wejdź w zakładkę **Actions** w repo.
2. Wybierz workflow **„Cotygodniowy raport czasu z Asany"**.
3. **Run workflow**:
   - zostaw `dry_run = true` → raport tylko policzy się i pojawi w logach (bez maila),
   - ustaw `dry_run = false` → wyśle prawdziwego maila od razu (niezależnie od godziny).

Ręczne uruchomienie pomija kontrolę godziny, więc przetestujesz o dowolnej porze.

## Harmonogram

Workflow ma **jeden** cron: `0 8 * * 5` (piątek `08:00 UTC`, czyli `10:00`
czasu letniego / `09:00` czasu zimowego w Polsce). Raport jest wysyłany w
**każdym** zaplanowanym piątkowym przebiegu – skrypt **nie** sprawdza już
dokładnej godziny.

> **Dlaczego nie ma kontroli godziny?** Harmonogramy GitHub Actions bywają
> uruchamiane z **dużym** opóźnieniem (nierzadko ~1 h). Wcześniejsza wersja
> wysyłała tylko o równej `10:00` i przez takie opóźnienie potrafiła pominąć
> piątek. Teraz opóźnienie GitHuba jedynie przesuwa wysyłkę o kilkadziesiąt
> minut później tego samego ranka – raport i tak dojdzie.

> Uwaga: w rzadkich przypadkach GitHub potrafi **całkiem pominąć** zaplanowany
> przebieg (przy bardzo dużym obciążeniu). Gdyby raport kiedyś nie przyszedł,
> zawsze można go dosłać ręcznie: *Actions → Run workflow → `dry_run=false`*.

## Dostosowanie

- **Inny zespół / inne osoby** – zmienne `ASANA_USER_GIDS` i `ASANA_USER_NAMES`
  (listy po przecinku, równoległe), lub edycja `DEFAULT_TEAM` w skrypcie.
- **Inny workspace** – `ASANA_WORKSPACE_GID`.
- **Inna strefa/dzień/godzina** – `REPORT_TZ` oraz wyrażenie `cron` w
  workflow (i ewentualnie warunek dnia `now_local.weekday() != 4` w skrypcie).
- **Szybkość** – wpisy czasu pobierane są równolegle. Liczbę wątków ustawia
  `ASANA_MAX_WORKERS` (domyślnie `8`). Więcej wątków = szybciej, ale przy zbyt
  agresywnym ustawieniu Asana częściej odpowiada limitem `429` (skrypt sam go
  respektuje i odczekuje). Wartość domyślna jest bezpiecznym kompromisem.

## Uruchomienie lokalne (opcjonalnie)

```bash
export ASANA_TOKEN=xxxx
DRY_RUN=1 python3 scripts/asana_weekly_report.py
```

Wymaga Pythona 3.9+ (skrypt korzysta tylko z biblioteki standardowej).

---

# Codzienny raport opóźnionych tasków (Slack)

Druga automatyzacja: **w dni robocze (pon–pt) rano (~09:00 czasu polskiego)**
wysyła na **Slacka** listę **opóźnionych** zadań (niezakończonych, z terminem,
który już minął) dla zespołu, pogrupowaną po osobach.

Domyślny zespół: Aleksandra Ster, Marcel Szydło, Adrian Horowski,
Przemysław Żywicki.

- `scripts/asana_overdue_tasks.py` – jednym zapytaniem do wyszukiwarki Asany
  pobiera niezakończone zadania z terminem przed dzisiaj i wysyła je na Slacka.
- `.github/workflows/asana-overdue-daily.yml` – cron w dni robocze (pon–pt)
  o `07:00 UTC` (= 09:00 latem / 08:00 zimą) oraz uruchomienie na żądanie.

## Konfiguracja Slacka (jednorazowo)

Potrzebny jest **Incoming Webhook** Slacka:

1. Wejdź na <https://api.slack.com/apps> → **Create New App** → *From scratch*.
2. Nadaj nazwę (np. „Asana Opóźnienia"), wybierz swój workspace Slacka.
3. W menu **Incoming Webhooks** → przełącz **Activate Incoming Webhooks** na *On*.
4. **Add New Webhook to Workspace** → wybierz kanał (lub swoje DM) → **Allow**.
5. Skopiuj wygenerowany **Webhook URL** (postać `https://hooks.slack.com/services/…`).
6. Dodaj go w repo jako sekret (*Settings → Secrets and variables → Actions*):

| Sekret | Wartość |
|---|---|
| `SLACK_WEBHOOK_URL` | skopiowany Webhook URL |

`ASANA_TOKEN` jest współdzielony z raportem czasu – nie trzeba go dodawać ponownie.

## Test

*Actions → „Codzienny raport opóźnionych tasków (Slack)" → Run workflow*:
- `dry_run = true` → wiadomość tylko wypisze się w logach (bez Slacka),
- `dry_run = false` → wyśle prawdziwą wiadomość na skonfigurowany kanał.

## Dostosowanie

- **Inny zespół** – `ASANA_USER_GIDS` / `ASANA_USER_NAMES` lub `DEFAULT_TEAM`
  w skrypcie.
- **Inna godzina / częstotliwość** – wyrażenie `cron` w workflow (obecnie
  dni robocze `0 7 * * 1-5`; np. codziennie z weekendami: `0 7 * * *`).
- **Definicja „opóźnienia"** – skrypt liczy zadania z terminem **przed** dziś
  (zadania z terminem na dziś nie są jeszcze traktowane jako opóźnione).

---

# Cotygodniowy raport KPI i budżetów z Optmyzr

Trzecia automatyzacja: **w każdy piątek ok. 10:00 (czasu polskiego)** przychodzi
mailem na `welc@harbingers.io` podsumowanie **KPI** i **budżetów** dla każdego
portfolio z Optmyzr — AS, MS, AH, PZ (lista pobierana na żywo, nowe portfolio
dołączy samo).

Dla każdego portfolia raport ma dwie tabele:

- **Budżet** – miesięczny budżet z **Budget Monitora Optmyzr**, wydatek MTD,
  % wykorzystania, oczekiwany % (wynikający z dnia miesiąca), prognoza na koniec
  miesiąca i status 🟢/🟡/🔴.
- **KPI** – ostatnie 7 pełnych dni vs poprzednie 7 dni: koszt, konwersje,
  wartość konwersji, ROAS, CPC, CTR, każde ze zmianą WoW.

Na górze jest sekcja **„Na już"** (tylko rzeczy wymagające reakcji), na dole
podsumowanie zbiorcze wszystkich portfolio i lista kont bez Budget Monitora.

## Czym to się różni od dwóch automatyzacji powyżej

Ta **nie jest** skryptem w GitHub Actions. Dane z Optmyzr są dostępne przez
**konektor MCP** podpięty do konta Claude, a nie przez klucz API, którego
GitHub Actions mógłby użyć. Dlatego raport uruchamia **Routine** (zadanie
cykliczne po stronie Claude):

| | |
|---|---|
| Nazwa | `Optmyzr – cotygodniowy raport KPI i budżetów per portfolio` |
| ID | `trig_01HzUTgiGRh6B5eb3HtbxryV` |
| Harmonogram | `8 8 * * 5` (UTC) = piątek **10:08** czasu letniego / **09:08** zimowego |
| Tryb | świeża sesja przy każdym uruchomieniu |
| Powiadomienia | e-mail + push |
| Treść promptu | `automations/optmyzr_weekly_kpi_budget_prompt.md` |

Routine widać i można nim zarządzać w Claude → **Routines**.

## Skąd biorą się budżety

Z **Budget Monitorów** ustawionych w Optmyzr (`Target Value` = budżet
miesięczny). Nic nie trzeba wpisywać w tym repo — zmiana budżetu w Optmyzr
automatycznie wchodzi do następnego raportu.

> **Konta bez Budget Monitora** trafiają do osobnej sekcji na końcu raportu,
> z samym wydatkiem i prognozą. Żeby takie konto miało pełny wiersz budżetowy,
> trzeba dodać mu Budget Monitor w Optmyzr.

## Jak zmienić treść raportu

`automations/optmyzr_weekly_kpi_budget_prompt.md` to źródło prawdy, ale
**Routine trzyma własną kopię promptu** — sama edycja pliku nic nie zmieni.
Po edycji poproś Claude: *„zaktualizuj prompt Routine
`trig_01HzUTgiGRh6B5eb3HtbxryV` treścią z
`automations/optmyzr_weekly_kpi_budget_prompt.md`"*.

Zmiana dnia/godziny to `cron_expression` tego samego Routine (wyrażenie jest
w **UTC** – latem odejmij 2 h od czasu polskiego, zimą 1 h).

## Znane pułapki

- **ROAS i ACOS z API Optmyzr są zawyżone ×100** (potrafią pokazać `83 024%`
  zamiast `8,3x`). Prompt jawnie zakazuje ich używania i każe liczyć ROAS jako
  `wartość konwersji / koszt`. Gdybyś kiedyś zobaczył w raporcie absurdalne
  procenty — to ten błąd wrócił.
- Raport zawsze kończy się na **wczoraj**; dzisiejszy dzień jest niepełny.
- Sumy portfolio liczone są z surowych metryk, nie jako średnia wskaźników.
