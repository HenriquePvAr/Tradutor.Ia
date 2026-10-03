# Fluxo de desenvolvimento

1. Use Python 3.11 e instale as dependências do perfil apropriado (`requirements-beta.txt` e `requirements-dev.txt` para a suíte principal).
2. Mantenha mudanças de produção em runtime e experimentos em `experiments/` ou `research/`.
3. Execute `python -m pytest --collect-only -q` após alterar discovery, paths ou localização de testes; depois execute `python -m pytest -q`.
4. Para testes frontend, rode os arquivos em `tests/` com `node --experimental-vm-modules` quando o teste usar `vm.SourceTextModule`.
5. Para entrypoints e packaging, rode os testes de contrato correspondentes e confirme imports sem iniciar serviços ou providers reais.
6. Revise `git diff --check`, arquivos staged, segredos e tamanhos antes de criar commits.

O pytest descobre os testes em `tests/` e os testes locais dos experimentos em `experiments/`. Marcadores `network` e `manual` continuam excluídos por padrão. O workflow completo e as limitações conhecidas estão em [TESTING.md](TESTING.md).
