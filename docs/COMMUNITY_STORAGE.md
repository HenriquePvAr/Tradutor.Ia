# Comunidade e armazenamento privado no Google Drive

Usuários podem publicar PDFs traduzidos em uma comunidade. **Apenas PDFs escolhidos
explicitamente** para publicação são enviados ao Google Drive privado do administrador.

## Arquitetura

    UI  →  /api/community/publish  →  post draft + job community_publish (fila SQLite)
                                          ↓ worker independente
                                     community_publish_runner (upload retomável)
                                          ↓ StorageProvider
                                     Google Drive privado  (só bytes do PDF)

    Leitura:  UI → /api/community/posts/<id>/pdf → backend → StorageProvider → stream

- **Banco** (`.cache/runtime/community.sqlite3`) é a **fonte de verdade**: posts,
  arquivos, moderação, visibilidade, audit log. Nunca guarda o PDF como BLOB — só uma
  referência (`storage_file_id`).
- **Google Drive** guarda **somente PDFs publicados** — nunca OCR, imagens, cache ou
  relatórios. Os arquivos permanecem **privados**; nenhuma permissão `anyone` é criada e
  nenhum link do Drive é exposto. O feed **nunca** consulta o Drive.
- **Worker** faz o upload de forma independente da UI; fechar a UI não interrompe o
  upload. Concorrência global 1.

## Storage abstraction

`community_storage.StorageProvider` define a interface (upload resumable, stream/range,
stat, trash, delete, exists, health). Implementações: `FakeStorageProvider` e
`FilesystemStorageProvider` (testes/local), `GoogleDriveStorageProvider` (produção). Trocar
por S3/R2 no futuro não exige mudar posts nem APIs.

## Publicar

No histórico, um capítulo com PDF mostra **“Publicar na comunidade”**. A UI envia
identificadores (slug/job), **nunca um caminho** — o backend resolve o PDF dentro de
`output/`, valida (dentro do root, `.pdf`, magic `%PDF`, não vazio, sem path traversal),
calcula o SHA-256 por streaming, cria o `community_file` e enfileira o job. O post fica
`publishing`; só vira `published` **após o upload e a verificação**.

## Status do post / arquivo

Post: `draft → publishing → published → unpublished/blocked/failed/deleted`.
Arquivo: `pending → uploading → verifying → verified → failed/deleting/deleted`.

## Upload retomável

Em chunks (256 KB), sem carregar o PDF inteiro na RAM; persiste `bytes_uploaded` para
retomar; heartbeat e progresso no banco; retry apenas em erros transitórios com backoff +
jitter; respeita cancelamento e parada do worker. Sobrevive ao fechamento da UI e é
reconciliado após crash do worker.

## Verificação remota

Após o upload: consulta metadata remota, confere tamanho e MIME, persiste o SHA-256 local
e o checksum do provider, marca `verified` e só então publica. Divergência → job `failed`,
post não publica, arquivo remoto **não** é destruído automaticamente (reconciliável).

## Leitura (streaming)

`GET /api/community/posts/<id>/pdf` valida status/moderação/visibilidade/autorização e
transmite do provider com `Range`/`206`, `Content-Length`, `HEAD`, `Content-Disposition`
saneado e `nosniff`. Não carrega o PDF inteiro na memória e **não expõe** o
`storage_file_id` nem credenciais.

## Feed

`GET /api/community/posts` consulta **somente o banco** (nunca o Drive). Mostra apenas
`published` e, quando a moderação estiver ativa, `moderation_status=approved`. Filtros por
série, texto, paginação. Cards expõem só metadata.

## Despublicar e excluir

- **Despublicar**: tira do feed, mantém o arquivo, preserva o audit log, reversível.
- **Excluir arquivo**: ação administrativa **separada** e explícita; move para a lixeira
  (ou remove) no provider, marca `deleted_at`. Nunca automático ao despublicar; nunca
  apaga o PDF local.

## Deduplicação e versões

Antes do upload verifica SHA-256/post/usuário/capítulo e job ativo, bloqueando duplicata
acidental. Um hash diferente é permitido como nova versão (com confirmação).

## OAuth administrativo (Desktop app)

O storage pertence à conta Google do administrador, então usa **OAuth 2.0 de usuário**
(não service account, que não pode ser dona de arquivos no Meu Drive). Use uma credencial
OAuth do tipo **Desktop app**. `drive_auth.py` roda o fluxo oficial `InstalledAppFlow` com
**redirect de loopback em `127.0.0.1` numa porta efêmera**, PKCE e acesso offline —
**nunca** OOB/copiar-colar, porta fixa nem webview embutida. O backend **nunca** dispara
OAuth durante uma requisição web.

```
python drive_auth.py authorize            # abre o navegador do sistema, faz consentimento e salva o token
python drive_auth.py status               # presenca/validade do token e config (nunca imprime valores)
python drive_auth.py create-root-folder   # cria a pasta privada do app e imprime so o ID
python drive_auth.py test-access          # confirma acesso a pasta raiz (nao faz upload)
python drive_auth.py revoke --yes         # revoga online quando possivel e remove o token local
```

Escopo mínimo `drive.file` (o app só vê arquivos que ele criou) — por isso a pasta raiz
deve ser **criada pelo próprio app** (`create-root-folder`); uma pasta criada manualmente
fora do app não é acessível com `drive.file`, e o sistema falha com mensagem clara em vez
de ampliar o escopo silenciosamente.

Token fora do repositório (`GOOGLE_OAUTH_TOKEN_PATH`), escrita atômica, permissão restrita
(0600), **nunca** em logs, no frontend ou no banco da comunidade. Refresh automático do
access token (repetido uma única vez por request em 401); `.env.example` traz **apenas
placeholders**. Token expirado/revogado → erro operacional claro pedindo `authorize`; o
backend não abre navegador sozinho.

## Reconciliação

`community_storage_reconcile.py --community-db … --jobs-db … --mode scan|report|repair-safe`.
Detecta post publicado sem arquivo, publishing sem job, upload em estado terminal,
tamanho divergente, arquivo ausente/na lixeira, duplicatas. `scan`/`report` não alteram
nada; `repair-safe` faz só correções não destrutivas. Ações destrutivas exigem confirmação
explícita separada. Não lista o Drive inteiro.

## Configuração para o smoke real (autorização futura)

`COMMUNITY_STORAGE_PROVIDER=google_drive`, `COMMUNITY_DRIVE_ROOT_FOLDER_ID`,
`GOOGLE_OAUTH_CLIENT_ID/SECRET`, `GOOGLE_OAUTH_TOKEN_PATH`, e uma autorização única via
`drive_auth.py authorize`. Localmente, o padrão `filesystem` mantém tudo privado em
`.cache/` sem Drive.

## Privacidade e moderação

Arquivos privados; o navegador acessa **o backend**, não o Drive. Estruturas de moderação
(`moderation_status`), autor, audit log e bloqueio já existem. A publicação exige ação
explícita e um aviso de responsabilidade — o sistema **não** declara automaticamente que
qualquer obra pode ser publicada.
