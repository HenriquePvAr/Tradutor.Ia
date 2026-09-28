# Privacidade e dados

O cliente Yomu não oferece mais tela de perfil social nem busca/carregamento de avatar ou
banner. O backend ainda mantém endpoints e armazenamento legados de perfil/mídia; eles não
são chamados pelo fluxo normal da interface. O sistema também processa imagens, OCR,
traduções e metadados de jobs. Performance artifacts devem conter somente tempos,
contagens, estados e identificadores técnicos.

Retenção e consentimento formal ainda estão em `PENDING POLICY DECISION`; nenhum prazo
autoritativo está definido neste repositório.

Perfil social e mídia de avatar/banner estão descontinuados no cliente. A conta de
autenticação continua ativa; remoção de schema, endpoints, Edge Function e mídia armazenada
não faz parte desta mudança.
