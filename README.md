# sj-cli

*Read this in [English](README.en.md).*

Ett kommandoradsverktyg som bokar pendlarresor med SJ för dig som har periodkort - t.ex. *SJ Årskort* eller *SJ 30-dagarskort*.

## Varför 
Oavsett vilket periodkort du innehar från SJ krävs det att du bokar biljetter för dina resor - du kan med andra ord inte bara köpa ett årskort och gå på valfri resa *(skillnaden är att när du innehar ett periodkort så kostar din resa 0kr när du bokar - du har ju redan betalt för ditt periodkort)*.

Problemet är bara att du **inte** är garanterad en plats på tåget du vill åka med, dvs. du kan ha köpt ditt årskort i tron om att du kan åka när du vill, men i själva verket så är det mycket möjligt att när du försöker boka din biljett så finns det helt enkelt ingen plats på tåget. Det kräver med andra ord att du måste ha en lång framförhållning och boka din biljett i förväg för att du ska få plats. Att boka dagen innan du ska åka tex. är nästintill omlöjligt *(på den sträcka denna utvecklare åker)*.

Det intressanta här, och hela anledningen till att jag skrev detta cli, är att trots att SJ *påstår* att det är fullt på tåget och att du inte kan boka någon biljett - så har jag aldrig klivit på ett tåg där varenda sittplats i min klass varit upptagen *(trots att det inte gått att boka en biljett alltså)* - och då har jag pendlat i många år. 

Vad detta beror på kan endast SJ svara på, men en gissning är att folk med pendlarkort har bokat en biljett, men sen inte åker med av någon anledning, en annan gissning är att SJ anser att "tåget är fullt" trots att det finns lediga sitplatser *(kanske av säkerhetsskäl eller dylikt)* - oavsett så är det otroligt frustrerande att lägga ut så mycket pengar för ett periodkort men att inte kunna åka med de resor man vill. 

## Lösningen ? 
Det finns säkert många lösningar som SJ skulle kunna applicera för att åtminstone minska problemet en aning, ett förslag skulle ju kunna vara att man är tvungen att "bekräfta" sin biljett 24 timmar innan man använder den, eller att man kanske helt sonika blir uppgraderad till ovan klass om den klass du försöker boka är full - jag vet inte vad problemet beror på så det är svårt att spekulera i en lösning - jag vet bara att jag vill åka med tåget 



på samma sätt som appen på sj.se gör, fast för många dagar på en gång: ett datumintervall,
enstaka dagar eller hela ISO-veckor. Det pratar med samma API som webbappen (bakåtkonstruerat),
så det kan logga in, söka, välja rätt avgång, hitta kortinnehavarens erbjudande till 0 kr och
slutföra bokningen, dag efter dag — helger och svenska röda dagar hoppas över, och en dag som
redan är bokad dubbelbokas aldrig.

Med `--dry-run` händer ingenting på riktigt: flaggan förhandsvisar `--book` och båda
avbokningsflaggorna. En lägesflagga krävs alltid — kör du verktyget utan flaggor skrivs bara
hjälptexten ut.

```
$ sj-cli --book
╭──────────────────────────────────╮
│  operation    booking tickets    │
│  account      user@example.com   │
│  travelpass   SJ Årskort Silver  │
│  holder       First Last         │
╰──────────────────────────────────╯

  route     Göteborg Central ⇄ Stockholm Central
  days      1 sep – 30 oct 2026 · weekdays only
  times     out 06:59 · back 17:22
  ticket    2 class calm · FULLFLEX · SJ High-speed train

tue 15 sep 2026   Göteborg Central ⇄ Stockholm Central
  ✓ searching outbound at 06:59
  ✓ checking offers for outbound at 06:59
  ✓ creating booking with outbound at 06:59
  ✓ searching return at 17:22
  ✓ checking offers for return at 17:22
  ✓ adding return leg at 17:22
  ✓ checking out booking ERU0HWB2
  → 06:59 – 10:04   3h 05m   X 2000 520   carriage 3 seat 17   2 klass Lugn   ERU0HWB2
  ← 17:22 – 20:28   3h 06m   X 2000 543   carriage 3 seat 66   2 klass Lugn   ERU0HWB2

wed 16 sep 2026   tickets already booked

sat 19 sep 2026   weekend

3 day(s) · 1 booked · 1 already booked · 1 skipped
```

Verktygets utskrifter är på engelska. Varje kortbundet läge (bokning, provkörning, avbokning,
listning) inleds med en rubrikruta som namnger operationen, det konfigurerade kontot, periodkortet
och innehavaren. Bokning och provkörning följs av körningens konfiguration som märkta fakta, ett
dämpat förloppsspår, en ruta per resdag (fetstilt datum + sträcka, resorna under) och en dämpad
sammanfattning; provkörningen visar de resor den *skulle* boka i samma rutor. `--list-bookings`
visar rutorna för de bokade dagarna med en `N day(s) · N booking(s)`-fot, `--list-travelpasses`
en ruta per kort. Inloggningslägena (`--login`, `--logout`, `--login-status`) svarar i stället med
en statusruta: grön eller röd punkt + utfall, sedan märkta fakta (konto, sessionens horisont,
tokens giltighetstid).

## Krav

- Python 3.13+
- `httpx` (det enda beroendet vid körning; `pytest`, `ruff` och `mypy` för utveckling)
- Ett SJ-konto med periodkort, och en telefon för engångsverifieringen via SMS

## Installation

```bash
git clone https://github.com/patchon/sj-cli.git && cd sj-cli
python3 -m venv venv
./venv/bin/pip install -e .                 # bara körtid; lägg till `--group dev` för utvecklingsverktygen

mkdir -p ~/.config/sj-cli
cp src/sj_cli/config.example.toml ~/.config/sj-cli/config.toml
$EDITOR ~/.config/sj-cli/config.toml      # inloggning, sträcka, datum, tider
```

Du kan också hoppa över kopieringen: kör `--login` i en terminal, så erbjuder sig verktyget att
skapa konfigurationen åt dig — det frågar efter dina SJ-uppgifter (lösenordet ekas aldrig), skriver
den dokumenterade mallen med uppgifterna ifyllda (filrättigheter 0600) och loggar in direkt. Bara
bokning och datumbaserad avbokning kräver att `[search_parameters]` (sträcka, datum, tider) fylls i
först.

Konfigurationen ligger **utanför** repot med flit — den innehåller ditt SJ-lösenord. `config.toml`
i repots rot ligger i `.gitignore`; bara mallen `src/sj_cli/config.example.toml` är
versionshanterad.

Installationen lägger ett `sj-cli`-kommando i venv:en. Aktivera venv:en en gång per skal och anropa
kommandot vid namn, eller hoppa över aktiveringen och använd hela sökvägen — de två är likvärdiga:

```bash
source venv/bin/activate   # lämna den igen med `deactivate`
sj-cli --login-status

./venv/bin/sj-cli --login-status   # samma sak, ingen aktivering behövs
```

### Konfiguration

```toml
[auth]
email = "user@example.com"
password = "your-password"

[search_parameters]
dates = "2026-09-01..2026-10-30"     # datum och/eller ISO-veckor: "W36, W38..40" (innevarande ISO-år), "2027-W02..03"; passerade dagar hoppas över (ett urval helt i det förflutna är ett fel)
time_leave = "06:59"                 # önskad avgång på utresan (HH:MM, svensk tid)
time_return = "17:22"                # önskad avgång på hemresan; krävs när roundtrip = true
station_from = "Göteborg Central"
station_to = "Stockholm Central"
comfort_class = "2 class calm"       # "1 class", "2 class", "2 class calm"
flexibility = "FULLFLEX"             # FULLFLEX, SEMIFLEX, NOFLEX
roundtrip = true
select_closest_ticket_available = true   # avgången närmast i tid; false = bara exakt tid
allow_class_fallback = true          # valfri: 2 class calm → 2 class när klassen inte finns
book_partial = false                 # valfri: boka hemresan ensam om utresan inte går att få
skip_weekends = true                 # valfri
skip_holidays = true                 # valfri: svenska röda dagar inkl. midsommar-, jul- och nyårsafton
service_types = ["SJ_HIGH", "SJ_IC"] # valfritt filter på tågtyp; utelämna eller ["ALL"] för inget
```

Varje fält valideras innan något nätverksanrop görs, och alla problem rapporteras på en gång.
Giltiga stationer är de som finns i `STATION_MAP` i `src/sj_cli/client.py` (Stockholm, Linköping,
Göteborg, Malmö, Uppsala, Lund — stavningarna "Central"/"C", oberoende av versaler). En
konfiguration som fortfarande använder de gamla nycklarna `date_start`/`date_end` får en rad med
migreringstips.

## Användning

```bash
source venv/bin/activate

sj-cli --book --dry-run        # förhandsvisa: visa vad som skulle bokas, utan att boka
sj-cli --book                  # boka på riktigt
sj-cli --list-bookings         # aktiva bokningar, en ruta per resdag
sj-cli --list-travelpasses     # kort med giltighet, dagar kvar och pris
sj-cli --cancel-date 2026-09-16                # avboka den dagens resor på den konfigurerade sträckan (bokningens övriga dagar behålls)
sj-cli --cancel-date 2026-09-16,2026-09-21..2026-09-25   # flera datum: kommalista och/eller intervall (båda ändpunkterna ingår)
sj-cli --cancel-date W43                       # en hel ISO-vecka (samma grammatik som dates)
sj-cli --cancel-booking JS3TWMF1 --dry-run     # förhandsvisa en avbokning: rutor + vad som skulle avbokas, inga frågor
sj-cli --cancel-booking JS3TWMF1,ABCD1234    # avboka via bokningsnummer (versaler spelar ingen roll)
sj-cli --login                 # logga in, cacha token, avsluta
sj-cli --logout                # avsluta sessionen på sj.se, radera cachad token + kakor
sj-cli --login-status          # exitkod 0 om inloggad — giltig eller förnybar token (för skript)

LOG_LEVEL=DEBUG sj-cli --book --dry-run   # diagnostik på stderr (TRACE lägger till httpx trafikloggar)
NO_COLOR=1 sj-cli --book --dry-run        # oformaterad utskrift (sker även automatiskt i en pipe)
```

Flaggorna utesluter varandra och en lägesflagga krävs (en körning utan flaggor skriver hjälpen och
avslutar med 1). Exitkod 0 vid lyckad körning, 1 vid fel, 130 vid Ctrl-C.

### Första inloggningen

Första körningen gör sj.se:s B2C-inloggning och frågar efter en SMS-kod (två minuters tidsgräns).
Token cachas i `~/.cache/sj-cli/token.json` och förnyas automatiskt; SSO-kakor cachas bredvid, så
senare fullständiga inloggningar brukar slippa SMS-steget. `--logout` avslutar sessionen på sj.se
och raderar båda cacharna — nästa inloggning kräver då SMS igen.

## Så bokas en dag

För varje valt datum (`dates`) från och med i dag:

1. Hoppa över helger och röda dagar (konfigurerbart) — inget API-anrop.
2. Dubblettkontroll mot dina befintliga bokningar: dagen hoppas över om båda resorna (eller den
   enda resan) redan är bokade; annars söks bara den riktning som saknas.
3. Tur och retur → en enda tur-och-retur-sökning → en enda bokning: utresans erbjudande skapar den
   preliminära bokningen, hemresans erbjudande läggs till i den (ett bokningsnummer, precis som i
   SJ:s app).
4. För varje resa: välj avgången närmast den konfigurerade tiden, ta fram komfortklassen (med
   reservval) och hitta kortinnehavarens erbjudande till 0 kr. Har den närmaste avgången inget
   sådant erbjudande provas ett alternativ — tidigare för utresan, senare för hemresan.
5. Slutför bokningen. Dagens ruta avslutas med de bokade resorna precis som `--list-bookings`
   kommer att visa dem (eller skälet till att inget bokades). Preliminära bokningar som en avbruten
   körning lämnat kvar — på din sträcka, äldre än tio minuter — avbokas automatiskt i början av
   nästa `--book`-körning; en varukorg du själv har öppen på sj.se lämnas i fred.

Omförsök: tillfälliga fel vid läsningar görs om (1 s / 2 s / 4 s); boknings- och
utcheckningsanrop görs om bara när anropet aldrig nådde servern, så en hicka i gatewayen kan inte
skapa en dubblettbokning. Tidsgränserna är generösa (30 s) eftersom bokningsanropen i sig tar
sekunder. Alla detaljer, gränsfall och meddelandekatalogen finns i [`SPEC.md`](SPEC.md) (på
engelska).

## Utveckling

```bash
./venv/bin/pip install -e . --group dev   # en gång: projektet + pytest, ruff, mypy
./venv/bin/pytest                         # ~250 tester, <1 s, utan nätverk (skriptad fejkklient)
./venv/bin/ruff check . && ./venv/bin/ruff format --check .   # lint + formatering
./venv/bin/mypy                           # typkontroll
```

Allt konfigureras i `pyproject.toml` (ruff väljer ALL med dokumenterade undantag). Den redigerbara
installationen lägger konsolskriptet `sj-cli` i venv:ens sökväg; `python -m sj_cli` är likvärdigt.

Struktur: vanlig src-layout — paketet är `src/sj_cli/`, en modul per ansvarsområde (`cli`
ingångspunkt, `auth`, `client` enbart HTTP, `booking` affärslogik, `config`, `tokens`, `logger`,
`output`, `dates`, `errors`) — se arkitekturtabellen i [`CLAUDE.md`](CLAUDE.md).
`tests/test_booking_flow.py` spikar bokningsflödets sekvens av API-anrop och dess returkontrakt;
kör den efter varje ändring i `booking.py`. Hemligheter (lösenord, token, autentiseringskoder)
maskeras i loggarna på alla nivåer.

## Ansvarsfriskrivning

Inofficiellt verktyg, utan koppling till eller godkännande från SJ. Det använder sj.se:s interna
webb-API — samma som webbappen använder — vilket kan ändras utan förvarning, och att automatisera
det är kanske inte något SJ:s användarvillkor tillåter: att köra detta är ditt beslut och din risk,
inklusive följderna för ditt konto. Använd det bara för ditt eget konto och ditt eget kort; du
ansvarar för vad det än bokar.

## Licens

[GNU AGPL v3 eller senare](LICENSE). Du får använda, studera, ändra och dela verktyget;
distribuerar du en ändrad version — eller kör en som en nättjänst som andra använder — måste du
släppa dina ändringar under samma licens. Det kommer utan garanti.
