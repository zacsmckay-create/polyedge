"""
PolyEdge — Polymarket Copy Trader
Tracks top Polymarket wallets, shows live trade alerts, consensus signals,
and lets you follow traders with Telegram notifications.
"""
import os, json, time, threading, requests
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
from datetime import datetime, timezone
from collections import defaultdict

app = Flask(__name__)
CORS(app)

ACCESS_CODE   = os.getenv('ACCESS_CODE', 'polyedge2025')
GAMMA_API     = 'https://gamma-api.polymarket.com'
DATA_API      = 'https://data-api.polymarket.com'

# ── In-memory stores ──────────────────────────────────────────────────────────
saved_traders   = {}   # address → {name, added_at}
telegram_config = {}   # {token, chat_id}
activity_cache  = {}   # address → [trades]
stats_cache     = {}   # address → {pnl, roi, win_rate, trades, volume}
market_cache    = {}   # conditionId → {question, category}

DATA_FILE = os.path.join(os.path.dirname(__file__), 'data.json')


DEFAULT_TRADERS = [
    '0x492442eab586f242b53bda933fd5de859c8a3782',
    '0x02227b8f5a9636e895607edd3185ed6ee5598ff7',
    '0x2a2c53bd278c04da9962fcf96490e17f3dfb9bc1',
    '0xefbc5fec8d7b0acdc8911bdd9a98d6964308f9a2',
    '0xc2e7800b5af46e6093872b177b7a5e7f0563be51',
    '0x36a3f17401e395ef4cb1b7f42bcdb8ab8e15fafb',
    '0x019782cab5d844f02bafb71f512758be78579f3c',
    '0x2005d16a84ceefa912d4e380cd32e7ff827875ea',
    '0x204f72f35326db932158cba6adff0b9a1da95e14',
    '0xead152b855effa6b5b5837f53b24c0756830c76a',
    '0xee613b3fc183ee44f9da9c05f53e2da107e3debf',
    '0x37c1874a60d348903594a96703e0507c518fc53a',
    '0x9495425feeb0c250accb89275c97587011b19a27',
    '0x777d9f00c2b4f7b829c9de0049ca3e707db05143',
    '0xdc876e6873772d38716fda7f2452a78d426d7ab6',
    '0x9f2fe025f84839ca81dd8e0338892605702d2ca8',
    '0xf195721ad850377c96cd634457c70cd9e8308057',
    '0x59a0744db1f39ff3afccd175f80e6e8dfc239a09',
    '0x8f037a2e4fd49d11267f4ab874ab7ba745ac64d6',
    '0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee',
    '0x07bdcabf60da99be8fad11092bf4e8412cffe993',
    '0x50b1db131a24a9d9450bbd0372a95d32ea88f076',
    '0x0eb568f307e9a48af2c3e688ad6074236712c494',
    '0x2eb10cb8596bf8c8ef409f72cfb5eb6438054ea4',
    '0xbddf61af533ff524d27154e589d2d7a81510c684',
    '0x54ac09857c3e76d50a2e7da064b0293d9a9e7c14',
    '0xbaa2bcb5439e985ce4ccf815b4700027d1b92c73',
    '0xdb27bf2ac5d428a9c63dbc914611036855a6c56e',
    '0x5d58e38cd0a7e6f5fa67b7f9c2f70dd70df09a15',
    '0x6480542954b70a674a74bd1a6015dec362dc8dc5',
    '0xfe787d2da716d60e8acff57fb87eb13cd4d10319',
    '0x507e52ef684ca2dd91f90a9d26d149dd3288beae',
    '0x43e98f912cd6ddadaad88d3297e78c0648e688e5',
    '0x9e9c8b080659b08c3474ea761790a20982e26421',
    '0x2663daca3cecf3767ca1c3b126002a8578a8ed1f',
    '0xd99f3bec8e060ada0aef0c4057695dd5bc22fcdc',
    '0xc21ea96be762bb55041529af6e386e7c53b80215',
    '0x2785e7022dc20757108204b13c08cea8613b70ae',
    '0x5d189e816b4149be00977c1a3c8840374aec4972',
    '0xc8ab97a9089a9ff7e6ef0688e6e591a066946418',
    '0xeebde7a0e019a63e6b476eb425505b7b3e6eba30',
    '0x4c2966a198cd7ac982110d0219b037afa9997570',
    '0xb27bc932bf8110d8f78e55da7d5f0497a18b5b82',
]

def load_data():
    global saved_traders, telegram_config
    # Load saved data first
    if os.path.exists(DATA_FILE):
        try:
            d = json.load(open(DATA_FILE))
            saved_traders   = d.get('saved_traders', {})
            telegram_config = d.get('telegram', {})
        except Exception:
            pass
    # Always ensure default traders are present
    for address in DEFAULT_TRADERS:
        if address not in saved_traders:
            saved_traders[address] = {'name': '', 'added_at': '2025-01-01T00:00:00'}

def save_data():
    with open(DATA_FILE, 'w') as f:
        json.dump({'saved_traders': saved_traders, 'telegram': telegram_config}, f)


# ── Polymarket API helpers ────────────────────────────────────────────────────

def get_activity(address, limit=100):
    """Fetch recent trades for a wallet address."""
    try:
        r = requests.get(f'{DATA_API}/activity',
                         params={'user': address, 'limit': limit},
                         timeout=10)
        if r.status_code == 200:
            return r.json() or []
    except Exception:
        pass
    return []


def get_market_info(condition_id):
    """Get market question and category, with caching."""
    if condition_id in market_cache:
        return market_cache[condition_id]
    try:
        r = requests.get(f'{GAMMA_API}/markets',
                         params={'conditionId': condition_id},
                         timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data:
                m = data[0]
                question = m.get('question', 'Unknown market')
                # Derive category from tags or question keywords
                tags = [t.get('label', '').lower() for t in m.get('tags', [])]
                cat  = 'other'
                if any(t in tags for t in ['crypto', 'bitcoin', 'ethereum', 'defi']):
                    cat = 'crypto'
                elif any(t in tags for t in ['sports', 'football', 'nfl', 'nba', 'soccer']):
                    cat = 'sports'
                elif any(t in tags for t in ['politics', 'election', 'government', 'trump']):
                    cat = 'politics'
                info = {'question': question, 'category': cat}
                market_cache[condition_id] = info
                return info
    except Exception:
        pass
    return {'question': 'Unknown market', 'category': 'other'}


def compute_stats(trades):
    """Compute P&L, ROI, win rate from a list of trades."""
    if not trades:
        return {'pnl': 0, 'roi': 0, 'win_rate': None, 'trade_count': 0, 'volume': 0}

    total_pnl  = 0.0
    total_cost = 0.0
    wins       = 0
    resolved   = 0
    volume     = 0.0

    for t in trades:
        size  = float(t.get('size', 0) or 0)
        price = float(t.get('price', 0) or 0)
        cost  = size * price
        volume += cost

        outcome = t.get('outcome')   # 'won' / 'lost' / None (unresolved)
        if outcome == 'won':
            pnl = size * (1 - price)   # profit = size - cost
            total_pnl  += pnl
            total_cost += cost
            wins       += 1
            resolved   += 1
        elif outcome == 'lost':
            total_pnl  -= cost
            total_cost += cost
            resolved   += 1

    roi      = (total_pnl / total_cost * 100) if total_cost > 0 else 0
    win_rate = (wins / resolved * 100) if resolved > 0 else None

    return {
        'pnl':        round(total_pnl, 2),
        'roi':        round(roi, 1),
        'win_rate':   round(win_rate, 1) if win_rate is not None else None,
        'trade_count': len(trades),
        'volume':     round(volume, 2),
    }


def get_trader_name(address):
    """Get Polymarket pseudonym for a wallet address."""
    if address in saved_traders and saved_traders[address].get('name'):
        return saved_traders[address]['name']
    try:
        r = requests.get(f'{GAMMA_API}/profiles',
                         params={'address': address},
                         timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data and isinstance(data, list):
                name = data[0].get('pseudonym') or data[0].get('name') or address[:10]
                return name
            elif data and isinstance(data, dict):
                return data.get('pseudonym') or data.get('name') or address[:10]
    except Exception:
        pass
    return address[:10] + '...'


def refresh_all_stats():
    """Background thread: refresh activity + stats for all saved traders."""
    while True:
        for address in list(saved_traders.keys()):
            try:
                trades = get_activity(address, limit=200)
                activity_cache[address] = trades
                stats_cache[address]    = compute_stats(trades)
                # Update name if not set
                if not saved_traders[address].get('name'):
                    saved_traders[address]['name'] = get_trader_name(address)
            except Exception:
                pass
            time.sleep(0.5)
        time.sleep(30)


def send_telegram(token, chat_id, message):
    try:
        requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML'},
            timeout=8
        )
    except Exception:
        pass


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/auth', methods=['POST'])
def auth():
    code = (request.json or {}).get('code', '').strip()
    if code == ACCESS_CODE:
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'Wrong access code'})


@app.route('/api/copy-alerts')
def copy_alerts():
    """Recent trades from ALL saved traders, newest first."""
    try:
        alerts = []
        for address, info in saved_traders.items():
            trades = activity_cache.get(address, [])
            for t in trades[:20]:  # last 20 trades per trader
                condition_id = t.get('conditionId') or t.get('market', '')
                market = get_market_info(condition_id) if condition_id else {'question': 'Unknown', 'category': 'other'}
                side   = t.get('side', '').upper()  # YES / NO
                size   = float(t.get('size', 0) or 0)
                price  = float(t.get('price', 0) or 0)
                usd    = round(size * price, 2)
                ts     = t.get('timestamp') or t.get('createdAt') or ''
                alerts.append({
                    'question':   market['question'],
                    'category':   market['category'],
                    'side':       side,
                    'price':      price,
                    'usd':        usd,
                    'trader':     info.get('name', address[:10]),
                    'address':    address,
                    'timestamp':  ts,
                    'stats':      stats_cache.get(address, {}),
                })

        # Sort newest first
        alerts.sort(key=lambda x: x['timestamp'], reverse=True)
        return jsonify({'ok': True, 'alerts': alerts[:100]})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/top-wallets')
def top_wallets():
    """Return saved traders ranked by P&L, best first."""
    try:
        wallets = []
        for address, info in saved_traders.items():
            stats = stats_cache.get(address, {})
            wallets.append({
                'address':    address,
                'name':       info.get('name', address[:10] + '...'),
                'pnl':        stats.get('pnl', 0),
                'roi':        stats.get('roi', 0),
                'win_rate':   stats.get('win_rate'),
                'trade_count': stats.get('trade_count', 0),
                'volume':     stats.get('volume', 0),
            })
        # Sort by P&L descending — best traders first
        wallets.sort(key=lambda x: x['pnl'], reverse=True)
        return jsonify({'ok': True, 'wallets': wallets})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/saved-traders')
def get_saved_traders():
    result = []
    for address, info in saved_traders.items():
        stats = stats_cache.get(address, {})
        result.append({
            'address':    address,
            'name':       info.get('name', address[:10] + '...'),
            'pnl':        stats.get('pnl', 0),
            'roi':        stats.get('roi', 0),
            'win_rate':   stats.get('win_rate'),
            'trade_count': stats.get('trade_count', 0),
        })
    return jsonify({'ok': True, 'traders': result, 'telegram': telegram_config})


@app.route('/api/add-trader', methods=['POST'])
def add_trader():
    address = (request.json or {}).get('address', '').strip()
    if not address:
        return jsonify({'ok': False, 'error': 'No address provided'})
    if address in saved_traders:
        return jsonify({'ok': False, 'error': 'Already saved'})
    name = get_trader_name(address)
    saved_traders[address] = {'name': name, 'added_at': datetime.now().isoformat()}
    save_data()
    # Kick off stats fetch in background
    threading.Thread(target=lambda: (
        activity_cache.update({address: get_activity(address, 200)}),
        stats_cache.update({address: compute_stats(activity_cache[address])})
    ), daemon=True).start()
    return jsonify({'ok': True, 'name': name})


@app.route('/api/remove-trader', methods=['POST'])
def remove_trader():
    address = (request.json or {}).get('address', '').strip()
    saved_traders.pop(address, None)
    activity_cache.pop(address, None)
    stats_cache.pop(address, None)
    save_data()
    return jsonify({'ok': True})


@app.route('/api/consensus')
def consensus():
    """Find markets where multiple saved traders are on the same side."""
    try:
        # bucket: (conditionId, side) → list of traders
        buckets = defaultdict(list)
        for address, info in saved_traders.items():
            trades = activity_cache.get(address, [])
            seen   = set()
            for t in trades[:50]:
                condition_id = t.get('conditionId') or t.get('market', '')
                side         = (t.get('side') or '').upper()
                key          = (condition_id, side)
                if key not in seen and condition_id and side:
                    seen.add(key)
                    size  = float(t.get('size', 0) or 0)
                    price = float(t.get('price', 0) or 0)
                    buckets[key].append({
                        'name':      info.get('name', address[:10]),
                        'address':   address,
                        'usd':       round(size * price, 2),
                        'timestamp': t.get('timestamp') or t.get('createdAt') or '',
                        'stats':     stats_cache.get(address, {}),
                    })

        signals = []
        for (condition_id, side), traders in buckets.items():
            if len(traders) < 2:
                continue
            market       = get_market_info(condition_id)
            combined_usd = sum(t['usd'] for t in traders)
            latest_ts    = max((t['timestamp'] for t in traders), default='')
            # Quality score: weight by number of quality traders (positive ROI)
            quality = sum(1 for t in traders if (t['stats'].get('roi') or 0) > 0)
            signals.append({
                'question':    market['question'],
                'category':    market['category'],
                'side':        side,
                'wallet_count': len(traders),
                'combined_usd': round(combined_usd, 2),
                'latest':      latest_ts,
                'quality':     quality,
                'traders':     traders,
                'condition_id': condition_id,
            })

        # Sort: most wallets first, then by combined USD
        signals.sort(key=lambda x: (x['wallet_count'], x['combined_usd']), reverse=True)
        return jsonify({'ok': True, 'signals': signals[:30]})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/wallet-history')
def wallet_history():
    address = request.args.get('address', '').strip()
    if not address:
        return jsonify({'ok': False, 'error': 'No address'})
    trades = activity_cache.get(address) or get_activity(address, 100)
    stats  = compute_stats(trades)
    name   = saved_traders.get(address, {}).get('name') or get_trader_name(address)
    result = []
    for t in trades:
        condition_id = t.get('conditionId') or t.get('market', '')
        market = get_market_info(condition_id) if condition_id else {'question': 'Unknown', 'category': 'other'}
        size   = float(t.get('size', 0) or 0)
        price  = float(t.get('price', 0) or 0)
        result.append({
            'question':  market['question'],
            'category':  market['category'],
            'side':      (t.get('side') or '').upper(),
            'price':     price,
            'usd':       round(size * price, 2),
            'outcome':   t.get('outcome'),
            'timestamp': t.get('timestamp') or t.get('createdAt') or '',
        })
    return jsonify({'ok': True, 'trades': result, 'stats': stats, 'name': name})


@app.route('/api/telegram', methods=['POST'])
def set_telegram():
    d = request.json or {}
    token   = d.get('token', '').strip()
    chat_id = d.get('chat_id', '').strip()
    if not token or not chat_id:
        return jsonify({'ok': False, 'error': 'Token and Chat ID required'})
    # Test it
    try:
        r = requests.get(f'https://api.telegram.org/bot{token}/getMe', timeout=8)
        if r.status_code != 200:
            return jsonify({'ok': False, 'error': 'Invalid bot token'})
    except Exception:
        return jsonify({'ok': False, 'error': 'Could not reach Telegram'})
    telegram_config['token']   = token
    telegram_config['chat_id'] = chat_id
    save_data()
    send_telegram(token, chat_id, '✅ <b>PolyEdge connected!</b>\nYou will receive alerts when saved traders place trades.')
    return jsonify({'ok': True})


@app.route('/api/refresh', methods=['POST'])
def manual_refresh():
    """Force immediate refresh of all trader stats."""
    threading.Thread(target=lambda: [
        (
            activity_cache.update({addr: get_activity(addr, 200)}),
            stats_cache.update({addr: compute_stats(activity_cache[addr])})
        )
        for addr in list(saved_traders.keys())
    ], daemon=True).start()
    return jsonify({'ok': True})


if __name__ == '__main__':
    load_data()
    threading.Thread(target=refresh_all_stats, daemon=True).start()
    port = int(os.getenv('PORT', 5001))
    print(f'\n  PolyEdge running at http://localhost:{port}\n')
    app.run(host='0.0.0.0', port=port, debug=False)
