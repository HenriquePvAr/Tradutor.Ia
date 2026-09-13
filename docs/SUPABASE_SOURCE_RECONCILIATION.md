# Reconciliação do código Edge

Em 2026-09-13, o código local das funções necessárias à primeira closed beta
foi comparado com o código ativo no projeto Supabase `Tradutor IA Community`.
Os seis entrypoints abaixo foram incorporados do remoto após revisão:

- `beta-bootstrap` (versão remota 8)
- `device-challenge` (versão remota 8)
- `device-register` (versão remota 8)
- `device-verify` (versão remota 8)
- `device-heartbeat` (versão remota 8)
- `translation-execute` (versão remota 11)

Os entrypoints locais agora correspondem ao conteúdo ativo consultado. As
funções continuam com `verify_jwt=true`, exigem autenticação e falham fechado
quando a configuração ou autorização não está disponível.

Nenhum secret, dado de usuário, migration ou registro remoto foi alterado.
As chaves de serviço e a chave DeepL permanecem exclusivamente na configuração
do backend; a reconciliação não constitui um deploy.
