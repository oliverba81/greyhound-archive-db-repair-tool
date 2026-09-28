# Übernommen aus greyhound-software/support-suite, Commit 8deb0611f0064ba81de3580b31f17849d0f66334 (2026-09-28).
# greyhound_license.py — Laufzeitprüfung der Desktop-Lizenzen der GREYHOUND Support Suite.
#
# Quelle: greyhound-software/support-suite, desktop-license-client/python/greyhound_license.py
# Die Python-Fassung von GreyhoundLicense.cs, mit denselben Regeln und gegen dieselben
# Testvektoren geprüft. Sie wird UNVERÄNDERT in die App kopiert. Nicht nachbauen,
# nicht „vereinfachen“, die Produktionsschlüssel nicht anfassen.
#
# Voraussetzung: Python 3.10+, Paket `cryptography` (für die ECDSA-Prüfung; PyInstaller
# bündelt es über seinen eigenen Hook). Sonst nur die Standardbibliothek.
#
# Aufruf in der App, als allererste Handlung beim Start (vor jedem Fenster):
#
#     import greyhound_license
#     result = greyhound_license.check("<app-id>")
#     if not result.is_valid:
#         ...  # result.message + result.lid zeigen, sys.exit(1)
#     result.on_ended(handler)  # Handler läuft in einem eigenen Thread, nie im UI-Thread
#
# Regeln (Kurzfassung, ausführlich in CLAUDE.md der Support Suite):
# - Offline, immer: Start nur, wenn iat − 24 h ≤ jetzt < exp, jetzt ≥ HS − 10 min,
#   max(jetzt, V) < exp und keine gespeicherte Absage vorliegt.
#   HS = höchste erreichte Zeit (nur Uhrprüfung), V = vorausgebuchte Laufzeit (nur Ablauf).
# - Online, wenn erreichbar: eine signierte Antwort mit der Nonce dieses Starts entscheidet
#   nach der Serverzeit und überstimmt die lokale Uhr.
# - Alle Zeiten sind UTC in Unix-Sekunden.

import atexit
import base64
import datetime
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

__all__ = ["check", "LicenseResult", "LICENSE_FILE_NAME"]

LICENSE_FILE_NAME = "greyhound-license.json"

# Produktionsschlüssel: kid, X, Y (base64, je 32 Byte). Dieselbe Tabelle wie in
# GreyhoundLicense.cs; ein Test hält beide gleich. „aktuell“ ist der Schlüssel in der
# .env des Servers, „nächster“ der im Tresor. Einträge, die mit EINTRAGEN beginnen,
# sind Platzhalter und werden ignoriert — mit ihnen startet kein Build (fail closed).
PRODUCTION_KEY_TABLE = (
    # aktuell — Fingerabdruck 42e18e8f76b5a80e (sha256 über SPKI, erste 16 Hex-Zeichen)
    ("prod-2026", "idvR+4zl3X2JATSmX3dI8eGjwMmVo+h6fnr1lhcT7b4=", "T3n46yTqJ+Jx+kWzWN49Pco1omewYCla0SDloIdm2WE="),
    # nächster — Fingerabdruck fce8a70c73cc430e; der private Teil liegt offline, nicht in der .env
    ("prod-2027", "q6B8FOH7r/Bjwoe7mHJOftbtZmAhi9cfbXLPG73/YCU=", "Kr76wB+lQg0cPpym0LxoEreGOeUw9mshZGNVTBDXXpo="),
)

CLOCK_TOLERANCE_BEFORE_IAT = 24 * 3600
CLOCK_TOLERANCE_BELOW_HS = 10 * 60
PREBOOK_SECONDS = 5 * 60
HTTP_TIMEOUT_SECONDS = 5
MAX_RESPONSE_BYTES = 8 * 1024

MSG_INVALID_FILE = "Lizenzdatei ungültig."
MSG_MISSING_FILE = "Lizenzdatei fehlt, bitte das ZIP vollständig entpacken."
MSG_MISSING_FILE_ZIP = (
    "Die App wurde direkt aus dem ZIP gestartet. Bitte das ZIP zuerst entpacken "
    "(Rechtsklick → Alle extrahieren) und die App aus dem entpackten Ordner starten."
)
MSG_REVOKED = "Die Freigabe wurde vom Support beendet."
MSG_WITHDRAWN = "Diese App wird nicht mehr bereitgestellt."
MSG_NOT_CONFIRMED = "Lizenz nicht bestätigt, bitte Support kontaktieren."

_KID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_LID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]*$")
_STORABLE_REASONS = ("expired", "revoked", "withdrawn")

# Nur für Tests: ersetzt die vertrauten Schlüssel ({kid: (x_bytes, y_bytes)}).
trusted_keys_override = None

_live_sessions = []
_live_lock = threading.Lock()


# ---------------------------------------------------------------------------- Ergebnis


class LicenseResult:
    """Ergebnis der Lizenzprüfung beim Start."""

    def __init__(self, is_valid, message, lid, expires_utc):
        self.is_valid = bool(is_valid)
        self.message = message or ""
        self.lid = lid
        # Ablauf als datetime (UTC) oder None, wenn die Lizenzdatei nicht lesbar war.
        self.expires_utc = expires_utc
        self._gate = threading.Lock()
        self._handlers = []
        self._ended_args = None
        # Nur für Tests: die Hintergrundprüfung und die Sitzung.
        self.background_check = None
        self.session = None

    @property
    def is_ended(self):
        """True, sobald die Lizenz während der Laufzeit geendet hat."""
        with self._gate:
            return self._ended_args is not None

    def on_ended(self, handler):
        """
        Die Lizenz endet während der Laufzeit (Ablauf oder signierte Absage).
        handler(message, lid) läuft in einem eigenen Thread, nie im UI-Thread. Haftend:
        ist das Ende schon eingetreten, wird ein neuer Handler sofort aufgerufen. Jeder
        Handler wird höchstens einmal aufgerufen.
        """
        if handler is None:
            return
        with self._gate:
            args = self._ended_args
            if args is None:
                self._handlers.append(handler)
                return
        self._dispatch(handler, args)

    def _raise_ended(self, message):
        with self._gate:
            if self._ended_args is not None:
                return False
            args = self._ended_args = (message or "", self.lid)
            handlers = list(self._handlers)
            self._handlers.clear()
        for h in handlers:
            self._dispatch(h, args)
        return True

    @staticmethod
    def _dispatch(handler, args):
        def run():
            try:
                handler(*args)
            except Exception:
                pass  # Ein fehlerhafter Handler darf die Lizenzprüfung nicht beenden.

        threading.Thread(target=run, name="GHL-Ended", daemon=True).start()


# ---------------------------------------------------------------------------- Einstieg


def check(app_id, now=None, elapsed=None, state_dir=None, http=None, license_dir=None):
    """
    Prüft die Lizenz. Apps rufen nur check(app_id) auf; alle weiteren Parameter sind
    ausschließlich für Tests da und keine Zeitquelle.

    now: () -> Unix-Sekunden (UTC); elapsed: () -> Sekunden seit Start (monoton);
    http: (url, body_bytes, timeout) -> (status, body_bytes).
    """
    production = now is None and elapsed is None
    if elapsed is None:
        t0 = time.monotonic()
        elapsed = lambda: time.monotonic() - t0  # noqa: E731
    if now is None:
        now = lambda: time.time()  # noqa: E731
    try:
        return _check_core(
            app_id,
            now,
            elapsed,
            state_dir or _default_state_dir(),
            http or _default_http,
            license_dir or _app_base_dir(),
            production,
        )
    except Exception:
        return LicenseResult(False, MSG_INVALID_FILE, None, None)


def _check_core(app_id, now, elapsed, state_dir, http, license_dir, production):
    license_path = os.path.join(license_dir, LICENSE_FILE_NAME)
    if not os.path.isfile(license_path):
        return LicenseResult(False, MSG_MISSING_FILE_ZIP if _looks_like_zip(license_dir) else MSG_MISSING_FILE, None, None)

    token = _read_license_file_token(license_path)
    payload = verify_token(token) if token is not None else None
    if payload is None or payload["tool"] != app_id:
        return LicenseResult(False, MSG_INVALID_FILE, payload["lid"] if payload else None, None)

    lid = payload["lid"]
    iat = payload["iat"]
    exp = payload["exp"]
    exp_utc = datetime.datetime.fromtimestamp(exp, tz=datetime.timezone.utc)
    state_path = os.path.join(state_dir, lid.lower() + ".json")
    lock_name = "GHL-" + lid.lower()

    state = _StateStore.read(state_path)
    stored_rejection = None
    if state.get("rejResp") is not None and state.get("rejSig") is not None:
        stored_rejection = verify_stored_rejection(state["rejResp"], state["rejSig"], lid)

    t = int(math.floor(now()))
    offline_failure = offline_failure_message(t, iat, exp, state, stored_rejection)

    if offline_failure is None:
        result = LicenseResult(True, "", lid, exp_utc)
        session = _LicenseSession(result, lid, exp, state_path, lock_name, elapsed)
        session.start_offline(t, state)
        result.session = session
        _keep(session)
        nonce = new_nonce()
        install_id = _install_id(state_dir)

        def background():
            body = _query_server(payload["iss"], token, lid, nonce, install_id, http)
            if body is not None:
                session.on_background_answer(body)

        worker = threading.Thread(target=background, name="GHL-Check", daemon=True)
        result.background_check = worker
        worker.start()
        if production:
            session.enable_production_hooks()
        return result

    # Offline nicht bestanden: blockierend online fragen. Der Server überstimmt die Uhr.
    answer = _query_server(payload["iss"], token, lid, new_nonce(), _install_id(state_dir), http)
    if answer is None:
        return LicenseResult(False, offline_failure, lid, exp_utc)
    if answer["valid"] is True:
        st = answer["serverTime"]
        if st >= exp:
            return LicenseResult(False, expired_message(exp), lid, exp_utc)
        if stored_rejection is not None and stored_rejection["serverTime"] >= st:
            return LicenseResult(False, rejection_message(stored_rejection.get("reason"), exp), lid, exp_utc)
        result = LicenseResult(True, "", lid, exp_utc)
        session = _LicenseSession(result, lid, exp, state_path, lock_name, elapsed)
        session.start_from_server(st)
        result.session = session
        _keep(session)
        if production:
            session.enable_production_hooks()
        return result

    if is_storable_reason(answer.get("reason")):
        _StateStore.save_rejection(state_path, lock_name, answer)
    return LicenseResult(False, rejection_message(answer.get("reason"), exp), lid, exp_utc)


def _keep(session):
    with _live_lock:
        _live_sessions.append(session)


def offline_failure_message(t, iat, exp, state, stored_rejection):
    """None = offline bestanden, sonst die Meldung für den Fall ohne Serverantwort."""
    if stored_rejection is not None:
        return rejection_message(stored_rejection.get("reason"), exp)
    if t < iat - CLOCK_TOLERANCE_BEFORE_IAT:
        return "Systemuhr prüfen: Das Datum liegt vor dem Download am " + _format_local(iat) + "."
    hs = state.get("hs")
    if hs is not None and t < hs - CLOCK_TOLERANCE_BELOW_HS:
        return "Systemuhr prüfen: Das Datum liegt vor dem letzten Start am " + _format_local(hs) + "."
    used = state.get("used")
    used = max(t, used) if used is not None else t
    if t >= exp or used >= exp:
        return expired_message(exp)
    return None


def expired_message(exp):
    return "Diese Kopie war bis " + _format_local(exp) + " freigegeben."


def rejection_message(reason, exp):
    if reason == "expired":
        return expired_message(exp)
    if reason == "revoked":
        return MSG_REVOKED
    if reason == "withdrawn":
        return MSG_WITHDRAWN
    return MSG_NOT_CONFIRMED


def is_storable_reason(reason):
    return reason in _STORABLE_REASONS


def _format_local(unix_seconds):
    return datetime.datetime.fromtimestamp(unix_seconds).strftime("%d.%m.%Y %H:%M")


def _looks_like_zip(directory):
    d = (directory or "").lower()
    return ".zip" in d or "\\temp\\temp" in d or "/temp/temp" in d


def _app_base_dir():
    # PyInstaller: sys.executable ist die exe selbst (auch bei --onefile, dort liegt nur
    # der entpackte Code unter _MEIPASS). Aus dem Quelltext: der Ordner des Startskripts.
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(sys.argv[0] or "."))


def _default_state_dir():
    local = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(local, "GREYHOUND", "License")


# ---------------------------------------------------------------------------- Lizenzdatei & Token


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_str(v):
    return isinstance(v, str)


def _parse_json_object(data):
    try:
        obj = json.loads(data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else data)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _read_license_file_token(path):
    try:
        if os.path.getsize(path) > 16 * 1024:
            return None
        with open(path, "rb") as f:
            obj = _parse_json_object(f.read())
        if obj is None or not _is_int(obj.get("v")) or obj.get("v") != 1:
            return None
        token = obj.get("token")
        return token if _is_str(token) and token else None
    except Exception:
        return None


def verify_token(token):
    """Prüft Signatur, dann Form. Liefert None bei jedem Fehler, sonst das Payload-dict."""
    if not _is_str(token) or len(token) > 4096:
        return None
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != "v1" or not _KID_RE.match(parts[1]):
        return None
    sig = b64url_decode(parts[3])
    payload_bytes = b64url_decode(parts[2])
    if sig is None or payload_bytes is None:
        return None
    key = trusted_keys().get(parts[1])
    if key is None:
        return None
    if not _ecdsa_verify(key, ("GHL-TOKEN-v1." + parts[1] + "." + parts[2]).encode("utf-8"), sig):
        return None

    p = _parse_json_object(payload_bytes)
    if p is None:
        return None
    if p.get("typ") not in ("customer", "staff"):
        return None
    lid = p.get("lid")
    if not _is_str(lid) or not _LID_RE.match(lid):
        return None
    tool = p.get("tool")
    if not _is_str(tool) or not tool or not _is_int(p.get("iat")) or not _is_int(p.get("exp")):
        return None
    if p["exp"] <= p["iat"]:
        return None
    if not _is_allowed_issuer(p.get("iss")):
        return None
    return p


def _is_allowed_issuer(iss):
    if not _is_str(iss) or not iss:
        return False
    try:
        u = urllib.parse.urlparse(iss)
    except Exception:
        return False
    if not u.netloc:
        return False
    if u.scheme == "https":
        return True
    return u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1")


def trusted_keys():
    if trusted_keys_override is not None:
        return trusted_keys_override
    keys = {}
    for row in PRODUCTION_KEY_TABLE:
        if len(row) != 3 or row[0].startswith("EINTRAGEN"):
            continue
        try:
            x = base64.b64decode(row[1], validate=True)
            y = base64.b64decode(row[2], validate=True)
        except Exception:
            continue
        if len(x) != 32 or len(y) != 32:
            continue
        keys[row[0]] = (x, y)
    return keys


# ---------------------------------------------------------------------------- Antworten des Servers


def verify_response_core(resp, sig, expected_lid):
    """
    Gemeinsamer Kern: Signatur über "GHL-CHECK-v1." + resp mit einem vertrauten
    Schlüssel, danach typ, v, kid (muss der prüfende sein) und lid.
    """
    if not _is_str(resp) or not _is_str(sig) or len(resp) > 4096:
        return None
    sig_bytes = b64url_decode(sig)
    resp_bytes = b64url_decode(resp)
    if sig_bytes is None or resp_bytes is None:
        return None
    data = ("GHL-CHECK-v1." + resp).encode("utf-8")
    matched_kid = None
    for kid, key in trusted_keys().items():
        if _ecdsa_verify(key, data, sig_bytes):
            matched_kid = kid
            break
    if matched_kid is None:
        return None
    body = _parse_json_object(resp_bytes)
    if body is None:
        return None
    if body.get("typ") != "ghl-check" or not _is_int(body.get("v")) or body.get("v") != 1 or body.get("kid") != matched_kid:
        return None
    lid = body.get("lid")
    if not _is_str(lid) or not _is_str(expected_lid) or lid.lower() != expected_lid.lower():
        return None
    if not isinstance(body.get("valid"), bool) or not _is_int(body.get("serverTime")) or not _is_int(body.get("exp")):
        return None
    reason = body.get("reason")
    if reason is not None and not _is_str(reason):
        return None
    if body["valid"] is False and not reason:
        return None
    nonce = body.get("nonce")
    if nonce is not None and not _is_str(nonce):
        return None
    body = dict(body)
    body["_raw_resp"] = resp
    body["_raw_sig"] = sig
    return body


def verify_online_response(resp, sig, expected_lid, expected_nonce):
    """Online-Antwort dieses Starts: zusätzlich muss die Nonce stimmen."""
    body = verify_response_core(resp, sig, expected_lid)
    if body is None or body.get("nonce") is None or body.get("nonce") != expected_nonce:
        return None
    return body


def verify_stored_rejection(resp, sig, expected_lid):
    """
    Gespeicherte Absage eines früheren Starts: ohne Nonce-Prüfung (sie stammt von
    damals), aber nur valid=false mit einem speicherbaren Grund.
    """
    body = verify_response_core(resp, sig, expected_lid)
    if body is None or body["valid"] is not False or not is_storable_reason(body.get("reason")):
        return None
    return body


def _query_server(url, token, lid, nonce, install_id, http):
    try:
        req = {"token": token, "nonce": nonce}
        if install_id is not None:
            req["installId"] = install_id
        status, data = http(url, json.dumps(req).encode("utf-8"), HTTP_TIMEOUT_SECONDS)
        if status != 200 or data is None or len(data) > MAX_RESPONSE_BYTES:
            return None
        signed = _parse_json_object(data)
        if signed is None:
            return None
        return verify_online_response(signed.get("resp"), signed.get("sig"), lid, nonce)
    except Exception:
        return None


def _default_http(url, body, timeout):
    # urllib nimmt unter Windows den Systemproxy aus der Registry (getproxies) und die
    # Zertifikate aus dem Windows-Zertifikatsspeicher (ssl.create_default_context).
    request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read(MAX_RESPONSE_BYTES + 1)


def new_nonce():
    return b64url_encode(secrets.token_bytes(32))


def _install_id(state_dir):
    try:
        path = os.path.join(state_dir, "install-id")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read().strip()
            try:
                return str(uuid.UUID(existing))
            except ValueError:
                pass
        os.makedirs(state_dir, exist_ok=True)
        new_id = str(uuid.uuid4())
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_id)
        return new_id
    except Exception:
        return None


# ---------------------------------------------------------------------------- Sitzung


class _LicenseSession:
    """Laufende Sitzung einer gültigen Lizenz: Buchführung von HS und V, Ablauf, Hintergrundantwort."""

    def __init__(self, result, lid, exp, state_path, lock_name, elapsed):
        self._gate = threading.RLock()
        self._result = result
        self._lid = lid
        self._exp = exp
        self._state_path = state_path
        self._lock_name = lock_name
        self._elapsed = elapsed
        # Aktuelle Zeit = Basis + Elapsed. Zwei Basen, weil HS und V verschiedene Fragen beantworten.
        self._base_v = 0.0
        self._base_t = 0.0
        self._last_booked_v = None
        self._last_write_elapsed = 0.0
        self._ended = False
        self._exited = False
        self._expired_end = False
        self._timer = None
        self._hooks = False

    def _e(self):
        return float(self._elapsed())

    @property
    def current_v(self):
        with self._gate:
            return self._base_v + self._e()

    @property
    def current_t(self):
        with self._gate:
            return self._base_t + self._e()

    @property
    def ended(self):
        with self._gate:
            return self._ended

    def start_offline(self, now, state):
        """Offline bestanden: base := max(now, V), baseT := max(now, HS), dann vorausbuchen."""
        with self._gate:
            e = self._e()
            used = state.get("used")
            hs = state.get("hs")
            self._base_v = (max(now, used) if used is not None else now) - e
            self._base_t = (max(now, hs) if hs is not None else now) - e
            self._book_locked(False)

    def start_from_server(self, server_time):
        """Blockierende Online-Antwort gültig: HS := V := serverTime, dann vorausbuchen."""
        with self._gate:
            self._replace_with_server_time_locked(server_time)

    def on_background_answer(self, body):
        if body.get("valid") is True:
            st = body["serverTime"]
            with self._gate:
                if self._ended or self._exited:
                    return
                self._replace_with_server_time_locked(st)
            if st >= self._exp:
                self._end(expired_message(self._exp), True)
            else:
                self._rearm()
            return
        if not is_storable_reason(body.get("reason")):
            return
        _StateStore.save_rejection(self._state_path, self._lock_name, body)
        self._end(rejection_message(body.get("reason"), self._exp), False)

    def _replace_with_server_time_locked(self, server_time):
        e = self._e()
        self._base_v = server_time - e
        self._base_t = server_time - e
        prebooked = server_time + PREBOOK_SECONDS
        lid = self._lid

        def change(s):
            s["hs"] = server_time
            s["used"] = prebooked
            if s.get("rejResp") is not None and s.get("rejSig") is not None:
                rej = verify_stored_rejection(s["rejResp"], s["rejSig"], lid)
                if rej is None or rej["serverTime"] < server_time:
                    s.pop("rejResp", None)
                    s.pop("rejSig", None)
            return s

        if _StateStore.update(self._state_path, self._lock_name, 2.0, change):
            self._last_booked_v = prebooked
        self._last_write_elapsed = e

    def _book_locked(self, final_expired):
        """HS := max(HS, baseT + E), V := max(V, base + E + 5 min) (bzw. mindestens exp)."""
        e = self._e()
        hs = int(math.floor(self._base_t + e))
        v = int(math.ceil(self._base_v + e + PREBOOK_SECONDS))
        if final_expired:
            v = max(v, self._exp)
        written = [None]

        def change(s):
            s["hs"] = max(s["hs"], hs) if s.get("hs") is not None else hs
            s["used"] = max(s["used"], v) if s.get("used") is not None else v
            written[0] = s["used"]
            return s

        if _StateStore.update(self._state_path, self._lock_name, 2.0, change):
            self._last_booked_v = written[0]
        self._last_write_elapsed = e

    def tick(self):
        """Periodischer Takt: Laufzeitgrenze prüfen, sonst alle 5 Minuten buchen."""
        expire = False
        with self._gate:
            if self._ended or self._exited:
                return
            e = self._e()
            if self._base_v + e >= self._exp:
                expire = True
            elif e - self._last_write_elapsed >= PREBOOK_SECONDS - 1:
                self._book_locked(False)
        if expire:
            self._end(expired_message(self._exp), True)
        else:
            self._rearm()

    def _end(self, message, expired):
        with self._gate:
            if self._ended:
                return
            self._ended = True
            self._expired_end = expired
            if expired:
                self._book_locked(True)
            self._cancel_timer_locked()
        self._result._raise_ended(message)

    def exit(self):
        """
        Sauberes Beenden: HS fortschreiben, die eigene Vorausbuchung zurückbuchen.
        Steht V noch auf dem zuletzt selbst gebuchten Wert, gilt V := base + E; sonst
        (eine andere Instanz hat weiter gebucht) V := max(V, base + E).
        """
        with self._gate:
            if self._exited:
                return
            self._exited = True
            self._cancel_timer_locked()
            e = self._e()
            hs = int(math.floor(self._base_t + e))
            used = int(math.ceil(self._base_v + e))
            if self._expired_end:
                used = max(used, self._exp)
            last_booked = self._last_booked_v

            def change(s):
                s["hs"] = max(s["hs"], hs) if s.get("hs") is not None else hs
                cur = s.get("used")
                own = cur is not None and last_booked is not None and cur == last_booked
                s["used"] = used if (own or cur is None) else max(cur, used)
                return s

            _StateStore.update(self._state_path, self._lock_name, 0.5, change)

    def enable_production_hooks(self):
        def on_exit():
            try:
                self.exit()
            except Exception:
                pass

        atexit.register(on_exit)
        with self._gate:
            if self._ended or self._exited:
                return
            self._hooks = True
        self._rearm()

    def _cancel_timer_locked(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _rearm(self):
        with self._gate:
            if not self._hooks or self._ended or self._exited:
                return
            e = self._e()
            until_book = PREBOOK_SECONDS - (e - self._last_write_elapsed)
            until_exp = self._exp - (self._base_v + e)
            due = min(until_book, until_exp)
            due = max(1.0, min(float(PREBOOK_SECONDS), due))
            self._cancel_timer_locked()

            def fire():
                try:
                    self.tick()
                except Exception:
                    pass

            self._timer = threading.Timer(due, fire)
            self._timer.daemon = True
            self._timer.start()


# ---------------------------------------------------------------------------- Zustand auf der Platte


class _StateStore:
    @staticmethod
    def read(path):
        try:
            if not os.path.isfile(path) or os.path.getsize(path) > 16 * 1024:
                return {}
            with open(path, "rb") as f:
                obj = _parse_json_object(f.read())
            if obj is None or not _is_int(obj.get("v")) or obj.get("v") != 1:
                return {}
            s = {}
            for k in ("hs", "used"):
                if _is_int(obj.get(k)):
                    s[k] = obj[k]
            if _is_str(obj.get("rejResp")) and _is_str(obj.get("rejSig")):
                s["rejResp"] = obj["rejResp"]
                s["rejSig"] = obj["rejSig"]
            return s
        except Exception:
            return {}

    @staticmethod
    def update(path, lock_name, wait_seconds, change):
        """Liest neu, wendet die Änderung an und schreibt atomar — unter der Sperre."""
        lock = _NamedLock(lock_name, os.path.dirname(path))
        try:
            if not lock.acquire(wait_seconds):
                return False
            return _StateStore._write(path, change(_StateStore.read(path)))
        except Exception:
            return False
        finally:
            lock.release()

    @staticmethod
    def save_rejection(path, lock_name, body):
        def change(s):
            s["rejResp"] = body["_raw_resp"]
            s["rejSig"] = body["_raw_sig"]
            return s

        _StateStore.update(path, lock_name, 2.0, change)

    @staticmethod
    def _write(path, state):
        tmp = None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            out = {"v": 1}
            for k in ("hs", "used", "rejResp", "rejSig"):
                if state.get(k) is not None:
                    out[k] = state[k]
            tmp = path + "." + uuid.uuid4().hex + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f)
            os.replace(tmp, path)
            tmp = None
            return True
        except Exception:
            return False
        finally:
            if tmp is not None:
                try:
                    os.remove(tmp)
                except Exception:
                    pass


class _NamedLock:
    """
    Prozessübergreifende Sperre je Lizenz. Unter Windows ein benannter Mutex
    (Local\\GHL-<lid>, derselbe Name wie in der C#-Fassung), sonst eine Sperrdatei.
    """

    def __init__(self, name, directory):
        self._name = name
        self._directory = directory
        self._handle = None
        self._kernel32 = None
        self._file = None
        self._owned = False

    def acquire(self, wait_seconds):
        if os.name == "nt":
            return self._acquire_windows(wait_seconds)
        return self._acquire_posix(wait_seconds)

    def _acquire_windows(self, wait_seconds):
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        # Ohne argtypes gäbe ctypes das Handle als 32-Bit-int weiter.
        kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32 = kernel32
        handle = kernel32.CreateMutexW(None, False, "Local\\" + self._name)
        if not handle:
            return False
        self._handle = handle
        rc = kernel32.WaitForSingleObject(handle, int(wait_seconds * 1000))
        # WAIT_OBJECT_0 = 0, WAIT_ABANDONED = 0x80 (der Vorbesitzer ist ohne Freigabe beendet).
        self._owned = rc in (0, 0x80)
        return self._owned

    def _acquire_posix(self, wait_seconds):
        import fcntl

        os.makedirs(self._directory, exist_ok=True)
        self._file = open(os.path.join(self._directory, self._name + ".lock"), "a+")
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._owned = True
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.01)

    def release(self):
        try:
            if os.name == "nt":
                if self._handle:
                    kernel32 = self._kernel32
                    if self._owned:
                        kernel32.ReleaseMutex(self._handle)
                    kernel32.CloseHandle(self._handle)
            elif self._file is not None:
                if self._owned:
                    import fcntl

                    fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
                self._file.close()
        except Exception:
            pass
        finally:
            self._handle = None
            self._file = None
            self._owned = False


# ---------------------------------------------------------------------------- Hilfen


def b64url_decode(s):
    if not _is_str(s) or not _B64URL_RE.match(s):
        return None
    rem = len(s) % 4
    if rem == 1:
        return None
    try:
        return base64.urlsafe_b64decode(s + "=" * ((4 - rem) % 4))
    except Exception:
        return None


def b64url_encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _ecdsa_verify(key, data, signature):
    """ECDSA P-256/SHA-256, Signatur als r‖s (64 Byte, IEEE P1363)."""
    if key is None or len(key) != 2 or signature is None or len(signature) != 64:
        return False
    try:
        public_key = ec.EllipticCurvePublicNumbers(
            int.from_bytes(key[0], "big"), int.from_bytes(key[1], "big"), ec.SECP256R1()
        ).public_key()
        r = int.from_bytes(signature[:32], "big")
        s = int.from_bytes(signature[32:], "big")
        public_key.verify(encode_dss_signature(r, s), data, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False
