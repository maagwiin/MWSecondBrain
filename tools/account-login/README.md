# Account login: validação OAuth

Este helper verifica um requisito antes de implementar chat ou serviços de inferência: uma nova autorização oficial Sign in with ChatGPT e uma inferência curta via Codex App Server. Ele não usa tokens existentes do Codex nem chaves da API paga.

Requisitos: Node.js 22 ou superior, Linux/macOS/WSL e uma instalação compatível do Codex CLI no computador que executará a verificação. Não há dependências npm externas. Os exemplos usam diretórios, usuário e hostname fictícios; substitua-os pelos seus valores.

## O que esta etapa comprova

A documentação oficial descreve registro OSS dinâmico e uso do token OAuth no Codex App Server. Uma autorização válida precisa conceder separadamente a permissão de usar o plano ChatGPT. Nem o catálogo de modelos nem a inicialização do App Server comprovam acesso a uma inferência ou assinatura Pro.

Fontes oficiais:

- [Registro e login](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Contas e sessões](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [VPS autogerenciada](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms)
- [Codex App Server com OAuth do plano](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server)
- [Modelos e inferência](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Uso do plano para usuários](https://learn.chatgpt.com/docs/sign-in-with-chatgpt)

## Login pela VPS com túnel SSH

Na VPS, entre na pasta deste helper e inicie o listener:

```bash
cd /path/to/app/tools/account-login
node cli.mjs login
```

No computador do navegador, mantenha este comando aberto:

```bash
ssh -p YOUR_SSH_PORT -N -o ExitOnForwardFailure=yes -L 1455:127.0.0.1:1455 user@your-vps.example
```

Substitua `YOUR_SSH_PORT` pela porta SSH configurada, por exemplo `22`. Conecte a VPN primeiro se o hostname só estiver acessível por uma rede privada.

Abra `http://127.0.0.1:1455` no Chrome ou Edge externo. Não use um navegador integrado que reescreve a porta local. `localhost:1455` também abre a página; o callback OAuth permanece exatamente `http://127.0.0.1:1455/auth/callback`. Outros hosts e portas são rejeitados com orientação sobre o endereço correto.

Clique em **Continue with ChatGPT**, selecione a conta e revise **Use your ChatGPT plan** no domínio oficial `auth.openai.com`. Nenhuma inferência é executada durante o login. A tentativa expira após 15 minutos e fecha conexões incompletas.

Os códigos e tokens chegam pelo callback e ficam protegidos na VPS. Nunca envie códigos OAuth, tokens, URLs de callback ou arquivos de credenciais pelo chat. O terminal exibe somente o resultado da validação, sem dados da conta.

O uso do túnel SSH é uma adaptação técnica aos requisitos de loopback. O procedimento literal da documentação usa OAuth local e transferência protegida, descrito abaixo.

## Procedimento documentado: OAuth local e transferência

Copie para seu computador apenas `package.json`, `auth.mjs`, `onboarding.mjs` e `cli.mjs`. Execute na pasta local:

```bash
node cli.mjs login
```

Abra o endereço local exibido e conclua a autorização. Prepare uma pasta protegida na VPS:

```bash
ssh -p YOUR_SSH_PORT user@your-vps.example 'mkdir -p /path/to/app/tools/account-login/private && chmod 700 /path/to/app/tools/account-login/private'
```

Transfira apenas o arquivo protegido recém-criado:

```bash
scp -P YOUR_SSH_PORT private/credentials.json user@your-vps.example:/path/to/app/tools/account-login/private/incoming.json
```

Na VPS, execute:

```bash
chmod 600 /path/to/app/tools/account-login/private/incoming.json
node /path/to/app/tools/account-login/cli.mjs import /path/to/app/tools/account-login/private/incoming.json
```

A importação verifica novamente o ID token e conserva o host ID próprio da VPS. O host ID copiado do computador local não substitui esse valor. Encerre o processo local depois da transferência.

## Verificar uma inferência

Depois do consentimento e da validação OAuth, execute na pasta do helper:

```bash
node cli.mjs verify
```

O helper consulta `/v1/models` com o token OAuth novo e escolhe o primeiro modelo visível do catálogo da conta. Para selecionar um modelo específico, acrescente seu slug após `verify`. A mensagem curta usa parte do limite do plano autorizado. Não existe fallback para a API paga.

Somente `turn.status=completed` gera `private/inference-proof.json`. A prova demonstra acesso àquela inferência usando a permissão do plano; ela mantém `plan_tier_verified=false`. Confira a assinatura e o consumo em **ChatGPT Settings > Usage**.

Se a autorização ou inferência falhar, a etapa de validação permanece pendente. O helper não libera automaticamente chat nem serviços posteriores.

## Proteção e limites

- `private/` usa modo 0700. Os arquivos protegidos usam 0600 e substituição atômica.
- O host ID fica em `private/host.json`, estável entre reinícios. Cada registro conserva o `client_id` emitido e a identidade validada.
- PKCE, state e nonce são novos em cada tentativa. O ID token exige assinatura JWKS RS256, issuer, audience, expiração, nonce e identidade corretos.
- Credenciais existentes só são substituídas depois da validação. Reautorização reutiliza o client ID emitido.
- O processo filho usa um diretório próprio para `CODEX_HOME`; a variável da sessão e as configurações globais do Codex permanecem preservadas. API keys herdadas não entram no processo filho.
- A verificação usa workspace vazio em `private/runtime/workspace`, prompt fixo sem ferramentas e sandbox readOnly com rede de ferramentas desativada. O processo filho desliga shell, unified exec, visualização de imagem, apps, plugins e web search. Pedidos RPC de ferramenta são recusados.
- Compatibilidade com Codex 0.160.1: `readOnly.access` é rejeitado com RPC -32600. O helper usa a forma suportada `{type: "readOnly", networkAccess: false}`. Esta etapa não comprova confinamento de leitura a um diretório; isso exige um permissionProfile verificado antes de implementar o runtime final.
- Tokens não entram no armazenamento do navegador, nos logs nem no controle de versão. `private/` está no `.gitignore`.
- Este helper atende a uma conta selecionada. Não implementa chat, múltiplas contas, logout nem refresh contínuo. Quando o token expira, execute `login` novamente. Para desconectar a autorização, use ChatGPT Settings.
- `MWSB_AUTH_DIR` permite escolher um diretório privado fora da árvore de código. Use armazenamento persistente protegido para preservar o host ID e a autorização. Não publique o listener de loopback.

## Testes

```bash
npm test
```

Os 17 testes exercitam PKCE, callback adulterado, vínculo de client ID, JWT inválido, permissão do plano, modos de arquivo, host ID, aliases locais, expiração com sockets abertos, importação protegida, compatibilidade do RPC e condição estrita de inferência concluída. Exchanges OAuth e conclusões de inferência são simulados; os testes não comprovam acesso real à conta e não consomem limites do plano.

Para verificar o handshake real do App Server sem inferência, execute separadamente:

```bash
node cli.mjs smoke
```

O smoke cria um runtime privado local e mantém `inference_verified=false`.
