import time
import os
import traceback
import requests
import pandas as pd
import pandas_ta as ta
from binance.client import Client
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

# ====== CONFIGURACIÓN KAIROS V6 (ORDER BOOK FORCE) ======
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY')
BINANCE_API_SECRET = os.getenv('BINANCE_SECRET_KEY') 
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# PARÁMETROS DE MERCADO
MIN_VOL_24H = 70_000_000 
TIMEFRAME_ENTRY = '3m'
TIMEFRAME_TREND = '15m'
EMA_FAST = 7
EMA_SLOW = 25

# CONFIGURACIÓN ORDER BOOK (FILTRO DE LIQUIDEZ)
OB_LEVELS = 20        # Niveles del libro a analizar
FORCE_RATIO_MIN = 1.3 # 30% más de volumen a favor para validar la entrada

# GESTIÓN DE RIESGO
MAX_DIST_VWAP = 0.04  # 4% Max distancia al VWAP
SMART_ENTRY_DIST = 0.01 # 1% distancia para forzar entrada LIMIT en EMA 7
SL_BUFFER = 0.004     # 0.4% margen de seguridad sobre el VWAP
RISK_REWARD = 2

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
    >>> SYSTEM ONLINE: ORDER BOOK LIQUIDEZ FILTER ACTIVE
    """)

def send_telegram_alert(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except:
        pass

def get_market_candidates():
    try:
        tickers = client.futures_ticker()
        # Filtramos por volumen y terminación USDT
        candidates = [t['symbol'] for t in tickers if t['symbol'].endswith('USDT') and float(t['quoteVolume']) > MIN_VOL_24H]
        return candidates[:50]
    except:
        return []

def get_orderbook_force(symbol):
    """Calcula si hay más presión de compra o venta real en los primeros niveles."""
    try:
        depth = client.futures_order_book(symbol=symbol, limit=OB_LEVELS)
        sum_bids = sum([float(bid[1]) for bid in depth['bids']])
        sum_asks = sum([float(ask[1]) for ask in depth['asks']])
        return sum_bids / sum_asks if sum_asks > 0 else 1.0
    except:
        return 1.0

def get_data(symbol, interval, limit=500):
    try:
        klines = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
        if not klines or len(klines) < 50: return None

        df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'q_vol', 'trades', 'b_vol', 'q_b_vol', 'ignore'])
        
        # Formateo numérico
        cols = ['open', 'high', 'low', 'close', 'volume']
        df[cols] = df[cols].astype(float)
        
        # --- CORRECCIÓN DATETIME INDEX PARA VWAP ---
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        df.sort_index(inplace=True)

        # Indicadores
        df['EMA_7'] = ta.ema(df['close'], length=EMA_FAST)
        df['EMA_25'] = ta.ema(df['close'], length=EMA_SLOW)
        
        # VWAP requiere el DatetimeIndex que configuramos arriba
        df.ta.vwap(append=True)
        
        df.dropna(inplace=True)
        return df
    except Exception as e:
        print(f"Error en data de {symbol}: {e}")
        return None

def analyze_symbol(symbol, usdt_balance):
    # Definir riesgo (4% del balance)
    risk_amount = (usdt_balance if usdt_balance > 10 else 10) * 0.04 

    df_entry = get_data(symbol, TIMEFRAME_ENTRY)
    df_trend = get_data(symbol, TIMEFRAME_TREND)
    
    # Validar que tengamos datos suficientes para evitar "index out of range"
    if df_entry is None or len(df_entry) < 5: return False
    if df_trend is None or len(df_trend) < 5: return False

    curr, prev, prev_2 = df_entry.iloc[-1], df_entry.iloc[-2], df_entry.iloc[-3]
    trend_macro = df_trend.iloc[-1]
    
    # Obtener nombre dinámico de columna VWAP
    vwap_col = [c for c in df_entry.columns if c.startswith('VWAP')][0]
    val_vwap, val_ema7, market_price = float(curr[vwap_col]), float(curr['EMA_7']), float(curr['close'])
    
    signal_type = None
    entry_price = market_price

    # --- LÓGICA LONG ---
    if (prev['EMA_7'] > prev['EMA_25']) and (prev_2['EMA_7'] <= prev_2['EMA_25']) \
       and (market_price > val_vwap) and (trend_macro['EMA_7'] > trend_macro['EMA_25']):
        
        force_ratio = get_orderbook_force(symbol)
        if force_ratio >= FORCE_RATIO_MIN:
            signal_type = "LONG 🟢"
            sl_price = val_vwap * (1 - SL_BUFFER)
            # Sniper Entry
            if ((market_price - val_ema7) / val_ema7) > SMART_ENTRY_DIST:
                entry_price, entry_type = val_ema7, "LIMIT (Pullback EMA 7)"
            else: entry_type = "LIMIT (Actual)"
            tp_price = entry_price + ((entry_price - sl_price) * RISK_REWARD)

    # --- LÓGICA SHORT ---
    elif (prev['EMA_7'] < prev['EMA_25']) and (prev_2['EMA_7'] >= prev_2['EMA_25']) \
         and (market_price < val_vwap) and (trend_macro['EMA_7'] < trend_macro['EMA_25']):
        
        force_ratio = get_orderbook_force(symbol)
        if force_ratio <= (1 / FORCE_RATIO_MIN):
            signal_type = "SHORT 🔴"
            sl_price = val_vwap * (1 + SL_BUFFER)
            # Sniper Entry
            if ((val_ema7 - market_price) / val_ema7) > SMART_ENTRY_DIST:
                entry_price, entry_type = val_ema7, "LIMIT (Pullback EMA 7)"
            else: entry_type = "LIMIT (Actual)"
            tp_price = entry_price - ((sl_price - entry_price) * RISK_REWARD)

    if signal_type:
        dist_sl = abs(entry_price - sl_price)
        pct_sl = (dist_sl / entry_price) * 100
        
        # Filtro de seguridad para el Stop Loss
        if 0.3 < pct_sl < 4.5:
            # CÁLCULO DE CANTIDAD: Riesgo ($) / Distancia al SL ($)
            cantidad_monedas = risk_amount / dist_sl
            
            msg = (
                f"⚡ **KAIROS SNIPER V6** ⚡\n"
                f"💎 **Moneda:** #{symbol}\n"
                f"📊 **Tipo:** {signal_type}\n"
                f"💰 **Riesgo Op:** ${risk_amount:.2f}\n\n"
                
                f"🚪 **Entrada:** ${entry_price:.4f}\n"
                f"🕹️ **Modo:** {entry_type}\n"
                f"🛑 **SL (VWAP):** ${sl_price:.4f} (-{pct_sl:.2f}%)\n"
                f"🎯 **TP ({RISK_REWARD}R):** ${tp_price:.4f}\n\n"
                
                f"🌊 **VWAP:** ${val_vwap:.4f}\n"
                f"📉 **EMA 7:** ${val_ema7:.4f}\n"
                f"🔢 **CANTIDAD:** {cantidad_monedas:.2f}\n\n"
                
                f"⚖️ **OB Ratio:** {force_ratio:.2f}"
            )
            
            send_telegram_alert(msg)
            print(f"\n✅ Señal completa enviada: {symbol} | Cant: {cantidad_monedas:.2f}")
            return True
    return False

def main_loop():
    print_header()
    last_alerts = {}
    while True:
        try:
            # Obtener balance de la cuenta de futuros
            bal_data = client.futures_account_balance()
            usdt_bal = next(float(a['balance']) for a in bal_data if a['asset'] == 'USDT')
            
            candidates = get_market_candidates()
            print(f"\r💰 Bal: ${usdt_bal:.2f} | Escaneando {len(candidates)} activos...", end="")
            
            for s in candidates:
                # Evitar alertas repetidas en menos de 5 minutos
                if s in last_alerts and (time.time() - last_alerts[s]) < 300: continue
                
                if analyze_symbol(s, usdt_bal):
                    last_alerts[s] = time.time()
                
                time.sleep(0.1) # Respetar límites de la API
                
            time.sleep(5)
        except Exception as e:
            print(f"\n[Error Loop] {e}")
            time.sleep(10)

if __name__ == "__main__":
    main_loop()