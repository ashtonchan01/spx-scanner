#!/usr/bin/env python3
"""IBKR Proxy Server — bridges the scanner app to IB Gateway/TWS via ib_insync."""

import asyncio, sys, math
from flask import Flask, request, jsonify
from ib_insync import IB, Stock, Index, Option, util

# Allow nested event loops (needed for ib_insync + Flask)
util.patchAsyncio()

app = Flask(__name__)
ib = IB()

# ── Config ──
IB_HOST = '127.0.0.1'
IB_PORT = 4001   # 4001 = IB Gateway (paper: 4002), 7496 = TWS (paper: 7497)
CLIENT_ID = 1

# ── Contracts ──
SPX = Index('SPX', 'CBOE')
VIX = Index('VIX', 'CBOE')

# ── Last known prices (persist across requests) ──
last_known = {'spx': None, 'vix': None}


def ensure_connected():
    if not ib.isConnected():
        ib.connect(IB_HOST, IB_PORT, clientId=CLIENT_ID)
        ib.reqMarketDataType(3)  # 3=delayed (works outside market hours), 1=live


@app.before_request
def before():
    try:
        ensure_connected()
    except Exception as e:
        return jsonify({'error': f'IB connection failed: {e}'}), 503


@app.after_request
def cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Headers'] = '*'
    return resp


@app.route('/ping')
def ping():
    return jsonify({'status': 'ok', 'connected': ib.isConnected()})


@app.route('/market/snapshot')
def market_snapshot():
    """Return live SPX and VIX prices, falling back to last known if unavailable."""
    import threading, time

    result = {'spx': None, 'vix': None, 'done': False}

    def fetch():
        try:
            ib.qualifyContracts(SPX, VIX)
            ib.reqMktData(SPX, '', False, False)
            ib.reqMktData(VIX, '', False, False)
            ib.sleep(2)

            for tick, key in [(ib.ticker(SPX), 'spx'), (ib.ticker(VIX), 'vix')]:
                for attr in ['marketPrice', 'last', 'close']:
                    val = getattr(tick, attr, None)
                    if callable(val):
                        val = val()
                    if val and isinstance(val, (int, float)) and not math.isnan(val) and val > 0:
                        result[key] = round(val, 2)
                        break
        except Exception:
            pass
        result['done'] = True

    t = threading.Thread(target=fetch, daemon=True)
    t.start()
    t.join(timeout=5)  # Hard 5-second timeout

    if result['spx']:
        last_known['spx'] = result['spx']
    if result['vix']:
        last_known['vix'] = result['vix']

    live = bool(result['spx'] or result['vix'])

    return jsonify({
        'spx': last_known['spx'],
        'vix': last_known['vix'],
        'live': live,
        'source': 'live' if live else 'last_known',
    })


@app.route('/options/spread')
def options_spread():
    """Return bid/ask/mid for a vertical spread.
    Params: short_strike, long_strike, right (P/C), expiry (YYYYMMDD)
    """
    short_strike = float(request.args['short_strike'])
    long_strike = float(request.args['long_strike'])
    right = request.args['right']  # 'P' or 'C'
    expiry = request.args['expiry']  # '20260320'

    short_opt = Option('SPX', expiry, short_strike, right, 'SMART')
    long_opt = Option('SPX', expiry, long_strike, right, 'SMART')
    ib.qualifyContracts(short_opt, long_opt)

    tickers = ib.reqTickers(short_opt, long_opt)
    ib.sleep(0.5)
    tickers = ib.reqTickers(short_opt, long_opt)

    short_t, long_t = tickers[0], tickers[1]

    # Credit spread: sell short, buy long
    # Natural credit = short bid - long ask
    # Mid credit = short mid - long mid
    short_bid = short_t.bid if short_t.bid > 0 else 0
    short_ask = short_t.ask if short_t.ask > 0 else 0
    long_bid = long_t.bid if long_t.bid > 0 else 0
    long_ask = long_t.ask if long_t.ask > 0 else 0

    short_mid = (short_bid + short_ask) / 2 if short_bid and short_ask else 0
    long_mid = (long_bid + long_ask) / 2 if long_bid and long_ask else 0

    natural = round(short_bid - long_ask, 2)
    mid = round(short_mid - long_mid, 2)

    return jsonify({
        'short_strike': short_strike,
        'long_strike': long_strike,
        'right': right,
        'expiry': expiry,
        'short': {'bid': short_bid, 'ask': short_ask, 'mid': round(short_mid, 2)},
        'long': {'bid': long_bid, 'ask': long_ask, 'mid': round(long_mid, 2)},
        'spread': {'natural': natural, 'mid': mid},
    })


if __name__ == '__main__':
    print('Connecting to IB Gateway...')
    try:
        ensure_connected()
        print(f'Connected to IB on {IB_HOST}:{IB_PORT}')
    except Exception as e:
        print(f'WARNING: Could not connect to IB: {e}')
        print('Start IB Gateway/TWS first, then the proxy will retry on each request.')

    print('Starting proxy on http://0.0.0.0:5050')
    app.run(host='0.0.0.0', port=5050, debug=False)
