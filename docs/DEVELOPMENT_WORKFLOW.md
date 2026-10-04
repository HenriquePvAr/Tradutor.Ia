# Fluxo de desenvolvimento

1. Use Python 3.11 e instale as dependências do perfil apropriado (`requirements-beta.txt` e `requirements-dev.txt` para a suíte principal).
2. Mantenha mudanças de produção em runtime e experimentos em `experiments/` ou `research/`.
3. Execute `python -m pytest --collect-only -q` após alterar discovery, paths ou localização de testes; depois execute `python -m pytest -q`.
4. Para testes frontend, rode os arquivos em `tests/` com `node --experimental-vm-modules` quando o teste usar `vm.SourceTextModule`.
5. Para entrypoints e packaging, rode os testes de contrato correspondentes e confirme imports sem iniciar serviços ou providers reais.
6. Revise `git diff --check`, arquivos staged, segredos e tamanhos antes de criar commits.

## Módulos Python e raiz

Implemente runtime novo no pacote `yomu/`, agrupado pela responsabilidade documentada em
[`PROJECT_STRUCTURE.md`](PROJECT_STRUCTURE.md). Não adicione módulos de domínio, teste ou
pesquisa diretamente na raiz. Os entrypoints de compatibilidade existentes são pequenos
shims; altere a implementação canônica e preserve o shim somente quando o contrato público
ou o packaging ainda depender do nome antigo.

Valide imports e árvore com:

```powershell
python -m compileall -q yomu test_support
python -m pytest --collect-only -q
python -m pytest -q
python tools/repository/check_root_policy.py
```

Para subprocessos, prefira `python -m yomu.<package>.<module>` em vez de executar um arquivo
diretamente dentro de `yomu/`; isso mantém a raiz do repositório no import path.

O pytest descobre os testes em `tests/` e os testes locais dos experimentos em `experiments/`. Marcadores `network` e `manual` continuam excluídos por padrão. O workflow completo e as limitações conhecidas estão em [TESTING.md](TESTING.md).
