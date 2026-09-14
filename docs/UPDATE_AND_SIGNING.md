# Update e signing

O updater verifica manifest HTTPS, assinatura Ed25519, SHA-256, canal e versão mínima.
A chave privada permanece fora do repositório e dos logs. O trust root público local está
configurado para `beta-2026-09` e o cliente aceita somente manifests do canal `beta`.

O installer recebe `ProductVersion` por define do `tools/build_installer.py`; nenhum installer
é gerado nesta fase.

O canal da closed beta ainda não está publicado (o endpoint remoto permanece placeholder).
A primeira atualização segura em campo será `0.9.0-beta.33` → `0.9.0-beta.34`, pois a beta.32
precede a inclusão do trust root e do parser SemVer. Manifesto, canal ausente ou incorreto,
hash, assinatura inválida e downgrade falham fechado antes de qualquer instalação.
