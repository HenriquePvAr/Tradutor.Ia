# Segurança e limites de confiança

Este documento descreve as fronteiras implementadas no Tradutor.IA e, igualmente importante,
o que elas **não** garantem. O projeto lida com URLs, páginas renderizadas, imagens e pastas
locais; nenhuma dessas entradas deve ser considerada confiável só porque veio de uma UI local ou
de um host público.

## Princípios operacionais

- Use somente conteúdo que você tem direito de acessar.
- Não inclua chaves, cookies, tokens, URLs assinadas ou caminhos pessoais em issues, relatórios
  ou commits.
- Não há bypass de login, paywall, CAPTCHA, Turnstile, DRM, proteção de canvas ou bloqueio
  anti-bot. Barreiras desse tipo terminam o fluxo com um estado codificado.
- Um adapter específico ou um score alto são evidência de estrutura de leitor; não são prova de
  autorização, disponibilidade futura ou qualidade de tradução.
- O modo padrão de testes é offline. Smokes com rede são manuais, opt-in e separados da coleta
  normal de `pytest`.

## URLs e SSRF

Antes da navegação, adaptadores de URL aceitam apenas HTTP(S), recusam credenciais embutidas e
rejeitam hosts loopback, privados, link-local, multicast, reservados, não especificados ou sem
resolução pública. Redirects de navegação e de imagens são seguidos manualmente, com revalidação
em cada hop. O adapter universal compara respostas DNS vistas na execução e falha fechada quando
um host muda de conjunto de endereços.

O acesso a recursos remotos é limitado ao leitor validado e, para adapters que permitem hosts
relacionados, a recursos observados e autorizados na própria execução. URLs completas, queries,
headers, cookies, request IDs e corpos não são o formato de diagnóstico público; relatórios usam
host e fingerprints de caminho/IDs opacos quando necessários.

### Limite importante

Isso reduz risco, mas não converte Chrome nem `requests` em um sandbox de egress completo. Uma
página pode pedir subrecursos enquanto é renderizada, e há uma janela DNS TOCTOU entre validação
e conexão HTTP. Em uma implantação que aceite URLs arbitrárias e não confiáveis, use uma política
de egress, firewall/proxy ou isolamento de navegador apropriado. Sem essa camada, trate a origem
como confiável e autorizada.

## Adapters de fonte

`chapter_source.py` resolve primeiro adapters registrados e só então usa o fallback universal.

| Fonte | Fronteira |
| --- | --- |
| Webtoons | adapter específico com hosts e seletores próprios |
| VortexScans | host literal `vortexscans.org` e path de capítulo restrito; instância nova por execução para que autorizações de recursos não vazem entre jobs |
| Universal | fallback para URL pública não reclamada; exige evidência agrupada e completa antes de download |
| Pasta local | fonte distinta, sem URL, com política de caminhos e snapshot interno |

O leitor universal coleta, sob tetos, evidências DOM, atributos JSON reconhecidos, recursos que
o navegador já observou, metadados CDP/performance sanitizados e pequenos JSONs de respostas já
recebidas. Texto extraído não é executado. A única paginação genérica permitida é um `href`
explícito, same-origin/same-path, com query de página numérica N+1 comprovada; ela usa navegação
direta já revalidada, não clique ou handler. Ambiguidade, ciclo, redirect ou cobertura incompleta
produzem `pagination_incomplete`, que bloqueia a seleção inclusive pelo caminho de revisão manual,
em vez de baixar um subconjunto. Iframe cross-origin, Shadow DOM fechado, canvas inexportável,
load more, carrosséis e sliders não são contornados; o resultado é revisão/falha controlada ou a
necessidade de um adapter específico.

Uma revisão humana pode incluir prévia visual apenas como `data:image` local, produzido a partir
de uma imagem DOM já carregada e visível. Não há URL de origem na prévia e a UI não busca imagem
remota para renderizá-la: ela aceita somente JPEG/PNG base64 limitado a 64 itens, 24.000 caracteres
por item e 1.000.000 no total. CORS/canvas tainted, imagem invisível/ausente ou fonte observada
só pela rede não são contornados; a prévia é omitida, enquanto ID opaco e metadados seguros seguem
disponíveis. Esse dado de apresentação não autoriza host, não eleva confiança e não altera o
manifesto de download.

Durante `awaiting_source_review`, a UI não reconstrói o DOM da revisão para o mesmo job a cada
polling. Assim, a aba atual não perde exclusões ou reordenações manuais antes da confirmação; isso
não pretende persistir a edição após reload da página ou troca de job.

Para VortexScans, um host de imagem não vira fonte de navegação e não entra em uma allowlist
global. Ele precisa ser público, observado e autorizado apenas na instância atual antes de ser
usado como recurso relacionado.

Mais detalhes estão em [Adapters de fonte](SOURCE_ADAPTERS.md) e
[Adaptador universal](UNIVERSAL_CHAPTER_ADAPTER.md).

## Pasta local e privacidade de caminhos

A entrada local só é aceita por uma UI cujo bind e cliente sejam loopback. Isso evita que uma
API acessível na rede interprete um caminho fornecido remotamente. Na linha de comando, a pasta
precisa ficar em uma raiz permitida por `LOCAL_INPUT_ROOTS` (ou em `input/`, se essa raiz padrão
existir).

A política recusa caminhos relativos, traversal, UNC/dispositivos, `file:`, symlinks e junctions.
Ela inspeciona somente arquivos diretos, valida bytes de imagem e limita tamanho/quantidade. Um
snapshot de nomes gerados é criado em `.cache/runtime/local_sources/`; jobs, manifests de saída e
a UI recebem fingerprint, contagens e referência opaca, não o caminho original. A materialização
posterior revalida o snapshot e não limpa uma saída não marcada como pertencente ao pipeline.

Essa proteção não substitui permissões de sistema contra um usuário local malicioso que consiga
alterar simultaneamente o diretório autorizado. O código reduz corridas comuns revalidando
componentes e bytes; ACLs, disco confiável e acesso físico continuam sendo responsabilidade do
operador. Veja [Entrada por pasta local](LOCAL_FOLDER_INPUT.md).

## Download e sessões

Os transportes compartilham orçamento de capítulo: timeout, redirects, bytes por arquivo, bytes
totais, quantidade de arquivos e duração. Cada resposta é validada por tipo e bytes; páginas HTML,
desafios e dados que não decodificam como imagem não entram como páginas.

`BrowserSessionTransport` pode copiar cookies do domínio literal do leitor para memória durante a
mesma execução. Eles não são gravados em banco ou relatório e são limpos no fechamento.
`CloudscraperTransport` é opcional, exige exatamente `ENABLE_CLOUDSCRAPER_TRANSPORT=1`, usa os
mesmos limites e não altera a política de challenge. Ele não é um mecanismo de evasão e uma flag
ausente ou ambígua não o habilita. Consulte [Transportes de download](DOWNLOAD_TRANSPORTS.md).

## Jobs, UI e dados persistidos

O navegador não executa o pipeline: a UI cria jobs persistentes e o worker independente executa
comando em lista de argumentos, não uma string de shell. Diagnósticos expostos à UI são códigos e
mensagens sanitizadas. A análise de fonte persiste metadados de decisão e IDs de candidatos; não
persiste cookies, headers privados, URL completa de recurso ou pixels de canvas.

Perfis reutilizáveis do fallback universal são dicas locais opt-in, por host exato e com validade
limitada. Eles não concedem acesso, não elevam score e não substituem nova validação/observação.

## Testes sem rede

`conftest.py` instala a guarda offline antes da importação dos módulos de teste. Ela bloqueia
conexões e DNS externo para a suíte padrão, permitindo apenas plumbing loopback necessário ao
processo de teste. `pytest.ini` exclui markers `network` e `manual` por padrão. Scripts manuais
de rede ficam fora do padrão `test_*.py` e exigem opt-ins explícitos.

Essa guarda é uma proteção de desenvolvimento e CI, não uma política de rede para uma execução de
produção. Uma execução solicitada pelo usuário fora da suíte de testes pode abrir navegador e
rede conforme o adapter e a configuração permitirem.

## O que não foi afirmado aqui

Este documento não declara pentest, homologação de qualquer site, cobertura de todos os leitores
ou sucesso de smoke externo. Em particular, a compatibilidade real de uma fonte deve ser verificada
somente em smoke autorizado e isolado, após os testes herméticos pertinentes. Para o estado de
cobertura e limitações funcionais, consulte [Auditoria funcional](FULL_FUNCTIONAL_AUDIT.md).
