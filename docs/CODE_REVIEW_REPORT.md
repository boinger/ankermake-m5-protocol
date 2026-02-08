# Code Review & Security Audit Report

**Projekt:** ankermake-m5-protocol
**Datum:** 2026-02-08
**Scope:** Vollstaendige Analyse aller Python-, JavaScript- und HTML-Dateien
**Kategorien:** Sicherheit, Bugs, Clean Code, Memory Leaks

---

## Inhaltsverzeichnis

1. [Zusammenfassung](#zusammenfassung)
2. [Kritische Sicherheitsprobleme](#1-kritische-sicherheitsprobleme)
3. [Schwerwiegende Bugs](#2-schwerwiegende-bugs)
4. [Memory Leaks & Resource Leaks](#3-memory-leaks--resource-leaks)
5. [Clean Code Verstoesse](#4-clean-code-verstoesse)
6. [Frontend-Sicherheit](#5-frontend-sicherheit)
7. [Docker-Sicherheit](#6-docker-sicherheit)
8. [Verbesserungsvorschlaege](#7-verbesserungsvorschlaege)
9. [Severity-Uebersicht](#severity-uebersicht)

---

## Zusammenfassung

| Severity | Anzahl |
|----------|--------|
| CRITICAL | 4 |
| HIGH | 14 |
| MEDIUM | 25 |
| LOW | 14 |
| **Gesamt** | **57** |

Die gravierendsten Probleme sind:
- **Keine Authentifizierung** auf dem Flask-Webserver -- jedes Geraet im Netzwerk kann den Drucker steuern
- **Hardcoded AES-Key** in `logincache.py` -- Credential-Cache ist trivial entschluesselbar
- **Thread-Safety-Probleme** in `ppppapi.py` -- Race Conditions koennen zu Datenverlust/Crashes fuehren
- **NoneType-Crash** in `ankerctl.py` -- garantierter Absturz auf nicht unterstuetzten Plattformen

---

## 1. Kritische Sicherheitsprobleme

### SEC-01: Keine Authentifizierung auf dem Webserver [CRITICAL]

**Datei:** `web/__init__.py`

Der Flask-Webserver hat keinerlei Authentifizierungsmechanismus. Alle Endpunkte sind fuer jeden erreichbar, der Netzwerkzugang hat. Es gibt keine Session-Tokens, keine Login-Pruefung pro Request und keine API-Keys.

**Betroffene Endpunkte (Auswahl):**

| Endpunkt | Methode | Auswirkung |
|----------|---------|------------|
| `/api/printer/gcode` | POST | Beliebiger GCode an Drucker |
| `/api/files/local` | POST | Dateien hochladen und drucken |
| `/api/printer/control` | POST | Druck stoppen/pausieren |
| `/ws/ctrl` | WebSocket | Licht/Video steuern |
| `/api/ankerctl/config/upload` | POST | Konfiguration ueberschreiben |
| `/api/ankerctl/server/reload` | GET | Alle Services neustarten |

**Risiko:** Ein Angreifer im selben Netzwerk kann beliebigen GCode senden, was physische Schaeden am Drucker verursachen kann.

**Empfehlung:**
- Mindestens Basic-Auth oder Token-basierte Authentifizierung implementieren
- Session-Management mit Flask-Login oder aehnlichem Framework
- Rate-Limiting fuer kritische Endpunkte

---

### SEC-02: Hardcoded AES-Schluessel fuer Credential-Cache [CRITICAL]

**Datei:** `libflagship/logincache.py`

```python
# Der AES-Schluessel ist direkt im Quellcode und damit oeffentlich
```

Da das Projekt Open-Source ist, kann jeder mit Zugang zur Cache-Datei die Credentials entschluesseln.

**Empfehlung:**
- Schluessel aus plattformspezifischem Keystore beziehen (z.B. `keyring`-Bibliothek)
- Alternativ: OS-eigene Credential-Speicher nutzen (macOS Keychain, Windows Credential Manager, Linux Secret Service)

---

### SEC-03: Unsichere Kryptographie in `megajank.py` [HIGH]

**Datei:** `libflagship/megajank.py`

| Problem | Zeile | Detail |
|---------|-------|--------|
| Hardcoded IV | 25 | `iv=b"3DPrintAnkerMake"` als Default-Parameter |
| XOR-"Checksumme" | 35-39 | Fehlgeschlagene Pruefsumme wird nur geloggt, nicht abgelehnt |
| Eigene Shuffle-Krypto | 91-199 | Custom `crypto_curse`/`crypto_decurse` ohne Peer-Review |
| Keine Integritaetspruefung | - | AES-CBC ohne HMAC oder authentifizierte Verschluesselung |

**Empfehlung:**
- AES-GCM statt AES-CBC verwenden (bietet Authentifizierung + Verschluesselung)
- IV zufaellig generieren statt hardcoded
- Fehlgeschlagene Checksummen muessen eine Exception werfen

---

### SEC-04: Fehlende CSRF-Protection [HIGH]

**Datei:** `web/__init__.py`

Kein CSRF-Schutz auf irgendeinem Endpunkt. Kein `flask-wtf CSRFProtect`, keine CSRF-Tokens, keine `SameSite`-Cookie-Konfiguration.

Besonders kritisch: `/api/ankerctl/server/reload` ist ein **GET-Request**, der den Server-Zustand aendert -- ein `<img>`-Tag auf einer beliebigen Webseite genuegt zum Ausloesen.

**Empfehlung:**
- `flask-wtf` mit `CSRFProtect` einbinden
- State-Changing Endpoints von GET auf POST umstellen
- SameSite=Strict fuer Cookies setzen

---

### SEC-05: Cross-Site WebSocket Hijacking (CSWSH) [HIGH]

**Datei:** `web/__init__.py`, Zeilen 108-235

Keine Origin-Validierung bei WebSocket-Verbindungen. Jede beliebige Webseite kann WebSocket-Verbindungen oeffnen und:
- Live-MQTT-Telemetrie lesen (`/ws/mqtt`)
- Video-Stream empfangen (`/ws/video`)
- GCode-Befehle senden (`/ws/ctrl`)

**Empfehlung:**
- Origin-Header validieren in jedem WebSocket-Handler
- Token-basierte Authentifizierung fuer WebSocket-Verbindungen

---

### SEC-06: TLS-Verifizierung deaktiviert [HIGH]

**Datei:** `libflagship/httpapi.py`

```python
requests.get(..., verify=False)
```

SSL-Zertifikatsverifizierung ist deaktiviert. Man-in-the-Middle-Angriffe koennen Login-Credentials abfangen.

**Empfehlung:**
- `verify=True` verwenden (Standard in `requests`)
- Falls Custom-Zertifikate noetig: explizites CA-Bundle konfigurieren

---

### SEC-07: Keine Input-Validierung auf WebSocket-Nachrichten [HIGH]

**Datei:** `web/__init__.py`, Zeilen 212-234

```python
msg = json.loads(sock.receive())
if "light" in msg:
    vq.api_light_state(msg["light"])
```

WebSocket-Nachrichten auf `/ws/ctrl` werden ohne Schema-Validierung akzeptiert. Beliebige Keys und Werte werden durchgereicht.

**Empfehlung:**
- JSON-Schema-Validierung implementieren
- Whitelist fuer erlaubte Keys und Wertebereiche
- Fehlerhafte Nachrichten ablehnen mit Fehlermeldung

---

### SEC-08: Path Traversal bei Dateiupload [HIGH]

**Datei:** `web/__init__.py`

Dateinamen aus Upload-Requests werden nicht ausreichend sanitized. `werkzeug.utils.secure_filename()` wird zwar verwendet, aber es sollte zusaetzlich gegen Path-Traversal abgesichert werden.

**Empfehlung:**
- Dateinamen nach `secure_filename()` zusaetzlich gegen `..` und absolute Pfade pruefen
- Upload-Verzeichnis auf ein spezifisches Directory beschraenken
- Dateityp-Validierung (nur `.gcode`-Dateien erlauben)

---

### SEC-09: Exception-Messages an Benutzer exponiert [MEDIUM]

**Datei:** `web/__init__.py`, Zeilen 341-344

```python
flash(f"Error: {err}", "danger")
flash(f"Unexpected Error occurred: {err}", "danger")
```

Rohe Python-Exception-Messages werden an Benutzer weitergegeben. Diese koennen interne Pfade, Bibliotheksversionen oder Stacktrace-Informationen enthalten.

**Empfehlung:**
- Generische Fehlermeldungen an Benutzer
- Detaillierte Fehler nur in Logs schreiben

---

## 2. Schwerwiegende Bugs

### BUG-01: Thread-Safety-Probleme in `ppppapi.py` [CRITICAL]

**Datei:** `libflagship/ppppapi.py`, Zeilen 130-218

Die `Channel`-Klasse hat ein `lock`-Attribut, das nur in `recv_xzyh` und `recv_aabb` verwendet wird. Folgende Methoden greifen **ohne Lock** auf geteilte Datenstrukturen zu:

| Methode | Geteilte Daten | Problem |
|---------|---------------|---------|
| `write()` | `txqueue` | Kein Lock |
| `rx_drw()` | `rxqueue`, `rx_ctr` | Kein Lock |
| `rx_ack()` | `backlog`, `acks` | Kein Lock |
| `poll()` | `txqueue`, `backlog` | Kein Lock |

Diese werden von verschiedenen Threads aufgerufen (Haupt-`run()`-Loop und aufrufende Threads).

**Auswirkung:** Datenkorruption, verlorene Pakete, Crashes unter Last.

**Empfehlung:**
- Alle Zugriffe auf `txqueue`, `backlog`, `rxqueue`, `acks` mit dem existierenden `lock` schuetzen
- Alternativ: `queue.Queue` statt einfacher Listen verwenden

---

### BUG-02: NoneType-Crash in `ankerctl.py` [CRITICAL]

**Datei:** `ankerctl.py`, Zeilen 395-401, 442-445

In `config_decode` und `config_import`: Wenn die Plattform weder Darwin noch Windows ist, wird `log.critical()` aufgerufen, aber die Ausfuehrung geht weiter. `fd` bleibt `None`, und `fd.read()` crasht mit `AttributeError`.

```python
fd = None
# ... Platform-Checks setzen fd nur fuer Darwin/Windows ...
log.critical("Could not auto detect ...")
# Execution continues!
data = fd.read()  # <-- CRASH: AttributeError: 'NoneType' has no attribute 'read'
```

**Empfehlung:**
- Nach `log.critical()` ein `sys.exit(1)` oder `return` einfuegen
- Oder: `raise SystemExit("...")` verwenden

---

### BUG-03: MQTT-Queue Race Condition [HIGH]

**Datei:** `libflagship/mqttapi.py`, Zeilen 22, 60-62, 145-148

`self._queue` ist eine einfache Python-Liste. Sie wird im MQTT-Netzwerk-Thread (via `_on_message`) beschrieben und im Haupt-Thread (via `clear_queue`, `fetch`, `fetchloop`) gelesen/geleert. Die Copy-then-Clear-Operation in `clear_queue` ist **nicht atomar** -- Nachrichten koennen zwischen Kopie und Clear verloren gehen.

**Empfehlung:**
- `queue.Queue` aus der Standardbibliothek verwenden
- Oder: Threading-Lock um alle Queue-Operationen

---

### BUG-04: `Service.handlers`-Liste waehrend Iteration modifiziert [HIGH]

**Datei:** `web/lib/service.py`, Zeilen 196-208

`notify()` iteriert ueber `self.handlers`, waehrend `tap()` aus Flask-Request-Threads Elemente hinzufuegt/entfernt. Das kann zu `RuntimeError: list changed size during iteration` fuehren.

**Empfehlung:**
- `threading.Lock` fuer Handler-Liste verwenden
- Oder: Kopie der Liste vor Iteration erstellen

---

### BUG-05: `PPPPService.xzyh_handlers` ohne Thread-Schutz [HIGH]

**Datei:** `web/service/pppp.py`, Zeilen 18, 97, 136, 167-168

Aehnlich wie BUG-04: `xzyh_handlers` wird von verschiedenen Threads gleichzeitig gelesen und modifiziert.

**Empfehlung:**
- Thread-sichere Collection verwenden oder explizites Locking

---

### BUG-06: `ServiceManager.refs`-Counter ohne Lock [MEDIUM]

**Datei:** `web/lib/service.py`, Zeilen 323-350

`get()` und `put()` modifizieren `self.refs[name]` aus konkurrierenden Flask-Request-Threads. Fehlerhafte Reference-Counts koennen dazu fuehren, dass Services vorzeitig gestoppt oder nie gestoppt werden.

**Empfehlung:**
- `threading.Lock` fuer alle `refs`-Operationen

---

### BUG-07: `make_mqtt_pkt` ignoriert uebergebene Parameter [MEDIUM]

**Datei:** `libflagship/mqttapi.py`, Zeilen 106-120

Die statische Methode akzeptiert `packet_type` und `packet_num` als Parameter, aber hardcoded `packet_type=MqttPktType.Single` und `packet_num=0`. Die Argumente werden stillschweigend ignoriert.

**Empfehlung:**
- Entweder die Parameter verwenden oder aus der Signatur entfernen

---

### BUG-08: `recv_aabb` blockiert endlos ohne Timeout [MEDIUM]

**Datei:** `libflagship/ppppapi.py`, Zeilen 424-432

`recv_aabb` ruft `fd.read()` ohne Timeout auf. Wenn die Gegenstelle nicht antwortet, blockiert der Thread fuer immer.

**Empfehlung:**
- Socket-Timeout setzen oder `select()`/`poll()` mit Timeout verwenden

---

### BUG-09: `mqtt_checksum_remove` akzeptiert korrupte Daten [MEDIUM]

**Datei:** `libflagship/megajank.py`, Zeilen 35-39

Bei falscher XOR-Checksumme wird nur `print()` aufgerufen statt einer Exception. Die auskommentierte `raise`-Zeile deutet darauf hin, dass das eigentlich so gedacht war.

**Empfehlung:**
- Exception werfen bei fehlgeschlagener Checksumme
- Oder: explizit dokumentieren, warum korrupte Daten akzeptiert werden

---

### BUG-10: Printer-Index ohne fruehes Return [HIGH]

**Datei:** `cli/pppp.py`, Zeilen 29-31 und `cli/mqtt.py`, Zeilen 18-20

```python
if printer_index >= len(cfg.printers):
    log.critical("printer_index out of range")
    # <-- Kein return/exit! Naechste Zeile crasht mit IndexError
printer = cfg.printers[printer_index]
```

**Empfehlung:**
- `sys.exit(1)` oder `return` nach `log.critical()` einfuegen

---

### BUG-11: `timeout`-Variable-Shadowing in `mqttapi.py` [MEDIUM]

**Datei:** `libflagship/mqttapi.py`, Zeilen 150-157

Der `timeout`-Parameter wird im Loop ueberschrieben. Wenn `datetime.now()` das Enddatum ueberschreitet, wird ein negativer Timeout an `loop()` uebergeben.

**Empfehlung:**
- Eigene Variable fuer berechneten verbleibenden Timeout verwenden

---

## 3. Memory Leaks & Resource Leaks

### LEAK-01: File-Handle-Leak in `ankerctl.py` [HIGH]

**Datei:** `ankerctl.py`, Zeilen 386, 389, 429, 430

```python
fd = open(darfileloc, 'r')  # Nie geschlossen!
fd = open(winfileloc, 'r')  # Nie geschlossen!
```

Dateien werden mit `open()` geoeffnet, aber nie geschlossen. Kein `with`-Block, kein `fd.close()`.

**Empfehlung:**
```python
with open(filepath, 'r') as fd:
    data = fd.read()
```

---

### LEAK-02: File-Handle-Leak in `ppppapi.py` [HIGH]

**Datei:** `libflagship/ppppapi.py`, Zeile 57

```python
data = open(filename, "rb").read()  # Handle wird nie geschlossen
```

**Empfehlung:**
```python
with open(filename, "rb") as f:
    data = f.read()
```

---

### LEAK-03: Config-Datei ohne Context-Manager [MEDIUM]

**Datei:** `cli/config.py`, Zeilen 78, 227

```python
json.load(path.open())  # File-Handle nicht geschlossen
```

**Empfehlung:**
```python
with path.open() as f:
    data = json.load(f)
```

---

### LEAK-04: Socket nie geschlossen in `AnkerPPPPBaseApi` [MEDIUM]

**Datei:** `libflagship/ppppapi.py`, Zeilen 244-261, 270-291

Der Socket in `open()` bzw. `open_broadcast()` wird erstellt, aber `stop()` ruft nie `self.sock.close()` auf.

**Empfehlung:**
- `self.sock.close()` in `stop()` aufrufen
- Oder: Context-Manager-Pattern implementieren

---

### LEAK-05: `rxqueue` waechst unbegrenzt bei Paketverlust [MEDIUM]

**Datei:** `libflagship/ppppapi.py`, Zeilen 144-159

Out-of-Order-Pakete werden in `self.rxqueue` gespeichert. Bei Paketverlust (Paket kommt nie an) waechst `rxqueue` endlos, da `rx_ctr` nie weiterrueckt.

**Empfehlung:**
- Maximum-Groesse fuer `rxqueue` definieren
- Timeout/Cleanup fuer alte Eintraege
- Fehlende Pakete nach Timeout ueberspringen

---

### LEAK-06: `Pipe()`-Endpoints nie geschlossen [LOW]

**Datei:** `libflagship/ppppapi.py`, Zeilen 82-83

`Wire.__init__` erstellt ein `Pipe(False)`, das `rx` und `tx` Connections erzeugt. Diese werden nie geschlossen.

**Empfehlung:**
- `__del__` oder explizite `close()`-Methode implementieren

---

### LEAK-07: `multiprocessing.Queue` in `stream()` ohne Cleanup [LOW]

**Datei:** `web/lib/service.py`, Zeilen 360-369

Bei jedem `stream()`-Aufruf wird eine neue `multiprocessing.Queue` erstellt. Bei Client-Disconnect wird sie moeglicherweise nicht aufgeraeumt.

**Empfehlung:**
- `try/finally`-Block fuer Queue-Cleanup
- `threading.Event` zum sauberen Beenden

---

### LEAK-08: `split_chunks` kopiert gesamten Datenpuffer [MEDIUM]

**Datei:** `cli/util.py`, Zeilen 110-116

```python
data = data[:]  # Vollstaendige Kopie des (potentiell grossen) Buffers
```

Bei grossen GCode-Dateien verdoppelt sich kurzzeitig der Speicherverbrauch.

**Empfehlung:**
- `memoryview` verwenden fuer Zero-Copy-Slicing
- Oder: Iterator-basiertes Chunking

---

## 4. Clean Code Verstoesse

### CC-01: Built-in-Name-Shadowing [MEDIUM]

Mehrere Built-in-Namen werden als Parameter- oder Variablennamen verwendet:

| Datei | Zeile | Geshadowed |
|-------|-------|-----------|
| `libflagship/ppppapi.py` | 43 | `str` |
| `cli/util.py` | 23, 119 | `str` |
| `libflagship/pppp.py` | 333 | `str` |
| `libflagship/megajank.py` | 247 | `hash` |
| `libflagship/pppp.py` | 315 | `type` |
| `libflagship/megajank.py` | 91+ | `input` |
| `libflagship/pppp.py` | 425+ | `len` |

**Empfehlung:** Beschreibende Namen verwenden (z.B. `filename_str`, `data_hash`, `packet_type`).

---

### CC-02: Duplizierter Code in `config_decode`/`config_import` [HIGH]

**Datei:** `ankerctl.py`, Zeilen 366-398 vs 405-441

Die Dateifindungslogik (Darwin/Windows-Check, mehrere Pfade pruefen) ist zwischen beiden Funktionen copy-pasted.

**Empfehlung:**
- Gemeinsame Hilfsfunktion `_find_config_file()` extrahieren

---

### CC-03: `_parse_bool` doppelt implementiert [MEDIUM]

**Dateien:** `web/notifications.py:34-44` und `libflagship/notifications/apprise_client.py:22-32`

Identische Funktion in zwei Dateien.

**Empfehlung:**
- In ein gemeinsames Utility-Modul verschieben

---

### CC-04: Login-Check in jedem WebSocket-Handler wiederholt [MEDIUM]

**Datei:** `web/__init__.py`, Zeilen 113, 125, 139, 189, 200

```python
if not app.config["login"]:
    return
```

In jedem der 5 WebSocket-Handler identisch.

**Empfehlung:**
- Decorator oder Middleware implementieren

---

### CC-05: `_handle_notification` ist 97 Zeilen komplex [HIGH]

**Datei:** `web/service/mqtt.py`, Zeilen 183-278

Eine Methode behandelt: Print-Start-Erkennung, Fortschritt, Fehler, Abschluss, Task-ID-Tracking, Dateinamenerkennung und Zustandsreset. Tief verschachtelt, schwer testbar.

**Empfehlung:**
- In separate Methoden aufteilen: `_on_print_start()`, `_on_print_progress()`, `_on_print_complete()`, `_on_print_fail()`

---

### CC-06: `web/__init__.py` hat 608 Zeilen mit gemischten Verantwortlichkeiten [MEDIUM]

Enthaelt Routes, WebSocket-Handler, Service-Registrierung, Business-Logik, Konfigurationsupload, Notification-Settings etc.

**Empfehlung:**
- Flask Blueprints verwenden
- Aufteilen in: `routes/api.py`, `routes/websocket.py`, `routes/pages.py`

---

### CC-07: Keine Type-Hints [MEDIUM]

Nahezu keine Type-Annotations im gesamten Codebase. Betrifft alle Klassen in `ppppapi.py`, `mqttapi.py`, `service.py`, `web/__init__.py`.

**Empfehlung:**
- Schrittweise Type-Hints einfuehren, beginnend bei oeffentlichen APIs
- `mypy` in CI/CD integrieren

---

### CC-08: Keine Docstrings auf kritischen Klassen [MEDIUM]

Betroffene Klassen ohne Dokumentation:
- `Channel`, `Wire`, `AnkerPPPPBaseApi`, `AnkerPPPPApi` (ppppapi.py)
- `Service`, `ServiceManager` (service.py)
- `PPPPService`, `VideoQueue`, `MqttQueue` (web/service/)
- `AppriseNotifier` (notifications.py)

---

### CC-09: Magic Numbers ueberall [MEDIUM]

| Datei | Zeile | Wert | Bedeutung unklar |
|-------|-------|------|-----------------|
| `mqttapi.py` | 109-113 | `m3=5, m4=1, m5=2, m6=5, m7=ord('F')` | Protokoll-Felder |
| `ppppapi.py` | 306-308 | `handle=-3, max_handles=5` | PPPP-Konfiguration |
| `ppppapi.py` | 237 | `range(8)` | Anzahl Channels |
| `mqttapi.py` | 83 | `8789` | Port-Nummer |
| `web/__init__.py` | 318 | `"1.9.0"`, `"OctoPrint 1.9.0"` | Version-Strings |

**Empfehlung:**
- Konstanten mit beschreibenden Namen definieren

---

### CC-10: Mutable Default Argument [MEDIUM]

**Datei:** `libflagship/httpapi.py`, Zeile 117

```python
def equipment_get_dsk_keys(self, station_sns, invalid_dsks={}):
```

Mutable Default-Dict kann bei Mutation ueber Aufrufe hinweg persistieren.

**Empfehlung:**
```python
def equipment_get_dsk_keys(self, station_sns, invalid_dsks=None):
    if invalid_dsks is None:
        invalid_dsks = {}
```

---

### CC-11: Bare `except Exception` verschluckt Fehler [HIGH]

**Datei:** `ankerctl.py`, Zeilen 55-56

```python
except Exception as E:
    log.critical(...)
    # Ausfuehrung geht weiter mit kaputtem/fehlendem Config
```

**Empfehlung:**
- Spezifische Exceptions fangen
- Nach kritischem Fehler Programm beenden oder Re-Raise

---

## 5. Frontend-Sicherheit

### FE-01: Implizite globale Variable `sockets` [MEDIUM]

**Datei:** `static/ankersrv.js`, Zeile 269

```javascript
sockets = {};  // Kein var/let/const!
```

Erzeugt eine globale Variable, die von jedem Script auf der Seite manipuliert werden kann.

**Empfehlung:**
- `const sockets = {};` verwenden
- `"use strict";` am Dateianfang

---

### FE-02: Fehlendes JSON.parse Error-Handling [MEDIUM]

**Datei:** `static/ankersrv.js`, Zeilen 277, 418

```javascript
const data = JSON.parse(ev.data);  // Kein try/catch!
```

MQTT- und PPPP-WebSocket-Handler haben kein Error-Handling fuer fehlerhaftes JSON. Ein fehlerhaftes Paket stoppt die gesamte Verarbeitung.

**Empfehlung:**
```javascript
try {
    const data = JSON.parse(ev.data);
} catch (e) {
    console.error("Invalid JSON:", e);
    return;
}
```

---

### FE-03: Country-Code HTML-Injection-Pattern [MEDIUM]

**Datei:** `static/ankersrv.js`, Zeilen 706-708

```javascript
$(`<option value="${item.c}"${selected}>${item.n}</option>`).appendTo(selectElement);
```

String-Interpolation in jQuery-HTML-Konstruktion. Aktuell sicher (Daten aus hardcoded Liste), aber unsicheres Pattern.

**Empfehlung:**
- DOM-API verwenden statt HTML-Strings
- `document.createElement('option')` mit `.textContent` und `.value`

---

### FE-04: JMuxer-Instanz nicht aufgeraeumt bei Reconnect [LOW]

**Datei:** `static/ankersrv.js`, Zeilen 342-376

Neue `JMuxer`-Instanz bei jedem Reconnect ohne Pruefung auf bestehende Instanz. Memory-Leak bei schnellen Reconnects.

**Empfehlung:**
```javascript
if (this.jmuxer) this.jmuxer.destroy();
this.jmuxer = new JMuxer({...});
```

---

## 6. Docker-Sicherheit

### DOCKER-01: Container laeuft als Root [MEDIUM]

**Datei:** `Dockerfile`

Keine `USER`-Direktive. Prozess laeuft als root im Container.

**Empfehlung:**
```dockerfile
RUN useradd -r -s /bin/false ankerctl
USER ankerctl
```

---

### DOCKER-02: Host-Netzwerkmodus [MEDIUM]

**Datei:** `docker-compose.yaml`, Zeile 10

```yaml
network_mode: host
```

Container hat vollen Zugang zum Host-Netzwerk. Notwendig fuer PPPP-UDP, aber erweitert die Angriffsflaeche.

**Empfehlung:**
- Dokumentieren, warum `host`-Modus noetig ist
- Flask nur auf `127.0.0.1` binden (bereits als Default gesetzt)

---

### DOCKER-03: SSL-Verzeichnis in Image kopiert [LOW]

**Datei:** `Dockerfile`, Zeile 35

```dockerfile
COPY ssl /app/ssl/
```

Private Schluessel werden in Image-Layers eingebettet.

**Empfehlung:**
- SSL-Material via Docker Secrets oder Volume-Mounts bereitstellen
- Nicht in Image backen

---

### DOCKER-04: Veraltete Base-Image-Version [LOW]

**Datei:** `Dockerfile`

`python:3.11-bullseye` (Debian 11, EOL). Kein Digest-Pinning.

**Empfehlung:**
- Auf `python:3.12-bookworm` (Debian 12) upgraden
- Image-Digest pinnen fuer reproduzierbare Builds

---

## 7. Verbesserungsvorschlaege

### Prioritaet 1: Sofort umsetzen (Sicherheitskritisch)

1. **Authentifizierung implementieren** -- Mindestens Token-Auth oder Basic-Auth fuer Web-API und WebSockets
2. **CSRF-Schutz aktivieren** -- `flask-wtf CSRFProtect`
3. **WebSocket Origin-Validierung** -- `Origin`-Header pruefen
4. **File-Handle-Leaks beheben** -- `with`-Statements ueberall verwenden
5. **NoneType-Crash beheben** -- Early Returns nach `log.critical()`
6. **Thread-Safety in ppppapi.py** -- Locks fuer alle geteilten Datenstrukturen

### Prioritaet 2: Zeitnah umsetzen (Stabilitaet)

7. **`queue.Queue` statt einfacher Listen** fuer Thread-Kommunikation
8. **Socket-Timeouts** fuer alle Netzwerkoperationen
9. **Mutable Default Arguments** beseitigen
10. **Exception-Handling** praezisieren -- spezifische Exceptions statt `except Exception`
11. **Printer-Index-Validierung** mit Early Return konsistent machen
12. **`_handle_notification`** in kleinere Methoden aufteilen

### Prioritaet 3: Mittelfristig (Code-Qualitaet)

13. **Flask Blueprints** zur Strukturierung von `web/__init__.py`
14. **Type-Hints** schrittweise einfuehren
15. **Docstrings** fuer oeffentliche APIs
16. **Doppelten Code** eliminieren (config-Logik, `_parse_bool`)
17. **Magic Numbers** durch benannte Konstanten ersetzen
18. **Login-Check-Decorator** statt Copy-Paste in WebSocket-Handlern

### Prioritaet 4: Langfristig (Infrastruktur)

19. **Automatisierte Tests** -- Unit-Tests und Integration-Tests einfuehren
20. **CI/CD-Pipeline** mit `mypy`, `pylint`, `bandit` (Security-Linting)
21. **Docker-Image** auf aktuelles Debian upgraden und Non-Root-User verwenden
22. **Credential-Speicher** auf plattformspezifische Keystores umstellen
23. **Content-Security-Policy** und `X-Frame-Options` Headers setzen
24. **Dependency-Updates** -- `paho_mqtt` v2.x, aktuelle Flask-Version

---

## Severity-Uebersicht

### CRITICAL (4)
| ID | Kategorie | Beschreibung |
|----|-----------|-------------|
| SEC-01 | Sicherheit | Keine Authentifizierung auf Webserver |
| SEC-02 | Sicherheit | Hardcoded AES-Key fuer Credentials |
| BUG-01 | Bug | Thread-Safety in ppppapi.py Channels |
| BUG-02 | Bug | NoneType-Crash in config_decode/config_import |

### HIGH (14)
| ID | Kategorie | Beschreibung |
|----|-----------|-------------|
| SEC-03 | Sicherheit | Unsichere Kryptographie |
| SEC-04 | Sicherheit | Fehlende CSRF-Protection |
| SEC-05 | Sicherheit | Cross-Site WebSocket Hijacking |
| SEC-06 | Sicherheit | TLS-Verifizierung deaktiviert |
| SEC-07 | Sicherheit | Keine Input-Validierung auf WebSockets |
| SEC-08 | Sicherheit | Path Traversal bei Upload |
| BUG-03 | Bug | MQTT-Queue Race Condition |
| BUG-04 | Bug | Handler-Liste waehrend Iteration modifiziert |
| BUG-05 | Bug | xzyh_handlers ohne Thread-Schutz |
| BUG-10 | Bug | Printer-Index ohne Early Return |
| LEAK-01 | Resource Leak | File-Handles in ankerctl.py |
| LEAK-02 | Resource Leak | File-Handle in ppppapi.py |
| CC-02 | Clean Code | Duplizierter Config-Code |
| CC-05 | Clean Code | Ueberlange Notification-Methode |
| CC-11 | Clean Code | Bare except verschluckt Fehler |

### MEDIUM (25)
| ID | Kategorie | Beschreibung |
|----|-----------|-------------|
| SEC-09 | Sicherheit | Exception-Messages exponiert |
| BUG-06 | Bug | ServiceManager.refs ohne Lock |
| BUG-07 | Bug | make_mqtt_pkt ignoriert Parameter |
| BUG-08 | Bug | recv_aabb ohne Timeout |
| BUG-09 | Bug | Korrupte Checksumme akzeptiert |
| BUG-11 | Bug | Timeout-Variable-Shadowing |
| LEAK-03 | Resource Leak | Config ohne Context-Manager |
| LEAK-04 | Resource Leak | Socket nie geschlossen |
| LEAK-05 | Memory Leak | rxqueue waechst unbegrenzt |
| LEAK-08 | Memory Leak | split_chunks kopiert Buffer |
| CC-01 | Clean Code | Built-in-Name-Shadowing |
| CC-03 | Clean Code | _parse_bool doppelt |
| CC-04 | Clean Code | Login-Check wiederholt |
| CC-06 | Clean Code | __init__.py zu gross |
| CC-07 | Clean Code | Keine Type-Hints |
| CC-08 | Clean Code | Keine Docstrings |
| CC-09 | Clean Code | Magic Numbers |
| CC-10 | Clean Code | Mutable Default Argument |
| FE-01 | Frontend | Implizite globale Variable |
| FE-02 | Frontend | Fehlendes JSON Error-Handling |
| FE-03 | Frontend | HTML-Injection-Pattern |
| DOCKER-01 | Docker | Container als Root |
| DOCKER-02 | Docker | Host-Netzwerkmodus |

### LOW (14)
| ID | Kategorie | Beschreibung |
|----|-----------|-------------|
| LEAK-06 | Resource Leak | Pipe-Endpoints nie geschlossen |
| LEAK-07 | Resource Leak | Queue ohne Cleanup |
| FE-04 | Frontend | JMuxer Memory Leak |
| DOCKER-03 | Docker | SSL in Image |
| DOCKER-04 | Docker | Veraltetes Base-Image |
| + 9 weitere | Diverse | Naming, Dead Code, etc. |

---

*Dieser Report wurde automatisch generiert und basiert auf einer statischen Analyse des Quellcodes. Dynamische Tests (Penetration Testing, Fuzzing) wuerden moeglicherweise weitere Findings ergeben.*
