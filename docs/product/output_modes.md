# Modos de saída e pipeline seletivo

> Estado: os **cinco modos executam de verdade** e agora têm **interface de presets**:
> `Tradução pronta`, `Apenas Clean`, `Apenas tradução`, `Tradução + Clean`, `Profissional`
> e `Personalizado`. Base verificada: `b4da199`. Revisado em: 2026-10-04.

## Interface — "O que você quer gerar?"

A tela de nova tradução tem um seletor de cartões com seis opções. Cada preset envia um
`output_plan` no payload; o backend (`yomu/runtime/output_plan.py`) é a **única fonte de
verdade** e normaliza/valida o plano, sobrepondo os campos legados
(`output_profile`/`output_format`/`translation_enabled`).

| Cartão | Envia | Operações efetivas |
| --- | --- | --- |
| Tradução pronta (padrão) | `output_plan=traducao_pronta` | translate, clean, typeset, pdf |
| Apenas Clean | `output_plan=apenas_clean` | clean (0 provedor, 0 YK) |
| Apenas tradução | `output_plan=apenas_traducao` | translate (dados de tradução) |
| Tradução + Clean | `output_plan=traducao_clean` | translate, clean |
| Profissional | `output_plan=profissional` | translate, clean, typeset, pdf, psd, qc |
| Personalizado | `output_plan=personalizado` + `operations[]` | o que o usuário marcar (com dependências) |

- **Personalizado** revela caixas de seleção (traduzir / imagens limpas / aplicar texto /
  PDF / PSD / QC). Dependências são aplicadas e explicadas ("Também será necessário:
  traduzir."); a etapa auto-adicionada aparece em destaque. O backend revalida.
- **Progresso plan-aware**: etapas não solicitadas não aparecem (um `Apenas tradução` não
  mostra "Reconstruindo páginas"/"Preparando documento"; um `Apenas Clean` não mostra
  "Tradução"). Dirigido por `effective_operations`.
- **Tela final orientada por artefatos produzidos** (`completion_summary.artifacts` com
  `available`): lista só o que foi gerado (sem botão/linha fantasma). `Apenas tradução`
  mostra "Tradução" (o arquivo `translation/translation.json`).
- **Histórico** mostra o modo (ex.: "Apenas Clean"); jobs legados exibem o modo inferido.
- **Sem termos internos** na escolha (nada de Gemini/Oracle/Chrome/provider/typeset).
  Download-only permanece como opção separada em "Modo de processamento".

## O que o usuário escolhe

Cada capítulo é processado segundo um **plano de saída** — o conjunto de etapas que o
usuário pede para gerar. Um plano é escolhido por um *preset* ou montado de forma
*personalizada*. O plano é a única fonte de verdade: ele decide se o provedor de tradução é
chamado, se há consumo de YK, qual perfil de artefatos é produzido e qual o formato final.

| Preset | Etapas | Tradução/YK | Estado |
| --- | --- | --- | --- |
| **Tradução pronta** | traduz + limpa + aplica texto + PDF | sim (1 YK/pág.) | IMPLEMENTADO (perfil `automatic`) |
| **Profissional** | tudo + PSD editável + QC + revisão | sim | IMPLEMENTADO (perfil `professional_qc`) |
| **Apenas Clean** | remove o texto, entrega páginas limpas (PNG) | **não — 0 provedor, 0 YK** | IMPLEMENTADO (render clean, sem typeset) |
| **Tradução + Clean** | traduz e limpa, **sem aplicar o texto** (PNG) | sim | IMPLEMENTADO (CLEAN/TYPESET desacoplados) |
| **Apenas tradução** | só os dados de tradução (JSON), sem imagens | sim | IMPLEMENTADO (`output_format=translation`) |
| **Apenas download** | baixa e valida as páginas originais | não | IMPLEMENTADO (`download_only`) |
| **Personalizado** | combinação escolhida pelo usuário | depende | IMPLEMENTADO para combinações executáveis |

## Apenas tradução — contrato de resultado

O entregável é `translation/translation.json` (origem→alvo por região/página + estado de
validação). **Não** há clean, typeset, imagem traduzida nem PDF — e o sistema reconhece isso
como um job **concluído e útil**, não como falha por ausência de PDF/PNG.

- **Sucesso:** a conclusão é *plan-aware*. Para translate-only, `output_contract_ok =
  yk_reservation.translation_ready(output_dir)` — o **mesmo** sinal de "tradução durável
  persistida" já usado na recuperação de falhas.
- **YK:** permanece ligado a *provider success + tradução válida persistida*, **nunca** a um
  artefato visual. Falha antes do provedor → 0 YK. Provedor falhou → 0 YK. Retry/re-export
  com alvo persistido → 0 provedor, 0 YK adicional.

## Regras de dependência (aplicadas por `normalize_output_plan`)

- Aplicar texto (**typeset**) exige **traduzir** — não há como diagramar texto não traduzido.
- **PSD** e **QC** exigem o perfil Profissional e puxam traduzir+aplicar (as camadas de
  texto carregam a tradução).
- **PDF** exige ao menos uma etapa que produza páginas (aplicar texto *ou* limpar); um PDF
  sem páginas é descartado.

## Por que "Apenas Clean" não cobra YK

`Apenas Clean` define `translation_enabled=false`. No limite real do provedor
(`benchmark_pipeline._resolve_translation_runtime`) isso significa **nenhum objeto de
provedor é instanciado** e o commit de carteira (único ponto de consumo de YK) está dentro
do ramo `if translation_enabled` — portanto um run de limpeza nunca chama o provedor nem
consome YK. Provado em `tests/test_output_plan.py`.

## CLEAN é agnóstico de provedor

A etapa de limpeza é uma capacidade abstrata (`Operation.CLEAN`). Hoje é a limpeza local da
renderização; uma limpeza remota futura troca a implementação sem alterar o contrato nem a
UI. Não há — e não deve haver — ramificação por provedor (`gemini`/`oracle`/`chrome`) no
plano de saída ou na UI.

## Compatibilidade

Jobs antigos sem `output_plan` continuam funcionando: o plano é **inferido** a partir dos
campos históricos (`download_only`, `translation_enabled`, `output_profile`,
`output_format`) por `plan_from_config`, então a tela de conclusão sempre sabe o que foi
pedido.

## Separação CLEAN / TYPESET (implementada)

Antes, `render_analyzed_image` sempre limpava **e** aplicava o texto no mesmo passo. Agora
recebe `typeset=`:

- `typeset=true` (fluxo automático/profissional): limpa e aplica o texto — inalterado.
- `typeset=false` (Apenas Clean, Tradução + Clean): limpa a arte e **não desenha nenhum
  glifo**; a página renderizada é byte-a-byte igual ao buffer limpo (provado por teste).

O pipeline é orientado por `effective_operations` (via `YOMU_EFFECTIVE_OPS`): jobs legados/
automáticos não têm esse env e tomam exatamente o caminho de antes (zero regressão). Só um
job com plano explícito ativa os gates (render clean-only, persistência só de CLEAN, PDF
desligado, manifesto com artefatos pedidos pelo plano).

## Próximos passos (PLANEJADOS — não implementados)

1. **UI de presets**: superfície para escolher o preset/etapas, reusando o componente de
   cartão `output-profile-card`; envia `output_plan` no payload. O backend já aceita, valida
   e **executa** os cinco modos de qualquer cliente.
2. **Tela de conclusão / progresso**: exibir só os artefatos solicitados e marcar os demais
   como `NÃO SOLICITADO` (não como ausente/falha), lendo `effective_operations`.
3. **Re-export**: a partir de um `Apenas tradução`/`Tradução + Clean` concluído, gerar outro
   formato (typeset/PSD) reutilizando a tradução persistida — 0 provedor, 0 YK adicional.

