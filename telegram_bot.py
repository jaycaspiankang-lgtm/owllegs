#!/usr/bin/env python3
"""
Telegram Bet Tracker Bot - Tracks bets and parlays.

Usage:
    export TELEGRAM_BOT_TOKEN="your-token"
    python telegram_bot.py

Commands:
    /start - Welcome message
    /help - Show help
    /parlay $amount - Start a parlay (then send legs)
    /parlays - Show your open parlays
    /parlay_won <id> - Mark parlay as won
    /parlay_lost <id> - Mark parlay as lost
    /bet @user $amount description - Log a bet
    /bets - Show open bets
    /settle <id> @winner - Settle a bet
    /scores <sport> - Show scores (nba, nfl, etc.)
    /lines <sport> - Show betting lines

Or just upload a screenshot of your betting slip!
"""

import os
import re
import io
import sqlite3
import logging
import json
import requests
from datetime import datetime
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Database
DATABASE = os.path.expanduser("~/Documents/owllegs/telegram_bets.db")

# Lazy load OCR
_ocr_reader = None


def get_ocr_reader():
    """Get or initialize the OCR reader."""
    global _ocr_reader
    if _ocr_reader is None:
        import easyocr
        _ocr_reader = easyocr.Reader(['en'], gpu=False)
    return _ocr_reader


def init_db():
    """Initialize the database."""
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
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
            chat_id TEXT,
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


def parse_odds(odds_str):
    """Parse American or decimal odds to decimal multiplier."""
    odds_str = str(odds_str).strip()

    if '.' in odds_str and not odds_str.startswith(('+', '-')):
        try:
            return float(odds_str)
        except:
            pass

    try:
        odds_int = int(odds_str.replace('+', ''))
        if odds_int > 0:
            return 1 + (odds_int / 100)
        else:
            return 1 + (100 / abs(odds_int))
    except:
        pass

    return 1.0


def parse_parlay_text(text):
    """Parse parlay legs from text input. Very forgiving parser."""
    legs = []

    # Normalize the text
    text = text.strip()

    # Split by newlines first
    lines = text.split('\n')

    # If only one line, try splitting by commas or common separators
    if len(lines) == 1:
        # Check for comma-separated
        if ',' in text:
            lines = [l.strip() for l in text.split(',')]
        # Check for semicolon-separated
        elif ';' in text:
            lines = [l.strip() for l in text.split(';')]

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Skip commands and comments
        if line.startswith('/') or line.startswith('#'):
            continue

        # Remove common prefixes: numbers, bullets, dashes
        line = re.sub(r'^[\d]+[.\)]\s*', '', line)  # "1. " or "1) "
        line = re.sub(r'^[-•*]\s*', '', line)  # "- " or "• " or "* "
        line = re.sub(r'^leg\s*\d*:?\s*', '', line, flags=re.IGNORECASE)  # "Leg 1:" etc

        line = line.strip()
        if not line:
            continue

        # Skip obvious non-picks
        skip_words = ['parlay', 'total', 'wager', 'stake', 'bet', 'slip']
        if any(line.lower() == word for word in skip_words):
            continue

        leg = {'pick': line, 'odds': 1.0}

        # Try to extract odds from various formats
        odds_patterns = [
            r'([+-]\d{3})\s*$',  # American odds: +150, -110
            r'([+-]\d+)\s*$',  # Shorter American: +15, -11
            r'@\s*([+-]?\d+\.?\d*)\s*$',  # @ 1.95
            r'\(([+-]?\d+\.?\d*)\)\s*$',  # (1.95) or (+150)
            r'\s(\d+\.\d{2})\s*$',  # Decimal: 1.95
        ]

        for pattern in odds_patterns:
            match = re.search(pattern, line)
            if match:
                odds_str = match.group(1)
                leg['odds'] = parse_odds(odds_str)
                leg['pick'] = line[:match.start()].strip()
                break

        # Clean up the pick text
        pick = leg['pick'].strip()

        # Remove trailing punctuation
        pick = re.sub(r'[,;:]+$', '', pick).strip()

        if pick and len(pick) >= 2:
            leg['pick'] = pick
            legs.append(leg)

    return legs


def add_parlay(user_id, user_name, chat_id, legs, stake=None, source="manual"):
    """Add a new parlay."""
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()

    total_odds = 1.0
    for leg in legs:
        odds = leg.get('odds', 1.0)
        total_odds *= odds

    potential_payout = ""
    if stake:
        try:
            stake_float = float(str(stake).replace('$', '').replace(',', ''))
            potential_payout = f"${stake_float * total_odds:.2f}"
        except:
            pass

    c.execute("""
        INSERT INTO parlays (user_id, user_name, chat_id, stake, total_odds,
                            potential_payout, legs, status, created_at, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
    """, (str(user_id), user_name, str(chat_id), str(stake) if stake else None, f"{total_odds:.2f}",
          potential_payout, json.dumps(legs), datetime.now().isoformat(), source))
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
                  (str(user_id), status))
    else:
        c.execute("SELECT * FROM parlays WHERE user_id = ? ORDER BY created_at DESC", (str(user_id),))
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
    """Update parlay status."""
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("""
        UPDATE parlays SET status = ?, result = ?, resolved_at = ?
        WHERE id = ?
    """, (status, result, datetime.now().isoformat(), parlay_id))
    conn.commit()
    conn.close()


def format_parlay(parlay, live_data=None):
    """Format a parlay for display, optionally with live scores."""
    legs = json.loads(parlay['legs']) if isinstance(parlay['legs'], str) else parlay['legs']

    lines = [f"*Parlay #{parlay['id']}* - {parlay['user_name']}"]

    if parlay.get('stake'):
        lines.append(f"Stake: {parlay['stake']} → Potential: {parlay['potential_payout']}")

    lines.append(f"Legs ({len(legs)}):")

    for i, leg in enumerate(legs, 1):
        pick = leg['pick']
        odds_str = f" ({leg.get('odds', '')})" if leg.get('odds') and leg.get('odds') != 1.0 else ""

        # Check for live score data
        live_info = ""
        if live_data:
            for game in live_data:
                # Match by team name in pick
                home = game.get('home', '').lower()
                away = game.get('away', '').lower()
                pick_lower = pick.lower()

                if home in pick_lower or away in pick_lower or \
                   any(word in pick_lower for word in home.split()) or \
                   any(word in pick_lower for word in away.split()):
                    score = game.get('score', '')
                    status = game.get('status', '')
                    if score:
                        live_info = f" → {score} ({status})"
                    elif status:
                        live_info = f" → {status}"
                    break

        lines.append(f"  {i}. {pick}{odds_str}{live_info}")

    status = parlay['status']
    if status == 'won':
        lines.append(f"\n*WON!*")
    elif status == 'lost':
        lines.append(f"\n*LOST*")
    elif status == 'open' and live_data:
        lines.append(f"\n_Live tracking enabled_")

    return "\n".join(lines)


def fetch_all_live_games():
    """Fetch live games from all major sports."""
    all_games = []

    for sport, url in ESPN_SCOREBOARD.items():
        try:
            resp = requests.get(url, timeout=10)
            data = resp.json()

            for event in data.get('events', []):
                competition = event.get('competitions', [{}])[0]
                competitors = competition.get('competitors', [])

                if len(competitors) >= 2:
                    home = competitors[0]
                    away = competitors[1]

                    status_data = event.get('status', {}).get('type', {})

                    game = {
                        'sport': sport,
                        'home': home.get('team', {}).get('displayName', ''),
                        'home_abbrev': home.get('team', {}).get('abbreviation', ''),
                        'away': away.get('team', {}).get('displayName', ''),
                        'away_abbrev': away.get('team', {}).get('abbreviation', ''),
                        'home_score': home.get('score', '0'),
                        'away_score': away.get('score', '0'),
                        'status': status_data.get('shortDetail', ''),
                        'state': status_data.get('state', ''),  # pre, in, post
                        'completed': status_data.get('completed', False),
                    }
                    game['score'] = f"{game['away_abbrev']} {game['away_score']} - {game['home_abbrev']} {game['home_score']}"
                    all_games.append(game)
        except Exception as e:
            logger.error(f"Error fetching {sport} scores: {e}")

    return all_games


# ESPN API
ESPN_SCOREBOARD = {
    'nba': 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',
    'nfl': 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard',
    'mlb': 'https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard',
    'nhl': 'https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard',
}

ODDS_SPORTS = {
    'nba': 'basketball/nba',
    'nfl': 'football/nfl',
    'mlb': 'baseball/mlb',
    'nhl': 'hockey/nhl',
    'ncaab': 'basketball/mens-college-basketball',
    'ncaaf': 'football/college-football',
    'soccer': 'soccer/usa.1',
    'mls': 'soccer/usa.1',
    'epl': 'soccer/eng.1',
    'laliga': 'soccer/esp.1',
    'bundesliga': 'soccer/ger.1',
    'seriea': 'soccer/ita.1',
    'ligue1': 'soccer/fra.1',
    'ucl': 'soccer/uefa.champions',
}


def fetch_scores(sport):
    """Fetch scores from ESPN."""
    url = ESPN_SCOREBOARD.get(sport.lower())
    if not url:
        return None

    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        games = []

        for event in data.get('events', [])[:8]:
            competition = event.get('competitions', [{}])[0]
            competitors = competition.get('competitors', [])

            if len(competitors) >= 2:
                home = competitors[0]
                away = competitors[1]

                status = event.get('status', {}).get('type', {}).get('shortDetail', '')
                game = f"{away.get('team', {}).get('abbreviation', '')} {away.get('score', '0')} @ {home.get('team', {}).get('abbreviation', '')} {home.get('score', '0')} ({status})"
                games.append(game)

        return games
    except Exception as e:
        logger.error(f"Error fetching scores: {e}")
        return None


def fetch_odds(sport, limit=None):
    """Fetch betting odds from ESPN."""
    sport_path = ODDS_SPORTS.get(sport.lower())
    if not sport_path:
        return None

    try:
        url = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/scoreboard"
        params = {}

        # For college sports, get all games (not just top 25)
        if sport.lower() in ('ncaab', 'ncaaf'):
            params['groups'] = '50'  # All D1 games
            params['limit'] = '100'

        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        games = []

        # For college sports show more games, otherwise limit to 8
        max_games = limit or (50 if sport.lower() in ('ncaab', 'ncaaf') else 8)

        for event in data.get("events", [])[:max_games]:
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
            }

            # Get odds from competition
            odds_list = competition.get("odds", [])
            if odds_list:
                odds = odds_list[0]
                if odds.get("spread"):
                    spread_val = odds.get("spread", 0)
                    game_info["spread"] = f"{float(spread_val):+.1f}"

                if odds.get("overUnder"):
                    game_info["total"] = odds.get("overUnder")

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
                'markets': 'player_points,player_assists,player_rebounds',
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
                                if 'points' in market_key:
                                    prop_type = 'pts'
                                elif 'assists' in market_key:
                                    prop_type = 'ast'
                                elif 'rebounds' in market_key:
                                    prop_type = 'reb'
                                else:
                                    continue
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
    import csv
    from io import StringIO

    projections = {}
    reader = csv.DictReader(StringIO(csv_content))

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
                detail = player.get('details', {}).get('detail', '')

                injuries.append({
                    'player': name,
                    'team': team_name,
                    'status': status,
                    'injury': injury_type,
                    'detail': detail
                })

        return injuries
    except Exception as e:
        logger.error(f"Error fetching injuries: {e}")
        return None


def fetch_nba_props():
    """Fetch NBA player props from ESPN/odds sources."""
    try:
        # ESPN doesn't have great prop data, but we can try to get player stats
        # For now, return sample structure - would need a proper props API
        url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
        resp = requests.get(url, timeout=15)
        data = resp.json()

        props = []
        for event in data.get('events', []):
            competition = event.get('competitions', [{}])[0]
            odds_list = competition.get('odds', [])

            # ESPN doesn't expose player props in the main API
            # We'd need a dedicated props source

        return props
    except Exception as e:
        logger.error(f"Error fetching props: {e}")
        return []


def compare_darko_to_props(props_data=None):
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
            'edges_reb': [],
            'last_updated': _darko_last_updated,
            'props_found': False
        }, None

    # Compare props to DARKO and find edges
    edges_pts = []
    edges_ast = []
    edges_reb = []

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
            elif prop_type == 'reb':
                darko_proj = darko['reb']
                delta = darko_proj - line
                edges_reb.append({
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
    edges_reb.sort(key=lambda x: abs(x['delta']), reverse=True)

    return {
        'edges_pts': edges_pts[:10],
        'edges_ast': edges_ast[:10],
        'edges_reb': edges_reb[:10],
        'last_updated': _darko_last_updated,
        'props_found': True
    }, None


def parse_betting_slip_ocr(ocr_lines):
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

    # Combine all OCR text to search through
    full_text = ' '.join(ocr_lines)

    for line in ocr_lines:
        line = line.strip()
        if len(line) < 3:
            continue

        line_lower = line.lower()

        # Look for team name + line pattern (e.g., "Lakers +3", "Chiefs -7.5", "Celtics ML")
        # Pattern: Team name followed by spread/ML/over/under
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
                # Build the pick string
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


# Telegram command handlers

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    await update.message.reply_text(
        "Welcome to Bet Tracker Bot!\n\n"
        "Track your parlays and bets:\n"
        "• /parlay $20 - then send your legs\n"
        "• /parlays - view your parlays\n"
        "• /scores nba - check scores\n"
        "• Upload a betting slip screenshot!\n\n"
        "Type /help for all commands."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    await update.message.reply_text(
        "*Parlay Tracker Bot*\n\n"
        "*Track Your Parlays:*\n"
        "/parlay - Start a parlay, then send legs:\n"
        "```\nLakers ML\nChiefs -3\nOver 220\n```\n"
        "/check - See live scores for your picks!\n"
        "/parlays - Your open parlays\n"
        "/parlay\\_won <id> - Mark as won\n"
        "/parlay\\_lost <id> - Mark as lost\n\n"
        "*Scores & Lines:*\n"
        "/scores nba - Live scores\n"
        "/lines nba - Betting lines/odds\n"
        "/lines lakers - Search team\n\n"
        "*Props & Projections:*\n"
        "/props - DARKO projections (PTS, AST)\n"
        "/injury - NBA injury report\n"
        "Upload DARKO CSV to update projections\n\n"
        "*Screenshots:*\n"
        "Upload a betting slip image to track it!\n",
        parse_mode='Markdown'
    )


async def parlay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /parlay command - can include picks inline."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    # Get full message text after /parlay
    full_text = update.message.text
    # Remove the /parlay command itself
    picks_text = re.sub(r'^/parlay\s*', '', full_text, flags=re.IGNORECASE).strip()

    # If picks were included with the command, create the parlay
    if picks_text:
        legs = parse_parlay_text(picks_text)

        if legs:
            parlay_id = add_parlay(user.id, user.first_name, chat_id, legs)
            parlay = get_parlay(parlay_id)
            live_games = fetch_all_live_games()

            await update.message.reply_text(
                f"✅ *Parlay #{parlay_id} registered!*\n\n"
                f"{format_parlay(parlay, live_data=live_games)}\n\n"
                f"/check for live updates",
                parse_mode='Markdown'
            )
            return

    # No picks provided, show help
    await update.message.reply_text(
        "Just send me your picks!\n\n"
        "Examples:\n"
        "`Lakers ML, Chiefs -3, Over 220`\n\n"
        "Or:\n"
        "```\nLakers ML\nChiefs -3\nOver 220\n```\n\n"
        "I'll auto-create a parlay from your picks.",
        parse_mode='Markdown'
    )


async def parlays_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /parlays command."""
    user_id = update.effective_user.id
    parlays = get_user_parlays(user_id)

    if not parlays:
        await update.message.reply_text("You have no open parlays! Create one with /parlay")
        return

    lines = ["*Your Open Parlays:*\n"]
    for parlay in parlays:
        lines.append(format_parlay(parlay))
        lines.append("")

    lines.append("_Use /check to see live scores!_")
    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /check command - show parlays with live scores."""
    user_id = update.effective_user.id

    # Check specific parlay or all
    parlay_id = None
    if context.args:
        try:
            parlay_id = int(context.args[0])
        except ValueError:
            pass

    await update.message.reply_text("Fetching live scores...")

    # Get live games
    live_games = fetch_all_live_games()

    if parlay_id:
        parlay = get_parlay(parlay_id)
        if not parlay:
            await update.message.reply_text(f"Parlay #{parlay_id} not found!")
            return
        parlays = [parlay]
    else:
        parlays = get_user_parlays(user_id)

    if not parlays:
        await update.message.reply_text("You have no open parlays!")
        return

    lines = [f"*Live Parlay Status* ({len(live_games)} games tracked)\n"]

    for parlay in parlays:
        lines.append(format_parlay(parlay, live_data=live_games))
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')


async def parlay_won(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /parlay_won command."""
    if not context.args:
        await update.message.reply_text("Usage: /parlay_won <id>")
        return

    try:
        parlay_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid parlay ID")
        return

    parlay = get_parlay(parlay_id)
    if not parlay:
        await update.message.reply_text(f"Parlay #{parlay_id} not found!")
        return

    if str(parlay['user_id']) != str(update.effective_user.id):
        await update.message.reply_text("You can only update your own parlays!")
        return

    update_parlay_status(parlay_id, 'won', parlay['potential_payout'])
    await update.message.reply_text(f"Parlay #{parlay_id} marked as WON! You won {parlay['potential_payout']}!")


async def parlay_lost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /parlay_lost command."""
    if not context.args:
        await update.message.reply_text("Usage: /parlay_lost <id>")
        return

    try:
        parlay_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid parlay ID")
        return

    parlay = get_parlay(parlay_id)
    if not parlay:
        await update.message.reply_text(f"Parlay #{parlay_id} not found!")
        return

    if str(parlay['user_id']) != str(update.effective_user.id):
        await update.message.reply_text("You can only update your own parlays!")
        return

    update_parlay_status(parlay_id, 'lost')
    await update.message.reply_text(f"Parlay #{parlay_id} marked as LOST. Better luck next time!")


async def parlay_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /parlay_delete command."""
    if not context.args:
        await update.message.reply_text("Usage: /parlay_delete <id>")
        return

    try:
        parlay_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid parlay ID")
        return

    parlay = get_parlay(parlay_id)
    if not parlay:
        await update.message.reply_text(f"Parlay #{parlay_id} not found!")
        return

    if str(parlay['user_id']) != str(update.effective_user.id):
        await update.message.reply_text("You can only delete your own parlays!")
        return

    # Delete from database
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("DELETE FROM parlays WHERE id = ?", (parlay_id,))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"Parlay #{parlay_id} deleted.")


async def scores_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /scores command."""
    sport = context.args[0].lower() if context.args else 'nba'

    if sport not in ESPN_SCOREBOARD:
        await update.message.reply_text(f"Unknown sport. Try: nba, nfl, mlb, nhl")
        return

    games = fetch_scores(sport)
    if not games:
        await update.message.reply_text(f"Couldn't fetch {sport.upper()} scores.")
        return

    lines = [f"*{sport.upper()} Scores:*\n"]
    lines.extend(games)

    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')


async def lines_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /lines command for betting odds."""
    query = context.args[0].lower() if context.args else 'nba'

    if query in ODDS_SPORTS:
        games = fetch_odds(query)
        if not games:
            await update.message.reply_text(f"No games/odds found for {query.upper()}")
            return

        # Build message, split if too long (Telegram 4096 char limit)
        header = f"*{query.upper()} Lines ({len(games)} games):*\n\n"
        messages = []
        current_msg = header

        for game in games:
            game_text = format_odds(game) + "\n\n"
            if len(current_msg) + len(game_text) > 3900:
                messages.append(current_msg)
                current_msg = ""
            current_msg += game_text

        if current_msg:
            messages.append(current_msg)

        for msg in messages:
            await update.message.reply_text(msg, parse_mode='Markdown')

        return

    # Search for team name across sports
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
        await update.message.reply_text(f"No games found for '{query}'. Try a team name or sport (nba, nfl, mlb, nhl)")
        return

    lines = [f"*Lines for '{query}':*\n"]
    for game in matching:
        lines.append(f"_{game.get('sport', '')}:_")
        lines.append(format_odds(game))
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')


async def props_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /props command - show DARKO projections vs prop lines."""
    await update.message.reply_text("Fetching prop lines and comparing to DARKO...")

    data, error = compare_darko_to_props()

    if error:
        await update.message.reply_text(error)
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

        if data.get('edges_reb'):
            lines.append("\n*REBOUNDS - Biggest Deltas:*")
            for e in data['edges_reb'][:8]:
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

    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')


async def injury_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /injury command - show NBA injury report."""
    await update.message.reply_text("Fetching injury report...")

    injuries = fetch_nba_injuries()

    if not injuries:
        await update.message.reply_text("Couldn't fetch injury report.")
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

    # Telegram message limit
    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n..."

    await update.message.reply_text(msg, parse_mode='Markdown')


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle document uploads (DARKO CSV)."""
    document = update.message.document
    if not document:
        return

    file_name = document.file_name or ""

    # Check if it's a CSV
    if not file_name.lower().endswith('.csv'):
        return

    logger.info(f"CSV upload received: {file_name}")

    try:
        file = await context.bot.get_file(document.file_id)
        file_bytes = await file.download_as_bytearray()
        csv_content = file_bytes.decode('utf-8')

        # Check if it looks like DARKO data
        if 'Player' in csv_content and 'PTS' in csv_content:
            count = parse_darko_csv(csv_content)
            await update.message.reply_text(
                f"✅ DARKO projections loaded!\n"
                f"Parsed {count} players.\n\n"
                f"Use /props to see top projections."
            )
        else:
            await update.message.reply_text(
                "This doesn't look like DARKO data. "
                "Expected columns: Player, Team, PTS, AST, etc."
            )

    except Exception as e:
        logger.error(f"Error processing CSV: {e}")
        await update.message.reply_text("Error processing CSV file.")


def looks_like_picks(text):
    """Check if text looks like betting picks."""
    text_lower = text.lower()

    # Common betting keywords
    pick_keywords = [
        'ml', 'moneyline', 'spread', 'over', 'under', 'o/u',
        'pts', 'points', 'win', 'cover', '+', '-',
        'lakers', 'celtics', 'warriors', 'bulls', 'heat', 'nets',  # NBA
        'chiefs', 'eagles', 'cowboys', 'bills', 'ravens', '49ers',  # NFL
        'yankees', 'dodgers', 'braves', 'astros', 'mets',  # MLB
    ]

    # Check for keywords
    if any(kw in text_lower for kw in pick_keywords):
        return True

    # Multiple lines often means picks
    if text.count('\n') >= 1:
        return True

    # Comma-separated items
    if text.count(',') >= 1:
        return True

    return False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle regular text messages - auto-detect picks."""
    text = update.message.text
    user = update.effective_user
    chat_id = update.effective_chat.id

    # Try to parse as picks
    legs = parse_parlay_text(text)

    # If we got at least one leg, create a parlay
    if legs and len(legs) >= 1:
        parlay_id = add_parlay(user.id, user.first_name, chat_id, legs)
        parlay = get_parlay(parlay_id)
        live_games = fetch_all_live_games()

        await update.message.reply_text(
            f"✅ *Parlay #{parlay_id} registered!*\n\n"
            f"{format_parlay(parlay, live_data=live_games)}\n\n"
            f"/check for live updates",
            parse_mode='Markdown'
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo uploads (betting slip screenshots)."""
    logger.info("Photo received!")
    user = update.effective_user
    chat_id = update.effective_chat.id

    await update.message.reply_text("Reading your betting slip... (this may take a moment)")

    try:
        # Get the largest photo
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)

        # Download the image
        image_bytes = await file.download_as_bytearray()

        # Run OCR
        reader = get_ocr_reader()
        results = reader.readtext(bytes(image_bytes))

        ocr_lines = [text for (_, text, conf) in results if conf > 0.3]

        if not ocr_lines:
            await update.message.reply_text(
                "Couldn't read any text from the image.\n"
                "Try /parlay $20 and type your legs manually."
            )
            return

        # Parse legs
        legs = parse_betting_slip_ocr(ocr_lines)

        if not legs:
            ocr_text = "\n".join(ocr_lines[:15])
            await update.message.reply_text(
                f"Here's what I read:\n```\n{ocr_text}\n```\n\n"
                f"Couldn't auto-parse legs. Use /parlay $20 and type them.",
                parse_mode='Markdown'
            )
            return

        # Look for stake in caption (optional)
        caption = update.message.caption or ""
        stake_match = re.search(r'\$(\d+(?:\.\d{2})?)', caption)
        stake = stake_match.group(1) if stake_match else None

        # Create parlay
        parlay_id = add_parlay(user.id, user.first_name, chat_id, legs, stake=stake, source="screenshot")
        parlay = get_parlay(parlay_id)

        # Get live scores for confirmation
        live_games = fetch_all_live_games()

        await update.message.reply_text(
            f"✅ *Parlay #{parlay_id} registered!*\n\n"
            f"{format_parlay(parlay, live_data=live_games)}\n\n"
            f"/check for live updates\n"
            f"/parlay\\_won {parlay_id} or /parlay\\_lost {parlay_id} when done",
            parse_mode='Markdown'
        )

    except Exception as e:
        logger.error(f"Error processing photo: {e}")
        await update.message.reply_text(
            "Had trouble reading the image.\n"
            "Try /parlay $20 and type your legs."
        )


def main():
    """Main entry point."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")

    if not token:
        print("Error: TELEGRAM_BOT_TOKEN environment variable not set")
        print("Get a token from @BotFather on Telegram")
        return

    init_db()
    logger.info("Database initialized")

    # Create application
    app = Application.builder().token(token).build()

    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("parlay", parlay_command))
    app.add_handler(CommandHandler("parlays", parlays_command))
    app.add_handler(CommandHandler("parlay_won", parlay_won))
    app.add_handler(CommandHandler("parlay_lost", parlay_lost))
    app.add_handler(CommandHandler("parlay_delete", parlay_delete))
    app.add_handler(CommandHandler("scores", scores_command))
    app.add_handler(CommandHandler("lines", lines_command))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CommandHandler("props", props_command))
    app.add_handler(CommandHandler("injury", injury_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    print("Telegram Bet Tracker bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
