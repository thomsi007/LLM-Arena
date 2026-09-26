# SearXNG – önálló kezelő

Saját, ingyenes metakereső (SearXNG) telepítése, indítása és beállítása **egy kattintással**, Windowson és Linuxon.
Az LLM Aréna webes keresője ezt használja, de a mappa **önállóan is működik**: ha csak egy helyi keresőre van
szükséged, elég ez a mappa (az LLM Aréna többi része nem kell hozzá).

## Indítás

| Rendszer | Indítás | Leállítás |
|---|---|---|
| Windows | dupla kattintás: `start.bat` | `stop.bat` |
| Linux / macOS | `./start.sh` | `./stop.sh` |

Az indító:

1. megkeresi a Pythont (3.9+). Windowson, ha nincs, felajánlja a telepítését (winget),
2. ha a SearXNG nincs telepítve, **telepíti** (Docker image letöltése, vagy natív módban a forrás + saját Python környezet),
3. elindítja a SearXNG-t (ha a port foglalt, a következő szabad portot választja),
4. megnyitja a **grafikus kezelőt** a böngészőben: <http://127.0.0.1:8899/>.

A kezelő ablaka (a parancssor) bezárható – a SearXNG tovább fut. Leállítás: `stop.bat` / `stop.sh`, vagy a kezelő
„■ Leállítás” gombja.

## Futtatási módok

- **Automatikus** (alapértelmezett): Docker, ha telepítve van és fut; ha nem, natív mód.
- **Docker**: a hivatalos `searxng/searxng` image, `llm-arena-searxng` nevű konténerben. Windowson a Docker Desktop
  kell (ha nem fut, a kezelő elindítja). Ez a legmegbízhatóbb mód.
- **Natív** (Docker nélkül): a SearXNG forrása a `native/` mappába kerül (git vagy zip), saját virtuális
  környezettel (`native/venv`). Első telepítéskor néhány perc. Linuxon kell hozzá a `python3-venv` csomag.

## Grafikus felület

- Állapot, indítás / újraindítás / leállítás, telepítés / frissítés
- Beállítások: mód, port, elérhetőség (csak ez a gép / helyi hálózat), példány neve, nyelv, biztonságos keresés,
  automatikus kiegészítés, időkorlátok, limiter, képproxy
- Keresőmotorok ki/be kapcsolása
- Próbakeresés
- Haladó: a teljes `settings.yml` kézi szerkesztése (ilyenkor a kezelő nem generálja újra)
- Napló, eltávolítás

Mentéskor a futó SearXNG automatikusan újraindul az új beállításokkal.

## Parancssor

```
python manager.py                 # grafikus kezelő
python manager.py start           # telepítés (ha kell) + indítás
python manager.py stop | restart | status | install | test
python manager.py url             # a futó példány címe
python manager.py start --mode native --port 8888 --json
```

## Fájlok

| Fájl / mappa | Tartalom |
|---|---|
| `manager.py` | a kezelő (csak Python standard könyvtár) |
| `config.json` | a kezelő beállításai |
| `data/settings.yml` | a SearXNG generált konfigurációja (Dockerben `/etc/searxng`) |
| `native/` | natív telepítés (forrás + venv) |
| `logs/` | naplók |

A `config.json`, `data/`, `native/` és `logs/` helyi adat, nincs verziókezelve.

## Kapcsolat az LLM Arénával

Az LLM Aréna **Beállítások → Webes keresés → SearXNG** gombjai ugyanezt a kezelőt hívják, így közös a telepítés, a
konténer és a beállítás. Az Aréna automatikusan megtalálja az itt elindított példányt („🔍 Keresés helyi SearXNG
után”), és átveszi a címét.

Más programból a keresés: `http://127.0.0.1:<port>/search?q=kérdés&format=json`.
