"""
SMC Trading Bot — Smart Money Concepts
Exchange: BingX Futuros Perpetuos
Pares: 9 simultáneos (LONG + SHORT)
Apalancamiento: x10 (configurable)
Servidor: Railway 24/7
"""

import os, time, logging, requests, hmac, hashlib, json, threading
import pandas as pd
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from anthropic import Anthropic
from flask import Flask, jsonify, send_from_directory

# ─── CONFIG ───────────────────────────────────────────────────────────────────

BINGX_API_KEY     = os.getenv("BINGX_API_KEY")
BINGX_SECRET      = os.getenv("BINGX_SECRET")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

PARES = [
    "SOL-USDT",   # SOL — más rentable históricamente
    "ETH-USDT",   # ETH — liquidez institucional
    "BTC-USDT",   # BTC — mueve el mercado
    "BNB-USDT",   # BNB — más consistente
    "AVAX-USDT",  # AVAX — alta volatilidad
    "LINK-USDT",  # LINK — muy técnico
    "ARB-USDT",   # ARB — Layer 2
    "OP-USDT",    # OP — complementa ARB
    "INJ-USDT",   # INJ — alta volatilidad
]

CAPITAL_TOTAL  = float(os.getenv("CAPITAL_TOTAL", "100"))
APALANCAMIENTO = int(os.getenv("APALANCAMIENTO", "10"))
TP_PCT         = 0.14
SL_PCT         = 0.02
MAX_POSICIONES = 2
CB_LIMITE      = 2   # circuit breaker: N pérdidas seguidas
BASE_URL       = "https://open-api.bingx.com"

# ─── LOGGING ──────────────────────────────────────────────────────────────────

os.makedirs("logs", exist_ok=True)
log = logging.getLogger("smc_bot")
log.setLevel(logging.INFO)
fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

ch = logging.StreamHandler()
ch.setFormatter(fmt)
log.addHandler(ch)

fh = TimedRotatingFileHandler("logs/bot.log", when="midnight", backupCount=7)
fh.setFormatter(fmt)
log.addHandler(fh)

ai = Anthropic(api_key=ANTHROPIC_API_KEY)

estado = {
    "posiciones":        [],
    "perdidas_seguidas": 0,
    "circuit_breaker":   False,
    "ops_total":         0,
    "ops_ganadas":       0,
    "capital":           CAPITAL_TOTAL,
    "capital_inicial":   CAPITAL_TOTAL,
    "apalancamiento":    APALANCAMIENTO,
    "pares_activos":     list(PARES),
}
lock = threading.Lock()

MARGEN_POR_PAR = CAPITAL_TOTAL / len(PARES)

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

def tg(msg: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        log.error(f"Telegram send: {e}")

def telegram_polling():
    """Hilo que escucha comandos Telegram vía long-polling."""
    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset:
                params["offset"] = offset
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params=params, timeout=35
            )
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                msg    = u.get("message", {})
                texto  = msg.get("text", "").strip()
                cid    = str(msg.get("chat", {}).get("id", ""))
                if cid != str(TELEGRAM_CHAT_ID):
                    continue
                manejar_comando(texto)
        except Exception as e:
            log.error(f"Telegram polling: {e}")
            time.sleep(5)

def manejar_comando(texto: str):
    if texto == "/reactivar":
        with lock:
            estado["circuit_breaker"]   = False
            estado["perdidas_seguidas"] = 0
        tg("✅ <b>Bot reactivado.</b> Circuit breaker reseteado. Reanudando operaciones.")
        log.info("Bot reactivado por Telegram")

    elif texto == "/estado":
        _enviar_reporte()

    elif texto == "/pausar":
        with lock:
            estado["circuit_breaker"] = True
        tg("⏸ <b>Bot pausado manualmente.</b> Usa /reactivar para continuar.")
        log.info("Bot pausado por Telegram")

    elif texto == "/capital":
        with lock:
            cap = estado["capital"]
            ops_t = estado["ops_total"]
            ops_g = estado["ops_ganadas"]
            lev   = estado["apalancamiento"]
        wr = ops_g / ops_t * 100 if ops_t else 0
        tg(f"💼 <b>Capital actual:</b> ${cap:.2f} USDT\n"
           f"🎯 Win Rate: {wr:.0f}% ({ops_g}/{ops_t})\n"
           f"⚡ Apalancamiento: x{lev}")

# ─── BINGX API ────────────────────────────────────────────────────────────────

def bx_sign(params: dict) -> str:
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(BINGX_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def bx_get(endpoint: str, params: dict = None, auth: bool = False) -> dict:
    params = dict(params or {})
    if auth:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = bx_sign(params)
    for intento in range(4):
        try:
            r = requests.get(
                f"{BASE_URL}{endpoint}",
                params=params,
                headers={"X-BX-APIKEY": BINGX_API_KEY},
                timeout=10
            )
            if r.status_code == 429:
                log.warning("BingX rate limit 429 — esperando 60s")
                time.sleep(60)
                continue
            data = r.json()
            if data.get("code") == 0:
                return data
            log.error(f"BingX GET {endpoint}: {data.get('code')} {data.get('msg')}")
            return {}
        except requests.exceptions.ConnectionError:
            log.error(f"Sin conexión (intento {intento+1}) — reintentando en 30s")
            time.sleep(30)
        except Exception as e:
            log.error(f"BingX GET {endpoint}: {e}")
            return {}
    return {}

def bx_post(endpoint: str, params: dict) -> dict:
    params = dict(params)
    for intento in range(4):
        try:
            p = dict(params)
            p["timestamp"] = int(time.time() * 1000)
            p["signature"] = bx_sign(p)
            r = requests.post(
                f"{BASE_URL}{endpoint}",
                params=p,
                headers={"X-BX-APIKEY": BINGX_API_KEY,
                         "Content-Type": "application/json"},
                timeout=10
            )
            if r.status_code == 429:
                log.warning("BingX rate limit 429 — esperando 60s")
                time.sleep(60)
                continue
            data = r.json()
            if data.get("code") == 0:
                return data
            msg = data.get("msg", "")
            if any(w in msg.lower() for w in ["insufficient", "balance", "margin"]):
                tg(f"⚠️ Sin fondos suficientes. Saltando par.")
                return {"error": "insufficient_funds"}
            log.error(f"BingX POST {endpoint}: {data.get('code')} {msg}")
            return {}
        except requests.exceptions.ConnectionError:
            log.error(f"Sin conexión (intento {intento+1}) — reintentando en 30s")
            time.sleep(30)
        except Exception as e:
            log.error(f"BingX POST {endpoint}: {e}")
            return {}
    return {}

def velas(simbolo: str, intervalo: str, limit: int = 200) -> pd.DataFrame:
    """intervalo: '1d', '4h', '1h'"""
    d = bx_get("/openApi/swap/v2/quote/klines",
               {"symbol": simbolo, "interval": intervalo, "limit": limit})
    if not d.get("data"):
        return pd.DataFrame()
    try:
        df = pd.DataFrame(d["data"])
        df = df.rename(columns={"time": "ts"})
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        return df.sort_values("ts").tail(limit).reset_index(drop=True)
    except Exception as e:
        log.error(f"Velas {simbolo}: {e}")
        return pd.DataFrame()

def precio(simbolo: str) -> float:
    d = bx_get("/openApi/swap/v2/quote/ticker", {"symbol": simbolo})
    try:
        return float(d["data"]["lastPrice"])
    except:
        return 0.0

def set_leverage(simbolo: str, lev: int):
    for side in ("LONG", "SHORT"):
        bx_post("/openApi/swap/v2/trade/leverage",
                {"symbol": simbolo, "side": side, "leverage": lev})

def ejecutar_orden(simbolo: str, lado: str, cantidad: float, sl: float, tp: float) -> bool:
    """lado: 'BUY'=LONG, 'SELL'=SHORT"""
    lev      = estado["apalancamiento"]
    pos_side = "LONG" if lado == "BUY" else "SHORT"
    close_s  = "SELL" if lado == "BUY" else "BUY"

    set_leverage(simbolo, lev)

    r = bx_post("/openApi/swap/v2/trade/order", {
        "symbol": simbolo, "side": lado,
        "positionSide": pos_side, "type": "MARKET",
        "quantity": str(cantidad)
    })
    if r.get("error") == "insufficient_funds":
        return False

    # Stop Loss
    bx_post("/openApi/swap/v2/trade/order", {
        "symbol": simbolo, "side": close_s,
        "positionSide": pos_side, "type": "STOP_MARKET",
        "quantity": str(cantidad), "stopPrice": str(sl),
        "workingType": "MARK_PRICE"
    })
    # Take Profit
    bx_post("/openApi/swap/v2/trade/order", {
        "symbol": simbolo, "side": close_s,
        "positionSide": pos_side, "type": "TAKE_PROFIT_MARKET",
        "quantity": str(cantidad), "stopPrice": str(tp),
        "workingType": "MARK_PRICE"
    })
    return True

def balance_bingx() -> float:
    d = bx_get("/openApi/swap/v2/user/balance", {}, auth=True)
    try:
        return float(d["data"]["balance"]["balance"])
    except:
        return 0.0

# ─── GESTIÓN CAPITAL DINÁMICA ─────────────────────────────────────────────────

def recalcular_capital():
    global MARGEN_POR_PAR
    n = max(len(estado["pares_activos"]), 1)
    MARGEN_POR_PAR = estado["capital"] / n

    cap_ini = estado["capital_inicial"]
    caida   = (cap_ini - estado["capital"]) / cap_ini

    if caida >= 0.40:
        estado["circuit_breaker"] = True
        tg(f"🔴 <b>CIRCUIT BREAKER PERMANENTE</b>\n"
           f"Capital cayó {caida*100:.0f}% del inicial (${estado['capital']:.2f}).\n"
           f"Bot detenido. Revisa manualmente y usa /reactivar.")
        log.critical(f"Capital caído {caida*100:.0f}% — CB permanente")
    elif caida >= 0.20 and estado["apalancamiento"] > 10:
        estado["apalancamiento"] = 10
        tg(f"⚠️ Capital cayó {caida*100:.0f}%. Apalancamiento reducido a x10.")
        log.warning("Apalancamiento reducido a x10 por caída de capital")

# ─── HISTORIAL ────────────────────────────────────────────────────────────────

def guardar_historial(simbolo, dir_, entrada, salida, pnl, resultado, confianza_ia):
    try:
        path = "historial.json"
        hist = []
        if os.path.exists(path):
            with open(path, "r") as f:
                hist = json.load(f)
        hist.append({
            "timestamp":    datetime.now().isoformat(timespec="seconds"),
            "simbolo":      simbolo,
            "direccion":    dir_,
            "entrada":      round(entrada, 6),
            "salida":       round(salida, 6),
            "pnl":          round(pnl, 4),
            "resultado":    resultado,
            "confianza_ia": confianza_ia,
            "capital_post": round(estado["capital"], 2),
        })
        with open(path, "w") as f:
            json.dump(hist, f, indent=2)
    except Exception as e:
        log.error(f"Historial: {e}")

# ─── SMC ──────────────────────────────────────────────────────────────────────

def tendencia(df: pd.DataFrame) -> str:
    if len(df) < 20: return "lateral"
    h, l = df["high"].values, df["low"].values
    if h[-1] > h[-4] and l[-1] > l[-4]: return "alcista"
    if h[-1] < h[-4] and l[-1] < l[-4]: return "bajista"
    return "lateral"

def hay_bos(df: pd.DataFrame, t: str) -> bool:
    if len(df) < 20: return False
    u   = df.tail(20)
    pc  = u["close"].iloc[-1]
    vol = u["volume"].iloc[-1]
    vma = u["volume"].mean()
    if t == "alcista": return pc > u["high"].iloc[:-3].max() and vol > vma * 1.3
    if t == "bajista": return pc < u["low"].iloc[:-3].min()  and vol > vma * 1.3
    return False

def buscar_ob(df: pd.DataFrame, t: str) -> dict:
    empty = {"zona_alta": 0, "zona_baja": 0, "valido": False}
    if len(df) < 30: return empty
    for i in range(len(df) - 5, max(len(df) - 25, 0), -1):
        v, s = df.iloc[i], df.iloc[i+1]
        if t == "alcista" and v["close"] < v["open"] and (s["close"]-s["open"]) > s["open"]*0.004:
            return {"zona_alta": v["open"], "zona_baja": v["close"], "valido": True}
        if t == "bajista" and v["close"] > v["open"] and (v["open"]-s["close"]) > s["open"]*0.004:
            return {"zona_alta": v["close"], "zona_baja": v["open"], "valido": True}
    return empty

def en_ob(pc: float, ob: dict) -> bool:
    if not ob["valido"]: return False
    m = (ob["zona_alta"] - ob["zona_baja"]) * 0.2
    return (ob["zona_baja"] - m) <= pc <= (ob["zona_alta"] + m)

def contar_toques(df: pd.DataFrame, ob: dict, t: str) -> int:
    if not ob["valido"]: return 0
    toques = 0
    zb, za = ob["zona_baja"] * 0.985, ob["zona_alta"] * 1.015
    u = df.tail(40).reset_index(drop=True)
    i = 0
    while i < len(u) - 1:
        v, s = u.iloc[i], u.iloc[i+1]
        if t == "alcista" and zb <= v["low"] <= za and s["close"] > s["open"]:
            toques += 1; i += 2; continue
        if t == "bajista" and zb <= v["high"] <= za and s["close"] < s["open"]:
            toques += 1; i += 2; continue
        i += 1
    return toques

def confirma_1h(df: pd.DataFrame, t: str) -> bool:
    if len(df) < 3: return False
    u, p = df.iloc[-1], df.iloc[-2]
    vol_ok = u["volume"] > df["volume"].tail(10).mean() * 1.1
    if t == "alcista":
        return u["close"] > u["open"] and u["close"] > p["open"] and u["open"] < p["close"] and vol_ok
    if t == "bajista":
        return u["close"] < u["open"] and u["close"] < p["open"] and u["open"] > p["close"] and vol_ok
    return False

# ─── FILTRO IA ────────────────────────────────────────────────────────────────

def filtro_ia(simbolo, t, pc, ob, toques) -> dict:
    for intento in range(3):
        try:
            r = ai.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=150,
                messages=[{"role": "user", "content": f"""Eres el filtro de riesgo de un bot SMC. Decide si entrar o no.

SEÑAL:
Par: {simbolo} | Fecha: {datetime.now().strftime('%Y-%m-%d %A')} | Mes: {datetime.now().month}
Tendencia Daily: {t} | Precio: ${pc:.4f}
Order Block: ${ob['zona_baja']:.4f} - ${ob['zona_alta']:.4f}
Toques trendline: {toques} | Dirección: {'LONG' if t == 'alcista' else 'SHORT'}

ANALIZA:
1. ¿Mes {datetime.now().month} es históricamente favorable en crypto?
2. Ciclo post-halving abril 2024 (mes {((datetime.now() - datetime(2024,4,1)).days // 30)}) — ¿apoya esta dirección?
3. ¿El setup repite patrones de 2021, 2023, 2024?
4. ¿Hay riesgos macro relevantes esta semana?
5. ¿Hay razones claras para NO entrar?

RESPONDE EXACTAMENTE (sin texto extra):
DECISION: ENTRAR o NO_ENTRAR
CONFIANZA: 0-100
RAZON: una línea breve"""}]
            )
            texto = r.content[0].text.strip()
            dec, conf, razon = "NO_ENTRAR", 0, "Sin respuesta"
            for l in texto.split("\n"):
                if "DECISION:" in l: dec = "ENTRAR" if "ENTRAR" in l else "NO_ENTRAR"
                elif "CONFIANZA:" in l:
                    try: conf = int(l.split(":")[1].strip())
                    except: pass
                elif "RAZON:" in l: razon = l.split(":", 1)[1].strip()
            return {"entrar": dec == "ENTRAR" and conf >= 55, "confianza": conf, "razon": razon}
        except Exception as e:
            log.error(f"IA intento {intento+1}: {e}")
            if intento < 2:
                time.sleep(5)

    # Fallback: si IA falla 3 veces, entra solo si 5/5 técnico (ya validado antes de llamar)
    log.warning(f"{simbolo} IA no disponible — fallback técnico 5/5")
    return {"entrar": True, "confianza": 100, "razon": "Fallback técnico 5/5 (IA no disponible)"}

# ─── POSICIONES ───────────────────────────────────────────────────────────────

def abrir(simbolo, t, pc, ia):
    lev     = estado["apalancamiento"]
    lado    = "BUY" if t == "alcista" else "SELL"
    dir_    = "LONG" if lado == "BUY" else "SHORT"
    sl      = round(pc * (1 - SL_PCT) if lado == "BUY" else pc * (1 + SL_PCT), 6)
    tp      = round(pc * (1 + TP_PCT) if lado == "BUY" else pc * (1 - TP_PCT), 6)
    m       = MARGEN_POR_PAR
    g_pot   = m * lev * TP_PCT
    p_pot   = m * lev * SL_PCT
    cant    = round((m * lev) / pc, 4)

    ok = ejecutar_orden(simbolo, lado, cant, sl, tp)
    if not ok:
        return

    with lock:
        estado["posiciones"].append({
            "simbolo": simbolo, "dir": dir_, "entrada": pc,
            "sl": sl, "tp": tp, "margen": m,
            "g_pot": g_pot, "p_pot": p_pot,
            "confianza_ia": ia["confianza"],
            "ts": datetime.now().isoformat(),
        })
        estado["ops_total"] += 1

    e = "🟢" if lado == "BUY" else "🔴"
    tg(f"""{e} <b>NUEVA POSICIÓN — {simbolo}</b>

📍 {dir_} @ ${pc:.4f}
🛡 SL: ${sl:.4f} (-{SL_PCT*100:.0f}%)
🎯 TP: ${tp:.4f} (+{TP_PCT*100:.0f}%)
⚡ x{lev} | Margen: ${m:.2f} | Pos: ${m*lev:.2f}
📈 Potencial: +${g_pot:.2f} | 📉 Máx pérdida: -${p_pot:.2f}
🤖 IA {ia['confianza']}% — {ia['razon']}
🔒 AISLADO | Abiertas: {len(estado['posiciones'])}/{MAX_POSICIONES}""")

def _cerrar_posicion(p: dict, pc: float):
    """Cierra una posición y actualiza estado. Llama sin lock activo."""
    tp_ok = (p["dir"] == "LONG" and pc >= p["tp"]) or (p["dir"] == "SHORT" and pc <= p["tp"])
    sl_ok = (p["dir"] == "LONG" and pc <= p["sl"]) or (p["dir"] == "SHORT" and pc >= p["sl"])
    if not (tp_ok or sl_ok):
        return

    with lock:
        if p not in estado["posiciones"]:
            return  # ya fue cerrada por otro hilo
        estado["posiciones"].remove(p)
        pnl = p["g_pot"] if tp_ok else -p["p_pot"]
        estado["capital"] += pnl
        resultado = "TP" if tp_ok else "SL"
        if tp_ok:
            estado["ops_ganadas"] += 1
            estado["perdidas_seguidas"] = 0
        else:
            estado["perdidas_seguidas"] += 1
        ps = estado["perdidas_seguidas"]
        ops_t = estado["ops_total"]
        ops_g = estado["ops_ganadas"]
        cap   = estado["capital"]

    guardar_historial(p["simbolo"], p["dir"], p["entrada"], pc,
                      pnl, resultado, p.get("confianza_ia", 0))

    wr = ops_g / ops_t * 100 if ops_t else 0
    e  = "✅" if tp_ok else "❌"
    tg(f"""{e} <b>CERRADA — {p['simbolo']}</b>

{p['dir']} ${p['entrada']:.4f} → ${pc:.4f}
{'💚 +' if pnl > 0 else '🔴 '}{abs(pnl):.2f} USDT ({resultado})
💼 Capital: ${cap:.2f}
🎯 Win Rate: {wr:.0f}% ({ops_g}/{ops_t})""")

    recalcular_capital()

    if ps >= CB_LIMITE:
        with lock:
            estado["circuit_breaker"] = True
        tg(f"⚠️ <b>CIRCUIT BREAKER ACTIVO</b>\n"
           f"{CB_LIMITE} pérdidas seguidas. Bot pausado.\n"
           f"Envía /reactivar para continuar.")

def monitor_posiciones():
    """Hilo que verifica SL/TP de todas las posiciones cada 5 minutos."""
    while True:
        try:
            with lock:
                snapshot = list(estado["posiciones"])
            for p in snapshot:
                pc = precio(p["simbolo"])
                if pc:
                    _cerrar_posicion(p, pc)
                time.sleep(1)
        except Exception as e:
            log.error(f"Monitor posiciones: {e}")
        time.sleep(5 * 60)

# ─── ANÁLISIS PAR ─────────────────────────────────────────────────────────────

def analizar(simbolo: str):
    with lock:
        if estado["circuit_breaker"]: return
        if len(estado["posiciones"]) >= MAX_POSICIONES: return
        if any(p["simbolo"] == simbolo for p in estado["posiciones"]): return

    df_d  = velas(simbolo, "1d", 50)
    df_4h = velas(simbolo, "4h", 100)
    df_1h = velas(simbolo, "1h", 50)
    if df_d.empty or df_4h.empty or df_1h.empty:
        return

    pc = precio(simbolo)
    if not pc: return

    t = tendencia(df_d)
    if t == "lateral": return
    if not hay_bos(df_4h, t): return

    ob = buscar_ob(df_4h, t)
    if not ob["valido"] or not en_ob(pc, ob): return

    tk = contar_toques(df_4h, ob, t)
    if tk < 3: return
    if not confirma_1h(df_1h, t): return

    log.info(f"{simbolo} ✅ 5/5 — consultando IA...")
    ia = filtro_ia(simbolo, t, pc, ob, tk)

    if not ia["entrar"]:
        log.info(f"{simbolo} IA rechaza ({ia['confianza']}%): {ia['razon']}")
        return

    log.info(f"{simbolo} ✅ IA aprueba {ia['confianza']}% — ejecutando")
    abrir(simbolo, t, pc, ia)

# ─── REPORTE ──────────────────────────────────────────────────────────────────

def _enviar_reporte():
    with lock:
        cap     = estado["capital"]
        cap_ini = estado["capital_inicial"]
        ops_t   = estado["ops_total"]
        ops_g   = estado["ops_ganadas"]
        lev     = estado["apalancamiento"]
        cb      = estado["circuit_breaker"]
        pos     = list(estado["posiciones"])

    wr  = ops_g / ops_t * 100 if ops_t else 0
    g   = cap - cap_ini
    pct = g / cap_ini * 100
    pos_txt = "\n".join(
        f"  • {p['simbolo']} {p['dir']} @ ${p['entrada']:.4f}" for p in pos
    ) or "  Ninguna"
    tg(f"""📊 <b>REPORTE — {datetime.now().strftime('%d/%m/%Y %H:%M')}</b>

💼 Capital inicial: ${cap_ini:.2f}
💰 Capital actual:  ${cap:.2f}
{'📈' if g >= 0 else '📉'} Ganancia: {'+' if g >= 0 else ''}{g:.2f} ({'+' if pct >= 0 else ''}{pct:.1f}%)
🎯 Win Rate: {wr:.0f}% ({ops_g}/{ops_t} ops)
⚡ x{lev} | 🤖 CB: {'⚠️ ACTIVO' if cb else '✅ Normal'}

📍 Posiciones abiertas:
{pos_txt}

🏦 Exchange: BingX Futuros Perpetuos""")

# ─── VERIFICACIÓN INICIAL ─────────────────────────────────────────────────────

def verificar_inicio():
    errores = []

    # BingX API
    log.info("Verificando BingX API...")
    b = balance_bingx()
    if b == 0:
        errores.append("❌ BingX API: balance=0 (verifica BINGX_API_KEY y BINGX_SECRET)")
    else:
        log.info(f"BingX OK — Balance USDT: ${b:.2f}")

    # Anthropic API
    log.info("Verificando Anthropic API...")
    try:
        ai.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=5,
            messages=[{"role": "user", "content": "ok"}]
        )
        log.info("Anthropic OK")
    except Exception as e:
        errores.append(f"❌ Anthropic API: {e}")

    # Telegram
    log.info("Verificando Telegram...")
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=10
        )
        if r.json().get("ok"):
            log.info("Telegram OK")
        else:
            errores.append("❌ Telegram: token inválido")
    except Exception as e:
        errores.append(f"❌ Telegram: {e}")

    # Pares disponibles en BingX
    log.info("Verificando pares en BingX Futuros...")
    pares_ok = []
    for s in list(estado["pares_activos"]):
        pc = precio(s)
        if pc:
            pares_ok.append(s)
            log.info(f"  ✅ {s} — ${pc:.4f}")
        else:
            log.warning(f"  ❌ {s} no disponible — removido")
            tg(f"⚠️ Par {s} no disponible en BingX. Removido de la lista.")

    estado["pares_activos"] = pares_ok
    global MARGEN_POR_PAR
    MARGEN_POR_PAR = estado["capital"] / max(len(pares_ok), 1)

    if errores:
        msg = "🚨 <b>ERROR AL INICIAR — Bot detenido</b>\n\n" + "\n".join(errores)
        tg(msg)
        log.critical(f"Errores de inicio: {errores}")
        raise SystemExit(1)

    tg(f"""🤖 <b>SMC BOT BINGX — INICIADO</b>

📊 Pares: {len(pares_ok)} | 💰 Capital: ${estado['capital']:.2f} USDT
⚡ x{estado['apalancamiento']} | 🎯 TP: {TP_PCT*100:.0f}% | SL: {SL_PCT*100:.0f}%
🔄 Señales: cada 4h | 🔍 Monitor SL/TP: cada 5min
📱 Comandos: /estado /pausar /reactivar /capital

{', '.join(pares_ok)}

✅ Activo 24/7 en Railway""")

# ─── DASHBOARD API ────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder=".")

@app.route("/")
def index():
    return send_from_directory(".", "dashboard.html")

@app.route("/api/estado")
def api_estado():
    with lock:
        pos = list(estado["posiciones"])
        cap     = estado["capital"]
        cap_ini = estado["capital_inicial"]
        ops_t   = estado["ops_total"]
        ops_g   = estado["ops_ganadas"]
        lev     = estado["apalancamiento"]
        cb      = estado["circuit_breaker"]
        perdidas= estado["perdidas_seguidas"]
        pares   = list(estado["pares_activos"])

    wr  = round(ops_g / ops_t * 100, 1) if ops_t else 0
    g   = round(cap - cap_ini, 2)
    pct = round(g / cap_ini * 100, 2) if cap_ini else 0

    return jsonify({
        "capital":           round(cap, 2),
        "capital_inicial":   cap_ini,
        "ganancia":          g,
        "ganancia_pct":      pct,
        "win_rate":          wr,
        "ops_total":         ops_t,
        "ops_ganadas":       ops_g,
        "apalancamiento":    lev,
        "circuit_breaker":   cb,
        "perdidas_seguidas": perdidas,
        "pares_activos":     pares,
        "posiciones":        pos,
        "timestamp":         datetime.now().isoformat(),
    })

@app.route("/api/historial")
def api_historial():
    try:
        if os.path.exists("historial.json"):
            with open("historial.json") as f:
                return jsonify(json.load(f))
    except Exception as e:
        log.error(f"Historial API: {e}")
    return jsonify([])

@app.route("/api/logs")
def api_logs():
    try:
        if os.path.exists("logs/bot.log"):
            with open("logs/bot.log") as f:
                lineas = f.readlines()
            return jsonify({"logs": lineas[-100:]})  # últimas 100 líneas
    except Exception as e:
        log.error(f"Logs API: {e}")
    return jsonify({"logs": []})

def iniciar_servidor():
    port = int(os.getenv("PORT", "8080"))
    log.info(f"Dashboard en http://0.0.0.0:{port}")
    import logging as _log
    _log.getLogger("werkzeug").setLevel(_log.ERROR)  # silencia logs de Flask
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    log.info("SMC Bot BingX iniciando...")

    verificar_inicio()

    threading.Thread(target=telegram_polling,   daemon=True, name="TelegramPoller").start()
    threading.Thread(target=monitor_posiciones, daemon=True, name="PosMonitor").start()
    threading.Thread(target=iniciar_servidor,   daemon=True, name="Dashboard").start()
    log.info("Hilos iniciados: TelegramPoller, PosMonitor, Dashboard")

    ultimo_reporte = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    ciclo = 0

    while True:
        ciclo += 1
        log.info(f"══ CICLO {ciclo} | {datetime.now().strftime('%Y-%m-%d %H:%M')} ══")

        recalcular_capital()

        for s in estado["pares_activos"]:
            try:
                analizar(s)
                time.sleep(3)
            except Exception as e:
                log.error(f"Error analizando {s}: {e}")

        ahora = datetime.now()
        if ahora.hour == 6 and (ahora - ultimo_reporte).total_seconds() > 3600:
            _enviar_reporte()
            ultimo_reporte = ahora

        log.info("Esperando 4 horas...")
        time.sleep(4 * 60 * 60)

if __name__ == "__main__":
    main()
