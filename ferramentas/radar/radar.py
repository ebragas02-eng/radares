#!/usr/bin/env python3
"""Radar da Arizona: separa as empresas novas do mês (base aberta do CNPJ da Receita) e acha o e-mail
de quem pediu marca sem procurador e recebeu despacho na RPI. Manda tudo à Arizona pelo token OIDC do GitHub.

Uso:
  python3 radar.py leads PASTA_DOS_ZIPS AAAAMM [--teste]
  python3 radar.py risco PASTA_DOS_ZIPS casos.csv [--teste]

Regras (decisões do Emerson, 14/11/2026): só SP, só ativas, só matriz, sem MEI, só empresas (natureza 2xxx),
contato direto com o empresário: e-mail que aparece em 2 ou mais empresas novas é de contabilidade e fica de fora.
O log mostra só contagens; nomes e e-mails vão direto à Arizona, sem passar por arquivo nem pelo log."""
import collections, csv, glob, io, json, os, re, sys, unicodedata, urllib.request, zipfile

API = os.environ.get('VIG_API', 'https://painel.arizonamarcas.com.br')
EMAIL = re.compile(r'^[a-z0-9._%+-]{1,64}@[a-z0-9.-]{1,190}\.[a-z]{2,24}$')


def linhas(nome):
    with zipfile.ZipFile(nome) as z:
        for n in z.namelist():
            with z.open(n) as f:
                for r in csv.reader(io.TextIOWrapper(f, encoding='latin-1', newline=''), delimiter=';', quotechar='"'):
                    yield r


def arquivos(pasta, prefixo):
    return sorted(glob.glob(os.path.join(pasta, prefixo + '*.zip')))


def norm(s):
    s = unicodedata.normalize('NFD', s or '').encode('ascii', 'ignore').decode().upper()
    s = re.sub(r'[^A-Z0-9 ]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def titulo(s):
    pequenas = {'DE', 'DA', 'DO', 'DAS', 'DOS', 'E'}
    return ' '.join(w.capitalize() if w not in pequenas else w.lower() for w in (s or '').split())


def mei_de(pasta, basicos):
    mei = set()
    for r in linhas(os.path.join(pasta, 'Simples.zip')):
        if r[0] in basicos and len(r) > 4 and r[4] == 'S':
            mei.add(r[0])
    return mei


def municipios(pasta):
    p = os.path.join(pasta, 'Municipios.zip')
    return {r[0]: titulo(r[1]) for r in linhas(p)} if os.path.exists(p) else {}


def leads(pasta, mes):
    ini, fim = mes + '01', mes + '31'
    cand = {}
    for z in arquivos(pasta, 'Estabelecimentos'):
        for r in linhas(z):
            if len(r) < 28 or r[19] != 'SP' or r[5] != '02' or r[3] != '1' or not (ini <= r[10] <= fim):
                continue
            cand[r[0]] = r
    print('SP, ativas, matriz, inicio em', mes, ':', len(cand))
    mei = mei_de(pasta, set(cand))
    emp = {}
    for z in arquivos(pasta, 'Empresas'):
        for r in linhas(z):
            if r[0] in cand:
                emp[r[0]] = r
    cnaes = {r[0]: r[1] for r in linhas(os.path.join(pasta, 'Cnaes.zip'))}
    mun = municipios(pasta)
    uso = collections.Counter(r[27].strip().lower() for r in cand.values() if r[27].strip())
    saida, fora = [], collections.Counter()
    for b, r in cand.items():
        e = r[27].strip().lower()
        if b in mei: fora['MEI'] += 1; continue
        if not emp.get(b) or not emp[b][2].startswith('2'): fora['não é empresa'] += 1; continue
        if not EMAIL.match(e): fora['sem e-mail válido'] += 1; continue
        if uso[e] >= 2: fora['e-mail de contabilidade (repetido)'] += 1; continue
        saida.append({'cnpj': r[0] + r[1] + r[2], 'razao': emp[b][1].strip()[:200], 'fantasia': r[4].strip()[:120], 'email': e,
                      'cnae': r[11], 'atividade': cnaes.get(r[11], '')[:160], 'cidade': mun.get(r[20], '')[:80], 'inicio': r[10]})
    print('fora:', dict(fora)); print('leads:', len(saida))
    return saida


def risco(pasta, arq):
    casos = list(csv.DictReader(open(arq, encoding='utf-8')))
    alvo = collections.defaultdict(list)
    for c in casos:
        alvo[norm(c['titular'])].append(c)
    print('casos na RPI:', len(casos), '| titulares distintos:', len(alvo))
    basico = {}
    for z in arquivos(pasta, 'Empresas'):
        for r in linhas(z):
            n = norm(r[1])
            if n in alvo:
                basico[r[0]] = n
    ach = {}
    for z in arquivos(pasta, 'Estabelecimentos'):
        for r in linhas(z):
            if len(r) < 28 or r[0] not in basico or r[3] != '1' or r[5] != '02':
                continue
            e = r[27].strip().lower()
            if EMAIL.match(e):
                ach[basico[r[0]]] = (r[0] + r[1] + r[2], e, r[19])
    saida = []
    for n, lista in alvo.items():
        if n not in ach: continue
        cnpj, e, uf = ach[n]
        for c in lista:
            if c.get('uf') and uf and c['uf'] != uf: continue
            saida.append({'rpi': c['rpi'], 'data_rpi': c['data_rpi'], 'prazo': c['prazo_ate'], 'tipo': c['tipo'], 'processo': c['processo'],
                          'marca': c['marca'][:160], 'classes': c['classes'][:60], 'titular': c['titular'][:200], 'email': e, 'cnpj': cnpj})
    print('casos com e-mail achado:', len(saida), dict(collections.Counter(x['tipo'] for x in saida)))
    return saida


def token_github():
    url = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL'] + '&audience=arizona-vigilancia'
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})
    return json.load(urllib.request.urlopen(req, timeout=30))['value']


def envia(rota, corpo):
    req = urllib.request.Request(API + rota, data=json.dumps(corpo).encode(), method='POST',
                                 headers={'Authorization': 'Bearer ' + token_github(), 'content-type': 'application/json'})
    with urllib.request.urlopen(req, timeout=300) as r:
        res = json.load(r)
    print('resposta:', {k: res.get(k) for k in ('ok', 'recebidos', 'novos', 'erro')})


if __name__ == '__main__':
    modo, pasta, alvo = sys.argv[1], sys.argv[2], sys.argv[3]
    teste = '--teste' in sys.argv
    if modo == 'leads':
        dados = leads(pasta, alvo)
        if not teste:
            for i in range(0, len(dados), 2000):
                envia('/api/vigilancia/radar-leads', {'mes': alvo, 'leads': dados[i:i + 2000]})
    elif modo == 'risco':
        dados = risco(pasta, alvo)
        if not teste and dados:
            envia('/api/vigilancia/radar-risco', {'casos': dados})
