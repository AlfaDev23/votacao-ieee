#!/usr/bin/env python3
"""
Servidor de votação para rede local.

Usa só a biblioteca padrão do Python (não precisa instalar nada).
Rode com:  python servidor.py
"""
import csv
import getpass
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PASTA = os.path.dirname(os.path.abspath(__file__))
ARQ_CONFIG = os.path.join(PASTA, "eleicao.json")
ARQ_BANCO = os.path.join(PASTA, "votacao.db")
PASTA_PUBLICO = os.path.join(PASTA, "publico")
PORTA = int(os.environ.get("PORTA", "8000"))

ALFABETO = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # sem 0/O, 1/I/L
TAM_CODIGO = 8
OPCAO_BRANCO = "Voto em branco"
OPCOES_REFERENDO = ["Sim", "Não", OPCAO_BRANCO]

MAX_FALHAS = 15          # tentativas erradas por aparelho...
JANELA_FALHAS = 600      # ...a cada 10 minutos
DURACAO_SESSAO = 12 * 3600

trava = threading.Lock()  # o volume é pequeno: serializamos todo acesso ao banco
sessoes = {}              # token -> instante de expiração
falhas = {}               # ip -> [instantes]

banco = sqlite3.connect(ARQ_BANCO, check_same_thread=False, isolation_level=None)
banco.execute("PRAGMA secure_delete=ON")
banco.executescript(
    """
    CREATE TABLE IF NOT EXISTS estado(chave TEXT PRIMARY KEY, valor TEXT);
    -- WITHOUT ROWID: as tabelas ficam ordenadas pela chave, não pela ordem de chegada.
    CREATE TABLE IF NOT EXISTS codigos(
        codigo TEXT PRIMARY KEY,
        usado  INTEGER NOT NULL DEFAULT 0
    ) WITHOUT ROWID;
    -- Só guardamos contagens. Não existe registro individual de voto.
    CREATE TABLE IF NOT EXISTS apuracao(
        cargo TEXT NOT NULL,
        opcao TEXT NOT NULL,
        total INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(cargo, opcao)
    ) WITHOUT ROWID;
    """
)


@contextmanager
def transacao():
    banco.execute("BEGIN IMMEDIATE")
    try:
        yield
        banco.execute("COMMIT")
    except BaseException:
        banco.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- estado

def ler_estado(chave, padrao=None):
    linha = banco.execute("SELECT valor FROM estado WHERE chave=?", (chave,)).fetchone()
    return linha[0] if linha else padrao


def gravar_estado(chave, valor):
    if valor is None:
        banco.execute("DELETE FROM estado WHERE chave=?", (chave,))
    else:
        banco.execute(
            "INSERT INTO estado(chave, valor) VALUES(?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor",
            (chave, valor),
        )


def fase_atual():
    return ler_estado("fase", "preparacao")


# ---------------------------------------------------------------- cédula

def montar_cedula(cfg):
    """Valida o conteúdo de eleicao.json e devolve a cédula pronta."""
    if not isinstance(cfg, dict):
        raise ValueError("O arquivo precisa ser um objeto JSON.")
    titulo = str(cfg.get("titulo") or "Eleição").strip()
    cargos, sem_candidatos, vistos = [], [], set()
    for item in cfg.get("cargos", []):
        nome = str(item.get("cargo", "")).strip()
        if not nome:
            raise ValueError("Há um cargo sem nome.")
        if nome in vistos:
            raise ValueError(f"Cargo repetido: {nome}")
        vistos.add(nome)
        candidatos = [str(c).strip() for c in item.get("candidatos", []) if str(c).strip()]
        if len(set(candidatos)) != len(candidatos):
            raise ValueError(f"Candidato repetido no cargo {nome}.")
        if OPCAO_BRANCO in candidatos:
            raise ValueError(f'"{OPCAO_BRANCO}" não pode ser nome de candidato.')
        if not candidatos:
            sem_candidatos.append(nome)
            continue
        if len(candidatos) == 1:
            cargos.append({"cargo": nome, "tipo": "referendo",
                           "candidatos": candidatos, "opcoes": list(OPCOES_REFERENDO)})
        else:
            cargos.append({"cargo": nome, "tipo": "escolha",
                           "candidatos": candidatos, "opcoes": candidatos + [OPCAO_BRANCO]})
    return {"titulo": titulo, "cargos": cargos, "sem_candidatos": sem_candidatos}


def cedula_vigente():
    """Na preparação, lê o arquivo (pode ser editado). Depois de aberta, usa a cópia congelada."""
    if fase_atual() == "preparacao":
        with open(ARQ_CONFIG, encoding="utf-8") as f:
            return montar_cedula(json.load(f))
    return json.loads(ler_estado("cedula"))


# ---------------------------------------------------------------- códigos e senha

def normalizar_codigo(texto):
    return re.sub(r"[^A-Z0-9]", "", str(texto or "").upper())


def formatar_codigo(c):
    return f"{c[:4]}-{c[4:]}"


def gerar_codigos(qtd):
    novos = []
    with transacao():
        while len(novos) < qtd:
            c = "".join(secrets.choice(ALFABETO) for _ in range(TAM_CODIGO))
            if banco.execute("INSERT OR IGNORE INTO codigos(codigo) VALUES(?)", (c,)).rowcount:
                novos.append(c)
    return novos


def hash_senha(senha, sal=None):
    sal = sal or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", senha.encode(), bytes.fromhex(sal), 200_000).hex()
    return f"{sal}${h}"


def senha_confere(senha):
    salvo = ler_estado("senha")
    if not salvo or not isinstance(senha, str):
        return False
    sal, _ = salvo.split("$", 1)
    return hmac.compare_digest(hash_senha(senha, sal), salvo)


def bloqueado(ip):
    agora = time.time()
    falhas[ip] = [t for t in falhas.get(ip, []) if agora - t < JANELA_FALHAS]
    return len(falhas[ip]) >= MAX_FALHAS


def registrar_falha(ip):
    falhas.setdefault(ip, []).append(time.time())


# ---------------------------------------------------------------- apuração

def resultados(cedula):
    tot = {(c, o): t for c, o, t in banco.execute("SELECT cargo, opcao, total FROM apuracao")}
    saida = []
    for cargo in cedula["cargos"]:
        opcoes = [{"opcao": o, "total": tot.get((cargo["cargo"], o), 0)} for o in cargo["opcoes"]]
        saida.append({**cargo, "resultado": opcoes, "soma": sum(o["total"] for o in opcoes)})
    return saida


def contagem_codigos():
    total, usados = banco.execute(
        "SELECT COUNT(*), COALESCE(SUM(usado), 0) FROM codigos").fetchone()
    return total, usados


# ---------------------------------------------------------------- HTTP

class Erro(Exception):
    def __init__(self, status, mensagem):
        super().__init__(mensagem)
        self.status, self.mensagem = status, mensagem


class Handler(BaseHTTPRequestHandler):
    server_version = "Votacao"
    sys_version = ""

    # Não registramos acessos: IP + horário de cada requisição poderia ligar
    # um aparelho ao momento do voto.
    def log_message(self, *args):
        pass

    # ----- utilidades

    def enviar(self, status, corpo, tipo="application/json; charset=utf-8", extra=None):
        if isinstance(corpo, (dict, list)):
            corpo = json.dumps(corpo, ensure_ascii=False)
        if isinstance(corpo, str):
            corpo = corpo.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corpo)

    def corpo_json(self):
        tam = int(self.headers.get("Content-Length") or 0)
        if tam > 65536:
            raise Erro(413, "Requisição grande demais.")
        try:
            return json.loads(self.rfile.read(tam) or b"{}")
        except json.JSONDecodeError:
            raise Erro(400, "Requisição inválida.")

    def ip(self):
        return self.client_address[0]

    def token_sessao(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return c["sessao"].value if "sessao" in c else None

    def exigir_admin(self):
        t = self.token_sessao()
        if not t or sessoes.get(t, 0) < time.time():
            sessoes.pop(t, None)
            raise Erro(401, "Faça login.")

    def arquivo(self, nome):
        with open(os.path.join(PASTA_PUBLICO, nome), "rb") as f:
            self.enviar(200, f.read(), "text/html; charset=utf-8")

    # ----- roteamento

    def do_GET(self):
        self.tratar("GET")

    def do_POST(self):
        self.tratar("POST")

    def tratar(self, metodo):
        caminho = urlparse(self.path).path.rstrip("/") or "/"
        rotas = {
            ("GET", "/"): lambda: self.arquivo("votar.html"),
            ("GET", "/admin"): lambda: self.arquivo("admin.html"),
            ("GET", "/api/cedula"): self.api_cedula,
            ("POST", "/api/validar"): self.api_validar,
            ("POST", "/api/votar"): self.api_votar,
            ("POST", "/api/admin/login"): self.api_login,
            ("POST", "/api/admin/sair"): self.api_sair,
            ("GET", "/api/admin/status"): self.api_status,
            ("POST", "/api/admin/gerar"): self.api_gerar,
            ("GET", "/api/admin/codigos"): self.api_codigos,
            ("POST", "/api/admin/abrir"): self.api_abrir,
            ("POST", "/api/admin/encerrar"): self.api_encerrar,
            ("POST", "/api/admin/zerar"): self.api_zerar,
            ("GET", "/api/admin/resultado.csv"): self.api_csv,
        }
        acao = rotas.get((metodo, caminho))
        try:
            if not acao:
                raise Erro(404, "Página não encontrada.")
            with trava:
                acao()
        except Erro as e:
            self.enviar(e.status, {"erro": e.mensagem})
        except (json.JSONDecodeError, ValueError, OSError) as e:
            self.enviar(500, {"erro": f"Erro na configuração: {e}"})

    # ----- eleitor

    def api_cedula(self):
        fase = fase_atual()
        if fase != "aberta":
            return self.enviar(200, {"fase": fase})
        c = cedula_vigente()
        self.enviar(200, {"fase": fase, "titulo": c["titulo"], "cargos": c["cargos"]})

    def checar_codigo(self, codigo):
        if bloqueado(self.ip()):
            raise Erro(429, "Muitas tentativas erradas. Procure a comissão eleitoral.")
        if fase_atual() != "aberta":
            raise Erro(409, "A votação não está aberta.")
        linha = banco.execute("SELECT usado FROM codigos WHERE codigo=?", (codigo,)).fetchone()
        if not linha:
            registrar_falha(self.ip())
            raise Erro(403, "Código não encontrado. Confira as letras e números.")
        if linha[0]:
            raise Erro(403, "Este código já foi usado.")

    def api_validar(self):
        self.checar_codigo(normalizar_codigo(self.corpo_json().get("codigo")))
        self.enviar(200, {"ok": True})

    def api_votar(self):
        dados = self.corpo_json()
        codigo = normalizar_codigo(dados.get("codigo"))
        votos = dados.get("votos")
        self.checar_codigo(codigo)
        cedula = cedula_vigente()
        if not isinstance(votos, dict) or set(votos) != {c["cargo"] for c in cedula["cargos"]}:
            raise Erro(400, "Responda todos os cargos.")
        for c in cedula["cargos"]:
            if votos[c["cargo"]] not in c["opcoes"]:
                raise Erro(400, f"Opção inválida em {c['cargo']}.")

        # Tudo numa transação: ou o código é queimado E os votos são somados, ou nada acontece.
        with transacao():
            if banco.execute("UPDATE codigos SET usado=1 WHERE codigo=? AND usado=0",
                             (codigo,)).rowcount != 1:
                raise Erro(403, "Este código já foi usado.")
            for cargo, opcao in votos.items():
                if banco.execute("UPDATE apuracao SET total=total+1 WHERE cargo=? AND opcao=?",
                                 (cargo, opcao)).rowcount != 1:
                    raise Erro(500, "Erro interno na apuração. Voto não registrado.")
        self.enviar(200, {"ok": True})

    # ----- comissão

    def api_login(self):
        if bloqueado(self.ip()):
            raise Erro(429, "Muitas tentativas erradas. Aguarde alguns minutos.")
        if not senha_confere(self.corpo_json().get("senha")):
            registrar_falha(self.ip())
            raise Erro(403, "Senha incorreta.")
        token = secrets.token_urlsafe(32)
        sessoes[token] = time.time() + DURACAO_SESSAO
        self.enviar(200, {"ok": True}, extra={
            "Set-Cookie": f"sessao={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={DURACAO_SESSAO}"})

    def api_sair(self):
        sessoes.pop(self.token_sessao(), None)
        self.enviar(200, {"ok": True}, extra={"Set-Cookie": "sessao=; Path=/; Max-Age=0"})

    def api_status(self):
        self.exigir_admin()
        fase = fase_atual()
        total, usados = contagem_codigos()
        resp = {"fase": fase, "codigos_total": total, "codigos_usados": usados}
        try:
            cedula = cedula_vigente()
            resp["cedula"] = cedula
        except (OSError, ValueError) as e:
            resp["erro_cedula"] = str(e)
        if fase == "encerrada":
            resp["resultados"] = resultados(cedula)
        self.enviar(200, resp)

    def api_gerar(self):
        self.exigir_admin()
        if fase_atual() == "encerrada":
            raise Erro(409, "A votação já foi encerrada.")
        try:
            qtd = int(self.corpo_json().get("quantidade", 0))
        except (TypeError, ValueError):
            qtd = 0
        if not 1 <= qtd <= 2000:
            raise Erro(400, "Escolha uma quantidade entre 1 e 2000.")
        novos = gerar_codigos(qtd)
        self.enviar(200, {"codigos": [formatar_codigo(c) for c in novos]})

    def api_codigos(self):
        self.exigir_admin()
        linhas = banco.execute("SELECT codigo FROM codigos WHERE usado=0 ORDER BY codigo")
        self.enviar(200, {"codigos": [formatar_codigo(l[0]) for l in linhas]})

    def api_abrir(self):
        self.exigir_admin()
        if fase_atual() != "preparacao":
            raise Erro(409, "A votação já foi aberta antes.")
        cedula = cedula_vigente()
        if not cedula["cargos"]:
            raise Erro(400, "Nenhum cargo tem candidatos em eleicao.json.")
        if contagem_codigos()[0] == 0:
            raise Erro(400, "Gere os códigos de votação antes de abrir.")
        with transacao():
            banco.execute("DELETE FROM apuracao")
            for c in cedula["cargos"]:
                for o in c["opcoes"]:
                    banco.execute("INSERT INTO apuracao(cargo, opcao, total) VALUES(?, ?, 0)",
                                  (c["cargo"], o))
            gravar_estado("cedula", json.dumps(cedula, ensure_ascii=False))
            gravar_estado("fase", "aberta")
        self.enviar(200, {"ok": True})

    def api_encerrar(self):
        self.exigir_admin()
        if fase_atual() != "aberta":
            raise Erro(409, "A votação não está aberta.")
        gravar_estado("fase", "encerrada")
        self.enviar(200, {"ok": True})

    def api_zerar(self):
        self.exigir_admin()
        dados = self.corpo_json()
        if dados.get("confirmacao") != "ZERAR":
            raise Erro(400, 'Digite ZERAR para confirmar.')
        if not senha_confere(dados.get("senha")):
            raise Erro(403, "Senha incorreta.")
        with transacao():
            banco.execute("DELETE FROM codigos")
            banco.execute("DELETE FROM apuracao")
            gravar_estado("cedula", None)
            gravar_estado("fase", "preparacao")
        banco.execute("VACUUM")
        self.enviar(200, {"ok": True})

    def api_csv(self):
        self.exigir_admin()
        if fase_atual() != "encerrada":
            raise Erro(409, "O resultado só sai depois de encerrar a votação.")
        cedula = cedula_vigente()
        saida = io.StringIO()
        w = csv.writer(saida, delimiter=";")
        w.writerow(["Cargo", "Candidato(s)", "Opção", "Votos"])
        for r in resultados(cedula):
            for o in r["resultado"]:
                w.writerow([r["cargo"], ", ".join(r["candidatos"]), o["opcao"], o["total"]])
        total, usados = contagem_codigos()
        w.writerow([])
        w.writerow(["Eleitores que votaram", "", "", usados])
        w.writerow(["Códigos gerados", "", "", total])
        self.enviar(200, "﻿" + saida.getvalue(), "text/csv; charset=utf-8",
                    {"Content-Disposition": 'attachment; filename="resultado.csv"'})


# ---------------------------------------------------------------- início

def ips_locais():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:  # não envia nada; só descobre qual interface seria usada
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    ips = {i for i in ips if not i.startswith("127.")}
    return sorted(ips) or ["127.0.0.1"]


def ler_senha(prompt):
    """Lê a senha mostrando * a cada tecla (no Windows). Fora do Windows usa getpass."""
    if os.name != "nt" or not sys.stdin.isatty():
        return getpass.getpass(prompt)
    import msvcrt
    print(prompt, end="", flush=True)
    letras = []
    while True:
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            print()
            return "".join(letras)
        if ch == "\x03":                 # Ctrl+C
            print()
            raise KeyboardInterrupt
        if ch in ("\x00", "\xe0"):       # setas, F1... ignora
            msvcrt.getwch()
            continue
        if ch == "\x08":                 # Backspace
            if letras:
                letras.pop()
                print("\b \b", end="", flush=True)
            continue
        if ch.isprintable():
            letras.append(ch)
            print("*", end="", flush=True)


def definir_senha():
    print("\nCrie a senha da comissão eleitoral (usada em /admin).")
    print("Digite e aperte Enter. Cada letra aparece como *.")
    while True:
        s1 = ler_senha("  Nova senha (mín. 6 caracteres): ")
        if len(s1) < 6:
            print("  Muito curta.")
            continue
        if ler_senha("  Repita a senha: ") != s1:
            print("  As senhas não conferem.")
            continue
        gravar_estado("senha", hash_senha(s1))
        print("  Senha salva.\n")
        return


def main():
    if "--trocar-senha" in sys.argv or not ler_estado("senha"):
        definir_senha()
    try:
        servidor = ThreadingHTTPServer(("0.0.0.0", PORTA), Handler)
    except OSError as e:
        sys.exit(f"Não consegui usar a porta {PORTA}: {e}")
    print("=" * 60)
    print("  SISTEMA DE VOTAÇÃO RODANDO")
    print("=" * 60)
    for ip in ips_locais():
        print(f"  Eleitores (celular):  http://{ip}:{PORTA}")
        print(f"  Comissão eleitoral:   http://{ip}:{PORTA}/admin\n")
    print(f"  Fase atual: {fase_atual()}")
    print("  Para desligar: feche esta janela ou aperte Ctrl+C.")
    print("=" * 60)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor desligado. Os dados continuam salvos em votacao.db.")


if __name__ == "__main__":
    main()
