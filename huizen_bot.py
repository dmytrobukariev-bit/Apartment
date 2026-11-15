"""Ищет студии и квартиры в аренду и присылает новые объявления в Telegram.

Источники: Kamernet, 123wonen, Pararius (если он не блокирует запросы). Настройки фильтра лежат в config.json,
токен бота и chat id берутся из переменных окружения
TELEGRAM_TOKEN и TELEGRAM_CHAT_ID.

Запуск:  python huizen_bot.py            (проверить и отправить новое)
         python huizen_bot.py --dry-run  (только показать, ничего не слать)
"""

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
SEEN_FILE = HERE / "seen.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
}

# Фразы, по которым видно, что регистрация по адресу невозможна.
NO_REGISTRATION = [
    "no registration", "registration not possible", "registration is not possible",
    "not possible to register", "cannot register", "can't register",
    "inschrijven niet mogelijk", "inschrijving niet mogelijk",
    "geen inschrijving", "niet inschrijven", "zonder inschrijving",
]
REGISTRATION_OK = [
    "registration possible", "registration is possible", "registration allowed",
    "inschrijven mogelijk", "inschrijving mogelijk", "inschrijven is mogelijk",
]


@dataclass
class Listing:
    source: str
    id: str
    url: str
    title: str
    price: int | None
    size: int | None = None
    rooms: int | None = None
    kind: str = ""          # apartment / studio / room
    city: str = ""
    available: str | None = None   # YYYY-MM-DD
    registration: bool | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"


def get(url: str) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    if "Just a moment" in r.text[:2000]:
        raise RuntimeError("заблокировано защитой от ботов")
    return r


def to_int(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text.split(",")[0])
    return int(digits) if digits else None


# ------------------------------------------------- Pararius / Huurwoningen
# У обоих сайтов одинаковая разметка (это одна компания).
def guess_kind(text: str) -> str:
    t = text.lower()
    if "studio" in t:
        return "studio"
    if re.search(r"appartement|apartment|flat|bovenwoning|benedenwoning|maisonnette|penthouse", t):
        return "apartment"
    if re.search(r"(^|[^\d\s-])\s*kamer(?!s)|\broom\b", t):
        return "room"
    if re.search(r"woning|huis|house", t):
        return "house"
    return "apartment"


def pararius_like(source: str, base: str, url_for_page) -> list[Listing]:
    out = []
    for city in CONFIG["cities"]:
        slug = city.lower().replace(" ", "-")
        ids = set()
        for page in range(1, 4):
            soup = BeautifulSoup(get(url_for_page(slug, page)).text, "html.parser")
            items = soup.select("section.listing-search-item")
            fresh = 0
            for it in items:
                link = it.select_one(".listing-search-item__link--title")
                if not link:
                    continue
                href = link["href"]
                lid = href.strip("/").split("/")[2]
                if lid in ids:
                    continue
                ids.add(lid)
                fresh += 1
                label = it.select_one(".listing-label")
                feats = {
                    li["class"][-1].split("--")[-1]: li.get_text(" ", strip=True)
                    for li in it.select(".illustrated-features li")
                }
                price_el = (it.select_one(".listing-search-item__price-main")
                            or it.select_one(".listing-search-item__price"))
                sub = it.select_one(".listing-search-item__sub-title")
                title = link.get_text(" ", strip=True)
                l = Listing(
                    source=source,
                    id=lid,
                    url=base + href,
                    title=title + (f", {sub.get_text(' ', strip=True)}" if sub else ""),
                    price=to_int(price_el.get_text() if price_el else None),
                    size=to_int(feats.get("surface-area")),
                    rooms=to_int(feats.get("number-of-rooms")),
                    kind=guess_kind(href.split("/")[1] + " " + title),
                    city=city,
                )
                if label:
                    l.notes.append(label.get_text(" ", strip=True))
                out.append(l)
            if not fresh or len(items) < 20:
                break
    return out


def pararius() -> list[Listing]:
    return pararius_like(
        "Pararius", "https://www.pararius.com",
        lambda slug, page: f"https://www.pararius.com/apartments/{slug}/0-{SEARCH_PRICE}"
                           + (f"/page-{page}" if page > 1 else ""))


def huurwoningen() -> list[Listing]:
    return pararius_like(
        "Huurwoningen", "https://www.huurwoningen.nl",
        lambda slug, page: f"https://www.huurwoningen.nl/in/{slug}/?price=0-{SEARCH_PRICE}"
                           + (f"&page={page}" if page > 1 else ""))


def pararius_details(l: Listing) -> None:
    """Подтягивает со страницы объявления дату заезда и упоминания регистрации."""
    soup = BeautifulSoup(get(l.url).text, "html.parser")
    text = soup.get_text(" ", strip=True).lower()
    l.registration = check_registration_text(text)
    m = re.search(r"(?:available|beschikbaar)\s+(?:from\s+|vanaf\s+|per\s+)?"
                  r"(\d{2})-(\d{2})-(\d{4})", text)
    if m:
        l.available = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    elif "available immediately" in text or "per direct" in text or "direct beschikbaar" in text:
        l.available = date.today().isoformat()


# ---------------------------------------------------------------- Kamernet
KAMERNET_TYPES = {1: "room", 2: "apartment", 4: "studio", 8: "anti-squat"}


def kamernet() -> list[Listing]:
    out = []
    for city in CONFIG["cities"]:
        slug = city.lower().replace(" ", "-")
        url = (f"https://kamernet.nl/en/for-rent/properties-{slug}"
               f"?maxRent={SEARCH_PRICE}&sort=1")
        html = get(url).text
        data = next_data(html)
        resp = data["props"]["pageProps"]["targetPageProps"]["findListingsResponse"]
        for x in resp.get("listings") or []:
            kind = KAMERNET_TYPES.get(x.get("listingType"), "other")
            out.append(Listing(
                source="Kamernet",
                id=str(x["listingId"]),
                url=(f"https://kamernet.nl/en/for-rent/{kind}-{x['citySlug']}/"
                     f"{x['streetSlug']}/{kind}-{x['listingId']}"),
                title=f"{kind.capitalize()} {x.get('street', '')}, {x.get('city', '')}",
                price=x.get("totalRentalPrice"),
                size=x.get("surfaceArea"),
                kind=kind,
                city=x.get("city", ""),
                available=(x.get("availabilityStartDate") or "")[:10] or None,
                notes=["коммуналка включена" if x.get("utilitiesIncluded")
                       else "коммуналка отдельно"],
            ))
    return out


def kamernet_details(l: Listing) -> None:
    data = next_data(get(l.url).text)
    raw = json.dumps(data)
    m = re.search(r'"isRegistrationAllowed":\s*(true|false|null)', raw)
    if m and m.group(1) != "null":
        l.registration = m.group(1) == "true"
    m = re.search(r'"deposit":\s*(\d+)', raw)
    if m:
        l.notes.append(f"депозит €{m.group(1)}")


# ---------------------------------------------------------------- 123wonen
WONEN_TYPES = {"appartement": "apartment", "bovenwoning": "apartment",
               "benedenwoning": "apartment", "maisonnette": "apartment",
               "studio": "studio", "kamer": "room"}


def wonen123() -> list[Listing]:
    out = []
    for city in CONFIG["cities"]:
        slug = city.lower().replace(" ", "-")
        soup = BeautifulSoup(get(f"https://www.123wonen.nl/huurwoningen/in/{slug}").text,
                             "html.parser")
        for card in soup.select("div.pandlist-container"):
            link = card.find("a", href=lambda h: h and "/huur/" in h)
            if not link:
                continue
            url = link["href"]
            text = " ".join(card.get_text(" ", strip=True).split())
            m = re.search(r"/huur/[^/]+/([^/]+)/(.+)$", url)
            kind = WONEN_TYPES.get(m.group(1), "house") if m else "other"
            price = re.search(r"€\s*([\d.]+)", text)
            size = re.search(r"Woonoppervlakte\s+(\d+)", text)
            beds = re.search(r"Slaapkamers\s+(\d+)", text)
            avail = re.search(r"Beschikbaarheid\s+(?:Per\s+)?(\d{2})-(\d{2})-(\d{4})", text)
            l = Listing(
                source="123wonen",
                id=m.group(2) if m else url,
                url=url,
                title=text.split(" €")[0][:80],
                price=to_int(price.group(1)) if price else None,
                size=int(size.group(1)) if size else None,
                rooms=int(beds.group(1)) + 1 if beds else None,  # спальни + гостиная
                kind=kind,
                city=city,
            )
            if avail:
                l.available = f"{avail.group(3)}-{avail.group(2)}-{avail.group(1)}"
            elif "Per direct" in text:
                l.available = date.today().isoformat()
            if "verhuurd" in card.get("class", []):
                l.notes.append("rented")
            out.append(l)
    return out


def text_details(l: Listing) -> None:
    """Общий разбор страницы объявления: ищем упоминания регистрации."""
    text = BeautifulSoup(get(l.url).text, "html.parser").get_text(" ", strip=True).lower()
    l.registration = check_registration_text(text)


wonen123_details = text_details

NL_MONTHS = {m: i for i, m in enumerate(
    ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus",
     "september", "oktober", "november", "december"], 1)}


def nl_date(text: str) -> str | None:
    m = re.search(r"(\d{1,2})\s+(" + "|".join(NL_MONTHS) + r")\s+(\d{4})", text.lower())
    if m:
        return f"{m.group(3)}-{NL_MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
    if "per direct" in text.lower() or "direct beschikbaar" in text.lower():
        return date.today().isoformat()
    return None


# ---------------------------------------------------------------- Marktplaats
def marktplaats() -> list[Listing]:
    out, ids = [], set()
    base = "https://www.marktplaats.nl/lrp/api/search?l1CategoryId=1032&limit=100&offset=0"
    for city in CONFIG["cities"]:
        # Все «дома и комнаты» по дате + отдельно раздел «huizen te huur»,
        # где агентства выкладывают студии и квартиры.
        found = []
        for extra in ("&sortBy=SORT_INDEX&sortOrder=DECREASING", "&l2CategoryId=2153"):
            found += get(f"{base}&query={city.lower()}{extra}").json().get("listings", [])
            time.sleep(1)
        for x in found:
            if x["itemId"] in ids:
                continue
            ids.add(x["itemId"])
            title = x.get("title", "")
            low = title.lower()
            price_info = x.get("priceInfo") or {}
            price = (price_info.get("priceCents") or 0) // 100 or None
            where = ((x.get("location") or {}).get("cityName") or "")
            if city.lower() not in (where + " " + low).lower():
                continue
            vip = x.get("vipUrl", "")
            # Только разделы «сдаётся дом/квартира/комната»: без гаражей, «ищу жильё» и т.п.
            if not re.search(r"/(huizen-te-huur|kamers-te-huur|appartementen-te-huur)/", vip):
                continue
            if "te koop" in low or "gevraagd" in low or (price and price > 5000):
                continue
            attrs = {a.get("key"): str(a.get("value", "")) for a in x.get("attributes", [])}
            size = re.search(r"(\d+)\s*m²", title) or re.search(r"^(\d+)$", attrs.get("livingArea", ""))
            out.append(Listing(
                source="Marktplaats",
                id=x["itemId"],
                url="https://www.marktplaats.nl" + x.get("vipUrl", ""),
                title=title,
                price=price,
                size=int(size.group(1)) if size else None,
                rooms=to_int(attrs.get("numberOfRooms")),
                kind=guess_kind(title),
                city=city,
            ))
    return out


# ---------------------------------------------------------------- Nederwoon
def nederwoon() -> list[Listing]:
    out = []
    for city in CONFIG["cities"]:
        soup = BeautifulSoup(get(
            f"https://www.nederwoon.nl/search?search_type=1&city={city}").text, "html.parser")
        for card in soup.select("div.location"):
            link = card.find("a", href=re.compile(r"/huurwoning/"))
            if not link:
                continue
            text = " ".join(card.get_text(" ", strip=True).split())
            if city.lower() not in text.lower():
                continue
            href = link["href"]
            price = re.search(r"€\s*([\d.,]+)", text)
            size = re.search(r"Woonoppervlakte\s+(\d+)", text)
            rooms = re.search(r"(\d+)\s+kamer", text)
            kind_word = re.search(r"\b(Studio|Appartement|Kamer|Woonhuis|Bovenwoning|"
                                  r"Benedenwoning|Maisonnette)\b", text)
            out.append(Listing(
                source="Nederwoon",
                id=href.strip("/").split("/")[2],
                url=("https://www.nederwoon.nl" + href) if href.startswith("/") else href,
                title=f"{link.get_text(' ', strip=True)}, {city}",
                price=to_int(price.group(1)) if price else None,
                size=int(size.group(1)) if size else None,
                rooms=int(rooms.group(1)) if rooms else None,
                kind=guess_kind(kind_word.group(1) if kind_word else href),
                city=city,
                available=nl_date(text),
            ))
    return out


# ---------------------------------------------------------------- Kamer.nl
def kamernl() -> list[Listing]:
    out, ids = [], set()
    for city in CONFIG["cities"]:
        slug = city.lower().replace(" ", "-")
        for kind_slug in ("kamer", "studio", "appartement"):
            soup = BeautifulSoup(get(f"https://www.kamer.nl/huren/{kind_slug}-{slug}/").text,
                                 "html.parser")
            for sc in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(sc.string or "")
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict) or data.get("@type") != "ItemList":
                    continue
                for x in data.get("itemListElement", []):
                    url = x.get("url", "")
                    m = re.search(r"/huren/([a-z]+)-" + re.escape(slug) + r"/([^/]+)/(\d+)/", url)
                    if not m or m.group(3) in ids:
                        continue
                    ids.add(m.group(3))
                    out.append(Listing(
                        source="Kamer.nl",
                        id=m.group(3),
                        url=url,
                        title=f"{m.group(1).capitalize()} {m.group(2).replace('-', ' ').title()}, {city}",
                        price=to_int(str(x.get("price", "")).split(".")[0]),
                        kind=guess_kind(m.group(1)),
                        city=city,
                        available=x.get("startTime"),
                    ))
    return out


def next_data(html: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("не нашёл данные на странице (сайт поменял разметку?)")
    return json.loads(m.group(1))


# ---------------------------------------------------------------- filters
def check_registration_text(text: str) -> bool | None:
    if any(p in text for p in NO_REGISTRATION):
        return False
    if any(p in text for p in REGISTRATION_OK):
        return True
    return None


EXTRA = {**CONFIG, **CONFIG.get("extra", {})}
SEARCH_PRICE = max(CONFIG["max_price"], EXTRA["max_price"])


def passes_basic(l: Listing) -> bool:
    """Подходит хотя бы под расширенные критерии."""
    return matches(l, CONFIG) or matches(l, EXTRA)


def matches(l: Listing, c: dict) -> bool:
    if l.city and l.city.lower() not in [x.lower() for x in c["cities"]]:
        return False
    if l.kind not in c["types"]:
        return False
    if l.price is None or l.price > c["max_price"] or l.price < c.get("min_price", 0):
        return False
    if l.size is not None and l.size < c["min_size_m2"]:
        return False
    if l.rooms is not None and l.rooms > c["max_rooms"]:
        return False
    if c.get("skip_under_option") and any(
            n.lower() in ("under option", "rented under option", "rented") for n in l.notes):
        return False
    return True


def passes_details(l: Listing) -> bool:
    return l.registration is not False


def warnings(l: Listing) -> list[str]:
    w = []
    if l.price is not None and l.price < 450:
        w.append("⚠️ подозрительно дёшево, будь осторожен с мошенниками")
    if l.available and l.available > CONFIG["needed_by"]:
        w.append(f"⚠️ заезд только с {l.available}, позже {CONFIG['needed_by']}")
    return w


# ---------------------------------------------------------------- telegram
def format_msg(l: Listing) -> str:
    kinds = {"apartment": "Квартира", "studio": "Студия", "room": "Комната", "house": "Дом"}
    head = ("⭐ <b>ПОДХОДИТ ПОД ТВОИ КРИТЕРИИ</b>" if matches(l, CONFIG)
            else "ℹ️ <i>Дополнительный вариант (вне основных критериев)</i>")
    lines = [head, f"🏠 <b>{esc(kinds.get(l.kind, l.kind))}: {esc(l.title)}</b>"]
    parts = []
    if l.price:
        parts.append(f"💶 €{l.price}/мес")
    if l.size:
        parts.append(f"📐 {l.size} м²")
    if l.rooms:
        parts.append(f"🚪 комнат: {l.rooms}")
    lines.append(" · ".join(parts))
    if l.available:
        lines.append("📅 свободно сейчас" if l.available <= date.today().isoformat()
                     else f"📅 свободно с {l.available}")
    reg = {True: "✅ регистрация возможна", False: "❌ без регистрации",
           None: "❓ регистрация не указана, уточни у арендодателя"}[l.registration]
    lines.append(reg)
    lines += [esc(n) for n in l.notes if n]
    lines += warnings(l)
    lines.append(f'🔗 <a href="{l.url}">{l.source}</a>')
    return "\n".join(lines)


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send(text: str) -> None:
    token = os.environ["TELEGRAM_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": False},
        timeout=30,
    )
    if r.status_code == 429:
        time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
        return send(text)
    r.raise_for_status()


# ---------------------------------------------------------------- main
SOURCES = [("Pararius", pararius, pararius_details),
           ("Huurwoningen", huurwoningen, pararius_details),
           ("Kamernet", kamernet, kamernet_details),
           ("Marktplaats", marktplaats, text_details),
           ("Nederwoon", nederwoon, text_details),
           ("Kamer.nl", kamernl, text_details),
           ("123wonen", wonen123, wonen123_details)]


def main() -> int:
    dry = "--dry-run" in sys.argv
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    seen: dict = json.loads(SEEN_FILE.read_text("utf-8")) if SEEN_FILE.exists() else {}
    first_run = "_started" not in seen
    seen.setdefault("_started", date.today().isoformat())
    errors, new = [], []

    for name, fetch, details in SOURCES:
        try:
            found = fetch()
        except Exception as e:  # один сломанный сайт не должен ронять остальные
            errors.append(f"{name}: {e}")
            continue
        print(f"{name}: найдено {len(found)}")
        for l in found:
            if l.key in seen or not passes_basic(l):
                continue
            try:
                details(l)
                time.sleep(1)
            except Exception as e:
                l.notes.append(f"детали не загрузились: {e}")
            seen[l.key] = date.today().isoformat()
            if passes_details(l):
                new.append(l)

    print(f"Новых подходящих: {len(new)}")
    for l in new:
        msg = format_msg(l)
        print("\n" + msg)
        if not dry:
            send(msg)
            time.sleep(1)

    if not dry:
        if first_run:
            send(f"🤖 Бот запущен. Слежу за жильём в {', '.join(CONFIG['cities'])} "
                 f"до €{CONFIG['max_price']}. Выше всё, что подходит прямо сейчас "
                 f"({len(new)} шт.), дальше буду присылать только новое.")
        SEEN_FILE.write_text(json.dumps(seen, indent=1, sort_keys=True), "utf-8")
    for e in errors:
        print("ОШИБКА", e, file=sys.stderr)
    # Падаем, только если не сработал ни один источник: тогда GitHub пришлёт письмо.
    return 1 if len(errors) == len(SOURCES) else 0


if __name__ == "__main__":
    sys.exit(main())
