# sj-cli

*Read this in [English](README.en.md).*

Ett kommandoradsverktyg som bokar resor med SJ åt dig som har periodkort, till
exempel *SJ Årskort* eller *SJ 30-dagarskort*.

![demo av sj-cli](demo.gif)

## Varför

Ett periodkort från SJ ger inte rätt att kliva på vilket tåg som helst. Varje
resa måste bokas, precis som med en vanlig biljett. Skillnaden är att bokningen
kostar 0 kr, eftersom resan redan är betald genom periodkortet.

Det man kanske inte har klart för sig när man köper kortet är att man inte är
garanterad plats på de avgångar man önskar. Är tåget fullbokat i din klass kan
du helt enkelt inte boka någon biljett, vilket i sin tur känns ganska tråkigt
när man betalat så mycket för ett månadskort, och i synnerhet för ett årskort.

I praktiken innebär det att man måste boka sin resa långt i förväg, vilket inte
alltid är möjligt. Att boka dagen innan är, på den sträcka jag pendlar, i det
närmaste omöjligt - det alltid "fullbokat".

Det mest frustrerande är att "fullbokat" inte betyder fullt. Under alla år jag
har pendlat har jag aldrig klivit på ett enda tåg där varje plats i min klass
varit upptagen, trots att det inte gått att boka biljett just för att tåget är
"fullbokat". Personalen ombord har dessutom varit tydlig med att tåget är "helt
fullbokat" och att alla därför måste sitta på sin bokade plats. Trots det har
det, som sagt, alltid funnits lediga platser i min klass.

Varför det är så kan bara SJ svara på. En gissning är att resenärer med
periodkort bokar platser som de sedan inte använder; en annan är att SJ räknar
tåget som fullt med god marginal. Oavsett orsak så känns det väldigt tråkigt att
inte kunna resa med önskat tåg när man har betalat så mycket pengar för sitt 
periodkort - framförallt när tåget inte ens är fullt (vilket det aldrig är). 

## Lösningen?

Det kan bara SJ lösa, men några tänkbara vägar skulle kanske kunna vara; 
* att en bokning måste bekräftas ett dygn före avgång för att gälla,
*  att en fullbokad klass ger uppgradering till nästa i stället för avslag,
*  eller helt enkelt en platsräkning som stämmer med hur tåget faktiskt ser ut.
Utan insyn i orsaken dock är det såklart svårt att veta vad som skulle hjälpa.

Det enda jag vet är att jag vill åka med tåget, och för att göra det måste jag
boka biljetter veckor och månader i förväg. Ironiskt nog blir jag därmed en del
av problemet. I det här fallet väljer jag ändå att sätta min egen pendling
först *(förlåt till dig som inte fick biljett en dag jag hade bokat men inte 
åkte)*.

## Verktyget

Det här verktyget löser inte SJ:s problem, men jag kan åtminstone säkerställa
att jag kan boka upp biljetter för alla resor jag tänkt göra, veckor och månader
i förväg..

Det bokar dina resor på samma sätt som appen på sj.se gör, fast för många dagar
på en gång. Enskilda resor kan också bokas interaktivt. Du anger:

* datumintervall, enstaka dagar eller hela ISO-veckor
* tid för avgång
* enkel eller tur och retur
* komfortklass
* flexibilitet
* sätespreferenser
* om helger och röda dagar ska hoppas över

Sedan bokar verktyget de biljetter som stämmer överens med dina önskemål, dag
för dag; en dag som redan är bokad dubbelbokas aldrig. Det pratar med samma API
som webbappen på sj.se (bakåtkonstruerat, alltså inget officiellt), så det kan
logga in, söka, välja rätt avgång, hitta periodkortets erbjudande till 0 kr och
slutföra bokningen. Verktyget kan även köras i "dry run", som förhandsvisar vad
som skulle ha bokats utan att göra några faktiska bokningsförsök.

## Krav

- Python 3.13+
- `httpx` (det enda beroendet vid körning; `pytest`, `ruff` och `mypy` för utveckling)
- Ett SJ-konto med periodkort, och en telefon för engångsverifieringen via SMS

## Installation

```bash
git clone https://github.com/patchon/sj-cli.git
cd sj-cli
python3 -m venv venv
./venv/bin/pip install -e .
source venv/bin/activate
mkdir -p ~/.config/sj-cli
cp src/sj_cli/config.example.toml ~/.config/sj-cli/config.toml
```

## Konfiguration

```bash
$EDITOR ~/.config/sj-cli/config.toml
```

```toml
[auth]
email = "user@example.com"
password = "your-password"

[search_parameters]
dates = "2026-09-01..2026-10-30"
time_leave = "04:01"
time_return = "17:22"
station_from = "Malmö Central"
station_to = "Stockholm Central"
comfort_class = "2 class calm"
flexibility = "FULLFLEX"
roundtrip = true
select_closest_ticket_available = true
allow_class_fallback = true
book_partial = true
skip_weekends = true
skip_holidays = true
service_types = ["SJ_HIGH"]
seat_preference = ["avoid table", "single", "aisle", "window", "forward"]
```

Med konfigurationen ovan kommer verktyget att:

* boka tur och retur på vardagar *(helger och röda dagar hoppas över)*
* mellan **2026-09-01** och **2026-10-30**
* från **Malmö Central** till **Stockholm Central**
* med avgång **04:01** från Malmö och **17:22** från Stockholm
* i 2 klass lugn *(med nedgradering till 2 klass om lugn är fullt)*
* boka bara ena hållet om tur och retur inte går att få
* ta närmaste avgång om den angivna tiden inte går att boka
* bara på SJ snabbtåg, med plats vald efter sätespreferensen

## Exempel

### Boka biljetter enligt konfiguration

```bash
$ > sj-cli --book
  ╭──────────────────────────────────╮
  │  operation    booking tickets    │
  │  account      jane@doe           │
  │  travelpass   SJ Periodkort      │
  │  holder       Jane Doe           │
  ╰──────────────────────────────────╯

  route     Malmö Central ⇄ Stockholm Central
  days      15 sep – 20 sep 2026 · weekdays only
  times     out 04:01 · back 17:22
  ticket    2 class calm · FULLFLEX · SJ High-speed train

  tue 15 sep 2026   Malmö Central ⇄ Stockholm Central
    ✓ searching outbound at 04:01
    ✓ checking offers for outbound at 04:01
    ✓ creating booking with outbound at 04:01
    ✓ searching return at 17:22
    ✓ checking offers for return at 17:22
    ✓ adding return leg at 17:22
    ✓ checking out booking ERU0HWB2
    → 04:01 – 08:38   4h 37m   X 2000 520   carriage 3 seat 45   2 class calm   FULLFLEX   ERU0HWB2
    ← 17:22 – 21:53   4h 31m   X 2000 543   carriage 3 seat 11   2 class calm   FULLFLEX   ERU0HWB2

  wed 16 sep 2026   tickets already booked

  thu 17 sep 2026   tickets already booked

  fri 18 sep 2026   tickets already booked

  sat 19 sep 2026   weekend

  sun 20 sep 2026   weekend

  ● 6 day(s) · 1 booked · 3 already booked · 2 skipped
```

### Lista bokningar

```bash
$ > sj-cli --list-bookings
  ╭──────────────────────────────────╮
  │  operation    listing bookings   │
  │  account      jane@doe           │
  │  travelpass   SJ Periodkort      │
  │  holder       Jane Doe           │
  ╰──────────────────────────────────╯

  mon 12 oct 2026   Malmö Central ⇄ Stockholm Central
    → 04:01 – 08:38   4h 37m   X 2000 520   carriage 3 seat 67   2 class calm   FULLFLEX   WXYZ1234
    ← 17:22 – 21:53   4h 31m   X 2000 543   carriage 3 seat 11   2 class calm   FULLFLEX   WXYZ1234

  tue 13 oct 2026   Malmö Central ⇄ Stockholm Central
    → 04:01 – 08:38   4h 37m   X 2000 520   carriage 3 seat 27   2 class calm   FULLFLEX   W3ST1234
    ← 17:22 – 21:53   4h 31m   X 2000 543   carriage 3 seat 27   2 class calm   FULLFLEX   W3ST1234

  ● 2 day(s) · 2 booking(s)
```

### Övrigt

Använd `--dry-run` för att se vad som skulle hända, flaggan förhandsvisar
`--book`, `--book-journey`, `--cancel-date`, `--cancel-booking`,
`--change-seat-date`, `--change-seat-booking` samt `--upgrade-class`.

Använd `--seat-details` i kombination med `--list-bookings` för att få
platsinformation.

Du kan också hoppa över kopieringen av konfigurationsfilen: kör `--login` i en
terminal, så erbjuder sig verktyget att skapa konfigurationen åt dig och frågar
efter e-post och lösenord. Parametrarna för resan måste dock alltid fyllas i
för hand.

Miljövariabler som stöds:
```bash
LOG_LEVEL=DEBUG|TRACE # diagnostik på stderr (TRACE lägger till httpx trafikloggar)
NO_COLOR=1            # oformaterad utskrift (sker även automatiskt i en pipe)
```

Se `sj-cli --help` för fullständig hjälp och information om kommandoradsflaggor.

### Inloggning

Verktyget hanterar sj.se:s B2C-inloggning och frågar efter en SMS-kod
(två minuters tidsgräns). Token cachas i `~/.cache/sj-cli/token.json` och
förnyas automatiskt; SSO-kakor cachas bredvid, senare fullständiga inloggningar
brukar slippa SMS-steget. `--logout` avslutar sessionen på sj.se och raderar
båda cacharna - nästa inloggning kräver då SMS igen.

## Utveckling

```bash
./venv/bin/pip install -e . --group dev
./venv/bin/pytest                                             # ~589 tester, <1 s, utan nätverk (skriptad fejkklient)
./venv/bin/ruff check . && ./venv/bin/ruff format --check .   # lint + formatering
./venv/bin/mypy                                               # typkontroll
```

Allt konfigureras i `pyproject.toml` (ruff väljer ALL med dokumenterade undantag).
Den redigerbara installationen lägger konsolskriptet `sj-cli` i venv:ens sökväg;
`python -m sj_cli` är likvärdigt.

Struktur: vanlig src-layout — paketet är `src/sj_cli/`, en modul per
ansvarsområde (`cli` ingångspunkt, `auth`, `client` enbart HTTP, `booking`
affärslogik, `config`, `tokens`, `logger`, `output`, `dates`, `seats`,
`stations`, `journey`, `errors`) — se arkitekturtabellen i
[`CLAUDE.md`](CLAUDE.md). `tests/test_booking_flow.py` spikar bokningsflödets
sekvens av API-anrop och dess returkontrakt; kör den efter varje ändring i
`booking.py`. Hemligheter (lösenord, token, autentiseringskoder) maskeras i
loggarna på alla nivåer.

## Ansvarsfriskrivning

Inofficiellt verktyg, utan koppling till eller godkännande från SJ. Det använder
sj.se:s interna webb-API — samma som webbappen använder — vilket kan ändras utan
förvarning, och att automatisera det är kanske inte något SJ:s användarvillkor
tillåter: att köra detta är ditt beslut och din risk, inklusive följderna för
ditt konto. Använd det bara för ditt eget konto och ditt eget kort; du
ansvarar för vad det än bokar.

## Licens

[GNU AGPL v3 eller senare](LICENSE). Du får använda, studera, ändra och dela verktyget;
distribuerar du en ändrad version — eller kör en som en nättjänst som andra använder — måste du
släppa dina ändringar under samma licens. Det kommer utan garanti.
