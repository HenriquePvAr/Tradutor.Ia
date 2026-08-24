# Tradutor IA — Guia do Usuário

> **Versão do guia verificada em:** commit `c81c798` · **Revisado em:** 2026-08-19
> **Para quem é:** Scans, tradutores, editores e testadores da Beta. Nenhum conhecimento
> técnico é necessário.

---

## Sumário

1. [O que o Tradutor IA faz](#1-o-que-o-tradutor-ia-faz)
2. [O que a Beta atual já permite](#2-o-que-a-beta-atual-já-permite)
3. [O que você precisa no computador](#3-o-que-você-precisa-no-computador)
4. [Como instalar hoje](#4-como-instalar-hoje)
5. [Abrindo o programa pela primeira vez](#5-abrindo-o-programa-pela-primeira-vez)
6. [Entrando na sua conta](#6-entrando-na-sua-conta)
7. [Conhecendo as telas](#7-conhecendo-as-telas)
8. [Traduzindo um capítulo, passo a passo](#8-traduzindo-um-capítulo-passo-a-passo)
9. [Escolhendo o modo e o escopo](#9-escolhendo-o-modo-e-o-escopo)
10. [Acompanhando o progresso](#10-acompanhando-o-progresso)
11. [Revisão das páginas encontradas](#11-revisão-das-páginas-encontradas)
12. [Cancelar uma tradução](#12-cancelar-uma-tradução)
13. [Se o programa fechar ou travar no meio](#13-se-o-programa-fechar-ou-travar-no-meio)
14. ["Revisão necessária": o que significa](#14-revisão-necessária-o-que-significa)
15. [Onde ficam o PDF e os arquivos](#15-onde-ficam-o-pdf-e-os-arquivos)
16. [Biblioteca: capítulos traduzidos](#16-biblioteca-capítulos-traduzidos)
17. [Traduzir vários capítulos de uma vez](#17-traduzir-vários-capítulos-de-uma-vez)
18. [Comunidade](#18-comunidade)
19. [Configurações](#19-configurações)
20. [Mensagens de erro e o que fazer](#20-mensagens-de-erro-e-o-que-fazer)
21. [Como reiniciar com segurança](#21-como-reiniciar-com-segurança)
22. [Privacidade e segurança](#22-privacidade-e-segurança)
23. [Limitações desta versão](#23-limitações-desta-versão)
24. [Perguntas frequentes](#24-perguntas-frequentes)
25. [Precisa de mais ajuda?](#25-precisa-de-mais-ajuda)

---

## 1. O que o Tradutor IA faz

Você informa o endereço de um capítulo. O Tradutor IA então:

1. **Analisa o capítulo** e descobre quais são as páginas de verdade;
2. **Baixa as imagens** e confere se todas chegaram inteiras;
3. **Encontra os balões** e lê o texto dentro deles;
4. **Traduz** o texto para português do Brasil;
5. **Redesenha as páginas**, apagando o texto original e escrevendo a tradução por cima,
   preservando a arte;
6. **Monta um PDF** do capítulo traduzido;
7. **Marca o que merece sua atenção**, em vez de fingir que ficou tudo perfeito.

Tudo isso acontece **no seu computador**. Suas traduções são suas e ficam na sua máquina
até que você decida publicá-las.

> 💡 O programa foi feito para ser honesto. Quando ele não tem certeza de alguma coisa, ele
> avisa em vez de inventar.

## 2. O que a Beta atual já permite

| Recurso | Disponível? |
| --- | --- |
| Traduzir um capítulo a partir de um endereço da internet | ✅ Sim |
| Traduzir imagens que já estão numa pasta do seu computador | ✅ Sim |
| Fila com vários capítulos em sequência | ✅ Sim |
| Continuar processando com o navegador fechado | ✅ Sim |
| Biblioteca com os capítulos já traduzidos | ✅ Sim |
| PDF do capítulo | ✅ Sim |
| Tela de revisão de qualidade | ✅ Sim |
| Publicar um PDF na comunidade | ✅ Sim (ação explícita, nunca automática) |
| Instalador com um clique (Setup) | ⏳ **Em desenvolvimento** |
| Atualização automática do programa | ⏳ **Em desenvolvimento** |
| Licença/validade de testador | ⏳ **Em desenvolvimento** |

> ⚠️ Itens marcados como **em desenvolvimento** ainda não existem. Se alguém disser que
> você deve "baixar o instalador", essa informação está errada para esta versão.

## 3. O que você precisa no computador

- **Windows 64 bits** (é o único sistema testado de ponta a ponta);
- **Google Chrome** instalado (usado para abrir o capítulo e localizar as páginas);
- **Conexão com a internet** durante a tradução;
- Espaço livre em disco — um capítulo completo com imagens, páginas redesenhadas e PDF
  pode ocupar algumas centenas de megabytes;
- Uma **conta** para entrar no programa;
- Uma **chave do serviço de tradução** (fornecida junto com o acesso à Beta).

## 4. Como instalar hoje

> ⚠️ **Instalador para usuário final: em desenvolvimento.** Nesta fase da Beta não existe
> um `Setup.exe`. A instalação é feita a partir do repositório do projeto, e normalmente é
> preparada por quem coordena a Beta.

Se você recebeu um computador ou uma pasta já preparada, pule direto para a
[seção 5](#5-abrindo-o-programa-pela-primeira-vez).

Se você mesmo vai preparar a instalação, o passo a passo técnico está em
[Instalação (documentação técnica)](../INSTALLATION.md) e em
[Ambiente de desenvolvimento](../technical/DOCUMENTACAO_TECNICA.md#27-ambiente-de-desenvolvimento-e-comandos).
Em resumo, esse caminho envolve instalar o Python, baixar o projeto, instalar as
dependências e preencher um arquivo de configuração com a sua chave de tradução e os dados
da sua conta.

> 💡 **Você não precisa fazer isso sozinho.** Se você é testador Scan e não trabalha com
> programação, peça a instalação preparada a quem coordena a Beta. Este guia assume que o
> programa já está instalado.

## 5. Abrindo o programa pela primeira vez

Na pasta do programa existe um atalho chamado **`start_tradutor.bat`**. Dê um duplo clique
nele.

O que acontece:

1. Uma janela pode piscar rapidamente — é normal;
2. O programa liga o **serviço de processamento**, que é a parte que faz o trabalho pesado;
3. Em seguida a interface abre no seu navegador, no endereço **`http://127.0.0.1:8080`**.

Se o navegador não abrir sozinho, digite `http://127.0.0.1:8080` na barra de endereços.

> 💡 O serviço de processamento continua funcionando mesmo se você fechar a aba do
> navegador. Um capítulo que já começou **não é perdido** porque você fechou a janela.

> ⚠️ Enquanto o programa estiver rodando, **não feche** a janela preta que ficou aberta
> (quando houver uma). Fechá-la encerra o programa.

## 6. Entrando na sua conta

O Tradutor IA pede **login**. Sem entrar na conta você não consegue criar traduções nem ver
a biblioteca.

Na tela de acesso você verá:

- campos **E-mail** e **Senha**;
- a opção **Lembrar de mim**;
- o botão **Entrar no painel**;
- um indicador de conexão: *verificando serviço…*, *serviço conectado* ou
  *sem conexão com o serviço*.

Se você ainda não tem conta, use **Criar conta**. Dependendo da configuração do seu
ambiente, pode ser necessário confirmar o cadastro pelo e-mail.

> ⚠️ A opção de **recuperação de senha** pode aparecer indisponível dependendo do ambiente.
> Nesse caso, peça ajuda a quem coordena a Beta.

## 7. Conhecendo as telas

Na lateral existe um menu com as áreas do programa:

| Área | Para que serve |
| --- | --- |
| **Início** | Resumo: séries em andamento, atividade recente e atalhos rápidos |
| **Nova tradução** | Onde você começa a tradução de um capítulo |
| **Fila** | Vários capítulos enfileirados para processar em sequência |
| **Capítulos traduzidos** | Sua biblioteca local: abrir PDF, pasta e relatórios |
| **Comunidade** | Descobrir traduções e publicar as suas |
| **Configurações** | Preferências do programa |
| **Logs** | Registro técnico do que aconteceu |
| **Perfil** | Seus dados e privacidade |

> 💡 Atalhos: as teclas **1 a 8** trocam de aba, e **Enter** inicia a análise da URL na
> tela de nova tradução.

## 8. Traduzindo um capítulo, passo a passo

O caminho normal é este:

```
Abrir o Tradutor IA
   ↓
Entrar na conta
   ↓
Nova tradução → colar o endereço do capítulo
   ↓
Iniciar tradução
   ↓
O programa analisa a fonte automaticamente
   ↓
Acompanhar o progresso
   ↓
(se aparecer) revisar os avisos
   ↓
Abrir o PDF
```

Detalhando:

**1. Abra a aba "Nova tradução".**

**2. Escolha a origem do capítulo.** Há dois cartões:

- **URL pública** — você cola o endereço do capítulo;
- **Pasta local** — você indica uma pasta do computador que já contém as imagens do
  capítulo. Essa opção só aparece quando o programa está rodando no seu próprio
  computador.

**3. Cole o endereço** no campo **URL do capítulo**.

**4. Confira os campos automáticos.** O **Nome do capítulo** e a **Pasta de saída** são
sugeridos sozinhos; você pode ajustá-los.

**5. Escolha o motor de tradução.** O padrão é **DeepL (Qualidade)**. Também existem
**Riva — mais rápido** e **Nemotron — alternativo**. A escolha vale só para este capítulo.

**6. Clique em "Iniciar tradução".** O programa analisa a fonte automaticamente antes de
criar o processamento. Se o link for inválido, a página não existir, a fonte estiver
temporariamente indisponível ou o site ainda não for compatível, nenhum processamento é
criado e a mensagem explica o motivo.

Quando um site ainda não é compatível, o Tradutor IA pergunta se você quer enviar o link ao
desenvolvedor. Esse envio exige clique explícito e registra apenas metadados sanitizados
como URL, domínio, motivo e versão do app; não envia cookies, senhas, tokens ou imagens do
capítulo.

> ⚠️ Use apenas conteúdo que você tem autorização para processar.

## 9. Escolhendo o modo e o escopo

### Modo de processamento

| Modo | Quando usar |
| --- | --- |
| **Rápido** | Uso geral. Processa com otimizações automáticas e validação de qualidade. |
| **Qualidade** | Páginas e fontes difíceis. Processamento mais conservador, mais lento. |
| **Download-only** | Só coleta e valida as páginas. Não lê texto, não traduz e não gera PDF. Útil para conferir se um capítulo está completo. |

### Escopo

Quantas páginas processar: **completo**, **3**, **5**, **20**, **50** ou um número que você
escolher.

> 💡 Para testar um capítulo novo, comece com **3 ou 5 páginas** no modo **Rápido**. Se o
> resultado agradar, refaça no escopo completo.

### Opções

| Opção | O que faz |
| --- | --- |
| **Usar cache** | Reaproveita download, leitura e tradução já validados. Deixe ligado. |
| **Forçar reprocessamento** | Ignora o que já foi feito e refaz tudo. Mais lento. |
| **Contexto temporário do capítulo** | Mantém nomes e termos consistentes entre os balões. |
| **Abrir pasta ao terminar** | Mostra os arquivos assim que o capítulo ficar pronto. |
| **Registrar perfil local da fonte** | Opcional; guarda apenas evidências sem dados sensíveis, e só depois de uma revisão manual concluída. |

## 10. Acompanhando o progresso

O painel **Pipeline** mostra as etapas na ordem. Em linguagem simples:

| Etapa na tela | O que está acontecendo |
| --- | --- |
| **Análise segura da fonte** | O programa está descobrindo quais são as páginas do capítulo |
| **Revisão das páginas** | Ele encontrou as páginas, mas quer que você confirme antes de continuar |
| **Download das páginas** | Está baixando as imagens e conferindo se chegaram inteiras |
| **Detecção de balões** | Está localizando os balões e as áreas de texto |
| **Leitura do texto** | Está lendo o que está escrito nas imagens |
| **Tradução** | Está traduzindo os textos para português |
| **Redesenho dos balões** | Está apagando o texto original e escrevendo a tradução |
| **Geração do PDF** | Está montando o arquivo final |
| **Revisão de qualidade** | Está conferindo o resultado e separando o que merece sua atenção |

Você também verá mensagens curtas como *"Na fila…"*, *"Iniciando o worker…"*,
*"Carregando páginas do leitor"*, *"Baixando as páginas…"* e *"Lendo o texto…"*, além de
uma estimativa de tempo.

> 💡 **"Worker"** é o nome do serviço que faz o processamento em segundo plano. Quando uma
> mensagem fala em worker, ela está falando dessa parte do programa.

O status geral aparece com nomes simples: *na fila*, *analisando fonte*, *rodando*,
*revisão das páginas*, *finalizado*, *revisão necessária*, *cancelado*, *erro*.

## 11. Revisão das páginas encontradas

Às vezes o programa encontra as páginas, mas **não tem certeza suficiente** de que a lista
está completa e na ordem certa. Em vez de arriscar, ele para e mostra o painel
**Páginas encontradas**.

Você pode:

- **Confirmar páginas** — segue para o download e o restante do processo;
- **Tentar nova análise** — refaz a descoberta;
- **Cancelar** — descarta este capítulo.

> 💡 Nesse momento **nada foi baixado nem traduzido ainda**. Cancelar aqui não desperdiça
> trabalho.

## 12. Cancelar uma tradução

Use o botão **Cancelar processamento** no cartão de status.

O que acontece: o programa avisa o serviço de processamento, encerra o trabalho com
segurança e **preserva o que já tinha sido produzido**. Nada é apagado por trás.

Um capítulo cancelado aparece como **cancelado**. Quando ele é recuperável, o botão
**Tentar novamente** fica disponível: ele cria uma nova tentativa vinculada, preservando a
execução anterior e seus arquivos.

## 13. Se o programa fechar ou travar no meio

O Tradutor IA foi feito esperando que coisas ruins aconteçam.

**Se você fechar a aba ou o navegador:** nada é perdido. O processamento continua em
segundo plano. Reabra `http://127.0.0.1:8080` e você volta a ver o progresso.

**Se o serviço de processamento cair sozinho:** quando você iniciou o programa pelo atalho
normal, ele **percebe a queda e liga um substituto automaticamente**, esperando alguns
segundos entre as tentativas. Se o serviço cair repetidamente (três vezes seguidas), o
programa **para de tentar** e passa a mostrar honestamente que está indisponível — é sinal
de que existe um problema real que precisa ser investigado, e não de que basta insistir.

**Se o computador desligar no meio:** o capítulo fica marcado como **interrompido** e os
arquivos já produzidos continuam na pasta de saída.

### Retomar um capítulo interrompido

Quando um capítulo é interrompido e o programa consegue confirmar que o estado salvo pode
continuar, aparece o aviso **"Processamento interrompido"** com o botão **Retomar**.

Ao clicar em **Retomar**:

- o programa continua o mesmo capítulo, **sem pedir o endereço de novo** e sem criar uma
  tradução duplicada;
- o Tradutor IA **reaproveita o progresso válido que estiver salvo** e continua a partir do
  ponto seguro de recuperação — não é uma promessa de retomar exatamente no último passo,
  e sim de não jogar fora o que já estava confirmado;
- o capítulo volta para a **fila normal** e começa quando chegar a vez dele. Se o serviço
  de processamento estiver fora do ar, ele fica aguardando; assim que o serviço voltar, o
  trabalho segue sozinho.

O botão só aparece quando o **programa** confirma que a retomada é segura — não basta o
capítulo estar marcado como interrompido. Um capítulo interrompido antes de o trabalho
realmente começar, por exemplo, não tem o que retomar e **não** mostra o botão. Nesse caso
inicie o capítulo novamente: os arquivos anteriores não são apagados e o reaproveitamento
de cache deixa a nova execução mais rápida.

Clicar duas vezes não cria dois trabalhos: enquanto o pedido está em andamento o botão fica
como **Retomando…** e desabilitado, e o programa recusa um segundo pedido para o mesmo
capítulo.

Se o capítulo já tiver mudado de estado (por exemplo, se ele já voltou a rodar), a
retomada é recusada com uma mensagem clara e a tela se atualiza sozinha com a situação
real. O botão continua disponível depois de um erro; nada é dado como retomado sem o
programa confirmar.

Um capítulo com estado **erro** ou **cancelado** que seja recuperável continua tendo o
botão **Tentar novamente**, que é outra coisa: ele começa uma nova tentativa em vez de
continuar de um ponto salvo.

## 14. "Revisão necessária": o que significa

Quando um capítulo termina como **revisão necessária**, isso **não quer dizer que a
tradução falhou**. Quer dizer:

> O PDF foi criado e está pronto para uso, mas alguns pontos não puderam ser provados como
> corretos automaticamente, e o programa prefere te mostrar a fingir que estava tudo certo.

Motivos comuns:

- um **logo** ou **título** desenhado, que não é texto comum;
- uma leitura **incerta** de uma palavra pequena, borrada ou estilizada;
- um **nome próprio** que o programa preferiu não alterar;
- um trecho de texto original que a verificação **não conseguiu provar** que foi tratado;
- um **efeito sonoro** — por padrão os efeitos sonoros são preservados, não traduzidos.

Na tela de **Revisão de qualidade** você pode filtrar por **Pendentes**, **Todos**,
**Concluídos**, **Rejeitados**, **Revisão manual** e outros; aceitar itens de baixo risco,
aceitar todos, desfazer a última ação em massa ou **Reprocessar pendências**.

> 💡 Um capítulo em **revisão necessária** já tem PDF. Você pode abri-lo e usá-lo enquanto
> decide o que revisar.

## 15. Onde ficam o PDF e os arquivos

Tudo fica dentro da pasta do programa, em `output/`. Em versões atuais, cada execução fica
numa subpasta do capítulo com um identificador próprio:

```text
output/<nome_do_capitulo>/<id_da_execucao>/
```

Isso permite repetir o mesmo capítulo sem apagar o PDF ou os relatórios da execução
anterior. Capítulos gerados por versões antigas podem continuar aparecendo diretamente em
`output/<nome_do_capitulo>/`.

| Arquivo | O que é |
| --- | --- |
| `<obra>_capitulo_<número>.pdf` | **O capítulo traduzido** |
| `pages/` | As páginas finais, uma imagem por página |
| `input/` | As imagens originais que foram baixadas |
| `quality_report.html` | Relatório de qualidade, legível no navegador |
| `download_report.html` | Relatório da coleta das páginas |
| `timing_report.txt` | Quanto tempo cada etapa levou |

A forma mais fácil de chegar lá é pela aba **Capítulos traduzidos**, que abre o PDF ou a
pasta para você.

## 16. Biblioteca: capítulos traduzidos

A aba **Capítulos traduzidos** é sua biblioteca local. Ela tem busca por nome e mostra o
total de capítulos.

Cada item permite abrir o PDF, abrir a pasta e ver os relatórios da execução.

Capítulos traduzidos em versões antigas do programa também aparecem: eles são descobertos
pela pasta `output/`, mesmo sem registro interno.

## 17. Traduzir vários capítulos de uma vez

A aba **Fila** serve para lotes — ideal para vários capítulos da mesma série.

1. Cole uma URL em **URL do capítulo** e clique em **Adicionar à fila** (ou pressione
   **Enter**);
2. Repita quantas vezes quiser;
3. Clique em **Iniciar fila**.

Os capítulos são processados **um por vez, em sequência**. Você pode **limpar** a fila ou
**cancelar** o processamento.

> 💡 Um de cada vez é proposital: leitura de imagem e tradução consomem bastante memória, e
> processar vários em paralelo tornaria tudo mais lento e menos confiável.

## 18. Comunidade

A aba **Comunidade** permite descobrir traduções, acompanhar obras e compartilhar
projetos.

> 🔒 **Nada do seu computador vai para a comunidade sozinho.** Terminar uma tradução,
> exportar, salvar ou reiniciar o programa **não publica nada**. O único caminho para um
> arquivo sair da sua máquina é você abrir **Publicar na comunidade** e confirmar.

Ao publicar, você escolhe a **visibilidade** e se **permite comentários** naquela
publicação. A publicação só fica visível para os outros **depois** que o envio do arquivo é
confirmado.

Se a comunidade não estiver configurada no seu ambiente, a área simplesmente não fica
disponível — isso é esperado e não é um erro do seu computador.

## 19. Configurações

Principais preferências disponíveis:

- **Geral** — idioma da interface (Português, English, Español, Français, 日本語, 한국어),
  tema, página inicial, abrir pasta ao terminar, notificações locais, confirmar ações
  destrutivas;
- **Processamento** — modo padrão, escopo padrão, usar cache quando seguro, contexto
  temporário, concorrência segura;
- **Arquivos** — retenção de outputs, limite de cache, confirmar antes de apagar, limpeza
  manual de temporários;
- **Acessibilidade e privacidade** — reduzir animações, aumentar contraste, escala da
  interface, tooltips, atalhos de teclado, limpar sessão, limpar dados locais;
- **Perfil** — perfil público, mostrar status online;
- **Comunidade** — notificações de comentários, respostas, moderação e atualizações.

## 20. Mensagens de erro e o que fazer

As mensagens abaixo são as que o programa realmente mostra.

### Problemas com o serviço de processamento

| O que você vê | O que significa | O que fazer |
| --- | --- | --- |
| *Serviço de processamento indisponível* | O serviço que faz o trabalho pesado não está ligado | Feche tudo e abra novamente pelo `start_tradutor.bat`. Se voltar a acontecer, veja a [seção 21](#21-como-reiniciar-com-segurança) |
| Capítulo parado em **na fila** e nada acontece | Não há serviço de processamento ativo | Mesmo procedimento acima |

### Problemas com a origem do capítulo

| O que você vê | O que significa | O que fazer |
| --- | --- | --- |
| *Não foi possível analisar a fonte.* | O programa não conseguiu entender aquela página | Confira o endereço; tente o link direto do capítulo |
| *Esta fonte ainda não é suportada.* | O site não é reconhecido | Use um site suportado ou baixe as imagens e use **Pasta local** |
| *Essa fonte exige autenticação.* | O capítulo está atrás de login | Não é possível seguir por aqui |
| *A fonte exige uma verificação interativa.* | Há um desafio anti-robô na página | Não é possível seguir por aqui |
| *A fonte recusou o acesso público.* | O site bloqueou o acesso | Tente mais tarde ou use outra origem |
| *A fonte limitou temporariamente o acesso.* | Muitos acessos em pouco tempo | Espere alguns minutos |
| *A página da fonte não está disponível.* | O endereço não respondeu | Confira se o capítulo ainda existe |
| *Nenhuma página do capítulo foi encontrada.* | Nada reconhecível como página | Confira se é mesmo a página do capítulo |
| *Não foi possível carregar todas as páginas do leitor.* | O leitor não terminou de entregar as páginas | Tente novamente; conexões lentas atrapalham |
| *Algumas páginas não puderam ser baixadas.* | O capítulo ficaria incompleto | O programa prefere parar a entregar capítulo faltando página |

### Problemas com o navegador

| O que você vê | O que fazer |
| --- | --- |
| *Nenhum navegador compatível foi encontrado no computador.* | Instale o Google Chrome |
| *O navegador configurado não foi encontrado.* | Confirme se o Chrome continua instalado |
| *O navegador foi encontrado, mas seu driver não está disponível.* | Peça ajuda técnica — falta um componente do navegador |
| *A versão do driver não é compatível com o navegador instalado.* | O Chrome foi atualizado; o componente precisa ser atualizado junto |
| *O navegador ultrapassou o tempo limite de inicialização.* | Feche janelas do Chrome e tente de novo |
| *O navegador encerrou antes de concluir a análise.* | Tente novamente; se repetir, reinicie o computador |

### Problemas de conexão

| O que você vê | O que fazer |
| --- | --- |
| *O site demorou demais para responder.* | Verifique sua internet e tente de novo |
| *Não foi possível conectar ao site.* | Verifique sua internet |
| *Ocorreu um problema ao baixar as imagens.* | Tente novamente |
| *A página ultrapassou o tempo limite de navegação.* | Conexão lenta ou site sobrecarregado |

### Outros

| O que você vê | O que significa | O que fazer |
| --- | --- | --- |
| *Disco cheio ao gravar as páginas baixadas.* | Sem espaço | Libere espaço e tente de novo |
| *O processamento foi cancelado.* | Você cancelou | Nada a fazer |
| *O PDF foi criado, mas alguns itens precisam de revisão.* | Terminou com avisos | Veja a [seção 14](#14-revisão-necessária-o-que-significa) |
| *Configure o arquivo .env e a NVIDIA_API_KEY antes de processar.* | Falta configurar a chave do serviço de tradução | Peça ajuda a quem preparou sua instalação. **Observação:** esta mensagem cita um serviço antigo; o serviço padrão hoje é o DeepL |

### Problemas com a leitura do texto ou a tradução

Falhas nessas etapas aparecem no cartão de status como **erro**, com a mensagem específica
do momento. Regra prática:

- Se o texto ficou lido errado em algumas partes → o capítulo termina em
  **revisão necessária**, e você corrige na tela de revisão;
- Se o serviço de tradução não respondeu ou a chave está faltando → o capítulo termina em
  **erro** e nenhuma tradução parcial é inventada. O programa **nunca** troca de serviço de
  tradução silenciosamente.

## 21. Como reiniciar com segurança

1. Feche a aba do navegador;
2. Feche a janela do programa, se houver uma aberta;
3. Espere alguns segundos;
4. Abra o `start_tradutor.bat` novamente.

Isso é seguro. Traduções já concluídas continuam na biblioteca, e capítulos que estavam no
meio ficam marcados como interrompidos com os arquivos preservados.

> ⚠️ **Não apague** as pastas `.cache` ou `output` para "resolver" um problema. `output`
> contém suas traduções, e `.cache` contém a lista de trabalhos e o reaproveitamento que
> deixa tudo mais rápido.

## 22. Privacidade e segurança

O que a versão atual realmente faz:

- **Suas traduções ficam no seu computador.** Imagens, textos lidos, traduções e PDFs são
  gravados apenas em pastas locais.
- **Nada é publicado automaticamente.** Publicar exige uma ação sua, explícita e
  autenticada.
- **O programa escuta apenas na sua própria máquina** (`127.0.0.1`). Abri-lo para a rede
  exige uma configuração deliberada.
- **Sua senha não é guardada pelo programa.** O login é feito pelo serviço de
  autenticação, e o programa recebe apenas uma sessão temporária.
- **Chaves e senhas são ocultadas nos registros.** O texto que aparece nos Logs passa por
  uma limpeza antes de ser mostrado.
- **A pasta local que você indica não é registrada no histórico** nem copiada para a pasta
  de saída — o programa usa uma cópia temporária interna.

O que a versão atual **não** promete:

- não há garantia de tradução perfeita nem de revisão editorial;
- o programa não obtém direitos sobre o conteúdo que você processa — a responsabilidade
  pelo uso é sua;
- não há criptografia dos arquivos gravados no seu disco.

## 23. Limitações desta versão

- Somente **Windows** foi testado de ponta a ponta;
- Idioma de origem: **inglês**;
- Idioma de destino: **português do Brasil**;
- É necessária **internet** e uma chave válida do serviço de tradução;
- É necessário o **Google Chrome** para capítulos vindos de um endereço;
- Um site que passa na análise **não é necessariamente um site suportado** — sem um
  adaptador específico, o programa pode pedir sua confirmação ou recusar;
- Sites com login, verificação anti-robô ou conteúdo protegido **não são contornados**;
- Um capítulo por vez;
- Efeitos sonoros são **preservados**, não traduzidos, por padrão;
- **Retomar** só aparece para capítulos interrompidos que o programa confirmou como
  recuperáveis (ver [seção 13](#13-se-o-programa-fechar-ou-travar-no-meio));
- Não existe instalador, atualização automática nem controle de licença de testador.

## 24. Perguntas frequentes

**Posso fechar o navegador enquanto traduz?**
Pode. O processamento continua. Reabra `http://127.0.0.1:8080` para acompanhar.

**Posso desligar o computador no meio?**
Pode, mas o capítulo ficará interrompido. Os arquivos produzidos até ali são preservados.
Ao reabrir o programa, se a retomada for possível o botão **Retomar** aparece para esse
capítulo — inclusive depois de atualizar a página, porque essa informação vem do programa,
e não da tela.

**Quanto tempo leva um capítulo?**
Depende do tamanho, da conexão e do computador. Um capítulo completo típico leva vários
minutos. A etapa de leitura do texto costuma ser a mais demorada.

**Por que meu capítulo terminou em "revisão necessária" se está bonito?**
Porque algum item não pôde ser **provado** correto automaticamente — quase sempre um logo,
um efeito sonoro ou uma palavra estilizada. O PDF está pronto para uso.

**Posso traduzir imagens que já baixei?**
Pode. Escolha **Pasta local** na tela de nova tradução. Essa opção só aparece quando o
programa roda no seu próprio computador.

**Por que os efeitos sonoros continuam em inglês?**
É intencional. Efeitos sonoros costumam ser arte desenhada; alterá-los quase sempre piora o
resultado. Eles ficam registrados na revisão.

**Posso mudar o serviço de tradução?**
Pode, no campo **Motor de tradução**, capítulo a capítulo. O padrão é **DeepL
(Qualidade)**.

**O programa vai atualizar sozinho?**
Não nesta versão. A atualização automática está em desenvolvimento.

**Minha tradução aparece para outras pessoas?**
Só se você publicar explicitamente na aba **Comunidade**.

**Onde está o PDF?**
Na pasta `output/`, dentro da subpasta do capítulo. A aba **Capítulos traduzidos** abre
para você.

**A tela ficou parada numa etapa. Travou?**
Nem sempre. Leitura de texto e redesenho podem levar minutos sem mudar o número. Se o
horário da última atualização parar de avançar por muito tempo, veja a
[seção 21](#21-como-reiniciar-com-segurança).

## 25. Precisa de mais ajuda?

1. Confira a tabela de [mensagens de erro](#20-mensagens-de-erro-e-o-que-fazer);
2. Veja a aba **Logs** dentro do programa;
3. Abra o `quality_report.html` da pasta do capítulo — ele explica o que ficou pendente;
4. Se o problema for técnico, o material para quem dá suporte está em
   [Troubleshooting](../TROUBLESHOOTING.md) e na
   [Documentação Técnica](../technical/DOCUMENTACAO_TECNICA.md#28-troubleshooting-técnico).

Ao pedir ajuda, informe: o que você estava fazendo, a mensagem exata que apareceu, e o
nome da pasta do capítulo em `output/`. **Nunca compartilhe sua senha nem sua chave do
serviço de tradução.**

---

### Nota sobre imagens

Este guia ainda **não contém capturas de tela**. Elas não foram incluídas porque as telas
reais só existem depois do login em um ambiente configurado, e capturá-las exigiria expor
dados de conta. Nenhuma imagem foi inventada ou simulada.

As capturas serão adicionadas em `docs/assets/user/` quando puderem ser feitas com uma
conta de demonstração segura. As telas previstas são: acesso, nova tradução, origem
validada, progresso do pipeline, revisão de páginas, revisão de qualidade, biblioteca,
resultado final e **capítulo interrompido com o botão Retomar**.
