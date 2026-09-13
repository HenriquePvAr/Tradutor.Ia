# Update e signing

O updater verifica manifest HTTPS, assinatura Ed25519, SHA-256, canal e versão mínima.
A chave privada permanece fora do repositório e dos logs. O canal de produção e a chave
pública de release ainda precisam ser configurados.

O installer recebe `ProductVersion` por define do `tools/build_installer.py`; nenhum installer
é gerado nesta fase.
