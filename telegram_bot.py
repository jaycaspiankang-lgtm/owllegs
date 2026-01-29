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
    """Parse parlay legs from text input."""
    legs = []
    lines = text.strip().split('\n')

    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or line.startswith('/'):
            continue

        leg = {'pick': line, 'odds': 1.0}

        odds_patterns = [
            r'([+-]\d+)\s*$',
            r'@\s*([+-]?\d+\.?\d*)\s*$',
            r'\(([+-]?\d+\.?\d*)\)\s*$',
            r'\s(\d+\.\d+)\s*$',
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


def add_parlay(user_id, user_name, chat_id, stake, legs, source="manual"):
    """Add a new parlay."""
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()

    total_odds = 1.0
    for leg in legs:
        odds = leg.get('odds', 1.0)
        total_odds *= odds

    try:
        stake_float = float(str(stake).replace('$', '').replace(',', ''))
        potential_payout = stake_float * total_odds
    except:
        potential_payout = 0

    c.execute("""
        INSERT INTO parlays (user_id, user_name, chat_id, stake, total_odds,
                            potential_payout, legs, status, created_at, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
    """, (str(user_id), user_name, str(chat_id), str(stake), f"{total_odds:.2f}",
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


def format_parlay(parlay):
    """Format a parlay for display."""
    legs = json.loads(parlay['legs']) if isinstance(parlay['legs'], str) else parlay['legs']

    lines = [f"*Parlay #{parlay['id']}* - {parlay['user_name']}"]
    lines.append(f"Stake: {parlay['stake']} → Potential: {parlay['potential_payout']}")
    lines.append(f"Total Odds: {parlay['total_odds']}x")
    lines.append("Legs:")

    for i, leg in enumerate(legs, 1):
        odds_str = f" ({leg.get('odds', '')})" if leg.get('odds') else ""
        lines.append(f"  {i}. {leg['pick']}{odds_str}")

    status = parlay['status']
    if status == 'won':
        lines.append(f"*WON {parlay['potential_payout']}!*")
    elif status == 'lost':
        lines.append(f"*LOST*")

    return "\n".join(lines)


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


def fetch_odds(sport):
    """Fetch betting odds from ESPN."""
    sport_path = ODDS_SPORTS.get(sport.lower())
    if not sport_path:
        return None

    try:
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


def parse_betting_slip_ocr(ocr_lines):
    """Parse OCR text from a betting slip."""
    legs = []

    for line in ocr_lines:
        line = line.strip()
        if not line or len(line) < 3:
            continue

        skip_words = ['parlay', 'total', 'wager', 'stake', 'potential', 'payout',
                      'slip', 'ticket', 'placed', 'accepted', 'pending']
        if any(word in line.lower() for word in skip_words):
            continue

        odds_match = re.search(r'([+-]\d{3}|\d+\.\d+)', line)
        if odds_match:
            odds_str = odds_match.group(1)
            pick = line[:odds_match.start()].strip()
            if not pick:
                pick = line[odds_match.end():].strip()
            if pick:
                legs.append({
                    'pick': pick,
                    'odds': parse_odds(odds_str)
                })
        elif re.search(r'(over|under|spread|ml|moneyline)', line.lower()):
            legs.append({
                'pick': line,
                'odds': 1.0
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
        "*Bet Tracker Bot Commands*\n\n"
        "*Parlays:*\n"
        "/parlay $20 - Start a parlay, then send legs:\n"
        "```\nLakers ML +150\nChiefs -3 -110\n```\n"
        "/parlays - Your open parlays\n"
        "/parlay\\_won <id> - Mark as won\n"
        "/parlay\\_lost <id> - Mark as lost\n\n"
        "*Scores & Lines:*\n"
        "/scores nba - NBA scores\n"
        "/scores nfl - NFL scores\n"
        "/lines nba - NBA betting lines/odds\n"
        "/lines lakers - Search team lines\n\n"
        "*Screenshots:*\n"
        "Upload a betting slip image and I'll try to read it!\n",
        parse_mode='Markdown'
    )


async def parlay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /parlay command."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    # Check for stake in args
    if context.args:
        stake_str = ' '.join(context.args)
        stake_match = re.search(r'\$?(\d+(?:\.\d{2})?)', stake_str)
        if stake_match:
            stake = stake_match.group(1)
            context.user_data['pending_parlay_stake'] = stake
            await update.message.reply_text(
                f"Got it! Creating a ${stake} parlay.\n\n"
                f"Now send me your legs, one per line:\n"
                f"```\nLakers ML +150\nChiefs -3 -110\nOver 48.5 -110\n```",
                parse_mode='Markdown'
            )
            return

    await update.message.reply_text(
        "Usage: /parlay $20\n"
        "Then send your legs in the next message."
    )


async def parlays_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /parlays command."""
    user_id = update.effective_user.id
    parlays = get_user_parlays(user_id)

    if not parlays:
        await update.message.reply_text("You have no open parlays! Create one with /parlay $20")
        return

    lines = ["*Your Open Parlays:*\n"]
    for parlay in parlays:
        lines.append(format_parlay(parlay))
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

        lines = [f"*{query.upper()} Lines:*\n"]
        for game in games:
            lines.append(format_odds(game))
            lines.append("")

        await update.message.reply_text("\n".join(lines), parse_mode='Markdown')
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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle regular text messages."""
    text = update.message.text
    user = update.effective_user
    chat_id = update.effective_chat.id

    # Check if user has a pending parlay
    if 'pending_parlay_stake' in context.user_data:
        stake = context.user_data.pop('pending_parlay_stake')
        legs = parse_parlay_text(text)

        if not legs:
            await update.message.reply_text(
                "Couldn't parse any legs. Format:\n"
                "```\nPick +odds\nPick -odds\n```",
                parse_mode='Markdown'
            )
            return

        parlay_id = add_parlay(user.id, user.first_name, chat_id, f"${stake}", legs)
        parlay = get_parlay(parlay_id)

        await update.message.reply_text(
            f"Parlay #{parlay_id} created!\n\n{format_parlay(parlay)}",
            parse_mode='Markdown'
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo uploads (betting slip screenshots)."""
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

        # Look for stake in caption
        caption = update.message.caption or ""
        stake_match = re.search(r'\$(\d+(?:\.\d{2})?)', caption)
        stake = stake_match.group(1) if stake_match else "10"

        # Create parlay
        parlay_id = add_parlay(user.id, user.first_name, chat_id, f"${stake}", legs, source="screenshot")
        parlay = get_parlay(parlay_id)

        await update.message.reply_text(
            f"Parlay #{parlay_id} created from screenshot!\n\n{format_parlay(parlay)}\n\n"
            f"_Wrong? Delete with /parlay\\_delete {parlay_id}_",
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
    app.add_handler(CommandHandler("scores", scores_command))
    app.add_handler(CommandHandler("lines", lines_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("Telegram Bet Tracker bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
