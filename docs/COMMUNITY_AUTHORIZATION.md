# Fronteira de autenticação e autorização da comunidade

A comunidade não usa identidade escolhida pelo navegador. Toda rota cria um
`RequestPrincipal` por meio de um `AuthProvider`; quando não há credencial válida, o
principal é anônimo e explícito. `user_id`, owner e roles são dados confiáveis somente
depois da validação da sessão pelo backend.

## Principal e providers

`RequestPrincipal` é imutável e contém:

- `user_id` normalizado;
- `authenticated`;
- roles confiáveis;
- `auth_source`;
- `session_id` opcional.

`AuthProvider` define `authenticate_request`, `require_authenticated`, `require_role` e
`require_csrf`. A implementação atual é `LocalSessionAuthProvider`. Ela é temporária,
somente para localhost, e mantém em memória o vínculo entre token aleatório, usuário,
roles, CSRF e expiração. Reiniciar o processo invalida todas as sessões.

Uma futura `SupabaseAuthProvider` deverá usar biblioteca mantida para verificar assinatura
JWT, issuer, audience, expiração e `sub`, e obter roles apenas de claims autorizadas ou do
banco. Esta branch não implementa JWT nem Supabase e não contém validação manual parcial.

## Sessão local temporária

Não existe login aberto nem admin automático. O bootstrap exige, simultaneamente:

- peer de rede em loopback;
- `COMMUNITY_LOCAL_BOOTSTRAP_SECRET` com 32 a 512 bytes, fora do Git;
- `COMMUNITY_LOCAL_BOOTSTRAP_USER_ID` definido no servidor;
- roles opcionais em `COMMUNITY_LOCAL_BOOTSTRAP_ROLES`.

`POST /api/community/auth/local-session` recebe apenas o segredo no header
`X-Tradutor-Bootstrap-Secret`. O request não aceita `user_id` ou role. Uma emissão nova
revoga a sessão anterior do usuário de bootstrap, e a sessão expira após
`COMMUNITY_LOCAL_SESSION_TTL_SECONDS` (15 minutos por padrão).

O token de sessão é criptograficamente aleatório, armazenado no servidor somente como
digest e enviado apenas em cookie `HttpOnly`, `SameSite=Strict`, com `Secure` em HTTPS.
Ele não aparece no body, URL ou logs. Um segundo cookie contém o token CSRF de
double-submit; mutações exigem também o header `X-Tradutor-CSRF`. Logout e revogação
invalidam a sessão no servidor. Se a sessão já expirou ou foi perdida no servidor, o
cleanup dos cookies continua exigindo o mesmo token CSRF no cookie e no header; logout
anônimo ou sem essa prova responde `403`.

Remova ou rotacione o segredo de bootstrap após a sessão administrativa ser emitida. Não
exponha o modo local por reverse proxy, túnel ou port forwarding: um proxy no mesmo host
seria visto como peer de loopback.

## Política de posts

A comunidade é **autenticada para ler**: `public` significa "qualquer membro
autenticado", nunca acesso anônimo. O PDF é sempre transmitido pelo backend e o arquivo
remoto permanece privado no Drive — nenhum link público é criado.

| Visibilidade | Quem lê o PDF | Requisitos adicionais |
|---|---|---|
| `public` (comunidade) | **qualquer usuário autenticado** | `published`, moderação `approved`, arquivo `verified` |
| `unlisted` | owner ou admin confiável | `published`, moderação `approved` ou `pending`, arquivo `verified` |
| `private` | owner ou admin confiável | `published`, moderação `approved` ou `pending`, arquivo `verified` |

Um leitor autenticado de um post `public` é **apenas leitor**: publicar, editar,
despublicar, excluir/trash e moderar continuam exigindo owner (ou admin conforme
política) — um membro comum recebe `404` ao tentar administrar post alheio.

Moderadores não recebem acesso a PDFs private/unlisted por padrão. Podem ser autorizados
somente por uma política administrativa explícita. Posts draft, publishing, unpublished,
blocked, failed ou deleted nunca entregam PDF; moderação rejected/blocked também nega.

**Anônimo nunca lê nada da comunidade** (nem `public`): recebe `401` e é convidado a
entrar. Um usuário autenticado sem acesso a um post restrito recebe `404`, evitando
enumeração. CSRF inválido recebe `403`. A mesma regra é usada por HEAD, GET completo e
Range — a autorização precede o parsing de Range, então um anônimo recebe `401` antes de
qualquer `416`. Respostas negadas não incluem tamanho real, Range, filename, checksum,
título privado ou `storage_file_id`.

O feed da comunidade (`GET /api/community/posts`) também exige autenticação: um anônimo
recebe `401`; um membro autenticado vê apenas posts `public` + `published` + `approved` +
`verified`, nunca private/unlisted/não-publicados.

Respostas de PDF usam `Cache-Control: private, no-store` e `Vary: Cookie`; as respostas
sensíveis negadas (`401/403/404/416`), `my-posts` e metadata de sessão usam a mesma
proteção contra cache compartilhado. HEAD autorizado responde a partir da metadata
verificada no banco, sem abrir stream no provider e sem incrementar visualizações.

## Ordem antes do storage

O caminho de leitura é fixo:

1. validar a sessão e construir o principal;
2. buscar metadata mínima no SQLite;
3. executar `can_read_post`/política central;
4. exigir arquivo `verified`;
5. validar o Range;
6. somente então construir e chamar o `StorageProvider`.

Assim, uma negação não constrói o provider, não atualiza OAuth, não faz HTTP e não acessa
Google Drive nem o filesystem de PDFs.

## Feed, ownership e administração

O feed público consulta somente o banco e exige public + published + approved + arquivo
verified. Busca e filtros não ampliam esse conjunto. `my-posts` sempre usa
`principal.user_id`; query/body não conseguem escolher outro usuário.

Criar/publicar, despublicar e excluir/trash exigem principal autenticado, ownership (ou
admin conforme política), CSRF e audit event com o ator real. Roles de admin/moderator vêm
somente do provider. Reconciliação por CLI continua sendo uma operação local do processo;
qualquer endpoint administrativo futuro deve usar `authorize_admin_operation` ou
`authorize_moderation`.

Outputs legados e pastas escolhidas apenas por slug não possuem owner confiável no banco;
somente admin pode adotá-los para publicação. Um usuário comum só pode publicar a partir
de um source job que tenha `community_owner_id` gravado pelo backend e igual ao principal.

Os headers `X-User-Id`, `X-Role`, `X-Admin` e `X-Owner` não autenticam e não concedem
privilégio. Campos `user_id`, `role`, `roles`, `actor_id`, `owner`, `admin` e `moderator`
em bodies de publicação são rejeitados.

O backend carimba o owner de um job quando `/api/ui/run` ou `/api/ui/queue/add` recebe uma
sessão válida com CSRF; campos equivalentes enviados no payload são ignorados. O source
job precisa ser uma tradução `finished` ou `review_required`, ter `exit_code=0` e um
`pdf_path` registrado pelo runner. `job_manifest.json`, job id, run id, status e caminho
devem coincidir. A publicação usa exatamente esse PDF e nunca adota outro por glob. Jobs
inexistentes, alheios, incompletos ou com artefato inválido retornam o mesmo `404`.

O job nasce em `STAGING`, estado não claimável, com uma lease temporária do processo da
API. Uma transação única no banco da comunidade classifica idempotência/duplicata e, se
necessário, vincula `upload_job_id`, arquivo `pending` e post `publishing`; somente depois
o job passa a `queued`. Se a API cair ou uma escrita entre bancos falhar, o worker
reclassifica o vínculo autoritativo: tentativa ativa vai para a fila, conclusão/falha é
reconciliada e staging sem vínculo só é descartado depois que seu processo criador não
está mais vivo.

A conclusão publica post e arquivo em uma transação condicional: post ainda
`publishing`, arquivo ainda mais recente e job ainda atual. `unpublish` invalida essa
condição e solicita cancelamento, de modo que uma conclusão atrasada nunca republica o
post. Controles genéricos da fila da UI não cancelam nem retomam jobs internos
`community_publish`.

Dois pedidos normais para o mesmo owner e source job reutilizam atomicamente o mesmo post
e, se já houver upload ativo, devolvem o mesmo `file_id`/`job_id`; não criam dois uploads.
Uma versão forçada é uma ação explícita e mantém a proveniência do source job.

O runner cria primeiro um snapshot temporário do PDF e confirma tamanho e SHA-256 antes
de construir o provider. Assim, uma troca do arquivo local entre o clique e a execução é
rejeitada sem enviar seus bytes. Falhas e recuperações registram nos eventos o usuário
que solicitou a publicação, não uma identidade enviada pelo cliente nem o worker.

Ao criar qualquer subprocesso de runner, o worker entrega um start gate fechado. O child
só executa o job depois que seu PID/create-time foi persistido; se o worker morrer nessa
janela, o gate nunca abre e o child termina sem executar. Toda saída precoce que ainda
deixe o job em voo é reconciliada para `interrupted` recuperável (ou `cancelled` quando
solicitado), e a limpeza usa o handle/PID/fingerprint do child para não deixar órfãos.

## Bind de rede

O middleware atua diretamente em scopes ASGI HTTP e WebSocket e também exige o opt-in
explícito; um provider forte, isoladamente, não libera peers externos.

`TRADUTOR_UI_HOST` usa `127.0.0.1` por padrão. `0.0.0.0` ou outro bind externo exige
`TRADUTOR_ALLOW_EXTERNAL_BIND=1` e um provider configurado que declare autenticação forte
para exposição externa. `LocalSessionAuthProvider` nunca satisfaz esse requisito, mesmo
com a flag explícita. Até a integração completa com Supabase e a proteção das demais
rotas locais, o servidor deve permanecer em loopback.

Além da validação no entrypoint, um middleware global rejeita peers não-loopback quando
o provider não declara autenticação externa forte. Isso mantém o app fechado mesmo se
alguém importar `app_ui:app` em outro runner ASGI com um bind inseguro.
