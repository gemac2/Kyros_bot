import time
import os
import traceback
import requests
import pandas as pd
import pandas_ta as ta
from binance.client import Client
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv

# Cargar variables de entorno (.env)
load_dotenv()

# ====== CONFIGURACIÓN KAIROS ======
# Asegúrate de que en tu .env la clave secreta se llame igual que aquí ('BINANCE_SECRET_KEY')
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY')
BINANCE_API_SECRET = os.getenv('BINANCE_SECRET_KEY') 
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ====== PARÁMETROS KAIROS V2 (AJUSTADOS) ======
MIN_VOL_24H = 70_000_000 
EXCLUDED_COINS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'USDCUSDT', 'FDUSDUSDT']
TIMEFRAME_ENTRY = '3m'
TIMEFRAME_TREND = '15m'

EMA_FAST = 7
EMA_SLOW = 25

# AQUI ESTA EL CAMBIO IMPORTANTE DE GESTION:
ATR_PERIOD = 14
ATR_MULTIPLIER = 3.5      # Antes 1.5 -> Ahora 3.5 (Más espacio para respirar)
MIN_SL_PERCENT = 0.006    # 0.6% Mínimo obligatorio de distancia de SL
RISK_REWARD = 2         # Mantenemos el ratio, pero ahora buscará recorridos más largos

# Inicializar cliente
try:
    client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
except Exception as e:
    print(f"Error conectando a Binance: {e}")
    exit()

def print_header():
    print(r"""
    ██╗  ██╗ █████╗ ██╗██████╗  ██████╗ ███████╗    ██╗███╗   ███╗
    ██║ ██╔╝██╔══██╗██║██╔══██╗██╔═══██╗██╔════╝    ██║████╗ ████║
    █████╔╝ ███████║██║██████╔╝██║   ██║███████╗    ██║██╔████╔██║
    ██╔═██╗ ██╔══██║██║██╔══██╗██║   ██║╚════██║    ██║██║╚██╔╝██║
    ██║  ██╗██║  ██║██║██║  ██║╚██████╔╝███████║    ██║██║ ╚═╝ ██║
    ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝    ╚═╝╚═╝     ╚═╝
    >>> SYSTEM ONLINE: WAITING FOR THE PERFECT MOMENT...
    """)

def send_telegram_alert(message: str) -> None:
    """Envía la señal al canal de Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[Telegram Error] No se pudo enviar mensaje: {e}")

def get_market_candidates():
    """Filtra monedas volátiles con volumen > 70M, excluyendo las top caps."""
    try:
        tickers = client.futures_ticker()
        candidates = []
        for t in tickers:
            symbol = t['symbol']
            vol = float(t['quoteVolume'])
            if symbol.endswith('USDT') and vol > MIN_VOL_24H and symbol not in EXCLUDED_COINS:
                candidates.append(symbol)
        
        candidates.sort(key=lambda x: [float(t['quoteVolume']) for t in tickers if t['symbol'] == x][0], reverse=True)
        return candidates[:50]
    except Exception as e:
        print(f"[Error API] Obteniendo candidatos: {e}")
        return []

def get_usdt_balance():
    """Obtiene el balance total de la billetera de Futuros en USDT."""
    try:
        balances = client.futures_account_balance()
        for asset in balances:
            if asset['asset'] == 'USDT':
                return float(asset['balance'])
    except Exception as e:
        print(f"[Error API] No se pudo leer balance: {e}")
        return 0.0
    return 0.0

def get_data(symbol, interval, limit=1000): # <--- CAMBIO: limit=1000 (Antes 100)
    """Descarga velas y calcula indicadores (EMA, VWAP, ATR)."""
    try:
        klines = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
        # ... (resto de la creación del DataFrame igual) ...
        df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'q_vol', 'trades', 'b_vol', 'q_b_vol', 'ignore'])
        
        cols = ['open', 'high', 'low', 'close', 'volume']
        df[cols] = df[cols].astype(float)
        
        # Corrección de tiempo para VWAP
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        
        # INDICADORES
        df['EMA_7'] = ta.ema(df['close'], length=EMA_FAST)
        df['EMA_25'] = ta.ema(df['close'], length=EMA_SLOW)
        
        # VWAP: Al tener 1000 velas, ahora el valor será mucho más preciso
        df.ta.vwap(append=True) 
        
        df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=ATR_PERIOD)
        
        return df
    except Exception as e:
        return None

def analyze_symbol(symbol, usdt_balance):
    """Núcleo KAIROS V6: Filtro de Extensión y Entrada Sniper."""
    
    safe_balance = usdt_balance if usdt_balance > 10 else 10
    risk_amount = safe_balance * 0.04  # 4% Riesgo

    # Obtenemos data
    df_entry = get_data(symbol, TIMEFRAME_ENTRY, limit=1000)
    df_trend = get_data(symbol, TIMEFRAME_TREND, limit=1000)
    
    if df_entry is None or df_trend is None: return False

    curr = df_entry.iloc[-1]
    prev = df_entry.iloc[-2]
    prev_2 = df_entry.iloc[-3]
    trend_macro = df_trend.iloc[-1]

    try:
        vwap_col = [c for c in df_entry.columns if c.startswith('VWAP')][0]
        val_vwap = float(curr[vwap_col])
        val_ema7 = float(curr['EMA_7']) # Necesitamos el valor de la EMA 7 para la entrada
    except (IndexError, KeyError): return False

    signal_type = None
    market_price = float(curr['close']) 
    entry_price = market_price # Por defecto, precio de mercado... pero lo cambiaremos abajo
    sl_price = 0.0
    tp_price = 0.0

    # === NUEVOS PARÁMETROS DE FILTRO ===
    MAX_DIST_VWAP = 0.04  # 4% Máximo permitido de distancia al VWAP (Si es más, se descarta)
    SMART_ENTRY_DIST = 0.01 # 1% Si el precio está a más de 0.8% de la EMA 7, entramos en la EMA.

    # Buffer SL
    SL_BUFFER = 0.004 

    # --- LÓGICA LONG ---
    if (prev['EMA_7'] > prev['EMA_25']) and (prev_2['EMA_7'] <= prev_2['EMA_25']) \
       and (market_price > val_vwap) and (trend_macro['EMA_7'] > trend_macro['EMA_25']):
        
        # 1. FILTRO DE EXTENSIÓN (¿Estamos persiguiendo el precio?)
        dist_vwap = (market_price - val_vwap) / val_vwap
        if dist_vwap > MAX_DIST_VWAP:
            print(f"🚫 {symbol} Ignorado: Extensión excesiva ({dist_vwap*100:.2f}%)")
            return False

        signal_type = "LONG 🟢"
        
        # 2. ENTRADA SNIPER (Smart Entry)
        # Si el precio está muy alejado de la EMA 7, no compramos arriba.
        # Ponemos la orden LIMIT en la EMA 7 esperando el retroceso.
        dist_ema7 = (market_price - val_ema7) / val_ema7
        
        if dist_ema7 > SMART_ENTRY_DIST:
            entry_price = val_ema7 # <--- AQUÍ ESTÁ EL TRUCO
            entry_type = "LIMIT (Pullback EMA 7)"
        else:
            entry_price = market_price
            entry_type = "LIMIT (Actual)"

        # SL Debajo del VWAP
        sl_price = val_vwap * (1 - SL_BUFFER)
        
        dist_sl = entry_price - sl_price
        tp_price = entry_price + (dist_sl * RISK_REWARD)

    # --- LÓGICA SHORT ---
    elif (prev['EMA_7'] < prev['EMA_25']) and (prev_2['EMA_7'] >= prev_2['EMA_25']) \
         and (market_price < val_vwap) and (trend_macro['EMA_7'] < trend_macro['EMA_25']):
        
        # 1. FILTRO DE EXTENSIÓN
        dist_vwap = (val_vwap - market_price) / val_vwap # Invertido para short
        if dist_vwap > MAX_DIST_VWAP:
            print(f"🚫 {symbol} Ignorado: Extensión excesiva ({dist_vwap*100:.2f}%)")
            return False

        signal_type = "SHORT 🔴"
        
        # 2. ENTRADA SNIPER
        dist_ema7 = (val_ema7 - market_price) / val_ema7
        
        if dist_ema7 > SMART_ENTRY_DIST:
            entry_price = val_ema7 # Esperamos que suba a tocar la EMA
            entry_type = "LIMIT (Pullback EMA 7)"
        else:
            entry_price = market_price
            entry_type = "LIMIT (Actual)"

        # SL Encima del VWAP
        sl_price = val_vwap * (1 + SL_BUFFER)
        
        dist_sl = sl_price - entry_price
        tp_price = entry_price - (dist_sl * RISK_REWARD)

    # --- ENVÍO ---
    if signal_type:
        distancia_sl = abs(entry_price - sl_price)
        porcentaje_sl = (distancia_sl / entry_price) * 100
        
        # Filtro: Si aun esperando el retroceso el SL es > 4%, mejor no operar
        if porcentaje_sl > 4.0: return False
        if porcentaje_sl < 0.3: return False
        
        cantidad_monedas = risk_amount / distancia_sl
        leverage = (entry_price * cantidad_monedas) / safe_balance

        msg = (
            f"⚡ **KAIROS V6 - SNIPER MODE** ⚡\n"
            f"💎 **Moneda:** #{symbol}\n"
            f"📊 **Tipo:** {signal_type}\n"
            f"💰 **Riesgo:** ${risk_amount:.2f}\n\n"
            
            f"🚪 **Entrada:** ${entry_price:.4f}\n"
            f"🕹️ **Modo:** {entry_type}\n"
            f"🛑 **SL (VWAP):** ${sl_price:.4f} (-{porcentaje_sl:.2f}%)\n"
            f"🎯 **TP (1.5R):** ${tp_price:.4f}\n\n"
            
            f"🌊 **VWAP:** ${val_vwap:.4f}\n"
            f"📉 **EMA 7:** ${val_ema7:.4f}\n"
            f"🔢 **CANTIDAD:** {cantidad_monedas:.2f}\n"
        )
        print(f">>> KAIROS: {symbol} | Dist VWAP: {dist_vwap*100:.2f}%")
        send_telegram_alert(msg)
        return True
    
    return False

def main_loop():
    print_header()
    send_telegram_alert("🤖 **KAIROS 1M - SYSTEM ONLINE**")
    
    last_alerts = {} 
    
    while True:
        try:
            current_balance = get_usdt_balance()
            if current_balance < 10:
                print(f"⚠️ Balance bajo: ${current_balance}")
            
            candidates = get_market_candidates()
            print(f"\r💰 Balance: ${current_balance:.2f} | Escaneando {len(candidates)} activos...", end="")
            
            for symbol in candidates:
                if symbol in last_alerts:
                    if (time.time() - last_alerts[symbol]) < 300:
                        continue
                
                found = analyze_symbol(symbol, current_balance)
                if found:
                    last_alerts[symbol] = time.time()
                
                time.sleep(0.2) 
            
            time.sleep(10)

        except KeyboardInterrupt:
            print("\n🛑 Deteniendo Kairos...")
            break
        except Exception as e:
            print(f"\n[Error Loop] {e}")
            traceback.print_exc()
            time.sleep(10)

# ==========================================
# ESTO ERA LO QUE FALTABA PARA QUE CORRIERA:
# ==========================================
if __name__ == "__main__":
    main_loop()