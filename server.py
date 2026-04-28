"""
PolyEdge — Polymarket Copy Trader
Tracks top Polymarket wallets, shows live trade alerts, consensus signals,
and lets you follow traders with Telegram notifications.
"""
import os, json, time, threading, requests, secrets
from functools import wraps
from flask import Flask, jsonify, request, render_template, session
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from tempfile import NamedTemporaryFile

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY') or secrets.token_hex(32)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)

BASE_DIR      = os.path.dirname(__file__)
GAMMA_API     = 'https://gamma-api.polymarket.com'
DATA_API      = 'https://data-api.polymarket.com'


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('authenticated'):
            return jsonify({'ok': False, 'error': 'Not authenticated'}), 401
        session.permanent = True
        return fn(*args, **kwargs)
    return wrapper


def is_wallet_address(value):
    if not isinstance(value, str) or len(value) != 42 or not value.startswith('0x'):
        return False
    return all(c in '0123456789abcdefABCDEF' for c in value[2:])


# ── In-memory stores ──────────────────────────────────────────────────────────
saved_traders    = {}   # address → {name, added_at}
telegram_config  = {}   # {token, chat_id}
activity_cache   = {}   # address → [trades]
stats_cache      = {}   # address → {pnl, roi, win_rate, trades, volume}
market_cache     = {}   # conditionId → {question, category, slug}
specialty_cache  = {}   # address → {specialty, category_stats}

CATEGORIES = ['sports', 'politics', 'crypto', 'finance', 'other']

CATEGORY_KEYWORDS = {
    'sports': ['nba', 'nfl', 'mlb', 'nhl', 'premier-league', 'champions-league',
               'tennis', 'golf', 'ufc', 'mma', 'boxing', 'cricket', 'rugby', 'formula-1',
               'nascar', 'ncaa', 'bundesliga', 'la-liga', 'serie-a', 'world-cup',
               'super-bowl', 'superbowl', 'nba-finals', 'stanley-cup', 'march-madness',
               'over-under', 'moneyline', 'nba-', 'nfl-', 'mlb-', 'nhl-', 'ufc-',
               'epl-', 'fifa', 'wimbledon', 'pga-', 'masters-', 'euros-'],
    'politics': ['trump', 'election', 'president', 'congress', 'senate', 'democrat',
                 'republican', 'biden', 'harris', 'nato', 'supreme-court', 'white-house',
                 'governor', 'parliament', 'prime-minister', 'vote', 'ballot',
                 'tariff', 'executive-order', 'impeach', 'ceasefire', 'ukraine',
                 'russia', 'china-', 'iran-', 'israel-', 'geopolit'],
    'crypto':   ['bitcoin', 'btc', 'ethereum', 'eth', 'solana', 'sol', 'crypto',
                 'defi', 'nft', 'dogecoin', 'doge-', 'xrp', 'bnb-', 'avax',
                 'chainlink', 'polygon', 'arbitrum', 'coinbase', 'binance',
                 'uniswap', 'blockchain', 'altcoin', 'memecoin'],
    'finance':  ['sp500', 'nasdaq', 'gdp', 'inflation', 'federal-reserve', 'interest-rate',
                 'oil-price', 'gold-price', 'stock-market', 'ipo-', 'recession',
                 'earnings-', 'market-cap', 'dow-jones', 'treasury'],
}


def classify_trade(trade):
    """Detect category from trade slug/eventSlug/title."""
    text = ' '.join([
        trade.get('slug', ''),
        trade.get('eventSlug', ''),
        trade.get('title', ''),
    ]).lower()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return cat
    return 'other'


def compute_category_stats(trades):
    """Return per-category P&L, ROI, and sample sizes."""
    buckets = {c: {'pnl': 0.0, 'cost': 0.0, 'wins': 0, 'resolved': 0, 'count': 0}
               for c in CATEGORIES}
    for t in trades:
        cat   = classify_trade(t)
        size  = float(t.get('size', 0) or 0)
        price = float(t.get('price', 0) or 0)
        cost  = size * price
        b     = buckets[cat]
        b['count'] += 1
        outcome = t.get('outcome')
        if outcome == 'won':
            b['pnl']      += size * (1 - price)
            b['cost']     += cost
            b['wins']     += 1
            b['resolved'] += 1
        elif outcome == 'lost':
            b['pnl']      -= cost
            b['cost']     += cost
            b['resolved'] += 1

    result = {}
    for cat, b in buckets.items():
        if b['count'] == 0:
            continue
        roi      = round(b['pnl'] / b['cost'] * 100, 1) if b['cost'] > 0 else 0
        win_rate = round(b['wins'] / b['resolved'] * 100, 1) if b['resolved'] > 0 else None
        result[cat] = {
            'pnl':        round(b['pnl'], 2),
            'roi':        roi,
            'win_rate':   win_rate,
            'trade_count': b['count'],
            'resolved_count': b['resolved'],
        }
    return result


def detect_specialty(category_stats):
    """Return the category with highest ROI (min 5 resolved trades)."""
    best_cat, best_roi = 'other', -999
    for cat, s in category_stats.items():
        if cat == 'other':
            continue
        if s.get('resolved_count', 0) >= 5 and (s.get('roi') or 0) > best_roi:
            best_roi = s['roi']
            best_cat = cat
    return best_cat

DATA_FILE = os.path.join(BASE_DIR, 'data.json')


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
            with open(DATA_FILE, encoding='utf-8') as f:
                d = json.load(f)
            saved_traders   = d.get('saved_traders', {})
            telegram_config = d.get('telegram', {})
        except Exception:
            pass
    # Always ensure default traders are present
    for address in DEFAULT_TRADERS:
        if address not in saved_traders:
            saved_traders[address] = {'name': '', 'added_at': '2025-01-01T00:00:00'}

def save_data():
    payload = {'saved_traders': saved_traders, 'telegram': telegram_config}
    temp_file = None
    try:
        with NamedTemporaryFile('w', encoding='utf-8', delete=False, dir=BASE_DIR) as f:
            temp_file = f.name
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, DATA_FILE)
    finally:
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass


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


def _to_float(value):
    try:
        return float(value)
    except Exception:
        return None


def _extract_yes_no_prices(market_obj):
    yes_price = None
    no_price = None

    outcome_prices = market_obj.get('outcomePrices')
    if isinstance(outcome_prices, str):
        try:
            outcome_prices = json.loads(outcome_prices)
        except Exception:
            outcome_prices = None
    if isinstance(outcome_prices, list) and len(outcome_prices) >= 2:
        yes_price = _to_float(outcome_prices[0])
        no_price = _to_float(outcome_prices[1])
    elif isinstance(outcome_prices, dict):
        yes_price = _to_float(outcome_prices.get('yes'))
        no_price = _to_float(outcome_prices.get('no'))

    if yes_price is None:
        yes_price = _to_float(market_obj.get('yesPrice'))
    if no_price is None:
        no_price = _to_float(market_obj.get('noPrice'))

    if yes_price is None and no_price is not None:
        yes_price = round(1 - no_price, 6)
    if no_price is None and yes_price is not None:
        no_price = round(1 - yes_price, 6)

    return yes_price, no_price


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
                events = m.get('events', [])
                slug = events[0].get('slug', '') if events else m.get('slug', '')
                yes_price, no_price = _extract_yes_no_prices(m)
                info = {
                    'question': question,
                    'category': cat,
                    'slug': slug,
                    'yes_price': yes_price,
                    'no_price': no_price,
                }
                market_cache[condition_id] = info
                return info
    except Exception:
        pass
    return {'question': 'Unknown market', 'category': 'other', 'slug': '', 'yes_price': None, 'no_price': None}


def compute_stats(trades):
    """Compute P&L, ROI, win rate from a list of trades."""
    if not trades:
        return {'pnl': 0, 'roi': 0, 'win_rate': None, 'trade_count': 0, 'resolved_count': 0, 'open_count': 0, 'volume': 0}

    total_pnl  = 0.0
    total_cost = 0.0
    wins       = 0
    resolved   = 0
    open_count = 0
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
        else:
            open_count += 1

    roi      = (total_pnl / total_cost * 100) if total_cost > 0 else 0
    win_rate = (wins / resolved * 100) if resolved > 0 else None

    return {
        'pnl':        round(total_pnl, 2),
        'roi':        round(roi, 1),
        'win_rate':   round(win_rate, 1) if win_rate is not None else None,
        'trade_count': len(trades),
        'resolved_count': resolved,
        'open_count': open_count,
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
                activity_cache[address]  = trades
                stats_cache[address]     = compute_stats(trades)
                cat_stats                = compute_category_stats(trades)
                specialty_cache[address] = {
                    'specialty':      detect_specialty(cat_stats),
                    'category_stats': cat_stats,
                }
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
    from flask import make_response
    import os
    session['authenticated'] = True
    session.permanent = True
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates', 'index.html')
    with open(path, 'r', encoding='utf-8') as f:
        html = f.read()
    resp = make_response(html, 200)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return resp


@app.route('/go')
def go_to_market():
    from flask import redirect
    slug         = request.args.get('slug', '')
    condition_id = request.args.get('id', '')
    if slug:
        return redirect(f'https://polymarket.com/event/{slug}')
    if condition_id:
        market = get_market_info(condition_id)
        slug   = market.get('slug', '')
        if slug:
            return redirect(f'https://polymarket.com/event/{slug}')
    return redirect('https://polymarket.com')


@app.route('/api/auth', methods=['POST'])
def auth():
    # Desktop-local mode: skip manual unlock and establish a local session.
    session['authenticated'] = True
    session.permanent = True
    return jsonify({'ok': True})


@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/api/copy-alerts')
@require_auth
def copy_alerts():
    """Recent trades from ALL saved traders, newest first."""
    try:
        alerts = []
        for address, info in saved_traders.items():
            trades = activity_cache.get(address, [])
            for t in trades[:20]:
                condition_id = t.get('conditionId') or t.get('market', '')
                question     = t.get('title') or 'Unknown market'
                category     = classify_trade(t)
                event_slug   = t.get('eventSlug', '')
                side  = t.get('side', '').upper()
                size  = float(t.get('size', 0) or 0)
                price = float(t.get('price', 0) or 0)
                usd   = round(size * price, 2)
                ts    = t.get('timestamp') or t.get('createdAt') or ''
                market_info = get_market_info(condition_id) if condition_id else {'yes_price': None, 'no_price': None}
                side_price = market_info.get('yes_price') if side == 'YES' else market_info.get('no_price')
                price_move_pp = round((side_price - price) * 100, 1) if side_price is not None else None
                alerts.append({
                    'question':     question,
                    'category':     category,
                    'condition_id': condition_id,
                    'event_slug':   event_slug,
                    'side':         side,
                    'price':        price,
                    'usd':          usd,
                    'trader':       info.get('name', address[:10]),
                    'address':      address,
                    'timestamp':    ts,
                    'stats':        stats_cache.get(address, {}),
                    'specialty':    specialty_cache.get(address, {}).get('specialty', 'other'),
                    'current_price': side_price,
                    'price_move_pp': price_move_pp,
                })

        alerts.sort(key=lambda x: x['timestamp'], reverse=True)
        return jsonify({'ok': True, 'alerts': alerts[:100]})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/top-wallets')
@require_auth
def top_wallets():
    """Return saved traders ranked by P&L, best first."""
    try:
        wallets = []
        for address, info in saved_traders.items():
            stats = stats_cache.get(address, {})
            sp    = specialty_cache.get(address, {})
            wallets.append({
                'address':        address,
                'name':           info.get('name', address[:10] + '...'),
                'pnl':            stats.get('pnl', 0),
                'roi':            stats.get('roi', 0),
                'win_rate':       stats.get('win_rate'),
                'trade_count':    stats.get('trade_count', 0),
                'resolved_count': stats.get('resolved_count', 0),
                'open_count':     stats.get('open_count', 0),
                'volume':         stats.get('volume', 0),
                'specialty':      sp.get('specialty', 'other'),
                'category_stats': sp.get('category_stats', {}),
            })
        wallets.sort(key=lambda x: x['pnl'], reverse=True)
        return jsonify({'ok': True, 'wallets': wallets})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/saved-traders')
@require_auth
def get_saved_traders():
    result = []
    for address, info in saved_traders.items():
        stats = stats_cache.get(address, {})
        sp    = specialty_cache.get(address, {})
        result.append({
            'address':        address,
            'name':           info.get('name', address[:10] + '...'),
            'pnl':            stats.get('pnl', 0),
            'roi':            stats.get('roi', 0),
            'win_rate':       stats.get('win_rate'),
            'trade_count':    stats.get('trade_count', 0),
            'resolved_count': stats.get('resolved_count', 0),
            'open_count':     stats.get('open_count', 0),
            'specialty':      sp.get('specialty', 'other'),
            'category_stats': sp.get('category_stats', {}),
        })
    telegram_status = {'connected': bool(telegram_config.get('token') and telegram_config.get('chat_id'))}
    if telegram_status['connected']:
        chat_id = telegram_config.get('chat_id', '')
        telegram_status['chat_id_masked'] = ('...' + chat_id[-4:]) if len(chat_id) > 4 else 'set'
    return jsonify({'ok': True, 'traders': result, 'telegram': telegram_status})


@app.route('/api/add-trader', methods=['POST'])
@require_auth
def add_trader():
    address = (request.json or {}).get('address', '').strip()
    if not address:
        return jsonify({'ok': False, 'error': 'No address provided'})
    if not is_wallet_address(address):
        return jsonify({'ok': False, 'error': 'Enter a valid wallet address'})
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
@require_auth
def remove_trader():
    address = (request.json or {}).get('address', '').strip()
    if not is_wallet_address(address):
        return jsonify({'ok': False, 'error': 'Enter a valid wallet address'})
    saved_traders.pop(address, None)
    activity_cache.pop(address, None)
    stats_cache.pop(address, None)
    save_data()
    return jsonify({'ok': True})


@app.route('/api/consensus')
@require_auth
def consensus():
    """Find markets where multiple saved traders are on the same side."""
    try:
        cat_filter = request.args.get('category', 'all')

        # bucket: (eventSlug, side) → list of traders
        buckets = defaultdict(list)
        for address, info in saved_traders.items():
            trades = activity_cache.get(address, [])
            seen   = set()
            for t in trades[:50]:
                condition_id = t.get('conditionId') or t.get('market', '')
                event_slug   = t.get('eventSlug', '') or condition_id
                side         = (t.get('side') or '').upper()
                category     = classify_trade(t)
                key          = (event_slug, side)
                if key not in seen and event_slug and side:
                    seen.add(key)
                    size  = float(t.get('size', 0) or 0)
                    price = float(t.get('price', 0) or 0)
                    sp    = specialty_cache.get(address, {})
                    buckets[key].append({
                        'name':      info.get('name', address[:10]),
                        'address':   address,
                        'usd':       round(size * price, 2),
                        'timestamp': t.get('timestamp') or t.get('createdAt') or '',
                        'stats':     stats_cache.get(address, {}),
                        'specialty': sp.get('specialty', 'other'),
                        'question':  t.get('title', ''),
                        'category':  category,
                        'event_slug': event_slug,
                        'condition_id': condition_id,
                        'entry_price': price,
                    })

        signals = []
        for (event_slug, side), traders in buckets.items():
            if len(traders) < 2:
                continue
            category     = traders[0]['category']
            if cat_filter != 'all' and category != cat_filter:
                continue
            question     = traders[0]['question']
            combined_usd = sum(t['usd'] for t in traders)
            latest_ts    = max((t['timestamp'] for t in traders), default='')
            quality      = sum(1 for t in traders if (t['stats'].get('roi') or 0) > 0)
            market_info  = get_market_info(traders[0]['condition_id']) if traders[0]['condition_id'] else {'yes_price': None, 'no_price': None}
            current_price = market_info.get('yes_price') if side == 'YES' else market_info.get('no_price')
            avg_entry_price = round(sum((t.get('entry_price') or 0) for t in traders) / len(traders), 4) if traders else None
            price_move_pp = round((current_price - avg_entry_price) * 100, 1) if (current_price is not None and avg_entry_price is not None) else None
            # Specialist bonus: count traders whose specialty matches this category
            specialist_count = sum(1 for t in traders if t['specialty'] == category)
            signals.append({
                'question':         question,
                'category':         category,
                'event_slug':       event_slug,
                'condition_id':     traders[0]['condition_id'],
                'side':             side,
                'wallet_count':     len(traders),
                'combined_usd':     round(combined_usd, 2),
                'latest':           latest_ts,
                'quality':          quality,
                'specialist_count': specialist_count,
                'traders':          traders,
                'current_price':    current_price,
                'avg_entry_price':  avg_entry_price,
                'price_move_pp':    price_move_pp,
            })

        # Sort: specialist count first, then wallet count, then USD
        signals.sort(key=lambda x: (x['specialist_count'], x['wallet_count'], x['combined_usd']), reverse=True)
        return jsonify({'ok': True, 'signals': signals[:30]})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/specialists')
@require_auth
def specialists():
    """Return top traders per category based on ROI."""
    try:
        result = {c: [] for c in CATEGORIES}
        for address, info in saved_traders.items():
            sp    = specialty_cache.get(address, {})
            stats = stats_cache.get(address, {})
            cat   = sp.get('specialty', 'other')
            cat_s = sp.get('category_stats', {}).get(cat, {})
            result[cat].append({
                'address':    address,
                'name':       info.get('name', address[:10] + '...'),
                'specialty':  cat,
                'roi':        cat_s.get('roi', 0),
                'pnl':        cat_s.get('pnl', 0),
                'win_rate':   cat_s.get('win_rate'),
                'trade_count': cat_s.get('trade_count', 0),
                'overall_roi': stats.get('roi', 0),
            })
        for cat in result:
            result[cat].sort(key=lambda x: x['roi'], reverse=True)
        return jsonify({'ok': True, 'specialists': result})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/wallet-history')
@require_auth
def wallet_history():
    address = request.args.get('address', '').strip()
    if not address:
        return jsonify({'ok': False, 'error': 'No address'})
    if not is_wallet_address(address):
        return jsonify({'ok': False, 'error': 'Enter a valid wallet address'})
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
@require_auth
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
@require_auth
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
    app.run(host='127.0.0.1', port=port, debug=False)
