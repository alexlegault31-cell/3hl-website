"""Gathers the current active season's real data into the JSON shape the
website consumes directly -- teams, standings, schedule, player season
totals, full per-game recap data, and the playoff bracket. Runs after
every game import/forfeit/delete, alongside the existing Discord channel
refresh."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import (
    Game,
    GoalieGameStat,
    Player,
    PlayerGameStat,
    PlayerSeason,
    PlayoffSeries,
    ScheduleGame,
    StandingsEntry,
    Team,
    TeamGameStat,
)
from bot.services.season_service import SeasonNotFound, get_active_season


def _fmt_toi(minutes) -> str:
    minutes = minutes or 0
    mins = int(minutes)
    secs = int(round((minutes - mins) * 60))
    return f"{mins}:{secs:02d}"


async def gather_export_data(session: AsyncSession) -> dict:
    try:
        season = await get_active_season(session)
    except SeasonNotFound:
        return {}

    teams = (await session.execute(select(Team).where(Team.is_active.is_(True)))).scalars().all()
    teams_json = [
        {"id": t.id, "name": t.name, "abbr": (t.abbreviation or t.name[-3:]).upper(), "color": t.primary_color or "#58A6FF"}
        for t in teams
    ]

    standings = (
        await session.execute(select(StandingsEntry).where(StandingsEntry.season_id == season.id).order_by(StandingsEntry.rank))
    ).scalars().all()
    standings_json = [
        {
            "teamId": s.team_id, "rank": s.rank, "prevRank": s.previous_rank,
            "w": s.wins, "l": s.losses, "otl": s.ot_losses, "pts": s.points,
            "gf": s.goals_for, "ga": s.goals_against, "streak": s.streak,
        }
        for s in standings
    ]

    schedule = (
        await session.execute(
            select(ScheduleGame).where(ScheduleGame.season_id == season.id).order_by(ScheduleGame.week, ScheduleGame.game_number)
        )
    ).scalars().all()

    game_ids = [g.game_id for g in schedule if g.game_id]
    games_by_id = {}
    if game_ids:
        games = (await session.execute(select(Game).where(Game.id.in_(game_ids)))).scalars().all()
        games_by_id = {g.id: g for g in games}

    schedule_json = []
    for g in schedule:
        entry = {
            "num": g.game_number, "week": g.week,
            "slot": f"{(g.day_of_week or '')[:3]} {g.game_time or ''}".strip(),
            "home": g.home_team_id, "away": g.away_team_id,
            "played": g.game_id in games_by_id,
            "homeScore": None, "awayScore": None,
        }
        if g.game_id in games_by_id:
            real_game = games_by_id[g.game_id]
            entry["homeScore"] = real_game.home_score
            entry["awayScore"] = real_game.away_score
        schedule_json.append(entry)

    player_seasons = (
        await session.execute(select(PlayerSeason).where(PlayerSeason.season_id == season.id, PlayerSeason.games_played > 0))
    ).scalars().all()
    players_json = []
    for ps in player_seasons:
        player = await session.get(Player, ps.player_id)
        if player is None:
            continue
        if player.is_goalie:
            players_json.append({
                "id": player.id, "name": player.gamertag, "teamId": ps.team_id, "pos": "G", "goalie": True,
                "gp": ps.games_played, "w": ps.wins, "l": ps.losses, "otl": ps.ot_losses,
                "gaa": ps.gaa, "svp": ps.save_pct, "so": ps.shutouts,
            })
        else:
            players_json.append({
                "id": player.id, "name": player.gamertag, "teamId": ps.team_id, "pos": "F", "goalie": False,
                "gp": ps.games_played, "g": ps.goals, "a": ps.assists, "p": ps.points,
                "pim": ps.pim, "hits": ps.hits,
            })

    # Full per-game recap data -- most recent first.
    game_results_json = []
    for sched_obj in [g for g in schedule if g.game_id in games_by_id]:
        game = games_by_id[sched_obj.game_id]

        home_team_stat = await session.scalar(
            select(TeamGameStat).where(TeamGameStat.game_id == game.id, TeamGameStat.team_id == sched_obj.home_team_id)
        )
        away_team_stat = await session.scalar(
            select(TeamGameStat).where(TeamGameStat.game_id == game.id, TeamGameStat.team_id == sched_obj.away_team_id)
        )
        skater_rows = (await session.execute(select(PlayerGameStat).where(PlayerGameStat.game_id == game.id))).scalars().all()
        goalie_rows = (await session.execute(select(GoalieGameStat).where(GoalieGameStat.game_id == game.id))).scalars().all()

        home_skaters, away_skaters = [], []
        for line in skater_rows:
            p = await session.get(Player, line.player_id)
            pass_pct = round((line.passes_completed / line.pass_attempts) * 100) if line.pass_attempts else 0
            row = {
                "name": p.gamertag if p else "Unknown", "pos": (line.position or "-")[:3].upper(),
                "g": line.goals, "a": line.assists, "p": line.points, "pm": line.plus_minus,
                "toi": _fmt_toi(line.minutes_played), "twp": _fmt_toi(line.time_with_puck),
                "shots": line.shots, "passPct": pass_pct, "fow": line.faceoffs_won, "fol": line.faceoffs_lost,
                "hits": line.hits, "ta": line.takeaways, "giveaways": line.giveaways,
                "bs": line.blocked_shots, "int": line.interceptions, "pim": line.pim,
            }
            (home_skaters if line.team_id == sched_obj.home_team_id else away_skaters).append(row)

        home_goalie, away_goalie = None, None
        for line in goalie_rows:
            p = await session.get(Player, line.player_id)
            result = "W" if line.result == 1 else ("OTL" if line.result == 2 else "L")
            sv_pct = round(line.saves / line.shots_against, 3) if line.shots_against else 0
            row = {
                "name": p.gamertag if p else "Unknown", "result": result, "sa": line.shots_against, "sv": line.saves,
                "ga": line.goals_against, "svPct": f"{sv_pct:.3f}", "toi": _fmt_toi(line.minutes_played),
                "pkChk": getattr(line, "poke_checks", 0), "despSv": getattr(line, "desperation_saves", 0),
            }
            if line.team_id == sched_obj.home_team_id:
                home_goalie = row
            else:
                away_goalie = row

        home_avg_pass = round(sum(s["passPct"] for s in home_skaters) / len(home_skaters)) if home_skaters else 0
        away_avg_pass = round(sum(s["passPct"] for s in away_skaters) / len(away_skaters)) if away_skaters else 0

        game_results_json.append({
            "id": sched_obj.game_number, "week": sched_obj.week,
            "home": sched_obj.home_team_id, "away": sched_obj.away_team_id,
            "homeScore": game.home_score, "awayScore": game.away_score,
            "mode": "6v6" if len(home_skaters) >= 5 else "4v4",
            "teamStats": {
                "home": {
                    "shots": home_team_stat.shots if home_team_stat else 0,
                    "faceoffsWon": sum(s["fow"] for s in home_skaters),
                    "hits": home_team_stat.hits if home_team_stat else 0,
                    "toa": _fmt_toi(home_team_stat.time_on_attack) if home_team_stat else "0:00",
                    "pim": home_team_stat.pim if home_team_stat else 0,
                    "passPct": home_avg_pass,
                },
                "away": {
                    "shots": away_team_stat.shots if away_team_stat else 0,
                    "faceoffsWon": sum(s["fow"] for s in away_skaters),
                    "hits": away_team_stat.hits if away_team_stat else 0,
                    "toa": _fmt_toi(away_team_stat.time_on_attack) if away_team_stat else "0:00",
                    "pim": away_team_stat.pim if away_team_stat else 0,
                    "passPct": away_avg_pass,
                },
            },
            "homeSkaters": home_skaters, "awaySkaters": away_skaters,
            "homeGoalie": home_goalie, "awayGoalie": away_goalie,
        })
    game_results_json.reverse()

    all_series = (
        await session.execute(
            select(PlayoffSeries).where(PlayoffSeries.season_id == season.id).order_by(PlayoffSeries.round_order, PlayoffSeries.series_order)
        )
    ).scalars().all()
    rounds: dict[str, list] = {}
    for s in all_series:
        rounds.setdefault(s.round_name, []).append({
            "a": s.team_a_id, "b": s.team_b_id, "winsA": s.wins_a, "winsB": s.wins_b,
            "done": s.winner_team_id is not None,
        })
    bracket_json = [{"round": name, "matches": matches} for name, matches in rounds.items()]

    return {
        "season": {"name": season.name, "number": season.number},
        "teams": teams_json,
        "standings": standings_json,
        "schedule": schedule_json,
        "players": players_json,
        "gameResults": game_results_json,
        "bracket": bracket_json,
    }
