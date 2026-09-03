# Quality Freeze

> **Base verificada:** `5ebd77e` (branch `beta/packaging`) · **Revisado em:** 2026-09-03
>
> [Voltar ao índice](README.md) · [Qualidade e validação](QUALITY_AND_VALIDATION.md)

## O que é o Quality Freeze

**Estado: ATIVO.**

O Quality Freeze é um congelamento deliberado do comportamento de produção do pipeline.
Enquanto ele estiver ativo, o repositório trata o conjunto "código de produção +
dependências pinadas + resultado de qualidade observado" como um contrato único: qualquer
alteração em qualquer um dos três invalida a evidência de qualidade dos outros dois.

Motivo concreto: parte das decisões visuais compara medidas de pixel contra limiares
literais calibrados no ambiente congelado (ver `OPENCV-THRESHOLD-SENSITIVITY-001` em
[Qualidade e validação](QUALITY_AND_VALIDATION.md)). A suíte de testes prova que a
lógica continua correta; ela **não** prova que a saída visual continua idêntica. Só um
E2E real prova isso.

### O que o freeze proíbe

- alterar comportamento de produção sem novo E2E real;
- alterar versão ou variante das bibliotecas de imagem/CV pinadas;
- trocar engine de OCR, provider de tradução ou modelo de reconstrução por padrão;
- "aproveitar" uma missão de outro tipo para refatorar o pipeline.

### O que o freeze permite

- documentação;
- testes que não mudam comportamento;
- correções de infraestrutura de teste;
- trabalho de empacotamento que não altere o código executado.

---

## Snapshot do freeze

| Marco | Commit | O que é |
| --- | --- | --- |
| **Production base** | `ee71cfa` | Último commit que define o **comportamento de produção** congelado. É esta a base cuja saída foi provada por E2E real. |
| **Test compatibility** | `afe0e03` | Ajustes de compatibilidade da suíte sobre a base de produção. **Não** é um commit de comportamento de produção. |
| **Post-freeze cleanup** | `a6a46e4` | Limpeza pós-freeze. **Não** é um commit de comportamento de produção. |

`afe0e03` e `a6a46e4` são posteriores à base de produção e existem para manter a suíte e o
repositório saudáveis. Nenhum dos dois deve ser citado como "o commit que produziu o
resultado de qualidade" — esse papel é exclusivamente de `ee71cfa`.

## Evidência

| Item | Resultado |
| --- | --- |
| E2E real | job `c5d92ff5…` |
| `python -m pytest` | 4514 passed · 34 skipped · 0 failed |
| `python -m unittest discover` | 3914 OK |
| Suítes `.mjs` | 14/14 verdes (`node --experimental-vm-modules`) |
| P28 | PASS |
| P31 | PASS |
| P32 | PASS |
| Worktree na verificação | limpo |

Os comandos exatos estão em [Desenvolvimento](DEVELOPMENT.md#testes) e
[Testes](TESTING.md).

## Como sair do freeze

O freeze não é levantado por uma suíte verde. Ele é levantado quando:

1. a variante de OpenCV efetivamente carregada em runtime for única e igual à declarada
   (ver `OPENCV-VARIANT-SHADOWING-001` em [Riscos conhecidos](DEVELOPMENT.md#riscos-conhecidos));
2. existir instalação limpa em Windows reproduzível a partir dos manifests;
3. um novo E2E real for executado sobre essa instalação limpa e comparado com a evidência
   acima.

### Estado em `beta/packaging` (#84F53R)

| Condição | Estado |
| --- | --- |
| 1 — OpenCV único e igual ao declarado | **ATENDIDA no perfil Beta** — `requirements-beta.txt` resolve uma única distribuição (`opencv-python==5.0.0.93`), `pip check` limpo, `scripts/check_runtime_profile.py` verde, `cv2.__version__ == 5.0.0` |
| 2 — instalação limpa reproduzível | **PARCIAL** — o perfil instala e roda limpo a partir dos manifests; falta a máquina Windows limpa |
| 3 — novo E2E real sobre essa instalação | **EXECUTADO — não reproduziu os gates congelados** (ver abaixo) |

#### E2E sob o runtime Beta — job `902b149e`

Shadow Slave — Chapter 1.5, DeepL `quality_optimized`, modo Qualidade, RapidOCR, escopo
COMPLETO, cache OFF, force ON, contexto ON, descoberta HTTP. Rodou em 6,2 min sobre
`.venv-beta`.

| Item | Resultado |
| --- | --- |
| Páginas | **35/35**, `page_gate_passed: true`, `accounting_closed: true`, 0 erro de página |
| PDF | **válido** — `%PDF-1.4`, 35 objetos de página, 10,27 MB |
| Provider | `deepl`/`quality_optimized`, sem fallback, sem mismatch |
| Paddle | **não instalado, não importado, não usado** — 0 módulos Paddle nos 5 processos do job; `fallbacks_to_paddle_mobile/full = 0` |
| OpenCV em runtime | **uma única** `cv2.pyd`, de `.venv-beta` (`cv2 5.0.0`) |
| Nomes próprios | preservados (`proper_noun_preserved: 1`) |
| Texto corrompido / mistura de idioma | `mixed_language_items: 0`, `text_overflow_items: 0` |
| Regressão visual severa | nenhuma — `pages_visual_validation_failed: 0` |
| **P28 / P31 / P32** | **NÃO reproduzem o PASS congelado** — `structured_review` em `p028:BALAO_1`, `p031:BALAO_1`, `p032:BALAO_2` |
| Resíduo físico | `ordinary_story_english_visible: 1` em `p032:BALAO_2` (`source_segmentation_incomplete`, OCR colou `AGATETHROUGHWHICH`) |
| Gate físico | `physical_gate_passed: false`, `physical_decision: "review"` — **fail-closed correto**: recusou renderizar o candidato ruim em vez de publicá-lo |

**O freeze continua ATIVO.** A condição 1 foi atendida *trocando* o `cv2` efetivamente
carregado de 4.10.0 para 5.0.0 — exatamente a mudança que
`OPENCV-THRESHOLD-SENSITIVITY-001` diz exigir um E2E real. O E2E agora existe e **não**
reproduziu os gates congelados: a baseline de qualidade **não** está preservada sob o
runtime Beta.

Atribuição ainda em aberto: um único job não separa a troca de `cv2` de (a) não determinismo
do DeepL entre execuções e (b) mudança do conteúdo na fonte desde julho. Fechar essa questão
exige um E2E de controle sobre `cv2 4.10.0` com o mesmo capítulo — não feito aqui.

Até lá, qualquer mudança de comportamento de produção precisa carregar seu próprio E2E.
