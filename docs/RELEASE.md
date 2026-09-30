# Release

Este é o documento autoritativo para preparar uma release do Yomu Sekai.

- `PRODUCT_VERSION` e `BUILD_VERSION` são definidos em `app_version.py`; não mantenha uma
  cópia manual de versões neste documento. No HEAD revisado pela M18, os valores-fonte são
  `0.9.0` e `0.9.1-beta.18`.

O owner já confirmou a distribuição da Beta 17 pelo updater. Isso não constitui nesta missão
uma nova validação de signing, instalação limpa ou readiness da Beta 18. Status de bloqueadores
copiados de avaliações anteriores devem ser tratados como históricos até serem revalidados pelo
processo próprio. Consulte [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) para o checklist; a M18
não executa gate de release.

Para a primeira closed beta, o cliente usa a configuração pública do Supabase e a autoridade
de licença/ tradução configurada no backend. A chave `service_role` nunca pertence ao cliente.
Cloudflare permanece opcional nesta etapa; qualquer proteção de borda futura deve ser configurada
sem alterar o contrato do cliente.
