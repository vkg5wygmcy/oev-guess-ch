# -*- coding: utf-8 -*-
"""HRDF-Fahrplan der Schweiz -> kompakte JSON-Dateien je Haltestellengruppe.
Aufruf in drei Stufen: parse, sort, shards (je ein Prozess)."""
import datetime, json, os, re, subprocess, sys

HR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HR, 'build')
os.makedirs(os.path.join(OUT, 'fp'), exist_ok=True)
STAGE = sys.argv[1] if len(sys.argv) > 1 else 'parse'
TMP = os.path.join(OUT, 'deps.tsv')
def log(*a): print(*a, flush=True)

B36 = '0123456789abcdefghijklmnopqrstuvwxyz'
def b36(n):
    n = int(n)
    if n <= 0: return '0'
    s = ''
    while n: n, r = divmod(n, 36); s = B36[r] + s
    return s

def rd(name): return open(os.path.join(HR, name), encoding='latin-1')

# ---------------------------------------------------------------- parse
if STAGE == 'parse':
    LINIE = {}
    for line in rd('LINIE'):
        m = re.match(r'^(\d{7}) N T (.*)$', line.rstrip('\n'))
        if m: LINIE['#' + m.group(1)] = m.group(2).strip()

    GDEF = {}
    for line in rd('GLEISE_WGS'):
        m = re.match(r"^(\d{7}) (#\d+) ([GA]) '(.*)'", line.rstrip())
        if m: GDEF.setdefault((m.group(1), m.group(2)), {})[m.group(3)] = m.group(4)
    GLEIS = {}
    for line in rd('GLEISE_WGS'):
        if len(line) < 30 or line[22] != '#': continue
        d = GDEF.get((line[0:7], line[22:30].strip()))
        if not d: continue
        txt = (d.get('G', '') + (' ' + d['A'] if d.get('A') else '')).strip()
        if txt: GLEIS.setdefault((line[0:7], line[8:14], line[15:21]), txt)
    del GDEF
    log('Gleise', len(GLEIS))

    fo = open(TMP, 'w', encoding='utf-8')
    ntrip = ndep = 0
    def flush(t):
        global ndep
        st = t['stops']
        if len(st) < 2: return
        gat, lin = t['gat'], t['lin']
        if lin.startswith('#'): lin = LINIE.get(lin, '')
        if lin and gat and lin.upper().startswith(gat.upper()): label = lin
        elif lin and gat in ('B', 'T', 'BUS', 'NFB', 'NFT', 'TRO', 'M'): label = gat + ' ' + lin
        else: label = (gat + lin) if lin else gat
        dest = st[-1][0]
        for i in range(len(st) - 1):
            s0, abf = st[i]
            if abf is None: continue
            via = ' '.join(b36(x[0]) for x in st[i + 1:i + 4] if x[0] != dest)
            gl = GLEIS.get((s0, t['fz'], t['vw']), '')
            fo.write('%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' % (
                s0, b36(abf), label, b36(dest), gl, t['bf'], t['vw'], via))
            ndep += 1

    t = None
    for line in rd('FPLAN'):
        c0 = line[0]
        if c0 == '*':
            k = line[1:3]
            if k == 'Z ':
                if t: flush(t)
                p = line.split()
                t = {'fz': p[1], 'vw': p[2], 'gat': '', 'lin': '', 'bf': '000000', 'stops': []}
                ntrip += 1
            elif t is None: continue
            elif k == 'G ': t['gat'] = line[3:6].strip()
            elif k == 'L ': t['lin'] = line[3:11].strip()
            elif k == 'A ' and line[3:5] == 'VE' and t['bf'] == '000000':
                p = line.split()
                if len(p) >= 5 and p[4].isdigit(): t['bf'] = p[4]
        elif t is not None and c0.isdigit():
            an = line[29:35].strip(); a = line[36:42].strip()
            if not an and not a: continue                    # Durchfahrt ohne Halt
            if an.startswith('-') and (not a or a.startswith('-')): continue
            abf = (int(a) // 100) * 60 + int(a) % 100 if a.isdigit() else None   # '-' = kein Einstieg
            t['stops'].append((line[0:7], abf))
    if t: flush(t)
    fo.close()
    log('Fahrten', ntrip, 'Abfahrten', ndep)

# ----------------------------------------------------------------- sort
elif STAGE == 'sort':
    subprocess.run(['sort', '-S', '700M', '-t', '\t', '-k1,1', '-s', '-o', TMP + '.s', TMP],
                   check=True, env=dict(os.environ, LC_ALL='C'))
    os.replace(TMP + '.s', TMP)
    log('sortiert')

# --------------------------------------------------------------- shards
elif STAGE == 'shards':
    def u(x):
        try: return x.encode('latin-1').decode('utf-8')
        except UnicodeError: return x
    ec = rd('ECKDATEN').read().split('\n')
    d0 = datetime.datetime.strptime(ec[0].strip(), '%d.%m.%Y').date()
    d1 = datetime.datetime.strptime(ec[1].strip(), '%d.%m.%Y').date()
    ND = (d1 - d0).days + 1
    WDAY = [(d0 + datetime.timedelta(days=i)).weekday() for i in range(ND)]
    FT = []
    for line in rd('FEIERTAG'):
        try:
            i = (datetime.datetime.strptime(line[:10], '%d.%m.%Y').date() - d0).days
            if 0 <= i < ND: FT.append(i)
        except ValueError: pass
    FTS = set(FT)
    WMASK = [0] * 7
    for i in range(ND):
        if i not in FTS: WMASK[WDAY[i]] |= 1 << i
    HMASK = 0
    for i in FT: HMASK |= 1 << i

    BITS = {'000000': (1 << ND) - 1}
    for line in rd('BITFELD'):
        line = line.rstrip('\n')
        if len(line) < 10: continue
        hx = line[7:].strip()
        b = bin(int(hx, 16))[2:].zfill(len(hx) * 4)[2:2 + ND]
        BITS[line[:6]] = int(b[::-1], 2)          # Bit i = Tag i

    def pattern(v):
        cnt = [bin(v & WMASK[w]).count('1') for w in range(7)]
        tot = [bin(WMASK[w]).count('1') for w in range(7)]
        mx = max(cnt)
        mask = 0
        for w in range(7):
            if mx and cnt[w] * 2 >= mx: mask |= 1 << w
        full = all(cnt[w] in (0, tot[w]) for w in range(7))
        hc = bin(v & HMASK).count('1')
        hol = 1 if FT and hc * 2 >= len(FT) else 0
        days = bin(v).count('1')
        return mask | (int(full) << 7) | (hol << 8), days

    NAMES, COORD = {}, {}
    for line in rd('BAHNHOF'):
        if len(line) > 12: NAMES[line[:7]] = u(line[12:].split('$')[0].strip())
    for line in rd('BFKOORD_WGS'):
        try: COORD[line[:7]] = (round(float(line[19:29]), 5), round(float(line[8:18]), 5))
        except ValueError: pass
    OPN, KN = {}, {}
    for line in rd('BETRIEB_DE'):
        line = u(line.strip())
        m = re.match(r'^(\d+)\s+K\s+(.*)$', line)
        if m:
            q = re.findall(r'"([^"]*)"', m.group(2))
            KN[m.group(1)] = q[-1] if q else m.group(2)
        m = re.match(r'^(\d+)\s+:\s+(.*)$', line)
        if m:
            for v in m.group(2).split(): OPN[v] = KN.get(m.group(1), v)

    MIN_DAYS = 14
    REF = set()
    nfiles = 0; nkept = 0; ndrop = 0
    def write(sh, data):
        global nfiles, nkept, ndrop
        if not data: return
        L, T, O, out = {}, {}, {}, {}
        for st, deps in data.items():
            grp = {}
            for f in deps:
                k = (f[1], f[2], f[3])
                g = grp.get(k)
                bits = BITS.get(f[5], BITS['000000'])
                if g is None:
                    grp[k] = g = {'v': 0, 'tr': {}, 'op': f[6], 'via': f[7] if len(f) > 7 else ''}
                g['v'] |= bits
                n = bin(bits).count('1')
                g['tr'][f[4]] = g['tr'].get(f[4], 0) + n
                if len(f) > 7 and len(f[7]) > len(g['via']): g['via'] = f[7]
            enc = []
            for (mn, lab, dest), g in sorted(grp.items(), key=lambda kv: int(kv[0][0], 36)):
                pat, days = pattern(g['v'])
                if days < MIN_DAYS: ndrop += 1; continue
                tr = max(g['tr'].items(), key=lambda kv: kv[1])[0] if g['tr'] else ''
                REF.add(dest)
                for x in g['via'].split(): REF.add(x)
                enc.append('%s,%s,%s,%s,%s,%s,%s' % (
                    mn, b36(L.setdefault(u(lab), len(L))), dest, b36(T.setdefault(u(tr), len(T))),
                    b36(pat), b36(O.setdefault(g['op'], len(O))), g['via']))
                nkept += 1
            if enc:
                out[st] = ';'.join(enc); REF.add(b36(int(st)))
        inv = lambda d: [k for k, _ in sorted(d.items(), key=lambda kv: kv[1])]
        js = {'s': out, 'L': inv(L), 'T': inv(T), 'O': [OPN.get(v, v) for v in inv(O)]}
        with open(os.path.join(OUT, 'fp', sh + '.json'), 'w', encoding='utf-8') as fh:
            json.dump(js, fh, ensure_ascii=False, separators=(',', ':'))
        nfiles += 1

    for fn in os.listdir(os.path.join(OUT, 'fp')): os.remove(os.path.join(OUT, 'fp', fn))
    cur, data = None, {}
    for line in open(TMP, encoding='utf-8'):
        f = line.rstrip('\n').split('\t')
        sh = f[0][:5]
        if sh != cur:
            write(cur, data); cur, data = sh, {}
        data.setdefault(f[0], []).append(f)
    write(cur, data)
    log('Dateien', nfiles, 'Abfahrten', nkept, 'verworfen (<%d Tage)' % MIN_DAYS, ndrop)

    with open(os.path.join(OUT, 'meta.json'), 'w', encoding='utf-8') as fh:
        json.dump({'from': str(d0), 'to': str(d1), 'title': u(ec[2].split('$')[0]) if len(ec) > 2 else ''},
                  fh, ensure_ascii=False, separators=(',', ':'))
    idx = {}
    for st, nm in NAMES.items():
        k = b36(int(st))
        if k in REF and st in COORD:
            has = os.path.exists(os.path.join(OUT, 'fp', st[:5] + '.json'))
            idx[k] = [nm, COORD[st][0], COORD[st][1]]
        elif k in REF:
            idx[k] = [nm]
    with open(os.path.join(OUT, 'stops.json'), 'w', encoding='utf-8') as fh:
        json.dump(idx, fh, ensure_ascii=False, separators=(',', ':'))
    log('Haltestellen im Index', len(idx))
