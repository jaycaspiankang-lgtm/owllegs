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
from datetime import datetime, timedelta
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Database
DATABASE = "/Users/jaykang/Documents/slackbot/bets.db"


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
• `settle <id> @winner` - Settle a bet
• `cancel <id>` - Cancel a bet
• `help` - Full help""")
        return

    if clean_text == "help":
        say("""*Bet Tracker Bot Help*

*Log a bet:*
`@betbot @alice vs @bob $50 on the game`
`@betbot I bet @bob 50 Lakers win`

*Commands:*
- `@betbot list` - Show open bets in this channel
- `@betbot listall` - Show all open bets
- `@betbot history` - Show recently settled bets
- `@betbot settle <id> winner @person` - Settle a bet
- `@betbot cancel <id>` - Cancel a bet
- `@betbot scores <nba|nfl|soccer|epl>` - Show recent scores
- `@betbot check` - Auto-match bets to game results
- `@betbot lines <sport>` - Betting lines (nba/nfl/mlb/nhl/soccer)
- `@betbot balance` - Check your balance
- `@betbot balances` - Show everyone's balances
- `@betbot myhistory` - Show your bet history
- `@betbot help` - Show this help""")
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


@app.event("message")
def handle_message(event, logger):
    """Handle regular messages (for logging, not responding)."""
    # We only respond to mentions, so this just prevents errors
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
