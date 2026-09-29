# Privacidade e dados

O cliente Yomu não oferece tela de perfil social nem busca/carregamento de avatar ou
banner. O runtime local não possui handlers, rotas, cache ou transporte para essas mídias.
O sistema também processa imagens, OCR,
traduções e metadados de jobs. Performance artifacts devem conter somente tempos,
contagens, estados e identificadores técnicos.

Retenção e consentimento formal ainda estão em `PENDING POLICY DECISION`; nenhum prazo
autoritativo está definido neste repositório.

Profile Media (upload, leitura e cache de avatar/banner) está descontinuada no produto ativo.
Os perfis textuais da Comunidade e a identidade da conta continuam separados e preservados;
login, licença, entitlement, YK, dispositivo e logout não dependem de mídia de perfil.
Arquivos/cache antigos no computador não são removidos automaticamente. O schema e as
migrations históricas permanecem intactos.

`REMOTE_PROFILE_MEDIA_DECOMMISSION: COMPLETE_FOR_ACTIVE_BACKEND` — no Supabase ativo
`ybxwjiycujminbwbdtlb`, a Edge Function `profile-media` está ausente, nenhuma Edge Function
ativa a referencia e a consulta read-only de logs não encontrou chamadas nas últimas 24h.
O source órfão foi removido deste repositório. O projeto legado
`mimrsxnhqbqkffsekxuw` permanece intacto como rollback temporário; a função, secrets e
dados desse projeto não foram removidos. Os campos de avatar/banner no schema ativo são
legado inerte e foram mantidos para uma futura manutenção deliberada.
