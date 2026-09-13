# Update e signing

O updater verifica manifest HTTPS, assinatura Ed25519, SHA-256, canal e versão mínima.
A chave privada permanece fora do repositório e dos logs. O canal de produção e a chave
pública de release ainda precisam ser configurados.

O installer recebe `ProductVersion` por define do `tools/build_installer.py`; nenhum installer
é gerado nesta fase.

O canal da closed beta ainda não está publicado. A ativação futura exige um endpoint HTTPS,
uma chave pública Ed25519 no cliente e a chave privada mantida fora do repositório. Manifesto,
hash, assinatura inválida e downgrade devem falhar fechado antes de qualquer instalação.
