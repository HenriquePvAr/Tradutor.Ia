# Interface da comunidade social

A área de comunidade consome **exclusivamente** os endpoints do backend social
(`/api/community/social/*`). O navegador **nunca** fala com a Supabase Data API
diretamente; o SDK Supabase no frontend é usado **apenas** para autenticação/sessão/refresh/
logout e para obter o access token.

## Arquitetura

```
Frontend (social_community.js + social_api.js)
  → backend Tradutor.Ia (/api/community/social)
  → JWT validado
  → Supabase Data API
  → PostgreSQL + RLS
```

- **`static/social_api.js`** — cliente do backend. Obtém o token pela camada de auth
  (`currentAccessToken`), anexa `Authorization: Bearer`, chama só `/api/community/social`,
  **remove** qualquer campo de propriedade/identidade/status do body (owner_id, author_id,
  user_id, reporter_id, recipient_id, role, admin, status, …) e mapeia erros para mensagens
  amigáveis (401/403/404/409/422/429/503) sem jargão técnico. Nunca envia a publishable
  key nem loga o token.
- **`static/social_community.js`** — a UI (ESM). Renderiza todo conteúdo de usuário com
  `textContent`/DOM seguro (nunca `innerHTML` com dados de usuário). Só monta quando o
  provider de auth é o Supabase (em modo local-session, a comunidade SQLite antiga é
  preservada). Estado leve em memória; `AbortController` no logout/expiração.
- Sem framework novo (o projeto usa JS puro + módulos ESM); reutiliza o toast e o modal de
  auth existentes.

## Navegação

Abas: **Explorar** (feed), **Favoritos**, **Continuar lendo** (histórico), **Minhas
obras**, **Meu perfil**, **Notificações**. Cabeçalho com título, e-mail do usuário e
**Sair**. Responsivo (grid que colapsa em 1 coluna no mobile, nav rolável, modais
adaptados).

## Autenticação e proteção

Sem login, a comunidade mostra um **gate** com **Entrar**/**Criar conta** (reusa o modal do
`auth_ui.js`) e **não** chama nenhum endpoint social. Ao expirar (401): cancela chamadas
pendentes, limpa o estado sensível e mostra "Sua sessão expirou. Entre novamente." — sem
loop de refresh. Logout limpa o estado social.

## Seções

- **Feed** — `GET /feed`, cards (título, sinopse, status, data, capa placeholder por
  inicial), favoritar, abrir obra; skeleton/vazio/erro/retry; paginação por cursor
  ("Carregar mais"); só community.
- **Obra** — `GET /works/{id}` + `/chapters`; favoritar/denunciar; controles do owner
  (editar, publicar/despublicar, excluir lógico, novo capítulo) escondidos para terceiros.
- **Minhas obras** — `GET /my-works`; criar/editar (title, slug, synopsis, status) — nunca
  owner_id/cover_path/timestamps; exclusão lógica com confirmação.
- **Capítulos** — listar/criar/editar/excluir do owner; nunca storage_file_id/checksum/PDF.
- **Comentários** — listar/comentar/responder (2 níveis visuais), editar/apagar o próprio,
  curtir; "(editado)"; **"Comentário removido"** para soft delete (respostas preservadas);
  textarea com contador; conteúdo escapado.
- **Curtidas** — capítulo e comentário, idempotentes, com update otimista + rollback e
  trava de duplo clique. Sem contagem inventada (o backend não a devolve).
- **Favoritos** — favoritar/desfavoritar (idempotente) + tela de favoritos.
- **Histórico / Continuar lendo** — `GET /history` (progresso, última leitura, concluído).
  A atualização automática de progresso virá com a ligação do leitor ao PDF (fase futura).
- **Perfil** — editar username/display_name/bio/theme_color/show_favorites/show_history/
  allow_profile_comments; avatar por iniciais + cor do tema; contador de bio; **onboarding**
  quando username/display_name faltam; conflito de username → 409 tratado. Perfis públicos
  por `GET /profiles/{username}`.
- **Denúncias** — modal para obra/capítulo/comentário (reason/details); reporter/status
  nunca enviados.
- **Notificações** — `GET /notifications`, destacar não lidas, marcar como lida
  (`PATCH /notifications/{id}/read`); sem criação pelo frontend; sem Realtime.

## Propriedade

A UI **esconde** ações que o usuário não possui, mas o backend + RLS são a autoridade —
testes garantem que chamadas manuais a conteúdo alheio continuam negadas (404). Nenhum
`owner_id`/`author_id`/`user_id`/`reporter_id`/`recipient_id`/`role` é montado pela UI; são
derivados do JWT no backend.

## Ligação com o PDF (pendente)

Não há associação segura entre os `chapters` sociais e os PDFs antigos (SQLite/Google
Drive): não existe endpoint social de PDF, `private.chapter_assets` está vazio e os
`chapters` públicos não têm coluna de arquivo. Por isso **não há botão "Ler"**; o owner vê
um aviso discreto "Arquivo ainda não vinculado" e terceiros não veem detalhes internos. A
ligação `chapter → PDF` será uma **fase separada**.

## Estados, acessibilidade e segurança

- Loading/skeleton, vazio, erro+retry, operação em andamento, confirmações discretas via
  toast (nunca `alert()`); mensagens sem RLS/SQL/JWT.
- Teclado, foco visível, `aria-label`/`aria-live`, modais com focus trap, Escape fecha e
  devolve o foco, `prefers-reduced-motion`, áreas tocáveis adequadas.
- Zero acesso direto a `/rest/v1`; zero secret/service_role; nenhum token/refresh em
  console; nenhum `innerHTML` com conteúdo de usuário; nenhum link do Drive; nenhum campo
  privado exposto.

## Como testar

- `pytest -q` inclui `test_social_community_ui.py` (contrato/segurança do fonte JS/HTML/CSS).
- `node --check static/tradutor_ui.js` e checagem de sintaxe ESM dos módulos novos.
- Smoke de runtime no navegador: o gate monta, sem erros no console e **sem** chamadas a
  `/rest/v1`; o fluxo autenticado completo é coberto pelo e2e do backend (fase anterior,
  com JWTs locais reais + RLS).

## Limitações

- Sem runner DOM no projeto → testes de frontend são de contrato/fonte; o comportamento em
  runtime é validado por smoke de navegador + e2e do backend.
- Ligação `chapter → PDF`, Supabase Storage, uploads (avatar/banner/vídeo/capa), Realtime,
  moderação e migração do SQLite ficam para fases futuras (placeholders visuais nesta fase).
