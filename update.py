"""Leg Room data updater. Runs on GitHub Actions and writes JSON files into data/.

python update.py full   -> everything, including team ratings from Basketball Reference
python update.py quick  -> results, slate, odds, injuries and player averages (skips team ratings)
"""
import json, os, re, sys, time, unicodedata, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo

ADL = ZoneInfo('Australia/Adelaide')
ET = ZoneInfo('America/New_York')
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
SEASON_START = date(2026, 10, 20)      # first day of the 2026-27 regular season (US date)
SEASON_YEAR = 2027                     # Basketball Reference / ESPN label the season by its end year
SITE_ORIGIN = 'https://legroomdev.github.io'
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
      'Accept': 'application/json, text/html;q=0.9, */*;q=0.8', 'Accept-Language': 'en-US,en;q=0.9'}
SB = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard'
SUMMARY = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event='
INJ = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries'
BYATH = ('https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/statistics/byathlete'
         '?region=us&lang=en&contentorigin=espn&isqualified=false&limit=1000&page=1&sort=general.avgMinutes:desc&seasontype=2')

FIX = {'GS': 'GSW', 'NY': 'NYK', 'SA': 'SAS', 'NO': 'NOP', 'UTAH': 'UTA', 'WSH': 'WAS', 'PHO': 'PHX', 'BRK': 'BKN', 'CHO': 'CHA', 'NJ': 'BKN'}
TEAMS = {'ATL': 'Atlanta Hawks', 'BOS': 'Boston Celtics', 'BKN': 'Brooklyn Nets', 'CHA': 'Charlotte Hornets', 'CHI': 'Chicago Bulls',
         'CLE': 'Cleveland Cavaliers', 'DAL': 'Dallas Mavericks', 'DEN': 'Denver Nuggets', 'DET': 'Detroit Pistons', 'GSW': 'Golden State Warriors',
         'HOU': 'Houston Rockets', 'IND': 'Indiana Pacers', 'LAC': 'Los Angeles Clippers', 'LAL': 'Los Angeles Lakers', 'MEM': 'Memphis Grizzlies',
         'MIA': 'Miami Heat', 'MIL': 'Milwaukee Bucks', 'MIN': 'Minnesota Timberwolves', 'NOP': 'New Orleans Pelicans', 'NYK': 'New York Knicks',
         'OKC': 'Oklahoma City Thunder', 'ORL': 'Orlando Magic', 'PHI': 'Philadelphia 76ers', 'PHX': 'Phoenix Suns', 'POR': 'Portland Trail Blazers',
         'SAC': 'Sacramento Kings', 'SAS': 'San Antonio Spurs', 'TOR': 'Toronto Raptors', 'UTA': 'Utah Jazz', 'WAS': 'Washington Wizards'}
BYNAME = {v.lower(): k for k, v in TEAMS.items()}
BYNAME['la clippers'] = 'LAC'

STATUS = {'steps': {}}


def log(step, msg):
    print(f'[{step}] {msg}', flush=True)
    STATUS['steps'][step] = msg


def std(a):
    a = (a or '').upper().strip()
    return FIX.get(a, a)


def team_from_name(n):
    n = (n or '').lower().strip()
    if n in BYNAME:
        return BYNAME[n]
    for full, ab in BYNAME.items():
        if n and (n in full or full.split()[-1] == n.split()[-1]):
            return ab
    return None


def nm(s):
    s = unicodedata.normalize('NFD', str(s or ''))
    s = ''.join(c for c in s if not unicodedata.combining(c)).replace('ł', 'l').replace('đ', 'd').lower()
    return re.sub(r'[^a-z]', '', s)


def fetch(url, as_json=True, tries=3, headers=None):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=dict(UA, **(headers or {})))
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode('utf-8', 'replace')
                return json.loads(body) if as_json else body
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f'{url[:90]}: {last}')


def parse_dt(s):
    s = (s or '').replace('Z', '+00:00')
    if re.match(r'.*T\d\d:\d\d\+', s):
        s = s.replace('+', ':00+', 1) if s.count(':') == 2 else s
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return datetime.strptime(s[:16], '%Y-%m-%dT%H:%M').replace(tzinfo=timezone.utc)


def rd(x, n=1):
    try:
        return round(float(x), n)
    except Exception:  # noqa: BLE001
        return 0


def load(name, default):
    p = os.path.join(DATA, name)
    if os.path.exists(p):
        try:
            with open(p, encoding='utf-8') as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            pass
    return default


def save(name, obj):
    os.makedirs(DATA, exist_ok=True)
    tmp = os.path.join(DATA, name + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, os.path.join(DATA, name))


# ---------- scoreboard ----------
SB_CACHE = {}


def scoreboard(d):
    key = d.strftime('%Y%m%d')
    if key not in SB_CACHE:
        js = fetch(f'{SB}?dates={key}&limit=100')
        out = []
        for ev in js.get('events', []):
            try:
                comp = ev['competitions'][0]
                st = (comp.get('status') or ev.get('status') or {}).get('type', {})
                cs = {c['homeAway']: c for c in comp['competitors']}
                odds = (comp.get('odds') or [{}])[0] or {}
                out.append({'id': str(ev['id']), 'start': parse_dt(comp.get('date') or ev.get('date')),
                            'state': st.get('state'), 'completed': bool(st.get('completed')), 'stname': st.get('name'),
                            'home': std(cs['home']['team']['abbreviation']), 'away': std(cs['away']['team']['abbreviation']),
                            'odds': odds})
            except Exception as e:  # noqa: BLE001
                print('skip event', ev.get('id'), e)
        SB_CACHE[key] = out
    return SB_CACHE[key]


# ---------- box scores ----------
def box(eid):
    s = fetch(SUMMARY + eid)
    comp = s['header']['competitions'][0]
    start = parse_dt(comp['date'])
    side = {}
    for c in comp['competitors']:
        ls = []
        for x in c.get('linescores') or []:
            v = x.get('displayValue', x.get('value'))
            try:
                ls.append(int(float(v)))
            except Exception:  # noqa: BLE001
                ls.append(0)
        side[c['homeAway']] = {'abbr': std(c['team']['abbreviation']), 'score': int(float(c.get('score') or 0)), 'ls': ls}
    periods = {str(i + 1): {'away': a, 'home': h} for i, (a, h) in enumerate(zip(side['away']['ls'], side['home']['ls']))}
    which = {side['away']['abbr']: 'away', side['home']['abbr']: 'home'}
    players = {'away': [], 'home': []}
    for tp in (s.get('boxscore') or {}).get('players') or []:
        sd = which.get(std(tp['team']['abbreviation']))
        if not sd or not tp.get('statistics'):
            continue
        st = tp['statistics'][0]
        names = [str(n).upper() for n in (st.get('names') or st.get('labels') or [])]
        idx = {n: i for i, n in enumerate(names)}
        for a in st.get('athletes') or []:
            v = a.get('stats') or []
            if a.get('didNotPlay') or not v:
                continue

            def num(k):
                i = idx.get(k)
                if i is None or i >= len(v):
                    return 0
                x = str(v[i]).split('-')[0].split(':')[0]
                try:
                    return int(float(x))
                except Exception:  # noqa: BLE001
                    return 0
            row = {'name': a['athlete']['displayName'], 'min': num('MIN'), 'st': 1 if a.get('starter') else 0,
                   'pts': num('PTS'), 'reb': num('REB'), 'ast': num('AST'), 'thr': num('3PT'), 'stl': num('STL'), 'blk': num('BLK')}
            if row['min'] == 0 and row['pts'] == 0 and row['reb'] == 0 and row['ast'] == 0:
                continue
            players[sd].append(row)
    d = start.astimezone(ADL).date().isoformat()
    return d, {'id': eid, 'away': side['away']['abbr'], 'home': side['home']['abbr'], 'date': d,
               'score': {'away': side['away']['score'], 'home': side['home']['score']}, 'periods': periods, 'players': players}


def update_results(results, now):
    have = {g.get('id') for r in results.values() for g in r.get('games', []) if g.get('id')}
    have_pair = {(r_date, g['away'], g['home']) for r_date, r in results.items() for g in r.get('games', [])}
    today_et = now.astimezone(ET).date()
    full = not any(g.get('id') for r in results.values() for g in r.get('games', []))
    start = SEASON_START if full else max(SEASON_START, today_et - timedelta(days=4))
    added = fails = 0
    d = start
    while d <= today_et:
        try:
            evs = scoreboard(d)
        except Exception as e:  # noqa: BLE001
            print('scoreboard failed', d, e)
            fails += 1
            d += timedelta(days=1)
            continue
        for ev in evs:
            if not (ev['completed'] or ev['state'] == 'post') or ev['id'] in have:
                continue
            adl = ev['start'].astimezone(ADL).date().isoformat()
            if not full and (adl, ev['away'], ev['home']) in have_pair and adl < (today_et - timedelta(days=1)).isoformat():
                continue
            try:
                dd, g = box(ev['id'])
            except Exception as e:  # noqa: BLE001
                print('box failed', ev['id'], e)
                fails += 1
                continue
            if not g['players']['away'] and not g['players']['home']:
                continue
            r = results.setdefault(dd, {'date': dd, 'games': []})
            r['games'] = [x for x in r['games'] if not (x['away'] == g['away'] and x['home'] == g['home'])] + [g]
            have.add(ev['id'])
            added += 1
            time.sleep(0.25)
        d += timedelta(days=1)
    total = sum(len(r.get('games', [])) for r in results.values())
    log('results', f'ok: {added} new games, {total} stored' + (f', {fails} fetches failed' if fails else ''))
    return results


# ---------- player averages ----------
def season_logs(results):
    """name key -> {gp, gs, team (latest), sums...}"""
    agg = {}
    for d in sorted(results):
        for g in results[d].get('games', []):
            for sd in ('away', 'home'):
                for p in (g.get('players') or {}).get(sd, []):
                    k = nm(p['name'])
                    a = agg.setdefault(k, {'name': p['name'], 'gp': 0, 'gs': 0, 'min': 0, 'pts': 0, 'reb': 0, 'ast': 0, 'stl': 0, 'blk': 0, 'thr': 0})
                    a['gp'] += 1
                    a['gs'] += 1 if p.get('st') else 0
                    for s in ('min', 'pts', 'reb', 'ast', 'stl', 'blk', 'thr'):
                        a[s] += p.get(s) or 0
                    a['team'] = g[sd]
    return agg


def player_table(results):
    logs = season_logs(results)
    rows = {}
    source = 'box scores'
    try:
        js = fetch(BYATH)
        cats = js.get('categories') or []
        athletes = js.get('athletes') or []
        for a in athletes:
            ath = a.get('athlete') or {}
            name = ath.get('displayName') or ath.get('fullName')
            if not name:
                continue
            vals = {}
            for i, c in enumerate(a.get('categories') or []):
                top = next((t for t in cats if t.get('name') == c.get('name')), cats[i] if i < len(cats) else {})
                for n, v in zip(top.get('names') or [], c.get('values') or []):
                    vals.setdefault(n, v)
            team = std(ath.get('teamShortName') or (ath.get('team') or {}).get('abbreviation') or a.get('teamShortName') or '')
            lg = logs.get(nm(name), {})
            if team not in TEAMS:
                team = lg.get('team')
            if team not in TEAMS:
                continue
            gp = int(vals.get('gamesPlayed') or 0)
            rows[nm(name) + '|' + team] = {'team': team, 'name': name, 'gp': gp, 'gs': lg.get('gs') if lg else None,
                              'min': rd(vals.get('avgMinutes')), 'pts': rd(vals.get('avgPoints')), 'reb': rd(vals.get('avgRebounds')),
                              'ast': rd(vals.get('avgAssists')), 'stl': rd(vals.get('avgSteals')), 'blk': rd(vals.get('avgBlocks')),
                              'thr': rd(vals.get('avgThreePointFieldGoalsMade'))}
        if len(rows) >= 200:
            source = 'ESPN season averages'
        else:
            print('byathlete returned only', len(rows), 'players; falling back to box scores')
            rows = {}
    except Exception as e:  # noqa: BLE001
        print('byathlete failed', e)
    if not rows:
        for k, a in logs.items():
            n = a['gp'] or 1
            rows[k] = {'team': a['team'], 'name': a['name'], 'gp': a['gp'], 'gs': a['gs'],
                       **{s: rd(a[s] / n) for s in ('min', 'pts', 'reb', 'ast', 'stl', 'blk', 'thr')}}
    # if box scores show a later team (trade), trust them
    for k, r in rows.items():
        lg = logs.get(nm(r['name']))
        if lg and lg.get('team') in TEAMS and lg['team'] != r['team'] and lg['gp'] >= 1:
            r['team'] = lg['team']
    by_team = {}
    for r in rows.values():
        if r['min'] >= 10:
            by_team.setdefault(r['team'], []).append({k: v for k, v in r.items() if k != 'team'})
    for t in by_team:
        by_team[t].sort(key=lambda r: -r['min'])
    log('players', f'ok: {sum(len(v) for v in by_team.values())} players across {len(by_team)} teams ({source})')
    return by_team


# ---------- injuries ----------
def injuries():
    out = {}
    try:
        js = fetch(INJ)
        for t in js.get('injuries') or []:
            ab = team_from_name(t.get('displayName')) or std((t.get('team') or {}).get('abbreviation'))
            if ab not in TEAMS:
                continue
            lst = []
            for i in t.get('injuries') or []:
                ath = i.get('athlete') or {}
                name = ath.get('displayName') or ath.get('fullName')
                st = str(i.get('status') or (i.get('type') or {}).get('description') or '').lower()
                if not name:
                    continue
                if 'out' in st or 'doubt' in st or 'suspen' in st:
                    lst.append({'name': name, 'status': 'out'})
                elif 'question' in st or 'day' in st or 'game' in st:
                    lst.append({'name': name, 'status': 'q'})
            out[ab] = lst
        log('injuries', f'ok: {len(out)} teams')
    except Exception as e:  # noqa: BLE001
        log('injuries', f'failed: {e}')
        return None
    return out


# ---------- odds ----------
def odds_of(ev):
    o = ev.get('odds') or {}
    total = o.get('overUnder')
    hl = None
    det = str(o.get('details') or '').strip()
    if det.upper() in ('EVEN', 'PK', "PICK'EM", 'PICK'):
        hl = 0.0
    else:
        m = re.match(r'([A-Za-z]+)\s*([+-]?\d+(?:\.\d+)?)', det)
        if m:
            fav, v = std(m.group(1)), float(m.group(2))
            hl = v if fav == ev['home'] else -v
        elif o.get('spread') is not None:
            try:
                hl = float(o['spread'])
            except Exception:  # noqa: BLE001
                hl = None
    try:
        total = float(total) if total is not None else None
    except Exception:  # noqa: BLE001
        total = None
    return total, hl


# ---------- slate ----------
def build_slate(results, players, inj, old, now):
    today_et = now.astimezone(ET).date()
    evs = []
    for k in range(-2, 4):
        try:
            evs += scoreboard(today_et + timedelta(days=k))
        except Exception as e:  # noqa: BLE001
            print('scoreboard failed', e)
    seen, evs2 = set(), []
    for e in evs:
        if e['id'] not in seen:
            seen.add(e['id'])
            evs2.append(e)
    evs = evs2
    starts = {}
    for e in evs:
        for t in (e['home'], e['away']):
            starts.setdefault(t, []).append(e['start'])
    oldmap = {(g.get('date'), g.get('away'), g.get('home')): g for g in (old or {}).get('games', [])}

    def b2b(team, st):
        return any(timedelta(hours=12) <= st - s <= timedelta(hours=30) for s in starts.get(team, []))

    games = []
    for e in evs:
        if not (now - timedelta(hours=5) <= e['start'] <= now + timedelta(hours=54)):
            continue
        if e['stname'] in ('STATUS_POSTPONED', 'STATUS_CANCELED'):
            continue
        loc = e['start'].astimezone(ADL)
        d = loc.date().isoformat()
        prev = oldmap.get((d, e['away'], e['home']), {})
        total, hl = odds_of(e)
        if total is None:
            total = prev.get('total')
        if hl is None:
            hl = prev.get('homeLine')
        started = e['state'] != 'pre'
        g = {'id': e['id'], 'away': e['away'], 'home': e['home'], 'date': d, 'time': loc.strftime('%H:%M'),
             'b2bA': b2b(e['away'], e['start']), 'b2bH': b2b(e['home'], e['start']), 'total': total, 'homeLine': hl,
             'players': {'away': players.get(e['away'], []), 'home': players.get(e['home'], [])}}
        if started and prev.get('inj') is not None:
            g['inj'] = prev['inj']          # freeze the pre-tip list once a game starts
            if prev.get('players'):
                g['players'] = prev['players']
        elif inj is not None:
            g['inj'] = {'away': inj.get(e['away'], []), 'home': inj.get(e['home'], [])}
        else:
            g['inj'] = prev.get('inj') or {'away': [], 'home': []}
        games.append(g)
    games.sort(key=lambda g: (g['date'], g['time']))
    log('slate', f'ok: {len(games)} games')
    return games


# ---------- team ratings ----------
def strip(s):
    return re.sub(r'<[^>]+>', '', s).replace('&nbsp;', ' ').replace('*', '').strip()


def br_table(html, tid):
    m = re.search(r'<table[^>]*id="' + re.escape(tid) + r'"[^>]*>(.*?)</table>', html, re.S)
    if not m:
        return {}
    out = {}
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', m.group(1), re.S):
        cells = dict((k, strip(v)) for k, v in re.findall(r'<t[dh][^>]*data-stat="([^"]+)"[^>]*>(.*?)</t[dh]>', row, re.S))
        name = cells.get('team') or cells.get('team_name')
        ab = team_from_name(name) if name else None
        if ab:
            out[ab] = cells
    return out


def team_ratings(old):
    try:
        html = fetch(f'https://www.basketball-reference.com/leagues/NBA_{SEASON_YEAR}.html', as_json=False)
        html = html.replace('<!--', '').replace('-->', '')
        adv = br_table(html, 'advanced-team')
        pg = br_table(html, 'per_game-team')
        opp = br_table(html, 'per_game-opponent')

        def f(d, *keys):
            for k in keys:
                if d.get(k) not in (None, ''):
                    try:
                        return float(d[k])
                    except Exception:  # noqa: BLE001
                        pass
            return None
        teams = {}
        for ab in TEAMS:
            a, p, o = adv.get(ab, {}), pg.get(ab, {}), opp.get(ab, {})
            t = {'g': f(p, 'g') or f(a, 'g'), 'pace': f(a, 'pace'), 'ortg': f(a, 'off_rtg'), 'drtg': f(a, 'def_rtg'),
                 'fg': f(p, 'fg'), 'fga': f(p, 'fga'), 'opp_pts': f(o, 'opp_pts', 'pts'), 'opp_reb': f(o, 'opp_trb', 'trb'),
                 'opp_ast': f(o, 'opp_ast', 'ast'), 'opp_thr': f(o, 'opp_fg3', 'fg3')}
            if t['pace'] and t['ortg'] and t['drtg']:
                teams[ab] = t
        if len(teams) < 25:
            raise RuntimeError(f'only {len(teams)} teams parsed')
        log('teams', f'ok: {len(teams)} teams from Basketball Reference')
        return {'asOf': datetime.now(ADL).strftime('%-d %b'), 'source': 'Basketball Reference', 'teams': teams}
    except Exception as e:  # noqa: BLE001
        log('teams', f'kept previous ratings ({e})')
        return old


def live_check():
    try:
        req = urllib.request.Request(SB, headers=dict(UA, Origin=SITE_ORIGIN))
        with urllib.request.urlopen(req, timeout=20) as r:
            acao = r.headers.get('Access-Control-Allow-Origin')
        STATUS['liveCors'] = acao or 'none'
    except Exception as e:  # noqa: BLE001
        STATUS['liveCors'] = f'error: {e}'


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'quick'
    now = datetime.now(timezone.utc)
    os.makedirs(DATA, exist_ok=True)
    seed = json.loads(SEED_JSON)

    if not os.path.exists(os.path.join(DATA, 'lastseason.json')) and seed.get('lastseason'):
        save('lastseason.json', seed['lastseason'])

    results = load('results.json', {})
    try:
        results = update_results(results, now)
        save('results.json', results)
    except Exception as e:  # noqa: BLE001
        log('results', f'failed: {e}')

    teams_old = load('teams.json', None) or seed.get('teams')
    if mode == 'full' or not teams_old:
        teams = team_ratings(teams_old)
    else:
        teams = teams_old
        STATUS['steps']['teams'] = 'unchanged (refreshed each morning)'
    if teams:
        save('teams.json', teams)

    old = load('slate.json', {})
    try:
        players = player_table(results)
        inj = injuries()
        games = build_slate(results, players, inj, old, now)
        if games or not old.get('games'):
            stamp = now.astimezone(ADL).isoformat(timespec='seconds')
            save('slate.json', {'updated': stamp, 'injUpdated': stamp if inj is not None else old.get('injUpdated'),
                                'statsAsOf': now.astimezone(ADL).strftime('%-d %b'), 'games': games})
    except Exception as e:  # noqa: BLE001
        log('slate', f'failed: {e}')

    live_check()
    STATUS['updated'] = now.astimezone(ADL).isoformat(timespec='seconds')
    STATUS['mode'] = mode
    save('status.json', STATUS)
    print(json.dumps(STATUS, indent=1))



# Starting data: team ratings and last season's averages, used until the first refresh.
SEED_JSON = r'''{"teams":{"asOf":"6 Nov","source":"Basketball Reference","teams":{"ATL":{"drtg":111.3,"fg":42.5,"fga":95.5,"g":8,"opp_ast":27.5,"opp_pts":114,"opp_reb":45.9,"opp_thr":10.6,"ortg":112.6,"pace":102.4},"BKN":{"drtg":113.8,"fg":35,"fga":83.9,"g":7,"opp_ast":27.6,"opp_pts":114.1,"opp_reb":42.4,"opp_thr":15,"ortg":105.3,"pace":98.8},"BOS":{"drtg":111.1,"fg":39.9,"fga":87.1,"g":7,"opp_ast":26,"opp_pts":109.1,"opp_reb":44.4,"opp_thr":14.7,"ortg":117.1,"pace":96.8},"CHA":{"drtg":113.7,"fg":35.3,"fga":87.1,"g":8,"opp_ast":25.4,"opp_pts":108.6,"opp_reb":40.3,"opp_thr":13.4,"ortg":101.9,"pace":95.5},"CHI":{"drtg":122.1,"fg":37.6,"fga":90.3,"g":7,"opp_ast":30,"opp_pts":128.9,"opp_reb":47.9,"opp_thr":14.7,"ortg":106.6,"pace":105.6},"CLE":{"drtg":113.5,"fg":42.6,"fga":90.9,"g":7,"opp_ast":26.9,"opp_pts":116.1,"opp_reb":46.4,"opp_thr":13.4,"ortg":117,"pace":100.8},"DAL":{"drtg":113,"fg":41.9,"fga":86.4,"g":7,"opp_ast":26.7,"opp_pts":112.3,"opp_reb":43.1,"opp_thr":11.7,"ortg":113,"pace":99.3},"DEN":{"drtg":117.5,"fg":39.3,"fga":83.7,"g":6,"opp_ast":28.3,"opp_pts":115.7,"opp_reb":46.2,"opp_thr":14,"ortg":115.2,"pace":96.7},"DET":{"drtg":102.4,"fg":40,"fga":85.3,"g":7,"opp_ast":22.6,"opp_pts":100.3,"opp_reb":44.7,"opp_thr":12.4,"ortg":114.2,"pace":97.9},"GSW":{"drtg":116.2,"fg":39.8,"fga":87.4,"g":8,"opp_ast":27.3,"opp_pts":114.6,"opp_reb":41.3,"opp_thr":12.6,"ortg":119.2,"pace":97.3},"HOU":{"drtg":109.4,"fg":42.6,"fga":85.6,"g":8,"opp_ast":23.1,"opp_pts":106.4,"opp_reb":39,"opp_thr":11.6,"ortg":122.6,"pace":97.2},"IND":{"drtg":114.9,"fg":40,"fga":85.9,"g":8,"opp_ast":26.9,"opp_pts":115.6,"opp_reb":47.6,"opp_thr":14.6,"ortg":111,"pace":100.6},"LAC":{"drtg":115.9,"fg":37.7,"fga":83.9,"g":7,"opp_ast":25.7,"opp_pts":115.4,"opp_reb":43,"opp_thr":12.4,"ortg":108.6,"pace":99.6},"LAL":{"drtg":114.9,"fg":42.4,"fga":81.5,"g":8,"opp_ast":27.4,"opp_pts":115.3,"opp_reb":40.8,"opp_thr":14.6,"ortg":122.1,"pace":100.3},"MEM":{"drtg":108.7,"fg":43.4,"fga":91,"g":7,"opp_ast":24,"opp_pts":110.1,"opp_reb":42.4,"opp_thr":11.7,"ortg":121.4,"pace":99.8},"MIA":{"drtg":119.8,"fg":42.7,"fga":89.3,"g":7,"opp_ast":29.9,"opp_pts":123.9,"opp_reb":45.7,"opp_thr":17.3,"ortg":114.2,"pace":103.4},"MIL":{"drtg":121.1,"fg":40.9,"fga":94.3,"g":7,"opp_ast":28.7,"opp_pts":124.1,"opp_reb":47.9,"opp_thr":14.7,"ortg":108.4,"pace":102.5},"MIN":{"drtg":105.5,"fg":44,"fga":93.6,"g":8,"opp_ast":24.6,"opp_pts":107.6,"opp_reb":44.8,"opp_thr":11.5,"ortg":116.7,"pace":100.7},"NOP":{"drtg":113.6,"fg":43.9,"fga":88.4,"g":7,"opp_ast":28.1,"opp_pts":120.1,"opp_reb":43.3,"opp_thr":12.4,"ortg":115.2,"pace":102.7},"NYK":{"drtg":108.9,"fg":37.1,"fga":88.7,"g":7,"opp_ast":26.1,"opp_pts":105.1,"opp_reb":41.7,"opp_thr":13.7,"ortg":110.2,"pace":96.6},"OKC":{"drtg":110.5,"fg":40,"fga":91.4,"g":7,"opp_ast":26.7,"opp_pts":107.7,"opp_reb":44.7,"opp_thr":14.7,"ortg":110.8,"pace":97.5},"ORL":{"drtg":117.3,"fg":37,"fga":81.7,"g":7,"opp_ast":29.4,"opp_pts":114.7,"opp_reb":43.9,"opp_thr":12,"ortg":108.1,"pace":97.8},"PHI":{"drtg":105.4,"fg":40,"fga":88.5,"g":8,"opp_ast":23.6,"opp_pts":105.1,"opp_reb":48.4,"opp_thr":11.1,"ortg":111.2,"pace":99.7},"PHX":{"drtg":124.5,"fg":41,"fga":88,"g":5,"opp_ast":26.6,"opp_pts":122.8,"opp_reb":46.4,"opp_thr":16.2,"ortg":113.8,"pace":96.6},"POR":{"drtg":110,"fg":41.3,"fga":87.8,"g":6,"opp_ast":23.7,"opp_pts":109.3,"opp_reb":40,"opp_thr":11.5,"ortg":120.9,"pace":99.4},"SAC":{"drtg":121.3,"fg":40.9,"fga":88.4,"g":7,"opp_ast":28.4,"opp_pts":124.3,"opp_reb":44.6,"opp_thr":13.7,"ortg":107.5,"pace":102.5},"SAS":{"drtg":106.9,"fg":43.1,"fga":92.9,"g":7,"opp_ast":24.1,"opp_pts":108.3,"opp_reb":45,"opp_thr":12,"ortg":116.1,"pace":99.8},"TOR":{"drtg":109.2,"fg":43,"fga":93.9,"g":7,"opp_ast":24.3,"opp_pts":111.4,"opp_reb":42.1,"opp_thr":12.9,"ortg":116.7,"pace":102.1},"UTA":{"drtg":117.4,"fg":37.4,"fga":87.4,"g":7,"opp_ast":22.7,"opp_pts":118.9,"opp_reb":45.3,"opp_thr":13.7,"ortg":105.7,"pace":99.7},"WAS":{"drtg":109.5,"fg":39,"fga":82,"g":7,"opp_ast":23.7,"opp_pts":107.4,"opp_reb":46.6,"opp_thr":11.9,"ortg":111.6,"pace":98.1}}},"lastseason":{"players":{"A.J. Lawson":{"ast":0.3,"blk":0.2,"gp":24,"min":9.4,"pts":4.2,"reb":1.8,"stl":0.5,"thr":0.8},"AJ Green":{"ast":1.9,"blk":0.1,"gp":78,"min":29.1,"pts":10.4,"reb":2.7,"stl":0.5,"thr":3},"AJ Johnson":{"ast":0.9,"blk":0,"gp":25,"min":8.6,"pts":2.8,"reb":1.2,"stl":0.3,"thr":0.3},"Aaron Gordon":{"ast":2.7,"blk":0.3,"gp":36,"min":27.9,"pts":16.2,"reb":5.8,"stl":0.6,"thr":1.7},"Aaron Holiday":{"ast":1.1,"blk":0.1,"gp":57,"min":13.7,"pts":5.5,"reb":1,"stl":0.5,"thr":1.2},"Aaron Nesmith":{"ast":1.9,"blk":0.5,"gp":45,"min":29.7,"pts":13.8,"reb":4.2,"stl":0.6,"thr":2.3},"Aaron Wiggins":{"ast":1.7,"blk":0.4,"gp":65,"min":21.8,"pts":9.4,"reb":3.1,"stl":0.9,"thr":1.5},"Ace Bailey":{"ast":1.8,"blk":0.7,"gp":72,"min":27.6,"pts":13.8,"reb":4.2,"stl":0.8,"thr":1.9},"Adem Bona":{"ast":0.5,"blk":1.2,"gp":71,"min":17.4,"pts":4.8,"reb":4.3,"stl":0.4,"thr":0},"Ajay Mitchell":{"ast":3.6,"blk":0.3,"gp":57,"min":25.8,"pts":13.6,"reb":3.3,"stl":1.2,"thr":1.1},"Al Horford":{"ast":2.6,"blk":1.1,"gp":45,"min":21.5,"pts":8.3,"reb":4.9,"stl":0.7,"thr":1.6},"Alex Caruso":{"ast":2,"blk":0.3,"gp":56,"min":18.2,"pts":6.2,"reb":2.8,"stl":1.3,"thr":0.9},"Alex Sarr":{"ast":2.7,"blk":2,"gp":48,"min":27.2,"pts":16.3,"reb":7.4,"stl":0.8,"thr":1},"Alperen \u015eeng\u00fcn":{"ast":6.2,"blk":1.1,"gp":72,"min":33.3,"pts":20.4,"reb":8.9,"stl":1.2,"thr":0.6},"Amen Thompson":{"ast":5.3,"blk":0.6,"gp":79,"min":37.4,"pts":18.3,"reb":7.8,"stl":1.5,"thr":0.3},"Amir Coffey":{"ast":0.4,"blk":0.1,"gp":30,"min":8.8,"pts":2.4,"reb":0.9,"stl":0.1,"thr":0.2},"Andre Drummond":{"ast":1.3,"blk":0.8,"gp":63,"min":19.5,"pts":6.4,"reb":8.4,"stl":0.6,"thr":0.5},"Andre Jackson Jr.":{"ast":0.9,"blk":0.1,"gp":48,"min":8.5,"pts":2.4,"reb":1.5,"stl":0.4,"thr":0.3},"Andrew Nembhard":{"ast":7.7,"blk":0.1,"gp":57,"min":31.3,"pts":16.9,"reb":2.8,"stl":0.9,"thr":1.9},"Andrew Wiggins":{"ast":2.7,"blk":1,"gp":68,"min":30.3,"pts":15.4,"reb":4.8,"stl":1.1,"thr":2},"Anfernee Simons":{"ast":2.4,"blk":0.1,"gp":49,"min":24.5,"pts":14.2,"reb":2.4,"stl":0.6,"thr":2.7},"Anthony Black":{"ast":3.7,"blk":0.7,"gp":64,"min":29.8,"pts":15,"reb":3.8,"stl":1.4,"thr":1.6},"Anthony Davis":{"ast":2.8,"blk":1.7,"gp":20,"min":31.3,"pts":20.4,"reb":11.1,"stl":1.1,"thr":0.5},"Anthony Edwards":{"ast":3.7,"blk":0.8,"gp":61,"min":35,"pts":28.8,"reb":5,"stl":1.4,"thr":3.4},"Anthony Gill":{"ast":1.3,"blk":0.4,"gp":55,"min":17.3,"pts":5.8,"reb":2.9,"stl":0.6,"thr":0.3},"Ariel Hukporti":{"ast":0.5,"blk":0.5,"gp":54,"min":9.2,"pts":2.2,"reb":2.9,"stl":0.2,"thr":0},"Asa Newell":{"ast":0.6,"blk":0.3,"gp":44,"min":11.4,"pts":5.2,"reb":2.2,"stl":0.4,"thr":0.7},"Ausar Thompson":{"ast":3.1,"blk":0.9,"gp":73,"min":26,"pts":9.9,"reb":5.7,"stl":2,"thr":0.1},"Austin Reaves":{"ast":5.5,"blk":0.4,"gp":51,"min":34.5,"pts":23.3,"reb":4.7,"stl":1.1,"thr":2.3},"Ayo Dosunmu":{"ast":3.6,"blk":0.3,"gp":45,"min":26.4,"pts":15,"reb":3,"stl":0.8,"thr":1.9},"Bam Adebayo":{"ast":3.2,"blk":0.7,"gp":73,"min":32.4,"pts":20.1,"reb":10,"stl":1.2,"thr":1.7},"Baylor Scheierman":{"ast":1.5,"blk":0.1,"gp":77,"min":18.6,"pts":5.5,"reb":3.5,"stl":0.5,"thr":1.3},"Ben Saraf":{"ast":3.3,"blk":0.2,"gp":44,"min":20.8,"pts":7.5,"reb":2.1,"stl":0.9,"thr":0.4},"Ben Sheppard":{"ast":1.8,"blk":0.1,"gp":65,"min":21.4,"pts":7.1,"reb":3,"stl":0.6,"thr":1.3},"Bennedict Mathurin":{"ast":2.3,"blk":0.1,"gp":28,"min":31.8,"pts":17.8,"reb":5.4,"stl":0.6,"thr":2.1},"Bez Mbeng":{"ast":4.1,"blk":0.3,"gp":15,"min":32.8,"pts":8.1,"reb":3.8,"stl":2.3,"thr":0.4},"Bilal Coulibaly":{"ast":2.6,"blk":1,"gp":56,"min":26.2,"pts":11.7,"reb":4.3,"stl":1.3,"thr":1.2},"Blake Hinson":{"ast":1.1,"blk":0.1,"gp":14,"min":20.4,"pts":11.9,"reb":2.4,"stl":0.4,"thr":2.6},"Blake Wesley":{"ast":2,"blk":0.2,"gp":31,"min":11.6,"pts":4.8,"reb":1.3,"stl":0.5,"thr":1},"Bobby Portis":{"ast":1.6,"blk":0.2,"gp":67,"min":24.2,"pts":13.7,"reb":6.4,"stl":0.6,"thr":2},"Bogdan Bogdanovi\u0107":{"ast":2.2,"blk":0.1,"gp":23,"min":19.7,"pts":7.4,"reb":2.6,"stl":0.4,"thr":1.4},"Bones Hyland":{"ast":2.6,"blk":0.2,"gp":71,"min":16.6,"pts":8.5,"reb":1.8,"stl":0.6,"thr":1.6},"Branden Carlson":{"ast":0.7,"blk":0.6,"gp":42,"min":11.6,"pts":5.8,"reb":3,"stl":0.2,"thr":0.7},"Brandin Podziemski":{"ast":3.7,"blk":0.2,"gp":82,"min":28.5,"pts":13.8,"reb":5.1,"stl":1.1,"thr":1.9},"Brandon Ingram":{"ast":3.7,"blk":0.7,"gp":77,"min":33.8,"pts":21.5,"reb":5.6,"stl":0.8,"thr":1.8},"Brandon Miller":{"ast":3.3,"blk":0.7,"gp":65,"min":30.3,"pts":20.2,"reb":4.9,"stl":1,"thr":3.1},"Brandon Williams":{"ast":3.9,"blk":0.3,"gp":66,"min":22.2,"pts":13,"reb":2.9,"stl":0.9,"thr":0.5},"Brice Sensabaugh":{"ast":1.9,"blk":0.2,"gp":75,"min":23.5,"pts":14.9,"reb":3.1,"stl":0.7,"thr":2.2},"Bronny James":{"ast":1.2,"blk":0.1,"gp":42,"min":8.9,"pts":2.9,"reb":0.5,"stl":0.5,"thr":0.5},"Brook Lopez":{"ast":1.3,"blk":1.2,"gp":75,"min":21.8,"pts":8.5,"reb":3.6,"stl":0.6,"thr":1.5},"Brooks Barnhizer":{"ast":0.6,"blk":0.1,"gp":40,"min":8.7,"pts":1.7,"reb":2,"stl":0.3,"thr":0.2},"Bruce Brown":{"ast":2.1,"blk":0.2,"gp":82,"min":24.4,"pts":7.9,"reb":3.9,"stl":1,"thr":0.7},"Bryce McGowens":{"ast":1.5,"blk":0.2,"gp":42,"min":21,"pts":8.1,"reb":2.1,"stl":0.6,"thr":0.9},"Bub Carrington":{"ast":4.6,"blk":0.2,"gp":82,"min":27.7,"pts":10.7,"reb":3.4,"stl":0.6,"thr":2.1},"Buddy Hield":{"ast":1.5,"blk":0.2,"gp":44,"min":17.5,"pts":8,"reb":2.5,"stl":0.8,"thr":1.5},"CJ McCollum":{"ast":4.1,"blk":0.6,"gp":41,"min":28.8,"pts":18.7,"reb":3.1,"stl":1,"thr":2.3},"Cade Cunningham":{"ast":9.9,"blk":0.8,"gp":64,"min":33.9,"pts":23.9,"reb":5.5,"stl":1.4,"thr":2},"Caleb Love":{"ast":2.5,"blk":0.1,"gp":49,"min":20.7,"pts":10.4,"reb":2.3,"stl":0.6,"thr":1.9},"Caleb Martin":{"ast":1.4,"blk":0.3,"gp":58,"min":14.8,"pts":3.9,"reb":2.5,"stl":0.7,"thr":0.3},"Cam Christie":{"ast":0.6,"blk":0.1,"gp":55,"min":8.7,"pts":2.8,"reb":1.4,"stl":0.3,"thr":0.2},"Cam Spencer":{"ast":5.6,"blk":0.2,"gp":72,"min":23.8,"pts":11.1,"reb":2.5,"stl":0.7,"thr":2},"Cam Thomas":{"ast":3.1,"blk":0.1,"gp":24,"min":24.3,"pts":15.6,"reb":1.8,"stl":0.2,"thr":1.6},"Cam Whitmore":{"ast":0.7,"blk":0.4,"gp":21,"min":16.9,"pts":9.2,"reb":2.8,"stl":0.7,"thr":0.8},"Cameron Johnson":{"ast":2.4,"blk":0.4,"gp":54,"min":30.5,"pts":12.2,"reb":3.8,"stl":0.7,"thr":2},"Cameron Payne":{"ast":2.6,"blk":0.3,"gp":22,"min":17,"pts":7.4,"reb":2,"stl":1.1,"thr":1.5},"Caris LeVert":{"ast":2.7,"blk":0.7,"gp":60,"min":19.2,"pts":7.4,"reb":2,"stl":0.9,"thr":1},"Carter Bryant":{"ast":0.7,"blk":0.3,"gp":71,"min":11.5,"pts":4.2,"reb":2.5,"stl":0.2,"thr":0.7},"Cason Wallace":{"ast":2.6,"blk":0.4,"gp":77,"min":26.6,"pts":8.6,"reb":3.1,"stl":1.9,"thr":1.3},"Cedric Coward":{"ast":2.8,"blk":0.4,"gp":62,"min":25.8,"pts":13.6,"reb":5.9,"stl":0.6,"thr":1.5},"Chaney Johnson":{"ast":2.1,"blk":0.5,"gp":17,"min":20.5,"pts":8.2,"reb":4.6,"stl":0.9,"thr":0.5},"Chet Holmgren":{"ast":1.7,"blk":1.9,"gp":69,"min":28.9,"pts":17.1,"reb":8.9,"stl":0.6,"thr":1.3},"Chris Paul":{"ast":3.3,"blk":0,"gp":16,"min":14.3,"pts":2.9,"reb":1.8,"stl":0.7,"thr":0.6},"Christian Braun":{"ast":2.7,"blk":0.3,"gp":44,"min":31.8,"pts":12,"reb":4.8,"stl":0.7,"thr":1},"Christian Koloko":{"ast":0.6,"blk":0.7,"gp":14,"min":11.1,"pts":3.1,"reb":2.6,"stl":0.3,"thr":0.1},"Clint Capela":{"ast":0.7,"blk":0.8,"gp":75,"min":12.3,"pts":3.8,"reb":4.6,"stl":0.5,"thr":0},"Coby White":{"ast":4.7,"blk":0.1,"gp":29,"min":29.1,"pts":18.6,"reb":3.7,"stl":0.7,"thr":2.5},"Cody Williams":{"ast":2,"blk":0.4,"gp":67,"min":24.3,"pts":8.8,"reb":3,"stl":0.8,"thr":0.4},"Cole Anthony":{"ast":3.5,"blk":0.3,"gp":35,"min":15.1,"pts":6.7,"reb":2.5,"stl":0.6,"thr":0.6},"Collin Gillespie":{"ast":4.6,"blk":0.2,"gp":80,"min":28.5,"pts":12.7,"reb":4.1,"stl":1.2,"thr":2.9},"Collin Murray-Boyles":{"ast":1.9,"blk":0.9,"gp":57,"min":21.9,"pts":8.5,"reb":5,"stl":0.9,"thr":0.3},"Collin Sexton":{"ast":3.7,"blk":0.2,"gp":42,"min":22.3,"pts":14.2,"reb":1.9,"stl":0.9,"thr":1.4},"Cooper Flagg":{"ast":4.5,"blk":0.9,"gp":70,"min":33.5,"pts":21,"reb":6.7,"stl":1.2,"thr":1},"Corey Kispert":{"ast":1.5,"blk":0.2,"gp":39,"min":18.2,"pts":9.2,"reb":2.3,"stl":0.2,"thr":1.5},"Cormac Ryan":{"ast":1.7,"blk":0.3,"gp":11,"min":24.6,"pts":14.3,"reb":2.5,"stl":1,"thr":2.5},"Craig Porter Jr.":{"ast":3.2,"blk":0.6,"gp":64,"min":17.9,"pts":4.5,"reb":3.4,"stl":0.9,"thr":0.5},"Curtis Jones":{"ast":1,"blk":0,"gp":10,"min":8.8,"pts":2.9,"reb":1.1,"stl":0.4,"thr":0.6},"D'Angelo Russell":{"ast":4,"blk":0.1,"gp":26,"min":19,"pts":10.2,"reb":2.3,"stl":0.5,"thr":1.3},"DaRon Holmes":{"ast":0.6,"blk":0.2,"gp":25,"min":8.4,"pts":3.7,"reb":1.4,"stl":0,"thr":0.8},"Daeqwon Plowden":{"ast":1.3,"blk":0.2,"gp":32,"min":26.4,"pts":10.8,"reb":3,"stl":0.7,"thr":1.9},"Dalen Terry":{"ast":1.3,"blk":0.3,"gp":34,"min":11.1,"pts":3.5,"reb":1.9,"stl":0.6,"thr":0.6},"Dalton Knecht":{"ast":0.4,"blk":0.2,"gp":54,"min":10.2,"pts":4.2,"reb":1.4,"stl":0.2,"thr":0.7},"Daniel Gafford":{"ast":1.1,"blk":1.3,"gp":55,"min":21.7,"pts":9.5,"reb":6.9,"stl":0.8,"thr":0},"Daniss Jenkins":{"ast":3.9,"blk":0.2,"gp":72,"min":20.2,"pts":9.3,"reb":2.3,"stl":0.9,"thr":1},"Danny Wolf":{"ast":2.2,"blk":0.6,"gp":57,"min":20.8,"pts":8.9,"reb":4.9,"stl":0.5,"thr":1.2},"Darius Garland":{"ast":6.9,"blk":0.1,"gp":26,"min":30.5,"pts":18,"reb":2.4,"stl":0.8,"thr":2.3},"Davion Mitchell":{"ast":6.5,"blk":0.2,"gp":70,"min":28.6,"pts":9.3,"reb":2.7,"stl":1,"thr":1.3},"Day'Ron Sharpe":{"ast":2.3,"blk":0.4,"gp":62,"min":18.7,"pts":8.7,"reb":6.7,"stl":1.1,"thr":0.1},"De'Aaron Fox":{"ast":6.2,"blk":0.3,"gp":72,"min":31,"pts":18.6,"reb":3.8,"stl":1.2,"thr":1.8},"De'Andre Hunter":{"ast":2.1,"blk":0.1,"gp":43,"min":26.2,"pts":14,"reb":4.2,"stl":0.7,"thr":1.7},"De'Anthony Melton":{"ast":2.6,"blk":0.4,"gp":49,"min":23,"pts":12.3,"reb":3.2,"stl":1.6,"thr":1.5},"DeAndre Jordan":{"ast":0.9,"blk":0.8,"gp":12,"min":16.6,"pts":4.4,"reb":6.3,"stl":0.3,"thr":0},"DeJon Jarreau":{"ast":3.3,"blk":0.7,"gp":11,"min":21,"pts":8,"reb":4.6,"stl":0.9,"thr":0.4},"DeMar DeRozan":{"ast":4.1,"blk":0.3,"gp":77,"min":31.2,"pts":18.4,"reb":2.9,"stl":1,"thr":0.6},"Dean Wade":{"ast":1.5,"blk":0.4,"gp":59,"min":22.3,"pts":5.8,"reb":4.2,"stl":0.7,"thr":1.2},"Deandre Ayton":{"ast":0.8,"blk":1,"gp":72,"min":27.2,"pts":12.5,"reb":8,"stl":0.6,"thr":0},"Dejounte Murray":{"ast":6.4,"blk":0.2,"gp":14,"min":27.8,"pts":16.7,"reb":5.4,"stl":1.6,"thr":1.4},"Deni Avdija":{"ast":6.7,"blk":0.6,"gp":66,"min":33.3,"pts":24.2,"reb":6.9,"stl":0.8,"thr":1.9},"Dennis Schr\u00f6der":{"ast":5.3,"blk":0.2,"gp":40,"min":26.4,"pts":12.8,"reb":3.1,"stl":0.8,"thr":1.4},"Derik Queen":{"ast":3.7,"blk":0.9,"gp":81,"min":25,"pts":11.7,"reb":7.1,"stl":1,"thr":0.3},"Derrick Jones Jr.":{"ast":1.4,"blk":1,"gp":50,"min":27,"pts":10.1,"reb":3.5,"stl":0.9,"thr":1.1},"Derrick White":{"ast":5.4,"blk":1.3,"gp":77,"min":34.1,"pts":16.5,"reb":4.4,"stl":1.1,"thr":2.7},"Desmond Bane":{"ast":4.1,"blk":0.5,"gp":82,"min":33.6,"pts":20.1,"reb":4.1,"stl":1,"thr":2},"Devin Booker":{"ast":6,"blk":0.3,"gp":64,"min":33.5,"pts":26.1,"reb":3.9,"stl":0.8,"thr":1.9},"Devin Carter":{"ast":2.7,"blk":0.2,"gp":38,"min":18.4,"pts":8.9,"reb":3.3,"stl":0.9,"thr":0.8},"Devin Vassell":{"ast":2.5,"blk":0.4,"gp":67,"min":30.5,"pts":13.9,"reb":4,"stl":0.9,"thr":2.5},"Dillon Brooks":{"ast":1.8,"blk":0.2,"gp":56,"min":30.4,"pts":20.2,"reb":3.6,"stl":1,"thr":2.3},"Domantas Sabonis":{"ast":4.1,"blk":0.2,"gp":19,"min":29.7,"pts":15.8,"reb":11.4,"stl":0.9,"thr":0.3},"Dominick Barlow":{"ast":1.2,"blk":0.7,"gp":71,"min":23.8,"pts":7.7,"reb":4.8,"stl":0.9,"thr":0.3},"Donovan Clingan":{"ast":2.1,"blk":1.7,"gp":77,"min":27.2,"pts":12.1,"reb":11.6,"stl":0.6,"thr":1.1},"Donovan Mitchell":{"ast":5.7,"blk":0.3,"gp":70,"min":33.5,"pts":27.9,"reb":4.5,"stl":1.5,"thr":3.2},"Donte DiVincenzo":{"ast":3.8,"blk":0.4,"gp":82,"min":30.4,"pts":12.2,"reb":4.1,"stl":1.3,"thr":3},"Dorian Finney-Smith":{"ast":1,"blk":0.2,"gp":37,"min":16.8,"pts":3.3,"reb":2.5,"stl":0.4,"thr":0.7},"Doug McDermott":{"ast":0.8,"blk":0.1,"gp":29,"min":15.1,"pts":5.7,"reb":1.3,"stl":0.2,"thr":1.7},"Drake Powell":{"ast":1.4,"blk":0.2,"gp":63,"min":21,"pts":6.5,"reb":1.8,"stl":0.6,"thr":0.8},"Draymond Green":{"ast":5.5,"blk":0.6,"gp":68,"min":27.5,"pts":8.4,"reb":5.5,"stl":0.9,"thr":1.5},"Drew Eubanks":{"ast":0.5,"blk":0.6,"gp":42,"min":13.1,"pts":5.2,"reb":3,"stl":0.4,"thr":0},"Drew Timme":{"ast":0.9,"blk":0,"gp":27,"min":8.7,"pts":3.4,"reb":1.2,"stl":0.2,"thr":0.4},"Dru Smith":{"ast":2.6,"blk":0.3,"gp":70,"min":16.3,"pts":5.6,"reb":2.5,"stl":1.4,"thr":0.5},"Duncan Robinson":{"ast":2.1,"blk":0.3,"gp":77,"min":27.4,"pts":12.2,"reb":2.7,"stl":0.6,"thr":2.9},"Duop Reath":{"ast":0.3,"blk":0.3,"gp":32,"min":8.1,"pts":2.9,"reb":1.2,"stl":0.2,"thr":0.7},"Dwight Powell":{"ast":1.1,"blk":0.3,"gp":63,"min":14.3,"pts":3.3,"reb":4.1,"stl":0.5,"thr":0},"Dylan Cardwell":{"ast":1.4,"blk":1.5,"gp":44,"min":20.6,"pts":5.4,"reb":7.5,"stl":0.7,"thr":0},"Dylan Harper":{"ast":3.9,"blk":0.3,"gp":69,"min":22.6,"pts":11.8,"reb":3.4,"stl":0.8,"thr":0.9},"Dyson Daniels":{"ast":5.9,"blk":0.4,"gp":76,"min":33.2,"pts":11.9,"reb":6.8,"stl":2,"thr":0.3},"E.J. Liddell":{"ast":0.9,"blk":0.4,"gp":26,"min":13.4,"pts":5.7,"reb":2.7,"stl":0.2,"thr":0.7},"Egor Demin":{"ast":3.3,"blk":0.3,"gp":52,"min":25.2,"pts":10.3,"reb":3.2,"stl":0.8,"thr":2.4},"Elijah Harkless":{"ast":2.9,"blk":0.2,"gp":26,"min":21,"pts":6.8,"reb":2,"stl":1.2,"thr":0.8},"Ethan Thompson":{"ast":1.8,"blk":0.3,"gp":32,"min":20.4,"pts":7,"reb":2.2,"stl":0.6,"thr":1.2},"Evan Mobley":{"ast":3.6,"blk":1.7,"gp":65,"min":31.9,"pts":18.2,"reb":9,"stl":0.7,"thr":1},"Franz Wagner":{"ast":3.3,"blk":0.3,"gp":34,"min":30,"pts":20.6,"reb":5.2,"stl":0.9,"thr":1.4},"GG Jackson II":{"ast":1.5,"blk":0.8,"gp":55,"min":21.4,"pts":12.5,"reb":4.3,"stl":0.6,"thr":1.2},"Gabe Vincent":{"ast":1.3,"blk":0,"gp":29,"min":19.3,"pts":4.8,"reb":0.9,"stl":0.5,"thr":1.3},"Garrison Mathews":{"ast":0.7,"blk":0.2,"gp":15,"min":13.1,"pts":5.2,"reb":1.1,"stl":0.4,"thr":1.1},"Gary Harris":{"ast":1.1,"blk":0.2,"gp":48,"min":13.8,"pts":2.7,"reb":1.3,"stl":0.6,"thr":0.6},"Gary Payton II":{"ast":1.7,"blk":0.3,"gp":73,"min":15.6,"pts":7.5,"reb":3.6,"stl":0.9,"thr":0.5},"Gary Trent Jr.":{"ast":1.2,"blk":0,"gp":65,"min":21.2,"pts":8.1,"reb":1,"stl":0.5,"thr":1.9},"Giannis Antetokounmpo":{"ast":5.4,"blk":0.7,"gp":36,"min":28.9,"pts":27.6,"reb":9.8,"stl":0.9,"thr":0.4},"Goga Bitadze":{"ast":1.3,"blk":1,"gp":64,"min":15.2,"pts":5.9,"reb":5,"stl":0.6,"thr":0},"Gradey Dick":{"ast":0.7,"blk":0.1,"gp":76,"min":14,"pts":6,"reb":1.9,"stl":0.6,"thr":0.7},"Grant Williams":{"ast":1.6,"blk":0.5,"gp":36,"min":19.8,"pts":7,"reb":3.9,"stl":0.5,"thr":1.4},"Grayson Allen":{"ast":3.8,"blk":0.3,"gp":51,"min":28.8,"pts":16.5,"reb":3,"stl":1.4,"thr":3.1},"Guerschon Yabusele":{"ast":0.4,"blk":0.1,"gp":41,"min":8.9,"pts":2.7,"reb":2.1,"stl":0.1,"thr":0.5},"Gui Santos":{"ast":2.3,"blk":0.3,"gp":68,"min":20.5,"pts":9.2,"reb":3.9,"stl":0.9,"thr":1.1},"Harrison Barnes":{"ast":1.9,"blk":0.2,"gp":77,"min":25.8,"pts":9.9,"reb":2.8,"stl":0.6,"thr":1.8},"Herbert Jones":{"ast":2.8,"blk":0.5,"gp":56,"min":28.4,"pts":8.9,"reb":3.4,"stl":1.6,"thr":1.4},"Hugo Gonzalez":{"ast":0.5,"blk":0.3,"gp":74,"min":14.6,"pts":3.9,"reb":3.3,"stl":0.6,"thr":0.6},"Immanuel Quickley":{"ast":5.9,"blk":0.1,"gp":70,"min":31.9,"pts":16.4,"reb":4,"stl":1.3,"thr":2.5},"Isaac Okoro":{"ast":1.6,"blk":0.5,"gp":63,"min":26.9,"pts":9.3,"reb":2.7,"stl":0.7,"thr":1.2},"Isaiah Collier":{"ast":7.2,"blk":0.3,"gp":59,"min":25.7,"pts":11.7,"reb":2.5,"stl":1.1,"thr":0.5},"Isaiah Hartenstein":{"ast":3.5,"blk":0.8,"gp":47,"min":24.2,"pts":9.2,"reb":9.4,"stl":1,"thr":0},"Isaiah Jackson":{"ast":0.8,"blk":0.8,"gp":38,"min":16.8,"pts":6.4,"reb":5.6,"stl":0.7,"thr":0},"Isaiah Joe":{"ast":1.3,"blk":0.2,"gp":71,"min":21.2,"pts":11.1,"reb":2.5,"stl":0.7,"thr":2.5},"Isaiah Livers":{"ast":0.6,"blk":0.2,"gp":36,"min":9.6,"pts":1.8,"reb":1.7,"stl":0.4,"thr":0.3},"Isaiah Stewart":{"ast":1.1,"blk":1.6,"gp":58,"min":22.7,"pts":10,"reb":5,"stl":0.3,"thr":0.7},"Ivica Zubac":{"ast":2.2,"blk":0.8,"gp":43,"min":30.9,"pts":14.4,"reb":11,"stl":0.4,"thr":0},"Ja Morant":{"ast":8.1,"blk":0.3,"gp":20,"min":28.5,"pts":19.5,"reb":3.3,"stl":1,"thr":1},"Ja'Kobe Walter":{"ast":1.2,"blk":0.2,"gp":72,"min":20.5,"pts":7.5,"reb":2.6,"stl":1,"thr":1.5},"Jabari Smith Jr.":{"ast":1.9,"blk":0.9,"gp":77,"min":35.1,"pts":15.8,"reb":6.9,"stl":0.7,"thr":2.3},"Jabari Walker":{"ast":0.5,"blk":0.2,"gp":64,"min":11.9,"pts":4.3,"reb":3,"stl":0.4,"thr":0.5},"Jaden Hardy":{"ast":0.9,"blk":0.1,"gp":34,"min":12.6,"pts":6.9,"reb":1.4,"stl":0.3,"thr":1.4},"Jaden Ivey":{"ast":1.6,"blk":0.4,"gp":33,"min":16.8,"pts":8.2,"reb":2.2,"stl":0.5,"thr":1.3},"Jaden McDaniels":{"ast":2.7,"blk":1,"gp":73,"min":31.7,"pts":14.8,"reb":4.2,"stl":1.1,"thr":1.4},"Jae'Sean Tate":{"ast":0.5,"blk":0.1,"gp":46,"min":8.8,"pts":2.8,"reb":1.6,"stl":0.2,"thr":0.2},"Jahmai Mashack":{"ast":2.2,"blk":0.5,"gp":31,"min":21.7,"pts":6.2,"reb":2.6,"stl":1.2,"thr":0.6},"Jaime Jaquez Jr.":{"ast":4.7,"blk":0.3,"gp":75,"min":28.3,"pts":15.4,"reb":5,"stl":0.7,"thr":0.8},"Jake LaRavia":{"ast":1.8,"blk":0.5,"gp":82,"min":25.1,"pts":8.2,"reb":4,"stl":1.3,"thr":1},"Jakob Poeltl":{"ast":2,"blk":0.7,"gp":46,"min":25,"pts":10.7,"reb":7,"stl":0.9,"thr":0},"Jalen Brunson":{"ast":6.8,"blk":0.1,"gp":74,"min":35,"pts":26,"reb":3.3,"stl":0.8,"thr":2.6},"Jalen Duren":{"ast":2,"blk":0.8,"gp":70,"min":28.2,"pts":19.5,"reb":10.5,"stl":0.8,"thr":0},"Jalen Green":{"ast":2.8,"blk":0.3,"gp":32,"min":25.9,"pts":17.8,"reb":3.6,"stl":1.1,"thr":2.2},"Jalen Johnson":{"ast":7.9,"blk":0.4,"gp":72,"min":35.2,"pts":22.5,"reb":10.3,"stl":1.2,"thr":1.7},"Jalen Pickett":{"ast":2.3,"blk":0.1,"gp":50,"min":16.1,"pts":5.2,"reb":2.3,"stl":0.3,"thr":1},"Jalen Slawson":{"ast":2.8,"blk":1.1,"gp":13,"min":23.9,"pts":7.3,"reb":4.4,"stl":1.5,"thr":1.2},"Jalen Smith":{"ast":1.2,"blk":0.8,"gp":53,"min":20.7,"pts":10.2,"reb":6.7,"stl":0.5,"thr":1.5},"Jalen Suggs":{"ast":5.5,"blk":0.7,"gp":57,"min":27.6,"pts":13.8,"reb":3.9,"stl":1.8,"thr":2.1},"Jalen Williams":{"ast":5.5,"blk":0.3,"gp":33,"min":28.4,"pts":17.1,"reb":4.6,"stl":1.2,"thr":0.7},"Jalen Wilson":{"ast":0.9,"blk":0,"gp":54,"min":15.9,"pts":6.4,"reb":2.1,"stl":0.4,"thr":1},"Jamal Cain":{"ast":0.7,"blk":0.1,"gp":40,"min":12.2,"pts":5.4,"reb":2,"stl":0.3,"thr":0.7},"Jamal Murray":{"ast":7.1,"blk":0.4,"gp":75,"min":35.4,"pts":25.4,"reb":4.4,"stl":0.9,"thr":3.3},"Jamal Shead":{"ast":5.4,"blk":0.2,"gp":82,"min":22.6,"pts":6.6,"reb":1.7,"stl":0.9,"thr":1},"Jamaree Bouyea":{"ast":1.8,"blk":0.3,"gp":46,"min":14,"pts":5.7,"reb":1.8,"stl":0.6,"thr":0.5},"James Harden":{"ast":8.1,"blk":0.4,"gp":44,"min":35.4,"pts":25.4,"reb":4.8,"stl":1.3,"thr":3.1},"Jamir Watkins":{"ast":1.3,"blk":0.5,"gp":50,"min":20.6,"pts":7.4,"reb":3.9,"stl":1.1,"thr":0.9},"Jamison Battle":{"ast":0.4,"blk":0,"gp":61,"min":8.5,"pts":3.1,"reb":1.5,"stl":0.1,"thr":0.6},"Jarace Walker":{"ast":2.5,"blk":0.3,"gp":76,"min":25.7,"pts":11.6,"reb":5.1,"stl":0.8,"thr":1.8},"Jared McCain":{"ast":1.7,"blk":0.1,"gp":37,"min":16.8,"pts":6.6,"reb":2,"stl":0.6,"thr":1.2},"Jaren Jackson Jr.":{"ast":1.9,"blk":1.5,"gp":45,"min":30.7,"pts":19.2,"reb":5.8,"stl":1,"thr":1.8},"Jarred Vanderbilt":{"ast":1.3,"blk":0.3,"gp":65,"min":17.4,"pts":4.4,"reb":4.5,"stl":0.8,"thr":0.4},"Jarrett Allen":{"ast":1.8,"blk":0.8,"gp":56,"min":27.1,"pts":15.4,"reb":8.5,"stl":1,"thr":0},"Jase Richardson":{"ast":1.1,"blk":0,"gp":54,"min":10.9,"pts":4.4,"reb":1.2,"stl":0.4,"thr":0.5},"Javon Small":{"ast":3.7,"blk":0.2,"gp":41,"min":20.2,"pts":9.7,"reb":3.1,"stl":0.8,"thr":1.5},"Javonte Green":{"ast":0.7,"blk":0.3,"gp":82,"min":17.6,"pts":6.9,"reb":2.8,"stl":1.2,"thr":1.1},"Jaxson Hayes":{"ast":0.9,"blk":0.8,"gp":66,"min":18.3,"pts":7.5,"reb":4.1,"stl":0.4,"thr":0},"Jay Huff":{"ast":1.5,"blk":1.9,"gp":82,"min":21,"pts":9.5,"reb":4,"stl":0.5,"thr":1.5},"Jaylen Brown":{"ast":5.1,"blk":0.4,"gp":71,"min":34.4,"pts":28.7,"reb":6.9,"stl":1,"thr":2},"Jaylen Clark":{"ast":0.6,"blk":0.1,"gp":68,"min":13.1,"pts":4,"reb":1.8,"stl":0.7,"thr":0.5},"Jaylen Wells":{"ast":1.6,"blk":0.1,"gp":69,"min":26.4,"pts":12.5,"reb":3.2,"stl":0.9,"thr":1.9},"Jaylin Williams":{"ast":2.4,"blk":0.6,"gp":65,"min":19.6,"pts":7.2,"reb":5.5,"stl":0.5,"thr":1.5},"Jaylon Tyson":{"ast":2.2,"blk":0.4,"gp":66,"min":26.9,"pts":13.2,"reb":5.1,"stl":0.8,"thr":2},"Jayson Tatum":{"ast":5.3,"blk":0.2,"gp":16,"min":32.6,"pts":21.8,"reb":10,"stl":1.4,"thr":2.9},"Jerami Grant":{"ast":2.1,"blk":0.6,"gp":57,"min":29.7,"pts":18.6,"reb":3.5,"stl":0.7,"thr":2.4},"Jeremiah Fears":{"ast":3.4,"blk":0.4,"gp":82,"min":25.8,"pts":14.3,"reb":3.7,"stl":1.2,"thr":1.2},"Jeremiah Robinson-Earl":{"ast":0.7,"blk":0.1,"gp":17,"min":17.6,"pts":4.6,"reb":5.2,"stl":0.4,"thr":0.5},"Jeremy Sochan":{"ast":1,"blk":0.2,"gp":28,"min":12.8,"pts":4.1,"reb":2.6,"stl":0.4,"thr":0.3},"Jericho Sims":{"ast":1.6,"blk":0.3,"gp":67,"min":19.7,"pts":5,"reb":5.5,"stl":0.3,"thr":0},"Jett Howard":{"ast":0.8,"blk":0.2,"gp":55,"min":12.6,"pts":5.5,"reb":1.6,"stl":0.2,"thr":1},"Jevon Carter":{"ast":2.3,"blk":0.3,"gp":30,"min":20.4,"pts":7.2,"reb":2.1,"stl":0.9,"thr":1.5},"Jimmy Butler":{"ast":4.9,"blk":0.2,"gp":38,"min":31.1,"pts":20,"reb":5.6,"stl":1.4,"thr":0.8},"Jock Landale":{"ast":1.7,"blk":0.5,"gp":45,"min":23.6,"pts":11.3,"reb":6.5,"stl":0.6,"thr":1.1},"Joel Embiid":{"ast":3.9,"blk":1.2,"gp":38,"min":31.6,"pts":26.9,"reb":7.7,"stl":0.6,"thr":1.4},"John Collins":{"ast":1,"blk":0.7,"gp":69,"min":27.1,"pts":13.6,"reb":5.3,"stl":0.9,"thr":1.3},"John Konchar":{"ast":1.3,"blk":0.4,"gp":30,"min":14.5,"pts":3.2,"reb":3.1,"stl":1,"thr":0.4},"John Poulakidas":{"ast":0.8,"blk":0.3,"gp":13,"min":19.5,"pts":8.8,"reb":2.3,"stl":0.5,"thr":2.4},"Johnny Furphy":{"ast":1.2,"blk":0.2,"gp":35,"min":18.4,"pts":5.1,"reb":4.4,"stl":0.6,"thr":0.7},"Jonas Valan\u010di\u016bnas":{"ast":1.2,"blk":0.6,"gp":65,"min":13.4,"pts":8.7,"reb":5.1,"stl":0.2,"thr":0.1},"Jonathan Isaac":{"ast":0.4,"blk":0.6,"gp":52,"min":10,"pts":2.6,"reb":2.5,"stl":0.4,"thr":0.1},"Jonathan Kuminga":{"ast":2.5,"blk":0.3,"gp":20,"min":23.8,"pts":12.1,"reb":5.9,"stl":0.4,"thr":0.9},"Jordan Clarkson":{"ast":1.3,"blk":0.1,"gp":72,"min":17.8,"pts":8.6,"reb":1.8,"stl":0.4,"thr":1},"Jordan Goodwin":{"ast":2.2,"blk":0.2,"gp":70,"min":22.5,"pts":8.7,"reb":4.9,"stl":1.5,"thr":1.6},"Jordan Hawkins":{"ast":0.8,"blk":0.2,"gp":51,"min":13.6,"pts":5.1,"reb":1.7,"stl":0.3,"thr":0.9},"Jordan Miller":{"ast":2.3,"blk":0.2,"gp":60,"min":22.1,"pts":10,"reb":3,"stl":0.8,"thr":0.6},"Jordan Poole":{"ast":3.1,"blk":0.4,"gp":39,"min":23.9,"pts":13.4,"reb":2,"stl":0.6,"thr":2.5},"Jordan Walsh":{"ast":0.8,"blk":0.5,"gp":68,"min":17.8,"pts":5.4,"reb":4,"stl":0.7,"thr":0.7},"Jose Alvarado":{"ast":3.1,"blk":0.1,"gp":41,"min":21.9,"pts":7.9,"reb":2.8,"stl":0.9,"thr":1.7},"Josh Giddey":{"ast":9.1,"blk":0.5,"gp":54,"min":32.1,"pts":17,"reb":8.3,"stl":1,"thr":1.9},"Josh Green":{"ast":0.8,"blk":0.1,"gp":58,"min":15.7,"pts":4.3,"reb":1.8,"stl":0.6,"thr":1},"Josh Hart":{"ast":4.8,"blk":0.3,"gp":66,"min":30.2,"pts":12,"reb":7.4,"stl":1.1,"thr":1.5},"Josh Minott":{"ast":1,"blk":0.4,"gp":33,"min":15.9,"pts":5.8,"reb":3.6,"stl":0.7,"thr":1},"Josh Okogie":{"ast":0.9,"blk":0.2,"gp":78,"min":17.4,"pts":4.5,"reb":2.6,"stl":0.8,"thr":0.8},"Jrue Holiday":{"ast":6.1,"blk":0.1,"gp":53,"min":29.4,"pts":16.3,"reb":4.6,"stl":1,"thr":2.6},"Julian Champagnie":{"ast":1.5,"blk":0.5,"gp":82,"min":27.6,"pts":11.1,"reb":5.8,"stl":0.8,"thr":2.4},"Julian Phillips":{"ast":0.2,"blk":0.2,"gp":35,"min":9.5,"pts":2.8,"reb":1.3,"stl":0.5,"thr":0.5},"Julian Reese":{"ast":1.8,"blk":0.6,"gp":13,"min":30.9,"pts":11.8,"reb":10.5,"stl":1.4,"thr":0},"Julian Strawther":{"ast":1.1,"blk":0.1,"gp":57,"min":15.1,"pts":7.2,"reb":2,"stl":0.4,"thr":1},"Julius Randle":{"ast":5,"blk":0.2,"gp":79,"min":33,"pts":21.1,"reb":6.7,"stl":1.1,"thr":1.4},"Justin Champagnie":{"ast":1.2,"blk":0.6,"gp":69,"min":20,"pts":8.7,"reb":5.6,"stl":0.9,"thr":0.8},"Justin Edwards":{"ast":1.3,"blk":0.2,"gp":64,"min":15.3,"pts":6,"reb":1.5,"stl":0.8,"thr":1},"Jusuf Nurki\u0107":{"ast":4.8,"blk":0.5,"gp":41,"min":26.4,"pts":10.9,"reb":10.4,"stl":1.3,"thr":0.6},"KJ Simpson":{"ast":2.5,"blk":0,"gp":14,"min":15.9,"pts":6,"reb":2.1,"stl":1.1,"thr":0.9},"Kam Jones":{"ast":3.2,"blk":0,"gp":37,"min":16.6,"pts":4.4,"reb":1.6,"stl":0.5,"thr":0.6},"Karl-Anthony Towns":{"ast":3,"blk":0.5,"gp":75,"min":31,"pts":20.1,"reb":11.9,"stl":0.9,"thr":1.5},"Karlo Matkovi\u0107":{"ast":0.8,"blk":0.9,"gp":62,"min":14.7,"pts":5.7,"reb":3.7,"stl":0.4,"thr":0.6},"Kasparas Jaku\u010dionis":{"ast":2.6,"blk":0.1,"gp":53,"min":17.8,"pts":6.2,"reb":2.6,"stl":0.6,"thr":1.2},"Kawhi Leonard":{"ast":3.6,"blk":0.4,"gp":65,"min":32.1,"pts":27.9,"reb":6.4,"stl":1.9,"thr":2.6},"Keaton Wallace":{"ast":1.8,"blk":0.1,"gp":53,"min":10.1,"pts":3.5,"reb":1.1,"stl":0.5,"thr":0.7},"Keegan Murray":{"ast":1.7,"blk":1.6,"gp":23,"min":34.5,"pts":14,"reb":5.7,"stl":1,"thr":1.3},"Kel'el Ware":{"ast":0.7,"blk":1.1,"gp":77,"min":22.1,"pts":11.1,"reb":9,"stl":0.8,"thr":1.2},"Keldon Johnson":{"ast":1.4,"blk":0.1,"gp":82,"min":23.3,"pts":13.2,"reb":5.4,"stl":0.6,"thr":1.2},"Kelly Olynyk":{"ast":1.2,"blk":0.1,"gp":42,"min":8.6,"pts":3.2,"reb":1.8,"stl":0.4,"thr":0.3},"Kelly Oubre Jr.":{"ast":1.6,"blk":0.5,"gp":50,"min":31.5,"pts":14.1,"reb":5,"stl":1.4,"thr":1.7},"Kennedy Chandler":{"ast":6.7,"blk":0.2,"gp":11,"min":32.3,"pts":15,"reb":3.4,"stl":1,"thr":1.4},"Kenrich Williams":{"ast":1.4,"blk":0.1,"gp":56,"min":15.3,"pts":6.5,"reb":3.3,"stl":0.6,"thr":0.9},"Kentavious Caldwell-Pope":{"ast":2.7,"blk":0.2,"gp":51,"min":21.3,"pts":8.4,"reb":2.5,"stl":0.8,"thr":1.1},"Keon Ellis":{"ast":0.6,"blk":0.5,"gp":43,"min":17.6,"pts":5.6,"reb":1.3,"stl":1.1,"thr":1.2},"Keshad Johnson":{"ast":0.3,"blk":0.3,"gp":32,"min":8.8,"pts":4.3,"reb":1.9,"stl":0.5,"thr":0.4},"Kevin Durant":{"ast":4.8,"blk":0.9,"gp":78,"min":36.4,"pts":26,"reb":5.5,"stl":0.8,"thr":2.4},"Kevin Huerter":{"ast":2.6,"blk":0.6,"gp":44,"min":23.6,"pts":10.9,"reb":3.8,"stl":0.8,"thr":1.6},"Kevin Love":{"ast":1.8,"blk":0.2,"gp":37,"min":16.6,"pts":6.7,"reb":5.8,"stl":0.4,"thr":1.4},"Kevin Porter Jr.":{"ast":7.4,"blk":0.5,"gp":38,"min":33.2,"pts":17.4,"reb":5.2,"stl":2.2,"thr":1.2},"Kevon Looney":{"ast":1.6,"blk":0.5,"gp":21,"min":14.7,"pts":2.8,"reb":5.6,"stl":0.4,"thr":0.1},"Keyonte George":{"ast":6.1,"blk":0.3,"gp":54,"min":33.1,"pts":23.6,"reb":3.7,"stl":1.1,"thr":2.5},"Khaman Maluach":{"ast":0.1,"blk":0.7,"gp":46,"min":8.9,"pts":3,"reb":2.9,"stl":0.1,"thr":0.1},"Khris Middleton":{"ast":3.3,"blk":0.3,"gp":34,"min":24.3,"pts":10.3,"reb":3.9,"stl":0.8,"thr":1},"Killian Hayes":{"ast":3.5,"blk":0.2,"gp":23,"min":17.7,"pts":5.5,"reb":2.3,"stl":0.9,"thr":0.7},"Klay Thompson":{"ast":1.4,"blk":0.3,"gp":69,"min":21.7,"pts":11.7,"reb":2.1,"stl":0.5,"thr":2.9},"Kobe Brown":{"ast":0.8,"blk":0.1,"gp":34,"min":8.7,"pts":2.9,"reb":1.6,"stl":0.3,"thr":0.4},"Kobe Sanders":{"ast":1.6,"blk":0.1,"gp":68,"min":19.9,"pts":7.3,"reb":2.3,"stl":0.7,"thr":1.2},"Kon Knueppel":{"ast":3.4,"blk":0.2,"gp":81,"min":31.5,"pts":18.5,"reb":5.3,"stl":0.7,"thr":3.4},"Kris Dunn":{"ast":3.6,"blk":0.2,"gp":82,"min":27.2,"pts":7.3,"reb":3.3,"stl":1.6,"thr":1},"Kris Murray":{"ast":1.4,"blk":0.4,"gp":57,"min":23.4,"pts":5.8,"reb":3.6,"stl":0.9,"thr":0.6},"Kristaps Porzi\u0146\u0123is":{"ast":2.7,"blk":1.3,"gp":17,"min":24.3,"pts":17.1,"reb":5.1,"stl":0.5,"thr":1.8},"Kyle Anderson":{"ast":3.1,"blk":0.5,"gp":20,"min":20.1,"pts":7.1,"reb":3.3,"stl":1.2,"thr":0.2},"Kyle Filipowski":{"ast":2.6,"blk":0.5,"gp":77,"min":23.4,"pts":11.4,"reb":7.2,"stl":0.9,"thr":1},"Kyle Kuzma":{"ast":2.7,"blk":0.4,"gp":69,"min":26.2,"pts":13,"reb":4.5,"stl":0.7,"thr":1.2},"Kyle Lowry":{"ast":0.8,"blk":0.1,"gp":14,"min":8.4,"pts":1.2,"reb":0.6,"stl":0.1,"thr":0.3},"Kyshawn George":{"ast":4.5,"blk":0.9,"gp":48,"min":29,"pts":14.8,"reb":5.1,"stl":1,"thr":2.1},"LJ Cryer":{"ast":1,"blk":0,"gp":18,"min":16.2,"pts":8.2,"reb":1.6,"stl":0.2,"thr":2.1},"LaMelo Ball":{"ast":7.1,"blk":0.2,"gp":72,"min":28,"pts":20.1,"reb":4.8,"stl":1.2,"thr":3.8},"Lachlan Olbrich":{"ast":1.1,"blk":0.2,"gp":37,"min":9.3,"pts":2.4,"reb":3,"stl":0.3,"thr":0.1},"Landry Shamet":{"ast":1.4,"blk":0.2,"gp":51,"min":23,"pts":9.3,"reb":1.8,"stl":0.6,"thr":2},"Larry Nance Jr.":{"ast":1,"blk":0.2,"gp":35,"min":12.8,"pts":3.7,"reb":2.7,"stl":0.6,"thr":0.6},"Lauri Markkanen":{"ast":2.1,"blk":0.5,"gp":42,"min":34.4,"pts":26.7,"reb":6.9,"stl":1,"thr":2.7},"LeBron James":{"ast":7.2,"blk":0.6,"gp":60,"min":33.2,"pts":20.9,"reb":6.1,"stl":1.2,"thr":1.3},"Leaky Black":{"ast":1.5,"blk":0.5,"gp":15,"min":28.9,"pts":7.1,"reb":5,"stl":1.2,"thr":1.3},"Leonard Miller":{"ast":1.3,"blk":0.6,"gp":27,"min":23.1,"pts":11.7,"reb":5.8,"stl":0.5,"thr":1.1},"Liam McNeeley":{"ast":0.8,"blk":0.1,"gp":31,"min":11.9,"pts":4.3,"reb":2.4,"stl":0.2,"thr":0.8},"Lonzo Ball":{"ast":3.9,"blk":0.3,"gp":35,"min":20.8,"pts":4.6,"reb":4,"stl":1.3,"thr":1.1},"Luguentz Dort":{"ast":1.2,"blk":0.4,"gp":69,"min":26.8,"pts":8.3,"reb":3.6,"stl":0.9,"thr":1.9},"Luka Don\u010di\u0107":{"ast":8.3,"blk":0.5,"gp":64,"min":35.8,"pts":33.5,"reb":7.7,"stl":1.6,"thr":4},"Luka Garza":{"ast":1,"blk":0.4,"gp":69,"min":16.2,"pts":8.1,"reb":4.1,"stl":0.4,"thr":0.8},"Luke Kennard":{"ast":2.1,"blk":0.1,"gp":46,"min":20.5,"pts":7.9,"reb":2.2,"stl":0.7,"thr":1.6},"Luke Kornet":{"ast":1.9,"blk":1,"gp":68,"min":21,"pts":6.5,"reb":6.1,"stl":0.5,"thr":0},"Luke Travers":{"ast":0.8,"blk":0.3,"gp":12,"min":8.6,"pts":2.3,"reb":2,"stl":0.6,"thr":0.1},"Malachi Smith":{"ast":3.3,"blk":0.3,"gp":15,"min":23.9,"pts":8.3,"reb":3.4,"stl":0.8,"thr":1.3},"Malaki Branham":{"ast":0.8,"blk":0.1,"gp":28,"min":9.8,"pts":4.6,"reb":1.6,"stl":0.4,"thr":0.5},"Malevy Leons":{"ast":0.9,"blk":0.4,"gp":25,"min":11,"pts":3.3,"reb":2.1,"stl":0.6,"thr":0.2},"Malik Monk":{"ast":3,"blk":0.4,"gp":62,"min":22,"pts":12.5,"reb":1.9,"stl":0.6,"thr":2},"MarJon Beauchamp":{"ast":1.1,"blk":0.3,"gp":14,"min":14,"pts":6.8,"reb":2.3,"stl":0.6,"thr":1.1},"Marcus Sasser":{"ast":2,"blk":0.1,"gp":38,"min":12,"pts":5.2,"reb":1,"stl":0.5,"thr":1.2},"Marcus Smart":{"ast":3,"blk":0.4,"gp":62,"min":28.5,"pts":9.3,"reb":2.8,"stl":1.4,"thr":1.6},"Mark Williams":{"ast":1,"blk":0.9,"gp":60,"min":23.6,"pts":11.7,"reb":8,"stl":0.9,"thr":0},"Marvin Bagley III":{"ast":1.5,"blk":0.7,"gp":38,"min":19.2,"pts":10.1,"reb":5.7,"stl":0.5,"thr":0.2},"Mason Plumlee":{"ast":1.1,"blk":0,"gp":14,"min":8.9,"pts":1.9,"reb":2.9,"stl":0.4,"thr":0},"Matas Buzelis":{"ast":2.1,"blk":1.5,"gp":77,"min":29.2,"pts":16.3,"reb":5.8,"stl":0.7,"thr":2.2},"Matisse Thybulle":{"ast":0.9,"blk":0.5,"gp":30,"min":16,"pts":5.8,"reb":2,"stl":2,"thr":1.2},"Max Christie":{"ast":2,"blk":0.4,"gp":77,"min":29.1,"pts":12.3,"reb":3.2,"stl":0.6,"thr":2.3},"Max Strus":{"ast":2,"blk":0,"gp":12,"min":24,"pts":11.2,"reb":5.4,"stl":0.3,"thr":2.8},"Maxi Kleber":{"ast":0.6,"blk":0.3,"gp":43,"min":10.7,"pts":2,"reb":2,"stl":0.4,"thr":0.1},"Maxime Raynaud":{"ast":1.4,"blk":0.5,"gp":74,"min":26.5,"pts":12.5,"reb":7.5,"stl":0.5,"thr":0.3},"Micah Peavy":{"ast":1,"blk":0.1,"gp":61,"min":15,"pts":4.3,"reb":1.9,"stl":0.7,"thr":0.5},"Micah Potter":{"ast":1.5,"blk":0.3,"gp":47,"min":19.3,"pts":9.7,"reb":5,"stl":0.5,"thr":1.5},"Michael Porter Jr.":{"ast":3,"blk":0.3,"gp":52,"min":32.5,"pts":24.2,"reb":7.1,"stl":1.1,"thr":3.4},"Mikal Bridges":{"ast":3.7,"blk":0.8,"gp":82,"min":32.8,"pts":14.4,"reb":3.8,"stl":1.3,"thr":1.9},"Mike Conley":{"ast":2.9,"blk":0.3,"gp":54,"min":18.4,"pts":4.5,"reb":1.7,"stl":0.6,"thr":1},"Miles Bridges":{"ast":3.2,"blk":0.4,"gp":77,"min":31,"pts":17.1,"reb":5.8,"stl":0.6,"thr":1.9},"Miles Kelly":{"ast":0.9,"blk":0.1,"gp":14,"min":9.6,"pts":3.1,"reb":1.7,"stl":0.3,"thr":0.7},"Miles McBride":{"ast":2.6,"blk":0.2,"gp":41,"min":26.3,"pts":12,"reb":2.4,"stl":0.9,"thr":2.7},"Mitchell Robinson":{"ast":0.9,"blk":1.2,"gp":60,"min":19.6,"pts":5.7,"reb":8.8,"stl":0.9,"thr":0},"Mohamed Diawara":{"ast":0.8,"blk":0.1,"gp":69,"min":9.2,"pts":3.6,"reb":1.4,"stl":0.2,"thr":0.7},"Moritz Wagner":{"ast":0.8,"blk":0.1,"gp":36,"min":11.9,"pts":6.9,"reb":3.2,"stl":0.4,"thr":0.6},"Moses Moody":{"ast":1.6,"blk":0.6,"gp":60,"min":25.7,"pts":12.1,"reb":3.3,"stl":1,"thr":2.5},"Mouhamed Gueye":{"ast":0.9,"blk":0.5,"gp":77,"min":15.3,"pts":4.4,"reb":3.6,"stl":0.8,"thr":0.5},"Moussa Ciss\u00e9":{"ast":0.2,"blk":1.2,"gp":38,"min":13.9,"pts":4.5,"reb":5.7,"stl":0.5,"thr":0},"Moussa Diabat\u00e9":{"ast":1.9,"blk":1,"gp":73,"min":26,"pts":7.9,"reb":8.7,"stl":0.8,"thr":0},"Myles Turner":{"ast":1.5,"blk":1.6,"gp":71,"min":26.9,"pts":11.9,"reb":5.3,"stl":0.7,"thr":2.1},"Myron Gardner":{"ast":1,"blk":0.2,"gp":45,"min":9.1,"pts":3.6,"reb":2.7,"stl":0.4,"thr":0.6},"Nae'Qwan Tomlin":{"ast":0.8,"blk":0.5,"gp":64,"min":15.7,"pts":5.8,"reb":2.9,"stl":0.6,"thr":0.4},"Naji Marshall":{"ast":3.3,"blk":0.1,"gp":74,"min":29.5,"pts":15.2,"reb":4.7,"stl":1.1,"thr":0.8},"Nate Williams":{"ast":1,"blk":0,"gp":14,"min":17.1,"pts":8,"reb":2.1,"stl":0.4,"thr":0.9},"Naz Reid":{"ast":2.2,"blk":1,"gp":77,"min":26.1,"pts":13.6,"reb":6.2,"stl":1,"thr":2.1},"Neemias Queta":{"ast":1.7,"blk":1.3,"gp":76,"min":25.3,"pts":10.2,"reb":8.4,"stl":0.8,"thr":0},"Nic Claxton":{"ast":3.7,"blk":1.1,"gp":69,"min":27.8,"pts":11.7,"reb":6.9,"stl":0.7,"thr":0},"Nick Richards":{"ast":0.3,"blk":0.5,"gp":28,"min":9.1,"pts":3.2,"reb":3.3,"stl":0.1,"thr":0},"Nick Smith Jr.":{"ast":1,"blk":0.1,"gp":30,"min":12.5,"pts":6.2,"reb":0.8,"stl":0.3,"thr":1.1},"Nickeil Alexander-Walker":{"ast":3.7,"blk":0.5,"gp":78,"min":33.4,"pts":20.8,"reb":3.4,"stl":1.3,"thr":3.2},"Nicolas Batum":{"ast":0.9,"blk":0.3,"gp":74,"min":17.5,"pts":4,"reb":2.5,"stl":0.6,"thr":1.2},"Nikola Joki\u0107":{"ast":10.7,"blk":0.8,"gp":65,"min":34.8,"pts":27.7,"reb":12.9,"stl":1.4,"thr":1.7},"Nikola Jovi\u0107":{"ast":2.2,"blk":0.4,"gp":47,"min":17.2,"pts":7.3,"reb":3.3,"stl":0.6,"thr":1},"Nikola Topi\u0107":{"ast":4.4,"blk":0,"gp":10,"min":16,"pts":5.2,"reb":1.9,"stl":0.5,"thr":0.6},"Nikola Vu\u010devi\u0107":{"ast":3.8,"blk":0.6,"gp":48,"min":30.8,"pts":16.9,"reb":9,"stl":0.7,"thr":1.7},"Nique Clifford":{"ast":2.4,"blk":0.3,"gp":75,"min":25.1,"pts":8.6,"reb":3.8,"stl":0.9,"thr":1},"Noah Clowney":{"ast":1.6,"blk":0.7,"gp":66,"min":27,"pts":12.3,"reb":4.1,"stl":0.8,"thr":2},"Noah Penda":{"ast":1.2,"blk":0.3,"gp":59,"min":12.8,"pts":3.8,"reb":3.2,"stl":0.5,"thr":0.5},"Nolan Traore":{"ast":3.8,"blk":0.4,"gp":56,"min":22.2,"pts":8.9,"reb":1.8,"stl":0.8,"thr":1.1},"Norman Powell":{"ast":2.5,"blk":0.2,"gp":58,"min":29.6,"pts":21.7,"reb":3.5,"stl":1.1,"thr":2.7},"OG Anunoby":{"ast":2.2,"blk":0.7,"gp":67,"min":33.2,"pts":16.7,"reb":5.2,"stl":1.6,"thr":2.3},"Obi Toppin":{"ast":2.3,"blk":0,"gp":24,"min":17.7,"pts":11.6,"reb":4.4,"stl":0.5,"thr":1.5},"Ochai Agbaji":{"ast":0.7,"blk":0.3,"gp":42,"min":15.5,"pts":4.3,"reb":2.3,"stl":0.4,"thr":0.3},"Olivier-Maxence Prosper":{"ast":1,"blk":0.2,"gp":53,"min":18.6,"pts":10,"reb":3.5,"stl":0.8,"thr":1.1},"Onyeka Okongwu":{"ast":3.1,"blk":1.1,"gp":74,"min":31,"pts":15.2,"reb":7.6,"stl":1.1,"thr":1.9},"Oscar Tshiebwe":{"ast":1.2,"blk":0.2,"gp":27,"min":16.6,"pts":7.8,"reb":6.6,"stl":0.6,"thr":0},"Oso Ighodaro":{"ast":2.3,"blk":0.7,"gp":82,"min":22,"pts":6.5,"reb":5.1,"stl":0.9,"thr":0},"Ousmane Dieng":{"ast":3.6,"blk":0.3,"gp":30,"min":26.8,"pts":11,"reb":4.6,"stl":0.8,"thr":1.5},"P.J. Washington":{"ast":1.8,"blk":1.1,"gp":56,"min":31,"pts":14.2,"reb":7,"stl":1,"thr":1.4},"PJ Hall":{"ast":0.7,"blk":0.7,"gp":12,"min":15.6,"pts":6.1,"reb":5.5,"stl":0.3,"thr":0.2},"Paolo Banchero":{"ast":5.2,"blk":0.6,"gp":72,"min":34.8,"pts":22.2,"reb":8.4,"stl":0.7,"thr":1.2},"Pascal Siakam":{"ast":3.8,"blk":0.4,"gp":62,"min":33.2,"pts":24,"reb":6.6,"stl":1.1,"thr":1.7},"Pat Spencer":{"ast":3.5,"blk":0.1,"gp":66,"min":18.6,"pts":7.2,"reb":2.4,"stl":0.7,"thr":0.9},"Patrick Williams":{"ast":1.5,"blk":0.4,"gp":72,"min":20.5,"pts":7,"reb":3,"stl":0.7,"thr":1.3},"Paul George":{"ast":3.6,"blk":0.4,"gp":37,"min":30.7,"pts":17.3,"reb":5.3,"stl":1.7,"thr":2.7},"Paul Reed":{"ast":1.2,"blk":0.9,"gp":65,"min":13.9,"pts":7.8,"reb":4.5,"stl":0.9,"thr":0.2},"Payton Pritchard":{"ast":5.2,"blk":0.1,"gp":79,"min":32.4,"pts":17,"reb":3.9,"stl":0.7,"thr":2.7},"Pelle Larsson":{"ast":3.4,"blk":0.2,"gp":70,"min":26.4,"pts":11.4,"reb":3.5,"stl":0.7,"thr":0.9},"Pete Nance":{"ast":1,"blk":0.3,"gp":47,"min":15.7,"pts":5.4,"reb":2.7,"stl":0.3,"thr":1.1},"Peyton Watson":{"ast":2.1,"blk":1.1,"gp":54,"min":29.6,"pts":14.6,"reb":4.9,"stl":0.9,"thr":1.5},"Precious Achiuwa":{"ast":1.4,"blk":0.7,"gp":73,"min":23.9,"pts":10.1,"reb":6.7,"stl":0.9,"thr":0.4},"Quentin Grimes":{"ast":3.3,"blk":0.4,"gp":75,"min":29.4,"pts":13.4,"reb":3.6,"stl":0.9,"thr":1.7},"Quenton Jackson":{"ast":2.9,"blk":0.1,"gp":49,"min":18.3,"pts":9.1,"reb":2.3,"stl":0.6,"thr":0.9},"Quinten Post":{"ast":1.4,"blk":0.5,"gp":67,"min":17.3,"pts":7.7,"reb":4,"stl":0.4,"thr":1.4},"RJ Barrett":{"ast":3.3,"blk":0.3,"gp":57,"min":30.3,"pts":19.3,"reb":5.3,"stl":0.7,"thr":1.7},"Rasheer Fleming":{"ast":0.3,"blk":0.4,"gp":55,"min":12.2,"pts":4.3,"reb":2.3,"stl":0.4,"thr":0.8},"RayJ Dennis":{"ast":2,"blk":0.3,"gp":13,"min":12.9,"pts":4.9,"reb":1.6,"stl":0.2,"thr":0.9},"Rayan Rupert":{"ast":0.7,"blk":0,"gp":48,"min":12,"pts":2.9,"reb":1.8,"stl":0.6,"thr":0.5},"Reed Sheppard":{"ast":3.4,"blk":0.7,"gp":82,"min":26.2,"pts":13.5,"reb":2.9,"stl":1.5,"thr":2.8},"Rob Dillingham":{"ast":1.7,"blk":0.1,"gp":35,"min":9.3,"pts":3.5,"reb":1.2,"stl":0.5,"thr":0.3},"Robert Williams":{"ast":1,"blk":1.5,"gp":59,"min":17.1,"pts":6.7,"reb":7,"stl":0.6,"thr":0.2},"Ron Harper Jr.":{"ast":0.8,"blk":0.3,"gp":29,"min":11,"pts":4.2,"reb":1.7,"stl":0.3,"thr":1},"Ron Holland":{"ast":1.2,"blk":0.3,"gp":78,"min":19.9,"pts":8.2,"reb":4,"stl":1.2,"thr":0.6},"Royce O'Neale":{"ast":2.7,"blk":0.4,"gp":78,"min":28.4,"pts":9.8,"reb":4.8,"stl":1.1,"thr":2.7},"Rudy Gobert":{"ast":1.7,"blk":1.6,"gp":76,"min":31.3,"pts":10.9,"reb":11.5,"stl":0.8,"thr":0},"Rui Hachimura":{"ast":0.8,"blk":0.3,"gp":68,"min":28.3,"pts":11.5,"reb":3.3,"stl":0.6,"thr":1.7},"Russell Westbrook":{"ast":6.7,"blk":0.2,"gp":64,"min":29,"pts":15.2,"reb":5.4,"stl":1.3,"thr":2},"Ryan Dunn":{"ast":1.5,"blk":0.4,"gp":70,"min":19.4,"pts":5.8,"reb":4.2,"stl":0.9,"thr":0.8},"Ryan Kalkbrenner":{"ast":0.8,"blk":1.5,"gp":69,"min":21.4,"pts":7.6,"reb":5.5,"stl":0.5,"thr":0},"Ryan Nembhard":{"ast":5.3,"blk":0,"gp":60,"min":19.5,"pts":6.6,"reb":2.2,"stl":0.4,"thr":0.8},"Ryan Rollins":{"ast":5.6,"blk":0.4,"gp":74,"min":32.1,"pts":17.3,"reb":4.6,"stl":1.5,"thr":2.5},"Saddiq Bey":{"ast":2.5,"blk":0.1,"gp":72,"min":31.2,"pts":17.7,"reb":5.6,"stl":0.9,"thr":2.1},"Sam Hauser":{"ast":1.5,"blk":0.3,"gp":78,"min":24.8,"pts":9.2,"reb":3.8,"stl":0.5,"thr":2.5},"Sam Merrill":{"ast":2.4,"blk":0.1,"gp":52,"min":26.5,"pts":12.8,"reb":2.6,"stl":0.6,"thr":3},"Sandro Mamukelashvili":{"ast":1.9,"blk":0.5,"gp":80,"min":21.9,"pts":11.2,"reb":4.9,"stl":0.8,"thr":1.4},"Santi Aldama":{"ast":2.9,"blk":0.7,"gp":43,"min":27.9,"pts":14,"reb":6.7,"stl":0.9,"thr":1.6},"Scoot Henderson":{"ast":3.7,"blk":0.3,"gp":30,"min":24.9,"pts":14.2,"reb":2.7,"stl":0.9,"thr":1.9},"Scottie Barnes":{"ast":5.9,"blk":1.5,"gp":80,"min":33.5,"pts":18.1,"reb":7.5,"stl":1.4,"thr":0.9},"Scotty Pippen Jr.":{"ast":4.7,"blk":0.4,"gp":10,"min":21.2,"pts":11.4,"reb":2.2,"stl":1.9,"thr":1},"Seth Curry":{"ast":1,"blk":0,"gp":10,"min":13.3,"pts":7.1,"reb":1.2,"stl":0.3,"thr":1.2},"Shaedon Sharpe":{"ast":2.6,"blk":0.1,"gp":50,"min":29.4,"pts":20.8,"reb":4.3,"stl":1.4,"thr":2.1},"Shai Gilgeous-Alexander":{"ast":6.6,"blk":0.8,"gp":68,"min":33.2,"pts":31.1,"reb":4.3,"stl":1.4,"thr":1.7},"Sharife Cooper":{"ast":3,"blk":0.1,"gp":41,"min":17.1,"pts":8.1,"reb":2.1,"stl":0.4,"thr":0.7},"Sidy Cissoko":{"ast":1.5,"blk":0.3,"gp":75,"min":19.1,"pts":5.1,"reb":2.2,"stl":0.7,"thr":0.8},"Simone Fontecchio":{"ast":1.4,"blk":0.1,"gp":70,"min":16.8,"pts":8.5,"reb":3,"stl":0.5,"thr":1.8},"Sion James":{"ast":2,"blk":0.2,"gp":82,"min":22.5,"pts":5.4,"reb":3.5,"stl":0.6,"thr":1},"Spencer Jones":{"ast":0.8,"blk":0.5,"gp":64,"min":22.1,"pts":5.5,"reb":3.3,"stl":0.8,"thr":0.9},"Stephen Curry":{"ast":4.7,"blk":0.4,"gp":43,"min":30.9,"pts":26.6,"reb":3.6,"stl":1.1,"thr":4.4},"Stephon Castle":{"ast":7.4,"blk":0.3,"gp":68,"min":30,"pts":16.7,"reb":5.3,"stl":1.1,"thr":1.2},"Steven Adams":{"ast":1.5,"blk":0.6,"gp":32,"min":22.8,"pts":5.8,"reb":8.6,"stl":0.7,"thr":0},"Svi Mykhailiuk":{"ast":1.9,"blk":0.1,"gp":50,"min":23.1,"pts":9.4,"reb":2.5,"stl":0.5,"thr":2},"T.J. McConnell":{"ast":5.1,"blk":0.2,"gp":56,"min":17.2,"pts":9.4,"reb":2.2,"stl":1,"thr":0.3},"Taelon Peter":{"ast":1.1,"blk":0.1,"gp":38,"min":12.9,"pts":4.5,"reb":1.6,"stl":0.7,"thr":1.1},"Taj Gibson":{"ast":0.6,"blk":0.1,"gp":10,"min":9.7,"pts":3.4,"reb":2.7,"stl":0.2,"thr":0.3},"Tari Eason":{"ast":1.5,"blk":0.5,"gp":60,"min":25.8,"pts":10.5,"reb":6.3,"stl":1.2,"thr":1.6},"Taurean Prince":{"ast":1.8,"blk":0.2,"gp":26,"min":23.5,"pts":9.2,"reb":3.1,"stl":0.6,"thr":2.3},"Taylor Hendricks":{"ast":0.7,"blk":0.2,"gp":33,"min":14.9,"pts":4.9,"reb":3,"stl":0.4,"thr":0.7},"Terance Mann":{"ast":3,"blk":0.2,"gp":63,"min":24.3,"pts":7.2,"reb":3.2,"stl":0.7,"thr":0.9},"Terrence Shannon Jr.":{"ast":0.9,"blk":0,"gp":43,"min":12.5,"pts":5.6,"reb":1.1,"stl":0.3,"thr":0.7},"Thomas Bryant":{"ast":0.6,"blk":0.4,"gp":60,"min":12.2,"pts":6.2,"reb":3.4,"stl":0.3,"thr":0.8},"Tidjane Sala\u00fcn":{"ast":0.7,"blk":0.2,"gp":37,"min":15.5,"pts":6,"reb":4,"stl":0.4,"thr":1},"Tim Hardaway Jr.":{"ast":1.4,"blk":0.1,"gp":80,"min":26.6,"pts":13.5,"reb":2.6,"stl":0.5,"thr":2.8},"Tobias Harris":{"ast":2.5,"blk":0.4,"gp":63,"min":27.7,"pts":13.3,"reb":5.1,"stl":0.9,"thr":1.3},"Tolu Smith":{"ast":0.9,"blk":0.4,"gp":15,"min":9.1,"pts":3.7,"reb":3.3,"stl":0.1,"thr":0},"Tony Bradley":{"ast":0.5,"blk":0.2,"gp":38,"min":10.9,"pts":4,"reb":2.8,"stl":0.2,"thr":0},"Toumani Camara":{"ast":2.5,"blk":0.4,"gp":82,"min":33.3,"pts":13.4,"reb":5.1,"stl":1.1,"thr":2.7},"Trae Young":{"ast":8.9,"blk":0.1,"gp":10,"min":28,"pts":19.3,"reb":1.5,"stl":1,"thr":1.8},"Trayce Jackson-Davis":{"ast":0.9,"blk":0.5,"gp":36,"min":11.4,"pts":4.2,"reb":3.1,"stl":0.4,"thr":0},"Tre Johnson":{"ast":2,"blk":0.3,"gp":60,"min":24.1,"pts":12.2,"reb":2.8,"stl":0.6,"thr":1.9},"Tre Jones":{"ast":5.4,"blk":0.2,"gp":65,"min":27,"pts":14.1,"reb":3.1,"stl":1.2,"thr":0.6},"Tre Mann":{"ast":1.6,"blk":0.1,"gp":53,"min":12.6,"pts":5.5,"reb":1.7,"stl":0.5,"thr":0.9},"Trendon Watford":{"ast":2.5,"blk":0.4,"gp":53,"min":16.3,"pts":6.5,"reb":3.3,"stl":0.3,"thr":0.2},"Trey Murphy III":{"ast":3.8,"blk":0.4,"gp":66,"min":35.5,"pts":21.5,"reb":5.7,"stl":1.5,"thr":3.2},"Tristan Da Silva":{"ast":1.6,"blk":0.3,"gp":77,"min":24.7,"pts":9.9,"reb":3.7,"stl":0.9,"thr":1.6},"Tristan Vukcevic":{"ast":1.1,"blk":0.7,"gp":49,"min":13.7,"pts":9,"reb":3,"stl":0.5,"thr":1},"Ty Jerome":{"ast":5.7,"blk":0.3,"gp":15,"min":22.6,"pts":19.7,"reb":2.8,"stl":1.1,"thr":2.8},"Tyler Burton":{"ast":1,"blk":0.3,"gp":12,"min":25.6,"pts":10.8,"reb":4.2,"stl":0.9,"thr":1.8},"Tyler Herro":{"ast":4.1,"blk":0.4,"gp":33,"min":31.3,"pts":20.5,"reb":4.8,"stl":0.7,"thr":2.5},"Tyler Kolek":{"ast":2.7,"blk":0.1,"gp":62,"min":11.7,"pts":4.4,"reb":1.6,"stl":0.4,"thr":0.7},"Tyler Smith":{"ast":0.4,"blk":0,"gp":12,"min":13.8,"pts":4.7,"reb":2.8,"stl":0.5,"thr":0.8},"Tyrese Martin":{"ast":1.9,"blk":0.1,"gp":37,"min":18.8,"pts":7.3,"reb":2.9,"stl":0.6,"thr":1.4},"Tyrese Maxey":{"ast":6.6,"blk":0.8,"gp":70,"min":38,"pts":28.3,"reb":4.1,"stl":1.9,"thr":3.1},"Tyrese Proctor":{"ast":1.5,"blk":0,"gp":50,"min":10.9,"pts":5.4,"reb":1.3,"stl":0.5,"thr":0.9},"Tyson Etienne":{"ast":1.7,"blk":0,"gp":24,"min":15.8,"pts":7.9,"reb":1.1,"stl":0.5,"thr":1.8},"Tyus Jones":{"ast":2.4,"blk":0.1,"gp":48,"min":15.7,"pts":3,"reb":1.1,"stl":0.7,"thr":0.6},"VJ Edgecombe":{"ast":4.2,"blk":0.5,"gp":75,"min":35,"pts":16,"reb":5.6,"stl":1.4,"thr":2},"Victor Wembanyama":{"ast":3.1,"blk":3.1,"gp":64,"min":29.2,"pts":25,"reb":11.5,"stl":1,"thr":1.9},"Vince Williams Jr.":{"ast":4.4,"blk":0.4,"gp":34,"min":21.6,"pts":8,"reb":4,"stl":0.8,"thr":1.4},"V\u00edt Krej\u010d\u00ed":{"ast":1.5,"blk":0.3,"gp":46,"min":22.3,"pts":9,"reb":2.1,"stl":0.8,"thr":2.2},"Walter Clayton":{"ast":3.2,"blk":0.3,"gp":45,"min":18,"pts":6.8,"reb":2,"stl":0.5,"thr":1},"Wendell Carter Jr.":{"ast":2,"blk":0.6,"gp":78,"min":29.3,"pts":11.8,"reb":7.4,"stl":0.8,"thr":0.9},"Will Richard":{"ast":1.3,"blk":0.1,"gp":69,"min":20,"pts":6.4,"reb":2.5,"stl":1.2,"thr":1},"Will Riley":{"ast":2,"blk":0.1,"gp":74,"min":22.1,"pts":10.3,"reb":2.9,"stl":0.7,"thr":1.1},"Yanic Konan Niederh\u00e4user":{"ast":0.3,"blk":0.7,"gp":41,"min":10.3,"pts":4.3,"reb":2.9,"stl":0.1,"thr":0},"Yuki Kawamura":{"ast":2.6,"blk":0,"gp":18,"min":11.6,"pts":3.4,"reb":1.8,"stl":0.5,"thr":0.6},"Yves Missi":{"ast":1.3,"blk":1.5,"gp":66,"min":19.7,"pts":5.7,"reb":5.8,"stl":0.3,"thr":0},"Zaccharie Risacher":{"ast":1.1,"blk":0.5,"gp":67,"min":22.4,"pts":9.6,"reb":3.8,"stl":0.9,"thr":1.4},"Zach Collins":{"ast":1.5,"blk":0.4,"gp":10,"min":18.4,"pts":9.7,"reb":5.6,"stl":0.2,"thr":0.9},"Zach Edey":{"ast":1.1,"blk":1.9,"gp":11,"min":25.8,"pts":13.6,"reb":11.1,"stl":0.6,"thr":0.1},"Zach LaVine":{"ast":2.3,"blk":0.3,"gp":39,"min":31.4,"pts":19.2,"reb":2.8,"stl":0.7,"thr":2.5},"Zeke Nnaji":{"ast":0.6,"blk":0.5,"gp":52,"min":12,"pts":3.7,"reb":2.6,"stl":0.3,"thr":0.3},"Ziaire Williams":{"ast":1.1,"blk":0.4,"gp":56,"min":22.9,"pts":10.2,"reb":2.4,"stl":1.4,"thr":1.5},"Zion Williamson":{"ast":3.2,"blk":0.5,"gp":62,"min":29.7,"pts":21,"reb":5.7,"stl":1,"thr":0}},"season":"2025-26"}}'''

if __name__ == '__main__':
    main()

