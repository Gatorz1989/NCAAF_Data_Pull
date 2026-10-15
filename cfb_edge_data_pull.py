#!/usr/bin/env python3
"""
cfb_edge_data_pull.py
──────────────────────────────────────────────────────────────────────────
Companion data-pull script for NCAA_Edge_Model_v1.html
(mirrors the role nfl_edge_data_pull.py plays for your NFL Edge Model)

Pulls:
  1. Team season stats + SP+ ratings  -> CollegeFootballData.com API (CFBD)
  2. Upcoming games / scores          -> ESPN unofficial CFB scoreboard API (no key)
  3. Betting lines (spread/total/ML)  -> The Odds API (americanfootball_ncaaf)
  4. Injuries                          -> not available (CFBD has no injuries
     endpoint at all — confirmed against their own API spec; not attempted)
  5. Current rosters                   -> CFBD /roster endpoint
     NOTE: this replaces an earlier ESPN-depth-chart-based approach. That was
     abandoned after ESPN's own API returned an explicit error confirming
     depth charts aren't supported for college football at all (not a parsing
     issue — the feature doesn't exist for this sport). Real rosters are used
     instead to validate whether the HTML's baked-in 2025 top-performer names
     are still actually on the team, which is the more important question
     anyway. cfbdepth.com and Ourlads remain non-options (no public API,
     JS-rendered, and Ourlads' ToS wouldn't permit scraping regardless).
  6. Head coaches + tenure/win%       -> CFBD /coaches endpoint
  7. Historical games + retroactive   -> CFBD /games + /lines + /ppa/games.
     model predictions (2024/2025)      Real results and real closing lines,
     PLUS a retroactive model prediction per game using week-by-week rolling
     PPA ratings (prior weeks only, no lookahead) run through a simplified
     version of the model's core scoring logic. This is what makes the Lab
     tab's Winner%/ATS%/O/U%/margin-error KPIs real instead of blank. It is
     NOT full feature parity with the live 2026 model (no historical
     coaching/injury/weather/red-zone data exists to feed those factors) —
     see pull_historical_games() and score_game_retroactively() for exactly
     what is and isn't modeled.
  8. bigballsdata.com cross-check       -> real scores + standings (free
     (Phase 1 — see project notes)         tier), compared against ESPN's
     scores as an independent second source. Not yet a primary data source
     for anything — this phase only proves the two sources reconcile and
     surfaces any disagreements, before anything downstream (Phase 2+)
     depends on bigballsdata data. Requires BIGBALLS_API_KEY; runs are
     skipped cleanly without it, same as every other optional key here.

Outputs: cfb_model_data.json in the same folder, matching the schema
NCAA_Edge_Model_v1.html's loadModelData() expects:

  {
    "season": 2026,
    "generated": "<ISO timestamp>",
    "meta": {"teamCount": N, "playerCount": N, "rookieCount": N},
    "teams": { "<ABBR>": {"offEPA":.., "defEPA":.., "offPassEPA":.., "offRushEPA":.., "games":N} },
    "games": [ {"homeTeam":"","awayTeam":"","week":N,"date":"","venue":"","spread":n,"total":n,"status":""} ],
    "players": { "<Player Name>": {...} },
    "injuries": {},
    "rosters": { "<ABBR>": ["Full Player Name", ...] },
    "coaches": { "<ABBR>": {"name":"","tenureYears":N,"winPctThisSeason":n,"careerWinPctAtSchool":n} },
    "historicalGames": { "2023": [...], "2024": [...], "2025": [...] },
    "bigballsStandings": [ {...} ],
    "bigballsTeamForm": { "<ABBR>": {...real form fields...} },
    "bigballsH2H": { "<AWAY>_<HOME>": {...real h2h fields...} },
    "cfbdH2H": { "<AWAY>_<HOME>": {...real /teams/matchup response...} },
    "recruitingAndTalent": { "<ABBR>": {"recruitingRank":, "recruitingPoints":, "talentScore":} },
    "bigballsDiscrepancies": [ {"matchup":"","espn":"","bigballsdata":""} ]
  }

USAGE
  1. Get a free CFBD API key:   https://collegefootballdata.com/key
  2. Get a free Odds API key:   https://the-odds-api.com  (optional — script runs without it, just skips lines)
  3. Get a free bigballsdata key: https://bigballsdata.com/dashboard/keys  (optional — Phase 1 cross-check only)
  4. Set env vars (or edit CONFIG below):
       set CFBD_API_KEY=your_key_here        (Windows)
       export CFBD_API_KEY=your_key_here     (Mac/Linux)
       set ODDS_API_KEY=your_key_here
       set BIGBALLS_API_KEY=your_key_here
  5. Run weekly (Tuesday after games, or daily during the season):
       python cfb_edge_data_pull.py
  6. In NCAA_Edge_Model_v1.html -> Data Connections tab -> Load cfb_model_data.json

Requires: pip install requests
──────────────────────────────────────────────────────────────────────────
"""

import os
import sys
import json
import time
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("Missing dependency. Run:  pip install requests")
    sys.exit(1)

# ── CONFIG ──────────────────────────────────────────────────────────────
def _clean_key(v):
    """Strip the things that break keys when they are pasted or typed into Windows: spaces, quotes, a stray leading +,
    a pasted 'Bearer ' prefix, line breaks."""
    v = (v or "").strip().strip("\"'").strip()
    if v.lower().startswith("bearer "):
        v = v[7:].strip()
    return v.lstrip("+").strip().strip("\"'").strip()


def _load_keys_file():
    """keys.txt next to this script (or in the folder you run it from): one NAME=value per line, # for comments.
    Names: CFBD_API_KEY, ODDS_API_KEY, BIGBALLS_API_KEY."""
    found = {}
    for base in (os.path.dirname(os.path.abspath(__file__)), os.getcwd()):
        p = os.path.join(base, "keys.txt")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8-sig") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k = k.strip().upper()
                        if _clean_key(v):
                            found.setdefault(k, _clean_key(v))
            except OSError as e:
                print(f"  ! Could not read {p}: {e}")
            break
    return found


_KEYS_FILE = _load_keys_file()


def _get_key(name):
    """keys.txt wins when it has the key; otherwise the Windows environment variable. Returns (value, where)."""
    if _KEYS_FILE.get(name):
        return _KEYS_FILE[name], "keys.txt"
    v = _clean_key(os.environ.get(name, ""))
    return v, ("environment" if v else "not set")


CFBD_API_KEY, _CFBD_SRC = _get_key("CFBD_API_KEY")        # https://collegefootballdata.com/key
ODDS_API_KEY, _ODDS_SRC = _get_key("ODDS_API_KEY")         # https://the-odds-api.com
BIGBALLS_API_KEY, _BIG_SRC = _get_key("BIGBALLS_API_KEY")  # https://bigballsdata.com/dashboard/keys


def _print_key_report():
    """First lines of every run: where each key came from, its length and first/last characters (never the whole key)."""
    print("Keys:")
    for nm, k, src in (("CFBD_API_KEY", CFBD_API_KEY, _CFBD_SRC), ("ODDS_API_KEY", ODDS_API_KEY, _ODDS_SRC),
                       ("BIGBALLS_API_KEY", BIGBALLS_API_KEY, _BIG_SRC)):
        if k:
            print(f"  {nm:17s} {src:11s} length {len(k):3d}  {k[:2]}...{k[-2:]}")
        else:
            print(f"  {nm:17s} NOT SET")
    if CFBD_API_KEY and ODDS_API_KEY and CFBD_API_KEY == ODDS_API_KEY:
        print("  ! CFBD and ODDS keys are identical — one of them is wrong.")
    if BIGBALLS_API_KEY and (BIGBALLS_API_KEY in (CFBD_API_KEY, ODDS_API_KEY)):
        print("  ! BIGBALLS key equals another key — one of them is wrong.")
SEASON        = int(os.environ.get("CFB_SEASON", "2026"))
OUTPUT_PATH   = os.environ.get("CFB_OUTPUT_PATH", "cfb_model_data.json")

CFBD_BASE   = "https://api.collegefootballdata.com"
ESPN_BASE   = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"
ODDS_BASE   = "https://api.the-odds-api.com/v4/sports/americanfootball_ncaaf/odds"
# Aligned to match the NFL Edge Model's actual real bookmaker list (found by
# direct inspection of that model's file, not the betrivers-based list
# previously used here) — so both models compare against the same books.
ODDS_API_BOOKMAKERS = "draftkings,fanduel,betmgm,caesars,bet365"
# Confirmed real, existing NCAAF markets — Anytime TD is not NFL-exclusive
# terminology, same market key works for both sports on The Odds API.
ODDS_API_PROP_MARKETS = "player_pass_yds,player_pass_tds,player_rush_yds,player_rush_tds,player_reception_yds,player_receptions,player_anytime_td"
BIGBALLS_BASE = "https://api.bigballsdata.com"

# Must match the "name" keys used in CFB_TEAM_MAP inside NCAA_Edge_Model_v1.html
# (school name -> the model's internal abbreviation). Keep this in sync if you
# add/remove teams from the HTML's TEAMS object.
TEAM_NAME_TO_ABBR = {
  "Alabama":"ALA",
  "Arkansas":"ARK",
  "Auburn":"AUB",
  "Florida":"FLA",
  "Georgia":"UGA",
  "Kentucky":"UK",
  "LSU":"LSU",
  "Mississippi State":"MSST",
  "Missouri":"MIZ",
  "Oklahoma":"OU",
  "Ole Miss":"MISS",
  "South Carolina":"SC",
  "Tennessee":"TENN",
  "Texas":"TEX",
  "Texas A&M":"TAMU",
  "Vanderbilt":"VAN",
  "Illinois":"ILL",
  "Indiana":"IND",
  "Iowa":"IOWA",
  "Maryland":"UMD",
  "Michigan":"MICH",
  "Michigan State":"MSU",
  "Minnesota":"MINN",
  "Nebraska":"NEB",
  "Northwestern":"NW",
  "Ohio State":"OSU",
  "Oregon":"ORE",
  "Penn State":"PSU",
  "Purdue":"PUR",
  "Rutgers":"RUTG",
  "UCLA":"UCLA",
  "USC":"USC",
  "Washington":"WASH",
  "Wisconsin":"WISC",
  "Arizona":"ARIZ",
  "Arizona State":"ASU",
  "Baylor":"BAY",
  "BYU":"BYU",
  "Cincinnati":"CIN",
  "Colorado":"COLO",
  "Houston":"HOU",
  "Iowa State":"ISU",
  "Kansas":"KU",
  "Kansas State":"KSU",
  "Oklahoma State":"OKST",
  "TCU":"TCU",
  "Texas Tech":"TTU",
  "UCF":"UCF",
  "Utah":"UTAH",
  "West Virginia":"WVU",
  "Boston College":"BC",
  "California":"CAL",
  "Clemson":"CLEM",
  "Duke":"DUKE",
  "Florida State":"FSU",
  "Georgia Tech":"GT",
  "Louisville":"LOU",
  "Miami":"MIA",
  "NC State":"NCST",
  "North Carolina":"UNC",
  "Pittsburgh":"PITT",
  "SMU":"SMU",
  "Stanford":"STAN",
  "Syracuse":"SYR",
  "Virginia":"UVA",
  "Virginia Tech":"VT",
  "Wake Forest":"WAKE",
  "Notre Dame":"ND",
  "UConn":"UCONN",
  "UMass":"UMASS",
  "Boise State":"BSU",
  "Colorado State":"CSU",
  "Fresno State":"FRES",
  "Oregon State":"ORST",
  "San Diego State":"SDSU",
  "Texas State":"TXST",
  "Utah State":"USU",
  "Washington State":"WSU",
  "Army":"ARMY",
  "Charlotte":"CHAR",
  "East Carolina":"ECU",
  "Florida Atlantic":"FAU",
  "Memphis":"MEM",
  "Navy":"NAVY",
  "North Texas":"UNT",
  "Rice":"RICE",
  "South Florida":"USF",
  "Temple":"TEM",
  "Tulane":"TULN",
  "Tulsa":"TLSA",
  "UAB":"UAB",
  "UTSA":"UTSA",
  "Air Force":"AF",
  "Hawaii":"HAW",
  "Hawai'i":"HAW",
  "Nevada":"NEV",
  "New Mexico":"UNM",
  "North Dakota State":"NDSU",
  "Northern Illinois":"NIU",
  "San Jose State":"SJSU",
  "San José State":"SJSU",
  "UNLV":"UNLV",
  "UTEP":"UTEP",
  "Wyoming":"WYO",
  "App State":"APP",
  "Arkansas State":"ARST",
  "Coastal Carolina":"CCU",
  "Georgia Southern":"GASO",
  "Georgia State":"GAST",
  "James Madison":"JMU",
  "Louisiana":"ULL",
  "Louisiana-Monroe":"ULM",
  "Louisiana Monroe":"ULM",
  "UL Monroe":"ULM",
  "Louisiana Tech":"LATECH",
  "Marshall":"MRSH",
  "Old Dominion":"ODU",
  "South Alabama":"USA",
  "Southern Miss":"USM",
  "Troy":"TROY",
  "Akron":"AKR",
  "Ball State":"BALL",
  "Bowling Green":"BGSU",
  "Buffalo":"BUFF",
  "Central Michigan":"CMU",
  "Eastern Michigan":"EMU",
  "Kent State":"KENT",
  "Miami (OH)":"MIAOH",
  "Ohio":"OHIO",
  "Sacramento State":"SACST",
  "Toledo":"TOL",
  "Western Michigan":"WMU",
  "Delaware":"DEL",
  "FIU":"FIU",
  "Florida International":"FIU",
  "Jacksonville State":"JVST",
  "Kennesaw State":"KENN",
  "Liberty":"LIB",
  "Middle Tennessee":"MTSU",
  "Missouri State":"MOST",
  "New Mexico State":"NMST",
  "Sam Houston":"SHSU",
  "Western Kentucky":"WKU",
}


_cfbd_auth_failed = False  # set once a 401 is seen, so we stop hammering
                            # the API with the same bad key ~150 more times
                            # and instead show one clear, visible warning.
_cfbd_call_succeeded = False  # set True on any successful response, for the status summary

def cfbd_get(path, params=None, _retry=True):
    global _cfbd_auth_failed, _cfbd_call_succeeded
    if not CFBD_API_KEY or _cfbd_auth_failed:
        return None
    headers = {"Authorization": f"Bearer {CFBD_API_KEY}"}
    try:
        r = requests.get(f"{CFBD_BASE}{path}", headers=headers, params=params or {}, timeout=20)
        r.raise_for_status()
        _cfbd_call_succeeded = True
        return r.json()
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            _cfbd_auth_failed = True
            print("  " + "=" * 70)
            print("  ! CFBD_API_KEY WAS REJECTED (401 Unauthorized).")
            print("    This usually means the wrong key got set for this variable —")
            print("    double-check CFBD_API_KEY, ODDS_API_KEY, and BIGBALLS_API_KEY")
            print("    are each set to THEIR OWN key, not the same key for all three.")
            print("    Skipping all further CFBD calls for this run rather than")
            print("    repeating this same error for every remaining request.")
            print("  " + "=" * 70)
        else:
            print(f"  ! CFBD error on {path}: {e}")
        return None
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        # Transient network hiccup (e.g. "Connection aborted" /
        # ConnectionResetError) — one retry before giving up, since these
        # aren't a real API/auth problem and a caller silently getting None
        # here can print a misleading "no data yet" message that isn't the
        # actual cause.
        if _retry:
            print(f"  ! CFBD connection error on {path} — retrying once: {e}")
            time.sleep(1.5)
            return cfbd_get(path, params, _retry=False)
        print(f"  ! CFBD connection error on {path} (retry also failed): {e}")
        return None
    except Exception as e:
        print(f"  ! CFBD error on {path}: {e}")
        return None


_bigballs_auth_failed = False
_bigballs_call_succeeded = False  # set True on any successful response, for the status summary
_bigballs_forbidden_endpoints = set()  # endpoint suffixes that returned 403 — stop retrying them this run
_bigballs_forbidden_counts = {}  # consecutive-403 tracker per endpoint suffix, before tripping the breaker above

def bigballs_get(path, params=None):
    """Same pattern as cfbd_get() — bearer-token auth, same error handling.
    bigballsdata's free tier is rate-limited (100 req/min, 1,000-2,000/day),
    so this is used sparingly: bulk endpoints only, no per-team/per-player
    looping in Phase 1.

    Also circuit-breaks per-endpoint on repeated 403s — discovered the hard
    way that bigballsdata's docs claiming an endpoint is free-tier doesn't
    guarantee this specific account actually has access to it (got 403
    Forbidden, not 401, meaning the key is valid but this endpoint isn't
    permitted). A 403 on one specific team's /form call doesn't mean the
    key is broken everywhere else, so this only stops retrying that ONE
    endpoint pattern (e.g. "form" or "h2h-intelligence") after a few
    consecutive failures, not the whole bigballsdata connection."""
    global _bigballs_auth_failed, _bigballs_call_succeeded
    if not BIGBALLS_API_KEY or _bigballs_auth_failed:
        return None

    # Endpoint "suffix" for circuit-breaking: the last path segment, e.g.
    # "/v1/teams/<id>/form" -> "form". Query params don't affect this.
    endpoint_suffix = path.rstrip("/").split("/")[-1]
    if endpoint_suffix in _bigballs_forbidden_endpoints:
        return None

    headers = {"Authorization": f"Bearer {BIGBALLS_API_KEY}"}
    try:
        r = requests.get(f"{BIGBALLS_BASE}{path}", headers=headers, params=params or {}, timeout=20)
        r.raise_for_status()
        _bigballs_call_succeeded = True
        _bigballs_forbidden_counts[endpoint_suffix] = 0  # reset on any success
        return r.json()
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            _bigballs_auth_failed = True
            print("  ! BIGBALLS_API_KEY WAS REJECTED (401 Unauthorized) — check it's set correctly.")
        elif e.response is not None and e.response.status_code == 403:
            _bigballs_forbidden_counts[endpoint_suffix] = _bigballs_forbidden_counts.get(endpoint_suffix, 0) + 1
            if _bigballs_forbidden_counts[endpoint_suffix] >= 3:
                _bigballs_forbidden_endpoints.add(endpoint_suffix)
                print(f"  ! bigballsdata '{endpoint_suffix}' endpoint returned 403 Forbidden {_bigballs_forbidden_counts[endpoint_suffix]} times —")
                print(f"    this account doesn't have access to it despite docs describing it as free-tier.")
                print(f"    Skipping all further '{endpoint_suffix}' calls this run rather than repeating the same 403.")
            else:
                print(f"  ! bigballsdata error on {path}: {e}")
        else:
            print(f"  ! bigballsdata error on {path}: {e}")
        return None
    except Exception as e:
        print(f"  ! bigballsdata error on {path}: {e}")
        return None


def pull_cfbd_h2h_for_games(games):
    """Real head-to-head matchup history from CFBD's own /teams/matchup
    endpoint — confirmed real via CFBD's official cfb.js SDK documentation,
    the same source that correctly predicted the recruiting/talent
    endpoints (which worked perfectly). Replaces the bigballsdata
    h2h-intelligence attempt, which this account's free tier doesn't
    actually have access to (confirmed via /v1/coverage). Bounded to real
    scheduled games, not every possible team combination, same principle
    as everything else bounded-by-real-games in this script."""
    print("Pulling CFBD head-to-head matchup history for scheduled games...")
    out = {}
    if not CFBD_API_KEY:
        print("  ! No CFBD_API_KEY — skipping.")
        return out

    # CFBD's endpoints expect real school names ("Texas"), not our internal
    # abbreviations ("TEX") — TEAM_NAME_TO_ABBR only maps name->abbr, so
    # build the reverse lookup here rather than passing abbrs directly,
    # which would silently return nothing.
    abbr_to_name = {abbr: name for name, abbr in TEAM_NAME_TO_ABBR.items()}

    sample_printed = False
    pulled = 0
    for g in (games or []):
        home_abbr = g.get("homeTeam")
        away_abbr = g.get("awayTeam")
        home_name = abbr_to_name.get(home_abbr)
        away_name = abbr_to_name.get(away_abbr)
        if not home_name or not away_name:
            continue
        resp = cfbd_get("/teams/matchup", {"team1": away_name, "team2": home_name})
        if not resp:
            continue
        pulled += 1
        if not sample_printed:
            print(f"     (sample /teams/matchup response keys: {list(resp.keys()) if isinstance(resp, dict) else 'not a dict — ' + str(type(resp))})")
            sample_printed = True
        out[f"{away_abbr}_{home_abbr}"] = resp

    print(f"  -> real H2H history for {pulled} of {len(games or [])} scheduled games")
    return out


def pull_recruiting_and_talent(year=None):
    """Real recruiting class rankings (/recruiting/teams) and team talent
    composite scores (/talent) from CFBD — confirmed real endpoints from
    CFBD's own official cfb.js SDK documentation. Used as a depth signal
    layered on top of returning-production/coaching-regime signals, not a
    replacement for real in-season performance data."""
    print("Pulling CFBD recruiting rankings + talent composite...")
    out = {}
    if not CFBD_API_KEY:
        print("  ! No CFBD_API_KEY — skipping.")
        return out

    if year is None:
        year = SEASON

    recruiting = cfbd_get("/recruiting/teams", {"year": year})
    talent = cfbd_get("/talent", {"year": year})

    recruiting_by_team = {}
    if recruiting:
        print(f"  -> {len(recruiting)} recruiting rankings rows")
        print(f"     (sample recruiting row keys: {list(recruiting[0].keys())})")
        for row in recruiting:
            school = row.get("team")
            if school:
                recruiting_by_team[school] = row

    talent_by_team = {}
    if talent:
        print(f"  -> {len(talent)} talent composite rows")
        print(f"     (sample talent row keys: {list(talent[0].keys())})")
        for row in talent:
            school = row.get("team") or row.get("school")
            if school:
                talent_by_team[school] = row

    for school in set(list(recruiting_by_team.keys()) + list(talent_by_team.keys())):
        abbr = TEAM_NAME_TO_ABBR.get(school)
        if not abbr:
            continue
        rec = recruiting_by_team.get(school, {})
        tal = talent_by_team.get(school, {})
        out[abbr] = {
            "recruitingRank": rec.get("rank"),
            "recruitingPoints": rec.get("points"),
            "talentScore": tal.get("talent"),
        }

    print(f"  -> {len(out)} teams with recruiting/talent data resolved to real abbreviations")
    return out


def pull_team_stats():
    """Season-level advanced stats (PPA/EPA proxy) + SP+ ratings from CFBD.
    Note: PPA/SP+ are computed from games actually played this season — early
    in the year (preseason or Week 0/1) these can legitimately come back
    empty even with a perfectly valid key, simply because there isn't enough
    season data yet for CFBD to compute season aggregates from."""
    print("Pulling CFBD team advanced stats (PPA)...")
    teams_out = {}

    if not CFBD_API_KEY:
        print("  ! No CFBD_API_KEY set — skipping live team stats.")
        print("    Get a free key at https://collegefootballdata.com/key")
        return teams_out

    ppa = cfbd_get("/ppa/teams", {"year": SEASON})  # offense/defense PPA per team
    sp  = cfbd_get("/ratings/sp", {"year": SEASON})  # SP+ overall/offense/defense

    sp_by_team = {row.get("team"): row for row in (sp or [])}

    if ppa:
        for row in ppa:
            school = row.get("team")
            abbr = TEAM_NAME_TO_ABBR.get(school)
            if not abbr:
                continue
            off = row.get("offense", {}) or {}
            defn = row.get("defense", {}) or {}
            sp_row = sp_by_team.get(school) or {}
            sp_off = sp_row.get("offense", {}) or {}
            sp_def = sp_row.get("defense", {}) or {}
            teams_out[abbr] = {
                "offEPA":     off.get("overall", 0) or 0,
                "defEPA":     -(defn.get("overall", 0) or 0),  # flip sign: lower opp PPA = better D
                "offPassEPA": off.get("passing", 0) or 0,
                "offRushEPA": off.get("rushing", 0) or 0,
                "games":      row.get("games", 0) or 0,
                "spOverall":  sp_row.get("rating"),
                # Previously only spOverall was pulled from this response —
                # everything below was sitting unused in data already being
                # fetched. Confirmed real fields from CFBD's own /ratings/sp
                # documentation, not a guess.
                "sos":              sp_row.get("sos"),
                "offRunRate":       sp_off.get("runRate"),
                "offPace":          sp_off.get("pace"),
                "offStandardDowns": sp_off.get("standardDowns"),
                "offPassingDowns":  sp_off.get("passingDowns"),
                "offExplosiveness": sp_off.get("explosiveness"),
                "defStandardDowns": sp_def.get("standardDowns"),
                "defPassingDowns":  sp_def.get("passingDowns"),
                "defExplosiveness": sp_def.get("explosiveness"),
                "defHavocTotal":    (sp_def.get("havoc") or {}).get("total"),
                "defHavocFront7":   (sp_def.get("havoc") or {}).get("frontSeven"),
                "defHavocDB":       (sp_def.get("havoc") or {}).get("db"),
            }
    else:
        print("  -> CFBD key is valid but returned no PPA data — this is normal")
        print("     very early in the season (not enough games played yet to")
        print("     compute season aggregates). Falls back to the static seed")
        print("     values / preseason power ratings baked into the HTML.")

    print(f"  -> {len(teams_out)} teams with live stats")
    return teams_out


def pull_games_espn(week=None):
    """Upcoming/recent games from ESPN's unofficial CFB scoreboard (no key needed).
    NOTE: an earlier version passed groups=80 assuming it meant "all FBS" —
    confirmed wrong (80 is actually ESPN's "Top 25" group; SEC=8, Big Ten=5,
    ACC=1, Big 12=4, Mountain West=17). There's no confirmed "all FBS" groups
    ID for the football scoreboard endpoint, so this drops groups filtering
    entirely and relies on a high limit + our own TEAM_NAME_TO_ABBR to filter
    client-side — same principle used elsewhere in this script for matching
    ESPN/CFBD school names against TEAM_NAME_TO_ABBR."""
    print("Pulling games from ESPN CFB scoreboard...")
    params = {"limit": 400}
    if week:
        params["week"] = week
    try:
        r = requests.get(f"{ESPN_BASE}/scoreboard", params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  ! ESPN scoreboard error: {e}")
        return []

    games_out = []
    for event in data.get("events", []):
        comp = (event.get("competitions") or [{}])[0]
        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})
        home_name = (home.get("team") or {}).get("location", "")
        away_name = (away.get("team") or {}).get("location", "")
        odds = (comp.get("odds") or [{}])[0] if comp.get("odds") else {}

        games_out.append({
            "homeTeam": TEAM_NAME_TO_ABBR.get(home_name, home_name),
            "awayTeam": TEAM_NAME_TO_ABBR.get(away_name, away_name),
            "week":     (data.get("week") or {}).get("number", 0),
            "date":     event.get("date", ""),
            "dateStr":  event.get("date", "")[:10] if event.get("date") else "",
            "venue":    ((comp.get("venue") or {}).get("fullName", "")),
            "spread":   odds.get("spread"),
            "total":    odds.get("overUnder"),
            "status":   ((comp.get("status") or {}).get("type") or {}).get("description", "Scheduled"),
            "id":       event.get("id", f"{away_name}_{home_name}"),
            # ESPN's competitor "score" field is a string ("14") even for a
            # completed game — never extracted before, which meant every
            # cross-validation check against another source's real score
            # silently compared against None regardless of what that other
            # source returned.
            "homeScore": int(home["score"]) if home.get("score") not in (None, "") else None,
            "awayScore": int(away["score"]) if away.get("score") not in (None, "") else None,
        })

    print(f"  -> {len(games_out)} games")
    return games_out


def pull_odds_lines():
    """Spread/total/ML/player-props from The Odds API for CFB. Optional —
    skipped if no key. Bookmaker list and prop markets aligned to match
    what the NFL Edge Model actually pulls (draftkings, fanduel, betmgm,
    caesars, bet365 — not the betrivers-based list previously used here),
    so both models are comparing against the same real books."""
    if not ODDS_API_KEY:
        print("No ODDS_API_KEY set — skipping betting lines (get one free at the-odds-api.com).")
        return {}
    print("Pulling betting lines from The Odds API...")
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "spreads,totals,h2h",
        "bookmakers": ODDS_API_BOOKMAKERS,
        "oddsFormat": "american",
    }
    try:
        r = requests.get(ODDS_BASE, params=params, timeout=20)
        r.raise_for_status()
        events = r.json()
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            print("  ! ODDS_API_KEY WAS REJECTED (401 Unauthorized) — double-check")
            print("    ODDS_API_KEY is set to your actual Odds API key, not the same")
            print("    key used for CFBD_API_KEY or BIGBALLS_API_KEY.")
        else:
            print(f"  ! Odds API error: {e}")
        return {}
    except Exception as e:
        print(f"  ! Odds API error: {e}")
        return {}

    lines_by_matchup = {}
    for ev in events:
        key = f"{ev.get('away_team')}_{ev.get('home_team')}"
        lines_by_matchup[key] = ev
    print(f"  -> {len(lines_by_matchup)} events with lines")
    return lines_by_matchup


def pull_player_props():
    """Real player props (passing/rushing/receiving yards+TDs, Anytime TD)
    from The Odds API — confirmed real, existing NCAAF markets (Anytime TD
    is not NFL-exclusive terminology; same market key works for both). This
    is a real per-event API cost (one call per game, not a bulk pull), so
    bounded to real upcoming scheduled games only, same principle as every
    other bounded-by-real-games pull in this script. Powers the Win Margin
    /Anytime TD tab."""
    print("Pulling player props from The Odds API (bounded to real scheduled games)...")
    out = {}
    if not ODDS_API_KEY:
        print("  ! No ODDS_API_KEY — skipping player props.")
        return out

    events_url = ODDS_BASE.replace("/odds", "/events")
    try:
        r = requests.get(events_url, params={"apiKey": ODDS_API_KEY}, timeout=20)
        r.raise_for_status()
        events = r.json()
    except Exception as e:
        print(f"  ! Odds API events list error: {e}")
        return out

    pulled = 0
    for ev in events:
        event_id = ev.get("id")
        if not event_id:
            continue
        prop_url = f"{ODDS_BASE.replace('/odds', '')}/events/{event_id}/odds"
        try:
            r = requests.get(prop_url, params={
                "apiKey": ODDS_API_KEY,
                "regions": "us",
                "markets": ODDS_API_PROP_MARKETS,
                "bookmakers": ODDS_API_BOOKMAKERS,
                "oddsFormat": "american",
            }, timeout=20)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  ! Odds API props error for event {event_id}: {e}")
            continue
        pulled += 1
        key = f"{data.get('away_team')}_{data.get('home_team')}"
        out[key] = data

    print(f"  -> real player props for {pulled} of {len(events)} real events")
    return out


# ── PLAYER GRADES (QB/RB/WR/TE) ────────────────────────────────────────────
# Two-tier system, same philosophy as the CB grading already in the HTML:
#   1. PFF grades if a key is configured (best available — real PFF is an
#      enterprise data product, not a self-serve API, so this stays
#      best-effort scaffolding for whenever/if you get institutional access)
#   2. Otherwise a transparent stats-derived proxy from CFBD season stats,
#      scaled roughly to a PFF-style 0-99 grade (50 = FBS average).
#
# These are simple, documented formulas — not a claim of PFF-equivalent
# accuracy. Treat them as a consistent, explainable ranking signal, not a
# ground truth grade.

PFF_API_KEY = os.environ.get("PFF_API_KEY", "")  # no public self-serve PFF API exists as of writing;
                                                    # set this only if you have institutional/enterprise access


def _grade_qb(row):
    comp_pct = row.get("completionPct", 0) or 0
    ypa      = row.get("yardsPerAttempt", 0) or 0
    td       = row.get("passingTDs", 0) or 0
    ints     = row.get("interceptions", 0) or 0
    grade = 50 + (ypa - 7.5) * 4 + (td - ints) * 1.2 + (comp_pct - 60) * 0.7
    return max(30, min(99, round(grade, 1)))


def _grade_rb(row):
    ypc = row.get("yardsPerCarry", 0) or 0
    td  = row.get("rushingTDs", 0) or 0
    yds = row.get("rushingYards", 0) or 0
    grade = 50 + (ypc - 4.5) * 8 + td * 1.0 + min(yds / 200.0, 10)
    return max(30, min(99, round(grade, 1)))


def _grade_receiver(row):
    ypr = row.get("yardsPerReception", 0) or 0
    td  = row.get("receivingTDs", 0) or 0
    rec = row.get("receptions", 0) or 0
    grade = 50 + (ypr - 11) * 3 + td * 2 + min(rec / 8.0, 8)
    return max(30, min(99, round(grade, 1)))


def pull_player_game_stats(weeks=None):
    """DISABLED as of this version. Three different guesses at CFBD's
    real per-game player stats endpoint have all failed:
      1. /stats/player/game  -> 404 on every call, no SDK/doc confirms it exists
      2. /game/box/advanced?gameId=X -> 400/429 on every one of 459 calls in
         one run, burning ~46% of a free tier's entire monthly quota (1,000
         calls) for zero results.
    Continuing to guess a fourth time risks exhausting the CFBD quota this
    script depends on for everything else (team stats, rosters, coaches,
    historical games — all of which work correctly today). Disabled here
    rather than burn more quota speculatively. Real fix needs either CFBD's
    actual current API reference checked directly (their docs site,
    api.collegefootballdata.com, may have moved to a v2 reference not
    covered by the older SDKs/wrappers searched so far) or direct outreach
    to CFBD support/Discord for the confirmed current path. The weekly
    props-grading pipeline in the HTML degrades gracefully without this —
    logged props simply stay ungraded until this is re-enabled with a
    verified endpoint.
    """
    print("Per-game player stats pull is disabled — see pull_player_game_stats() docstring for why.")
    return {}


def fetch_week_games_players(wk):
    """ONE shared, cached CFBD /games/players call per completed week. Both the per-game carries/receptions pull and the
    defense-vs-position pull read from this, so a week is never fetched twice. Returns the compact per-game summary
    (see summarize_week_games_players) or None when the endpoint returns nothing."""
    cache = _load_cache()
    wk_cache = cache.setdefault(f"games_players_{SEASON}", {})
    got = wk_cache.get(str(wk))
    # entries cached by an older version have no carCount -> refetch once so carries are available
    if got and all("carCount" in sd for g in got for sd in g.get("sides", [])):
        return got
    rows = cfbd_get("/games/players", {"year": SEASON, "week": wk, "seasonType": "regular"})
    if not rows:
        return got or None
    wk_cache[str(wk)] = summarize_week_games_players(rows)
    _save_cache(cache)
    time.sleep(0.3)
    return wk_cache[str(wk)]


def pull_player_game_counts(weeks):
    """Per-game carries and receptions from the shared /games/players weekly fetch (finished weeks are cached, so a
    normal run only fetches the newest week). Feeds the model's recent-form and with/without-teammate splits for the
    Replacement Opportunity Share. CFBD has NO targets field anywhere, so receptions stand in for targets.

    Quota-safe: if the FIRST week returns nothing, the pull stops there (at most 1 wasted call) and says so.
    Returns {"TEAM|Player": {"w": [weeks], "car": [carries], "rec": [receptions]}}."""
    out = {}
    if not CFBD_API_KEY or not weeks:
        print("Per-game carries/receptions: skipped (no CFBD key or no completed weeks).")
        return out
    print(f"Pulling per-game carries/receptions for weeks {list(weeks)}...")
    probed = False
    for wk in sorted(weeks):
        games = fetch_week_games_players(wk)
        if not games:
            if not probed:
                print(f"  ! /games/players returned nothing for week {wk} — stopping (endpoint unavailable on this tier or the shape changed).")
                break
            continue
        probed = True
        per = {}
        for g in games:
            for sd in g.get("sides", []):
                ab = sd.get("team")
                if not ab:
                    continue
                for nm, v in (sd.get("carCount") or {}).items():
                    per.setdefault((ab, nm), {"car": 0, "rec": 0})["car"] += v
                for nm, v in (sd.get("recCount") or {}).items():
                    per.setdefault((ab, nm), {"car": 0, "rec": 0})["rec"] += v
        for (ab, nm), d in per.items():
            if not (d["car"] or d["rec"]):
                continue
            e = out.setdefault(f"{ab}|{nm}", {"w": [], "car": [], "rec": []})
            e["w"].append(wk); e["car"].append(d["car"]); e["rec"].append(d["rec"])
    print(f"  -> per-game counts for {len(out)} players")
    return out



# CFBD /stats/player/season is LONG format: one row per player + statType (e.g. {player, team, category:'passing',
# statType:'YDS', stat:'2531'}). Spellings vary a little between API versions, so each wide field accepts several aliases.
_STAT_ALIASES = {
    "passing": {
        "completions": ("COMPLETIONS", "COMP", "CMP"), "attempts": ("ATT", "ATTEMPTS"), "completionPct": ("PCT", "COMPLETIONPCT"),
        "passingYards": ("YDS", "YARDS"), "passingTDs": ("TD", "TDS"), "interceptions": ("INT", "INTS"), "yardsPerAttempt": ("YPA", "YDS/ATT"),
    },
    "rushing": {
        "carries": ("CAR", "ATT", "CARRIES"), "rushingYards": ("YDS", "YARDS"), "yardsPerCarry": ("YPC", "YDS/CAR"),
        "rushingTDs": ("TD", "TDS"), "longRush": ("LONG",),
    },
    "receiving": {
        "receptions": ("REC", "RECEPTIONS"), "receivingYards": ("YDS", "YARDS"), "yardsPerReception": ("YPR", "YDS/REC"),
        "receivingTDs": ("TD", "TDS"), "longRec": ("LONG",),
    },
}
# Attempts/carries/catches at which a proxy grade is fully trusted; below that it is pulled toward the 50 average,
# so one 80-yard catch in week 1 can't read as an elite receiver.
_GRADE_FULL_VOLUME = {"passing": ("attempts", 150.0), "rushing": ("carries", 80.0), "receiving": ("receptions", 25.0)}
_POS_MAP = {"QB": "QB", "RB": "RB", "HB": "RB", "FB": "RB", "WR": "WR", "TE": "TE"}


def _wide_stats(category, kinds):
    """{statType: value} -> wide dict with the field names the model reads (plus the raw statTypes)."""
    out = {"raw": dict(kinds)}
    up = {str(k).upper(): v for k, v in kinds.items()}
    for field, aliases in _STAT_ALIASES[category].items():
        for al in aliases:
            if al in up and up[al] is not None:
                out[field] = up[al]
                break
    if category == "passing":
        att, comp = out.get("attempts"), out.get("completions")
        if att and comp is not None:
            out["completionPct"] = round(100.0 * comp / att, 1)
        elif out.get("completionPct") is not None and out["completionPct"] <= 1.5:
            out["completionPct"] = round(out["completionPct"] * 100.0, 1)   # CFBD sometimes sends a 0-1 fraction
        if out.get("yardsPerAttempt") is None and att and out.get("passingYards") is not None:
            out["yardsPerAttempt"] = round(out["passingYards"] / att, 2)
    elif category == "rushing":
        if out.get("yardsPerCarry") is None and out.get("carries") and out.get("rushingYards") is not None:
            out["yardsPerCarry"] = round(out["rushingYards"] / out["carries"], 2)
    else:
        if out.get("yardsPerReception") is None and out.get("receptions") and out.get("receivingYards") is not None:
            out["yardsPerReception"] = round(out["receivingYards"] / out["receptions"], 2)
    return out


def _shrunk_grade(category, wide, grade_fn):
    vol_key, full = _GRADE_FULL_VOLUME[category]
    vol = _num(wide.get(vol_key)) or 0.0
    raw = grade_fn(wide)
    w = min(1.0, vol / full)
    return max(30, min(99, round(50 + (raw - 50) * w, 1))), vol


def pull_player_stats():
    """Season player stats (passing/rushing/receiving) from CFBD, with a proxy grade computed per player. Populates the
    'players' output block that powers the QB/RB/WR/TE grades on the Team Grades tab.

    v2.0 fix: CFBD returns these stats in long format (one row per player per statType). The old code read wide keys
    like row.get('passingTDs') that never exist, so every grade collapsed to the floor value of 30. Rows are now grouped
    per player first, mapped to the wide field names the model reads, graded on volume-adjusted rates, and WR vs TE is
    taken from the roster position (CFBD's stat rows carry no position)."""
    print("Pulling CFBD player season stats (QB/RB/WR/TE)...")
    players_out = {}

    if not CFBD_API_KEY:
        print("  ! No CFBD_API_KEY — skipping player stats/grades entirely.")
        return players_out

    grade_fns = {"passing": _grade_qb, "rushing": _grade_rb, "receiving": _grade_receiver}
    default_pos = {"passing": "QB", "rushing": "RB", "receiving": "WR"}
    acc = {}   # (abbr, name) -> {category: {statType: value}}
    for category in ("passing", "rushing", "receiving"):
        rows = cfbd_get("/stats/player/season", {"year": SEASON, "category": category})
        if not rows:
            print(f"  -> no {category} stats yet for {SEASON} (normal if season just started)")
            continue
        for row in rows:
            name = _g(row, "player", "playerName")
            abbr = TEAM_NAME_TO_ABBR.get(row.get("team"))
            if not name or not abbr:
                continue
            st = _g(row, "statType", "stat_type")
            if st is not None:
                val = _num(_g(row, "stat", "value"))
                if val is not None:
                    acc.setdefault((abbr, name), {}).setdefault(category, {})[st] = val
            else:   # already-wide row (older/alternate response shape): keep every numeric field
                kinds = {k: _num(v) for k, v in row.items() if _num(v) is not None}
                if kinds:
                    acc.setdefault((abbr, name), {}).setdefault(category, {}).update(
                        {"YDS": kinds.get(f"{category}Yards", kinds.get("yards")), "TD": kinds.get(f"{category}TDs", kinds.get("td"))})

    for (abbr, name), cats in acc.items():
        wides = {c: _wide_stats(c, kinds) for c, kinds in cats.items()}
        pos = _POS_MAP.get(str(ROSTER_POS.get((abbr, name)) or "").upper())
        if pos == "QB":
            cat = "passing"
        elif pos == "RB":
            cat = "rushing"
        elif pos in ("WR", "TE"):
            cat = "receiving"
        else:   # roster position unknown: infer from workload
            p, r, c = wides.get("passing", {}), wides.get("rushing", {}), wides.get("receiving", {})
            if (p.get("attempts") or 0) >= 20:
                cat = "passing"
            elif (r.get("carries") or 0) >= (c.get("receptions") or 0):
                cat = "rushing"
            else:
                cat = "receiving"
            pos = default_pos[cat]
        if cat not in wides:
            # roster says QB/RB/WR but he has no line in that category (e.g. a WR who only ran the ball): grade what exists
            cat = next(iter(wides))
            pos = pos if pos in ("QB", "RB", "WR", "TE") else default_pos[cat]
        grade, vol = _shrunk_grade(cat, wides[cat], grade_fns[cat])
        players_out[name] = {"team": abbr, "pos": pos, "grade": grade, "source": "proxy", "category": cat,
                             "volume": vol, "stats": wides}

    print(f"  -> graded {len(players_out)} players (proxy, volume-adjusted)")
    if players_out:
        gs = [p["grade"] for p in players_out.values()]
        print(f"     grade range {min(gs)}–{max(gs)}, {sum(1 for g in gs if g <= 30)} at the floor")

    if PFF_API_KEY:
        print("  ! PFF_API_KEY is set, but this script does not call a real PFF")
        print("    endpoint — PFF has no documented public/self-serve API to")
        print("    integrate against. If you have enterprise PFF data access")
        print("    with a real endpoint/export format, tell me the shape and")
        print("    I'll wire an actual pull_pff_grades() function here.")

    return players_out


def pull_coaches():
    """Head coach + tenure/win% per team from CFBD's /coaches endpoint.

    Scope note: this pulls Head Coach data only, not offensive/defensive
    coordinators. CFBD's coordinator coverage is inconsistent across 90+
    FBS programs (assistants change constantly and aren't tracked as
    reliably as HCs), so rather than half-fill an OC/DC-tree structure
    with gaps, this builds a simpler, fully-real signal: who the HC is,
    how long they've been at this school, and their actual win% — no
    subjective "elite/proven/rising" labels, since those aren't derivable
    from free data without guessing.
    """
    print("Pulling CFBD head coach data...")
    coaches_out = {}

    if not CFBD_API_KEY:
        print("  ! No CFBD_API_KEY — skipping coach data.")
        return coaches_out

    data = cfbd_get("/coaches", {"year": SEASON})
    if not data:
        print("  -> no coach data returned for this endpoint/tier")
        return coaches_out

    for row in data:
        first = row.get("firstName", "")
        last = row.get("lastName", "")
        name = f"{first} {last}".strip()
        seasons = row.get("seasons", []) or []
        if not seasons:
            continue

        # Find this coach's record at each school, keyed by school
        by_school = {}
        for s in seasons:
            school = s.get("school")
            if not school:
                continue
            by_school.setdefault(school, []).append(s)

        for school, srows in by_school.items():
            abbr = TEAM_NAME_TO_ABBR.get(school)
            if not abbr:
                continue
            srows.sort(key=lambda r: r.get("year", 0))
            current = next((r for r in srows if r.get("year") == SEASON), srows[-1])
            tenure_years = len(srows)

            total_wins = sum(r.get("wins", 0) or 0 for r in srows)
            total_losses = sum(r.get("losses", 0) or 0 for r in srows)
            total_games = total_wins + total_losses
            career_win_pct = round(total_wins / total_games, 3) if total_games else None

            cur_wins = current.get("wins", 0) or 0
            cur_losses = current.get("losses", 0) or 0
            cur_games = cur_wins + cur_losses
            season_win_pct = round(cur_wins / cur_games, 3) if cur_games else None

            # Only keep this coach for a school if they're the most recent entry there
            # (avoids overwriting a current coach with an earlier stint's data)
            existing = coaches_out.get(abbr)
            if existing and existing.get("_lastYear", 0) > current.get("year", 0):
                continue

            coaches_out[abbr] = {
                "name": name,
                "tenureYears": tenure_years,
                "winPctThisSeason": season_win_pct,
                "careerWinPctAtSchool": career_win_pct,
                "_lastYear": current.get("year", 0),
            }

    # Strip the internal bookkeeping field before returning
    for abbr in coaches_out:
        coaches_out[abbr].pop("_lastYear", None)

    print(f"  -> {len(coaches_out)} teams with coach data")
    return coaches_out


def pull_ppa_games(year):
    """Real per-game PPA (Predicted Points Added) for every team in a season —
    confirmed real endpoint (CFBD's own R client cfbfastR wraps this exact
    path as cfbd_metrics_ppa_games). Tries one bulk call first; falls back
    to per-team calls if the bulk call isn't supported on this tier."""
    rows = cfbd_get("/ppa/games", {"year": year})
    if not rows:
        print(f"  -> bulk /ppa/games?year={year} returned nothing — falling back to per-team calls")
        rows = []
        for school in TEAM_NAME_TO_ABBR:
            team_rows = cfbd_get("/ppa/games", {"year": year, "team": school})
            if team_rows:
                rows.extend(team_rows)
            time.sleep(0.1)
    return rows or []


EARLY_K = float(os.environ.get("CFB_EARLY_K", "4"))   # prior-season "pseudo-games"; 0 = old behavior (pure prior-games average)


def build_rolling_ratings(ppa_rows_by_year, returning_by_year=None, early_k=None):
    """Week-by-week PRIOR-games-only rolling average PPA per team, so a
    game's prediction only ever uses information that existed before that
    game was played — no lookahead into future weeks or the season's final
    numbers. Returns {year: {abbr: {week: {"off":x, "def":x}}}}.

    EARLY-SEASON BLEND (v2.1): rating = (sum of this season's prior games + K x carried prior-season rating) / (games + K).
    The carried prior-season rating is the prior full-season average scaled by returning production
    (carry = 0.35 + 0.65 x percentPPA returning, 0.70 if unknown), so a team that lost its QB and most of its production
    starts closer to average and the prior year fades as real games accumulate. K = EARLY_K (default 4); K = 0 restores the
    old pure prior-games average.

    Week 1 fallback (K = 0 only): uses the team's full-PRIOR-season average PPA if that
    season was also pulled (e.g. 2025 week 1 falls back to 2024's full-season
    numbers). If no prior season data exists at all (2024 week 1, no 2023
    pull), falls back to a neutral 0.0 — a real, disclosed simplification,
    not hidden.
    """
    # First pass: organize raw per-game rows by year/team, sorted by week
    by_year_team = {}
    season_avg_by_year_team = {}  # for cross-season week-1 fallback
    fcs_games_excluded = 0
    for year, rows in ppa_rows_by_year.items():
        by_year_team[year] = {}
        season_totals = {}
        for row in rows:
            abbr = TEAM_NAME_TO_ABBR.get(row.get("team"))
            if not abbr:
                continue
            # Discount FCS buy-games: a 40-point win over an FCS opponent
            # produces an unrealistically inflated PPA that shouldn't carry
            # the same weight as a real FBS matchup in the rolling rating.
            # Excluded outright (not partially discounted) since there's no
            # real data to justify a specific partial-weight factor.
            opponent = row.get("opponent")
            if opponent and opponent not in TEAM_NAME_TO_ABBR:
                fcs_games_excluded += 1
                continue
            week = row.get("week")
            off = row.get("offense", {}).get("overall") if isinstance(row.get("offense"), dict) else row.get("off_overall")
            defn = row.get("defense", {}).get("overall") if isinstance(row.get("defense"), dict) else row.get("def_overall")
            if off is None or defn is None or week is None:
                continue
            by_year_team[year].setdefault(abbr, []).append({"week": week, "off": off, "def": defn})
            season_totals.setdefault(abbr, {"off": [], "def": []})
            season_totals[abbr]["off"].append(off)
            season_totals[abbr]["def"].append(defn)
        season_avg_by_year_team[year] = {
            abbr: {"off": sum(v["off"]) / len(v["off"]), "def": sum(v["def"]) / len(v["def"])}
            for abbr, v in season_totals.items() if v["off"]
        }
        for abbr in by_year_team[year]:
            by_year_team[year][abbr].sort(key=lambda r: r["week"])

    if fcs_games_excluded:
        print(f"  -> excluded {fcs_games_excluded} FBS-vs-FCS buy-game rows from rolling ratings")

    # Second pass: build the rolling (prior-weeks-only) rating per team per week
    rolling = {}
    years_sorted = sorted(ppa_rows_by_year.keys())
    for year in years_sorted:
        rolling[year] = {}
        prior_year = year - 1
        for abbr, games in by_year_team[year].items():
            rolling[year][abbr] = {}
            prior_off, prior_def = [], []
            K = EARLY_K if early_k is None else early_k
            pa = season_avg_by_year_team.get(prior_year, {}).get(abbr)
            ret = ((returning_by_year or {}).get(year, {}).get(abbr) or {}).get("percentPPA")
            carry = (0.35 + 0.65 * max(0.0, min(1.0, ret))) if ret is not None else 0.70
            base_off, base_def = (pa["off"] * carry, pa["def"] * carry) if pa else (0.0, 0.0)
            for g in games:
                wk = g["week"]
                if K > 0:
                    n = len(prior_off)
                    rolling[year][abbr][wk] = {"off": (sum(prior_off) + K * base_off) / (n + K),
                                               "def": (sum(prior_def) + K * base_def) / (n + K), "n": n}
                elif prior_off:
                    rolling[year][abbr][wk] = {
                        "off": sum(prior_off) / len(prior_off),
                        "def": sum(prior_def) / len(prior_def),
                    }
                elif prior_year in season_avg_by_year_team and abbr in season_avg_by_year_team[prior_year]:
                    rolling[year][abbr][wk] = season_avg_by_year_team[prior_year][abbr]
                else:
                    rolling[year][abbr][wk] = {"off": 0.0, "def": 0.0}
                prior_off.append(g["off"])
                prior_def.append(g["def"])
    return rolling


LEAGUE_AVG_PTS_RETRO = 28.0   # matches the CFB_YDS_PER_PT-era baseline already used elsewhere
PPA_TO_POINTS_SCALE  = 32.2   # recalibrated from 70.0 after the real 2024/2025 backtest
                               # showed a 2.17x score inflation (117% too high) across
                               # 1,490 games — 70.0 * (1/2.17) ≈ 32.2. This is an
                               # empirical correction from actual outcomes, not a
                               # theoretical re-derivation, so re-check this again after
                               # a future backtest once more real data accumulates.
RETRO_HFA            = 2.5    # matches the live model's standard home-field-advantage value


def score_game_retroactively(away_abbr, home_abbr, week, year, rolling):
    """Simplified retroactive prediction using week-by-week rolling PPA —
    NOT a full replica of the live model's scoring engine (which also
    factors in coaching, injuries, weather, red zone %, divisional
    adjustments, etc. — none of which exist as historical inputs here).
    This captures the core team-strength signal the live model is built
    around, which is what actually matters for calibration purposes:
    is the model's fundamental team-strength read too aggressive, too
    conservative, biased toward home/away, etc.
    """
    yr_ratings = rolling.get(year, {})
    away_r = yr_ratings.get(away_abbr, {}).get(week)
    home_r = yr_ratings.get(home_abbr, {}).get(week)
    if not away_r or not home_r:
        return None

    # Raw CFBD PPA sign convention: positive offense = good, positive defense
    # = bad (allows more). Both offense and opponent-defense values point the
    # same direction for a team's expected scoring, so they add, not subtract.
    score_home = LEAGUE_AVG_PTS_RETRO + (home_r["off"] + away_r["def"]) * PPA_TO_POINTS_SCALE + RETRO_HFA
    score_away = LEAGUE_AVG_PTS_RETRO + (away_r["off"] + home_r["def"]) * PPA_TO_POINTS_SCALE

    score_home = max(0, score_home)
    score_away = max(0, score_away)

    return {
        "modelScoreH": round(score_home, 1),
        "modelScoreA": round(score_away, 1),
        "modelMargin": round(score_home - score_away, 1),
        "modelTotal": round(score_home + score_away, 1),
    }


def _normalize_team_name(name):
    """Loose normalization for matching bigballsdata's team naming against
    TEAM_NAME_TO_ABBR, since their exact convention isn't documented."""
    if not name:
        return ""
    return "".join(ch.lower() for ch in name if ch.isalnum())


_NORMALIZED_NAME_TO_ABBR = {_normalize_team_name(k): v for k, v in TEAM_NAME_TO_ABBR.items()}


def _resolve_bigballs_team(raw_name):
    """raw_name can be a plain string OR a nested team object (confirmed
    real shape: {'id':..., 'name': 'Villanova Wildcats', 'short_name':
    'VILL', 'logo_url':...}) — handle both rather than assume a string,
    since assuming string caused a 100% resolution failure (iterating over
    a dict's keys character-by-character instead of the team name)."""
    if not raw_name:
        return None

    if isinstance(raw_name, dict):
        # Try short_name against our own abbreviation values first — it's
        # often already close to a real abbreviation (e.g. their "NCSU"
        # for the team we call "NCST" — same team, different convention).
        short = raw_name.get("short_name")
        if short:
            short_norm = _normalize_team_name(short)
            for abbr in set(TEAM_NAME_TO_ABBR.values()):
                if _normalize_team_name(abbr) == short_norm:
                    return abbr
        name_str = raw_name.get("name") or short
    else:
        name_str = raw_name

    if not name_str:
        return None

    norm = _normalize_team_name(name_str)
    if norm in _NORMALIZED_NAME_TO_ABBR:
        return _NORMALIZED_NAME_TO_ABBR[norm]
    # Loose fallback: substring match against our known school names —
    # same spirit as other cross-source name resolution in this script.
    for known_norm, abbr in _NORMALIZED_NAME_TO_ABBR.items():
        if known_norm and (known_norm in norm or norm in known_norm):
            return abbr
    return None  # genuinely not one of our 138 tracked FBS teams (e.g. an FCS opponent)


def test_bigballs_match_statistics(bigballs_matches, sample_size=3):
    """Small, bounded test of GET /v1/matches/:id/statistics — confirmed
    player_stats=True on this account's free tier via /v1/coverage, and
    this endpoint looks like the practical access path (keyed by a match id
    we already have, not an individual player id we'd need to look up in
    bulk with no confirmed way to do so). Deliberately limited to a handful
    of real match ids with real, complete final scores (not upcoming/live
    games, since box scores are written by an hourly job after the game
    ends) — this is a verification pass, not a production pull, so it
    should never cost more than a few calls regardless of how many matches
    were pulled overall."""
    print(f"Testing bigballsdata /v1/matches/:id/statistics (bounded to {sample_size} real completed matches)...")
    if not BIGBALLS_API_KEY:
        print("  ! No BIGBALLS_API_KEY — skipping.")
        return None
    if not bigballs_matches:
        print("  -> no bigballsdata matches available to test against")
        return None

    completed = [m for m in bigballs_matches if (m.get("status") or "").lower() in ("finished", "final", "completed", "post")]
    if not completed:
        print("  -> no completed matches in this pull to test against (try again after some games finish)")
        return None

    tested = 0
    for m in completed[:sample_size]:
        match_id = m.get("id")
        if not match_id:
            continue
        resp = bigballs_get(f"/v1/matches/{match_id}/statistics")
        tested += 1
        if resp:
            data = resp.get("data") if isinstance(resp, dict) else resp
            print(f"  -> match {match_id}: real response received")
            print(f"     (top-level keys: {list(data.keys()) if isinstance(data, dict) else 'not a dict — ' + str(type(data))})")
            if isinstance(data, dict):
                for key, val in data.items():
                    if isinstance(val, list) and val:
                        print(f"     ('{key}' is a list of {len(val)} — sample item keys: {list(val[0].keys()) if isinstance(val[0], dict) else type(val[0])})")
        else:
            print(f"  -> match {match_id}: no response")

    print(f"  -> tested {tested} real match(es) — see keys above to confirm whether this response includes player-level rows")
    return None


def pull_bigballs_coverage():
    """One-call diagnostic against bigballsdata's own /v1/coverage endpoint
    — their documented way to check what's actually enabled for a given
    key/account in real time, rather than relying on marketing docs (which
    said Team Form + H2H Intelligence were free-tier; the account's actual
    403s said otherwise). Purely informational, printed for visibility."""
    if not BIGBALLS_API_KEY:
        return None
    print("Checking bigballsdata /v1/coverage for real account access status...")
    resp = bigballs_get("/v1/coverage")
    if resp:
        print(f"  -> real coverage response: {resp}")
    else:
        print("  -> no response (or already circuit-broken from an earlier failure)")
    return resp


def pull_bigballs_matches(season=None):
    """Real CFB scores from bigballsdata's free tier — a genuinely
    independent second source from CFBD/ESPN, used here for cross-
    validation (Phase 1), not as a primary source yet. Bulk endpoint,
    no per-team looping, stays well inside the free-tier rate limit
    (100 req/min, 1,000-2,000/day)."""
    if season is None:
        season = SEASON
    print(f"Pulling bigballsdata NCAAF matches for {season} (cross-validation)...")
    if not BIGBALLS_API_KEY:
        print("  ! No BIGBALLS_API_KEY — skipping bigballsdata cross-validation.")
        return []

    resp = bigballs_get("/v1/matches", {"sport": "american_football", "league": "ncaaf", "season": season})
    if not resp:
        print("  -> no response")
        return []

    rows = resp.get("data") if isinstance(resp, dict) else resp
    if not rows:
        print("  -> 0 matches returned")
        return []

    print(f"  -> {len(rows)} matches returned")
    print(f"     (sample match keys: {list(rows[0].keys())})")
    return rows


def build_bigballs_team_id_map(bigballs_matches):
    """Extracts an {abbr: bigballsdata_team_id} map from the real match
    data already pulled — home/away fields are nested team objects with
    real 'id'/'name'/'short_name' fields (confirmed from Phase 1). Needed
    because /v1/teams/:id/form and /v1/teams/:id/h2h-intelligence are
    keyed by bigballsdata's own team id, not our internal abbreviations."""
    id_map = {}
    for m in bigballs_matches or []:
        for side in ("home", "away"):
            raw = m.get(side)
            if not isinstance(raw, dict):
                continue
            abbr = _resolve_bigballs_team(raw)
            team_id = raw.get("id")
            if abbr and team_id and abbr not in id_map:
                id_map[abbr] = team_id
    return id_map


def pull_bigballs_team_form(team_id_map):
    """Real recent win/loss form per team from bigballsdata's free tier
    (/v1/teams/:id/form) — confirmed free-tier endpoint. One call per
    resolved team (~138 max), well inside the free-tier rate limit."""
    print("Pulling bigballsdata team form...")
    if not BIGBALLS_API_KEY:
        print("  ! No BIGBALLS_API_KEY — skipping.")
        return {}
    if not team_id_map:
        print("  -> no resolved team IDs to pull form for")
        return {}

    out = {}
    sample_printed = False
    for abbr, team_id in team_id_map.items():
        resp = bigballs_get(f"/v1/teams/{team_id}/form")
        if not resp:
            continue
        data = resp.get("data") if isinstance(resp, dict) else resp
        if not data:
            continue
        if not sample_printed:
            print(f"     (sample team-form keys: {list(data.keys()) if isinstance(data, dict) else 'not a dict — ' + str(type(data))})")
            sample_printed = True
        out[abbr] = data

    print(f"  -> real form data for {len(out)} of {len(team_id_map)} resolved teams")
    return out


def pull_bigballs_h2h_for_games(games, team_id_map):
    """Real head-to-head history for each REAL scheduled game (from the
    ESPN pull), not all 138x137 possible team combinations — bounded to
    actual games keeps this well inside the free-tier budget (~24-100
    calls depending on how many games are currently scheduled, not
    thousands). Uses /v1/teams/:id/h2h-intelligence, confirmed free-tier."""
    print("Pulling bigballsdata head-to-head intelligence for scheduled games...")
    if not BIGBALLS_API_KEY:
        print("  ! No BIGBALLS_API_KEY — skipping.")
        return {}
    if not team_id_map:
        print("  -> no resolved team IDs to pull h2h for")
        return {}

    out = {}
    sample_printed = False
    pulled = 0
    for g in (games or []):
        home_abbr = g.get("homeTeam")
        away_abbr = g.get("awayTeam")
        home_id = team_id_map.get(home_abbr)
        away_id = team_id_map.get(away_abbr)
        if not home_id or not away_id:
            continue
        resp = bigballs_get(f"/v1/teams/{home_id}/h2h-intelligence", {"opponent": away_id})
        if not resp:
            continue
        data = resp.get("data") if isinstance(resp, dict) else resp
        if not data:
            continue
        pulled += 1
        if not sample_printed:
            print(f"     (sample h2h-intelligence keys: {list(data.keys()) if isinstance(data, dict) else 'not a dict — ' + str(type(data))})")
            sample_printed = True
        out[f"{away_abbr}_{home_abbr}"] = data

    print(f"  -> real h2h data for {pulled} of {len(games or [])} scheduled games")
    return out


def pull_bigballs_standings(season=None):
    """Real conference/division standings from bigballsdata's free tier —
    cross-validated against whatever win/loss records CFBD/ESPN already
    provide, rather than trusted blindly as a second primary source."""
    if season is None:
        season = SEASON
    print(f"Pulling bigballsdata NCAAF standings for {season} (cross-validation)...")
    if not BIGBALLS_API_KEY:
        print("  ! No BIGBALLS_API_KEY — skipping.")
        return []

    resp = bigballs_get("/v1/standings", {"sport": "american_football", "league": "ncaaf", "season": season})
    if not resp:
        print("  -> no response")
        return []

    rows = resp.get("data") if isinstance(resp, dict) else resp

    # The matches endpoint confirmed "data" is a flat list, but standings
    # turned out to be double-nested: {"data": {"standings": [{"league_id":,
    # "rows": [...actual per-team standings...]}, ...]}}. Unwrap both
    # levels rather than assuming a single level, and print the real
    # structure either way so a wrong guess is visible immediately instead
    # of silently returning conference-summary objects with no team data.
    if isinstance(rows, dict):
        print(f"  -> standings returned as a dict, not a flat list (keys: {list(rows.keys())[:10]})")
        conference_entries = []
        for key, val in rows.items():
            if isinstance(val, list):
                conference_entries.extend(val)

        # Each conference entry may itself wrap the real team rows under
        # its own "rows" key (confirmed shape) — unwrap that second level.
        flattened = []
        for entry in conference_entries:
            if isinstance(entry, dict) and isinstance(entry.get("rows"), list):
                flattened.extend(entry["rows"])
            elif isinstance(entry, dict):
                flattened.append(entry)  # already a flat team row, no further nesting

        if flattened:
            print(f"  -> flattened {len(flattened)} team-standing rows out of the grouped response")
            print(f"     (sample standings row keys: {list(flattened[0].keys())})")
            return flattened
        print("  -> could not flatten into rows — inspect the printed keys above and adjust this function")
        return []

    if not rows:
        print("  -> 0 standings rows returned")
        return []

    print(f"  -> {len(rows)} standings rows returned")
    print(f"     (sample standings keys: {list(rows[0].keys())})")
    return rows


def cross_validate_with_bigballs(espn_games, bigballs_matches):
    """Compares final scores between ESPN (our current primary source) and
    bigballsdata (independent second source) for games both sides have,
    and flags disagreements rather than silently trusting either one.
    This is Phase 1's actual purpose: prove the two sources can be
    reconciled before anything downstream depends on bigballsdata data.
    Returns a list of flagged discrepancies for the output JSON, plus a
    console summary either way (agreement is worth confirming too).
    """
    print("Cross-validating bigballsdata vs ESPN scores...")
    if not bigballs_matches:
        print("  -> no bigballsdata matches to cross-check against")
        return []

    # Index bigballsdata matches by (home_abbr, away_abbr) for lookup.
    # "home"/"away" field names are confirmed real (from the sample-key
    # diagnostic). Scores live under a nested "score" object rather than
    # flat home_score/away_score fields — exact internal shape isn't
    # confirmed yet, so this tries the common patterns defensively and
    # prints one real "score"/"linescore" value the first time through,
    # so a wrong guess is visible immediately instead of silently matching
    # zero games.
    bb_by_matchup = {}
    dumped_score_sample = False
    resolved_count, unresolved_count = 0, 0
    unresolved_samples = []
    for m in bigballs_matches:
        home_raw = m.get("home") or m.get("home_team") or m.get("homeTeam")
        away_raw = m.get("away") or m.get("away_team") or m.get("awayTeam")
        home_abbr = _resolve_bigballs_team(home_raw)
        away_abbr = _resolve_bigballs_team(away_raw)
        if not home_abbr or not away_abbr:
            unresolved_count += 1
            if len(unresolved_samples) < 5:
                unresolved_samples.append(f"{away_raw!r} @ {home_raw!r}")
            continue
        resolved_count += 1
        if not dumped_score_sample:
            print(f"     (sample match — status: {m.get('status')!r}, score: {m.get('score')!r}, linescore: {m.get('linescore')!r})")
            dumped_score_sample = True
        bb_by_matchup[(away_abbr, home_abbr)] = m

    print(f"  -> {resolved_count} bigballsdata matches resolved to known team abbreviations, {unresolved_count} did not")
    if unresolved_samples:
        print(f"     (unresolved examples: {unresolved_samples})")

    def _extract_bb_score(match, side):
        """side is 'home' or 'away'. Tries every plausible nested/flat shape
        for bigballsdata's score field rather than assuming one."""
        score = match.get("score")
        if isinstance(score, dict):
            for key in (side, f"{side}_score", f"{side}Score"):
                if key in score and score[key] is not None:
                    return score[key]
        flat_key = f"{side}_score" if side == "home" else f"{side}_score"
        return match.get(f"{side}_score") or match.get(f"{side}Score")

    matched, agreed, disagreed = 0, 0, []
    espn_keys_sample = [(g.get("awayTeam"), g.get("homeTeam")) for g in (espn_games or [])][:5]
    bb_keys_sample = list(bb_by_matchup.keys())[:5]
    print(f"     (sample ESPN team-pair keys: {espn_keys_sample})")
    print(f"     (sample bigballsdata team-pair keys: {bb_keys_sample})")
    for g in (espn_games or []):
        key = (g.get("awayTeam"), g.get("homeTeam"))
        bb_match = bb_by_matchup.get(key)
        if not bb_match:
            continue
        matched += 1

        # Only compare completed games — a live/in-progress game's score
        # can differ between two sources purely from polling timing (one
        # source catches a touchdown a few seconds before the other), which
        # would show up as a false "discrepancy" that isn't a real data
        # quality issue. Status strings aren't confirmed identical between
        # sources, so check loosely rather than assume an exact match.
        espn_status = (g.get("status") or "").lower()
        bb_status = (bb_match.get("status") or "").lower()
        espn_final = "final" in espn_status
        bb_final = bb_status in ("final", "completed", "post", "finished")
        if not (espn_final and bb_final):
            continue

        espn_home_score = g.get("homeScore")
        espn_away_score = g.get("awayScore")
        bb_home_score = _extract_bb_score(bb_match, "home")
        bb_away_score = _extract_bb_score(bb_match, "away")
        if espn_home_score is None or bb_home_score is None:
            continue
        if espn_home_score == bb_home_score and espn_away_score == bb_away_score:
            agreed += 1
        else:
            disagreed.append({
                "matchup": f"{key[0]} @ {key[1]}",
                "espn": f"{espn_away_score}-{espn_home_score}",
                "bigballsdata": f"{bb_away_score}-{bb_home_score}",
            })

    print(f"  -> {matched} games matched between sources, {agreed} agreed, {len(disagreed)} disagreed")
    if disagreed:
        for d in disagreed[:10]:
            print(f"     ! SCORE MISMATCH {d['matchup']}: ESPN={d['espn']} vs bigballsdata={d['bigballsdata']}")
    return disagreed


def pull_historical_games(years=None):
    """Real final scores + real closing lines for past AND in-progress CFB
    seasons, PLUS a retroactive model prediction per game using week-by-week
    rolling PPA ratings — so the Lab tab's backtest KPIs (Winner%, ATS%,
    O/U%, margin error) are real, not blank.

    2026 (the current season) works the same way as 2024/2025: only games
    that have actually been played get included (checked via homePoints/
    awayPoints being set), and each one's prediction uses only rolling PPA
    from PRIOR weeks that season — so as 2026 unfolds, this becomes an
    honest, real-time read on how well-calibrated the model's core
    team-strength logic is *this season*, not just a historical exercise.

    IMPORTANT — what the retroactive prediction is and isn't:
    This uses real, period-appropriate team strength (rolling PPA, prior
    weeks only — no lookahead) run through a SIMPLIFIED version of the
    live model's core scoring logic. It is not a perfect replica of the
    full live model (no coaching/injury/weather/red-zone/divisional
    adjustments — none of those exist as clean historical inputs), but it
    captures the fundamental team-strength signal, which is what actually
    matters for finding calibration bias (too aggressive on favorites?
    biased on totals? etc). Treat backtest results as "how well-calibrated
    is the model's core team-strength read," not a claim of full-feature
    parity with a live 2026 projection.
    """
    if years is None:
        years = [2024, 2025, 2026]
    print(f"Pulling historical CFB games for {years}...")
    out = {}

    if not CFBD_API_KEY:
        print("  ! No CFBD_API_KEY — skipping historical games.")
        return out

    valid_schools = set(TEAM_NAME_TO_ABBR.keys())

    print("  Pulling per-game PPA for rolling ratings (this powers the retroactive predictions)...")
    ppa_rows_by_year = {}
    for year in years:
        ppa_rows_by_year[year] = pull_ppa_games(year)
        print(f"  -> {year}: {len(ppa_rows_by_year[year])} team-game PPA rows")
        if ppa_rows_by_year[year] and year == years[0]:
            sample_keys = list(ppa_rows_by_year[year][0].keys())
            print(f"     (sample row keys: {sample_keys})")
    # Also grab the year before the earliest requested year, so that year's
    # week-1 games get a real prior-season fallback instead of a flat 0.0.
    earliest_prior = min(years) - 1
    if earliest_prior not in ppa_rows_by_year:
        ppa_rows_by_year[earliest_prior] = pull_ppa_games(earliest_prior)
        print(f"  -> {earliest_prior} (prior-season fallback only): {len(ppa_rows_by_year[earliest_prior])} rows")

    returning_by_year = {y: pull_returning_production(y) for y in years}
    rolling = build_rolling_ratings(ppa_rows_by_year, returning_by_year)
    total_ratings = sum(len(v) for yr in rolling.values() for v in yr.values())
    print(f"  -> built rolling ratings for {total_ratings} team-weeks across all years"
          f"{' (0 means the off/def field extraction likely needs adjusting — check the sample row keys above)' if total_ratings == 0 else ''}")

    for year in years:
        games = cfbd_get("/games", {"year": year, "seasonType": "regular", "division": "fbs"})
        lines_data = cfbd_get("/lines", {"year": year, "seasonType": "regular"})

        # Index lines by (week, home, away) for merge
        lines_idx = {}
        for row in (lines_data or []):
            key = (row.get("week"), row.get("homeTeam"), row.get("awayTeam"))
            lines_list = row.get("lines", []) or []
            if not lines_list:
                continue
            # Prefer a consensus/median line if present, else first available
            chosen = lines_list[0]
            lines_idx[key] = chosen

        year_games = []
        scored = 0
        for g in (games or []):
            home = g.get("homeTeam")
            away = g.get("awayTeam")
            if home not in valid_schools or away not in valid_schools:
                continue  # only keep games between teams this model tracks
            if g.get("homePoints") is None or g.get("awayPoints") is None:
                continue  # game hasn't been played / no final score

            week = g.get("week")
            line_row = lines_idx.get((week, home, away), {})
            spread = line_row.get("spread")
            over_under = line_row.get("overUnder")
            home_abbr = TEAM_NAME_TO_ABBR[home]
            away_abbr = TEAM_NAME_TO_ABBR[away]

            game_out = {
                "wk": week,
                "away": away_abbr,
                "home": home_abbr,
                "awayScore": g.get("awayPoints"),
                "homeScore": g.get("homePoints"),
                "spread": float(spread) if spread not in (None, "") else None,
                "total": float(over_under) if over_under not in (None, "") else None,
                "note": "",
            }

            retro = score_game_retroactively(away_abbr, home_abbr, week, year, rolling)
            if retro:
                game_out.update(retro)
                scored += 1
            # else: leaves modelScoreA/H/Margin/Total absent, exactly as
            # before — btNormalizeGame() shows "—" rather than faking it.

            year_games.append(game_out)

        out[str(year)] = year_games
        print(f"  -> {year}: {len(year_games)} games ({scored} with retroactive model predictions)")

    return out


def pull_injuries():
    """CFBD's public API has no injuries endpoint — confirmed by checking
    their actual API spec (github.com/CFBD/cfb-api/swagger.json): the tag
    list covers games/drives/plays/teams/coaches/players/rankings/betting/
    recruiting/ratings/metrics/stats/draft, with no "injuries" category at
    all. The earlier /player/injuries guess 404'd for exactly that reason.
    Returns {} without making a request — there's nothing to call.
    Injury status may become available via ESPN's team injuries endpoint
    (site.api.espn.com/.../teams/{id}/injuries) in a future pass if needed."""
    print("Pulling injuries...")
    print("  -> CFBD has no public injuries endpoint (confirmed against their API spec) — skipping.")
    return {}


# ── CURRENT ROSTER (replaces the ESPN depth-chart approach) ────────────────
# ESPN's API explicitly does not support depth charts for college football —
# confirmed directly from their own error response: "Depth charts not
# supported for football|college-football|2026|<team_id>" (HTTP 400 from
# their Core API), and their older site.api.espn.com endpoint returns 200
# but with no depth-chart content at all, just team metadata. This isn't a
# parsing problem — the feature doesn't exist for this sport. Confirmed via
# two independent real requests, not a guess.
#
# The more useful thing CFBD actually offers is /roster — the real, current
# roster for each team. That doesn't tell us who's "starting," but it solves
# the more important problem this project started with: whether last
# season's top performers (baked into the HTML from 2025 PFF data) are still
# even on the team. If a name doesn't appear on the current roster, they've
# very likely graduated, entered the portal, or been drafted.

ROSTER_POS = {}  # (ABBR, 'Full Name') -> position, filled by pull_current_rosters()


def pull_current_rosters():
    """Real current-season roster per team from CFBD's /roster endpoint.
    Returns {ABBR: ["Full Player Name", ...]} — used by the HTML to check
    whether its baked-in 2025 top-performer names are still actually on
    the roster, rather than trusting stale season data indefinitely."""
    print("Pulling current CFBD rosters...")
    rosters_out = {}

    if not CFBD_API_KEY:
        print("  ! No CFBD_API_KEY — skipping roster pull.")
        return rosters_out

    # Try one bulk call for the whole season first — much cheaper than 138
    # per-team calls if CFBD supports it without a team filter.
    rows = cfbd_get("/roster", {"year": SEASON})
    if not rows:
        print("  -> bulk /roster call returned nothing — falling back to per-team calls")
        rows = []
        for school in TEAM_NAME_TO_ABBR:
            team_rows = cfbd_get("/roster", {"year": SEASON, "team": school})
            if team_rows:
                rows.extend(team_rows)
            time.sleep(0.15)

    if not rows:
        print("  ! No roster data returned at all — check your key/tier, or the season may be too early.")
        return rosters_out

    for row in rows:
        school = row.get("team")
        abbr = TEAM_NAME_TO_ABBR.get(school)
        if not abbr:
            continue
        first = row.get("firstName") or row.get("first_name") or ""
        last = row.get("lastName") or row.get("last_name") or ""
        full_name = f"{first} {last}".strip()
        if full_name:
            rosters_out.setdefault(abbr, []).append(full_name)
            ROSTER_POS[(abbr, full_name)] = row.get("position")

    print(f"  -> rosters for {len(rosters_out)} teams, {sum(len(v) for v in rosters_out.values())} total players")
    return rosters_out


def pull_transfer_portal(years=None):
    """Direct transfer-portal entries from CFBD's /player/portal endpoint —
    a much more reliable signal than inferring a transfer from "this name
    disappeared from a roster pull." Returns {"Player Name": {"origin":,
    "destination":, "year":, "stars":}} for anyone found in the portal for
    the given year(s), which the HTML can use to flag a seeded player as
    departed even before/independent of the general roster cross-check.
    """
    if years is None:
        years = [SEASON]
    print(f"Pulling transfer portal data for {years}...")
    out = {}

    if not CFBD_API_KEY:
        print("  ! No CFBD_API_KEY — skipping transfer portal pull.")
        return out

    for year in years:
        rows = cfbd_get("/player/portal", {"year": year})
        if not rows:
            print(f"  -> {year}: no portal data returned")
            continue
        for row in rows:
            first = row.get("firstName") or row.get("first_name") or ""
            last = row.get("lastName") or row.get("last_name") or ""
            full_name = f"{first} {last}".strip()
            if not full_name:
                continue
            out[full_name] = {
                "origin": row.get("origin"),
                "destination": row.get("destination"),
                "year": year,
                "stars": row.get("stars"),
                "transferDate": row.get("transferDate"),
            }
        print(f"  -> {year}: {len(rows)} portal entries")

    print(f"  -> {len(out)} total unique players tracked in the portal")
    return out


# ════════════════════════════════════════════════════════════════════════
# v2.0 ADDITIONS — defensive + offensive CFBD data, venues (weather) — one call per endpoint
# unless noted. Every function degrades to {} / [] on any failure and never raises, so one bad
# endpoint can never break the whole pull. Shapes follow CFBD's published API; fields are read with
# .get() fallbacks (CFBD mixes camelCase and snake_case between endpoints).
# ════════════════════════════════════════════════════════════════════════
CACHE_PATH = os.environ.get("CFB_CACHE_PATH", "cfb_pull_cache.json")  # per-week box-score summaries, so each week is pulled only once


def _g(d, *keys, default=None):
    """First non-None value among several possible key spellings."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d.get(k)
    return default


def _num(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _abbr(school):
    return TEAM_NAME_TO_ABBR.get(school)


def _adv_side(side):
    """Flatten one side (offense or defense) of a /stats/season/advanced row."""
    side = side or {}
    hv = side.get("havoc") or {}
    sd = side.get("standardDowns") or {}
    pd = side.get("passingDowns") or {}
    rp = side.get("rushingPlays") or {}
    pp = side.get("passingPlays") or {}
    return {
        "plays": _num(side.get("plays")),
        "ppa": _num(side.get("ppa")),
        "successRate": _num(side.get("successRate")),
        "explosiveness": _num(side.get("explosiveness")),
        "powerSuccess": _num(side.get("powerSuccess")),
        "stuffRate": _num(side.get("stuffRate")),
        "lineYards": _num(side.get("lineYards")),
        "secondLevelYards": _num(side.get("secondLevelYards")),
        "openFieldYards": _num(side.get("openFieldYards")),
        "pointsPerOpportunity": _num(side.get("pointsPerOpportunity")),
        "havocTotal": _num(hv.get("total")),
        "havocFront7": _num(hv.get("frontSeven")),
        "havocDB": _num(hv.get("db")),
        "stdDownsSR": _num(sd.get("successRate")),
        "stdDownsPPA": _num(sd.get("ppa")),
        "passDownsSR": _num(pd.get("successRate")),
        "passDownsPPA": _num(pd.get("ppa")),
        "rushSR": _num(rp.get("successRate")),
        "rushPPA": _num(rp.get("ppa")),
        "rushExplosiveness": _num(rp.get("explosiveness")),
        "passSR": _num(pp.get("successRate")),
        "passPPA": _num(pp.get("ppa")),
        "passExplosiveness": _num(pp.get("explosiveness")),
    }


def pull_team_advanced():
    """/stats/season/advanced — success rate, explosiveness, line yards, stuff rate, power success,
    points per scoring opportunity, havoc, down/distance and rush-vs-pass splits. Offense AND defense."""
    print("Pulling CFBD advanced team stats (success rate / explosiveness / line yards / havoc)...")
    out = {}
    rows = cfbd_get("/stats/season/advanced", {"year": SEASON, "excludeGarbageTime": "true"})
    for row in rows or []:
        abbr = _abbr(row.get("team"))
        if abbr:
            out[abbr] = {"off": _adv_side(row.get("offense")), "def": _adv_side(row.get("defense"))}
    print(f"  -> {len(out)} teams with advanced stats")
    return out


# Team-stat names kept from /stats/season (long format: one row per team+statName). Anything not listed is dropped.
KEEP_TEAM_STATS = {
    "games", "sacks", "tacklesForLoss", "passesIntercepted", "passesDeflected", "interceptions", "fumblesRecovered",
    "fumblesLost", "turnovers", "penalties", "penaltyYards", "thirdDowns", "thirdDownConversions", "fourthDowns",
    "fourthDownConversions", "firstDowns", "totalYards", "netPassingYards", "rushingYards", "passAttempts",
    "passCompletions", "rushingAttempts", "possessionTime", "sacksOpponent", "tacklesForLossOpponent",
    "totalYardsOpponent", "netPassingYardsOpponent", "rushingYardsOpponent", "passAttemptsOpponent",
    "rushingAttemptsOpponent", "thirdDownsOpponent", "thirdDownConversionsOpponent", "turnoversOpponent",
    "firstDownsOpponent", "passesInterceptedOpponent",
}


def pull_team_season_stats():
    """/stats/season — pace/volume + sacks, TFL, passes defended, takeaways, 3rd-down conversion, both directions."""
    print("Pulling CFBD team season stats (sacks / TFL / takeaways / 3rd down / pace)...")
    out = {}
    rows = cfbd_get("/stats/season", {"year": SEASON})
    for row in rows or []:
        abbr = _abbr(row.get("team"))
        name = _g(row, "statName", "stat_name")
        if not abbr or name not in KEEP_TEAM_STATS:
            continue
        val = _num(_g(row, "statValue", "stat_value"))
        if val is not None:
            out.setdefault(abbr, {})[name] = val
    print(f"  -> {len(out)} teams with season stats")
    return out


def pull_ppa_players():
    """/ppa/players/season — per-player predicted points added (QB/RB/WR/TE offense and defenders)."""
    print("Pulling CFBD player PPA...")
    rows = cfbd_get("/ppa/players/season", {"year": SEASON, "threshold": 25, "excludeGarbageTime": "true"})
    out = []
    for row in rows or []:
        abbr = _abbr(row.get("team"))
        if not abbr or not row.get("name"):
            continue
        avg = row.get("averagePPA") or {}
        tot = row.get("totalPPA") or {}
        out.append({
            "name": row["name"], "team": abbr, "pos": row.get("position"),
            "avgAll": _num(avg.get("all")), "avgPass": _num(avg.get("pass")), "avgRush": _num(avg.get("rush")),
            "avgThirdDown": _num(avg.get("thirdDown")), "avgPassingDowns": _num(avg.get("passingDowns")),
            "totalAll": _num(tot.get("all")),
        })
    print(f"  -> {len(out)} player PPA rows")
    return out


def pull_player_usage(year=None):
    """/player/usage — share of team plays each player is used on (overall / pass / rush / by down). year=SEASON-1 gives the
    PRIOR-season role of this year's portal transfers (their old team), which anchors the transfer-portal projection."""
    year = year or SEASON
    print(f"Pulling CFBD player usage ({year})...")
    cache = _load_cache()
    uc = cache.setdefault("player_usage", {})
    if year < SEASON and str(year) in uc:
        print(f"  -> {len(uc[str(year)])} cached player usage rows for {year}")
        return uc[str(year)]
    rows = cfbd_get("/player/usage", {"year": year, "excludeGarbageTime": "true"})
    out = []
    for row in rows or []:
        abbr = _abbr(row.get("team"))
        u = row.get("usage") or {}
        if not abbr or not row.get("name"):
            continue
        out.append({
            "name": row["name"], "team": abbr, "pos": row.get("position"),
            "overall": _num(u.get("overall")), "pass": _num(u.get("pass")), "rush": _num(u.get("rush")),
            "thirdDown": _num(u.get("thirdDown")), "passingDowns": _num(u.get("passingDowns")),
        })
    print(f"  -> {len(out)} player usage rows")
    if year < SEASON and out:
        uc[str(year)] = out
        _save_cache(cache)
    return out


def pull_returning_production(year=None):
    """/player/returning — percent of last season's production (PPA / usage) that returns, by team. Past years are cached
    (they feed the early-season blend in the retro backtest)."""
    year = year or SEASON
    print(f"Pulling CFBD returning production ({year})...")
    cache = _load_cache()
    rc = cache.setdefault("returning", {})
    if year < SEASON and str(year) in rc:
        return rc[str(year)]
    rows = cfbd_get("/player/returning", {"year": year})
    out = {}
    for row in rows or []:
        abbr = _abbr(row.get("team"))
        if not abbr:
            continue
        out[abbr] = {k: _num(_g(row, k, snake)) for k, snake in [
            ("percentPPA", "percent_ppa"), ("percentPassingPPA", "percent_passing_ppa"),
            ("percentReceivingPPA", "percent_receiving_ppa"), ("percentRushingPPA", "percent_rushing_ppa"),
            ("usage", "usage"), ("passingUsage", "passing_usage"), ("receivingUsage", "receiving_usage"),
            ("rushingUsage", "rushing_usage")]}
    print(f"  -> returning production for {len(out)} teams")
    if year < SEASON and out:
        rc[str(year)] = out
        _save_cache(cache)
    return out


def pull_team_meta_and_venues():
    """/teams/fbs (names, colors, logos, home venue with lat/lon/dome/elevation/timezone) and /venues (every venue by
    name, so neutral-site and bowl games resolve too). Feeds weather (Open-Meteo forecast) and the matchup card."""
    print("Pulling CFBD team metadata + venues (weather coordinates)...")
    team_meta, home_venue, by_name = {}, {}, {}
    for t in cfbd_get("/teams/fbs", {"year": SEASON}) or []:
        abbr = _abbr(t.get("school"))
        if not abbr:
            continue
        logos = t.get("logos") or []
        team_meta[abbr] = {"school": t.get("school"), "mascot": t.get("mascot"), "conference": t.get("conference"),
                           "color": t.get("color"), "altColor": _g(t, "alt_color", "altColor"), "logo": logos[0] if logos else None,
                           "espnId": t.get("id")}   # CFBD team id == ESPN team id (logo URLs, no network lookup needed)
        loc = t.get("location") or {}
        lat, lon = _num(_g(loc, "latitude", "y")), _num(_g(loc, "longitude", "x"))
        if lat is not None and lon is not None:
            home_venue[abbr] = {"venue": loc.get("name"), "lat": lat, "lon": lon, "dome": bool(loc.get("dome")),
                                "grass": loc.get("grass"), "elevation": _num(loc.get("elevation")),
                                "timezone": loc.get("timezone"), "city": loc.get("city"), "state": loc.get("state")}
    for v in cfbd_get("/venues") or []:
        name = v.get("name")
        loc = v.get("location") or {}
        lat, lon = _num(_g(loc, "y", "latitude")), _num(_g(loc, "x", "longitude"))
        if name and lat is not None and lon is not None:
            by_name[name.strip().lower()] = {"venue": name, "lat": lat, "lon": lon, "dome": bool(v.get("dome")),
                                             "grass": v.get("grass"), "elevation": _num(v.get("elevation")),
                                             "timezone": v.get("timezone"), "city": v.get("city"), "state": v.get("state")}
    print(f"  -> {len(team_meta)} teams, {len(home_venue)} home venues, {len(by_name)} venues by name")
    return team_meta, home_venue, by_name


def _pull_line_history_one(year):
    games = {}
    for g in cfbd_get("/games", {"year": year, "seasonType": "regular"}) or []:
        gid = g.get("id")
        if gid is None:
            continue
        games[gid] = {"id": gid, "season": year, "week": g.get("week"),
                      "home": _abbr(_g(g, "homeTeam", "home_team")), "away": _abbr(_g(g, "awayTeam", "away_team")),
                      "homeConf": _g(g, "homeConference", "home_conference"), "awayConf": _g(g, "awayConference", "away_conference"),
                      "neutral": bool(_g(g, "neutralSite", "neutral_site")),
                      "homePts": _g(g, "homePoints", "home_points"), "awayPts": _g(g, "awayPoints", "away_points")}
    out = []
    for g in cfbd_get("/lines", {"year": year, "seasonType": "regular"}) or []:
        base = games.get(g.get("id"))
        if not base or base["homePts"] is None or base["awayPts"] is None:
            continue
        pick = None
        for ln in g.get("lines") or []:
            sp, tot = _num(ln.get("spread")), _num(_g(ln, "overUnder", "over_under"))
            if sp is None or tot is None:
                continue
            if pick is None or str(ln.get("provider", "")).lower() in ("draftkings", "consensus"):
                pick = {"book": ln.get("provider"), "spread": sp, "total": tot}
        if pick:
            out.append({**base, **pick})   # spread is the HOME line (negative = home favored), CFBD convention
    return out


def pull_line_history(years=None):
    """/games + /lines per season: closing spread/total, conferences and final score for every finished game. Feeds the
    college win-margin distribution (2024-25 baseline + current season) and edge-tier calibration (the 3/6/9-point
    cut-offs came from NFL results). Finished seasons are cached, so only the current season costs calls after the first pull."""
    years = years or [SEASON - 2, SEASON - 1, SEASON]
    print(f"Pulling CFBD betting-line history + results for {years} (win-margin distribution / edge-tier calibration)...")
    cache = _load_cache()
    lc = cache.setdefault("line_history", {})
    out = []
    for y in years:
        if str(y) in lc and y < SEASON:
            rows = lc[str(y)]
        else:
            rows = _pull_line_history_one(y)
            if rows and y < SEASON:
                lc[str(y)] = rows
        print(f"  {y}: {len(rows)} finished games with a closing line")
        out.extend(rows)
    _save_cache(cache)
    return out


# ── v2.1 additions: current lines w/ openers, pace, ATS/OU records, injuries ─────────────────────
_GAMES_CACHE = {}


def _season_games():
    """CFBD /games for the current season, fetched once per run and shared (current lines + the schedule block)."""
    if "g" not in _GAMES_CACHE:
        _GAMES_CACHE["g"] = cfbd_get("/games", {"year": SEASON, "seasonType": "regular"}) or []
    return _GAMES_CACHE["g"]


def pull_schedule_block():
    """The CFBD schedule the model's Fetch Games / date-range load requires: scheduleGames (upcoming, from CFBD /games) and
    teamGameLedger (finished games), both carrying CFBD game ids, abbreviations the model uses for teams, and ISO dates.
    Shares the one /games call with pull_current_lines(). Games with a team outside the model's list are skipped."""
    print("Building the CFBD schedule block (upcoming games + finished-game ledger)...")
    sched, ledger, skipped = [], [], 0
    for g in _season_games():
        gid = g.get("id")
        home_n, away_n = _g(g, "homeTeam", "home_team"), _g(g, "awayTeam", "away_team")
        home, away = _abbr(home_n), _abbr(away_n)
        date = _g(g, "startDate", "start_date")
        if gid is None or not home or not away or not date:
            skipped += 1
            continue
        hp, ap = _g(g, "homePoints", "home_points"), _g(g, "awayPoints", "away_points")
        row = {"provider": "CFBD", "season": SEASON, "id": str(gid), "week": g.get("week"), "home": home, "away": away,
               "homeName": home_n, "awayName": away_n, "date": date, "venue": g.get("venue") or "",
               "neutralSite": bool(_g(g, "neutralSite", "neutral_site")), "startTimeTBD": bool(_g(g, "startTimeTBD", "start_time_tbd")),
               "seasonType": str(_g(g, "seasonType", "season_type") or "regular").lower()}
        if hp is not None and ap is not None:
            row.update({"completed": True, "homeScore": hp, "awayScore": ap})
            ledger.append(row)
        else:
            row["completed"] = False
            sched.append(row)
    print(f"  -> {len(sched)} upcoming games, {len(ledger)} finished games ({skipped} skipped: team not in the model's list or no date)")
    return sched, ledger


def pull_current_lines():
    """/games + /lines for the CURRENT season, upcoming (unfinished) games only. Keeps every book plus the OPENING numbers
    CFBD carries per line (spreadOpen / overUnderOpen — field names per CFBD's API docs but NOT verified against a live
    response, so every read is guarded and a missing opener just means 'no opener available'). One morning pull therefore
    gives line movement (open -> now) without needing a daily snapshot. 2 calls."""
    print("Pulling CFBD current lines with opening numbers (line movement)...")
    base = {}
    for g in _season_games():
        gid = g.get("id")
        if gid is None or _g(g, "homePoints", "home_points") is not None:
            continue                                   # finished games are handled by pull_line_history
        base[gid] = {"id": gid, "week": g.get("week"), "date": _g(g, "startDate", "start_date"),
                     "home": _abbr(_g(g, "homeTeam", "home_team")), "away": _abbr(_g(g, "awayTeam", "away_team")),
                     "neutral": bool(_g(g, "neutralSite", "neutral_site"))}
    out = []
    for g in cfbd_get("/lines", {"year": SEASON, "seasonType": "regular"}) or []:
        b = base.get(g.get("id"))
        if not b:
            continue
        books = []
        for ln in g.get("lines") or []:
            books.append({"book": ln.get("provider"),
                          "spread": _num(ln.get("spread")),
                          "spreadOpen": _num(_g(ln, "spreadOpen", "spread_open")),
                          "total": _num(_g(ln, "overUnder", "over_under")),
                          "totalOpen": _num(_g(ln, "overUnderOpen", "over_under_open")),
                          "homeML": _num(_g(ln, "homeMoneyline", "home_moneyline")),
                          "awayML": _num(_g(ln, "awayMoneyline", "away_moneyline"))})
        books = [x for x in books if x["spread"] is not None or x["total"] is not None]
        if not books:
            continue
        pick = next((x for x in books if str(x["book"]).lower() in ("draftkings", "consensus")), books[0])
        out.append({**b, "books": books, "spread": pick["spread"], "spreadOpen": pick["spreadOpen"],
                    "total": pick["total"], "totalOpen": pick["totalOpen"]})   # spread = HOME line (negative = home favored)
    n_open = sum(1 for r in out if r.get("spreadOpen") is not None)
    print(f"  -> {len(out)} upcoming games with lines ({n_open} with an opening spread)")
    return out


def build_pace(team_season_stats, line_history):
    """Plays per game and points per play, both directions, from /stats/season totals (already pulled — no new calls) and
    this season's finished-game scores. Model use: projected total = ppp_off * opp-allowed plays blend; see model wrapper."""
    gp, pf, pa = {}, {}, {}
    for r in line_history or []:
        if r.get("season") != SEASON or r.get("homePts") is None or r.get("awayPts") is None:
            continue
        for tm, f, a in ((r["home"], r["homePts"], r["awayPts"]), (r["away"], r["awayPts"], r["homePts"])):
            gp[tm] = gp.get(tm, 0) + 1
            pf[tm] = pf.get(tm, 0) + f
            pa[tm] = pa.get(tm, 0) + a
    out = {}
    for tm, st in (team_season_stats or {}).items():
        g = st.get("games") or gp.get(tm)
        pl = (st.get("rushingAttempts") or 0) + (st.get("passAttempts") or 0)
        opl = (st.get("rushingAttemptsOpponent") or 0) + (st.get("passAttemptsOpponent") or 0)
        if not g or not pl:
            continue
        row = {"games": g, "playsPG": round(pl / g, 2)}
        if opl:
            row["oppPlaysPG"] = round(opl / g, 2)
        n = gp.get(tm)
        if n:
            row["ptsPG"] = round(pf[tm] / n, 2)
            row["oppPtsPG"] = round(pa[tm] / n, 2)
            row["ppp"] = round(pf[tm] / (pl / g * n), 4)                 # points scored per offensive play
            if opl:
                row["oppPpp"] = round(pa[tm] / (opl / g * n), 4)         # points allowed per opponent play
        out[tm] = row
    print(f"  -> pace / points-per-play for {len(out)} teams")
    return out


def build_ats_records(line_history):
    """ATS and over/under record per team per season from finished games that have a closing line (no new calls)."""
    recs = {}
    for r in line_history or []:
        if r.get("homePts") is None or r.get("spread") is None:
            continue
        margin = r["homePts"] - r["awayPts"]
        cover = margin + r["spread"]                  # >0 home covers (spread is HOME line)
        tot = (r["homePts"] + r["awayPts"]) - r["total"] if r.get("total") is not None else None
        for tm, sign in ((r["home"], 1), (r["away"], -1)):
            rec = recs.setdefault(tm, {}).setdefault(str(r["season"]), {"ats": [0, 0, 0], "ou": [0, 0, 0], "favATS": [0, 0], "dogATS": [0, 0]})
            c = cover * sign
            i = 0 if c > 0 else (1 if c < 0 else 2)
            rec["ats"][i] += 1
            fav = (r["spread"] * sign) < 0
            if i < 2:
                rec["favATS" if fav else "dogATS"][i] += 1
            if tot is not None:
                rec["ou"][0 if tot > 0 else (1 if tot < 0 else 2)] += 1   # [over, under, push]
    print(f"  -> ATS / O-U records for {len(recs)} teams")
    return recs


def _norm_inj_status(st):
    """Map free-text status to the codes the model's INJURY_MAP display uses (OUT / D / Q, else a short upper-case label)."""
    t = str(st or "").strip().lower()
    if not t:
        return ""
    if t.startswith("out") or "suspend" in t or "season" in t or t == "ir":
        return "OUT"
    if t.startswith("doubt"):
        return "D"
    if t.startswith("quest"):
        return "Q"
    return str(st).strip().upper()[:12]


_ESPN_INJ_URLS = [
    # Same family the NFL model uses (/apis/site/v2/...). The old /apis/v2/... path is kept as a last resort.
    "https://site.api.espn.com/apis/site/v2/sports/football/college-football/injuries",
    "https://site.web.api.espn.com/apis/site/v2/sports/football/college-football/injuries",   # reported to work when site.api is blocked (403/400)
    "https://site.api.espn.com/apis/v2/sports/football/college-football/injuries",
]


def _school_to_abbr(nm):
    """School / team text -> the model's abbreviation, or None."""
    if not nm:
        return None
    nm = str(nm).strip()
    if nm in TEAM_NAME_TO_ABBR:
        return TEAM_NAME_TO_ABBR[nm]
    if nm.upper() in set(TEAM_NAME_TO_ABBR.values()):
        return nm.upper()
    low = {k.lower(): v for k, v in TEAM_NAME_TO_ABBR.items()}
    return low.get(nm.lower())


def pull_injuries_espn():
    """Best-effort ESPN college football injuries (league-wide feed, same family the NFL model uses). Tries the
    /apis/site/v2 address first. ESPN may publish nothing for college, so an empty result is reported as such, not as an
    error. Output matches the model's INJURY_MAP:
    {"Player Name": {"team": ABBR, "pos": "QB", "status": "OUT|D|Q|...", "detail": "...", "source": "espn"}}."""
    print("Pulling injuries (ESPN, then BigBall, then manual_injuries.json)...")
    out = {}
    last_err = None
    for url in _ESPN_INJ_URLS:
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 EdgeReport"})
            r.raise_for_status()
            data = r.json()
            groups = data.get("injuries") or data.get("items") or []
            for blk in groups:
                t = blk.get("team") or {}
                ab = (_school_to_abbr(t.get("displayName")) or _school_to_abbr(t.get("location")) or _school_to_abbr(blk.get("displayName"))
                      or _school_to_abbr(blk.get("abbreviation")) or _school_to_abbr(t.get("abbreviation")))
                for it in blk.get("injuries") or blk.get("athletes") or []:
                    ath = it.get("athlete") or {}
                    nm = ath.get("displayName") or ath.get("fullName")
                    st = _norm_inj_status(it.get("status") or (it.get("type") or {}).get("description"))
                    team = ab or _school_to_abbr(((ath.get("team") or {}).get("displayName")))
                    if nm and st and team:
                        out[nm] = {"team": team, "pos": (ath.get("position") or {}).get("abbreviation"), "status": st,
                                   "detail": (it.get("details") or {}).get("type") or (it.get("type") or {}).get("description") or "", "source": "espn"}
            print(f"  ESPN {url.split('.com')[1]}: HTTP 200, {len(groups)} team blocks, {len(out)} players")
            if out:
                break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"  ! ESPN injuries failed at {url.split('.com')[1]} ({last_err[:110]})")
    if not out:
        print("  -> ESPN returned no college injuries (it may not publish them) — BigBall and manual_injuries.json are the other sources")
    print(f"  -> ESPN injuries: {len(out)} players")
    return out


def pull_injuries_bigballs(have):
    """BigBall unified injuries feed (/v1/injuries?sport=american_football), the same endpoint the NFL model uses. It may
    carry NFL only, or NFL + college: rows are kept only when they clearly belong to a college team in this model. The
    log shows how many rows came back, how many are college, and the field names of one sample row so the real shape is
    visible after the first run. Returns {name: entry}. ESPN/BigBall status disagreements are listed, never silently overwritten."""
    out = {}
    if not BIGBALLS_API_KEY:
        print("  -> BigBall injuries: skipped (no BIGBALLS_API_KEY)")
        return out
    data = bigballs_get("/v1/injuries", {"sport": "american_football"})
    if data is None:
        print("  -> BigBall injuries: no response (see any message above)")
        return out
    rows = None
    for c in (data if isinstance(data, list) else None, (data or {}).get("data") if isinstance(data, dict) else None,
              (data or {}).get("injuries") if isinstance(data, dict) else None, (data or {}).get("items") if isinstance(data, dict) else None,
              ((data or {}).get("data") or {}).get("injuries") if isinstance(data, dict) and isinstance((data or {}).get("data"), dict) else None):
        if isinstance(c, list):
            rows = c
            break
    if rows is None:
        print(f"  ! BigBall injuries: unexpected response shape (keys: {list(data.keys())[:8] if isinstance(data, dict) else type(data).__name__})")
        return out
    print(f"  BigBall injuries: {len(rows)} rows returned")
    if rows and isinstance(rows[0], dict):
        print(f"     (sample row keys: {sorted(rows[0].keys())[:14]})")
    college, other = 0, 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        pl = row.get("player") if isinstance(row.get("player"), dict) else {}
        nm = row.get("player_name") or row.get("name") or pl.get("name") or pl.get("displayName") or ""
        st = _norm_inj_status(row.get("status") or row.get("injury_status") or "")
        tm = row.get("team") if not isinstance(row.get("team"), dict) else (row["team"].get("name") or row["team"].get("abbreviation"))
        tm = tm or row.get("team_name") or row.get("team_abbr") or pl.get("team")
        if isinstance(tm, dict):
            tm = tm.get("name") or tm.get("abbreviation")
        lg = " ".join(str(row.get(k) or "") for k in ("league", "league_name", "competition", "sport_league", "division")).lower()
        ab = _school_to_abbr(tm)
        is_college = ab is not None and (not lg.strip() or any(x in lg for x in ("ncaa", "college", "cfb", "fbs")))
        if not is_college:
            other += 1
            continue
        college += 1
        if nm and st:
            out[nm] = {"team": ab, "pos": row.get("position") or row.get("pos") or pl.get("position"), "status": st,
                       "detail": row.get("detail") or row.get("injury_type") or row.get("description") or "", "source": "bigballs"}
    print(f"  -> BigBall injuries: {college} college rows kept, {other} other rows ignored (non-college or unmatched team)")
    if rows and not college:
        print("     BigBall's feed has no college rows for this account/endpoint — it is NFL-only or the team names do not match.")
    return out


# ── Conference availability reports (SEC, Big Ten, ACC, Big 12, Mountain West) ───────────────────────────────────
# Each conference now requires weekly player availability reports (conference games only, posted Wed-Fri before Saturday).
# The page layouts could not be verified from the development environment, so the reader is defensive: it understands
# (1) status headings with "Position Name" lines under them, (2) table rows, and (3) JSON embedded in the page. If a page
# yields no players, the raw page is saved to availability_raw/<conf>.html and the log says so, so the parser can be fixed
# from one real example. To add or fix a source without editing code, put lines "CONF=https://url" in availability_urls.txt.
AVAIL_SOURCES = {
    "SEC":       ["https://www.secsports.com/reports"],
    "BIG TEN":   ["https://bigten.org/fb/article/60284/"],
    "ACC":       ["https://theacc.com/sports/2025/8/28/availability-reporting-football.aspx"],
    "BIG 12":    ["https://big12sports.com/FBreporting.aspx", "https://big12sports.com/sports/2025/8/14/FBreporting.aspx"],
    "MOUNTAIN WEST": ["https://themw.com/availability-reports"],
}
_AVAIL_STATUS_WORDS = [  # checked in order; first hit wins
    ("out for the first half", "Q"), ("first half", "Q"), ("game time decision", "Q"), ("game-time decision", "Q"), ("gtd", "Q"),
    ("doubtful", "D"), ("questionable", "Q"), ("probable", "PROBABLE"), ("out", "OUT"), ("unavailable", "OUT"),
    ("suspended", "OUT"), ("available", "AVAILABLE"),
]
_AVAIL_POS = {"QB", "RB", "HB", "FB", "WR", "TE", "OL", "OT", "OG", "C", "DL", "DE", "DT", "NT", "LB", "ILB", "OLB", "MLB",
              "CB", "DB", "S", "FS", "SS", "K", "P", "PK", "LS", "KR", "PR", "ATH", "EDGE"}
_AVAIL_POS_WORDS = {"quarterback": "QB", "running back": "RB", "wide receiver": "WR", "tight end": "TE", "offensive lineman": "OL",
                    "offensive tackle": "OL", "offensive guard": "OL", "center": "C", "defensive lineman": "DL", "defensive end": "DL",
                    "defensive tackle": "DL", "linebacker": "LB", "cornerback": "CB", "safety": "S", "defensive back": "DB",
                    "place kicker": "K", "kicker": "K", "punter": "P", "long snapper": "LS", "receiver": "WR", "lineman": "OL"}


def _avail_status(text):
    t = " " + str(text or "").lower() + " "
    for w, code in _AVAIL_STATUS_WORDS:
        if (" " + w + " ") in t or t.strip().startswith(w):
            return code, ("out first half" if "first half" in w else "")
    return None, ""


def _avail_lines(html):
    """HTML -> list of text lines; table cells on one row are joined with ' | '."""
    import re as _re
    h = _re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    h = _re.sub(r"(?i)</t[dh]\s*>", " | ", h)
    h = _re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|section|article|ul|ol|table|header)>", "\n", h)
    h = _re.sub(r"(?s)<[^>]+>", " ", h)
    import html as _html
    h = _html.unescape(h)
    out = []
    for ln in h.split("\n"):
        ln = _re.sub(r"\s+", " ", ln).strip(" |\t")
        if ln:
            out.append(ln)
    return out


def _avail_team_in(line, school_lc):
    """Abbreviation when the line is (or starts with / ends with) one school name; None otherwise."""
    t = line.lower().strip(" :-–—|")
    for suffix in (" football availability report", " availability report", " football", " report", " availability", " players", ":"):
        if t.endswith(suffix):
            t = t[: -len(suffix)].strip(" :-–—|")
    return school_lc.get(t)


def _avail_player_line(line):
    """'WR Jane Doe' / 'Wide Receiver Jane Doe' / 'Jane Doe, WR' / 'Jane Doe (WR)' -> (name, pos) or None."""
    import re as _re
    s = line.strip(" -•*·•")
    m = _re.match(r"^([A-Za-z]{1,5})[\s.:-]+([A-Z][A-Za-z.'’\-]+(?:\s+[A-Z][A-Za-z.'’\-]+){1,3}(?:\s+(?:Jr|Sr|II|III|IV)\.?)?)$", s)
    if m and m.group(1).upper() in _AVAIL_POS:
        return m.group(2).strip(), m.group(1).upper()
    low = s.lower()
    for w, p in sorted(_AVAIL_POS_WORDS.items(), key=lambda kv: -len(kv[0])):
        if low.startswith(w + " "):
            nm = s[len(w) + 1:].strip()
            if _re.match(r"^[A-Z][A-Za-z.'’\-]+(?:\s+[A-Z][A-Za-z.'’\-]+){1,3}(?:\s+(?:Jr|Sr|II|III|IV)\.?)?$", nm):
                return nm, p
    m = _re.match(r"^([A-Z][A-Za-z.'’\-]+(?:\s+[A-Z][A-Za-z.'’\-]+){1,3})\s*[,(\-–]\s*([A-Za-z]{1,5})\)?$", s)
    if m and m.group(2).upper() in _AVAIL_POS:
        return m.group(1).strip(), m.group(2).upper()
    return None


def _avail_parse_json(html, school_lc):
    """JSON blobs inside <script> tags: any list of dicts with a name-like and a status-like field."""
    import re as _re
    found = []
    for blob in _re.findall(r"(?is)<script[^>]*>(.*?)</script>", html):
        blob = blob.strip()
        if not blob or blob[0] not in "[{":
            m = _re.search(r"=\s*(\{.*\}|\[.*\])\s*;?\s*$", blob, _re.S)
            if not m:
                continue
            blob = m.group(1)
        try:
            data = json.loads(blob)
        except Exception:
            continue
        stack = [(data, None)]
        while stack:
            x, ctx = stack.pop()
            if isinstance(x, list):
                for it in x:
                    if isinstance(it, dict):
                        nm = it.get("name") or it.get("player") or it.get("playerName") or it.get("player_name")
                        st = it.get("status") or it.get("designation") or it.get("availability")
                        tm = it.get("team") or it.get("school") or it.get("teamName")
                        if isinstance(nm, dict):
                            nm = nm.get("name") or nm.get("fullName")
                        if isinstance(tm, dict):
                            tm = tm.get("name") or tm.get("title")
                        if nm and st:
                            found.append((_school_to_abbr(tm) or school_lc.get(str(tm or "").lower()) or ctx, str(nm), it.get("position") or it.get("pos"), str(st)))
                    stack.append((it, ctx))
            elif isinstance(x, dict):
                tm = x.get("team") or x.get("school") or x.get("teamName")
                if isinstance(tm, dict):
                    tm = tm.get("name") or tm.get("title")
                c2 = (_school_to_abbr(tm) or school_lc.get(str(tm or "").lower())) if isinstance(tm, str) else None
                for v in x.values():
                    stack.append((v, c2 or ctx))
    return found


def parse_availability_html(html):
    """Returns [(team_abbr_or_None, name, pos_or_None, status_text)]."""
    school_lc = {k.lower(): v for k, v in TEAM_NAME_TO_ABBR.items()}
    for ab in set(TEAM_NAME_TO_ABBR.values()):
        school_lc.setdefault(ab.lower(), ab)
    rows = _avail_parse_json(html, school_lc)
    if rows:
        return rows
    team, status, rows = None, None, []
    for ln in _avail_lines(html):
        cells = [c.strip() for c in ln.split(" | ")] if " | " in ln else [ln]
        if len(cells) >= 2:                      # table row: find team, status, position and name among the cells
            import re as _re
            t = next((school_lc.get(c.lower()) for c in cells if school_lc.get(c.lower())), None)
            st = next((c for c in cells if _avail_status(c)[0]), None)
            pl = next((p for c in cells for p in [_avail_player_line(c)] if p), None)
            if pl is None:
                pos_c = next((c.upper() for c in cells if c.upper() in _AVAIL_POS), None)
                nm_c = next((c for c in cells if _re.match(r"^[A-Z][A-Za-z.'’\-]+(?:\s+[A-Z][A-Za-z.'’\-]+){1,3}(?:\s+(?:Jr|Sr|II|III|IV)\.?)?$", c)
                             and not school_lc.get(c.lower()) and not _avail_status(c)[0]), None)
                if nm_c:
                    pl = (nm_c, pos_c)
            if pl and (st or status):
                rows.append((t or team, pl[0], pl[1], st or status))
                continue
            if t:
                team = t
            continue
        t = _avail_team_in(ln, school_lc)
        if t:
            team, status = t, None
            continue
        pl = _avail_player_line(ln)
        if pl and status:
            rows.append((team, pl[0], pl[1], status))
            continue
        s_code, _ = _avail_status(ln)
        if s_code and len(ln) < 60:              # a status heading such as "OUT", "Questionable (2)"
            status = ln
            # "Name, POS - Out" on one line
            inline = ln.split(" - ")[0].split(" – ")[0]
            pl2 = _avail_player_line(inline) if inline != ln else None
            if pl2:
                rows.append((team, pl2[0], pl2[1], ln))
    return rows


def pull_conference_availability():
    """Fetches each conference's availability page and returns {name: INJURY_MAP entry}. Never raises."""
    print("Pulling conference availability reports (SEC, Big Ten, ACC, Big 12, Mountain West)...")
    srcs = {k: list(v) for k, v in AVAIL_SOURCES.items()}
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "availability_urls.txt")
        if os.path.isfile(p):
            with open(p, encoding="utf-8-sig") as fh:
                for ln in fh:
                    if "=" in ln and not ln.strip().startswith("#"):
                        k, v = ln.split("=", 1)
                        if v.strip():
                            srcs[k.strip().upper()] = [v.strip()]
    except Exception as e:
        print(f"  ! availability_urls.txt unreadable: {e}")
    out, total = {}, 0
    for conf, urls in srcs.items():
        got, note = [], ""
        for url in urls:
            try:
                r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) EdgeReport"})
                if r.status_code >= 400:
                    note = f"HTTP {r.status_code} at {url}"
                    continue
                rows = parse_availability_html(r.text)
                note = f"HTTP {r.status_code}, {len(r.text)//1024} KB, {len(rows)} player rows at {url}"
                if rows:
                    got = rows
                    break
                try:
                    os.makedirs("availability_raw", exist_ok=True)
                    with open(os.path.join("availability_raw", conf.replace(" ", "_") + ".html"), "w", encoding="utf-8") as fh:
                        fh.write(r.text)
                    note += " (no players read — raw page saved to availability_raw/)"
                except OSError:
                    pass
            except Exception as e:
                note = f"{type(e).__name__} at {url}"
        n_ok = 0
        for tm, nm, pos, st in got:
            code, det = _avail_status(st)
            if not nm or not code or code == "AVAILABLE":
                continue
            if tm is None:
                continue
            out[nm] = {"team": tm, "pos": pos, "status": code, "detail": det or f"{conf} availability report", "source": "conference:" + conf.lower()}
            n_ok += 1
        total += n_ok
        print(f"  {conf:14s} {note} -> {n_ok} usable (non-available, team identified)")
    print(f"  -> conference availability reports: {total} players")
    if not total:
        print("     None read. Reports appear Wed-Fri for Saturday conference games and cover conference games only. If a page loaded")
        print("     but showed no players, send me availability_raw/*.html (or paste the page text) and I will fix the reader.")
    return out


def pull_injuries_all():
    conf = pull_conference_availability()      # official conference reports (names + status), conference games only
    esp = pull_injuries_espn()
    bb = pull_injuries_bigballs(esp)
    merged = dict(conf)                        # priority: manual (applied later) > conference reports > ESPN > BigBall
    flagged = []
    for src in (esp, bb):
        for nm, e in src.items():
            if nm in merged:
                if merged[nm]["status"] != e["status"]:
                    flagged.append((nm, merged[nm]["source"], merged[nm]["status"], e["source"], e["status"]))
            else:
                merged[nm] = e
    if flagged:
        print(f"  ! source disagreements ({len(flagged)}); higher-priority source kept: " + "; ".join(f"{n} {a} {b} / {c} {d}" for n, a, b, c, d in flagged[:8]))
    print(f"  -> injuries before manual entries: {len(merged)} players (conference {len(conf)}, ESPN {len(esp)}, BigBall {len(bb)})")
    return merged


def merge_manual_injuries(inj):
    """manual_injuries.json next to the script: {"ALA": [{"name": "Jalen Milroe", "pos": "QB", "status": "Out", "detail": "ankle"}], ...}
    (team abbreviation or school name as the key). Manual entries replace any feed entry for the same player."""
    path = os.environ.get("CFB_MANUAL_INJURIES", "manual_injuries.json")
    if not os.path.exists(path):
        return inj
    try:
        with open(path, encoding="utf-8") as f:
            man = json.load(f)
    except Exception as e:
        print(f"  ! could not read {path}: {e}")
        return inj
    n = 0
    for tm, lst in (man or {}).items():
        ab = TEAM_NAME_TO_ABBR.get(tm, tm)
        for it in lst or []:
            if it.get("name"):
                inj[it["name"]] = {"team": ab, "pos": it.get("pos"), "status": _norm_inj_status(it.get("status", "Out")) or "OUT",
                                   "detail": it.get("detail", ""), "source": "manual"}
                n += 1
    print(f"  -> merged {n} manual injury entries from {path}")
    return inj

def _load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(c):
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(c, f)
    except Exception as e:
        print(f"  ! could not write cache {CACHE_PATH}: {e}")


def _stat_pairs(cat):
    """{typeName: [(athleteName, value)...]} for one CFBD /games/players category block."""
    out = {}
    for t in (cat or {}).get("types") or []:
        out[t.get("name")] = [(a.get("name"), a.get("stat")) for a in (t.get("athletes") or [])]
    return out


def _to_float(s):
    try:
        return float(str(s).split("/")[0])
    except (TypeError, ValueError):
        return 0.0


def summarize_week_games_players(rows):
    """/games/players payload for one week -> compact per-game per-team receiver / rusher / passer lines."""
    games = []
    for g in rows or []:
        teams = g.get("teams") or []
        if len(teams) != 2:
            continue
        entry = {"id": g.get("id"), "sides": []}
        for t in teams:
            abbr = _abbr(t.get("team"))
            cats = {c.get("name"): _stat_pairs(c) for c in (t.get("categories") or [])}
            rec = [{"name": n, "yds": _to_float(v)} for n, v in (cats.get("receiving", {}).get("YDS") or [])]
            rush = [{"name": n, "yds": _to_float(v)} for n, v in (cats.get("rushing", {}).get("YDS") or [])]
            rec_n = {n: _to_float(v) for n, v in (cats.get("receiving", {}).get("REC") or [])}
            car_n = {n: _to_float(v) for n, v in (cats.get("rushing", {}).get("CAR") or [])}
            pas = cats.get("passing", {}).get("YDS") or []
            entry["sides"].append({"team": abbr, "team_name": t.get("team"), "rec": rec, "rush": rush,
                                   "recCount": rec_n, "carCount": car_n, "passYds": sum(_to_float(v) for _, v in pas)})
        games.append(entry)
    return games


def pull_def_vs_position(weeks, pos_lookup):
    """Yards each DEFENSE allowed to the opposing WR1 / WR2 / TE / RB (receiving + rushing) and QB passing, per game.
    One /games/players call per completed week; each week is cached so it is pulled once. pos_lookup is
    {(ABBR, 'Player Name'): 'WR'}. Ranks run 1 = toughest defense versus that position."""
    print("Building defense-vs-position splits from per-game box scores...")
    for wk in weeks or []:
        fetch_week_games_players(wk)
    wk_cache = _load_cache().get(f"games_players_{SEASON}", {})
    wk_cache = {k: v for k, v in wk_cache.items() if not weeks or int(k) in set(int(w) for w in weeks)}

    acc = {}   # defense abbr -> accumulators
    for wk, games in wk_cache.items():
        for g in games:
            if len(g["sides"]) != 2:
                continue
            for i in (0, 1):
                off, dfn = g["sides"][i], g["sides"][1 - i]
                d_ab, o_ab = dfn["team"], off["team"]
                if not d_ab or not o_ab:
                    continue
                a = acc.setdefault(d_ab, {"games": 0, "wr1": 0.0, "wr2": 0.0, "te": 0.0, "rbRec": 0.0, "rbRush": 0.0, "qbPass": 0.0})
                a["games"] += 1
                a["qbPass"] += off["passYds"]
                wrs, others = [], {"TE": 0.0, "RB": 0.0}
                for r in off["rec"]:
                    pos = pos_lookup.get((o_ab, r["name"]))
                    if pos == "WR":
                        wrs.append(r["yds"])
                    elif pos in others:
                        others[pos] += r["yds"]
                wrs.sort(reverse=True)
                a["wr1"] += wrs[0] if len(wrs) > 0 else 0.0
                a["wr2"] += wrs[1] if len(wrs) > 1 else 0.0
                a["te"] += others["TE"]
                a["rbRec"] += others["RB"]
                a["rbRush"] += sum(r["yds"] for r in off["rush"] if pos_lookup.get((o_ab, r["name"])) == "RB")
    out = {}
    for ab, a in acc.items():
        n = a["games"]
        if not n:
            continue
        out[ab] = {"games": n, "wr1YdsPG": round(a["wr1"] / n, 1), "wr2YdsPG": round(a["wr2"] / n, 1), "teYdsPG": round(a["te"] / n, 1),
                   "rbRecYdsPG": round(a["rbRec"] / n, 1), "rbRushYdsPG": round(a["rbRush"] / n, 1), "qbPassYdsPG": round(a["qbPass"] / n, 1)}
    # ranks: 1 = allows the fewest yards (toughest). Only teams with >= 3 games are ranked (small samples are noise).
    ranked = [ab for ab in out if out[ab]["games"] >= 3]
    for field, rank_key in [("wr1YdsPG", "wr1Rank"), ("wr2YdsPG", "wr2Rank"), ("teYdsPG", "teRank"),
                            ("rbRecYdsPG", "rbRecRank"), ("rbRushYdsPG", "rbRushRank"), ("qbPassYdsPG", "qbRank")]:
        for i, ab in enumerate(sorted(ranked, key=lambda x: out[x][field]), 1):
            out[ab][rank_key] = i
    print(f"  -> defense-vs-position for {len(out)} teams ({len(ranked)} with 3+ games)")
    return out


def main():
    print(f"═══ CFB Edge Data Pull — Season {SEASON} ═══")
    _print_key_report()
    if not CFBD_API_KEY:
        print("NOTE: running without CFBD_API_KEY — team stats will be empty and the")
        print("      model will fall back to the static seed values in the HTML.")

    teams   = pull_team_stats()
    games   = pull_games_espn()
    odds    = pull_odds_lines()
    player_props = pull_player_props()  # real Odds API player props for the Win Margin/Anytime TD tab
    injuries = merge_manual_injuries(pull_injuries_all())   # CFBD has no injuries endpoint; see pull_injuries()
    rosters = pull_current_rosters()   # first: fills ROSTER_POS so player stats can tell WR from TE
    players = pull_player_stats()
    coaches = pull_coaches()
    historical_games = pull_historical_games([2024, 2025, 2026])
    transfer_portal = pull_transfer_portal([SEASON, SEASON - 1])  # this year + last year's cycle,
                                                                    # since portal moves for a season
                                                                    # can be logged under either year
    # Only the most recent 2 weeks by default — re-pulling box scores for
    # the entire season-to-date on every run isn't sustainable against the
    # free tier's 1,000 calls/month (each completed game is its own call).
    # Weekly grading only needs recent results anyway; 2 weeks (not 1)
    # gives a one-week buffer in case a run gets skipped.
    # Completed weeks come from the played 2026 games (historical_games has only games with final scores).
    # The ESPN scoreboard list holds upcoming games only, so it can't be used here.
    _completed_weeks = sorted(set(
        int(g["wk"]) for g in (historical_games.get(str(SEASON)) or historical_games.get(SEASON) or [])
        if g.get("wk") is not None and str(g.get("wk")).lstrip("-").isdigit() and 1 <= int(g["wk"]) <= 15
        and g.get("homeScore") is not None
    ))
    print(f"Completed {SEASON} regular-season weeks: {_completed_weeks}")
    _recent_weeks = _completed_weeks[-2:] if _completed_weeks else None
    player_game_stats = pull_player_game_stats(_recent_weeks)  # real per-game stats for the weekly props-grading pipeline
    player_games = pull_player_game_counts(_completed_weeks)   # per-game carries/receptions (recent form + with/without splits)

    # Phase 1: bigballsdata as an independent second CFB data source —
    # cross-validated against ESPN, not yet trusted as a primary source.
    bigballs_matches = pull_bigballs_matches()
    bigballs_standings = pull_bigballs_standings()
    bigballs_discrepancies = cross_validate_with_bigballs(games, bigballs_matches)

    # Diagnostic: check real account access before attempting Team Form/H2H.
    # A real run confirmed the definitive answer: this account's free tier
    # has team_form=False and h2h_intelligence=False (despite marketing docs
    # describing them as free-tier) but player_stats=True — worth revisiting
    # for the disabled props-grading pipeline separately. Gate the actual
    # pull attempts on this real response instead of discovering the same
    # 403s reactively via the circuit breaker every run.
    bigballs_coverage = pull_bigballs_coverage()
    bigballs_features = (bigballs_coverage or {}).get("features", {})

    # Team Form + H2H Intelligence — real bigballsdata signals, bounded to
    # resolved teams (form) and actual scheduled games (h2h), not every
    # possible team combination. Left in place (rather than removed) since
    # the coverage gate below already prevents any wasted calls if this
    # account's tier doesn't have access — if that ever changes, this
    # starts working again with no code changes needed. CFBD's real
    # /teams/matchup (pulled separately below) is the primary H2H source
    # now, since this account's tier doesn't have h2h_intelligence.
    bigballs_team_ids = build_bigballs_team_id_map(bigballs_matches)
    if bigballs_features.get("team_form", True):
        bigballs_team_form = pull_bigballs_team_form(bigballs_team_ids)
    else:
        print("Skipping bigballsdata team form — confirmed unavailable on this account's tier (/v1/coverage).")
        bigballs_team_form = {}
    if bigballs_features.get("h2h_intelligence", True):
        bigballs_h2h = pull_bigballs_h2h_for_games(games, bigballs_team_ids)
    else:
        print("Skipping bigballsdata H2H intelligence — confirmed unavailable on this account's tier (/v1/coverage).")
        bigballs_h2h = {}

    # Real H2H via CFBD's own /teams/matchup — the actual primary source
    # now, using the existing CFBD key rather than a bigballsdata feature
    # this account doesn't have access to.
    cfbd_h2h = pull_cfbd_h2h_for_games(games)

    # Bounded, diagnostic-only test of bigballsdata's real per-match
    # statistics endpoint as a potential path to revive per-game player
    # stats (disabled earlier after CFBD guesses burned real quota). Never
    # more than a handful of calls — this is verification, not production.
    if bigballs_features.get("player_stats", True):
        test_bigballs_match_statistics(bigballs_matches)
    else:
        print("Skipping bigballsdata match-statistics test — player_stats not available on this account's tier.")

    # Real recruiting rankings + talent composite — a depth signal layered
    # on top of returning-production/coaching-regime, not a replacement.
    recruiting_and_talent = pull_recruiting_and_talent()

    # ── v2.0 additions: defense + offense + weather venues ─────────────────────────────
    teams_advanced = pull_team_advanced()
    team_season_stats = pull_team_season_stats()
    ppa_players = pull_ppa_players()
    player_usage = pull_player_usage()
    player_usage_prev = pull_player_usage(SEASON - 1)   # prior-season role for portal transfers
    returning = pull_returning_production()
    team_meta, home_venues, venues_by_name = pull_team_meta_and_venues()
    line_history = pull_line_history()
    schedule_games, team_game_ledger = pull_schedule_block()
    current_lines = pull_current_lines()
    pace = build_pace(team_season_stats, line_history)
    ats_records = build_ats_records(line_history)
    pos_lookup = dict(ROSTER_POS)
    for nm, pl in (players or {}).items():                       # graded players carry a position too
        if pl.get("team") and pl.get("pos"):
            pos_lookup.setdefault((pl["team"], nm), pl["pos"])
    def_vs_pos = pull_def_vs_position(_completed_weeks, pos_lookup)

    # Merge odds lines onto games where we have a matchup match
    for g in games:
        key_variants = [f"{g['awayTeam']}_{g['homeTeam']}"]
        for kv in key_variants:
            if kv in odds:
                ev = odds[kv]
                bm = (ev.get("bookmakers") or [{}])[0]
                markets = {m["key"]: m for m in bm.get("markets", [])}
                if "spreads" in markets:
                    outcomes = markets["spreads"].get("outcomes", [])
                    home_outcome = next((o for o in outcomes if o.get("name") == ev.get("home_team")), {})
                    g["spread"] = home_outcome.get("point", g.get("spread"))
                if "totals" in markets:
                    outcomes = markets["totals"].get("outcomes", [])
                    if outcomes:
                        g["total"] = outcomes[0].get("point", g.get("total"))

    output = {
        "season": SEASON,
        "source": "CFBD",
        "generated": datetime.now(timezone.utc).isoformat(),
        "scheduleGames": schedule_games,
        "teamGameLedger": {"games": team_game_ledger},
        "meta": {
            "teamCount":   len(teams),
            "playerCount": len(players),
            "rookieCount": 0,   # populated via the Transfer Portal / True Freshman import in-app
        },
        "teams": teams,
        "games": games,
        "players": players,
        "injuries": injuries,
        "rosters": rosters,
        "transferPortal": transfer_portal,
        "playerGameStats": player_game_stats,
        "playerGames": player_games,
        "bigballsStandings": bigballs_standings,
        "bigballsDiscrepancies": bigballs_discrepancies,
        "bigballsTeamForm": bigballs_team_form,
        "bigballsH2H": bigballs_h2h,
        "cfbdH2H": cfbd_h2h,
        "playerProps": player_props,
        "recruitingAndTalent": recruiting_and_talent,
        "coaches": coaches,
        "historicalGames": historical_games,
        # v2.0 additions
        "teamsAdvanced": teams_advanced,
        "teamSeasonStats": team_season_stats,
        "ppaPlayers": ppa_players,
        "playerUsage": player_usage,
        "playerUsagePrev": player_usage_prev,
        "returningProduction": returning,
        "teamMeta": team_meta,
        "venues": {"byTeam": home_venues, "byName": venues_by_name},
        "lineHistory": line_history,
        "currentLines": current_lines,
        "pace": pace,
        "atsRecords": ats_records,
        "defVsPosition": def_vs_pos,
        # Explicit success/failure per data source, rather than making the
        # HTML guess from array lengths (which can legitimately be empty
        # early in a week even when the source itself worked fine).
        "dataSourceStatus": {
            "cfbd": {
                "keyProvided": bool(CFBD_API_KEY),
                "authFailed": _cfbd_auth_failed,
                "succeeded": _cfbd_call_succeeded,
            },
            "bigballsdata": {
                "keyProvided": bool(BIGBALLS_API_KEY),
                "authFailed": _bigballs_auth_failed,
                "succeeded": _bigballs_call_succeeded,
            },
            "oddsApi": {
                "keyProvided": bool(ODDS_API_KEY),
            },
        },
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    hist_total = sum(len(v) for v in historical_games.values())
    roster_total = sum(len(v) for v in rosters.values())
    print(f"\n✅ Wrote {OUTPUT_PATH}  ({len(teams)} teams, {len(games)} games, "
          f"{roster_total} roster players across {len(rosters)} teams, {len(players)} graded players, "
          f"{len(coaches)} coaches, {hist_total} historical games, {len(transfer_portal)} portal entries, "
          f"{len(bigballs_matches)} bigballsdata matches cross-checked, {len(bigballs_discrepancies)} discrepancies flagged)")
    print("   Load it in NCAA_Edge_Model_v2_0.html -> Data Connections tab -> Load cfb_model_data.json")


if __name__ == "__main__":
    main()
