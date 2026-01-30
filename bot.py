#!/usr/bin/env python3
"""
Slack Bet Tracker Bot - Tracks bets announced in channels.

Usage:
    export SLACK_BOT_TOKEN="xoxb-your-token"
    export SLACK_APP_TOKEN="xapp-your-token"
    python bot.py

Mention the bot to log a bet:
    @betbot @alice vs @bob $50 on who finishes first
    @betbot @alice owes @bob $20

Commands (mention the bot):
    @betbot list - show all open bets
    @betbot history - show resolved bets
    @betbot settle <bet_id> winner @person - mark bet as settled
    @betbot cancel <bet_id> - cancel a bet
    @betbot help - show help
"""

import os
import re
import sqlite3
import logging
import requests
import io
from datetime import datetime, timedelta
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# Lazy load EasyOCR (heavy import)
_ocr_reader = None

def get_ocr_reader():
    """Get or initialize the OCR reader."""
    global _ocr_reader
    if _ocr_reader is None:
        import easyocr
        _ocr_reader = easyocr.Reader(['en'], gpu=False)
    return _ocr_reader

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Database
DATABASE = "/Users/jaykang/Documents/owllegs/bets.db"


def init_db():
    """Initialize the database."""
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT,
            person1_id TEXT,
            person1_name TEXT,
            person2_id TEXT,
            person2_name TEXT,
            amount TEXT,
            description TEXT,
            status TEXT DEFAULT 'open',
            winner_id TEXT,
            created_at TEXT,
            resolved_at TEXT,
            created_by TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS parlays (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            user_name TEXT,
            channel_id TEXT,
            stake TEXT,
            total_odds TEXT,
            potential_payout TEXT,
            legs TEXT,
            status TEXT DEFAULT 'open',
            result TEXT,
            created_at TEXT,
            resolved_at TEXT,
            source TEXT
        )
    """)
    conn.commit()
    conn.close()


def add_bet(channel_id, person1_id, person1_name, person2_id, person2_name,
            amount, description, created_by):
    """Add a new bet to the database."""
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO bets (channel_id, person1_id, person1_name, person2_id,
                         person2_name, amount, description, status, created_at, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
    """, (channel_id, person1_id, person1_name, person2_id, person2_name,
          amount, description, datetime.now().isoformat(), created_by))
    bet_id = c.lastrowid
    conn.commit()
    conn.close()
    return bet_id


def get_open_bets(channel_id=None):
    """Get all open bets, optionally filtered by channel."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if channel_id:
        c.execute("SELECT * FROM bets WHERE status = 'open' AND channel_id = ? ORDER BY created_at DESC",
                  (channel_id,))
    else:
        c.execute("SELECT * FROM bets WHERE status = 'open' ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_resolved_bets(channel_id=None, limit=10):
    """Get resolved bets."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if channel_id:
        c.execute("""SELECT * FROM bets WHERE status != 'open' AND channel_id = ?
                    ORDER BY resolved_at DESC LIMIT ?""", (channel_id, limit))
    else:
        c.execute("SELECT * FROM bets WHERE status != 'open' ORDER BY resolved_at DESC LIMIT ?",
                  (limit,))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def settle_bet(bet_id, winner_id, winner_name):
    """Settle a bet with a winner."""
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        UPDATE bets SET status = 'settled', winner_id = ?, resolved_at = ?
        WHERE id = ? AND status = 'open'
    """, (winner_id, datetime.now().isoformat(), bet_id))
    updated = c.rowcount
    conn.commit()
    conn.close()
    return updated > 0


def cancel_bet(bet_id):
    """Cancel a bet."""
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        UPDATE bets SET status = 'cancelled', resolved_at = ?
        WHERE id = ? AND status = 'open'
    """, (datetime.now().isoformat(), bet_id))
    updated = c.rowcount
    conn.commit()
    conn.close()
    return updated > 0


def get_bet(bet_id):
    """Get a specific bet by ID."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM bets WHERE id = ?", (bet_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_balances():
    """Calculate balances for all users from settled bets."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM bets WHERE status = 'settled'")
    rows = c.fetchall()
    conn.close()

    balances = {}  # user_id -> {'name': name, 'balance': amount}

    for bet in rows:
        bet = dict(bet)
        winner_id = bet['winner_id']
        loser_id = bet['person1_id'] if winner_id == bet['person2_id'] else bet['person2_id']
        winner_name = bet['person1_name'] if winner_id == bet['person1_id'] else bet['person2_name']
        loser_name = bet['person2_name'] if winner_id == bet['person1_id'] else bet['person1_name']

        # Parse amount (remove $ and convert to float)
        amount_str = bet['amount'].replace('$', '').replace(',', '')
        try:
            amount = float(amount_str)
        except:
            amount = 0

        # Update winner balance
        if winner_id not in balances:
            balances[winner_id] = {'name': winner_name, 'balance': 0}
        balances[winner_id]['balance'] += amount

        # Update loser balance
        if loser_id not in balances:
            balances[loser_id] = {'name': loser_name, 'balance': 0}
        balances[loser_id]['balance'] -= amount

    return balances


def get_user_balance(user_id):
    """Get balance for a specific user."""
    balances = get_balances()
    return balances.get(user_id, {'name': 'Unknown', 'balance': 0})


def get_user_debts(user_id):
    """Get who this user owes and who owes them."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""SELECT * FROM bets WHERE status = 'settled'
                 AND (person1_id = ? OR person2_id = ?)""", (user_id, user_id))
    rows = c.fetchall()
    conn.close()

    # Track net debt between this user and each other user
    debts = {}  # other_user_id -> {'name': name, 'amount': net_amount}
    # positive = they owe you, negative = you owe them

    for bet in rows:
        bet = dict(bet)
        winner_id = bet['winner_id']

        # Determine the other person in this bet
        if bet['person1_id'] == user_id:
            other_id = bet['person2_id']
            other_name = bet['person2_name']
        else:
            other_id = bet['person1_id']
            other_name = bet['person1_name']

        # Parse amount
        amount_str = bet['amount'].replace('$', '').replace(',', '')
        try:
            amount = float(amount_str)
        except:
            amount = 0

        if other_id not in debts:
            debts[other_id] = {'name': other_name, 'amount': 0}

        # If user won, other owes them. If user lost, user owes other.
        if winner_id == user_id:
            debts[other_id]['amount'] += amount  # they owe you
        else:
            debts[other_id]['amount'] -= amount  # you owe them

    return debts


def get_all_records():
    """Get win/loss records for all users."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM bets WHERE status = 'settled'")
    rows = c.fetchall()
    conn.close()

    records = {}  # user_id -> {'name': name, 'wins': 0, 'losses': 0}

    for bet in rows:
        bet = dict(bet)
        winner_id = bet['winner_id']
        loser_id = bet['person1_id'] if winner_id == bet['person2_id'] else bet['person2_id']
        winner_name = bet['person1_name'] if winner_id == bet['person1_id'] else bet['person2_name']
        loser_name = bet['person2_name'] if winner_id == bet['person1_id'] else bet['person1_name']

        # Update winner
        if winner_id not in records:
            records[winner_id] = {'name': winner_name, 'wins': 0, 'losses': 0}
        records[winner_id]['wins'] += 1

        # Update loser
        if loser_id not in records:
            records[loser_id] = {'name': loser_name, 'wins': 0, 'losses': 0}
        records[loser_id]['losses'] += 1

    # Calculate percentages
    for user_id, data in records.items():
        total = data['wins'] + data['losses']
        data['total'] = total
        data['win_pct'] = (data['wins'] / total * 100) if total > 0 else 0

    return records


def get_user_history(user_id, limit=15):
    """Get settled bets involving a specific user."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""SELECT * FROM bets WHERE status = 'settled'
                 AND (person1_id = ? OR person2_id = ?)
                 ORDER BY resolved_at DESC LIMIT ?""", (user_id, user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]


# Parlay functions
import json


def add_parlay(user_id, user_name, channel_id, stake, legs, source="manual"):
    """Add a new parlay to track."""
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()

    # Calculate total odds (multiply all leg odds)
    total_odds = 1.0
    for leg in legs:
        odds = leg.get('odds', 1.0)
        if isinstance(odds, str):
            odds = parse_odds(odds)
        total_odds *= odds

    # Calculate potential payout
    try:
        stake_float = float(str(stake).replace('$', '').replace(',', ''))
        potential_payout = stake_float * total_odds
    except:
        potential_payout = 0

    c.execute("""
        INSERT INTO parlays (user_id, user_name, channel_id, stake, total_odds,
                            potential_payout, legs, status, created_at, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
    """, (user_id, user_name, channel_id, str(stake), f"{total_odds:.2f}",
          f"${potential_payout:.2f}", json.dumps(legs), datetime.now().isoformat(), source))
    parlay_id = c.lastrowid
    conn.commit()
    conn.close()
    return parlay_id


def get_user_parlays(user_id, status='open'):
    """Get parlays for a user."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if status:
        c.execute("SELECT * FROM parlays WHERE user_id = ? AND status = ? ORDER BY created_at DESC",
                  (user_id, status))
    else:
        c.execute("SELECT * FROM parlays WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_parlay(parlay_id):
    """Get a specific parlay."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM parlays WHERE id = ?", (parlay_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def update_parlay_status(parlay_id, status, result=None):
    """Update parlay status (won/lost/pushed)."""
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        UPDATE parlays SET status = ?, result = ?, resolved_at = ?
        WHERE id = ?
    """, (status, result, datetime.now().isoformat(), parlay_id))
    conn.commit()
    conn.close()


def parse_odds(odds_str):
    """Parse American or decimal odds to decimal multiplier."""
    odds_str = str(odds_str).strip()

    # Already decimal (e.g., "2.5", "1.91")
    if '.' in odds_str and not odds_str.startswith(('+', '-')):
        try:
            return float(odds_str)
        except:
            pass

    # American odds
    try:
        odds_int = int(odds_str.replace('+', ''))
        if odds_int > 0:
            # Positive American odds: +150 means bet $100 to win $150
            return 1 + (odds_int / 100)
        else:
            # Negative American odds: -150 means bet $150 to win $100
            return 1 + (100 / abs(odds_int))
    except:
        pass

    return 1.0  # Default if can't parse


def parse_parlay_text(text):
    """Parse parlay legs from text input."""
    legs = []
    lines = text.strip().split('\n')

    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        # Try to parse: "Team/Pick odds" or "Team/Pick @ odds" or "Team/Pick (odds)"
        # Common formats:
        # - Lakers ML +150
        # - Lakers -3.5 -110
        # - Over 220.5 -110
        # - Chiefs to win +200

        leg = {'pick': line, 'odds': 1.0}

        # Look for odds at the end
        odds_patterns = [
            r'([+-]\d+)\s*$',  # American odds at end: +150, -110
            r'@\s*([+-]?\d+\.?\d*)\s*$',  # @ odds
            r'\(([+-]?\d+\.?\d*)\)\s*$',  # (odds)
            r'\s(\d+\.\d+)\s*$',  # Decimal odds: 2.50
        ]

        for pattern in odds_patterns:
            match = re.search(pattern, line)
            if match:
                odds_str = match.group(1)
                leg['odds'] = parse_odds(odds_str)
                leg['pick'] = line[:match.start()].strip()
                break

        if leg['pick']:
            legs.append(leg)

    return legs


def format_parlay(parlay):
    """Format a parlay for display."""
    legs = json.loads(parlay['legs']) if isinstance(parlay['legs'], str) else parlay['legs']

    lines = [f"*Parlay #{parlay['id']}* - {parlay['user_name']}"]
    lines.append(f"Stake: {parlay['stake']} → Potential: {parlay['potential_payout']}")
    lines.append(f"Total Odds: {parlay['total_odds']}x")
    lines.append("Legs:")

    for i, leg in enumerate(legs, 1):
        odds_str = f" ({leg.get('odds', '')})" if leg.get('odds') else ""
        status_icon = ""
        if leg.get('status') == 'won':
            status_icon = " ✓"
        elif leg.get('status') == 'lost':
            status_icon = " ✗"
        lines.append(f"  {i}. {leg['pick']}{odds_str}{status_icon}")

    status = parlay['status']
    if status == 'won':
        lines.append(f"*WON {parlay['potential_payout']}!*")
    elif status == 'lost':
        lines.append(f"*LOST*")
    elif status == 'pushed':
        lines.append(f"*PUSHED*")

    return "\n".join(lines)


# DARKO projections storage (in-memory, updated when CSV uploaded)
_darko_projections = {}
_darko_last_updated = None

# The Odds API key
ODDS_API_KEY = "4dc7a4a974da09518d53c1b93ba7a4cd"


def fetch_nba_player_props():
    """Fetch NBA player props from The Odds API."""
    try:
        # First get today's games
        events_url = f"https://api.the-odds-api.com/v4/sports/basketball_nba/events?apiKey={ODDS_API_KEY}"
        resp = requests.get(events_url, timeout=15)
        events = resp.json()

        all_props = []

        # Get props for each game (limit to first 5 to save API calls)
        for event in events[:5]:
            event_id = event['id']
            props_url = f"https://api.the-odds-api.com/v4/sports/basketball_nba/events/{event_id}/odds"
            params = {
                'apiKey': ODDS_API_KEY,
                'regions': 'us',
                'markets': 'player_points,player_assists',
                'oddsFormat': 'american'
            }

            try:
                props_resp = requests.get(props_url, params=params, timeout=15)
                props_data = props_resp.json()

                # Parse the props
                for bookmaker in props_data.get('bookmakers', [])[:1]:  # Just first bookmaker
                    for market in bookmaker.get('markets', []):
                        market_key = market.get('key', '')
                        for outcome in market.get('outcomes', []):
                            player_name = outcome.get('description', '')
                            line = outcome.get('point', 0)
                            over_under = outcome.get('name', '')

                            if player_name and line and over_under == 'Over':
                                prop_type = 'pts' if 'points' in market_key else 'ast'
                                all_props.append({
                                    'player': player_name,
                                    'type': prop_type,
                                    'line': line
                                })
            except Exception as e:
                logger.error(f"Error fetching props for event {event_id}: {e}")
                continue

        return all_props
    except Exception as e:
        logger.error(f"Error fetching NBA props: {e}")
        return []


def parse_darko_csv(csv_content):
    """Parse DARKO CSV content into a dictionary by player name."""
    global _darko_projections, _darko_last_updated
    import csv as csv_module
    from io import StringIO

    projections = {}
    reader = csv_module.DictReader(StringIO(csv_content))

    for row in reader:
        player = row.get('Player', '').strip()
        if player:
            projections[player.lower()] = {
                'name': player,
                'team': row.get('Team', ''),
                'minutes': float(row.get('Minutes', 0) or 0),
                'pts': float(row.get('PTS', 0) or 0),
                'ast': float(row.get('AST', 0) or 0),
                'reb': float(row.get('DREB', 0) or 0) + float(row.get('OREB', 0) or 0),
                'stl': float(row.get('STL', 0) or 0),
                'blk': float(row.get('BLK', 0) or 0),
            }

    _darko_projections = projections
    _darko_last_updated = datetime.now()
    return len(projections)


def fetch_nba_injuries():
    """Fetch NBA injury report from ESPN."""
    try:
        url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
        resp = requests.get(url, timeout=15)
        data = resp.json()

        injuries = []
        for team in data.get('items', []):
            team_name = team.get('team', {}).get('displayName', 'Unknown')
            for player in team.get('injuries', []):
                athlete = player.get('athlete', {})
                name = athlete.get('displayName', 'Unknown')
                status = player.get('status', 'Unknown')
                injury_type = player.get('type', {}).get('description', '')

                injuries.append({
                    'player': name,
                    'team': team_name,
                    'status': status,
                    'injury': injury_type,
                })

        return injuries
    except Exception as e:
        logger.error(f"Error fetching injuries: {e}")
        return None


def compare_darko_to_props():
    """Compare DARKO projections to prop lines and find biggest edges."""
    if not _darko_projections:
        return None, "No DARKO data loaded. Upload the CSV first!"

    # Fetch live prop lines
    props = fetch_nba_player_props()

    if not props:
        # Fall back to just showing projections
        players = list(_darko_projections.values())
        top_pts = sorted(players, key=lambda x: x['pts'], reverse=True)[:15]
        top_ast = sorted(players, key=lambda x: x['ast'], reverse=True)[:15]
        return {
            'top_pts': top_pts,
            'top_ast': top_ast,
            'edges_pts': [],
            'edges_ast': [],
            'last_updated': _darko_last_updated,
            'props_found': False
        }, None

    # Compare props to DARKO and find edges
    edges_pts = []
    edges_ast = []

    for prop in props:
        player_name = prop['player'].lower()
        line = prop['line']
        prop_type = prop['type']

        # Find matching DARKO projection
        darko = None
        for key, val in _darko_projections.items():
            # Match by last name or full name
            if player_name in key or key in player_name:
                darko = val
                break
            # Try matching last name
            prop_last = player_name.split()[-1] if player_name else ''
            darko_last = key.split()[-1] if key else ''
            if prop_last == darko_last and len(prop_last) > 3:
                darko = val
                break

        if darko:
            if prop_type == 'pts':
                darko_proj = darko['pts']
                delta = darko_proj - line
                edges_pts.append({
                    'player': prop['player'],
                    'team': darko.get('team', ''),
                    'line': line,
                    'darko': darko_proj,
                    'delta': delta,
                    'edge': 'OVER' if delta > 0 else 'UNDER'
                })
            elif prop_type == 'ast':
                darko_proj = darko['ast']
                delta = darko_proj - line
                edges_ast.append({
                    'player': prop['player'],
                    'team': darko.get('team', ''),
                    'line': line,
                    'darko': darko_proj,
                    'delta': delta,
                    'edge': 'OVER' if delta > 0 else 'UNDER'
                })

    # Sort by absolute delta (biggest edges first)
    edges_pts.sort(key=lambda x: abs(x['delta']), reverse=True)
    edges_ast.sort(key=lambda x: abs(x['delta']), reverse=True)

    return {
        'edges_pts': edges_pts[:10],
        'edges_ast': edges_ast[:10],
        'last_updated': _darko_last_updated,
        'props_found': True
    }, None


# ESPN API for odds (they have betting data now)
ODDS_SPORTS = {
    'nba': 'basketball/nba',
    'nfl': 'football/nfl',
    'mlb': 'baseball/mlb',
    'nhl': 'hockey/nhl',
    'soccer': 'soccer/usa.1',
    'epl': 'soccer/eng.1',
    'ncaab': 'basketball/mens-college-basketball',
    'ncaaf': 'football/college-football',
}


def fetch_odds(sport):
    """Fetch betting odds from ESPN."""
    sport_path = ODDS_SPORTS.get(sport.lower())
    if not sport_path:
        return None

    try:
        # ESPN scoreboard with odds
        url = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/scoreboard"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        games = []

        for event in data.get("events", [])[:8]:
            competition = event.get("competitions", [{}])[0]
            competitors = competition.get("competitors", [])

            if len(competitors) < 2:
                continue

            home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
            away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])

            game_info = {
                "home": home.get("team", {}).get("displayName", ""),
                "away": away.get("team", {}).get("displayName", ""),
                "status": event.get("status", {}).get("type", {}).get("shortDetail", ""),
                "spread": None,
                "total": None,
                "moneyline": {},
            }

            # Get odds from competition
            odds_list = competition.get("odds", [])
            if odds_list:
                odds = odds_list[0]
                if odds.get("spread"):
                    spread_val = odds.get("spread", 0)
                    fav = odds.get("favoriteTeamId")
                    if fav:
                        fav_name = home["team"]["displayName"] if home["team"].get("id") == fav else away["team"]["displayName"]
                        game_info["spread"] = f"{fav_name} {float(spread_val):+.1f}"
                    else:
                        game_info["spread"] = f"{spread_val}"

                if odds.get("overUnder"):
                    game_info["total"] = odds.get("overUnder")

                # Moneyline from details
                details = odds.get("details", "")
                if details:
                    game_info["details"] = details

            # Add score if game started
            if competition.get("status", {}).get("type", {}).get("state") != "pre":
                home_score = home.get("score", "0")
                away_score = away.get("score", "0")
                game_info["score"] = f"{away_score}-{home_score}"

            games.append(game_info)

        return games
    except Exception as e:
        logger.error(f"Error fetching odds: {e}")
        return None


def format_odds(game):
    """Format game odds for display."""
    lines = [f"*{game['away']} @ {game['home']}*"]
    lines.append(f"  {game.get('status', '')}")

    if game.get("score"):
        lines.append(f"  Score: {game['score']}")
    if game.get("spread"):
        lines.append(f"  Spread: {game['spread']}")
    if game.get("total"):
        lines.append(f"  O/U: {game['total']}")
    if game.get("details"):
        lines.append(f"  {game['details']}")

    return "\n".join(lines)


# Kalshi Prediction Market API
KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"


def fetch_kalshi_markets(limit=200, status="open"):
    """Fetch markets from Kalshi."""
    try:
        url = f"{KALSHI_API_BASE}/markets"
        params = {"limit": limit, "status": status}
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        return data.get("markets", [])
    except Exception as e:
        logger.error(f"Error fetching Kalshi markets: {e}")
        return None


def search_kalshi_markets(query, limit=10):
    """Search Kalshi markets by keyword."""
    markets = fetch_kalshi_markets(limit=500)
    if not markets:
        return None

    query_lower = query.lower()
    query_words = query_lower.split()

    matches = []
    for market in markets:
        ticker = market.get("ticker", "").lower()
        event_ticker = market.get("event_ticker", "").lower()
        yes_title = market.get("yes_sub_title", "").lower()
        no_title = market.get("no_sub_title", "").lower()

        # Check if any query word matches
        searchable = f"{ticker} {event_ticker} {yes_title} {no_title}"
        if all(word in searchable for word in query_words):
            matches.append(market)

    # Sort by volume (most active first)
    matches.sort(key=lambda m: float(m.get("volume_24h_fp", "0") or "0"), reverse=True)
    return matches[:limit]


def format_kalshi_market(market):
    """Format a Kalshi market for Slack display."""
    ticker = market.get("ticker", "")
    yes_title = market.get("yes_sub_title", ticker)

    # Get prices (convert from decimal string to percentage)
    yes_ask = market.get("yes_ask_dollars")
    yes_bid = market.get("yes_bid_dollars")
    last_price = market.get("last_price_dollars")

    # Format price as percentage
    if last_price:
        try:
            price_pct = float(last_price) * 100
            price_str = f"{price_pct:.0f}%"
        except:
            price_str = "N/A"
    elif yes_ask:
        try:
            price_pct = float(yes_ask) * 100
            price_str = f"{price_pct:.0f}%"
        except:
            price_str = "N/A"
    else:
        price_str = "N/A"

    # Volume
    vol_24h = market.get("volume_24h_fp", "0")
    try:
        vol_str = f"${float(vol_24h):,.0f}"
    except:
        vol_str = "$0"

    # Bid/ask spread
    spread_str = ""
    if yes_bid and yes_ask:
        try:
            bid_pct = float(yes_bid) * 100
            ask_pct = float(yes_ask) * 100
            spread_str = f" (bid {bid_pct:.0f}¢ / ask {ask_pct:.0f}¢)"
        except:
            pass

    lines = [
        f"*{yes_title}*",
        f"  Yes: {price_str}{spread_str}",
        f"  24h Vol: {vol_str}",
        f"  `{ticker}`"
    ]
    return "\n".join(lines)


# ESPN API endpoints
ESPN_SCOREBOARD = {
    'nba': 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',
    'nfl': 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard',
    'soccer': 'https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/scoreboard',  # MLS
    'epl': 'https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard',  # Premier League
}


def fetch_scores(sport):
    """Fetch recent scores from ESPN API."""
    url = ESPN_SCOREBOARD.get(sport.lower())
    if not url:
        return None

    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        games = []

        for event in data.get('events', []):
            competition = event.get('competitions', [{}])[0]
            competitors = competition.get('competitors', [])

            if len(competitors) >= 2:
                home = competitors[0]
                away = competitors[1]

                game = {
                    'id': event.get('id'),
                    'name': event.get('name'),
                    'date': event.get('date'),
                    'status': event.get('status', {}).get('type', {}).get('description', 'Unknown'),
                    'completed': event.get('status', {}).get('type', {}).get('completed', False),
                    'home_team': home.get('team', {}).get('displayName', 'Unknown'),
                    'home_abbrev': home.get('team', {}).get('abbreviation', ''),
                    'home_score': home.get('score', '0'),
                    'away_team': away.get('team', {}).get('displayName', 'Unknown'),
                    'away_abbrev': away.get('team', {}).get('abbreviation', ''),
                    'away_score': away.get('score', '0'),
                    'winner': None
                }

                if game['completed']:
                    home_score = int(game['home_score']) if game['home_score'].isdigit() else 0
                    away_score = int(game['away_score']) if game['away_score'].isdigit() else 0
                    if home_score > away_score:
                        game['winner'] = game['home_team']
                    elif away_score > home_score:
                        game['winner'] = game['away_team']
                    else:
                        game['winner'] = 'Tie'

                games.append(game)

        return games
    except Exception as e:
        logger.error(f"Error fetching scores: {e}")
        return None


def match_bet_to_game(bet_description, games):
    """Try to match a bet description to a game result."""
    desc_lower = bet_description.lower()

    for game in games:
        if not game['completed']:
            continue

        # Check if any team name or abbreviation is in the bet description
        teams = [
            game['home_team'].lower(),
            game['away_team'].lower(),
            game['home_abbrev'].lower(),
            game['away_abbrev'].lower(),
        ]

        for team in teams:
            if team and len(team) > 2 and team in desc_lower:
                return game

    return None


def format_game(game):
    """Format a game for display."""
    if game['completed']:
        return (f"{game['away_team']} {game['away_score']} @ "
                f"{game['home_team']} {game['home_score']} (Final) "
                f"- Winner: {game['winner']}")
    else:
        return (f"{game['away_team']} {game['away_score']} @ "
                f"{game['home_team']} {game['home_score']} ({game['status']})")


# Initialize Slack app
app = App(token=os.environ.get("SLACK_BOT_TOKEN"))


def get_user_name(client, user_id):
    """Get display name for a user ID."""
    try:
        result = client.users_info(user=user_id)
        user = result["user"]
        return user.get("real_name") or user.get("name") or user_id
    except:
        return user_id


def parse_bet_message(text, bot_user_id, sender_id):
    """Parse a bet from a message. Returns dict or None."""
    # Remove bot mention
    text = re.sub(f'<@{bot_user_id}>', '', text).strip()

    # Pattern: @person1 vs @person2 $amount description
    pattern = r'<@(\w+)>\s+(?:vs\.?|versus)\s+<@(\w+)>\s+\$?(\d+(?:\.\d{2})?)\s+(.+)'
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return {
            'person1_id': match.group(1),
            'person2_id': match.group(2),
            'amount': f"${match.group(3)}",
            'description': match.group(4).strip()
        }

    # Pattern: @person1 owes @person2 $amount [for description]
    pattern = r'<@(\w+)>\s+owes\s+<@(\w+)>\s+\$?(\d+(?:\.\d{2})?)\s*(?:for\s+)?(.*)'
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return {
            'person1_id': match.group(1),
            'person2_id': match.group(2),
            'amount': f"${match.group(3)}",
            'description': match.group(4).strip() or "debt"
        }

    # Flexible pattern: "I bet @person amount ..." or "bet @person amount ..."
    pattern = r'(?:i\s+)?bet\s+<@(\w+)>\s+\$?(\d+(?:\.\d{2})?)\s*(.*)'
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return {
            'person1_id': sender_id,
            'person2_id': match.group(1),
            'amount': f"${match.group(2)}",
            'description': match.group(3).strip() or "bet"
        }

    # Flexible pattern: "@person amount on/that/for ..."
    pattern = r'<@(\w+)>\s+\$?(\d+(?:\.\d{2})?)\s+(?:on|that|for)?\s*(.*)'
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return {
            'person1_id': sender_id,
            'person2_id': match.group(1),
            'amount': f"${match.group(2)}",
            'description': match.group(3).strip() or "bet"
        }

    # Flexible pattern: "amount with/against @person ..."
    pattern = r'\$?(\d+(?:\.\d{2})?)\s+(?:with|against|vs)?\s*<@(\w+)>\s*(.*)'
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return {
            'person1_id': sender_id,
            'person2_id': match.group(2),
            'amount': f"${match.group(1)}",
            'description': match.group(3).strip() or "bet"
        }

    # Last resort: find any @mention and any number
    mentions = re.findall(r'<@(\w+)>', text)

    # Find amounts - look for $ followed by numbers, or standalone numbers that look like bets
    # Prioritize amounts with $ sign, then larger numbers
    dollar_amounts = re.findall(r'\$(\d+(?:\.\d{2})?)', text)

    # For numbers without $, only match if they're standalone (not part of IDs)
    # Look for numbers preceded by space/start and followed by space/end
    standalone_amounts = re.findall(r'(?:^|[\s,])(\d{2,})(?:[\s,.]|$)', text)

    # Combine and prioritize: dollar amounts first, then standalone numbers
    amounts = dollar_amounts + standalone_amounts

    # Filter out the bot mention and get valid amounts (>= 1)
    mentions = [m for m in mentions if m != bot_user_id]
    amounts = [a for a in amounts if float(a) >= 1]

    # Sort amounts by value descending (prefer larger numbers as they're more likely the bet)
    amounts = sorted(amounts, key=lambda x: float(x), reverse=True)

    if mentions and amounts:
        # Use the largest amount found (most likely the actual bet amount)
        bet_amount = amounts[0]

        # Remove mentions and amount from text to get description
        desc = text
        for m in mentions:
            desc = re.sub(f'<@{m}>', '', desc)
        desc = re.sub(rf'\$?{re.escape(bet_amount)}', '', desc, count=1)
        desc = re.sub(r'\s+', ' ', desc).strip()
        desc = re.sub(r'^(bet|i bet|on|that|for)\s*', '', desc, flags=re.IGNORECASE).strip()

        if len(mentions) >= 2:
            return {
                'person1_id': mentions[0],
                'person2_id': mentions[1],
                'amount': f"${bet_amount}",
                'description': desc or "bet"
            }
        else:
            return {
                'person1_id': sender_id,
                'person2_id': mentions[0],
                'amount': f"${bet_amount}",
                'description': desc or "bet"
            }

    return None


def format_bet(bet, quiet=True):
    """Format a bet for display. Use quiet=True to avoid @mentions."""
    if quiet:
        return (f"*#{bet['id']}* - {bet['person1_name']} vs {bet['person2_name']} "
                f"for {bet['amount']}: {bet['description']}")
    else:
        return (f"*#{bet['id']}* - <@{bet['person1_id']}> vs <@{bet['person2_id']}> "
                f"for {bet['amount']}: {bet['description']}")


@app.event("app_mention")
def handle_mention(event, say, client):
    """Handle when the bot is mentioned."""
    text = event.get("text", "")
    channel_id = event.get("channel")
    user_id = event.get("user")

    # Get bot's user ID
    auth = client.auth_test()
    bot_user_id = auth["user_id"]

    # Clean text (remove bot mention)
    clean_text = re.sub(f'<@{bot_user_id}>', '', text).strip().lower()

    # Handle commands
    if clean_text in ("commands", "command", "cmds", "cmd"):
        say("""*Commands:*
• `list` - Open bets in this channel
• `all` - All open bets
• `mybets` - Your open bets
• `history` - Recently settled bets
• `myhistory` - Your bet history
• `balance` - Your balance & debts
• `balances` - Leaderboard
• `shame` - Wall of shame (worst records)
• `scores nba/nfl/soccer` - Sports scores
• `lines nba/nfl/mlb` - Betting odds/spreads
• `kalshi <query>` - Prediction markets
• `parlay $amt` + legs - Track a parlay
• `parlays` - Your open parlays
• `parlay <id> won/lost/delete` - Mark result or delete
• `check parlays` - Live scores for your parlays
• `props` - DARKO projections (upload CSV first)
• `injury` - NBA injury report
• `settle <id> @winner` - Settle a bet
• `cancel <id>` - Cancel a bet
• `help` - Full help""")
        return

    if clean_text == "help":
        say("""*Bet Tracker Bot Help*

*Log a bet:*
`@betbot @alice vs @bob $50 on the game`
`@betbot I bet @bob 50 Lakers win`

*Track a parlay:*
```@betbot parlay $20
Lakers ML +150
Chiefs -3 -110
Over 220.5 -110```

*Commands:*
- `@betbot list` - Show open bets in this channel
- `@betbot listall` - Show all open bets
- `@betbot history` - Show recently settled bets
- `@betbot settle <id> winner @person` - Settle a bet
- `@betbot cancel <id>` - Cancel a bet
- `@betbot scores <nba|nfl|soccer|epl>` - Show recent scores
- `@betbot check` - Auto-match bets to game results
- `@betbot lines <sport>` - Betting lines (nba/nfl/mlb/nhl/soccer)
- `@betbot kalshi <query>` - Search Kalshi prediction markets
- `@betbot parlays` - Show your open parlays
- `@betbot parlay <id> won` - Mark parlay as won
- `@betbot parlay <id> lost` - Mark parlay as lost
- `@betbot parlay <id> delete` - Delete a parlay
- `@betbot check parlays` - Live scores for your parlays
- `@betbot props` - Show DARKO projections (PTS, AST)
- `@betbot injury` - Show NBA injury report
- `@betbot balance` - Check your balance
- `@betbot balances` - Show everyone's balances
- `@betbot myhistory` - Show your bet history
- `@betbot help` - Show this help

_Tip: Upload a betting slip screenshot to track parlays, or upload DARKO CSV for projections!_""")
        return

    if clean_text in ("list", "bets", "open", "openbets", "open bets"):
        bets = get_open_bets(channel_id)
        if not bets:
            say("No open bets in this channel!")
        else:
            lines = ["*Open Bets in this channel:*"]
            for bet in bets:
                lines.append(format_bet(bet))
            say("\n".join(lines))
        return

    if clean_text in ("listall", "list all", "all", "all bets", "allbets"):
        bets = get_open_bets()
        if not bets:
            say("No open bets anywhere!")
        else:
            lines = ["*All Open Bets:*"]
            for bet in bets:
                lines.append(format_bet(bet))
            say("\n".join(lines))
        return

    if clean_text in ("history", "recent", "resolved", "past", "past bets"):
        bets = get_resolved_bets(channel_id)
        if not bets:
            say("No bet history in this channel!")
        else:
            lines = ["*Recent Bet History:*"]
            for bet in bets:
                status = bet['status']
                if status == 'settled':
                    status = f"won by <@{bet['winner_id']}>"
                lines.append(f"#{bet['id']} - {bet['person1_name']} vs {bet['person2_name']} "
                           f"for {bet['amount']}: {bet['description']} [{status}]")
            say("\n".join(lines))
        return

    # Balance command - check your own balance
    if clean_text in ("balance", "mybalance", "my balance"):
        user_balance = get_user_balance(user_id)
        balance = user_balance['balance']
        debts = get_user_debts(user_id)

        lines = []
        if balance > 0:
            lines.append(f"*You are up ${balance:.2f}*")
        elif balance < 0:
            lines.append(f"*You are down ${abs(balance):.2f}*")
        else:
            lines.append(f"*You are even*")

        # Show individual debts (use names, not @mentions)
        you_owe = []
        they_owe = []
        for other_id, data in debts.items():
            if data['amount'] > 0:
                they_owe.append(f"{data['name']} owes you ${data['amount']:.2f}")
            elif data['amount'] < 0:
                you_owe.append(f"You owe {data['name']} ${abs(data['amount']):.2f}")

        if you_owe:
            lines.append("\n" + "\n".join(you_owe))
        if they_owe:
            lines.append("\n" + "\n".join(they_owe))

        say("\n".join(lines))
        return

    # Balances/leaderboard command - show everyone
    if clean_text in ("balances", "leaderboard", "standings", "all balances"):
        balances = get_balances()
        if not balances:
            say("No settled bets yet - no balances to show!")
            return

        # Sort by balance descending
        sorted_balances = sorted(balances.items(), key=lambda x: x[1]['balance'], reverse=True)

        lines = ["*Leaderboard:*"]
        for user_id_key, data in sorted_balances:
            balance = data['balance']
            name = data['name']
            if balance > 0:
                lines.append(f"{name}: +${balance:.2f}")
            elif balance < 0:
                lines.append(f"{name}: -${abs(balance):.2f}")
            else:
                lines.append(f"{name}: $0.00")

        say("\n".join(lines))
        return

    # My open bets command
    if clean_text in ("mybets", "my bets", "myopen", "my open"):
        all_open = get_open_bets()
        my_bets = [b for b in all_open if b['person1_id'] == user_id or b['person2_id'] == user_id]

        if not my_bets:
            say("You have no open bets!")
            return

        lines = ["*Your Open Bets:*"]
        for bet in my_bets:
            lines.append(format_bet(bet))

        say("\n".join(lines))
        return

    # My history command - show user's bet history
    if clean_text in ("myhistory", "my history"):
        bets = get_user_history(user_id)
        if not bets:
            say("You have no bet history yet!")
            return

        lines = ["*Your Bet History:*"]
        wins = 0
        losses = 0
        for bet in bets:
            if bet['winner_id'] == user_id:
                result = "WON"
                wins += 1
            else:
                result = "LOST"
                losses += 1

            # Use name instead of @mention
            opponent_name = bet['person2_name'] if bet['person1_id'] == user_id else bet['person1_name']
            lines.append(f"• {result} {bet['amount']} vs {opponent_name}: {bet['description']}")

        lines.append(f"\n*Record: {wins}W - {losses}L*")
        say("\n".join(lines))
        return

    # Wall of shame - worst win percentages
    if clean_text in ("shame", "wall of shame", "wallofshame", "losers", "worst"):
        records = get_all_records()
        if not records:
            say("No settled bets yet!")
            return

        # Sort by win percentage (lowest first), require at least 2 bets
        sorted_records = sorted(
            [(uid, data) for uid, data in records.items() if data['total'] >= 2],
            key=lambda x: x[1]['win_pct']
        )

        if not sorted_records:
            say("Not enough bets to determine the wall of shame!")
            return

        lines = ["*Wall of Shame:*"]
        for i, (uid, data) in enumerate(sorted_records[:5]):
            lines.append(f"{i+1}. {data['name']}: {data['wins']}W-{data['losses']}L ({data['win_pct']:.0f}%)")

        say("\n".join(lines))
        return

    # Parlay commands
    if clean_text in ("parlay", "parlays", "myparlay", "myparlays", "my parlays"):
        parlays = get_user_parlays(user_id, status='open')
        if not parlays:
            say("You have no open parlays! Add one with:\n`@betbot parlay add $10`\nThen list your legs (one per line)")
        else:
            lines = ["*Your Open Parlays:*\n"]
            for parlay in parlays:
                lines.append(format_parlay(parlay))
                lines.append("")
            say("\n".join(lines))
        return

    if clean_text in ("parlay history", "parlays history", "parlay all"):
        parlays = get_user_parlays(user_id, status=None)
        if not parlays:
            say("You have no parlay history!")
        else:
            lines = ["*Your Parlay History:*\n"]
            for parlay in parlays[:10]:
                lines.append(format_parlay(parlay))
                lines.append("")
            say("\n".join(lines))
        return

    # Parlay add command
    parlay_add_match = re.match(r'parlay\s+(?:add|new|create)\s+\$?(\d+(?:\.\d{2})?)\s*(.*)', clean_text, re.DOTALL)
    if parlay_add_match:
        stake = parlay_add_match.group(1)
        legs_text = parlay_add_match.group(2).strip()

        if not legs_text:
            say(f"Got it! Adding a ${stake} parlay. Now reply with your legs, one per line:\n```\nLakers ML +150\nChiefs -3 -110\nOver 48.5 -110\n```")
            # Store pending parlay in memory (simple approach)
            return

        legs = parse_parlay_text(legs_text)
        if not legs:
            say("Couldn't parse any legs. Format each leg like:\n`Team/Pick +odds` or `Team/Pick -odds`")
            return

        user_name = get_user_name(client, user_id)
        parlay_id = add_parlay(user_id, user_name, channel_id, f"${stake}", legs)

        parlay = get_parlay(parlay_id)
        say(f"Parlay #{parlay_id} added!\n\n{format_parlay(parlay)}")
        return

    # Parlay with multiline (when someone posts legs after "parlay add")
    parlay_multiline_match = re.match(r'parlay\s+\$?(\d+(?:\.\d{2})?)\s*\n(.+)', text.replace(f'<@{bot_user_id}>', '').strip(), re.DOTALL | re.IGNORECASE)
    if parlay_multiline_match:
        stake = parlay_multiline_match.group(1)
        legs_text = parlay_multiline_match.group(2).strip()

        legs = parse_parlay_text(legs_text)
        if not legs:
            say("Couldn't parse any legs. Format each leg like:\n`Team/Pick +odds` or `Team/Pick -odds`")
            return

        user_name = get_user_name(client, user_id)
        parlay_id = add_parlay(user_id, user_name, channel_id, f"${stake}", legs)

        parlay = get_parlay(parlay_id)
        say(f"Parlay #{parlay_id} added!\n\n{format_parlay(parlay)}")
        return

    # Parlay won/lost/delete commands
    parlay_result_match = re.match(r'parlay\s+(\d+)\s+(won|win|lost|lose|push|pushed|delete|cancel|remove)', clean_text)
    if parlay_result_match:
        parlay_id = int(parlay_result_match.group(1))
        result = parlay_result_match.group(2).lower()

        parlay = get_parlay(parlay_id)
        if not parlay:
            say(f"Parlay #{parlay_id} not found!")
            return
        if parlay['user_id'] != user_id:
            say("You can only update your own parlays!")
            return

        if result in ('won', 'win'):
            update_parlay_status(parlay_id, 'won', parlay['potential_payout'])
            say(f"Parlay #{parlay_id} marked as WON! You won {parlay['potential_payout']}!")
        elif result in ('lost', 'lose'):
            update_parlay_status(parlay_id, 'lost')
            say(f"Parlay #{parlay_id} marked as LOST. Better luck next time!")
        elif result in ('delete', 'cancel', 'remove'):
            conn = sqlite3.connect(DATABASE)
            c = conn.cursor()
            c.execute("DELETE FROM parlays WHERE id = ?", (parlay_id,))
            conn.commit()
            conn.close()
            say(f"Parlay #{parlay_id} deleted.")
        else:
            update_parlay_status(parlay_id, 'pushed')
            say(f"Parlay #{parlay_id} marked as PUSHED.")
        return

    # Scores command
    scores_match = re.match(r'scores?\s*(\w+)?', clean_text)
    if scores_match:
        sport = scores_match.group(1) or 'nba'
        sport = sport.lower()

        if sport not in ESPN_SCOREBOARD:
            say(f"Unknown sport '{sport}'. Try: nba, nfl, soccer, epl")
            return

        games = fetch_scores(sport)
        if not games:
            say(f"Couldn't fetch {sport.upper()} scores right now.")
            return

        lines = [f"*{sport.upper()} Scores:*"]
        for game in games[:10]:
            lines.append(format_game(game))

        say("\n".join(lines))
        return

    # Betting lines/odds command
    lines_match = re.match(r'(?:lines?|odds|spread|spreads|betting)\s*(.*)', clean_text)
    if lines_match:
        query = lines_match.group(1).strip().lower() or 'nba'

        # Check if it's a sport
        if query in ODDS_SPORTS:
            games = fetch_odds(query)
            if not games:
                say(f"No games/odds found for {query.upper()}")
                return

            lines = [f"*{query.upper()} Lines:*\n"]
            for game in games:
                lines.append(format_odds(game))
                lines.append("")

            say("\n".join(lines))
            return

        # Otherwise search for team name across sports
        all_games = []
        for sport in ['nba', 'nfl', 'mlb', 'nhl']:
            games = fetch_odds(sport)
            if games:
                for game in games:
                    game['sport'] = sport.upper()
                    all_games.append(game)

        # Filter by team name
        matching = []
        for game in all_games:
            if (query in game.get('home', '').lower() or
                query in game.get('away', '').lower()):
                matching.append(game)

        if not matching:
            say(f"No games found for '{query}'. Try a team name or sport (nba, nfl, mlb, nhl)")
            return

        lines = [f"*Lines for '{query}':*\n"]
        for game in matching:
            lines.append(f"_{game.get('sport', '')}:_")
            lines.append(format_odds(game))
            lines.append("")

        say("\n".join(lines))
        return

    # Kalshi prediction market command
    kalshi_match = re.match(r'(?:kalshi|predict|prediction|market|markets)\s*(.*)', clean_text)
    if kalshi_match:
        query = kalshi_match.group(1).strip()

        if not query:
            # Show trending/popular markets
            markets = fetch_kalshi_markets(limit=100)
            if not markets:
                say("Couldn't fetch Kalshi markets right now.")
                return

            # Sort by 24h volume
            markets.sort(key=lambda m: float(m.get("volume_24h_fp", "0") or "0"), reverse=True)
            top_markets = markets[:8]

            lines = ["*Trending Prediction Markets (Kalshi):*\n"]
            for market in top_markets:
                lines.append(format_kalshi_market(market))
                lines.append("")

            say("\n".join(lines))
            return

        # Search for markets
        markets = search_kalshi_markets(query)
        if not markets:
            say(f"No prediction markets found for '{query}'. Try different keywords.")
            return

        lines = [f"*Prediction Markets for '{query}':*\n"]
        for market in markets[:8]:
            lines.append(format_kalshi_market(market))
            lines.append("")

        say("\n".join(lines))
        return

    # Check command - auto-match bets to scores
    if clean_text == "check":
        open_bets = get_open_bets(channel_id)
        if not open_bets:
            say("No open bets to check!")
            return

        # Fetch scores from all sports
        all_games = []
        for sport in ['nba', 'nfl', 'soccer', 'epl']:
            games = fetch_scores(sport)
            if games:
                all_games.extend(games)

        matches = []
        for bet in open_bets:
            game = match_bet_to_game(bet['description'], all_games)
            if game:
                matches.append((bet, game))

        if not matches:
            say("Couldn't auto-match any bets to recent games. You can settle manually with `settle <id> winner @person`")
            return

        lines = ["*Potential bet matches found:*"]
        for bet, game in matches:
            lines.append(f"\n*Bet #{bet['id']}*: {bet['description']}")
            lines.append(f"  Matched game: {game['away_team']} vs {game['home_team']}")
            lines.append(f"  Result: {game['away_score']} - {game['home_score']}, Winner: {game['winner']}")
            lines.append(f"  → To settle: `@betbot settle {bet['id']} winner @person`")

        say("\n".join(lines))
        return

    # Check parlays - show parlays with live scores
    if clean_text in ("check parlays", "parlay check", "check parlay", "parlays check"):
        parlays = get_user_parlays(user_id, status='open')
        if not parlays:
            say("You have no open parlays!")
            return

        # Fetch live scores from all sports
        all_games = []
        for sport in ['nba', 'nfl', 'mlb', 'nhl']:
            games = fetch_scores(sport)
            if games:
                all_games.extend(games)

        lines = [f"*Live Parlay Status* ({len(all_games)} games tracked)\n"]

        for parlay in parlays:
            legs = json.loads(parlay['legs']) if isinstance(parlay['legs'], str) else parlay['legs']
            lines.append(f"*Parlay #{parlay['id']}* - {parlay['user_name']}")
            if parlay.get('stake'):
                lines.append(f"Stake: {parlay['stake']} → Potential: {parlay['potential_payout']}")
            lines.append(f"Legs ({len(legs)}):")

            for i, leg in enumerate(legs, 1):
                pick = leg['pick']
                odds_str = f" ({leg.get('odds', '')})" if leg.get('odds') and leg.get('odds') != 1.0 else ""

                # Try to match to live game
                live_info = ""
                pick_lower = pick.lower()
                for game in all_games:
                    home = game.get('home_team', '').lower()
                    away = game.get('away_team', '').lower()
                    if home in pick_lower or away in pick_lower or \
                       any(word in pick_lower for word in home.split() if len(word) > 3) or \
                       any(word in pick_lower for word in away.split() if len(word) > 3):
                        score = f"{game.get('away_team', '')} {game.get('away_score', 0)} - {game.get('home_team', '')} {game.get('home_score', 0)}"
                        status = game.get('status', '')
                        live_info = f" → {score} ({status})"
                        break

                lines.append(f"  {i}. {pick}{odds_str}{live_info}")

            lines.append("")

        say("\n".join(lines))
        return

    # Props command - show DARKO projections vs prop lines
    if clean_text in ("props", "projections", "darko"):
        say("Fetching prop lines and comparing to DARKO...")

        data, error = compare_darko_to_props()

        if error:
            say(error)
            return

        lines = ["*DARKO vs Prop Lines - Biggest Edges*\n"]

        if data.get('last_updated'):
            lines.append(f"_DARKO data: {data['last_updated'].strftime('%Y-%m-%d %H:%M')}_\n")

        if data.get('props_found') and data.get('edges_pts'):
            lines.append("*POINTS - Biggest Deltas:*")
            for e in data['edges_pts'][:8]:
                delta_str = f"+{e['delta']:.1f}" if e['delta'] > 0 else f"{e['delta']:.1f}"
                lines.append(f"• {e['player']}: Line {e['line']} | DARKO {e['darko']:.1f} | *{e['edge']} ({delta_str})*")

            lines.append("\n*ASSISTS - Biggest Deltas:*")
            for e in data['edges_ast'][:8]:
                delta_str = f"+{e['delta']:.1f}" if e['delta'] > 0 else f"{e['delta']:.1f}"
                lines.append(f"• {e['player']}: Line {e['line']} | DARKO {e['darko']:.1f} | *{e['edge']} ({delta_str})*")

        elif data.get('top_pts'):
            lines.append("_(No prop lines available - showing top projections)_\n")
            lines.append("*Top Points Projections:*")
            for i, p in enumerate(data['top_pts'][:10], 1):
                lines.append(f"{i}. {p['name']} ({p['team']}) - {p['pts']:.1f} PTS")

            lines.append("\n*Top Assists Projections:*")
            for i, p in enumerate(data['top_ast'][:10], 1):
                lines.append(f"{i}. {p['name']} ({p['team']}) - {p['ast']:.1f} AST")
        else:
            lines.append("No edges found. Make sure DARKO CSV is uploaded.")

        lines.append("\n_Upload fresh DARKO CSV daily for best results_")

        say("\n".join(lines))
        return

    # Injury command - show NBA injury report
    if clean_text in ("injury", "injuries", "injury report"):
        injuries = fetch_nba_injuries()

        if not injuries:
            say("Couldn't fetch injury report.")
            return

        # Group by status
        out = [i for i in injuries if 'out' in i['status'].lower()]
        doubtful = [i for i in injuries if 'doubtful' in i['status'].lower()]
        questionable = [i for i in injuries if 'questionable' in i['status'].lower() or 'day-to-day' in i['status'].lower()]

        lines = ["*NBA Injury Report*\n"]

        if out:
            lines.append("*OUT:*")
            for i in out[:15]:
                lines.append(f"• {i['player']} ({i['team']}) - {i['injury']}")

        if doubtful:
            lines.append("\n*DOUBTFUL:*")
            for i in doubtful[:10]:
                lines.append(f"• {i['player']} ({i['team']}) - {i['injury']}")

        if questionable:
            lines.append("\n*QUESTIONABLE/DTD:*")
            for i in questionable[:15]:
                lines.append(f"• {i['player']} ({i['team']}) - {i['injury']}")

        say("\n".join(lines))
        return

    # Settle command - flexible parsing
    # Use original text (not lowercased) to preserve user ID case
    text_no_bot = re.sub(f'<@{bot_user_id}>', '', text).strip()

    # Try multiple patterns for settling
    settle_patterns = [
        r'settle\s+(\d+)\s+(?:winner\s+)?<@(\w+)>',  # settle 1 winner @person
        r'settle\s+(\d+)\s+<@(\w+)>',                 # settle 1 @person
        r'(\d+)\s+(?:winner|won|goes to)\s+<@(\w+)>', # 1 winner @person
        r'<@(\w+)>\s+(?:won|wins)\s+(?:bet\s+)?(\d+)',# @person won bet 1
        r'(?:close|resolve|end)\s+(\d+)\s+<@(\w+)>',  # close 1 @person
        r'(\d+)\s+<@(\w+)>\s+(?:won|wins)',           # 1 @person won
        r'(\d+)\s+to\s+<@(\w+)>',                     # 1 to @person
    ]

    settle_match = None
    bet_id = None
    winner_id = None

    for pattern in settle_patterns:
        match = re.search(pattern, text_no_bot, re.IGNORECASE)
        if match:
            groups = match.groups()
            # Figure out which group is the number and which is the user
            if groups[0].isdigit():
                bet_id = int(groups[0])
                winner_id = groups[1]
            else:
                winner_id = groups[0]
                bet_id = int(groups[1])
            settle_match = match
            break

    if settle_match:
        bet = get_bet(bet_id)

        if not bet:
            say(f"Bet #{bet_id} not found!")
            return
        if bet['status'] != 'open':
            say(f"Bet #{bet_id} is already {bet['status']}!")
            return
        if winner_id not in (bet['person1_id'], bet['person2_id']):
            say(f"Winner must be one of the people in the bet!")
            return

        winner_name = get_user_name(client, winner_id)
        settle_bet(bet_id, winner_id, winner_name)

        loser_id = bet['person2_id'] if winner_id == bet['person1_id'] else bet['person1_id']
        say(f"Bet #{bet_id} settled! <@{winner_id}> wins {bet['amount']} from <@{loser_id}>!")
        return

    # Cancel command
    cancel_match = re.match(r'cancel\s+(\d+)', clean_text)
    if cancel_match:
        bet_id = int(cancel_match.group(1))
        if cancel_bet(bet_id):
            say(f"Bet #{bet_id} cancelled!")
        else:
            say(f"Couldn't cancel bet #{bet_id} (not found or already resolved)")
        return

    # Try to parse as a new bet
    bet_data = parse_bet_message(text, bot_user_id, user_id)
    if bet_data:
        person1_name = get_user_name(client, bet_data['person1_id'])
        person2_name = get_user_name(client, bet_data['person2_id'])

        bet_id = add_bet(
            channel_id=channel_id,
            person1_id=bet_data['person1_id'],
            person1_name=person1_name,
            person2_id=bet_data['person2_id'],
            person2_name=person2_name,
            amount=bet_data['amount'],
            description=bet_data['description'],
            created_by=user_id
        )

        say(f"Bet #{bet_id} recorded! <@{bet_data['person1_id']}> vs <@{bet_data['person2_id']}> "
            f"for {bet_data['amount']}: {bet_data['description']}")
        return

    # Didn't understand
    say("I didn't understand that. Try `@betbot help` for usage info.")


def parse_betting_slip_ocr(ocr_text_lines):
    """Parse OCR text from a betting slip. Focuses on team names + lines."""
    legs = []

    # Known team names (partial matches OK)
    teams = [
        # NBA
        'lakers', 'celtics', 'warriors', 'bulls', 'heat', 'nets', 'knicks', 'sixers',
        'bucks', 'suns', 'mavericks', 'mavs', 'clippers', 'nuggets', 'grizzlies',
        'cavaliers', 'cavs', 'thunder', 'pelicans', 'timberwolves', 'wolves', 'kings',
        'hawks', 'hornets', 'magic', 'pacers', 'pistons', 'raptors', 'wizards',
        'spurs', 'jazz', 'trail blazers', 'blazers', 'rockets',
        # NFL
        'chiefs', 'eagles', 'cowboys', 'bills', 'ravens', '49ers', 'niners', 'dolphins',
        'lions', 'packers', 'bengals', 'chargers', 'seahawks', 'steelers', 'rams',
        'vikings', 'jaguars', 'jags', 'texans', 'colts', 'broncos', 'raiders', 'saints',
        'patriots', 'pats', 'bears', 'falcons', 'cardinals', 'giants', 'jets', 'titans',
        'panthers', 'browns', 'commanders', 'buccaneers', 'bucs',
        # MLB
        'yankees', 'dodgers', 'braves', 'astros', 'mets', 'phillies', 'padres',
        'mariners', 'blue jays', 'orioles', 'rays', 'twins', 'guardians', 'rangers',
        'red sox', 'white sox', 'cubs', 'brewers', 'cardinals', 'diamondbacks', 'dbacks',
        'giants', 'reds', 'pirates', 'royals', 'tigers', 'athletics', 'angels', 'rockies', 'marlins', 'nationals',
        # NHL
        'bruins', 'avalanche', 'panthers', 'oilers', 'rangers', 'hurricanes', 'devils',
        'maple leafs', 'leafs', 'lightning', 'stars', 'jets', 'wild', 'golden knights',
        'knights', 'flames', 'kraken', 'penguins', 'pens', 'capitals', 'caps', 'canucks',
        'islanders', 'isles', 'kings', 'blackhawks', 'hawks', 'blues', 'senators', 'sens',
        'sabres', 'red wings', 'wings', 'ducks', 'coyotes', 'predators', 'preds', 'sharks',
        # Soccer
        'arsenal', 'chelsea', 'liverpool', 'man city', 'manchester city', 'man united',
        'manchester united', 'tottenham', 'spurs', 'barcelona', 'real madrid', 'bayern',
        'psg', 'juventus', 'inter', 'milan', 'dortmund', 'ajax', 'benfica', 'porto',
    ]

    for line in ocr_text_lines:
        line = line.strip()
        if len(line) < 3:
            continue

        line_lower = line.lower()

        # Look for team name + line pattern (e.g., "Lakers +3", "Chiefs -7.5", "Celtics ML")
        bet_pattern = re.search(
            r'([A-Za-z][A-Za-z\s\.\']+?)\s*([+-]?\d+\.?\d*|ML|ml|moneyline|over|under|o\d+\.?\d*|u\d+\.?\d*)\s*([+-]\d{2,3})?',
            line, re.IGNORECASE
        )

        if bet_pattern:
            potential_team = bet_pattern.group(1).strip().lower()
            line_info = bet_pattern.group(2).strip()
            odds = bet_pattern.group(3)

            # Check if this matches a known team
            team_match = None
            for team in teams:
                if team in potential_team or potential_team in team:
                    team_match = potential_team.title()
                    break

            if team_match:
                pick = f"{team_match} {line_info}"
                odds_val = parse_odds(odds) if odds else 1.0

                # Avoid duplicates
                if not any(team_match.lower() in leg['pick'].lower() for leg in legs):
                    legs.append({
                        'pick': pick,
                        'odds': odds_val
                    })
                continue

        # Also check for over/under totals (e.g., "Over 220.5", "Under 45")
        total_pattern = re.search(
            r'(over|under|o|u)\s*(\d+\.?\d*)\s*([+-]\d{2,3})?',
            line, re.IGNORECASE
        )

        if total_pattern:
            ou_type = total_pattern.group(1).upper()
            if ou_type in ('O', 'U'):
                ou_type = 'Over' if ou_type == 'O' else 'Under'
            total_num = total_pattern.group(2)
            odds = total_pattern.group(3)

            pick = f"{ou_type} {total_num}"
            odds_val = parse_odds(odds) if odds else 1.0

            # Avoid duplicates
            if not any(pick.lower() in leg['pick'].lower() for leg in legs):
                legs.append({
                    'pick': pick,
                    'odds': odds_val
                })

    return legs


@app.event("message")
def handle_message(event, say, client):
    """Handle regular messages, including file uploads."""
    # Check if this message has files
    files = event.get("files", [])
    text = event.get("text", "")
    user_id = event.get("user")
    channel_id = event.get("channel")
    subtype = event.get("subtype")

    # Log for debugging
    if files:
        logger.info(f"Message with files received: {len(files)} files, text: {text[:50] if text else 'none'}")

    # Skip bot messages and message_changed events
    if subtype in ("bot_message", "message_changed", "message_deleted"):
        return

    if not files:
        return

    # Any image upload we'll try to process as a betting slip
    # User can include stake in message like "$20" or "parlay $50"
    logger.info(f"Processing file upload from {user_id}")

    # Look for stake amount in the message
    stake_match = re.search(r'\$(\d+(?:\.\d{2})?)', text)
    stake = stake_match.group(1) if stake_match else "10"

    # Process CSV files (DARKO projections)
    for file_info in files:
        file_name = file_info.get("name", "")
        if file_name.lower().endswith('.csv') or file_info.get("mimetype") == "text/csv":
            try:
                file_url = file_info.get("url_private_download") or file_info.get("url_private")
                if not file_url:
                    continue

                headers = {"Authorization": f"Bearer {os.environ.get('SLACK_BOT_TOKEN')}"}
                resp = requests.get(file_url, headers=headers, timeout=30)

                if resp.status_code != 200:
                    say("Couldn't download the CSV file.")
                    continue

                csv_content = resp.content.decode('utf-8')

                # Check if it looks like DARKO data
                if 'Player' in csv_content and 'PTS' in csv_content:
                    count = parse_darko_csv(csv_content)
                    say(f"✅ DARKO projections loaded!\n"
                        f"Parsed {count} players.\n\n"
                        f"Use `@betbot props` to see top projections.")
                else:
                    say("This doesn't look like DARKO data. "
                        "Expected columns: Player, Team, PTS, AST, etc.")
                return

            except Exception as e:
                logger.error(f"Error processing CSV: {e}")
                say("Error processing CSV file.")
                return

    # Process image files
    for file_info in files:
        if file_info.get("mimetype", "").startswith("image/"):
            try:
                # Download the file
                file_url = file_info.get("url_private_download") or file_info.get("url_private")
                if not file_url:
                    continue

                headers = {"Authorization": f"Bearer {os.environ.get('SLACK_BOT_TOKEN')}"}
                resp = requests.get(file_url, headers=headers, timeout=30)

                if resp.status_code != 200:
                    say("Couldn't download the image. Make sure I have file access permissions.")
                    continue

                # Run OCR on the image
                say("Reading your betting slip... (this may take a moment)")

                try:
                    from PIL import Image
                    image = Image.open(io.BytesIO(resp.content))

                    # Get OCR reader and process image
                    reader = get_ocr_reader()
                    results = reader.readtext(resp.content)

                    # Extract text from OCR results
                    ocr_lines = [text for (_, text, conf) in results if conf > 0.3]

                    if not ocr_lines:
                        say("Couldn't read any text from the image. Try a clearer screenshot or type out your parlay.")
                        return

                    # Parse the OCR text into legs
                    legs = parse_betting_slip_ocr(ocr_lines)

                    if not legs:
                        # Show what we found and ask user to format it
                        ocr_text = "\n".join(ocr_lines[:20])  # First 20 lines
                        say(f"Here's what I read from the slip:\n```\n{ocr_text}\n```\n\n"
                            f"I couldn't auto-parse the legs. Please type them out:\n"
                            f"`@betbot parlay ${stake}`\n```\nPick1 +odds\nPick2 -odds\n```")
                        return

                    # Create the parlay
                    user_name = get_user_name(client, user_id)
                    parlay_id = add_parlay(user_id, user_name, channel_id, f"${stake}", legs, source="screenshot")

                    parlay = get_parlay(parlay_id)
                    say(f"Parlay #{parlay_id} created from your screenshot!\n\n{format_parlay(parlay)}\n\n"
                        f"_If any legs are wrong, cancel with `@betbot parlay {parlay_id} cancel` and re-enter manually._")
                    return

                except Exception as ocr_error:
                    logger.error(f"OCR error: {ocr_error}")
                    say(f"Had trouble reading the image. Please type out your parlay:\n"
                        f"`@betbot parlay ${stake}`\n```\nPick1 +odds\nPick2 -odds\n```")
                    return

            except Exception as e:
                logger.error(f"Error processing file: {e}")
                say("Had trouble processing that image. Try typing out your parlay instead.")
                return


@app.event("file_shared")
def handle_file_shared(event, logger):
    """Handle file shared events (prevents errors)."""
    pass


def main():
    """Main entry point."""
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    app_token = os.environ.get("SLACK_APP_TOKEN")

    if not bot_token:
        print("Error: SLACK_BOT_TOKEN environment variable not set")
        print("Get a token from https://api.slack.com/apps")
        return

    if not app_token:
        print("Error: SLACK_APP_TOKEN environment variable not set")
        print("Enable Socket Mode in your Slack app and get an app-level token")
        return

    init_db()
    logger.info("Database initialized")

    print("Bet Tracker bot starting...")
    handler = SocketModeHandler(app, app_token)
    handler.start()


if __name__ == "__main__":
    main()
