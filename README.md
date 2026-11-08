# ipcam-timelapse

Docker-basert tjeneste for automatisk bildeopptak og timelapse-generering fra IP-kameraer (Reolink og kompatible). Støtter flere kameraer parallelt, PTZ-presets, soloppgang/solnedgang-styring, FTP-opplasting og Pushover-varsler.

## Funksjoner

- Periodisk bildeopptak fra HTTP snapshot-URL (konfigurerbar intervall)
- Tidsstempel brennes inn på bildet (posisjon og format kan tilpasses)
- Støtte for `continuous`- og `sunrise_to_sunset`-modus basert på faktisk astronomisk soloppgang/solnedgang
- Valgfri lagring kun i dagslys (`save_daylight_only`)
- PTZ-presets via Reolink JSON-API med bildesammenligningssjekk (unngår opptak ved manuell bruk)
- Automatisk generering av timelapse-videoer:
  - **Rolling timelapse** — oppdateres hver time (siste N timer)
  - **Avsluttende rolling timelapse** — lages ved solnedgang (ved `save_daylight_only`)
  - **Dagstimelapse** — hele dagen, lages etter solnedgang, navngitt etter ukedag
- FTP-opplasting av siste bilde og timelapse-videoer
- Opprydding av mørke/ubrukelige bilder fra arkiv
- Pushover-varsler ved stans i opptak
- Health check HTTP-endepunkt (`/health`)
- Automatisk config-reload uten omstart

## Filstruktur

```
ipcam-timelapse/
├── timelapse.py           # Applikasjonslogikk
├── config.example.yaml    # Mal med kommentarer og placeholders (trygg å sjekke inn)
├── config.yaml            # Konfigurasjon (ignorert av git, inneholder credentials)
├── Dockerfile
├── requirements.txt
├── deploy/compose.yml     # Mal for compose med ferdigbygd image fra ghcr.io
└── .github/workflows/     # Bygger og publiserer imaget ved push til main
```

## Kom i gang

### Forutsetninger

- Docker og Docker Compose
- IP-kamera med HTTP snapshot-URL

### Oppsett

1. Kopier malen og tilpass (se [Konfigurasjon](#konfigurasjon) nedenfor):

   ```bash
   cp config.example.yaml config.yaml
   ```

   Erstatt alle `<VERDIER I VINKELPARENTES>` og bytt ut eksempel-IP-ene (`192.0.2.x`) med dine egne.
2. Monter config-filen og en bildemappe som volumes i `compose.yml`.
3. Start tjenesten:

```bash
docker compose up -d
```

### compose.yml (eksempel)

```yaml
services:
  iptimelapse:
    image: ghcr.io/terjefl/ipcam-timelapse:latest
    container_name: iptimelapse
    restart: unless-stopped
    environment:
      - TZ=Europe/Oslo
    ports:
      - "8080:8080"
    volumes:
      - /path/to/config.yaml:/app/config.yaml:ro
      - /mnt/nas/IPTimelapse:/images
```

---

## Konfigurasjon

Konfigurasjonsfilen er `config.yaml`. Den lastes automatisk på nytt hvert 30. sekund uten omstart.

### Global-seksjon

```yaml
global:
  timezone: Europe/Oslo       # IANA-tidssone
  health_port: 8080           # Port for /health-endepunktet

  pushover:                   # Valgfritt — Pushover-varsler
    token: "DIN_APP_TOKEN"
    user: "DIN_USER_KEY"
```

### Kamera-seksjon

Hvert kamera defineres som et element under `cameras`:

```yaml
cameras:
  - name: KAMERA1             # Unikt navn — brukes i logging og filnavn
    url: "http://10.0.0.10/snapshot.jpg"   # HTTP snapshot-URL
    root_folder: /images/KAMERA1           # Lokal lagringsmappe
    interval_minutes: 1       # Bildeintervall (minutter)
    mode: continuous          # continuous | sunrise_to_sunset
    save_daylight_only: false # true = lagre kun bilder i dagslys
    latitude: 60.00           # Nødvendig for sun-modus og save_daylight_only
    longitude: 9.29
    sunrise_offset_minutes: -60   # Juster soloppgang (negativt = start tidligere)
    sunset_offset_minutes: 60     # Juster solnedgang (positivt = stopp senere)
```

#### Tidsstempel

```yaml
    timestamp:
      horizontal: center      # left | center | right
      vertical: bottom        # top | bottom
      date_format: "%d.%m.%Y %H:%M:%S"   # Python strftime-format
```

#### FTP-opplasting

```yaml
    ftp:
      enabled: true
      host: ftp.example.com
      port: 21
      username: brukernavn
      password: passord
      folder: /www/KAMERA1           # Mappe på FTP-serveren
      latest_filename: kamera1.jpg   # Siste bilde (overskrives)
      hourly_filename: rolling.mp4   # Rolling timelapse (overskrives)
      # Dagstimelapse: navngitt etter ukedag, f.eks. KAMERA1_mandag.mp4
```

#### Timelapse

```yaml
    timelapse:
      enabled: true
      fps: 10                  # Bilder per sekund i videoen
      rolling_hours: 4         # Antall timer i rolling timelapse
      crf: 23                  # H.264-kvalitet: 0=lossless, 51=lavest; 18–28 er typisk
```

Genererte filer lagres under `<root_folder>/timelapse/`:
- `rolling.mp4` — oppdateres hver time
- `<name>_<ukedag>.mp4` — dagstimelapse

#### PTZ (pan-tilt-zoom)

Brukes for Reolink PTZ-kameraer. Tjenesten beveger kameraet til en lagret preset før hvert bilde tas.

```yaml
    camera:                    # Påkrevd ved bruk av PTZ
      ip: "10.0.0.10"
      username: admin
      password: passord
      channel: 0

    ptz:
      preset_id: 12            # Preset-nummer lagret i kameraet
      settle_seconds: 5        # Ventetid etter preset-kall (sekunder)
      override_minutes: 10     # Tving preset etter N min uten bilde; 0 = aldri
      image_check: true        # Hopp over ved bildeforskjell (manuell bruk)
      image_similarity_threshold: 0.90  # Pearson-korrelasjon (0–1)
      image_check_min_std: 8.0   # Nesten svart bilde (stddev under denne) kan ikke bedømmes; preset kjøres uansett
```

**`image_check`** sammenligner et testbilde med sist kjente referansebilde (64×64 thumbnail, Pearson-korrelasjon). Hvis bildene er ulike nok, antas kameraet å være i manuell posisjon og opptak hoppes over. Om natten er bildet nesten svart, og noen få lamper dominerer korrelasjonen; en lampe som slås av eller på ser da ut som en kamerabevegelse. Er standardavviket i miniatyrbildet under `image_check_min_std`, hoppes bildesjekken over og preset kjøres som normalt.

**`override_minutes`** sørger for at PTZ-presettet tvinges etter angitt antall minutter selv om kameraet er i manuell posisjon.

#### Fokusovervåking

Analyserer bildeskarpheten, retter fokus selv med zoom-jiggle, og varsler først når det ikke lot seg rette innen rimelig tid. Bruker tostegs-deteksjon for å skille mellom uskarp linse og dårlig sikt (tåke, dis, motlys). Fokussjekken påvirker aldri selve opptaket — bildet lagres og lastes opp uansett resultat.

```yaml
    focus:
      enabled: true
      interval_minutes: 5          # Sjekk fokus hvert N-te minutt
      focus_threshold: 20.0        # Laplacian-varians; under denne = uskarp
      light_threshold: 30.0        # Lysnivå under denne = natt, hopp over sjekk
      colour_threshold: 2.0        # Fargemetning under denne = svart/hvitt nattmodus (IR), hopp over sjekk
      confirm_cycles: 2            # Minst N påfølgende uskarpe sjekker ...
      confirm_minutes: 15          # ... og minst N min sammenhengende før uskarpheten regnes som bekreftet
      alert_only: false            # false = zoom-jiggle ved bekreftet uskarphet; true = bare overvåk
      jiggle_wait_minutes: 10      # Ventetid etter første jiggle; dobles per forsøk (10, 20, 40, 80)
      jiggle_max_per_day: 4        # Maks zoom-jiggle per dag
      unfixed_after_minutes: 60    # Uskarpt så lenge uten bedring = «uskarp (ikke rettet)» → 503 og varsel
      recover_factor: 2.0          # Tilbake først når skarpheten er over focus_threshold * N ...
      recover_cycles: 2            # ... i N påfølgende sjekker (hysterese)
      weather_window_minutes: 15   # Annet kamera også uskarpt innen N min = vær, ikke linse (0 = av)
      notify: true                 # Pushover ved «ikke rettet» og når fokus er tilbake
      visibility_threshold: 0.0    # Histogram-stddev under denne = tåke/dis, hopp over (0 = av)
      roi_fraction: 0.6            # Bruk sentrum N% av bildet (1.0 = hele bildet)
      min_sun_elevation: 3         # Deaktiver når solen står lavere enn N grader, morgen og kveld (0 = av)
      exclude_before_sunset_minutes: 0    # Eldre variant: deaktiver N min før solnedgang (0 = av)
      exclude_hours: []            # Faste tidsvindu, f.eks. [{start: "07:30", end: "09:30"}]
      zoom_normal: 19              # Zoom-posisjon ved normal drift (kun ved alert_only: false)
      zoom_out: 17                 # Zoom-posisjon brukt under jiggle
      camera:                      # Påkrevd ved bruk av zoom-jiggle
        ip: "10.0.0.10"
        username: admin
        password: passord
        channel: 0
```

**Forløp ved uskarphet.** Én måling under terskelen gjør ingenting. Først når bildet har vært uskarpt i
`confirm_cycles` sjekker *og* `confirm_minutes` minutter sammenhengende, regnes uskarpheten som bekreftet
(status `uskarp`). Da kjøres zoom-jiggle (med mindre `alert_only: true`), og det ventes `jiggle_wait_minutes`
før neste forsøk, med dobling for hvert forsøk og tak på `jiggle_max_per_day`. Hjelper ingenting innen
`unfixed_after_minutes`, settes status `uskarp (ikke rettet)`: `/health/<kamera>/focus` gir 503 og ett
Pushover-varsel sendes. Status går tilbake til `ok` først når skarpheten er over `focus_threshold * recover_factor`
i `recover_cycles` sjekker på rad, så lett dis ikke gir vipping fram og tilbake. En pause på over to timer
(natt) nullstiller sporingen, så gårsdagens uskarphet vurderes på nytt neste morgen.

**Vær, ikke linse** (`weather_window_minutes`): Er flere kameraer uskarpe innenfor samme vindu, regnes det som
tåke eller dis, status blir `inaktiv (dårlig sikt (flere kameraer))` og ingenting gjøres. Ingen linse mister fokus
på to kameraer samtidig.

**Siktsjekk** (`visibility_threshold`): Lavt standardavvik på histogrammet betyr et uniformt, flatt bilde — typisk tåke, dis eller motlys. Fokussjekken hoppes da over for å unngå falske varsler. Typiske verdier: klart vær ~40–80, lett dis ~20–35, tett tåke <15.

**Fokussjekk** (`roi_fraction`): Laplacian-varians beregnes kun på sentrum-ROI for å unngå påvirkning fra himmel og bilkanter.

**Nattmodus** (`colour_threshold`): Reolink-kameraer går over i svart/hvitt med IR-lys når det blir mørkt. Forsterkningen holder lysnivået oppe (typisk 100–150), så `light_threshold` fanger det ikke, mens støyreduksjonen gjør bildet glatt og gir falske «uskarp». Fargemetning ≈ 0 avslører nattmodus sikkert; i farger ligger den typisk på 25–55.

**Solhøyde** (`min_sun_elevation`): Rett etter soloppgang og før solnedgang gir lavt lys og morgentåke lave skarphetsverdier uten at linsen er ute av fokus. Sjekken hoppes over når solen står lavere enn N grader, symmetrisk morgen og kveld. Erfaringsverdi: stabile målinger fra ca. 2–3°. Ved 60°N står solen 6,6° over horisonten midt på dagen 21. desember, så 3° stenger ikke vinteren.

**Fokussjekk deaktiveres automatisk** etter solnedgang (styrt av `latitude`/`longitude`), ved lav sol, i nattmodus samt i konfigurerte tidsvinduer. Årsaken vises som `inaktiv (…)` i `/health` og på dashbordet.

#### Stall-varsel (Pushover)

```yaml
    stall_alert:
      enabled: true
      minutes: 5               # Varsle etter N minutters stans
```

Krever at `pushover` er konfigurert i `global`-seksjonen. Sender varsel når stansen oppdages, og ny melding når opptaket er gjenopptatt.

#### Mørk-opprydding

Sletter automatisk bilder som er for mørke til å være nyttige.

```yaml
    dark_cleanup:
      enabled: true
      run_at: "12:00"          # Tidspunkt for kjøring (HH:MM)
      lookback_hours: 24       # Sjekk bilder fra de siste N timene
      darkness_threshold: 30   # Pikselverdi-terskel (0–255)
      dark_percent_limit: 98.5 # % mørke piksler som kreves for sletting
```

Et bilde slettes hvis minst `dark_percent_limit` % av pikslene har verdi under `darkness_threshold`.

---

## Lagringsstruktur

```
<root_folder>/
├── latest.jpg                      # Siste bilde (overskrives alltid)
├── 2026-04-01/
│   ├── 2026-04-01_08-00-00.jpg
│   ├── 2026-04-01_08-01-00.jpg
│   └── ...
├── 2026-04-02/
│   └── ...
└── timelapse/
    ├── rolling.mp4                 # Rolling timelapse (siste N timer)
    └── KAMERA1_onsdag.mp4          # Dagstimelapse
```

---

## Health check

Tjenesten eksponerer HTTP-endepunkter på konfigurert port (standard `8080`):

### Samlet status

```
GET /health
```

Returnerer JSON med status for alle kameraer. HTTP 200 ved ok/warning/starting, 503 ved error.

### Per-kamera endepunkter

```
GET /health/{name}           — Overall status for kameraet
GET /health/{name}/capture   — Kun bildestatus (tar kameraet bilder?)
GET /health/{name}/ftp       — FTP-opplasting (feiler siste opplasting?)
GET /health/{name}/focus     — Fokus-status (kun kameraer med focus: enabled: true)
```

Alle returnerer `200 OK` eller `503 Service Unavailable` med en kort tekstmelding — egnet for Uptime Kuma og lignende overvåkingsverktøy.

**Planlagte hopp er ikke feil.** Når PTZ-bildesjekken finner at kameraet står i manuell posisjon, telles hoppet som aktivitet: status blir `paused`, og verken `/health`, capture-endepunktet eller stall-varselet slår ut. Feil gis først når override-grensen passeres uten bilde.

**Fokus-endepunktet** returnerer 200 også når fokussjekken er inaktiv (natt, nattmodus, lav sol, dårlig sikt) og mens uskarphet håndteres med zoom-jiggle (`uskarp`). Kun `uskarp (ikke rettet)` gir 503. `/health` har i tillegg `focus_detail` per kamera med tilstand, hvor lenge bildet har vært uskarpt, antall jiggle i dag og siste skarphets- og siktverdi.

**Feilmeldinger** i `last_error`, logg og dashbord maskeres for `user=`, `password=` og `token=` i URL-er, slik at kamerapassordet i snapshot-URL-en ikke lekker ut via port 8080.

| Status | Beskrivelse | HTTP |
|--------|-------------|------|
| `ok` | Opptak fungerer normalt | 200 |
| `starting` | Venter på første bilde | 200 |
| `warning` | Noen feil, men fortsatt aktiv | 200 |
| `paused` | Opptak hoppet over med vilje (PTZ i manuell posisjon) | 200 |
| `error` | Mer enn 3 påfølgende feil eller overdue | 503 |

### Eksempel — Uptime Kuma oppsett for BJORE1-NV

| Monitor | URL | Type |
|---------|-----|------|
| BJORE1-NV capture | `http://<host>:8080/health/BJORE1-NV/capture` | HTTP(s) |
| BJORE1-NV FTP | `http://<host>:8080/health/BJORE1-NV/ftp` | HTTP(s) |
| BJORE1-NV fokus | `http://<host>:8080/health/BJORE1-NV/focus` | HTTP(s) |

Docker-imaget er konfigurert med `HEALTHCHECK` som bruker `/health`-endepunktet.

---

## Sikkerhet

> **Advarsel:** `config.yaml` inneholder passord og API-tokens. Sjekk aldri denne filen inn i versjonskontroll med faktiske verdier. Bruk `.gitignore` eller miljøvariabler.

`config.example.yaml` inneholder kun placeholders og er trygg å sjekke inn. Hold den oppdatert når nye innstillinger legges til.

Anbefalt `.gitignore`-oppføring:
```
config.yaml
!config.example.yaml
```

---

## Avhengigheter

| Pakke | Bruk |
|-------|------|
| `requests` | HTTP-henting av kamerabilder og PTZ-API |
| `Pillow` | Bildebehandling og tidsstempel |
| `astral` | Beregning av soloppgang/solnedgang |
| `PyYAML` | Innlasting av konfigurasjonsfil |
| `numpy` | Mørkhetsvurdering og bildesammenligning |
| `ffmpeg` | Videoenkoding (systemavhengighet, installert i Docker-image) |

---

## Image, bygg og utvikling

GitHub Actions bygger `ghcr.io/terjefl/ipcam-timelapse:latest` og `:sha-<kort>` ved push til `main`
(offentlig pakke). Syntaks- og importsjekk kjører først, så et ødelagt image ikke publiseres.
`config.yaml` trenger aldri nytt image: den leses på nytt hvert 30. sekund.

Oppdater en kjørende installasjon ved å trekke nytt image og starte containeren på nytt
(Portainer: «Pull and redeploy» med re-pull). Rull tilbake ved å sette `image:` til en `:sha-<kort>`-tag.

```bash
# Bygg image lokalt
docker build -t ipcam-timelapse .

# Kjør med lokal config
docker run --rm \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  -v /tmp/images:/images \
  -p 8080:8080 \
  ipcam-timelapse
```
