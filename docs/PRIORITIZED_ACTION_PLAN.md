# Priorisierte Aktionsliste -- Schwachstellen-Behebung

**Projekt:** ankermake-m5-protocol
**Datum:** 2026-02-08
**Bezug:** [CODE_REVIEW_REPORT.md](./CODE_REVIEW_REPORT.md)

---

## Wie diese Liste zu lesen ist

Die Schwachstellen sind in **4 Sprints** aufgeteilt. Jeder Sprint baut auf dem vorherigen auf.
Innerhalb eines Sprints sind die Punkte nach **Risiko x Aufwand** sortiert -- oben stehen
Dinge, die mit wenig Aufwand den groessten Sicherheitsgewinn bringen ("Quick Wins" zuerst).

> **Faustregel:** Sprint 1 sollte **vor dem naechsten Release** abgeschlossen sein.
> Sprint 2 zeitnah danach. Sprint 3 und 4 koennen ins Backlog.

---

## Sprint 1: Sofort -- Sicherheitskritisch & Crash-Bugs

Diese Punkte muessen **vor jeder weiteren Nutzung im Netzwerk** behoben werden.

### 1.1 NoneType-Crash in `ankerctl.py` beheben [BUG-02]

| | |
|---|---|
| **Aufwand** | 5 Minuten |
| **Risiko** | Garantierter Crash auf Linux |
| **Dateien** | `ankerctl.py` (Zeilen 395-401, 442-445) |

**Was zu tun ist:**
- Nach jedem `log.critical()`-Aufruf ein `sys.exit(1)` einfuegen
- Betrifft `config_decode()` und `config_import()`
- Gleiches Problem in `cli/pppp.py:29-31` und `cli/mqtt.py:18-20` (BUG-10)

**Worauf achten:**
- Nicht `return` verwenden, wenn die Funktion noch Logik danach hat, die auf `fd` zugreift
- `sys.exit(1)` ist hier korrekt, weil es ein nicht-behebbarer Zustand ist

---

### 1.2 File-Handle-Leaks schliessen [LEAK-01, LEAK-02, LEAK-03]

| | |
|---|---|
| **Aufwand** | 15 Minuten |
| **Risiko** | Resource-Exhaustion bei Dauerbetrieb |
| **Dateien** | `ankerctl.py`, `libflagship/ppppapi.py:57`, `cli/config.py:78,227` |

**Was zu tun ist:**
- Alle `open()` durch `with open() as f:` ersetzen
- Konkret 5 Stellen im Code

**Worauf achten:**
- Bei `ppppapi.py:57` wird `data = open(filename, "rb").read()` verwendet -- hier reicht
  `with open(filename, "rb") as f: data = f.read()`
- Bei `cli/config.py:78` steht `json.load(path.open())` -- umschreiben zu
  `with path.open() as f: json.load(f)`

---

### 1.3 Authentifizierung fuer den Webserver [SEC-01]

| | |
|---|---|
| **Aufwand** | 2-4 Stunden |
| **Risiko** | **Hoechstes Sicherheitsrisiko im Projekt** |
| **Dateien** | `web/__init__.py`, `web/config.py` |

**Was zu tun ist:**
- Minimalloesung: Token-basierte Auth
  - Beim ersten Start zufaelligen Token generieren und in Config speichern
  - Jeder Request muss Token als Header (`Authorization: Bearer <token>`) oder Query-Parameter mitschicken
  - Token in der Web-UI anzeigen/kopierbar machen
- Alternativ: `flask-httpauth` mit Basic-Auth (Benutzername/Passwort in Config)

**Worauf achten:**
- WebSocket-Endpunkte nicht vergessen -- diese muessen den Token im Handshake pruefen
- `/api/files/local` (Upload), `/api/printer/gcode` und `/ws/ctrl` sind die kritischsten Endpunkte
- Timing-sichere Token-Vergleiche verwenden (`hmac.compare_digest`)
- OctoPrint-Kompatibilitaetsendpunkte (`/api/version`) sollten ebenfalls geschuetzt sein

---

### 1.4 CSRF-Schutz aktivieren [SEC-04]

| | |
|---|---|
| **Aufwand** | 30 Minuten |
| **Risiko** | Angriff ueber beliebige Webseite moeglich |
| **Dateien** | `web/__init__.py`, Templates |

**Was zu tun ist:**
1. `pip install flask-wtf` und in `requirements.txt` aufnehmen
2. In `web/__init__.py`:
   ```python
   from flask_wtf.csrf import CSRFProtect
   csrf = CSRFProtect(app)
   ```
3. In Templates das CSRF-Token in Formulare einfuegen
4. **Sofort-Fix:** `/api/ankerctl/server/reload` von GET auf POST umstellen

**Worauf achten:**
- API-Endpunkte, die von JavaScript aufgerufen werden, brauchen das Token im Header
- WebSocket-Verbindungen sind von CSRF nicht betroffen (Origin-Check ist dort relevanter)
- OctoPrint-kompatible Clients koennen durch CSRF-Tokens brechen -- ggf. API-Key-Auth als Alternative

---

### 1.5 WebSocket Origin-Validierung [SEC-05]

| | |
|---|---|
| **Aufwand** | 30 Minuten |
| **Risiko** | Jede Webseite kann WebSocket-Verbindung oeffnen |
| **Dateien** | `web/__init__.py` (alle 5 WebSocket-Handler) |

**Was zu tun ist:**
- Decorator oder Hilfsfunktion erstellen:
  ```python
  def check_ws_origin(request):
      origin = request.headers.get("Origin", "")
      allowed = [f"http://localhost:{port}", f"http://127.0.0.1:{port}"]
      if origin and origin not in allowed:
          raise Forbidden("Invalid origin")
  ```
- Am Anfang jedes WebSocket-Handlers aufrufen

**Worauf achten:**
- `Origin`-Header kann bei Browser-Requests nicht gefaelscht werden
- Konfigurierbare Whitelist fuer Nutzer, die ueber andere IPs zugreifen
- Login-Check (`if not app.config["login"]`) steht schon in jedem Handler -- Origin-Check danach einfuegen

---

### 1.6 Thread-Safety in `ppppapi.py` herstellen [BUG-01]

| | |
|---|---|
| **Aufwand** | 1-2 Stunden |
| **Risiko** | Datenkorruption, verlorene Pakete, Crashes |
| **Dateien** | `libflagship/ppppapi.py` |

**Was zu tun ist:**
- Alle Zugriffe auf `txqueue`, `backlog`, `rxqueue`, `acks` in der `Channel`-Klasse
  mit `self.lock` schuetzen
- Besser: `txqueue` und `rxqueue` auf `queue.Queue` umstellen (thread-safe by design)

**Worauf achten:**
- Lock-Ordering beachten -- wenn mehrere Locks noetig sind, immer in gleicher Reihenfolge
- `with self.lock:` verwenden statt `lock.acquire()`/`lock.release()`
- `queue.Queue` hat `.put()`, `.get()`, `.empty()` -- API ist anders als bei Listen
- Performance: Lock-Granularitaet pruefen -- nicht den gesamten `poll()`-Loop locken

---

## Sprint 2: Zeitnah -- Stabilitaet & weitere Sicherheit

### 2.1 MQTT-Queue auf `queue.Queue` umstellen [BUG-03]

| | |
|---|---|
| **Aufwand** | 30 Minuten |
| **Dateien** | `libflagship/mqttapi.py` |

**Was zu tun ist:**
- `self._queue = []` ersetzen durch `self._queue = queue.Queue()`
- `append()` -> `put()`, Iteration -> `get()` mit Timeout
- `clear_queue()` umschreiben (Queue leeren bis `empty()`)

**Worauf achten:**
- `fetchloop()` nutzt `timeout` -- `queue.Queue.get(timeout=...)` passt hier direkt

---

### 2.2 Handler-Listen thread-sicher machen [BUG-04, BUG-05]

| | |
|---|---|
| **Aufwand** | 45 Minuten |
| **Dateien** | `web/lib/service.py`, `web/service/pppp.py` |

**Was zu tun ist:**
- `threading.Lock` fuer `self.handlers` und `self.xzyh_handlers`
- In `notify()`: `with self.lock: handlers = list(self.handlers)` vor Iteration

**Worauf achten:**
- Keine Locks halten waehrend Callback-Aufrufen (Deadlock-Gefahr)
- Kopie der Liste erstellen, dann Lock freigeben, dann iterieren

---

### 2.3 Socket-Timeouts setzen [BUG-08]

| | |
|---|---|
| **Aufwand** | 15 Minuten |
| **Dateien** | `libflagship/ppppapi.py` |

**Was zu tun ist:**
- `self.sock.settimeout(30)` nach Socket-Erstellung
- In `recv_aabb()`: `socket.timeout`-Exception abfangen und Reconnect ausloesen

---

### 2.4 `ServiceManager.refs` absichern [BUG-06]

| | |
|---|---|
| **Aufwand** | 20 Minuten |
| **Dateien** | `web/lib/service.py` |

**Was zu tun ist:**
- `threading.Lock` fuer `get()`/`put()`-Operationen auf `self.refs`

---

### 2.5 TLS-Verifizierung aktivieren [SEC-06]

| | |
|---|---|
| **Aufwand** | 15 Minuten |
| **Dateien** | `libflagship/httpapi.py` |

**Was zu tun ist:**
- `verify=False` entfernen (Standard ist `True`)
- Falls Anker-Server Custom-Zertifikate nutzen: CA-Bundle konfigurierbar machen

**Worauf achten:**
- Testen ob Anker-API mit Standard-CA-Bundle funktioniert
- Falls nicht: Zertifikat pinnen oder Custom-CA-Bundle mitliefern

---

### 2.6 Mutable Default Argument fixen [CC-10]

| | |
|---|---|
| **Aufwand** | 5 Minuten |
| **Dateien** | `libflagship/httpapi.py:117` |

**Was zu tun ist:**
```python
# Vorher:
def equipment_get_dsk_keys(self, station_sns, invalid_dsks={}):
# Nachher:
def equipment_get_dsk_keys(self, station_sns, invalid_dsks=None):
    if invalid_dsks is None:
        invalid_dsks = {}
```

---

### 2.7 Checksummen-Fehler nicht ignorieren [BUG-09]

| | |
|---|---|
| **Aufwand** | 10 Minuten |
| **Dateien** | `libflagship/megajank.py:35-39` |

**Was zu tun ist:**
- Auskommentiertes `raise` wieder aktivieren
- Oder: explizit loggen mit `log.error()` statt `print()`

---

### 2.8 Exception-Handling praezisieren [CC-11]

| | |
|---|---|
| **Aufwand** | 30 Minuten |
| **Dateien** | `ankerctl.py:55-56` und weitere |

**Was zu tun ist:**
- `except Exception` durch spezifische Exceptions ersetzen
- Nach `log.critical()` immer `sys.exit(1)` oder Re-Raise

---

## Sprint 3: Mittelfristig -- Code-Qualitaet

| # | Task | Aufwand | Dateien |
|---|------|---------|---------|
| 3.1 | Config-Logik deduplizieren [CC-02] | 1h | `ankerctl.py` |
| 3.2 | `_parse_bool` in Utility verschieben [CC-03] | 15min | `web/notifications.py`, `libflagship/notifications/apprise_client.py` |
| 3.3 | Login-Check als Decorator [CC-04] | 30min | `web/__init__.py` |
| 3.4 | `_handle_notification` aufteilen [CC-05] | 1h | `web/service/mqtt.py` |
| 3.5 | Flask Blueprints einfuehren [CC-06] | 2-3h | `web/__init__.py` -> `web/routes/` |
| 3.6 | Magic Numbers durch Konstanten ersetzen [CC-09] | 1h | Diverse |
| 3.7 | Built-in-Shadowing beheben [CC-01] | 30min | Diverse |
| 3.8 | `rxqueue`-Groessenlimit [LEAK-05] | 30min | `libflagship/ppppapi.py` |
| 3.9 | Socket in `stop()` schliessen [LEAK-04] | 10min | `libflagship/ppppapi.py` |
| 3.10 | JSON.parse Error-Handling im Frontend [FE-02] | 15min | `static/ankersrv.js` |
| 3.11 | Globale Variable `sockets` fixen [FE-01] | 5min | `static/ankersrv.js` |
| 3.12 | Exception-Messages nicht an User zeigen [SEC-09] | 20min | `web/__init__.py` |

---

## Sprint 4: Langfristig -- Infrastruktur & Haertung

| # | Task | Aufwand | Beschreibung |
|---|------|---------|-------------|
| 4.1 | Automatisierte Tests einfuehren | 1-2 Tage | pytest fuer kritische Pfade (ppppapi, mqttapi, megajank) |
| 4.2 | CI/CD mit Linting | 2-3h | `mypy`, `pylint`, `bandit` in GitHub Actions |
| 4.3 | Type-Hints schrittweise einfuehren [CC-07] | Fortlaufend | Beginnend bei oeffentlichen APIs |
| 4.4 | Docker Non-Root User [DOCKER-01] | 30min | `USER`-Direktive im Dockerfile |
| 4.5 | Docker Base-Image aktualisieren [DOCKER-04] | 15min | `python:3.12-bookworm` |
| 4.6 | Credential-Speicher auf Keyring umstellen [SEC-02] | 2-3h | `keyring`-Bibliothek integrieren |
| 4.7 | AES-GCM statt AES-CBC [SEC-03] | 2h | `libflagship/megajank.py` |
| 4.8 | Content-Security-Policy Header | 30min | `web/__init__.py` |
| 4.9 | Dependency-Updates | 1h | `paho_mqtt` v2.x, aktuelles Flask |
| 4.10 | SSL-Material aus Docker-Image entfernen [DOCKER-03] | 15min | Volume-Mount statt COPY |

---

## Checkliste: Worauf generell achten

### Beim Coden

- [ ] **Immer `with` fuer Dateien und Locks** -- nie `open()` ohne Context-Manager
- [ ] **`queue.Queue` statt `list`** fuer Thread-Kommunikation
- [ ] **Kein `except Exception`** -- spezifische Exceptions fangen
- [ ] **Nach `log.critical()` immer beenden** -- `sys.exit(1)` oder `raise`
- [ ] **Keine Mutable Default Arguments** -- `None` als Default, dann im Body initialisieren
- [ ] **Keine Built-in-Namen shadowing** -- nicht `str`, `type`, `hash`, `input`, `len` als Variablennamen

### Bei Sicherheitsaenderungen

- [ ] **Jeden neuen Endpunkt mit Auth versehen** -- nie "erstmal ohne, spaeter hinzufuegen"
- [ ] **State-Changes nur via POST/PUT/DELETE** -- nie GET fuer Aenderungen
- [ ] **Input immer validieren** -- Schema, Typ, Laenge, Wertebereich
- [ ] **Timing-sichere Vergleiche** fuer Tokens (`hmac.compare_digest`)
- [ ] **Fehler-Details nur in Logs** -- generische Meldungen an User

### Beim Review

- [ ] **Thread-Safety pruefen** -- wird eine Variable von mehreren Threads genutzt?
- [ ] **Resource-Cleanup pruefen** -- wird jede geoeffnete Resource auch geschlossen?
- [ ] **Error-Pfade pruefen** -- was passiert wenn ein Aufruf fehlschlaegt? Geht die Ausfuehrung weiter?
- [ ] **Boundary-Checks** -- was passiert bei leerem Input, `None`, negativen Werten?

---

## Aufwandsschaetzung Gesamt

| Sprint | Aufwand geschaetzt | Kritikalitaet |
|--------|-------------------|---------------|
| Sprint 1 | 5-9 Stunden | **Muss vor Produktion** |
| Sprint 2 | 3-4 Stunden | **Naechstes Release** |
| Sprint 3 | 7-9 Stunden | Backlog |
| Sprint 4 | 2-3 Tage | Backlog |

---

*Referenz: Vollstaendige Details zu jedem Finding in [CODE_REVIEW_REPORT.md](./CODE_REVIEW_REPORT.md)*
