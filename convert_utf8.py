#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ConvertUTF-8  —  Verificador e reparador de codificação para arquivos de texto.

Varre a pasta do próprio script (ou um caminho informado) e, para cada arquivo
de texto encontrado (.html .css .js .php .json .txt .md), detecta e corrige:

  1. Mojibake
  UTF-8 que foi lido por engano como CP1252/Latin-1 e regravado,
  produzindo lixo como 'pÃ¡gina' (-> 'página') ou 'â€"' (-> '—').

  2. Codificação legada
  Arquivos em CP1252/Latin-1/UTF-16/UTF-32 são convertidos para UTF-8.

  3. BOM
  O marcador de ordem de bytes (BOM) UTF-8 é removido por padrão
  (ele quebra header() em PHP, por exemplo).

  4. Acentuação decomposta
  Normaliza para NFC, unificando 'á' (a + acento combinante) em 'á' (código único). 
  Idêntico na tela, mas conserta busca, grep e comparação de strings.

ESTADO-ALVO: todo arquivo fica em UTF-8, sem BOM, sem mojibake e em NFC.

USO RÁPIDO
--------
  python convert_utf8.py                  # varre a pasta do script e pergunta
  python convert_utf8.py ./src            # varre uma pasta específica
  python convert_utf8.py arquivo.php      # corrige um único arquivo
  python convert_utf8.py --check          # só verifica (CI); sai com código != 0
  python convert_utf8.py --all            # lista também os arquivos saudáveis
  python convert_utf8.py --deep           # tenta o round-trip nos restos
  python convert_utf8.py --keep-bom       # preserva o BOM UTF-8
  python convert_utf8.py --no-nfc         # não normaliza acentuação
  python convert_utf8.py --no-backup      # não gera .bak (para quem usa git)
  python convert_utf8.py --backup-dir bak # concentra os backups numa pasta
  python convert_utf8.py --restore        # desfaz: restaura a partir dos .bak
  python convert_utf8.py --yes            # pula a confirmação (automação)

CÓDIGOS DE SAÍDA: 0 = limpo, 1 = havia problemas (ou foram corrigidos), 2 = erro.
"""

from __future__ import annotations

import argparse
import difflib
import os
import sys
import unicodedata
from pathlib import Path


# ============================================================================
# CONFIGURAÇÃO  —  ajuste aqui sem precisar mexer na lógica abaixo.
# ============================================================================

# Extensões de arquivo que serão analisadas.
EXTENSOES_ALVO = {".html", ".css", ".js", ".php", ".json", ".txt", ".md"}

# Pastas nunca varridas (dependências, metadados de VCS, saída de build).
# Remova "vendor" se você também quiser corrigir bibliotecas PHP vendoradas.
PASTAS_IGNORADAS = {
    ".git", ".svn", ".hg", "node_modules", "vendor", "__pycache__",
    ".idea", ".vscode", "dist", "build", ".next", ".cache",
}

# Respostas aceitas como confirmação no prompt (maiúsc./minúsc. indiferente).
TECLAS_CONFIRMACAO = {"y", "yes", "s", "sim"}

SUFIXO_BACKUP = ".bak"          # sufixo dos arquivos de backup
SUFIXO_TEMP = ".convtmp"        # sufixo do arquivo temporário durante a gravação
TAMANHO_MAXIMO = 25 * 1024 * 1024  # ignora arquivos maiores que isto (segurança)
CHAR_SUBSTITUICAO = "\ufffd"    # U+FFFD: marca um byte que já se perdeu antes
LIMITE_CONTROLE = 0.30          # acima desta fração de chars de controle = binário


# ============================================================================
# CORES NO TERMINAL  —  ANSI com desligamento automático.
# ============================================================================
class Cor:
    """Códigos de cor ANSI.

    Ficam VAZIOS (string em branco) quando a saída não é um terminal de verdade
    (ex.: redirecionada para arquivo) ou quando a variável de ambiente NO_COLOR
    está definida — esse é um padrão informal respeitado pela maioria das CLIs.
    """

    def __init__(self, ativo: bool):
        self.RESET = "\033[0m" if ativo else ""
        self.NEGRITO = "\033[1m" if ativo else ""
        self.VERMELHO = "\033[31m" if ativo else ""
        self.VERDE = "\033[32m" if ativo else ""
        self.AMARELO = "\033[33m" if ativo else ""
        self.CINZA = "\033[90m" if ativo else ""


def criar_cores() -> Cor:
    """Decide se as cores devem ser usadas e devolve o objeto Cor."""
    ativo = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    return Cor(ativo)


# ============================================================================
# TABELA DE MOJIBAKE  —  gerada por código (sem mapa digitado à mão).
#
# Para cada caractere "correto", calculamos como ele APARECE quando seus bytes
# UTF-8 reais são lidos por engano como CP1252. Essa forma quebrada vira a
# CHAVE; o caractere correto vira o VALOR.
#
# Por que isso é seguro: toda chave gerada é da forma "Ã/Â/â€ + um byte que NÃO
# é letra ASCII". Combinações assim nunca aparecem dentro de texto português
# correto — então "SÃO PAULO" e "OPÇÃO" passam intactos e nunca são alterados.
# ============================================================================
def _montar_tabela_mojibake() -> dict:
    """Constrói o dicionário {sequência_quebrada: caractere_correto}."""
    alvos = []  # lista dos caracteres "corretos" que queremos proteger

    # Suplemento Latin-1 (U+00A1..U+00FF): todas as letras acentuadas usadas em
    # português, mais ª º ° « » ± etc.
    alvos += [chr(cp) for cp in range(0x00A1, 0x0100)]

    # Pontuação e símbolos tipográficos comuns (estilo Windows/Office).
    alvos += list(
        "\u20ac"               # €  euro
        "\u2013\u2014"         # –  —  travessões (en dash / em dash)
        "\u2018\u2019"         # '  '  aspas simples curvas
        "\u201c\u201d"         # "  "  aspas duplas curvas
        "\u2026"               # …  reticências
        "\u2022"               # •  marcador (bullet)
        "\u2122\u00a9\u00ae"   # ™  ©  ®
        "\u00a0"               # espaço inquebrável (NBSP)
    )

    tabela = {}
    for ch in alvos:
        try:
            # Bytes UTF-8 reais do caractere, relidos como CP1252 = a forma quebrada.
            quebrado = ch.encode("utf-8").decode("cp1252")
        except UnicodeDecodeError:
            # CP1252 tem posições indefinidas (0x81, 0x8D, 0x8F, 0x90, 0x9D);
            # caracteres cuja forma quebrada cairia nelas são simplesmente pulados.
            continue
        if quebrado != ch:  # mantém só transformações reais
            tabela[quebrado] = ch
    return tabela


TABELA_MOJIBAKE = _montar_tabela_mojibake()
# Substituímos as chaves mais LONGAS primeiro, para que correções de 3
# caracteres (ex.: travessão) tenham prioridade sobre as de 2.
_CHAVES_ORDENADAS = sorted(TABELA_MOJIBAKE, key=len, reverse=True)


# ============================================================================
# FUNÇÕES DE REPARO DE TEXTO
# ============================================================================
def contar_mojibake(texto: str) -> int:
    """Conta quantas sequências quebradas conhecidas existem no texto."""
    return sum(texto.count(chave) for chave in TABELA_MOJIBAKE)


def corrigir_mojibake(texto: str) -> str:
    """Troca cada sequência quebrada pelo caractere correto.

    Roda em MÚLTIPLAS passadas para tratar corrupção dupla (quando o texto
    passou pelo erro de codificação mais de uma vez, ex.: 'Ã£Â©'). Para assim
    que o texto estabiliza — na prática converge em 1 ou 2 passadas.
    """
    for _ in range(5):  # teto de passadas, por garantia
        anterior = texto
        for chave in _CHAVES_ORDENADAS:
            if chave in texto:
                texto = texto.replace(chave, TABELA_MOJIBAKE[chave])
        if texto == anterior:
            break  # nada mudou nesta passada -> convergiu
    return texto


def corrigir_profundo(texto: str) -> str:
    """Modo agressivo (--deep): reverte o round-trip de codificação no texto todo.

    Só é ACEITO se reduzir a contagem de mojibake — assim, texto já correto
    nunca é danificado. Serve para pegar sequências raras que não estão na
    tabela (caracteres fora do conjunto latino comum).
    """
    for codec in ("cp1252", "latin-1"):
        try:
            candidato = texto.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if contar_mojibake(candidato) < contar_mojibake(texto):
            return candidato
    return texto


def para_nfc(texto: str):
    """Normaliza o texto para NFC (acentos precompostos).

    Devolve (texto_normalizado, quantidade). A quantidade é uma estimativa de
    quantos acentos combinantes foram absorvidos — útil só para o relatório.
    """
    normalizado = unicodedata.normalize("NFC", texto)
    if normalizado == texto:
        return texto, 0
    # Conta quantas marcas combinantes desapareceram ao compor os acentos.
    antes = sum(1 for ch in texto if unicodedata.combining(ch))
    depois = sum(1 for ch in normalizado if unicodedata.combining(ch))
    return normalizado, max(antes - depois, 1)


# ============================================================================
# DETECÇÃO DE CODIFICAÇÃO
# ============================================================================
# Assinaturas de BOM (Byte Order Mark) -> (rótulo, codec_para_decodificar, tamanho).
# A ORDEM importa: UTF-32-LE começa com FF FE 00 00 e UTF-16-LE começa com
# FF FE; por isso o UTF-32 (4 bytes) precisa ser testado ANTES do UTF-16.
_BOMS = [
    (b"\x00\x00\xfe\xff", "utf-32-be", "utf-32", 4),
    (b"\xff\xfe\x00\x00", "utf-32-le", "utf-32", 4),
    (b"\xef\xbb\xbf",     "utf-8 (com BOM)", "utf-8-sig", 3),
    (b"\xff\xfe",         "utf-16-le", "utf-16", 2),
    (b"\xfe\xff",         "utf-16-be", "utf-16", 2),
]


def detectar_bom(raw: bytes):
    """Devolve (rótulo, codec, tamanho) se houver BOM no início; senão None."""
    for assinatura, rotulo, codec, tamanho in _BOMS:
        if raw.startswith(assinatura):
            return rotulo, codec, tamanho
    return None


def parece_binario(raw: bytes) -> bool:
    """Heurística simples: um byte NUL no início indica conteúdo binário."""
    return b"\x00" in raw[:4096]


def proporcao_controle(texto: str) -> float:
    """Fração de caracteres de controle no texto (ignorando espaços comuns).

    Usada como segunda barreira contra arquivos binários disfarçados de texto:
    se a proporção for alta, o "texto" é provavelmente lixo binário.
    """
    if not texto:
        return 0.0
    controle = sum(
        1 for ch in texto
        if unicodedata.category(ch) == "Cc" and ch not in "\t\n\r\f\v"
    )
    return controle / len(texto)


# ============================================================================
# ANÁLISE POR ARQUIVO
# ============================================================================
class Relatorio:
    """Guarda o resultado da análise de um único arquivo."""

    def __init__(self, caminho: Path):
        self.caminho = caminho
        self.origem = "utf-8"           # rótulo da codificação detectada
        self.problemas = []             # descrições legíveis (para o relatório)
        self.qtd_total = 0              # soma das magnitudes (para o total geral)
        self.texto_original = ""        # texto como está hoje (base do diff)
        self.texto_novo = None          # texto corrigido (None = nada a fazer)
        self.novos_bytes = None         # bytes finais a gravar (None = nada a fazer)
        self.perda_irreversivel = 0     # quantidade de U+FFFD encontrados
        self.so_rebytes = False         # True se mudam só os bytes (sem mudança visível)
        self.tem_diff_visivel = False   # True se vale a pena mostrar diff de linhas

    @property
    def precisa_corrigir(self) -> bool:
        """True quando há algo concreto a gravar."""
        return self.novos_bytes is not None


def analisar(caminho: Path, opcoes) -> "Relatorio | None":
    """Analisa um arquivo e devolve um Relatorio se houver algo a corrigir.

    Nunca levanta exceção: arquivos ilegíveis ou binários são simplesmente
    pulados (retorna None). Em modo --all, retorna o Relatorio mesmo para
    arquivos saudáveis, para que possam ser listados.
    """
    try:
        raw = caminho.read_bytes()
    except OSError:
        return None
    if len(raw) > TAMANHO_MAXIMO:
        return None

    rel = Relatorio(caminho)
    tem_bom_utf8 = False
    converteu_codec = False
    texto = None

    # --- 1. Detecta BOM PRIMEIRO (precisa vir antes do teste de binário,
    #         porque arquivos UTF-16 têm muitos bytes NUL legítimos) ---
    bom = detectar_bom(raw)
    if bom is not None:
        rotulo, codec, _tam = bom
        rel.origem = rotulo
        if codec == "utf-8-sig":
            # UTF-8 com BOM: já é UTF-8, só precisa (talvez) tirar o BOM.
            tem_bom_utf8 = True
            try:
                texto = raw.decode("utf-8-sig")  # decodifica e remove o BOM
            except UnicodeDecodeError:
                texto = raw.decode("utf-8-sig", "replace")
        else:
            # UTF-16 / UTF-32: NÃO é UTF-8 -> precisa conversão de verdade.
            converteu_codec = True
            try:
                texto = raw.decode(codec)  # o codec genérico já remove o BOM
            except UnicodeDecodeError:
                return None  # irrecuperável neste codec
    else:
        # --- 2. Sem BOM: tenta UTF-8 estrito ---
        if parece_binario(raw):
            return None  # byte NUL no início -> binário
        try:
            texto = raw.decode("utf-8")
            rel.origem = "utf-8"
        except UnicodeDecodeError:
            # --- 3. Não é UTF-8 válido: trata como legado single-byte ---
            # CP1252 primeiro (superset do printable do Latin-1: também cobre
            # aspas e travessões tipográficos); cai para Latin-1 se falhar.
            for codec in ("cp1252", "latin-1"):
                try:
                    texto = raw.decode(codec)
                    rel.origem = codec
                    converteu_codec = True
                    break
                except UnicodeDecodeError:
                    continue
            if texto is None:
                return None
            # Segunda barreira anti-binário: muitos caracteres de controle?
            if proporcao_controle(texto) > LIMITE_CONTROLE:
                return None

    # Neste ponto temos `texto` como str. Guarda o estado atual (base do diff).
    rel.texto_original = texto

    # --- 4. Conta perda já consumada (U+FFFD que já estava no arquivo) ---
    rel.perda_irreversivel = texto.count(CHAR_SUBSTITUICAO)

    # --- 5. Pipeline de correção ---
    qtd_mojibake = contar_mojibake(texto)
    novo = corrigir_mojibake(texto)
    if opcoes.deep:
        novo = corrigir_profundo(novo)
    qtd_nfc = 0
    if not opcoes.sem_nfc:
        novo, qtd_nfc = para_nfc(novo)

    # --- 6. Monta os bytes finais ---
    # BOM UTF-8 é removido por padrão; mantido só com --keep-bom.
    manter_bom = tem_bom_utf8 and opcoes.manter_bom
    remover_bom = tem_bom_utf8 and not opcoes.manter_bom
    prefixo = "\ufeff" if manter_bom else ""
    # Gravamos como "utf-8" (não "utf-8-sig") para não readicionar BOM sozinho.
    novos_bytes = (prefixo + novo).encode("utf-8")

    # --- 7. Registra os problemas encontrados (para o relatório) ---
    if qtd_mojibake:
        rel.problemas.append(f"mojibake corrigido (x{qtd_mojibake})")
        rel.qtd_total += qtd_mojibake
    if qtd_nfc:
        rel.problemas.append(f"acentuação normalizada para NFC (x{qtd_nfc})")
        rel.qtd_total += qtd_nfc
    if remover_bom:
        rel.problemas.append("BOM UTF-8 removido")
        rel.qtd_total += 1
    if converteu_codec:
        rel.problemas.append(f"convertido de {rel.origem} para UTF-8")
        rel.qtd_total += 1
    if rel.perda_irreversivel:
        # Não soma no total: não é algo que conseguimos consertar, é só aviso.
        rel.problemas.append(
            f"{rel.perda_irreversivel} caractere(s) ja perdido(s) U+FFFD "
            "(nao recuperavel)"
        )

    # Só vale a pena mostrar diff de linhas quando há mojibake (mudança visível).
    rel.tem_diff_visivel = qtd_mojibake > 0

    # --- 8. Há mudança real? ---
    if novos_bytes == raw:
        # Não há bytes a reescrever. Ainda assim retornamos o relatório se o
        # arquivo tiver perda irreversível (para avisar) ou em modo --all (para
        # listar). Nesses casos texto_novo/novos_bytes ficam None: nada é gravado.
        if opcoes.listar_todos or rel.perda_irreversivel:
            return rel
        return None

    rel.texto_novo = novo
    rel.novos_bytes = novos_bytes
    # "só rebytes": os bytes mudam, mas o texto na tela é idêntico
    # (conversão de codificação ou remoção de BOM, sem alterar caracteres).
    rel.so_rebytes = (novo == texto)
    return rel


# ============================================================================
# VARREDURA DO SISTEMA DE ARQUIVOS
# ============================================================================
def varrer(base: Path, extensoes: set, ignoradas: set):
    """Percorre `base` e subpastas e gera os arquivos-alvo.

    Não segue links simbólicos (evita sair da árvore) e ignora o próprio script.
    """
    eu = Path(__file__).resolve()
    for raiz, dirs, arquivos in os.walk(base, followlinks=False):
        # Poda as pastas ignoradas IN-PLACE para o os.walk não descer nelas.
        dirs[:] = [d for d in dirs if d not in ignoradas]
        for nome in arquivos:
            p = Path(raiz) / nome
            if p.suffix.lower() not in extensoes:
                continue
            if p.is_symlink():
                continue
            if p.resolve() == eu:  # nunca reescreve a si mesmo
                continue
            yield p


# ============================================================================
# GRAVAÇÃO SEGURA  (backup -> temporário -> os.replace atômico)
# ============================================================================
def caminho_backup(caminho: Path, base: Path, dir_backup: "Path | None") -> Path:
    """Decide onde gravar o .bak deste arquivo."""
    if dir_backup is None:
        # Backup ao lado do original: arquivo.ext -> arquivo.ext.bak
        return caminho.with_name(caminho.name + SUFIXO_BACKUP)
    # Backup concentrado numa pasta, espelhando a estrutura relativa.
    relativo = caminho.relative_to(base)
    destino = dir_backup / relativo
    return destino.with_name(destino.name + SUFIXO_BACKUP)


def gravar_corrigido(rel: Relatorio, base: Path, fazer_backup: bool,
                     dir_backup: "Path | None") -> None:
    """Grava o conteúdo corrigido em UTF-8, preservando as quebras de linha.

    As quebras de linha originais (LF ou CRLF) são preservadas byte a byte
    porque gravamos os bytes já prontos (rel.novos_bytes), sem modo texto.
    A troca é atômica: escreve num arquivo temporário e faz os.replace.
    """
    caminho = rel.caminho

    if fazer_backup:
        bak = caminho_backup(caminho, base, dir_backup)
        bak.parent.mkdir(parents=True, exist_ok=True)
        if not bak.exists():  # não sobrescreve um backup anterior
            bak.write_bytes(caminho.read_bytes())

    tmp = caminho.with_name(caminho.name + SUFIXO_TEMP)
    tmp.write_bytes(rel.novos_bytes)
    os.replace(tmp, caminho)  # atômico no mesmo sistema de arquivos


# ============================================================================
# RESTAURAÇÃO  (desfaz uma execução anterior a partir dos .bak)
# ============================================================================
def restaurar_backups(base: Path, dir_backup: "Path | None", cor: Cor,
                      auto_sim: bool) -> int:
    """Restaura os arquivos a partir dos backups .bak e os remove em seguida."""
    raiz = dir_backup if dir_backup is not None else base
    encontrados = []  # pares (caminho_do_bak, caminho_alvo)

    for r, dirs, arquivos in os.walk(raiz, followlinks=False):
        dirs[:] = [d for d in dirs if d not in PASTAS_IGNORADAS]
        for nome in arquivos:
            if not nome.endswith(SUFIXO_BACKUP):
                continue
            bak = Path(r) / nome
            nome_original = nome[: -len(SUFIXO_BACKUP)]  # tira o ".bak"
            if dir_backup is None:
                alvo = bak.with_name(nome_original)
            else:
                relativo = bak.relative_to(dir_backup).with_name(nome_original)
                alvo = base / relativo
            encontrados.append((bak, alvo))

    if not encontrados:
        print("Nenhum backup .bak encontrado.")
        return 0

    print(f"Backups encontrados ({len(encontrados)}):\n")
    for bak, alvo in encontrados:
        print(f"  {os.path.relpath(alvo, base)}  <-  {os.path.relpath(bak, base)}")

    if not confirmar("\nRestaurar esses arquivos? [y/s para confirmar] ", auto_sim):
        print("Cancelado.")
        return 1

    n = 0
    for bak, alvo in encontrados:
        try:
            alvo.parent.mkdir(parents=True, exist_ok=True)
            alvo.write_bytes(bak.read_bytes())
            bak.unlink()  # remove o backup após restaurar
            n += 1
        except OSError as e:
            print(f"  {cor.VERMELHO}ERRO ao restaurar {alvo}: {e}{cor.RESET}",
                  file=sys.stderr)

    print(f"\nRestaurados: {n}/{len(encontrados)} arquivo(s).")
    return 0


# ============================================================================
# APRESENTAÇÃO  (prévia das mudanças)
# ============================================================================
def mostrar_preview(rel: Relatorio, cor: Cor, max_linhas: int = 6) -> None:
    """Mostra uma amostra das mudanças de um arquivo antes da confirmação."""
    if rel.tem_diff_visivel and rel.texto_novo is not None:
        antigas = rel.texto_original.splitlines()
        novas = rel.texto_novo.splitlines()
        # n=0 -> só as linhas alteradas, sem contexto ao redor.
        diff = difflib.unified_diff(antigas, novas, lineterm="", n=0)
        linhas = [
            l for l in diff
            if l and l[0] in "+-" and not l.startswith(("+++", "---"))
        ]
        for l in linhas[:max_linhas]:
            amostra = l[:120] + ("…" if len(l) > 120 else "")
            cor_linha = cor.VERMELHO if l.startswith("-") else cor.VERDE
            print(f"      {cor_linha}{amostra}{cor.RESET}")
        if len(linhas) > max_linhas:
            restantes = len(linhas) - max_linhas
            print(f"      {cor.CINZA}... (+{restantes} linha(s) alterada(s)){cor.RESET}")
    else:
        # Mudança sem reflexo visível (conversão de codificação / BOM / NFC).
        print(f"      {cor.CINZA}(alteracao em nivel de bytes, "
              f"sem mudanca visivel no texto){cor.RESET}")


# ============================================================================
# UTILITÁRIOS DE CLI
# ============================================================================
def confirmar(mensagem: str, auto_sim: bool) -> bool:
    """Pergunta ao usuário; retorna True só se ele confirmar (y/s)."""
    if auto_sim:
        return True
    try:
        resposta = input(mensagem).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return resposta in TECLAS_CONFIRMACAO


def obter_argumentos():
    """Define e lê os argumentos de linha de comando."""
    p = argparse.ArgumentParser(
        prog="convert_utf8.py",
        description="ConvertUTF-8 — verifica e corrige a codificação de arquivos de texto.",
        epilog="Sem confirmação, nada é gravado. Backups .bak são criados por padrão.",
    )
    p.add_argument("caminho", nargs="?",
                   help="Arquivo ou pasta a processar (padrão: a pasta deste script).")
    p.add_argument("--check", action="store_true",
                   help="Só verifica e relata; não altera nada. Sai com código != 0 se houver problema.")
    p.add_argument("--all", dest="listar_todos", action="store_true",
                   help="Lista também os arquivos saudáveis, com a codificação detectada.")
    p.add_argument("--deep", action="store_true",
                   help="Tenta também o round-trip nos restos que a tabela não pegar.")
    p.add_argument("--no-nfc", dest="sem_nfc", action="store_true",
                   help="Não normaliza a acentuação para NFC.")
    p.add_argument("--keep-bom", dest="manter_bom", action="store_true",
                   help="Preserva o BOM UTF-8 em vez de removê-lo.")
    p.add_argument("--no-backup", dest="backup", action="store_false",
                   help="Não gera cópias .bak antes de sobrescrever.")
    p.add_argument("--backup-dir", dest="dir_backup", metavar="PASTA",
                   help="Concentra os backups nesta pasta (espelha a estrutura de pastas).")
    p.add_argument("--restore", action="store_true",
                   help="Restaura os arquivos a partir dos .bak e encerra.")
    p.add_argument("--yes", dest="auto_sim", action="store_true",
                   help="Pula a confirmação interativa (uso em automação/CI).")
    p.add_argument("--no-color", action="store_true",
                   help="Desliga as cores no terminal.")
    return p.parse_args()


# ============================================================================
# PROGRAMA PRINCIPAL
# ============================================================================
def main() -> int:
    args = obter_argumentos()

    if args.no_color:
        os.environ["NO_COLOR"] = "1"
    cor = criar_cores()

    dir_backup = Path(args.dir_backup).resolve() if args.dir_backup else None

    # Resolve o alvo: arquivo único, pasta informada, ou a pasta do script.
    arquivo_unico = None
    if args.caminho:
        alvo = Path(args.caminho).resolve()
        if alvo.is_file():
            arquivo_unico = alvo
            base = alvo.parent
        elif alvo.is_dir():
            base = alvo
        else:
            print(f"Caminho não encontrado: {alvo}", file=sys.stderr)
            return 2
    else:
        base = Path(__file__).resolve().parent

    # Modo restauração: desfaz e sai antes de qualquer outra coisa.
    if args.restore:
        return restaurar_backups(base, dir_backup, cor, args.auto_sim)

    print(f"{cor.NEGRITO}ConvertUTF-8{cor.RESET}")
    print(f"Alvo: {base}")
    print(f"Extensões: {', '.join(sorted(EXTENSOES_ALVO))}\n")

    # --- Coleta os relatórios ---
    if arquivo_unico is not None:
        fontes = [arquivo_unico]
    else:
        fontes = varrer(base, EXTENSOES_ALVO, PASTAS_IGNORADAS)

    relatorios = []
    for p in fontes:
        rel = analisar(p, args)
        if rel is not None:
            relatorios.append(rel)

    a_corrigir = [r for r in relatorios if r.precisa_corrigir]
    # Arquivos que não serão reescritos: ou são saudáveis, ou só têm aviso de
    # perda irreversível (U+FFFD) que não dá para consertar.
    so_aviso = [r for r in relatorios
                if not r.precisa_corrigir and r.perda_irreversivel]
    saudaveis = [r for r in relatorios
                 if not r.precisa_corrigir and not r.perda_irreversivel]

    # --- Lista os saudáveis, se pedido (--all) ---
    if args.listar_todos and saudaveis:
        print(f"{cor.CINZA}Arquivos saudáveis ({len(saudaveis)}):{cor.RESET}")
        for r in saudaveis:
            caminho_rel = os.path.relpath(r.caminho, base)
            print(f"  {cor.VERDE}OK{cor.RESET}  {caminho_rel}  "
                  f"{cor.CINZA}[{r.origem}]{cor.RESET}")
        print()

    # --- Avisa sobre perda irreversível (sempre, mesmo sem --all) ---
    if so_aviso:
        print(f"{cor.AMARELO}Arquivos com perda ja consumada "
              f"({len(so_aviso)}) — validos em UTF-8, mas com U+FFFD que NAO "
              f"pode ser recuperado:{cor.RESET}")
        for r in so_aviso:
            caminho_rel = os.path.relpath(r.caminho, base)
            print(f"  {cor.AMARELO}!{cor.RESET}  {caminho_rel}  "
                  f"{cor.CINZA}({r.perda_irreversivel}x U+FFFD){cor.RESET}")
        print()

    # --- Nada a corrigir ---
    if not a_corrigir:
        if so_aviso:
            print(f"{cor.CINZA}Nenhuma correção de codificação a aplicar "
                  f"(veja os avisos acima).{cor.RESET}")
        else:
            print(f"{cor.VERDE}Nenhum problema de codificação encontrado. "
                  f"Tudo certo.{cor.RESET}")
        return 0

    # --- Relatório dos problemas + prévia ---
    total = sum(r.qtd_total for r in a_corrigir)
    com_perda = [r for r in a_corrigir if r.perda_irreversivel]

    print(f"{cor.AMARELO}Arquivos a corrigir ({len(a_corrigir)}):{cor.RESET}\n")
    for r in a_corrigir:
        caminho_rel = os.path.relpath(r.caminho, base)
        print(f"  {cor.NEGRITO}{caminho_rel}{cor.RESET}  "
              f"{cor.CINZA}[{r.origem}]{cor.RESET}")
        print(f"      {'; '.join(r.problemas)}")
        mostrar_preview(r, cor)
        print()

    print(f"Total de problemas encontrados: {cor.NEGRITO}{total}{cor.RESET}")

    if com_perda:
        print(f"{cor.AMARELO}Atencao: {len(com_perda)} arquivo(s) contem "
              f"caracteres ja perdidos (U+FFFD) que NAO podem ser recuperados "
              f"automaticamente. Revise-os a mao.{cor.RESET}")

    # --- Modo verificação: não grava, sai com código de erro ---
    if args.check:
        print(f"\n{cor.CINZA}Modo --check: nenhum arquivo foi alterado.{cor.RESET}")
        return 1

    # --- Aviso sobre backups ---
    if args.backup:
        destino = str(dir_backup) if dir_backup else "ao lado dos originais"
        print(f"Backups: ativados ({destino}).")
    else:
        print(f"{cor.AMARELO}Backups: DESATIVADOS (--no-backup).{cor.RESET}")

    # --- Confirmação ---
    if not confirmar(
        f"\nCorrigir esses {len(a_corrigir)} arquivo(s)? [y/s para confirmar] ",
        args.auto_sim,
    ):
        print("Cancelado. Nenhum arquivo foi modificado.")
        return 1

    # --- Aplicação ---
    corrigidos = 0
    for r in a_corrigir:
        try:
            gravar_corrigido(r, base, args.backup, dir_backup)
            corrigidos += 1
        except OSError as e:
            print(f"  {cor.VERMELHO}ERRO ao gravar {r.caminho}: {e}{cor.RESET}",
                  file=sys.stderr)

    print(f"\n{cor.VERDE}Concluído: {corrigidos}/{len(a_corrigir)} "
          f"arquivo(s) corrigido(s).{cor.RESET}")
    return 0 if corrigidos == len(a_corrigir) else 1


if __name__ == "__main__":
    raise SystemExit(main())
