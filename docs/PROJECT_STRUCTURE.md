# Estrutura do projeto

O repositório mantém deliberadamente os módulos Python de runtime na raiz por compatibilidade com imports legados, inicialização desktop e configuração PyInstaller. A reorganização desta versão removeu testes e harnesses da raiz sem tentar uma migração `src/` que ampliaria o risco de packaging.

| Local | Conteúdo |
| --- | --- |
| `app_ui.py`, `desktop_app.py`, `worker_service.py`, `job_runner.py` e módulos Python da raiz | Runtime e entrypoints legados do produto |
| `apps/`, `ui/`, `static/`, `supabase/`, `packaging/`, `assets/` | Aplicações auxiliares, interface, migrations, empacotamento e recursos |
| `tests/` | Testes Python e JavaScript; os testes Python são descobertos pelo pytest |
| `tests/integration/`, `tests/sql/` | Integrações e fixtures SQL já organizadas |
| `experiments/` | Experimentos isolados e seus testes próprios |
| `research/` | Ferramentas e evidências de pesquisa; não importadas pelo runtime |
| `scripts/` | Operação, validação de ambiente e smokes explicitamente opt-in |
| `tools/` | Administração, build, release, desenvolvimento e harnesses |
| `docs/` | Documentação de produto, desenvolvimento, arquitetura e histórico |
| `reports/` | Evidência pequena e canônica; outputs grandes ficam externos |

## Onde adicionar trabalho

- Novo teste de produto: `tests/`, com nome `test_*.py` ou `test_*.mjs`.
- Novo harness reutilizável: `tools/harnesses/`; smoke operacional: `scripts/`.
- Novo experimento: `experiments/<nome>/` ou `research/<domínio>/`, com README, ambiente e limites de produção explícitos.
- Novo relatório: `reports/` apenas quando pequeno, sanitizado e necessário para rastreabilidade; caso contrário mantenha o output fora do Git e versione apenas um manifesto enxuto.
- Novo módulo de runtime: preserve os imports e entrypoints existentes; qualquer mudança de localização requer uma migração independente com testes e verificação do packaging.

O fluxo de desenvolvimento e a política de artefatos estão em [DEVELOPMENT_WORKFLOW.md](DEVELOPMENT_WORKFLOW.md) e [ARTIFACT_POLICY.md](ARTIFACT_POLICY.md).
