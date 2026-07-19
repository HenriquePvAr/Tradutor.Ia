# Entrada por pasta local

Esta fonte permite processar imagens de capítulo que já estão no computador. Ela é uma
alternativa à URL pública: não abre Webtoon, não cria Selenium, não usa `file://` e não usa o
downloader HTTP para obter as páginas de origem. Depois de a entrada ser aceita, as etapas
normais de OCR, tradução e PDF continuam sendo as do pipeline; portanto, uma execução completa
pode usar os provedores configurados para essas etapas.

O objetivo da fronteira local é aceitar somente uma pasta de páginas autorizada, produzir um
snapshot estável e passar ao restante do pipeline apenas referências opacas. Ela não é um modo
de navegar no sistema de arquivos a partir de uma URL, nem uma permissão para uma UI exposta na
rede ler arquivos da máquina.

## Como usar

### Preparar uma raiz permitida

Por padrão, a única raiz permitida é `<repositório>/input`, **se ela já existir**. O programa
nunca cria essa pasta apenas porque foi importado ou aberto. Para usar outra raiz, configure
`LOCAL_INPUT_ROOTS` com caminhos absolutos separados pelo separador do sistema (no Windows,
normalmente `;`). Exemplo conceitual:

```text
LOCAL_INPUT_ROOTS=C:\CapitulosPermitidos;D:\OutroAcervoPermitido
```

Selecione uma subpasta de capítulo dentro dessa raiz, e não a raiz em si. A política recusa
caminhos relativos, `..`, `file:`, UNC, dispositivos Windows, links simbólicos, junctions e
qualquer caminho que escape da raiz depois de resolvido.

### Pela UI local

Na tela **Nova tradução**, escolha **Pasta local**, informe a pasta e mantenha o escopo completo.
A API aceita essa modalidade somente quando **tanto** o endereço em que a UI está ligada quanto o
navegador conectado são loopback. Uma UI ligada em rede não pode transformar uma requisição
remota em leitura de arquivos locais; nesse caso a solicitação falha com
`local_folder_requires_loopback_ui`.

O campo mostra a pasta apenas no formulário de envio. O histórico não reconstrói nem exibe esse
caminho depois: ele conserva somente o nome final da pasta, contagens e referências opacas.

### Pela linha de comando

Quando a pasta está sob uma raiz permitida, a entrada direta é:

```powershell
.\.venv\Scripts\python.exe run_webtoon.py --local-folder "C:\CapitulosPermitidos\capitulo-01" --output capitulo_local_01 --mode fast
```

`--local-folder`, URL e o manifesto interno são mutuamente exclusivos. Para uma pasta local,
`--source-candidate-id` não é aceito e a saída precisa permanecer sob `output/`. O runner de
jobs usa `run_local_folder.py --snapshot-ref ...` internamente; `--snapshot-ref` e
`--input-manifest` não são interfaces para apontar o pipeline a um caminho arbitrário.

Use uma execução real somente quando ela for desejada e o ambiente de OCR/tradução estiver
configurado. Os testes herméticos não executam esse comando contra uma pasta real nem chamam
provedores externos.

## Validação antes do snapshot

Somente arquivos regulares diretamente dentro da pasta do capítulo entram na análise. Não há
varredura recursiva. Arquivos auxiliares sem extensão de imagem permitida são ignorados; formatos
de imagem conhecidos, mas não aceitos, falham fechados em vez de serem omitidos silenciosamente.

| Controle | Comportamento |
| --- | --- |
| Ordem | ordenação natural e determinística (`1`, `2`, `10`) |
| Extensões | `.png`, `.jpg`, `.jpeg`, `.webp`, `.avif` |
| Formato real | assinatura, MIME/decodificação e coerência com a extensão |
| Integridade | `Pillow.verify()`, carregamento completo e `cv2.imdecode()` |
| Limites | até 32 MiB por arquivo, 1 GiB no capítulo e 400 páginas |
| Diretório | enumeração limitada; arquivos indiretos, symlinks e reparse points são recusados |
| Duplicatas | SHA-256 duplicado interrompe toda a entrada |

Uma imagem inválida, duplicada ou um limite excedido não produz um capítulo menor sem aviso: a
entrada inteira é recusada antes de enfileirar o processamento.

## Snapshot, proveniência e materialização

Ao aceitar a pasta, o adaptador copia os bytes validados para um filho novo de
`.cache/runtime/local_sources/`. Os nomes são gerados (`0001.png`, por exemplo), e o manifesto
interno contém hashes, dimensões, formato, tamanho, IDs opacos e uma impressão digital da origem.
Ele não contém o caminho original nem os nomes originais das páginas.

O job guarda `source_type=local_folder`, nome/versão do adapter, contagens, uma impressão digital
da pasta e um `snapshot_ref` opaco. O comando do worker recebe somente esse `snapshot_ref`; o
runner o resolve novamente como filho direto do workspace controlado. Antes de copiar as páginas
para `output/<execução>/input`, ele confere layout, hash, tamanho e bytes outra vez.

Os originais são somente leitura para o processo: não são movidos, renomeados, alterados ou
apagados. Um destino de entrada já existente também não é limpo por padrão. A limpeza só é
permitida para um diretório marcado como pertencente a um snapshot local e com autorização
explícita de reprocessamento; conteúdo não pertencente ao pipeline causa falha fechada.

Os snapshots são artefatos internos de cache. Esta implementação não promete limpeza automática
de snapshots antigos; trate a retenção deles conforme a política local de armazenamento, sem
apagar artefatos de uma execução ativa.

## Páginas lógicas e Smart Split

Arquivos de pasta local são tratados como páginas lógicas completas. Por isso o relatório marca:

```json
{
  "logical_pages": true,
  "requires_smart_split": false
}
```

O Smart Split faz *passthrough*: mantém cada página na ordem e nas dimensões originais, em vez de
juntar fatias e recortá-las novamente. Isso evita partir uma página alta que já foi fornecida
como unidade editorial. O comportamento padrão para fatias Webtoon continua separado e não é
alterado pela entrada local.

## Erros sanitizados

Os erros locais usam códigos sem incluir o caminho do usuário em DTOs ou logs públicos. Exemplos:

| Código | Significado resumido |
| --- | --- |
| `local_input_not_configured` | não há raiz local permitida disponível |
| `local_path_unsupported`, `local_path_traversal`, `local_path_not_allowed` | o caminho não satisfaz a política |
| `local_reparse_point` | link, junction ou reparse point foi encontrado |
| `local_unsupported_extension`, `invalid_local_image` | tipo/extensão ou bytes não são aceitos |
| `local_input_limit`, `local_input_duplicate` | excedeu limite ou há conteúdo repetido |
| `local_workspace_invalid`, `local_snapshot_conflict` | o snapshot interno não passou na validação |
| `local_folder_requires_loopback_ui` | tentativa de enviar pasta por UI não-loopback |

## Cobertura verificada e limites conhecidos

Os seguintes testes são herméticos: usam diretórios temporários e imagens sintéticas, sem
Webtoon, NVIDIA, Chrome, Selenium ou downloader remoto.

- `test_local_folder_source.py`: política de raízes, traversal, reparse points, formatos,
  limites, duplicatas, snapshot e ausência de caminhos no manifesto;
- `test_local_folder_input.py`: revalidação do snapshot, materialização e proteção da saída;
- `test_local_folder_cli.py`: referência opaca, rota CLI/runner e confinamento da saída;
- `test_logical_pages.py`: passthrough do Smart Split para páginas lógicas;
- `test_local_pipeline_e2e.py`: encadeamento sintético de pasta, snapshot, job, worker e PDF
  com `fake_pipeline.py`.

O E2E sintético comprova o contrato de encadeamento, não a qualidade de OCR/tradução nem a
execução de um capítulo real. Nenhum smoke externo de fonte, OCR de capítulo ou provider de
tradução é afirmado por este documento. Para as fronteiras de URL e rede, consulte
[Adapters de fonte](SOURCE_ADAPTERS.md) e [Segurança](SECURITY.md).
