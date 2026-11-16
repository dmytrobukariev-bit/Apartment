"""Ищет студии и квартиры в аренду и присылает новые объявления в Telegram.

Источники: Nederwoon, Marktplaats, Kamernet, Kamer.nl, 123wonen, Pararius и
Huurwoningen (последние два, если не блокируют запросы). Настройки фильтра в
config.json, токен бота и chat id в переменных окружения TELEGRAM_TOKEN и
TELEGRAM_CHAT_ID.

Объявления показываются по одному с кнопками «👍 Оставить» / «👎 Не то».
Очередь, сохранённые и уже виденные объявления хранятся в state.json.

Запуск:  python huizen_bot.py            (работать ~5,5 часов: сайты + кнопки)
         python huizen_bot.py --dry-run  (только показать найденное, ничего не слать)
"""

import json
import os
import re
import subprocess
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


# ---------------------------------------------------------------- спам
# Платные «сервисы поиска жилья» выкладывают десятки однотипных объявлений
# («Studio gevonden in of bij Apeldoorn») со ссылкой на свою подписку. Это не квартиры.
SPAM_TITLE = re.compile(r"gevonden in of bij|woonruimte gevonden|nu beschikbaar via", re.I)
SPAM_SELLER = re.compile(r"zoekservice|snel wonen|huurservice|woningzoek|rentola|huurzone|huurflits", re.I)


# Не жильё: магазины, офисы, склады, гаражи, домики на турбазах
NOT_HOME = re.compile(r"winkel|bedrijfs|kantoor|praktijk|opslag|garage|parkeer|chalet|"
                      r"stacaravan|recreatie|vakantie", re.I)


def is_spam(title: str, seller: str = "") -> bool:
    return bool(SPAM_TITLE.search(title) or NOT_HOME.search(title)
                or (seller and SPAM_SELLER.search(seller)))


def purge_spam(st: dict) -> None:
    """Убирает уже попавший в очередь спам (и с экрана, если он сейчас показан)."""
    before = len(st["queue"])
    st["queue"] = [q for q in st["queue"] if not is_spam(q["title"])]
    if before != len(st["queue"]):
        print(f"Убрано рекламы из очереди: {before - len(st['queue'])}")
    cur = st.get("current")
    if cur and is_spam(cur["title"]):
        delete(cur["message_id"])
        st["current"] = None
    for sid in [s for s, v in st["saved"].items() if is_spam(v["title"])]:
        st["saved"].pop(sid)


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
            seller = ((x.get("sellerInformation") or {}).get("sellerName") or "").lower()
            if is_spam(title, seller):
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


def html_to_plain(text: str) -> str:
    plain = re.sub(r'<a href="([^"]+)">[^<]*</a>', r"\1", text)
    plain = re.sub(r"<[^>]+>", "", plain)
    return plain.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


class TelegramError(RuntimeError):
    def __init__(self, status: int, text: str):
        super().__init__(f"Telegram {status}: {text[:300]}")
        self.status = status


def tg(method: str, attempt: int = 0, http_timeout: int = 40, **params) -> dict:
    """Вызов Telegram Bot API с повторами при сбоях и лимитах."""
    token = os.environ["TELEGRAM_TOKEN"]
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                          json=params, timeout=http_timeout)
    except requests.RequestException:
        if attempt < 3:
            time.sleep(5)
            return tg(method, attempt + 1, http_timeout, **params)
        raise
    if r.status_code == 429 and attempt < 5:
        time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
        return tg(method, attempt + 1, http_timeout, **params)
    if not r.ok:
        raise TelegramError(r.status_code, r.text)
    return r.json().get("result")


def send(text: str, buttons: list | None = None, disable_preview: bool = False) -> int:
    """Отправляет сообщение (HTML, при ошибке разметки простым текстом). Возвращает message_id."""
    params = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": disable_preview}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    try:
        return tg("sendMessage", **params)["message_id"]
    except TelegramError as e:
        if e.status != 400:
            raise
        print("Telegram 400, шлю простым текстом:", e, file=sys.stderr)
        params.pop("parse_mode")
        params["text"] = html_to_plain(text)
        return tg("sendMessage", **params)["message_id"]


def delete(message_id: int) -> None:
    try:
        tg("deleteMessage", chat_id=os.environ["TELEGRAM_CHAT_ID"], message_id=message_id)
    except TelegramError:
        # Сообщения старше 48 часов Telegram удалять не даёт: просто сворачиваем текст.
        try:
            tg("editMessageText", chat_id=os.environ["TELEGRAM_CHAT_ID"],
               message_id=message_id, text="👎 скрыто")
        except TelegramError:
            pass


# ---------------------------------------------------------------- state
STATE_FILE = HERE / "state.json"
LIKE_BUTTONS = [[{"text": "👍 Оставить", "callback_data": "like"},
                 {"text": "👎 Не то", "callback_data": "no"}]]
SAVED_BUTTONS = [[{"text": "🗑 Убрать из сохранённых", "callback_data": "unlike"}]]


def load_state() -> dict:
    if STATE_FILE.exists():
        st = json.loads(STATE_FILE.read_text("utf-8"))
    else:  # переход со старого формата
        old = json.loads(SEEN_FILE.read_text("utf-8")) if SEEN_FILE.exists() else {}
        st = {"_started": old.pop("_started", None), "seen": {}}
    st.setdefault("seen", {})
    st.setdefault("queue", [])     # объявления, которые ещё не показаны
    st.setdefault("current", None)  # {"message_id", "key", "text"} — то, что сейчас на экране
    st.setdefault("saved", {})      # message_id -> {"key", "title", "url"}
    st.setdefault("offset", 0)      # для getUpdates
    st.setdefault("notice_id", None)
    return st


def save_state(st: dict) -> None:
    STATE_FILE.write_text(json.dumps(st, indent=1, ensure_ascii=False, sort_keys=True), "utf-8")


# ---------------------------------------------------------------- дубли
# Одна и та же квартира часто висит на нескольких сайтах (например, Nederwoon
# и Marktplaats). Узнаём её по улице, цене (±€10) и площади.
STREET_RE = re.compile(
    r"([a-zà-ÿ][\w\-'.]*(?:straat|laan|weg|plein|gracht|kade|singel|hof|park|dreef|pad|"
    r"steeg|markt|dijk|veld|erf|hoeve|boulevard|allee|wal|haven|ring|baan|berg|kamp|gang|"
    r"poort|kanaal|kanaal-noord|kanaal-zuid))\b")


def fingerprint(item: dict) -> dict:
    text = item.get("title", "") + " " + item.get("text", "")
    head = (item.get("title") or "").lower()
    street = STREET_RE.findall(head.replace("apeldoorn", " "))
    price = re.search(r"€(\d+)/мес", text)
    size = re.search(r"📐 (\d+) м²", text) or re.search(r"(\d+)\s*m²", text)
    kind = re.search(r"🏠 <b>(\w+)", text)
    return {"street": street[-1] if street else "",
            "price": int(price.group(1)) if price else 0,
            "size": int(size.group(1)) if size else 0,
            "kind": kind.group(1) if kind else ""}


def same_home(a: dict, b: dict) -> bool:
    if not a["price"] or not b["price"] or abs(a["price"] - b["price"]) > 10:
        return False
    if a["street"] and b["street"]:
        return a["street"] == b["street"] and (not a["size"] or not b["size"]
                                               or abs(a["size"] - b["size"]) <= 2)
    if a["size"] and b["size"]:
        return a["size"] == b["size"]
    # Улица или площадь неизвестны у одного из двух: та же цена (±€2) и тот же тип
    return abs(a["price"] - b["price"]) <= 2 and a.get("kind") == b.get("kind") != ""


def is_duplicate(st: dict, item: dict) -> bool:
    fp = fingerprint(item)
    others = st.setdefault("shown_fps", []) + [fingerprint(q) for q in st["queue"]]
    if st["current"]:
        others.append(fingerprint(st["current"]))
    return any(same_home(fp, o) for o in others)


def dedupe_queue(st: dict) -> None:
    """Убирает из уже собранной очереди повторы (одно жильё с разных сайтов)."""
    queue, st["queue"] = st["queue"], []
    removed = 0
    for item in queue:
        if is_duplicate(st, item):
            removed += 1
        else:
            st["queue"].append(item)
    if removed:
        print(f"Убрано повторов из очереди: {removed}")


def dedupe_saved(st: dict) -> None:
    """Одна квартира, сохранённая дважды (с разных сайтов): оставляем одну, с подробностями."""
    items = list(st["saved"].items())
    # оставляем самый подробный вариант (с площадью, датой, описанием)
    items.sort(key=lambda kv: (-len(kv[1].get("summary") or ""), -len(kv[1].get("text") or "")))
    kept, drop = [], []
    for sid, v in items:
        fp = fingerprint({"title": v["title"], "text": v.get("text", "")})
        if any(same_home(fp, k) for k in kept):
            drop.append(sid)
        else:
            kept.append(fp)
    for sid in drop:
        print("Убран повтор из сохранённых:", st["saved"].pop(sid)["title"])
    if drop:
        refresh_saved_list(st)


# ---------------------------------------------------------------- scraping
def scrape(st: dict) -> list[str]:
    """Проверяет все сайты и кладёт новые подходящие объявления в очередь."""
    errors, added = [], 0
    queued = {q["key"] for q in st["queue"]}
    # Сохранённые до появления сводок: дозаполним их, когда встретим на сайте
    need_info = {v["key"]: v for v in st["saved"].values() if v.get("key") and not v.get("text")}
    for name, fetch, details in SOURCES:
        try:
            found = fetch()
        except Exception as e:  # один сломанный сайт не должен ронять остальные
            errors.append(f"{name}: {e}")
            continue
        print(f"{name}: найдено {len(found)}")
        for l in found:
            if l.key in need_info:
                v = need_info.pop(l.key)
                v["text"] = nice_dates(format_msg(l)) + description_block(l.url)
                v["summary"] = short_summary(v["text"])
                print("Дополнено сохранённое:", v["summary"])
            if l.key in st["seen"] or l.key in queued or not passes_basic(l):
                continue
            try:
                details(l)
                time.sleep(1)
            except Exception as e:
                print(f"{name}: детали {l.url} не загрузились: {e}", file=sys.stderr)
            st["seen"][l.key] = date.today().isoformat()
            if not passes_details(l):  # прямо написано «без регистрации»
                continue
            item = {"key": l.key, "text": format_msg(l), "star": matches(l, CONFIG),
                    "title": l.title, "url": l.url}
            if is_duplicate(st, item):
                print(f"Повтор, пропускаю: {l.title}")
                continue
            # ⭐ встают перед дополнительными вариантами
            pos = len(st["queue"])
            if item["star"]:
                pos = next((i for i, q in enumerate(st["queue"]) if not q["star"]), pos)
            st["queue"].insert(pos, item)
            queued.add(l.key)
            added += 1
    dedupe_saved(st)
    print(f"Добавлено в очередь: {added}, всего в очереди: {len(st['queue'])}")
    for e in errors:
        print("ОШИБКА", e, file=sys.stderr)
    return errors


# ---------------------------------------------------------------- описание + перевод
DESC_SELECTORS = [
    "div.description-content",                      # Nederwoon
    "[class*=Description-module-description]",      # Marktplaats
    "[class*=About_collapsedText]",                 # Kamernet
    ".listing-detail-description__additional",      # Pararius / Huurwoningen
    "div.leading-7.text-dark-gray-700",             # Kamer.nl
]
DESC_MAX = 1500


def get_description(url: str) -> str:
    """Достаёт текст описания со страницы объявления (пустая строка, если не вышло)."""
    try:
        soup = BeautifulSoup(get(url).text, "html.parser")
    except Exception as e:
        print("Описание не загрузилось:", url, e, file=sys.stderr)
        return ""
    for sel in DESC_SELECTORS:
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 80:
            text = el.get_text("\n", strip=True)
            break
    else:
        candidates = []
        for sc in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(sc.string or "")
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and isinstance(data.get("description"), str):
                candidates.append(BeautifulSoup(data["description"], "html.parser").get_text(" "))
        text = max(candidates, key=len, default="")
        if len(text) < 150:
            return ""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    if len(text) > DESC_MAX:
        text = text[:DESC_MAX].rsplit(" ", 1)[0] + "…"
    return text


def _chunks(text: str, size: int) -> list[str]:
    parts, cur = [], ""
    for sent in re.split(r"(?<=[.!?\n])\s*", text):
        if len(cur) + len(sent) + 1 > size and cur:
            parts.append(cur)
            cur = ""
        cur += (" " if cur else "") + sent[:size]
    if cur:
        parts.append(cur)
    return parts


def translate_ru(text: str) -> str | None:
    """Перевод на русский: сначала Google, потом MyMemory. None, если оба не сработали."""
    try:
        r = requests.get("https://translate.googleapis.com/translate_a/single",
                         params={"client": "gtx", "sl": "auto", "tl": "ru", "dt": "t", "q": text},
                         headers=HEADERS, timeout=20)
        if r.ok:
            out = "".join(p[0] for p in r.json()[0] if p and p[0])
            if out.strip():
                return out
    except Exception as e:
        print("Google перевод:", e, file=sys.stderr)
    try:
        out = []
        for chunk in _chunks(text, 450):  # MyMemory: до 500 символов за запрос
            r = requests.get("https://api.mymemory.translated.net/get",
                             params={"q": chunk, "langpair": "nl|ru"}, timeout=20)
            data = r.json()
            if r.ok and data.get("responseStatus") in (200, "200") and not data.get("quotaFinished"):
                out.append(data["responseData"]["translatedText"])
            else:
                return None
            time.sleep(0.5)
        return " ".join(out) or None
    except Exception as e:
        print("MyMemory перевод:", e, file=sys.stderr)
        return None


def description_block(url: str) -> str:
    original = get_description(url)
    if not original:
        return ""
    ru = translate_ru(original)
    if ru and len(ru) > 2500:
        ru = ru[:2500].rsplit(" ", 1)[0] + "…"
    if ru:
        return f"\n\n📝 <b>Описание</b> (перевод):\n<blockquote expandable>{esc(ru)}</blockquote>"
    return (f"\n\n📝 <b>Описание</b> (перевод сейчас недоступен):\n"
            f"<blockquote expandable>{esc(original)}</blockquote>")


# ---------------------------------------------------------------- даты по-русски
RU_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
             "сентября", "октября", "ноября", "декабря"]


def ru_date(d: date) -> str:
    s = f"{d.day} {RU_MONTHS[d.month - 1]}"
    return s if d.year == date.today().year else f"{s} {d.year}"


def nice_dates(text: str) -> str:
    """«свободно с 2026-12-01» → «свободно с 1 декабря (через 16 дней) ✅ успеваешь до 11 января»."""
    needed = date.fromisoformat(CONFIG["needed_by"])

    def available(m: re.Match) -> str:
        d = date.fromisoformat(m.group(1))
        days = (d - date.today()).days
        if days <= 0:
            return "📅 свободно сейчас ✅"
        mark = (f"✅ успеваешь до {ru_date(needed)}" if d <= needed
                else f"⚠️ позже твоего выселения ({ru_date(needed)})")
        return f"📅 свободно с {ru_date(d)} (через {days} дн.) {mark}"

    text = re.sub(r"📅 свободно с (\d{4}-\d{2}-\d{2})", available, text)
    text = text.replace("📅 свободно сейчас\n", "📅 свободно сейчас ✅\n")
    # заезд позже нужной даты уже отмечен выше, отдельное предупреждение лишнее
    text = re.sub(r"\n⚠️ заезд только с [^\n]*", "", text)
    return re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b",
                  lambda m: ru_date(date(int(m.group(1)), int(m.group(2)), int(m.group(3)))), text)


# ---------------------------------------------------------------- queue
def show_next(st: dict) -> None:
    """Показывает следующее объявление из очереди, если сейчас ничего не показано."""
    if st["current"]:
        return
    if not st["queue"]:
        return
    if st.get("notice_id"):
        delete(st["notice_id"])
        st["notice_id"] = None
    item = st["queue"].pop(0)
    st.setdefault("shown_fps", []).append(fingerprint(item))
    st["shown_fps"] = st["shown_fps"][-1000:]
    left = len(st["queue"])
    full = nice_dates(item["text"]) + description_block(item["url"])
    text = full + (f"\n\n📬 В очереди ещё: {left}" if left else "\n\n📬 Это последнее в очереди")
    mid = send(text, LIKE_BUTTONS)
    st["current"] = {"message_id": mid, "key": item["key"], "text": full,
                     "title": item["title"], "url": item["url"]}


def queue_empty_notice(st: dict) -> None:
    if not st["current"] and not st["queue"] and not st.get("notice_id"):
        st["notice_id"] = send("✅ Очередь пуста. Пришлю, как только появится что-то новое.")


HELP = ("🤖 Я ищу жильё в Апельдорне и присылаю объявления по одному.\n\n"
        "👍 Оставить: сохраню (сообщение само исчезнет через минуту, чтобы не засорять чат)\n"
        "👎 Не то: удалю его и покажу следующее\n\n"
        "Команды:\n/saved: сохранённые (нажми на вариант, чтобы открыть подробно)\n"
        "/queue: сколько осталось в очереди\n"
        "/next: показать следующее, если застряло")
SAVED_DELETE_AFTER = 60       # «💚 Сохранено» исчезает через минуту
DETAILS_DELETE_AFTER = 10 * 60  # открытые из /saved подробности и сам список: через 10 минут


def schedule_delete(st: dict, mid: int, after: int) -> None:
    st.setdefault("auto_delete", []).append([mid, time.time() + after])


def cancel_delete(st: dict, mid: int) -> None:
    st["auto_delete"] = [x for x in st.get("auto_delete", []) if x[0] != mid]


def run_auto_delete(st: dict) -> bool:
    due = [x for x in st.get("auto_delete", []) if x[1] <= time.time()]
    for mid, _ in due:
        if mid == st.get("saved_list_id"):
            st["saved_list_id"] = None
        try:
            tg("deleteMessage", chat_id=os.environ["TELEGRAM_CHAT_ID"], message_id=mid)
        except TelegramError:
            pass
    st["auto_delete"] = [x for x in st.get("auto_delete", []) if x[1] > time.time()]
    return bool(due)


def poll_wait(st: dict) -> int:
    """Сколько ждать нажатий: до ближайшего автоудаления, но не больше 50 секунд."""
    soonest = min((x[1] for x in st.get("auto_delete", [])), default=time.time() + 50)
    return int(max(1, min(50, soonest - time.time() + 1)))


def saved_list_markup(st: dict) -> dict:
    rows = []
    for i, (sid, v) in enumerate(st["saved"].items(), 1):
        label = f"{i}. {v.get('summary') or v['title']}"
        rows.append([{"text": label[:60], "callback_data": f"show:{sid}"}])
    return {"inline_keyboard": rows}


def refresh_saved_list(st: dict) -> None:
    lid = st.get("saved_list_id")
    if not lid:
        return
    try:
        if st["saved"]:
            tg("editMessageReplyMarkup", chat_id=os.environ["TELEGRAM_CHAT_ID"],
               message_id=lid, reply_markup=saved_list_markup(st))
        else:
            delete(lid)
            st["saved_list_id"] = None
    except TelegramError:
        pass


def saved_details(v: dict) -> str:
    if v.get("text"):
        return "💚 <b>СОХРАНЁННОЕ</b>\n" + v["text"]
    lines = ["💚 <b>СОХРАНЁННОЕ</b>"]
    if v.get("summary"):
        lines.append(f"<b>{esc(v['summary'])}</b>")
    lines.append(esc(v["title"]))
    if v.get("url"):
        source = v["key"].split(":")[0] if v.get("key") else "ссылка"
        lines.append(f'🔗 <a href="{v["url"]}">{esc(source)}</a>')
    return "\n".join(lines)


def handle_updates(st: dict, wait: int) -> bool:
    """Обрабатывает нажатия кнопок и команды. Возвращает True, если что-то изменилось."""
    try:
        updates = tg("getUpdates", http_timeout=wait + 15, timeout=wait, offset=st["offset"],
                     allowed_updates=["message", "callback_query"]) or []
    except TelegramError as e:
        print("getUpdates:", e, file=sys.stderr)
        time.sleep(5)
        return False
    chat = str(os.environ["TELEGRAM_CHAT_ID"])
    changed = False
    for u in updates:
        st["offset"] = u["update_id"] + 1
        changed = True
        if "callback_query" in u:
            cq = u["callback_query"]
            msg = cq.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != chat:
                continue
            on_button(st, cq["data"], msg["message_id"], cq["id"])
        elif "message" in u:
            m = u["message"]
            if str(m.get("chat", {}).get("id")) != chat:
                continue
            on_command(st, (m.get("text") or "").strip().split("@")[0])
    return changed


def needs_confirm(action: str) -> bool:
    # 👎 «Не то» удаляет сразу; подтверждение только для сохранённых
    return action == "unlike" or action.startswith("unsave:")


def original_buttons(action: str) -> list:
    """Какие кнопки вернуть, если удаление отменили."""
    if action == "no":
        return LIKE_BUTTONS
    if action == "unlike":
        return SAVED_BUTTONS
    sid = action.split(":", 1)[1]
    return [[{"text": "✖ Закрыть", "callback_data": "close"}],
            [{"text": "🗑 Убрать из сохранённых", "callback_data": f"unsave:{sid}"}]]


def on_button(st: dict, action: str, mid: int, cq_id: str) -> None:
    chat = os.environ["TELEGRAM_CHAT_ID"]

    def answer_cq(text: str) -> None:
        try:
            tg("answerCallbackQuery", callback_query_id=cq_id, text=text)
        except TelegramError:
            pass

    # Удаление: сначала спрашиваем подтверждение
    if needs_confirm(action):
        try:
            tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={
                "inline_keyboard": [[{"text": "✅ Да, удалить", "callback_data": f"y:{action}"},
                                     {"text": "↩️ Отмена", "callback_data": f"back:{action}"}]]})
        except TelegramError:
            pass
        answer_cq("Точно удалить?")
        return
    if action.startswith("back:"):
        try:
            tg("editMessageReplyMarkup", chat_id=chat, message_id=mid,
               reply_markup={"inline_keyboard": original_buttons(action[5:])})
        except TelegramError:
            pass
        answer_cq("Отменено")
        return
    if action.startswith("y:"):
        action = action[2:]

    cur = st["current"]
    is_current = cur and cur["message_id"] == mid
    answer = ""
    if action == "like":
        info = ({"key": cur["key"], "title": cur["title"], "url": cur["url"],
                 "summary": short_summary(cur["text"]), "text": cur["text"]} if is_current
                else {"key": "", "title": "объявление", "url": ""})
        st["saved"][str(mid)] = info
        try:
            if is_current:
                tg("editMessageText", chat_id=os.environ["TELEGRAM_CHAT_ID"], message_id=mid,
                   text=f"💚 <b>Сохранено:</b> {esc(info['summary'] or info['title'])}\n"
                        f"<i>Это сообщение исчезнет через минуту. Открыть снова: /saved</i>",
                   parse_mode="HTML", disable_web_page_preview=True,
                   reply_markup={"inline_keyboard": SAVED_BUTTONS})
            else:
                tg("editMessageReplyMarkup", chat_id=os.environ["TELEGRAM_CHAT_ID"],
                   message_id=mid, reply_markup={"inline_keyboard": SAVED_BUTTONS})
        except TelegramError:
            pass
        schedule_delete(st, mid, SAVED_DELETE_AFTER)
        refresh_saved_list(st)
        answer = "Сохранено 💚"
    elif action in ("no", "unlike"):
        if st["saved"].pop(str(mid), None) is not None:
            refresh_saved_list(st)
        cancel_delete(st, mid)
        delete(mid)
        answer = "Убрано"
    elif action.startswith("show:"):
        v = st["saved"].get(action[5:])
        if v:
            sid = action[5:]
            new_mid = send(saved_details(v), [
                [{"text": "✖ Закрыть", "callback_data": "close"}],
                [{"text": "🗑 Убрать из сохранённых", "callback_data": f"unsave:{sid}"}]])
            schedule_delete(st, new_mid, DETAILS_DELETE_AFTER)
        else:
            answer = "Уже удалено"
    elif action.startswith("unsave:"):
        st["saved"].pop(action[7:], None)
        cancel_delete(st, mid)
        delete(mid)
        refresh_saved_list(st)
        answer = "Убрано из сохранённых"
    elif action == "close":
        cancel_delete(st, mid)
        delete(mid)
    if is_current and action in ("like", "no"):
        st["current"] = None
    try:
        tg("answerCallbackQuery", callback_query_id=cq_id, text=answer)
    except TelegramError:
        pass
    if action in ("like", "no"):
        show_next(st)
        queue_empty_notice(st)


def short_summary(text: str) -> str:
    """Короткая строка из полного текста объявления: тип · цена · площадь · дата."""
    parts = []
    kind = re.search(r"🏠 <b>([^:<]+):", text)
    if kind:
        parts.append(kind.group(1))
    price = re.search(r"€(\d+)/мес", text)
    if price:
        parts.append(f"€{price.group(1)}/мес")
    size = re.search(r"(\d+) м²", text)
    if size:
        parts.append(f"{size.group(1)} м²")
    when = re.search(r"📅 свободно (с [^(\n]+?|сейчас)(?:\s*\(|\s*✅|\n|$)", text)
    if when:
        parts.append(f"свободно {when.group(1).strip()}")
    if "✅ регистрация возможна" in text:
        parts.append("регистрация ✅")
    return " · ".join(parts)


def on_command(st: dict, cmd: str) -> None:
    if cmd == "/saved":
        if not st["saved"]:
            send("Сохранённых пока нет.")
        else:
            if st.get("saved_list_id"):  # старый список убираем, чтобы не копились
                cancel_delete(st, st["saved_list_id"])
                delete(st["saved_list_id"])
            lid = send(f"💚 <b>Сохранённые ({len(st['saved'])})</b>\n"
                       "Нажми на вариант, чтобы открыть подробно:")
            tg("editMessageReplyMarkup", chat_id=os.environ["TELEGRAM_CHAT_ID"],
               message_id=lid, reply_markup=saved_list_markup(st))
            st["saved_list_id"] = lid
            schedule_delete(st, lid, DETAILS_DELETE_AFTER)
    elif cmd == "/queue":
        stars = sum(q["star"] for q in st["queue"])
        send(f"📬 В очереди: {len(st['queue'])} (из них ⭐ {stars})")
    elif cmd == "/next":
        st["current"] = None
        show_next(st)
        queue_empty_notice(st)
    else:
        send(HELP)


# ---------------------------------------------------------------- git (состояние в репозитории)
def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True)


def commit_state() -> None:
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    git("add", "state.json")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("commit", "-q", "-m", "update bot state")
    for _ in range(3):
        if git("push", "-q").returncode == 0:
            return
        git("pull", "-q", "--rebase", "-X", "theirs")
    print("Не получилось сохранить состояние в репозиторий", file=sys.stderr)


CODE_FILES = ("huizen_bot.py", "config.json", "requirements.txt", ".github")
# Версия, с которой бот реально запущен. Сравнивать с HEAD нельзя: при сохранении
# состояния бот делает git pull, HEAD уезжает на новый код, а в памяти остаётся старый.
STARTED_AT_SHA = ""


def code_changed() -> bool:
    """Появилась ли на GitHub новая версия бота (тогда перезапускаемся)."""
    global STARTED_AT_SHA
    if not os.environ.get("GITHUB_ACTIONS"):
        return False
    if not STARTED_AT_SHA:
        STARTED_AT_SHA = os.environ.get("GITHUB_SHA") or git("rev-parse", "HEAD").stdout.strip()
    git("fetch", "-q", "origin", "main")
    return git("diff", "--quiet", STARTED_AT_SHA, "origin/main", "--",
               *CODE_FILES).returncode != 0


def restart_workflow() -> None:
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        return
    r = requests.post(
        f"https://api.github.com/repos/{repo}/actions/workflows/check.yml/dispatches",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"ref": "main"}, timeout=30)
    print("Перезапуск workflow:", r.status_code, r.text[:200])


# ---------------------------------------------------------------- main
SOURCES = [("Pararius", pararius, pararius_details),
           ("Huurwoningen", huurwoningen, pararius_details),
           ("Kamernet", kamernet, kamernet_details),
           ("Marktplaats", marktplaats, text_details),
           ("Nederwoon", nederwoon, text_details),
           ("Kamer.nl", kamernl, text_details),
           ("123wonen", wonen123, wonen123_details)]

SCRAPE_EVERY = 10 * 60        # проверять сайты каждые 10 минут
COMMIT_EVERY = 5 * 60         # сохранять состояние в репозиторий не чаще раза в 5 минут
RUN_FOR = 5 * 3600 + 40 * 60  # GitHub даёт до 6 часов на запуск, потом перезапуск


def serve() -> int:
    st = load_state()
    purge_spam(st)
    dedupe_queue(st)
    if not st.get("help_sent"):
        st["help_sent"] = True
        send(HELP)
    began = time.time()
    last_scrape = last_commit = 0.0
    dirty = True
    while time.time() - began < RUN_FOR:
        if time.time() - last_scrape >= SCRAPE_EVERY:
            if code_changed():
                print("Есть новая версия бота, перезапускаюсь")
                break
            scrape(st)
            last_scrape = time.time()
            dirty = True
            try:
                show_next(st)
            except TelegramError as e:
                print("Не удалось показать объявление:", e, file=sys.stderr)
        if handle_updates(st, wait=poll_wait(st)):
            dirty = True
        if run_auto_delete(st):
            dirty = True
        if dirty:
            save_state(st)
            if time.time() - last_commit >= COMMIT_EVERY:
                commit_state()
                last_commit = time.time()
                dirty = False
    save_state(st)
    commit_state()
    restart_workflow()
    return 0


def dry_run() -> int:
    st = load_state()
    st["queue"], st["seen"] = [], {}
    scrape(st)
    for q in st["queue"]:
        print("\n" + q["text"])
    return 0


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    if "--dry-run" in sys.argv:
        return dry_run()
    return serve()


if __name__ == "__main__":
    sys.exit(main())
