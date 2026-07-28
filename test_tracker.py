#!/usr/bin/env python3
"""Тести трекера. Без залежностей і без мережі — усі HTTP-виклики підмінені.

    python test_tracker.py
"""

import csv
import io
import json
import os
import sys
import tempfile
import traceback
import urllib.error

sys.argv = ["test"]                      # щоб argparse у модулі не вдавився
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spread_tracker as T               # noqa: E402

# Тести пишуть налаштування на диск. Робимо це в тимчасовій теці, інакше
# вони затирають бойовий tracker_settings.json — на цьому вже попалися.
os.chdir(tempfile.mkdtemp(prefix="tracker-test-"))

PASS, FAIL = 0, []


def check(name, fn):
    global PASS
    try:
        fn()
        PASS += 1
        print(f"  \033[32mok\033[0m   {name}")
    except AssertionError as e:
        FAIL.append((name, str(e) or "assert"))
        print(f"  \033[31mFAIL\033[0m {name}: {e}")
    except Exception:
        FAIL.append((name, traceback.format_exc(limit=2)))
        print(f"  \033[31mERR\033[0m  {name}\n{traceback.format_exc(limit=2)}")


def section(t):
    print(f"\n\033[1m{t}\033[0m")


# ---------------------------------------------------------------- alert_step

def new_state():
    return {"in_zone": False, "last_ping": 0.0, "peak": 0.0}


section("Логіка сповіщень (alert_step)")

def t_quiet():
    s = new_state()
    assert T.alert_step(s, 0.3, 0.8, 900, 1000) is None
    assert not s["in_zone"]
check("під порогом — мовчить", t_quiet)

def t_open():
    s = new_state()
    assert T.alert_step(s, 0.9, 0.8, 900, 1000) == "open"
    assert s["in_zone"] and s["peak"] == 0.9 and s["last_ping"] == 1000
check("перетин порогу — 'open'", t_open)

def t_cooldown():
    s = new_state()
    T.alert_step(s, 0.9, 0.8, 900, 1000)
    assert T.alert_step(s, 1.0, 0.8, 900, 1100) is None, "мусить мовчати в cooldown"
    assert T.alert_step(s, 1.0, 0.8, 900, 1400) is None
check("у зоні до cooldown — тиша", t_cooldown)

def t_repeat():
    s = new_state()
    T.alert_step(s, 0.9, 0.8, 900, 1000)
    assert T.alert_step(s, 1.1, 0.8, 900, 1900) == "repeat"
    assert s["last_ping"] == 1900, "таймер має перезапуститись"
check("після cooldown — 'repeat'", t_repeat)

def t_close():
    s = new_state()
    T.alert_step(s, 0.9, 0.8, 900, 1000)
    assert T.alert_step(s, 0.2, 0.8, 900, 1100) == "close"
    assert not s["in_zone"]
    assert T.alert_step(s, 0.2, 0.8, 900, 1200) is None, "друге закриття не шлеться"
check("вихід із зони — 'close', і лише раз", t_close)

def t_peak():
    s = new_state()
    for arb, now in ((0.9, 1000), (1.5, 1030), (1.1, 1060)):
        T.alert_step(s, arb, 0.8, 900, now)
    assert s["peak"] == 1.5, f"пік {s['peak']}"
check("пік тримається максимальний", t_peak)

def t_peak_negative():
    s = new_state()
    T.alert_step(s, -0.9, 0.8, 900, 1000)
    T.alert_step(s, -1.7, 0.8, 900, 1030)
    T.alert_step(s, -1.0, 0.8, 900, 1060)
    assert s["peak"] == -1.7, f"пік {s['peak']} — має братись за модулем"
check("від'ємний арбітраж теж ловиться", t_peak_negative)

def t_reopen():
    s = new_state()
    T.alert_step(s, 2.0, 0.8, 900, 1000)
    T.alert_step(s, 0.1, 0.8, 900, 1100)          # close
    assert T.alert_step(s, 0.9, 0.8, 900, 1200) == "open", "нова зона = новий 'open'"
    assert s["peak"] == 0.9, "пік має скинутись на новій зоні"
check("нова зона скидає пік", t_reopen)

def t_boundary():
    s = new_state()
    assert T.alert_step(s, 0.8, 0.8, 900, 1000) == "open", "рівно поріг = спрацювання"
check("рівність порогу спрацьовує", t_boundary)


# ---------------------------------------------------------------- команди

section("Команди бота (handle_cmd)")

def rt():
    return {"thr": {"SOL": 0.8, "XRP": 1.2}, "size": 1000.0, "muted_until": 0.0}

PICKED = ["SOL", "XRP"]
LAST = {"SOL": {"arb": 0.35, "spot_ask": 74.3}, "XRP": {"arb": 1.31, "spot_ask": 1.065}}

def t_help():
    assert "/set" in T.handle_cmd("/help", rt(), PICKED, LAST)
    assert T.handle_cmd("/start", rt(), PICKED, LAST) == T.HELP
check("/help і /start", t_help)

def t_status():
    out = T.handle_cmd("/status", rt(), PICKED, LAST)
    assert "0.8" in out and "1.2" in out and "+0.35%" in out
    assert "🟢" in out, "XRP вище свого порогу — має бути позначка"
check("/status показує пороги і останній замір", t_status)

def t_set():
    r = rt()
    T.handle_cmd("/set XRP 1.5", r, PICKED, LAST)
    assert r["thr"]["XRP"] == 1.5
check("/set міняє поріг", t_set)

def t_set_aliases():
    for cmd, exp in (("/set uXRP 2", 2.0), ("/set xrp 1,8", 1.8),
                     ("/set XRP 0.9%", 0.9), ("/set@Bot XRP 1.1", 1.1)):
        r = rt()
        T.handle_cmd(cmd, r, PICKED, LAST)
        assert r["thr"]["XRP"] == exp, f"{cmd} -> {r['thr']['XRP']}, чекав {exp}"
check("/set приймає uXRP, кому, %, @ім'я_бота", t_set_aliases)

def t_set_bad():
    r = rt()
    for cmd in ("/set DOGE 1", "/set XRP abc", "/set XRP 99", "/set XRP 0.001", "/set XRP"):
        out = T.handle_cmd(cmd, r, PICKED, LAST)
        assert out and "✅" not in out, f"{cmd} мало б відхилитись, а дало: {out}"
    assert r["thr"] == {"SOL": 0.8, "XRP": 1.2}, "стан не мав змінитись"
check("/set відхиляє сміття і не псує стан", t_set_bad)

def t_size():
    r = rt()
    T.handle_cmd("/size $2,500", r, PICKED, LAST)
    assert r["size"] == 2500.0
    T.handle_cmd("/size 0", r, PICKED, LAST)
    assert r["size"] == 2500.0, "нуль мав бути відхилений"
check("/size парсить $ і коми, відхиляє нуль", t_size)

def t_mute():
    r = rt()
    T.handle_cmd("/mute 30", r, PICKED, LAST)
    import time as _t
    assert 29 * 60 < r["muted_until"] - _t.time() <= 30 * 60
    T.handle_cmd("/unmute", r, PICKED, LAST)
    assert r["muted_until"] == 0
check("/mute і /unmute", t_mute)

def t_noise():
    assert T.handle_cmd("просто текст", rt(), PICKED, LAST) is None
    assert T.handle_cmd("", rt(), PICKED, LAST) is None
    assert "Не знаю" in T.handle_cmd("/bogus", rt(), PICKED, LAST)
check("не-команди ігноруються", t_noise)


# ---------------------------------------------------------------- сховище

section("Збереження налаштувань")

def t_settings_roundtrip():
    d = tempfile.mkdtemp()
    cwd = os.getcwd()
    try:
        os.chdir(d)
        r = {"thr": {"SOL": 1.1, "XRP": 2.2}, "size": 7500.0}
        T.save_settings(r)
        back = T.load_settings()
        assert back["thresholds"] == {"SOL": 1.1, "XRP": 2.2}, back
        assert back["size"] == 7500.0
    finally:
        os.chdir(cwd)
check("save -> load повертає те саме", t_settings_roundtrip)

def t_settings_missing():
    d = tempfile.mkdtemp()
    cwd = os.getcwd()
    try:
        os.chdir(d)
        assert T.load_settings() == {}, "відсутній файл = порожньо, без падіння"
        with open(T.SETTINGS_FILE, "w") as f:
            f.write("{битий джейсон")
        assert T.load_settings() == {}, "битий файл теж не має валити"
    finally:
        os.chdir(cwd)
check("відсутній / битий файл не валить", t_settings_missing)


# ---------------------------------------------------------------- математика

section("Розрахунок спреду (measure)")

class FakeHTTP:
    """Підміна fetch_json: віддає заготовлені відповіді за фрагментом URL."""
    def __init__(self, bid, ask, out_wrapped, out_usdc, gas=0.01):
        self.bid, self.ask = bid, ask
        self.out_wrapped, self.out_usdc, self.gas = out_wrapped, out_usdc, gas
        self.calls = []

    def __call__(self, url, tries=3, timeout=20):
        self.calls.append(url)
        if "binance" in url:
            return {"bidPrice": str(self.bid), "askPrice": str(self.ask)}
        out = self.out_wrapped if f"tokenIn={T.USDC}" in url else self.out_usdc
        return {"code": 0, "data": {"routeSummary": {
            "amountOut": str(out), "gasUsd": str(self.gas), "l1FeeUsd": "0"}}}


def with_http(fake, fn):
    orig = T.fetch_json
    T.fetch_json = fake
    try:
        return fn()
    finally:
        T.fetch_json = orig


def t_math():
    # SOL по 100.00; за $1000 дають 9.8 обгортки (тобто 102.04 за штуку),
    # а продаж 10 штук повертає 1010 USDC (101.00 за штуку).
    fake = FakeHTTP(bid=99.99, ask=100.0,
                    out_wrapped=int(9.8 * 1e18), out_usdc=int(1010 * 1e6), gas=0.5)
    cfg = {"token": "0xWRAP", "decimals": 18, "binance": "SOLUSDT", "threshold": 0.8}
    m = with_http(fake, lambda: T.measure("SOL", cfg, 1000.0))

    assert abs(m["wrap_buy"] - 1000 / 9.8) < 1e-9, m["wrap_buy"]
    assert abs(m["wrap_sell"] - 101.0) < 1e-9, m["wrap_sell"]
    # premium_buy проти ask
    assert abs(m["premium_buy"] - ((1000 / 9.8) / 100.0 - 1) * 100) < 1e-9
    # premium_sell проти bid
    assert abs(m["premium_sell"] - (101.0 / 99.99 - 1) * 100) < 1e-9
    # arb: 10 штук * 101.00 - 0.5 газу = 1009.5 з $1000 -> +0.95%
    assert abs(m["arb"] - 0.95) < 1e-9, m["arb"]
    assert m["gas_usd"] == 1.0, "газ обох ніг сумується"
check("арифметика buy/sell/arb сходиться до копійки", t_math)

def t_math_decimals():
    """XRP ~$1 — перевіряємо, що 6 vs 18 decimals не з'їхали."""
    fake = FakeHTTP(bid=1.0, ask=1.0,
                    out_wrapped=int(990 * 1e18), out_usdc=int(1000 * 1e6), gas=0)
    cfg = {"token": "0xWRAP", "decimals": 18, "binance": "XRPUSDT", "threshold": 1.2}
    m = with_http(fake, lambda: T.measure("XRP", cfg, 1000.0))
    assert abs(m["wrap_buy"] - 1000 / 990) < 1e-12
    assert abs(m["wrap_sell"] - 1.0) < 1e-12
    assert abs(m["arb"]) < 1e-9, m["arb"]
check("розрядність токенів не плутається", t_math_decimals)

def t_no_route():
    def bad(url, tries=3, timeout=20):
        if "binance" in url:
            return {"bidPrice": "1", "askPrice": "1"}
        return {"code": 4008, "message": "route not found"}
    cfg = {"token": "0xW", "decimals": 18, "binance": "XRPUSDT", "threshold": 1.2}
    try:
        with_http(bad, lambda: T.measure("XRP", cfg, 1000.0))
        raise AssertionError("мав кинути FetchError")
    except T.FetchError as e:
        assert "route not found" in str(e), str(e)
check("відсутній маршрут -> FetchError з причиною", t_no_route)


# ---------------------------------------------------------------- мережа

section("Стійкість мережевого шару")

def t_retry():
    calls = {"n": 0}
    real_open = T.urllib.request.urlopen

    def flaky(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("тимчасово недоступно")
        return io.BytesIO(b'{"ok": true}')

    T.urllib.request.urlopen = flaky
    try:
        assert T.fetch_json("https://x/y") == {"ok": True}
        assert calls["n"] == 3, f"мало бути 3 спроби, було {calls['n']}"
    finally:
        T.urllib.request.urlopen = real_open
check("тимчасовий збій переживається ретраями", t_retry)

def t_give_up():
    real_open = T.urllib.request.urlopen

    def dead(req, timeout=None):
        raise urllib.error.URLError("мертво")

    T.urllib.request.urlopen = dead
    try:
        try:
            T.fetch_json("https://x/y", tries=2)
            raise AssertionError("мав кинути FetchError")
        except T.FetchError:
            pass
    finally:
        T.urllib.request.urlopen = real_open
check("після вичерпання спроб -> FetchError", t_give_up)

def t_tg_send_survives():
    real_open = T.urllib.request.urlopen

    def dead(req, timeout=None):
        raise urllib.error.URLError("телеграм лежить")

    T.urllib.request.urlopen = dead
    err = sys.stderr
    sys.stderr = io.StringIO()
    try:
        assert T.tg_send("t", "c", "текст") is False, "має повернути False, а не впасти"
    finally:
        T.urllib.request.urlopen = real_open
        sys.stderr = err
check("падіння Telegram не валить трекер", t_tg_send_survives)


# ---------------------------------------------------------------- вивід

section("Лог і форматування")

def t_csv():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "log.csv")
    m = {"ts": "2026-07-28T18:00:00+00:00", "asset": "XRP", "spot_bid": 1.06,
         "spot_ask": 1.0601, "wrap_buy": 1.07, "wrap_sell": 1.065,
         "premium_buy": 0.94, "premium_sell": 0.47, "arb": 0.45,
         "gas_usd": 0.01, "size_usd": 1000.0}
    T.write_csv(path, m, 1.2, False)
    T.write_csv(path, m, 1.2, True)
    rows = list(csv.reader(open(path, encoding="utf-8")))
    assert rows[0][0] == "ts" and rows[0][1] == "asset"
    assert len(rows) == 3, f"заголовок + 2 рядки, а не {len(rows)}"
    assert rows[1][-1] == "0" and rows[2][-1] == "1", "колонка alert"
    assert rows[1][-2] == "1.2", "поріг пишеться в лог"
check("CSV: заголовок один раз, рядки дописуються", t_csv)

def t_digits():
    assert T.digits(1.06) == 4, "дешевий токен — 4 знаки"
    assert T.digits(74.3) == 3, "дорогий — 3"
check("точність підбирається під ціну", t_digits)

def t_render():
    m = {"ts": "2026-07-28T18:00:00+00:00", "asset": "SOL", "spot_bid": 73.85,
         "spot_ask": 73.86, "wrap_buy": 74.44, "wrap_sell": 74.33,
         "premium_buy": 0.79, "premium_sell": 0.66, "arb": 0.64,
         "gas_usd": 0.01, "size_usd": 1000.0}
    out = T.render(m, 0.8, False)
    assert "18:00:00" in out and "SOL" in out and "+0.64%" in out
    assert "<<<" not in out
    assert "<<< 0.8%" in T.render(m, 0.8, True)
check("рядок логу містить час, актив, арбітраж і мітку", t_render)

def t_alert_text():
    m = {"ts": "2026-07-28T18:00:00+00:00", "asset": "XRP", "spot_bid": 1.06,
         "spot_ask": 1.0601, "wrap_buy": 1.07, "wrap_sell": 1.065,
         "premium_buy": 0.94, "premium_sell": 0.47, "arb": 1.35,
         "gas_usd": 0.01, "size_usd": 5000.0}
    out = T.tg_alert_text(m, 1.2)
    assert "🟢" in out and "XRP arb +1.35%" in out and "$5,000" in out
    assert out.count("<b>") == out.count("</b>"), "теги HTML мають бути парні"
    m["arb"] = -1.35
    assert "🔴" in T.tg_alert_text(m, 1.2), "мінус має бути червоним"
    close = T.tg_close_text("XRP", 0.2, 1.2, 1.9)
    assert "+1.90%" in close and close.count("<b>") == close.count("</b>")
check("повідомлення в Telegram коректні й теги парні", t_alert_text)


# ---------------------------------------------------------------- конфіг

section("Реєстр активів")

def t_assets():
    for name, c in T.ASSETS.items():
        assert c["token"].startswith("0x") and len(c["token"]) == 42, name
        assert c["binance"].endswith("USDT"), name
        assert 0.05 <= c["threshold"] <= 50, name
        assert c["decimals"] == 18, f"{name}: обгортки Universal на EVM — 18 знаків"
    assert len({c["token"].lower() for c in T.ASSETS.values()}) == len(T.ASSETS), \
        "адреси не мають повторюватись"
check("адреси, символи і пороги валідні", t_assets)


# ---------------------------------------------------------------- підсумок

print()
if FAIL:
    print(f"\033[31m{len(FAIL)} провалено\033[0m, {PASS} пройдено")
    for n, e in FAIL:
        print(f"  - {n}: {e}")
    sys.exit(1)
print(f"\033[32mвсі {PASS} тестів пройдено\033[0m")
