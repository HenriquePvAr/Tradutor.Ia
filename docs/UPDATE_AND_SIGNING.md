# Update e signing

O updater verifica manifest HTTPS, assinatura Ed25519, SHA-256, canal e versão mínima.
A chave privada permanece fora do repositório e dos logs. O trust root público local está
configurado para `beta-2026-09` e o cliente aceita somente manifests do canal `beta`.

Para a distribuição Windows, o artefato principal é o installer Inno Setup
`YomuSekai-<versão>-Setup-x64.exe`. O fluxo da aba Atualizações baixa o installer para staging,
confere assinatura, SHA-256 e tamanho, e só então inicia o processo externo sem `shell=True`.
O caminho ZIP permanece compatível apenas para manifests legados e não é o caminho principal da UI.

O installer recebe `ProductVersion` por define do `tools/build_installer.py`; nenhum installer
é gerado nesta fase.

O canal da closed beta ainda não está publicado (o endpoint remoto permanece placeholder).
A primeira atualização segura em campo será `0.9.0-beta.35` → `0.9.0-beta.36`, pois a beta.34
precede a inclusão do trust root e do parser SemVer. Manifesto, canal ausente ou incorreto,
hash, assinatura inválida e downgrade falham fechado antes de qualquer instalação.
