# LLM Aréna

Helyben futó alkalmazás, amelyben **két külön Llama Server** (vagy bármilyen OpenAI-kompatibilis endpoint) modell
nemcsak versenyez, hanem **vitázik, kritizál, közösen tervez, kódot ír, tesztel és javít**.

```
Feladat → Két LLM elemzése → Közös döntés → Vita → Közös tervezés → Kód → Teszt → Javítás → Végleges megoldás
```

* **Nincs külső függőség** – csak Python 3.10+ standard könyvtár; a felület egyetlen böngészőoldal (vanilla JS, CDN nélkül).
* Offline is működik; alapértelmezésben csak `127.0.0.1`-en figyel.

## Indítás

```bash
# 1) két llama-server (példa)
llama-server -m modell-a.gguf --port 8080
llama-server -m modell-b.gguf --port 8081

# 2) az aréna
python3 -m llm_arena            # → http://127.0.0.1:8765
# vagy: ./run.sh  /  run.bat    (böngészőt is nyit)
```

Opciók: `--host`, `--port`, `--data-dir` (alapértelmezés: `./data`), `--open`.

### Kipróbálás valódi modell nélkül

A csomag tartalmaz egy mock llama-servert, amely minden munkafolyamatra szerkezetileg helyes választ ad
(a teszt → javítás ciklust is bemutatja: szándékosan hibás kódot ír, majd kijavítja):

```bash
python3 -m llm_arena.mock_server --port 8080 --name mock-a &
python3 -m llm_arena.mock_server --port 8081 --name mock-b &
python3 -m llm_arena
```

## Funkciók

| Fül | Mit csinál |
|---|---|
| **LLM beállítások** | LLM A és LLM B: URL, modell (üres/„auto” = automatikus felismerés `/v1/models` vagy `/props` alapján), API-kulcs, timeout, temperature, max token, rendszerprompt, streaming, újrapróbálás, kontextus-keret. Független, lépésenkénti kapcsolatteszt (`/health` → modellek → próba-válasz). |
| **Aréna** | Ugyanaz a feladat párhuzamosan mindkét modellnek; válaszidő, első token ideje, token-infó (valós vagy becsült), tok/s, hibák. Több körös (előzményekkel), körönkénti újrapróbálás, másolás, export. |
| **Vita** | A = érvelő/javaslattevő, B = kritikus/ellenérvelő. Menet: álláspont → kritika → válasz → válasz az új érvekre (N körben) → összegzés. Strukturált belső állapot (érvek, kifogások, elfogadott pontok, kérdések, javaslatok). Eredmény: **közös ténylista, vitatott pontok, megoldási lehetőségek, nyitott kérdések, közös következtetés** – a moderátor összegzését a másik modell ellenőrzi és kiegészíti. |
| **Programtervezés** | 11 lépés: követelmények → hiányzó követelmények → architektúra (opcionálisan mindkét modell javasol + közös döntés) → modulok → adatstruktúrák → algoritmusok → tervezési review → kód → kódellenőrzés → tesztek → hibajavítás → végleges verzió. Az egyik modell fejlesztő, a másik reviewer (logika, biztonság, teljesítmény, hibás API-használat, edge case-ek, hiányzó tesztek, karbantarthatóság). |
| **Kódnézet** | Fájlok, verziók, diffek, kézi szerkesztés (új verzióként), fájl/ZIP letöltés, módosítási napló. |
| **Tesztelés** | Fejlesztő: unit + integrációs; reviewer: edge-case, hibakezelési, teljesítménytesztek. Bukásnál mindkét modell elemzi a hibát, a fejlesztő javít (kódot, vagy ha a teszt hibás, a tesztet), újrafuttatás – max. iterációig. Kimutatás: sikeres/sikertelen tesztek, javított hibák, fennmaradó problémák, végleges kód. |
| **Közös döntés** | Nem győztes–vesztes: mindkét modell mindkét *anonimizált* megoldást pontozza (helyesség, teljesség, megvalósíthatóság, biztonság, teljesítmény, tesztelhetőség), szempontonkénti átlagok, erősségek/gyengeségek, átvett legjobb ötletek, majd **közösen összeállított megoldás**, amit a másik modell ellenőriz. |
| **Teljes folyamat** | A fenti lépések automatikusan egymás után, folytatható állapottal, végleges jelentéssel. |
| **Fájlcsatolás** | Az Aréna, Vita, Programtervezés és Teljes folyamat bemenetéhez fájlok csatolhatók (gomb, húzd-és-ejtsd, vagy beillesztés). Szöveg / forráskód / JSON / CSV / HTML / Markdown, DOCX, XLSX, PPTX, ODT/ODS, PDF (opcionális `pypdf` csomaggal), képek (multimodális modellnek `image_url`-ként). A Kódnézetben és a Tesztelésben saját forrás- és tesztfájlok tölthetők fel. A csatolmányok a projekttel együtt mentődnek/exportálódnak; a Projekt fülön kezelhetők. |
| **Webes eszközök** | A modellek szükség esetén élő információt kereshetnek a weben (`web_search`) és elolvashatnak oldalakat (`fetch_url`) – maguk döntik el, mikor kell. Aktív: Aréna, Vita, elemzés, tervezési lépések. Keresőmotor: DuckDuckGo (kulcs nélkül), saját SearXNG vagy Brave Search API. Minden keresés és forrás látszik a válaszkártyán és az exportban. |
| **HTML export** | Minden fontos fülön „⤓ HTML export” gomb (Aréna, Vita, Programtervezés, Kód, Tesztelés, Közös döntés, Teljes folyamat), a Projekt fülön teljes riport. Önálló, modern, jól olvasható HTML fájl: beágyazott stílus, világos/sötét téma automatikusan, nyomtatható PDF-be; a modellválaszok biztonságosan escape-elve. |
| **Napló** | Minden hívás, hiba, újrapróbálás, teszteredmény; szűrés, letöltés. |
| **Projekt** | Mentés, betöltés, új projekt, export/import (`.arena.json`, API-kulcsok opcionálisan), beszélgetés exportja (Markdown/JSON), kód ZIP. Automatikus mentés 10 mp-enként és minden folyamat végén. |

Mindenhol: **streaming**, folyamatjelző, **megszakítás** (a futó HTTP kapcsolat azonnal lezárul),
**folytatás/újrapróbálás** az utolsó sikeres lépéstől, válasz másolása.

## Kódfuttatás – biztonság

A generált kód tesztelése alapértelmezésben **ki van kapcsolva** (Beállítások → „Generált kód futtatásának engedélyezése”).
Bekapcsolva a kód ideiglenes mappában, külön folyamatban fut: tisztított környezeti változók, időkorlát,
POSIX-on CPU-/memória-/fájlméret-limit, a fájlnevek útvonal-ellenőrzésen mennek át. **Ez folyamat-izoláció, nem sandbox** –
a kód a te felhasználóddal fut. A tesztek `unittest`-alapúak (pytest-stílusú `def test_…()` függvények is futnak),
így nincs szükség külső csomagra.

## Architektúra

```
llm_arena/
  providers/          kommunikációs réteg (UI-tól független)
    base.py           LLMConfig, ChatResult, hibaosztályok, CancelToken, LLMProvider interfész
    openai_compat.py  llama-server / OpenAI-kompatibilis kliens (stream + nem stream)
    __init__.py       provider-regiszter: register_provider("név", Osztály)
  workflows/          arena, debate, consensus, design, testing, pipeline (+ common: WorkflowContext)
  prompts.py          minden prompt egy helyen
  project.py          projektállapot, verziózás, mentés/export/import
  jobs.py             háttérfeladatok + eseménynapló (SSE-n streamelve)
  sandbox.py          tesztfuttató
  app.py              szolgáltatási réteg      server.py  HTTP/JSON/SSE API
  static/             felület (index.html, css, js/views/*)
  mock_server.py      mock llama-server demóhoz és tesztekhez
```

**Új endpoint-típus** hozzáadása: `LLMProvider` leszármazott (`health`, `list_models`, `chat`), majd
`register_provider("sajat", SajatProvider)`, és a konfigurációban `provider: "sajat"`.

Minden modellválasz strukturált objektum, pl.:

```json
{"id": "…", "slot": "A", "model": "llama-3.1-8b", "role": "critic", "workflow": "debate",
 "stage": "debate_critique", "round": 1, "content": "…", "reasoning": "",
 "latency": 3.41, "ttft": 0.22, "tokens": 412, "prompt_tokens": 950, "tokens_estimated": false,
 "tokens_per_second": 38.5, "status": "done", "error": null, "structured": {"claims": ["…"]}}
```

### Hibakezelés

Endpoint elérhetetlen, timeout (teljes kérésre is), hibás JSON, HTTP hibák (5xx/429 újrapróbálva, 4xx nem),
üres válasz (pl. csak „gondolkodó” kimenet), modellhiba (`error` mező a válaszban vagy a streamben), megszakadt stream
(`[DONE]` nélkül) – mind külön hibatípus, magyar üzenettel. Egy hiba **csak az érintett munkafolyamatot** állítja le:
az Arénában a másik modell válasza megmarad, a vita/tervezés/pipeline állapota mentődik és folytatható. Hibás
JSON-válasz esetén a rendszer egyszer javítást kér a modelltől.

### Hibaüzenetek

Minden hiba egységes, érthető formában jelenik meg: **mi történt** (cím), **miért** (konkrét ok),
**mit tehetsz** (💡 tipp – pl. „ellenőrizd, fut-e a llama-server a megadott porton”, „növeld a timeoutot”,
„a modell nem multimodális”), valamint lenyitható **Részletek** (szerverválasz, HTTP-státusz, traceback) és
**Másolás** gomb a hibajelentéshez. A felugró hibák addig maradnak, amíg be nem zárod; a munkafolyamatok
hibái a fülön is megmaradnak, a „Folytatás / újrapróbálás” gombbal az elakadt lépéstől folytathatók.

### Webes keresés és böngészés

**Keresés – hibatűrő lánc:** a beállított keresőmotor → DuckDuckGo → DuckDuckGo lite → valódi böngésző
(DuckDuckGo) → SearXNG / Brave (ha be van állítva) → Wikipedia. Átmeneti hibánál (időtúllépés, 5xx) újrapróbál,
robotellenőrző / captcha oldalt felismer és a következő szolgáltatóra lép, hosztonként ritkítja a kéréseket, és
15 percig gyorsítótárazza a kereséseket (az oldalakat 30 percig).

**Találatszűrés:** URL-normalizálás (követőparaméterek – `utm_*`, `fbclid`, `gclid` … – törlése), duplikátumok és
hirdetések kiszűrése, relevancia szerinti rangsor (kulcsszó-egyezés, megbízható források enyhe előnye, alacsony
értékű oldalak hátránya), domainenként legfeljebb 2 találat, a nem releváns találatok elhagyása, tisztított kivonatok.

**Oldalolvasás:** először gyors HTTP-letöltés; ha az oldal JavaScriptes, kevés szöveget ad vagy robotfalat mutat,
automatikusan a **Playwright + playwright-stealth** böngészővel olvassa újra (vagy „Mindig” módban eleve azzal).
A fő tartalmat (`<main>`/`<article>`) veszi ki, eldobja a menüt, sütibannert, megosztó gombokat, ismétlődő sorokat,
PDF-ből is kinyeri a szöveget (pypdf-fel), és a modell `focus` kérdése szerint a legrelevánsabb bekezdéseket adja át.

**Hogy a hiba ne vigye félre a modellt:**
* a modell hibánál csak **egy rövid sort** kap („átmeneti hiba, próbálj más kulcsszót egyszer, vagy válaszolj a saját
  tudásodból”); a részletek a felületen, az eszközhívás kártyáján és a Naplóban látszanak;
* az **ismételt, azonos hívást** memóriából szolgálja ki (nem keres újra);
* **két egymás utáni sikertelen kör után** leállítja az eszközhasználatot, és a modell a saját tudásából válaszol,
  jelezve, hogy élő adat nem volt elérhető;
* a válaszonkénti **webes tartalom teljes mennyisége korlátozott**, a korábbi eszközkimenetek rövidülnek;
* a webes tartalomból eltávolítja a chat-sablon tokeneket és a tipikus prompt-injekciós mondatokat, és külső,
  nem megbízható forrásként jelöli.

**Helyi SearXNG egy kattintással (ingyenes, nyílt forráskódú metakereső):**
Beállítások → Webes eszközök → „▶ SearXNG indítása”. Az LLM Aréna:
1. ellenőrzi a Dockert (Windows/macOS: az ingyenes [Docker Desktop](https://www.docker.com/products/docker-desktop/) kell, fusson),
2. létrehozza a `data/searxng/settings.yml`-t (JSON-kimenet bekapcsolva, limiter kikapcsolva, véletlen titkos kulcs),
3. letölti és elindítja a hivatalos `searxng/searxng` konténert (`llm-arena-searxng`, csak `127.0.0.1:<port>`-on
   érhető el, újraindul a géppel együtt),
4. megvárja, amíg válaszol, majd **automatikusan beírja a címét a SearXNG URL mezőbe, és keresőmotornak választja**.

Ha már fut egy SearXNG (akár saját telepítés), a „🔍 Keresés helyi SearXNG után” gomb megtalálja a szokásos portokon
(8888, 8080, 8081 …), ellenőrzi a JSON-kimenetet, és szintén automatikusan átveszi. Opcionálisan az LLM Arénával
együtt is indulhat („Induljon az LLM Arénával együtt”). Haladóknak: Python mód (`searx` csomag, hivatalosan csak Linux).
Kézi indítás Dockerrel, ha szükséges:
```bash
docker run -d --name llm-arena-searxng -p 127.0.0.1:8888:8080 -v ./data/searxng:/etc/searxng \
  -e GRANIAN_HOST=0.0.0.0 searxng/searxng
```

**Böngésző telepítése (opcionális):**
```bash
pip install playwright playwright-stealth
python -m playwright install chromium     # vagy Beállítások → Böngésző: Microsoft Edge / Google Chrome
```
Windowson az Edge általában telepítve van, így a „Microsoft Edge” csatornával a `playwright install` el is hagyható.
A Beállításokban a „🧭 Böngésző tesztelése” gomb megmutatja, melyik böngésző indult, és aktív-e a stealth.

* **Natív tool-hívás** (OpenAI `tools`): a llama-servert `--jinja` kapcsolóval indítsd. Ha a szerver elutasítja,
  a program automatikusan a szöveges `<tool_call>{...}</tool_call>` protokollra vált.
* **Biztonság:** a `fetch_url` (és a böngésző navigációja is) alapból tiltja a belső hálózati címeket; csak http/https;
  méret- és időkorlát.

### Tippek helyi modellekhez

* Kis kontextusú modelleknél csökkentsd a „Kontextus-keret” értéket (a rendszer ehhez vágja a promptokat).
* Gondolkodó (reasoning) modelleknél (pl. Qwen3) növeld a max tokent, vagy kapcsold be a „Gondolkodás kikapcsolása”
  opciót (`chat_template_kwargs: {"enable_thinking": false}` – llama-server támogatja; ha a szerver elutasítja,
  a kérés automatikusan nélküle megy újra). A `<think>` / `reasoning_content` külön, lenyitható blokkban jelenik meg.
  Nagy modelleknél a timeoutot is érdemes megemelni (a timeout a teljes kérésre vonatkozik).
* A promptok angolok (a helyi modellek ezt követik legjobban), a válasz nyelve beállítható (alapértelmezés: magyar).

## Tesztek

```bash
python3 -m unittest discover -s tests -t .
```

98 teszt: provider-hibaágak (timeout, 5xx, 4xx, hibás JSON, üres válasz, megszakadt stream, modellhiba, megszakítás,
újrapróbálás), parserek, sandbox (időtúllépés, importhiba), munkafolyamatok (aréna hibaizoláció, vita folytatása hiba
után, teljes pipeline javító ciklussal), mentés/export/import, HTTP API + SSE.
