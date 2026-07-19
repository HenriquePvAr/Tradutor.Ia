# Entrada por pasta local

Traduzir a partir de arquivos que você já tem no disco, sem nenhum acesso a site externo.

## Uso

Coloque as imagens numa pasta dentro de uma raiz permitida (padrão: `input/`):

```
input/meu_capitulo/
  1.png
  2.png
  10.png
```

A ordenação é **natural**: `1`, `2`, `10` — nunca `1`, `10`, `2`.

## Raízes permitidas

Padrão: `<repo>/input`. Configurável, com defaults seguros. A política recusa:

- caminho fora das raízes configuradas;
- path traversal (`..\`);
- fuga por symlink ou junction (o caminho real é resolvido e revalidado);
- UNC remoto;
- dispositivos especiais;
- `C:\` ou diretório de sistema como raiz.

Nunca é usado `file://` e nenhum navegador é aberto.

## Validação de cada arquivo

Extensões aceitas: `.png`, `.jpg`, `.jpeg`, `.webp`, `.avif` — mas a extensão **não** é a prova. Cada arquivo passa por:

| Checagem | Por quê |
| --- | --- |
| magic bytes / assinatura real | extensão mente |
| MIME real | idem |
| markup detection | HTML ou JSON salvo como `.jpg` |
| Pillow `verify()` + `load()` | pega truncamento e corrupção |
| OpenCV quando necessário | segunda opinião no decode |
| largura / altura / área | descarta lixo |
| tamanho > 0, teto por arquivo e total | limita entrada abusiva |
| `sha256` | deduplicação por conteúdo |

Rejeição gera `rejection_reason` no manifest — o arquivo original não é tocado.

## Snapshot isolado

Cada job materializa um snapshot próprio:

- nomes internos gerados, não os originais;
- caminho original registrado **apenas server-side**;
- idempotente, sem sobrescrever snapshot de outro job;
- **os arquivos originais nunca são alterados, movidos ou apagados**.

O frontend e os endpoints sociais nunca recebem o caminho local nem o caminho do snapshot.

## Páginas lógicas e Smart Split

Esta é a parte que mais importa acertar.

O Smart Split existe porque assets de webtoon são **fatias de transporte, não páginas**: ele junta as fatias e recorta em faixas horizontais seguras, para um balão não ficar partido entre dois arquivos.

Arquivos de uma pasta local normalmente **já são páginas completas**. Rodar o Smart Split neles juntaria tudo e recortaria de novo, destruindo os limites de página do autor.

Por isso o manifest marca `logical_page: true`, e `prepare_smart_webtoon_pages(..., logical_pages=True)` faz *passthrough*: as páginas passam intactas, na ordem, e o relatório grava `smart_split_skipped: true` com `skip_reason: inputs_are_complete_pages`.

O caminho padrão (fatias) continua funcionando exatamente como antes — há teste para os dois.

## Manifest

`job_id`, `source_type: local_folder`, `adapter_name`, `adapter_version`, `created_at`,
`input_count`, `accepted_count`, `rejected_count`, `ordered_pages`, e por página: filename
interno, `hash`, MIME, `width`, `height`, `size_bytes`, `logical_page` e
`rejection_reason` quando aplicável.

## Erros

`local_folder_not_found`, `local_path_not_allowed`, `no_supported_images`,
`invalid_local_image`, `duplicate_local_image`, `snapshot_failed`, `local_input_too_large`.

Mensagens sanitizadas; nenhum traceback e nenhum caminho no frontend.

## Testes

```
.\.venv\Scripts\python.exe -m pytest test_local_folder_source.py test_local_folder_input.py test_logical_pages.py -q
```

Tudo hermético: imagens sintéticas, sem rede, sem NVIDIA, sem Drive, sem Supabase remoto.

## Limitações

- A UI ainda **não** tem o seletor "URL / Pasta local"; o fluxo local existe na camada de
  adapter, snapshot e manifest, e ainda não foi ligado à tela Nova tradução.
- O E2E hermético completo (OCR fake → tradução fake → PDF → quality gate) **não** foi
  executado nesta tarefa; o que está provado é a entrada, a validação, o snapshot e o
  passthrough de páginas lógicas.
- Publicação a partir de entrada local usa o fluxo explícito existente, não exercitado aqui.
