# Saídas e artefatos

Este documento descreve o que os perfis atuais solicitam e como identificar os arquivos de uma
execução. Para o usuário, o resumo é simples: **Automático** é o resultado para leitura/publicação;
**QC Profissional** acrescenta material para revisão e finalização manual.

## Perfis

| Perfil | Resultado solicitado |
| --- | --- |
| `automatic` — **Automático** | PDF traduzido (`final_pdf`) e imagens traduzidas (`translated_image`). |
| `professional_qc` — **QC Profissional** | Os itens do Automático, imagens limpas (`clean_image`), PSD editável (`editable_psd`), Quality Report (`quality_report`) e Review Manifest (`review_manifest`). |

O perfil é independente do modo de processamento. **Download-only** coleta e valida as páginas
originais, sem OCR, tradução/provider ou YK, e força o perfil Automático sem produzir seus
artefatos de tradução.

## Artefatos e caminhos

O Artifact Contract V2 (`artifact_contract.py`) define os caminhos canônicos relativos à raiz do
output:

| Artefato | Caminho canônico | Uso |
| --- | --- | --- |
| PDF traduzido | `final/<slug>_ptbr.pdf` no resolver V2 | Leitura ou publicação. O pipeline atual ainda grava o PDF efetivo na raiz do output com o nome devolvido por `pdf_naming.py`; use o caminho do job/run. |
| Imagem traduzida | `images/translated/NNN.png` | Uma página lógica com tradução. |
| Imagem limpa | `images/clean/NNN.png` | Arte sem lettering original, antes do texto traduzido. |
| PSD editável | `psd/NNN.psd` | ORIGINAL, CLEAN e TEXT com Type Layers nativas. |
| Quality Report | `qc/quality_report.json` no resolver V2 | Diagnóstico detalhado de qualidade e findings. O pipeline atual também grava `quality_report.json` e `.html` na raiz do output (ou `regression_report.*` em execução direcionada). |
| Review Manifest | `qc/review_manifest.json` | Índice leve de QC, páginas e regiões ligadas à Review/PSD quando há identidade correspondente. |

O pipeline mantém também formatos e localizações legados por compatibilidade. Por exemplo,
imagens traduzidas continuam em `pages/` e imagens limpas podem existir como `*_cleaned.png`,
além das cópias canônicas V2. Isso não significa que todos os
artefatos estejam presentes em toda execução. Para saber o que foi produzido num run, consulte
`artifact_manifest.json` e os caminhos persistidos no job/run. O manifest V2 registra também o
caminho efetivo quando disponível; não confunda seu resolver canônico com uma promessa de que
cópias/aliases legados foram removidos. Não deduza nomes enumerando a
pasta nem renomeie arquivos manualmente.

## Job, run e artifact

- **Job** é a unidade enfileirada e acompanhada pelo worker; o manifesto do job registra sua
  identidade e o diretório de saída associado.
- **Run** é a execução concreta, identificada pelo `run_id` nos manifests quando disponível.
  Reexecuções/re-exports podem ser ligadas ao job de origem; outputs anteriores são preservados
  conforme o fluxo atual.
- **Artifact** é um arquivo produzido. No contrato V2 cada tipo tem estado (`requested`,
  `produced`, `skipped`, `unavailable` ou `failed`), perfil solicitante e, para imagens/PSD,
  identidade de página lógica. O `artifact_manifest.json` registra cobertura e caminhos sem
  transformar um arquivo ausente em sucesso.

A UI normalmente organiza os jobs sob `output/<slug>/<run_id>/`; a CLI e outputs históricos podem
usar outro layout. O caminho registrado é a autoridade para abrir o arquivo.

## Quality Report, Review e conclusão

O **Quality Report** guarda detalhes de validação, métricas e achados. O **Review Manifest** é um
índice para navegação de QC, não uma cópia integral do relatório nem o armazenamento das edições
humanas. Quando os dados fornecem identidade de página/região, os itens podem apontar à página e
à Type Layer correspondente; uma associação incerta não é inventada.

`review_required` significa que o trabalho útil foi concluído e há revisão recomendada; não é,
por si só, `failed`. O status e os artefatos essenciais persistidos determinam a conclusão.
Review é QC linguístico; inspeção visual e finalização tipográfica avançada acontecem no PSD com
Photoshop ou Photopea.

## PSD profissional

O `editable_psd_writer.py` produz o `EDITABLE_PSD` atual com os grupos `ORIGINAL`, `CLEAN` e
`TEXT`; as camadas de texto são Type Layers nativas. `professional_psd.py` é um caminho legado
separado e rasterizado, não o writer do contrato `EDITABLE_PSD`. Aviso ou substituição de fonte
não implica rasterização. Não rasterize texto para economizar espaço.

A compatibilidade física foi validada no Photoshop 2025 e no Photopea, conforme
[M15 — matriz de compatibilidade](PSD_COMPATIBILITY_MATRIX.md). O round-trip direto entre os dois
editores permanece **NOT RUN**. Tamanho e tempo medidos, além de estimativas explicitamente
marcadas para 208 páginas, estão no [relatório M17](PSD_PERFORMANCE_REPORT.md).
