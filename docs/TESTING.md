# Testes herméticos e smokes manuais

## Padrão: offline

Os arquivos `test_*.py` são testes unitários ou integrações offline. A proteção
central bloqueia conexões de socket antes de qualquer request; use fakes e
mocks locais para exercitar clientes externos.

Instale a dependência de desenvolvimento antes de usar pytest:

```powershell
python -m pip install -r requirements-dev.txt
```

O comando padrão garantido para a suíte hermética é:

```powershell
python -m pytest
```

Cada módulo descoberto por `unittest` importa o bootstrap offline como primeiro passo de
execução. Processos Python descendentes herdam um marcador e o `PYTHONPATH` necessários para
que `sitecustomize.py` reinstale a mesma barreira antes de carregar o pipeline. A CI e a
documentação usam `pytest` para collection, marcadores e a garantia central antes da
importação dos módulos.

Workers usados por testes com banco temporário escrevem logs ao lado desse banco, nunca em
`.cache/runtime/logs` da execução de produção. Processos-filho desses testes herdam o guard
offline; conexões externas falham antes de qualquer request.

O guard é uma proteção Python em nível de processo, não um firewall do sistema operacional.
Um teste novo não pode iniciar navegador, `curl` ou outro binário de rede sem uma revisão
explícita; a suíte atual usa somente filhos Python/fakes. A execução externa real continua
reservada aos smokes manuais com opt-in.

Os testes do adapter universal, da política de redirects, dos limites de transporte e da
revisão de páginas usam drivers, sessões e respostas falsas. Eles não submetem uma URL de
capítulo, não abrem Chrome nem fazem a análise real de fonte. Qualquer teste novo desse
fluxo deve injetar essas dependências locais; a análise real só ocorre após uma submissão
explícita do usuário na UI/CLI e não pertence à suíte padrão.

No GitHub Actions, o workflow **Hermetic Tests** roda em `windows-latest` a
cada push para `main` e pull request destinado a `main`. Ele instala somente
as dependências declaradas, mantém a rede de smokes desautorizada e não
executa scripts manuais nem usa segredo NVIDIA.

O equivalente local da CI é:

```powershell
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
python -m pytest --collect-only -q
python -m pytest -q
node --check static/tradutor_ui.js
```

O `pytest.ini` exclui os marcadores `network` e `manual` por padrão. Os
marcadores disponíveis são `unit`, `integration`, `network`, `manual` e
`slow`.

## Smokes de rede: somente manual e opt-in

Os smokes não usam prefixo `test_` e não participam de discovery, CI, IDE ou
execução padrão. Nunca os execute em paralelo com E2E.

Para NVIDIA, as duas variáveis devem ter exatamente o valor `1`. O smoke usa
`enable_cache=False`, portanto não lê nem grava o cache de tradução normal.

```powershell
$env:ALLOW_NETWORK_TESTS = "1"
$env:ALLOW_NVIDIA_SMOKE = "1"
python -m scripts.manual_nvidia_smoke --short
```

Para Webtoon, também são exigidas duas autorizações. A URL deve ser fornecida
explicitamente e a saída deve ser uma pasta nova chamada `smoke_*` dentro do
diretório temporário. O script recusa sobrescrever e não faz cleanup
automático. Cache, imagens de entrada e renderização ficam dentro dessa mesma
pasta, nunca nas raízes normais do projeto.

```powershell
$env:ALLOW_NETWORK_TESTS = "1"
$env:ALLOW_WEBTOON_SMOKE = "1"
python -m scripts.manual_webtoon_smoke `
  --url "<URL_AUTORIZADA>" `
  --output-dir "$env:TEMP\smoke_webtoon_manual"
```

Sem ambas as variáveis específicas, cada script encerra com código não zero
antes de importar cliente NVIDIA, Selenium, downloader ou configuração do
pipeline.

Os smokes manuais de rede não são um atalho para validar o fallback universal. Caso uma
análise real de fonte seja autorizada fora da suíte hermética, ela deve respeitar o contrato
de segurança, limites e revisão descrito em
[Adaptador universal de capítulos](UNIVERSAL_CHAPTER_ADAPTER.md), e nunca deve ser usada
como descoberta automática de testes.
