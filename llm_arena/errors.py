"""Human readable error descriptions.

Every error that reaches the UI is a dict:
    {kind, label, message, hint, detail, status, stage}
``label``  – short title (what went wrong)
``message``– the concrete reason
``hint``   – what the user can do about it
``detail`` – technical details (server body, traceback) for copying
"""

from __future__ import annotations

import json
import traceback

HINTS = {
    "unreachable": "Ellenőrizd, hogy fut-e a llama-server, és jó-e az URL és a port a Beállításokban "
                   "(pl. http://127.0.0.1:8080). Tűzfal, VPN vagy rossz IP-cím is okozhatja.",
    "timeout": "A modell nem válaszolt időben. Növeld a Timeout értékét a Beállításokban (nagy, gondolkodó "
               "modelleknél 600–900 s), vagy csökkentsd a Max token / Kontextus-keret értékét.",
    "invalid_json": "A szerver nem OpenAI-kompatibilis választ adott. Ellenőrizd, hogy az URL a llama-serverre "
                    "mutat, és az kiszolgálja a /v1/chat/completions végpontot.",
    "empty_response": "A modell nem adott szöveges választ. Gondolkodó (reasoning) modellnél növeld a Max tokent, "
                      "vagy kapcsold be a „Gondolkodás kikapcsolása” opciót.",
    "model_error": "A modell/szerver hibát jelzett – gyakran a prompt túl hosszú a modell kontextusához. "
                   "Csökkentsd a Kontextus-keretet, vagy indítsd a llama-servert nagyobb -c értékkel.",
    "interrupted": "A kapcsolat válasz közben megszakadt – a szerver valószínűleg újraindult vagy összeomlott "
                   "(pl. elfogyott a memória). Nézd meg a llama-server konzolját, majd próbáld újra.",
    "cancelled": "A műveletet megszakítottad. A „Folytatás / újrapróbálás” gombbal folytatható.",
    "tools_unsupported": "A szerver nem támogatja a natív tool-hívást (indítsd --jinja kapcsolóval). "
                         "A program automatikusan szöveges módra vált.",
    "execution_disabled": "Kapcsold be a „Generált kód futtatásának engedélyezése” opciót a Beállításokban.",
    "no_code": "A modell nem adott felismerhető kódfájlt. Próbáld újra, vagy használj erősebb / kódra "
               "hangolt modellt.",
    "no_tests": "Egyik modell sem adott értelmezhető tesztfájlt. Próbáld újra a „Tesztek generálása” gombbal.",
    "no_evaluation": "Egyik modell sem adott értelmezhető értékelést (JSON). Próbáld újra, vagy kapcsold be "
                     "a „Hibás JSON esetén javítás kérése” opciót.",
    "analysis_failed": "Egyik modell sem adott elemzést. Ellenőrizd a modellek kapcsolatát a Beállításokban.",
    "no_proposal": "A fejlesztő modell nem adott javaslatot – nézd meg a hibaüzenetét a válaszkártyán.",
    "invalid_input": "Ellenőrizd a megadott adatokat, majd próbáld újra.",
    "conflict": "Várd meg, amíg a futó folyamat befejeződik, vagy szakítsd meg a fejléc ■ gombjával.",
    "not_found": "A kért elem nem létezik (lehet, hogy törölték vagy másik projekt van betöltve).",
    "permission": "A program nem tud írni/olvasni egy fájlt. Ellenőrizd a mappa jogosultságait, és hogy "
                  "víruskereső vagy más program nem zárolja-e.",
    "disk": "Fájlrendszer-hiba. Ellenőrizd a szabad helyet és az adatmappa elérési útját (--data-dir).",
    "attachment": "Ellenőrizd a fájl típusát és méretét. Támogatott: szöveges fájlok, kód, JSON/CSV, "
                  "DOCX, XLSX, PDF (pypdf csomaggal), képek.",
    "web": "A webes keresés nem sikerült. Ellenőrizd az internetkapcsolatot / proxyt, vagy válts keresőmotort "
           "(SearXNG, Brave) a Beállításokban.",
    "internal": "Váratlan programhiba. A „Részletek” alatti szöveget másold ki és küldd el a hibajelentéssel.",
}

HTTP_HINTS = {
    400: "A szerver hibásnak tartotta a kérést – leggyakrabban túl hosszú a prompt a modell kontextusához, vagy "
         "nem támogatott paraméter. Csökkentsd a Kontextus-keretet / Max tokent.",
    401: "Hibás vagy hiányzó API-kulcs – ellenőrizd a Beállításokban.",
    403: "A szerver megtagadta a hozzáférést – ellenőrizd az API-kulcsot és a jogosultságokat.",
    404: "Rossz endpoint vagy modellnév. Ellenőrizd az URL-t (ne legyen benne kétszer /v1) és a modell nevét "
         "(Felismerés gomb).",
    413: "Túl nagy kérés – csökkentsd a Kontextus-keretet vagy a csatolt fájlok méretét.",
    429: "A szerver túlterhelt / minden slot foglalt. Várj, vagy indítsd a llama-servert több párhuzamos "
         "slottal (--parallel).",
    500: "A szerver belső hibát adott – nézd meg a llama-server konzolját (gyakori ok: memória- vagy "
         "kontextustúllépés, nem támogatott funkció).",
    503: "A szerver még tölti a modellt vagy nem elérhető – várj egy kicsit, majd próbáld újra.",
}


def hint_for(kind: str, status: int | None = None) -> str:
    if kind == "http_error" and status:
        if status in HTTP_HINTS:
            return HTTP_HINTS[status]
        return HTTP_HINTS[500] if status >= 500 else HTTP_HINTS[400]
    return HINTS.get(kind, "")


def make(kind: str, label: str, message: str, *, detail: str | None = None, status: int | None = None,
         stage: str | None = None, hint: str | None = None) -> dict:
    return {"kind": kind, "label": label, "message": message, "hint": hint or hint_for(kind, status),
            "detail": (detail or "")[-6000:] or None, "status": status, "stage": stage}


def describe_exception(e: BaseException, *, stage: str | None = None) -> dict:
    """Turn any exception into the UI error dict."""
    err = getattr(e, "error", None)  # StepFailed carries a ready dict
    if isinstance(err, dict):
        out = dict(err)
        out.setdefault("hint", hint_for(out.get("kind", ""), out.get("status")))
        if not out.get("hint"):
            out["hint"] = hint_for(out.get("kind", ""), out.get("status"))
        out.setdefault("stage", getattr(e, "stage", None) or stage)
        return out
    to_dict = getattr(e, "to_dict", None)
    if callable(to_dict):
        try:
            out = to_dict()
            if isinstance(out, dict) and out.get("message"):
                out.setdefault("stage", stage)
                return out
        except Exception:  # noqa: BLE001
            pass
    name = type(e).__name__
    if isinstance(e, PermissionError):
        return make("permission", "Hozzáférés megtagadva", f"{e}", stage=stage)
    if isinstance(e, FileNotFoundError):
        return make("not_found", "A fájl nem található", f"{e}", stage=stage)
    if isinstance(e, OSError):
        return make("disk", "Fájl- vagy hálózati hiba", f"{e}", stage=stage)
    if isinstance(e, json.JSONDecodeError):
        return make("invalid_input", "Hibás JSON", f"A megadott adat nem érvényes JSON: {e}", stage=stage)
    if type(e) is ValueError or isinstance(e, UnicodeError):
        # Only explicit validation errors are "bad input"; TypeError/KeyError/... are program bugs.
        return make("invalid_input", "Hibás bemenet", str(e) or name, stage=stage)
    return make("internal", "Váratlan programhiba", f"{name}: {e}",
                detail="".join(traceback.format_exception(type(e), e, e.__traceback__)), stage=stage)
