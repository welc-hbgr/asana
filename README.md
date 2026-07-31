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

Cron w GitHub Actions działa w UTC i nie zna zmiany czasu, dlatego workflow
odpala się o `08:00` i `09:00 UTC` w piątek, a skrypt wysyła raport tylko
wtedy, gdy w Polsce jest naprawdę `10:00` (czas letni i zimowy są w ten
sposób obsłużone automatycznie). Pozostałe przebiegi kończą się bez wysyłki.

> Uwaga: harmonogramy GitHub Actions bywają uruchamiane z kilkuminutowym
> opóźnieniem przy dużym obciążeniu – to normalne dla cronów GitHuba.

## Dostosowanie

- **Inny zespół / inne osoby** – zmienne `ASANA_USER_GIDS` i `ASANA_USER_NAMES`
  (listy po przecinku, równoległe), lub edycja `DEFAULT_TEAM` w skrypcie.
- **Inny workspace** – `ASANA_WORKSPACE_GID`.
- **Inna strefa/godzina** – `REPORT_TZ` oraz godziny w cronie i warunek
  `now_local.hour != 10` w skrypcie.

## Uruchomienie lokalne (opcjonalnie)

```bash
export ASANA_TOKEN=xxxx
DRY_RUN=1 python3 scripts/asana_weekly_report.py
```

Wymaga Pythona 3.9+ (skrypt korzysta tylko z biblioteki standardowej).
