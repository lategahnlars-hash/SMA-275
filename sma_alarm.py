#!/usr/bin/env python3
"""
SMA-Alarm: meldet dich, wenn der Schlusskurs den gleitenden Durchschnitt
(Standard: 250 Handelstage) über- oder unterschreitet.

Annahme der Strategie (bitte an deine Regeln anpassen):
  Kurs ÜBER dem SMA  -> gehebelter ETF (Amundi MSCI World 2x Leveraged)
  Kurs UNTER dem SMA -> ungehebelter ETF (Scalable MSCI AC World Xtrackers, LU2903252349)

Das Signal wird auf dem UNGEHEBELTEN ETF berechnet (SIGNAL_TICKER).

Installation:   pip install yfinance pandas
Test:           python sma_alarm.py --test      (schickt eine Testnachricht)
Status:         python sma_alarm.py --status    (zeigt Kurs/SMA, ohne zu melden)
Wochenbericht:  python sma_alarm.py --wochenbericht   (schickt Kurs im Vergleich zum SMA)
Normaler Lauf:  python sma_alarm.py             (einmal pro Handelstag nach Börsenschluss)

Zusätzlich gibt es den 2-%-Alarm (BESTAETIGUNG_PROZENT): Er kommt erst, wenn der Kurs den SMA
gekreuzt hat UND mindestens 2 % auf der neuen Seite liegt. Kehrt der Kurs vorher um, kommt
kein 2-%-Alarm. Der normale Alarm beim bloßen Kreuzen bleibt davon unabhängig.

Benachrichtigung (mindestens eine einrichten, per Umgebungsvariable):
  NTFY_TOPIC               -> Push aufs Handy über die kostenlose App "ntfy"
                              (Topic frei wählen, schwer erratbar, z. B. "sma275-x7k2p9")
  MAIL_TO, MAIL_FROM, SMTP_HOST, SMTP_PORT (optional, Standard 587),
  SMTP_USER, SMTP_PASSWORD -> E-Mail
"""
import json
import os
import smtplib
import sys
import urllib.request
from email.message import EmailMessage
from pathlib import Path

# ---------------- Einstellungen ----------------
SIGNAL_TICKER = "SCWX.DE"         # Scalable MSCI AC World Xtrackers (Xetra, EUR), Yahoo-Symbol bitte prüfen
NAME_UNGEHEBELT = "Scalable MSCI AC World Xtrackers (SCWX, ungehebelt)"
NAME_GEHEBELT = "Amundi MSCI World 2x Leveraged (LVWC)"
SMA_TAGE = 250
BAND_PROZENT = 0.0                # z. B. 1.0 = Wechsel erst bei 1 % Abstand zum SMA (weniger Fehlsignale)
BESTAETIGUNG_PROZENT = 2.0        # 2-%-Alarm: Kurs muss nach dem Kreuzen mind. 2 % über/unter dem SMA liegen (0 = aus)
STATE_FILE = Path(__file__).with_name("sma_state.json")
# -----------------------------------------------

UEBER, UNTER = "UEBER", "UNTER"


def hole_kurse(ticker: str):
    """Tägliche Schlusskurse der letzten ~3 Jahre."""
    import yfinance as yf  # erst hier importiert, damit die Logik ohne yfinance testbar ist

    close = yf.Ticker(ticker).history(period="3y", interval="1d")["Close"].dropna()
    if len(close) < SMA_TAGE + 5:
        raise RuntimeError(f"Zu wenige Kursdaten für {ticker}: {len(close)} Tage")
    return close


def bestimme_zustand(close, sma_tage=SMA_TAGE, band_prozent=BAND_PROZENT, vorher=None):
    """Gibt (zustand, letzter_kurs, sma) zurück.
    Liegt der Kurs innerhalb des Bands um den SMA, bleibt der bisherige Zustand bestehen."""
    sma = close.rolling(sma_tage).mean().iloc[-1]
    kurs = close.iloc[-1]
    band = band_prozent / 100.0
    if kurs > sma * (1 + band):
        zustand = UEBER
    elif kurs < sma * (1 - band):
        zustand = UNTER
    else:
        zustand = vorher
    if zustand is None:  # allererster Lauf innerhalb des Bands
        zustand = UEBER if kurs >= sma else UNTER
    return zustand, float(kurs), float(sma)


def lade_zustand():
    """Gibt (zustand, bestaetigter_zustand) zurück."""
    try:
        d = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None, None
    return d.get("zustand"), d.get("bestaetigt")


def speichere_zustand(zustand, bestaetigt):
    STATE_FILE.write_text(json.dumps({"zustand": zustand, "bestaetigt": bestaetigt}))


def sende_ntfy(titel, text):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=text.encode("utf-8"),
        headers={"Title": titel.encode("utf-8"), "Priority": "high"},
    )
    urllib.request.urlopen(req, timeout=15).read()
    return True


def sende_mail(titel, text):
    ziel = os.environ.get("MAIL_TO")
    host = os.environ.get("SMTP_HOST")
    if not (ziel and host):
        return False
    msg = EmailMessage()
    msg["Subject"], msg["To"] = titel, ziel
    msg["From"] = os.environ.get("MAIL_FROM", os.environ.get("SMTP_USER", ziel))
    msg.set_content(text)
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=20) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.send_message(msg)
    return True


def benachrichtige(titel, text):
    print(f"{titel}\n{text}")
    gesendet = False
    for sender in (sende_ntfy, sende_mail):
        try:
            gesendet = sender(titel, text) or gesendet
        except Exception as e:  # ein Kanal darf den anderen nicht blockieren
            print(f"Warnung: {sender.__name__} fehlgeschlagen: {e}", file=sys.stderr)
    if not gesendet:
        print("Hinweis: Kein Benachrichtigungskanal eingerichtet (NTFY_TOPIC oder MAIL_*).", file=sys.stderr)


def nachricht(zustand, kurs, sma):
    abstand = (kurs / sma - 1) * 100
    if zustand == UEBER:
        titel = f"SMA{SMA_TAGE}: Kurs ÜBER dem Durchschnitt"
        ziel = NAME_GEHEBELT
    else:
        titel = f"SMA{SMA_TAGE}: Kurs UNTER dem Durchschnitt"
        ziel = NAME_UNGEHEBELT
    text = (
        f"{SIGNAL_TICKER}: Schlusskurs {kurs:.2f}, SMA{SMA_TAGE} {sma:.2f} ({abstand:+.2f} %).\n"
        f"Laut Strategie: {ziel}."
    )
    return titel, text


def bestaetigung_nachricht(zustand, kurs, sma):
    abstand = (kurs / sma - 1) * 100
    if zustand == UEBER:
        titel = f"SMA{SMA_TAGE}: 2-%-Alarm, Kurs mehr als {BESTAETIGUNG_PROZENT:g} % ÜBER dem SMA"
        ziel = NAME_GEHEBELT
    else:
        titel = f"SMA{SMA_TAGE}: 2-%-Alarm, Kurs mehr als {BESTAETIGUNG_PROZENT:g} % UNTER dem SMA"
        ziel = NAME_UNGEHEBELT
    text = (
        f"{SIGNAL_TICKER}: Schlusskurs {kurs:.2f}, SMA{SMA_TAGE} {sma:.2f} ({abstand:+.2f} %).\n"
        f"Der SMA wurde gekreuzt und der Abstand von {BESTAETIGUNG_PROZENT:g} % ist erfüllt.\n"
        f"Laut Strategie mit Bestätigung: {ziel}."
    )
    return titel, text


def wochenbericht(close, zustand, bestaetigt, kurs, sma):
    abstand = (kurs / sma - 1) * 100
    datum = close.index[-1].strftime("%d.%m.%Y")
    seite = "ÜBER" if kurs >= sma else "UNTER"
    namen = {UEBER: NAME_GEHEBELT, UNTER: NAME_UNGEHEBELT}
    titel = f"SMA{SMA_TAGE} Wochenbericht: Kurs {seite} dem Durchschnitt"
    zeilen = [
        f"{SIGNAL_TICKER}, Schlusskurs vom {datum}: {kurs:.2f}",
        f"SMA{SMA_TAGE}: {sma:.2f}",
        f"Abstand: {abstand:+.2f} %",
        f"Laut Strategie (Kreuzen des SMA): {namen[zustand]}",
    ]
    if bestaetigt:
        zeilen.append(f"Mit {BESTAETIGUNG_PROZENT:g} %-Bestätigung: {namen[bestaetigt]}")
    return titel, "\n".join(zeilen)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--test":
        benachrichtige("SMA-Alarm Test", "Wenn du das liest, funktioniert die Benachrichtigung.")
        return

    close = hole_kurse(SIGNAL_TICKER)
    vorher, best_vorher = lade_zustand()
    zustand, kurs, sma = bestimme_zustand(close, vorher=vorher)
    abstand = (kurs / sma - 1) * 100

    # 2-%-Zustand: wechselt erst, wenn der Kurs 2 % auf der anderen Seite liegt.
    # Beim ersten Lauf (oder nach dem Update) startet er ohne Alarm im aktuellen Zustand.
    if BESTAETIGUNG_PROZENT > 0:
        bestaetigt, _, _ = bestimme_zustand(
            close, band_prozent=BESTAETIGUNG_PROZENT, vorher=best_vorher or zustand
        )
    else:
        bestaetigt = None

    if arg == "--wochenbericht":  # ändert den gespeicherten Zustand nicht
        benachrichtige(*wochenbericht(close, zustand, bestaetigt, kurs, sma))
        return

    if arg == "--status":
        print(f"{SIGNAL_TICKER}: Kurs {kurs:.2f}, SMA{SMA_TAGE} {sma:.2f}, "
              f"Abstand {abstand:+.2f} %, Zustand {zustand}, 2-%-Zustand {bestaetigt}")
        return

    meldung = False
    if zustand != vorher:
        titel, text = nachricht(zustand, kurs, sma)
        if vorher is None:
            titel = "Start: " + titel
        benachrichtige(titel, text)
        meldung = True

    if best_vorher is not None and bestaetigt is not None and bestaetigt != best_vorher:
        benachrichtige(*bestaetigung_nachricht(bestaetigt, kurs, sma))
        meldung = True

    if not meldung:
        print(f"Kein Wechsel ({zustand}, 2-%-Zustand {bestaetigt}): "
              f"Kurs {kurs:.2f}, SMA{SMA_TAGE} {sma:.2f} ({abstand:+.2f} %)")

    speichere_zustand(zustand, bestaetigt)


if __name__ == "__main__":
    main()
