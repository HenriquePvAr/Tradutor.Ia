# Privacidade e dados

O cliente Yomu não oferece tela de perfil social nem busca/carregamento de avatar ou
banner. O runtime local não possui handlers, rotas, cache ou transporte para essas mídias.
O sistema também processa imagens, OCR,
traduções e metadados de jobs. Performance artifacts devem conter somente tempos,
contagens, estados e identificadores técnicos.

Retenção e consentimento formal ainda estão em `PENDING POLICY DECISION`; nenhum prazo
autoritativo está definido neste repositório.

Perfil social e mídia de avatar/banner estão descontinuados localmente. Login, identidade
da conta, licença, entitlement, YK, dispositivo e logout permanecem. Arquivos/cache antigos
no computador não são removidos automaticamente. O schema remoto, campos, mídia armazenada,
secrets e Edge Function não são alterados nesta fase.

`REMOTE_PROFILE_MEDIA_DECOMMISSION: PENDING_MISSION_14` — a remoção remota exige auditoria
e autorização próprias; a função remota ainda pode existir.
