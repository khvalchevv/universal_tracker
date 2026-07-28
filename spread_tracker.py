#!/usr/bin/env python3
"""Трекер спреду між нативним активом (Binance) і обгорткою Universal (Base).

Рахує три числа на кожен актив:

  premium_buy   скільки переплачуєш за обгортку проти купівлі оригіналу
  premium_sell  скільки виручиш за обгортку проти продажу оригіналу
  arb           чистий результат: Binance -> мінт 1:1 -> продаж на Base

arb — головне число. Universal мінтить нативний актив в обгортку один-в-один
без комісій і сам платить газ, тож якщо обгортка продається на Base дорожче,
ніж оригінал коштує на Binance, різниця за вирахуванням газу свопу лишається
в кишені. Саме по ньому спрацьовує алерт.

Ціни на Base — виконувані квоти агрегатора на конкретний розмір угоди, а не
середина пулу. Прослизання вже враховане: на XRP різниця між маркою пулу і
реальною квотою доходить до 0.6 п.п.

Пороги за замовчуванням підібрані по 42-45 днях погодинної історії так, щоб
алерт ловив реальні дислокації, а не постійний шум (див. THRESHOLD нижче).

Використання:
    python spread_tracker.py                      # SOL+XRP, свої пороги, кожні 30 с
    python spread_tracker.py --assets XRP         # тільки XRP
    python spread_tracker.py --size 5000          # рахувати на розмір $5000
    python spread_tracker.py --threshold 0.5      # один поріг на всі активи
    python spread_tracker.py --once               # один замір і вихід

Telegram:
    1. @BotFather -> /newbot -> токен
    2. напиши своєму боту будь-що (інакше він не має права тобі писати)
    3. python spread_tracker.py --tg-token <TOKEN> --tg-setup   # покаже chat id
    4. python spread_tracker.py --tg-token <TOKEN> --tg-chat <ID> --tg-test

    Щоб не тягати ключі в командному рядку, поклади їх у змінні оточення
    TG_BOT_TOKEN і TG_CHAT_ID — підхопляться самі.
"""

import argparse
import csv
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# --- що торгуємо -------------------------------------------------------------

CHAIN = "base"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DEC = 6

# threshold — поріг алерту у %, підібраний по історії окремо для кожного активу.
# SOL: 0.8% -> ~1.1 год/добу, 2.2 події/тиждень, серії по ~3.4 год.
# XRP: 1.2% -> ~1.3 год/добу, але зібрані в рідкі довгі серії. Медіана в XRP
#      від'ємна, дрібний плюс до 1% — це шум навколо вартості кола, а справжні
#      дислокації в нього виходять глибокі й довгі (27-28 лип: +9% за 14 годин).
ASSETS = {
    "SOL": {
        "token": "0x9B8Df6E244526ab5F6e6400d331DB28C8fdDdb55",
        "decimals": 18,
        "binance": "SOLUSDT",
        "threshold": 0.8,
    },
    "XRP": {
        "token": "0x2615a94df961278DcbC41Fb0a54fEc5f10a693aE",
        "decimals": 18,
        "binance": "XRPUSDT",
        "threshold": 1.2,
    },
}

KYBER = "https://aggregator-api.kyberswap.com/{chain}/api/v1/routes"
BINANCE = "https://api.binance.com/api/v3/ticker/bookTicker?symbol={sym}"
TELEGRAM = "https://api.telegram.org/bot{token}/{method}"

UA = {"User-Agent": "usol-spread-tracker/1.1", "Accept": "application/json"}

# Windows-консоль інакше ріже кирилицю в кракозябри
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

# ANSI кольори — вимикаються, якщо вивід перенаправлено у файл
_tty = sys.stdout.isatty()
DIM = "\033[2m" if _tty else ""
RED = "\033[31m" if _tty else ""
GRN = "\033[32m" if _tty else ""
YEL = "\033[33m" if _tty else ""
BLD = "\033[1m" if _tty else ""
OFF = "\033[0m" if _tty else ""


class FetchError(Exception):
    pass


def fetch_json(url, tries=3, timeout=20):
    """GET з ретраями. Мережа моргає постійно, падати через це не варто."""
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
            last = e
            if attempt < tries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise FetchError(f"{url.split('?')[0]}: {last}")


def binance_book(symbol):
    """Найкращі bid/ask. USDT vs USDC розходяться менш ніж на 0.05%."""
    d = fetch_json(BINANCE.format(sym=symbol))
    return float(d["bidPrice"]), float(d["askPrice"])


def kyber_route(token_in, token_out, amount_wei):
    """Виконувана квота на конкретний розмір. Повертає (amount_out_wei, gas_usd)."""
    url = (f"{KYBER.format(chain=CHAIN)}"
           f"?tokenIn={token_in}&tokenOut={token_out}&amountIn={amount_wei}")
    d = fetch_json(url)
    if d.get("code") != 0 or not d.get("data"):
        raise FetchError(f"kyber: {d.get('message', 'немає маршруту')}")
    r = d["data"]["routeSummary"]
    gas = float(r.get("gasUsd") or 0) + float(r.get("l1FeeUsd") or 0)
    return int(r["amountOut"]), gas


def measure(name, cfg, size_usd):
    """Один замір по одному активу. Обидві сторони знімаються поруч у часі,
    щоб не порівнювати ціни з різних моментів."""
    wrapped, dec = cfg["token"], cfg["decimals"]
    bid, ask = binance_book(cfg["binance"])

    # купівля: size_usd у USDC -> скільки обгортки віддадуть
    out_wrapped, gas_buy = kyber_route(USDC, wrapped, int(round(size_usd * 10 ** USDC_DEC)))
    buy_px = size_usd / (out_wrapped / 10 ** dec)

    time.sleep(0.4)  # не молотити агрегатор двома запитами впритул

    # продаж: еквівалент size_usd в обгортці -> скільки USDC повернеться
    qty = size_usd / ask
    out_usdc, gas_sell = kyber_route(wrapped, USDC, int(round(qty * 10 ** dec)))
    sell_px = (out_usdc / 10 ** USDC_DEC) / qty

    # чистий арбітраж: купив оригінал по ask -> замінтив 1:1 безкоштовно ->
    # продав обгортку на Base. Газ мінту Universal бере на себе, лишається
    # тільки газ свопу.
    arb = ((qty * sell_px - gas_sell) / size_usd - 1) * 100

    return {
        "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "asset": name,
        "spot_bid": bid,
        "spot_ask": ask,
        "wrap_buy": buy_px,
        "wrap_sell": sell_px,
        "premium_buy": (buy_px / ask - 1) * 100,
        "premium_sell": (sell_px / bid - 1) * 100,
        "arb": arb,
        "gas_usd": round(gas_buy + gas_sell, 4),
        "size_usd": size_usd,
    }


def colour(v, threshold):
    if v >= threshold:
        return GRN + BLD
    if v <= -threshold:
        return RED + BLD
    return DIM


def digits(px):
    """XRP коштує близько долара, SOL — сімдесят. Однакова точність тут дурна."""
    return 4 if px < 10 else 3


def render(m, threshold, hit):
    d = digits(m["spot_ask"])
    line = (f"{DIM}{m['ts'][11:19]}{OFF}  {BLD}{m['asset']:<3}{OFF}  "
            f"spot {m['spot_bid']:.{d}f}/{m['spot_ask']:.{d}f}   "
            f"buy {m['wrap_buy']:.{d}f} {DIM}({m['premium_buy']:+.2f}%){OFF}  "
            f"sell {m['wrap_sell']:.{d}f} {DIM}({m['premium_sell']:+.2f}%){OFF}   "
            f"arb {colour(m['arb'], threshold)}{m['arb']:+.2f}%{OFF}")
    if hit:
        line += f"  {YEL}{BLD}<<< {threshold}%{OFF}"
    return line


def write_csv(path, m, threshold, hit):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts", "asset", "spot_bid", "spot_ask", "wrap_buy", "wrap_sell",
                        "premium_buy_pct", "premium_sell_pct", "arb_pct",
                        "gas_usd", "size_usd", "threshold", "alert"])
        w.writerow([m["ts"], m["asset"], f"{m['spot_bid']:.5f}", f"{m['spot_ask']:.5f}",
                    f"{m['wrap_buy']:.5f}", f"{m['wrap_sell']:.5f}",
                    f"{m['premium_buy']:.4f}", f"{m['premium_sell']:.4f}",
                    f"{m['arb']:.4f}", m["gas_usd"], m["size_usd"], threshold, int(hit)])


def notify(url, m, threshold):
    """POST на алерті. Вебхук відправляє дані назовні — тільки якщо явно вказав."""
    body = json.dumps({"threshold": threshold, **m}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={**UA, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        print(f"{RED}  вебхук не пройшов: {e}{OFF}", file=sys.stderr)


# --- telegram ----------------------------------------------------------------

def tg_send(token, chat, text):
    """Повідомлення в Telegram. Мовчки не падає — трекер важливіший за алерт."""
    body = json.dumps({
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(TELEGRAM.format(token=token, method="sendMessage"),
                                 data=body,
                                 headers={**UA, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r).get("ok", False)
    except urllib.error.HTTPError as e:
        print(f"{RED}  telegram {e.code}: "
              f"{e.read().decode('utf-8', 'replace')[:200]}{OFF}", file=sys.stderr)
    except Exception as e:
        print(f"{RED}  telegram: {e}{OFF}", file=sys.stderr)
    return False


def tg_setup(token):
    """Показує chat id усіх, хто вже писав боту."""
    try:
        d = fetch_json(TELEGRAM.format(token=token, method="getUpdates"))
    except FetchError as e:
        print(f"{RED}не вдалося опитати бота: {e}{OFF}", file=sys.stderr)
        return 1
    if not d.get("ok"):
        print(f"{RED}токен відхилено: {d.get('description')}{OFF}", file=sys.stderr)
        return 1
    seen = {}
    for u in d.get("result", []):
        ch = (u.get("message") or u.get("channel_post") or {}).get("chat") or {}
        if ch.get("id"):
            name = ch.get("title") or " ".join(filter(None, [
                ch.get("first_name"), ch.get("last_name")])) or ch.get("username", "")
            seen[ch["id"]] = f"{name} ({ch.get('type')})"
    if not seen:
        print("Історія порожня. Напиши боту будь-яке повідомлення і запусти ще раз.")
        print(f"{DIM}Telegram віддає getUpdates лише за останні ~24 години.{OFF}")
        return 1
    print(f"{BLD}Знайдені чати:{OFF}")
    for cid, who in seen.items():
        print(f"  --tg-chat {cid}   {DIM}{who}{OFF}")
    return 0


def alert_step(s, arb, threshold, cooldown_s, now):
    """Чиста логіка сповіщень — без мережі, щоб її можна було тестувати.

    Мутує стан s і повертає, що саме треба надіслати:
      'open'   — щойно зайшли в зону
      'repeat' — досі в зоні, минув cooldown
      'close'  — вийшли з зони
      None     — мовчимо
    """
    if abs(arb) >= threshold:
        first = not s["in_zone"]
        s["peak"] = arb if first else max(s["peak"], arb, key=abs)
        s["in_zone"] = True
        if first or now - s["last_ping"] >= cooldown_s:
            s["last_ping"] = now
            return "open" if first else "repeat"
        return None
    if s["in_zone"]:
        s["in_zone"] = False
        return "close"
    return None


def tg_close_text(name, arb, threshold, peak):
    return (f"⚪️ <b>{name}: спред закрився</b> — {arb:+.2f}%, "
            f"нижче порогу {threshold}%\n<code>пік у вікні {peak:+.2f}%</code>")


def tg_alert_text(m, threshold):
    d = digits(m["spot_ask"])
    arrow = "🟢" if m["arb"] > 0 else "🔴"
    return (
        f"{arrow} <b>{m['asset']} arb {m['arb']:+.2f}%</b>  <i>(поріг {threshold}%)</i>\n\n"
        f"{m['asset']} Binance ask  <b>{m['spot_ask']:.{d}f}</b>\n"
        f"u{m['asset']} sell (Base)  <b>{m['wrap_sell']:.{d}f}</b>\n"
        f"u{m['asset']} buy  (Base)  {m['wrap_buy']:.{d}f}  ({m['premium_buy']:+.2f}%)\n\n"
        f"<code>розмір ${m['size_usd']:,.0f} · газ ${m['gas_usd']:.2f} · "
        f"{m['ts'][11:16]} UTC</code>"
    )


# --- налаштування, які змінюються з бота ------------------------------------

SETTINGS_FILE = "tracker_settings.json"

HELP = (
    "<b>Команди</b>\n"
    "<code>/status</code> — пороги і останній замір\n"
    "<code>/set XRP 1.5</code> — поріг для активу, у %\n"
    "<code>/size 5000</code> — розмір угоди, на який рахувати\n"
    "<code>/mute 60</code> — тиша на N хвилин\n"
    "<code>/unmute</code> — увімкнути назад\n"
    "<code>/help</code> — цей список"
)


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(rt):
    """Пороги переживають перезапуск — інакше правка з бота губиться."""
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"thresholds": rt["thr"], "size": rt["size"]}, f,
                      ensure_ascii=False, indent=1)
    except OSError as e:
        print(f"{RED}  не зберіг налаштування: {e}{OFF}", file=sys.stderr)


def tg_updates(token, offset, wait=0):
    """Нові повідомлення боту.

    wait > 0 — довге опитування: з'єднання висить на боці Telegram і
    повертається одразу, щойно прийде команда. Це і швидша реакція, і в
    рази менше запитів, ніж смикати сервер щосекунди.
    """
    try:
        url = TELEGRAM.format(token=token, method="getUpdates")
        d = fetch_json(f"{url}?offset={offset}&timeout={wait}"
                       f"&allowed_updates=%5B%22message%22%5D",
                       tries=1, timeout=wait + 12)
        return d.get("result", []) if d.get("ok") else []
    except FetchError:
        return []


def fmt_status(rt, picked, last):
    lines = [f"<b>Статус</b>  <i>розмір ${rt['size']:,.0f}</i>"]
    for n in picked:
        m = last.get(n)
        cur = f"{m['arb']:+.2f}%" if m else "—"
        mark = " 🟢" if m and abs(m["arb"]) >= rt["thr"][n] else ""
        lines.append(f"<code>{n:<3}</code> поріг <b>{rt['thr'][n]}%</b> · зараз {cur}{mark}")
    if rt["muted_until"] > time.time():
        left = int((rt["muted_until"] - time.time()) / 60) + 1
        lines.append(f"\n🔇 тиша ще {left} хв")
    return "\n".join(lines)


def handle_cmd(text, rt, picked, last):
    """Розбирає команду з чату. Повертає текст відповіді або None."""
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return None
    cmd = parts[0].split("@")[0].lower()   # /set@MyBot -> /set
    args = parts[1:]

    if cmd in ("/start", "/help"):
        return HELP

    if cmd == "/status":
        return fmt_status(rt, picked, last)

    if cmd == "/set":
        if len(args) != 2:
            return "Формат: <code>/set XRP 1.5</code>"
        asset = args[0].upper().lstrip("U")     # приймаємо і uXRP, і XRP
        if asset not in picked:
            return f"Не відстежую {args[0]}. Активні: {', '.join(picked)}"
        try:
            val = float(args[1].replace(",", ".").rstrip("%"))
        except ValueError:
            return f"«{args[1]}» — не число"
        if not 0.05 <= val <= 50:
            return "Поріг має бути в межах 0.05…50%"
        old = rt["thr"][asset]
        rt["thr"][asset] = round(val, 3)
        save_settings(rt)
        return f"✅ {asset}: поріг <b>{old}% → {rt['thr'][asset]}%</b>"

    if cmd == "/size":
        if len(args) != 1:
            return "Формат: <code>/size 5000</code>"
        try:
            val = float(args[0].replace(",", "").replace("$", ""))
        except ValueError:
            return f"«{args[0]}» — не число"
        if not 10 <= val <= 1_000_000:
            return "Розмір має бути в межах $10…$1,000,000"
        old = rt["size"]
        rt["size"] = val
        save_settings(rt)
        return (f"✅ розмір: <b>${old:,.0f} → ${val:,.0f}</b>\n"
                f"<i>квоти рахуються на цей обсяг, тож прослизання зміниться</i>")

    if cmd == "/mute":
        mins = 60
        if args:
            try:
                mins = int(args[0])
            except ValueError:
                return f"«{args[0]}» — не число хвилин"
        if not 1 <= mins <= 10080:
            return "Від 1 хвилини до тижня"
        rt["muted_until"] = time.time() + mins * 60
        return f"🔇 тиша на {mins} хв"

    if cmd == "/unmute":
        rt["muted_until"] = 0
        return "🔔 сповіщення увімкнено"

    return f"Не знаю команди {cmd}\n\n{HELP}"


def pump_commands(a, rt, picked, last, cursor, wait=0):
    """Забирає й обробляє команди. Повертає новий offset."""
    for u in tg_updates(a.tg_token, cursor, wait):
        cursor = u["update_id"] + 1
        msg = u.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = msg.get("text") or ""
        if chat != str(a.tg_chat):
            continue                       # чужим не даємо крутити налаштування
        reply = handle_cmd(text, rt, picked, last)
        if reply:
            tg_send(a.tg_token, a.tg_chat, reply)
            print(f"{DIM}  ← {text.strip()}{OFF}")
    return cursor


def main():
    p = argparse.ArgumentParser(description="Спред оригінал (Binance) vs обгортка (Base)")
    p.add_argument("--assets", default=",".join(ASSETS),
                   help="через кому: " + ",".join(ASSETS))
    p.add_argument("--size", type=float, default=1000, help="розмір угоди в $ (типово 1000)")
    p.add_argument("--threshold", type=float,
                   help="єдиний поріг на всі активи; без нього — свій на кожен "
                        + ", ".join(f"{k} {v['threshold']}%" for k, v in ASSETS.items()))
    p.add_argument("--interval", type=int, default=30, help="пауза між тіками, с (типово 30)")
    p.add_argument("--csv", default="spread_log.csv", help="файл логу")
    p.add_argument("--webhook", help="URL для POST на алерті")
    p.add_argument("--once", action="store_true", help="один замір і вихід")
    p.add_argument("--tg-token", default=os.environ.get("TG_BOT_TOKEN"),
                   help="токен бота (або TG_BOT_TOKEN)")
    p.add_argument("--tg-chat", default=os.environ.get("TG_CHAT_ID"),
                   help="chat id (або TG_CHAT_ID)")
    p.add_argument("--tg-cooldown", type=int, default=15,
                   help="хв між повторами, поки спред тримається (типово 15)")
    p.add_argument("--tg-setup", action="store_true", help="показати доступні chat id і вийти")
    p.add_argument("--tg-test", action="store_true", help="надіслати тестове і вийти")
    a = p.parse_args()

    if a.tg_setup:
        if not a.tg_token:
            print(f"{RED}потрібен --tg-token{OFF}", file=sys.stderr)
            return 1
        return tg_setup(a.tg_token)

    picked = [s.strip().upper() for s in a.assets.split(",") if s.strip()]
    unknown = [s for s in picked if s not in ASSETS]
    if unknown:
        print(f"{RED}невідомі активи: {', '.join(unknown)}{OFF}", file=sys.stderr)
        return 1
    # пріоритет: --threshold > збережене з бота > типове для активу
    saved = load_settings()
    thr = {}
    for n in picked:
        if a.threshold is not None:
            thr[n] = a.threshold
        else:
            thr[n] = float(saved.get("thresholds", {}).get(n, ASSETS[n]["threshold"]))
    rt = {"thr": thr, "size": float(saved.get("size", a.size)), "muted_until": 0.0}
    if a.size != p.get_default("size"):
        rt["size"] = a.size          # явний --size б'є збережене

    tg_on = bool(a.tg_token and a.tg_chat)
    if (a.tg_token or a.tg_chat) and not tg_on:
        print(f"{YEL}Telegram вимкнено: вказано лише одне з --tg-token / --tg-chat{OFF}",
              file=sys.stderr)

    if a.tg_test:
        if not tg_on:
            print(f"{RED}потрібні --tg-token і --tg-chat{OFF}", file=sys.stderr)
            return 1
        ok = tg_send(a.tg_token, a.tg_chat,
                     "✅ <b>Трекер спреду підключено</b>\n<code>"
                     + " · ".join(f"{n} {thr[n]}%" for n in picked)
                     + f" · розмір ${rt['size']:,.0f}</code>\n\n" + HELP)
        print("надіслано" if ok else f"{RED}не пройшло{OFF}")
        return 0 if ok else 1

    signal.signal(signal.SIGINT, lambda *_: (print(f"\n{DIM}зупинено{OFF}"), sys.exit(0)))

    print(f"{BLD}Оригінал (Binance) vs обгортка Universal (Base){OFF}")
    print(f"{DIM}" + " · ".join(f"{n} поріг {thr[n]}%" for n in picked)
          + f" · розмір ${rt['size']:,.0f} · кожні {a.interval} с · лог {a.csv}"
          + (" · telegram" if tg_on else "") + f"{OFF}\n")

    last = {}      # останній замір по кожному активу — для /status
    cursor = 0     # offset getUpdates
    if tg_on:
        cursor = pump_commands(a, rt, picked, last, cursor)   # з'їсти старі команди
        tg_send(a.tg_token, a.tg_chat,
                "▶️ <b>Трекер запущено</b>\n<code>"
                + " · ".join(f"{n} {rt['thr'][n]}%" for n in picked)
                + f" · ${rt['size']:,.0f}</code>")

    # Спред тримається вище порогу годинами — слати на кожному тіку нема сенсу.
    # Шлемо на вході в зону, далі не частіше за cooldown, і один раз на виході.
    state = {n: {"in_zone": False, "last_ping": 0.0, "peak": 0.0} for n in picked}
    fails = 0

    while True:
        for name in picked:
            try:
                m = measure(name, ASSETS[name], rt["size"])
                fails = 0
                last[name] = m
            except FetchError as e:
                fails += 1
                print(f"{DIM}{datetime.now().strftime('%H:%M:%S')}{OFF}  "
                      f"{BLD}{name:<3}{OFF}  {RED}{e}{OFF}", file=sys.stderr)
                if fails >= 5:
                    print(f"{RED}5 збоїв поспіль — пауза 5 хв{OFF}", file=sys.stderr)
                    time.sleep(300)
                    fails = 0
                continue

            t = rt["thr"][name]
            hit = abs(m["arb"]) >= t
            print(render(m, t, hit))
            write_csv(a.csv, m, t, hit)

            if hit:
                sys.stdout.write("\a")
                sys.stdout.flush()
                if a.webhook:
                    notify(a.webhook, m, t)

            if tg_on and rt["muted_until"] <= time.time():
                ev = alert_step(state[name], m["arb"], t,
                                a.tg_cooldown * 60, time.time())
                if ev in ("open", "repeat"):
                    tg_send(a.tg_token, a.tg_chat, tg_alert_text(m, t))
                elif ev == "close":
                    tg_send(a.tg_token, a.tg_chat,
                            tg_close_text(name, m["arb"], t, state[name]["peak"]))

            time.sleep(0.3)  # між активами, щоб не впертися в ліміти

        if a.once:
            return 0

        # Пауза шматками, щоб команди з бота відпрацьовували за секунди,
        # а не чекали кінця інтервалу.
        deadline = time.time() + a.interval
        while time.time() < deadline:
            left = deadline - time.time()
            if not tg_on:
                time.sleep(min(3, max(0.2, left)))
                continue
            started = time.time()
            cursor = pump_commands(a, rt, picked, last, cursor,
                                   wait=int(min(20, max(1, left))))
            # якщо мережа впала, довгий пул повернеться миттєво — не крутити
            # цикл на повній швидкості
            if time.time() - started < 0.5:
                time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main() or 0)
