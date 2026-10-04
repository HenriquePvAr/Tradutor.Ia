# YOMU_AUTH_LOGIN_STARTUP_PHYSICAL_ROOT_CAUSE_FIX_V1 — Final Report

**Base:** `3f38715` · **Branch:** `fix/auth-login-startup-physical-v1` · **Push:** NO

## Owner evidence (authoritative)
- EXE físico: history/selection/delete PASS; "login FAIL".
- No EXE de profile limpo apareceu uma tela de login; owner: **"é uma tela mock/placeholder"** e apontou o **Beta 18** como o login correto (personagem vermelho).

## Root cause (provada, dois sintomas distintos)

### 1. "App abre sem login" — NÃO é bug de produto
WebView2 usa profile **persistente e compartilhado** (`%LOCALAPPDATA%/YomuSekai/webview-profile`, `private_mode:False`). O profile do owner tem uma **sessão Supabase persistida** (`sb-*-auth-token`). O SDK restaura no startup → autenticado → pula login. Isso é **correto** pelo contrato "Lembrar de mim". Com profile limpo isolado, o backend responde `auth_state: "unauthenticated"` e serve o login. `signOut({scope:'local'})` limpa a sessão (logout+restart → login). **Fix: nenhum** (infra; testar sempre com profile isolado, nunca apagar o do owner).

### 2. "O login é um mock" — ESTE era o bug, agora corrigido
A arte de fundo do login (`static/assets/auth-background.jpg` e `auth-background-hd.png`) é **excluída do git** pelas regras catch-all `*.jpg`/`*.png` no `.gitignore`. Builds feitos de **worktree/checkout limpo** (os EXEs destas missões audit-first, CI, clone novo) **não empacotam a arte** → `.auth-login-page` renderiza escuro/vazio = "mock". Os **releases oficiais (Beta 18)** só ficam corretos porque são buildados do **working dir principal**, onde os dois arquivos existem localmente como untracked.

**Evidência convergente:** CSS de `auth-background` e HTML do login **idênticos** Beta 18 vs atual; arte com **mesmo md5** em todos os builds; diffs de JS de auth são **melhorias aditivas** (sem regressão); o EXE "mock" não tinha `auth-background*` empacotado; meu worktree limpo também não tinha (gitignorado); precedente: `comic-original.png`/`comic-translated.png` já são rastreados apesar de `*.png`.

**A decisão de startup/auth sempre esteve correta** — login sem-sessão aparece e o form autentica no Supabase real. O que faltava era a **arte versionada**.

## Fix aplicado (diff mínimo, root-cause)
- `.gitignore`: negações `!static/assets/auth-background.jpg` e `!static/assets/auth-background-hd.png`.
- `static/assets/auth-background.jpg` e `auth-background-hd.png`: agora **rastreados** (git add), igual ao precedente dos `comic-*.png`.
- **Zero mudança de código** (Python/JS/CSS intactos).

Resultado: todo build limpo (worktree, CI, clone) passa a empacotar a arte → login renderiza correto em qualquer lugar.

## Testes
- Auth/login focados: **600 passed, 9 skipped, 0 failed**.
- Regressão completa de 5807 testes: N/A — nenhuma lógica de código mudou (apenas 2 binários versionados + negação de gitignore).

## Entregável físico
EXE buildado do worktree (com a arte) via `.venv-beta` PyInstaller 6.22.2 → `C:\Users\henri\YomuExperimental\auth-login-startup-physical-v1\dist\YomuSekai\`. Aberto com profile de teste limpo e isolado para confirmação visual do owner.

**AWAITING_OWNER_CONFIRMATION** — não declarar PASS físico antes do owner ver o login com a arte.

## DOCUMENTATION IMPACT
TECHNICAL DOC:   NOT REQUIRED
USER GUIDE:      NOT REQUIRED
SCREENSHOTS:     NOT REQUIRED
WHY: fix é versionar assets já existentes + negação de .gitignore; sem mudança de comportamento, API, config, UI ou fluxo documentado. A arte do login já era a de produção.
