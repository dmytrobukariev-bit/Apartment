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


# ---------------------------------------------------------------- Pararius
def pararius() -> list[Listing]:
    out = []
    for city in CONFIG["cities"]:
        slug = city.lower().replace(" ", "-")
        for page in range(1, 4):
            url = f"https://www.pararius.com/apartments/{slug}/0-{SEARCH_PRICE}"
            if page > 1:
                url += f"/page-{page}"
            soup = BeautifulSoup(get(url).text, "html.parser")
            items = soup.select("section.listing-search-item--for-rent")
            for it in items:
                link = it.select_one(".listing-search-item__link--title")
                if not link:
                    continue
                href = link["href"]
                label = it.select_one(".listing-label")
                label_text = label.get_text(" ", strip=True) if label else ""
                feats = {
                    li["class"][-1].split("--")[-1]: li.get_text(" ", strip=True)
                    for li in it.select(".illustrated-features li")
                }
                price_el = it.select_one(".listing-search-item__price-main") or it.select_one(
                    ".listing-search-item__price")
                sub = it.select_one(".listing-search-item__sub-title")
                kind = "studio" if href.startswith("/studio") else (
                    "room" if href.startswith("/room") else "apartment")
                l = Listing(
                    source="Pararius",
                    id=href.split("/")[3],
                    url="https://www.pararius.com" + href,
                    title=link.get_text(" ", strip=True) + (
                        f", {sub.get_text(' ', strip=True)}" if sub else ""),
                    price=to_int(price_el.get_text() if price_el else None),
                    size=to_int(feats.get("surface-area")),
                    rooms=to_int(feats.get("number-of-rooms")),
                    kind=kind,
                    city=city,
                )
                if label_text:
                    l.notes.append(label_text)
                out.append(l)
            if not soup.select_one(".pagination__item--next a, a.pagination__link--next"):
                break
    return out


def pararius_details(l: Listing) -> None:
    """Подтягивает со страницы объявления дату заезда и упоминания регистрации."""
    soup = BeautifulSoup(get(l.url).text, "html.parser")
    text = soup.get_text(" ", strip=True).lower()
    l.registration = check_registration_text(text)
    m = re.search(r"available\s+(?:from\s+)?(\d{2})-(\d{2})-(\d{4})", text)
    if m:
        l.available = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    elif "available immediately" in text:
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


def wonen123_details(l: Listing) -> None:
    text = BeautifulSoup(get(l.url).text, "html.parser").get_text(" ", strip=True).lower()
    l.registration = check_registration_text(text)


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
    if l.price is None or l.price > c["max_price"]:
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
        lines.append(f"📅 свободно с {l.available}")
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
           ("Kamernet", kamernet, kamernet_details),
           ("123wonen", wonen123, wonen123_details)]


def main() -> int:
    dry = "--dry-run" in sys.argv
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
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
