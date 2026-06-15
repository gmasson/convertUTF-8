# ConvertUTF-8

Verificador e reparador de codificação para arquivos de texto. Encontra arquivos com **mojibake** (acentuação quebrada), **codificação legada** ou **BOM**, e corrige tudo para **UTF-8 limpo** — em um único arquivo Python, sem nenhuma dependência externa.

## O problema

Mojibake acontece quando um arquivo **UTF-8** é lido por engano como **CP1252/Latin-1** e regravado. O resultado é o lixo clássico: `página` vira `pÃ¡gina`, `função` vira `funÃ§Ã£o`, e o travessão `—` vira `â€"`. O arquivo continua sendo UTF-8 *válido* — só que com o conteúdo errado, o que engana muitas ferramentas que só checam "é UTF-8?".

O ConvertUTF-8 trata esse caso e mais alguns vizinhos: arquivos legados em Latin-1/CP1252/UTF-16/UTF-32, presença de BOM, e acentuação decomposta (NFD).

**Estado-alvo:** todo arquivo fica em UTF-8, sem BOM, sem mojibake e normalizado em NFC.

## Requisitos

- **Python 3.8+**
- Nenhuma dependência. Só a biblioteca padrão.

## Instalação

Não há instalação. Baixe o `convert_utf8.py` e coloque na raiz do projeto que você quer limpar.

## Uso

Por padrão, o script varre a **pasta onde ele está** (e todas as subpastas), mostra o que encontrou e **só grava depois de você confirmar com `y` ou `s`**.

```bash
python convert_utf8.py                  # varre a pasta do script e pergunta
python convert_utf8.py ./src            # varre uma pasta específica
python convert_utf8.py arquivo.php      # corrige um único arquivo
```

### Fluxo

1. **Varre** os arquivos com as extensões alvo (`.html .css .js .php .json .txt .md`).
2. **Mostra** a lista, os problemas de cada arquivo e uma **prévia (diff)** das mudanças.
3. **Pergunta** se pode aplicar. Nada é gravado até você responder `y`/`s`.
4. **Corrige**, gerando um backup `.bak` e gravando de forma atômica.

### Opções

| Flag | O que faz |
|------|-----------|
| `--check` | Só verifica e relata; **não altera nada**. Sai com código `!= 0` se houver problema. Ideal para CI / pre-commit. |
| `--all` | Lista também os arquivos **saudáveis**, com a codificação detectada. |
| `--deep` | Além da tabela, tenta o *round-trip* de codificação para pegar sequências raras. |
| `--no-nfc` | Não normaliza a acentuação para NFC. |
| `--keep-bom` | Preserva o BOM UTF-8 em vez de removê-lo. |
| `--no-backup` | Não gera cópias `.bak` (útil para quem já versiona com git). |
| `--backup-dir PASTA` | Concentra os backups em uma pasta, espelhando a estrutura. |
| `--restore` | **Desfaz**: restaura os arquivos a partir dos `.bak` e encerra. |
| `--yes` | Pula a confirmação interativa (automação). |
| `--no-color` | Desliga as cores no terminal. |

### Exemplos

```bash
# Verificar sem alterar (retorna erro se achar algo) — bom para CI
python convert_utf8.py --check

# Ver tudo, inclusive os arquivos que já estão OK
python convert_utf8.py --all

# Corrigir sem perguntar, sem gerar .bak (confiando no git)
python convert_utf8.py --yes --no-backup

# Guardar os backups numa pasta separada
python convert_utf8.py --backup-dir ../backups_encoding

# Me arrependi: desfazer a última execução
python convert_utf8.py --restore
```

### Códigos de saída

| Código | Significado |
|--------|-------------|
| `0` | Limpo (ou correções aplicadas com sucesso). |
| `1` | Havia problemas (em `--check`) ou alguma gravação falhou. |
| `2` | Erro de uso (ex.: caminho inexistente). |

## Como funciona

### Detecção de codificação

Para cada arquivo, na ordem:

1. **BOM primeiro.** Se houver um *Byte Order Mark* (UTF-8/UTF-16/UTF-32), ele define a codificação. Isso vem antes do teste de binário porque arquivos UTF-16 têm muitos bytes `NUL` legítimos.
2. **UTF-8 estrito.** Sem BOM, tenta decodificar como UTF-8. Se passar, o arquivo é UTF-8 — aí entra a checagem de mojibake e NFC.
3. **Legado.** Se não for UTF-8 válido, decodifica como **CP1252** (e cai para **Latin-1** se falhar) e reescreve em UTF-8.

### A tabela de mojibake (a parte importante)

Em vez de aplicar o truque `encode('cp1252').decode('utf-8')` no arquivo inteiro — que é **tudo-ou-nada** e pode corromper texto correto —, o ConvertUTF-8 usa uma tabela de substituição **gerada por código**: para cada caractere correto, ele calcula como aquele caractere *aparece quando quebrado* e monta o mapa `quebrado → correto`.

Isso é seguro por construção: toda chave gerada tem a forma `Ã`/`Â`/`â€` **seguida de um byte que não é letra ASCII**. Combinações assim **nunca** aparecem em português correto — então `SÃO PAULO` e `OPÇÃO` passam intactos e nunca são alterados. A correção roda em múltiplas passadas para tratar corrupção dupla (`Ã£Â©`).

### Acentuação (NFC)

Um acento pode estar **precomposto** (`á` = um único código) ou **decomposto** (`a` + acento combinante, comum em conteúdo vindo de macOS). São idênticos na tela, mas a forma decomposta quebra busca, `grep` e comparação de strings. A normalização **NFC** unifica os dois.

## Segurança

A ferramenta reescreve arquivos, então segue alguns cuidados por padrão:

- **Confirmação obrigatória** com prévia das mudanças antes de qualquer gravação.
- **Backup `.bak`** ao lado de cada arquivo alterado (desligável com `--no-backup`).
- **Gravação atômica**: escreve em um arquivo temporário e faz `os.replace`, de modo que uma execução interrompida nunca deixa o arquivo pela metade.
- **Não segue links simbólicos** e ignora pastas como `.git`, `node_modules` e `vendor`.
- **Pula binários** (detecta bytes `NUL` e alta proporção de caracteres de controle), mesmo que tenham uma extensão de texto.
- **Preserva as quebras de linha** originais (LF ou CRLF) byte a byte.

## Limitações

- **CP1252 × Latin-1** é ambíguo só pelos bytes — a recuperação é *best-effort* (CP1252 primeiro, que cobre aspas e travessões tipográficos). Quase sempre certo para português.
- **UTF-16 sem BOM** não é detectado automaticamente (seria adivinhação frágil). Por segurança, esses arquivos são pulados em vez de arriscar corrompê-los.
- **`U+FFFD` (�)** indica dado que **já se perdeu** antes da ferramenta entrar em ação. Isso é irreversível: o ConvertUTF-8 apenas **avisa** quais arquivos têm esse caractere; não há como recuperar a informação original.

## Evite que volte a acontecer

Limpar os arquivos resolve o sintoma. A causa costuma ser uma destas:

- **Editor** abrindo/salvando com codificação errada → configure o padrão para **UTF-8**.
- **PHP** lendo/gravando arquivo sem tratar a codificação (ex.: `file_get_contents` + regravação) → garanta UTF-8 em todo o I/O.
- **FTP em modo ASCII** ao subir arquivos → use modo binário.
- **`.gitattributes`** tratando como texto e convertendo um arquivo que deveria ser intocado.

Rodar `python convert_utf8.py --check` em um *hook* de pre-commit ajuda a barrar regressões antes que entrem no repositório.

## Licença

MIT.
