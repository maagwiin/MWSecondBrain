import {createServer} from 'node:http';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {mkdir} from 'node:fs/promises';
import {join} from 'node:path';
import {attempt,acceptCallback,verifyIdToken,credentialsFrom,atomicWrite,readProtected,hostId,ISSUER,RESOURCE,REDIRECT} from './auth.mjs';

export async function importRegistration({dir,source,fetcher=fetch}) {
  const record=await readProtected(source);
  const jwks=await officialJSON(`${ISSUER}/.well-known/jwks.json`,{},fetcher);
  verifyIdToken(record.id_token,jwks,{clientId:record.client_id,nonce:record.oidc_nonce,subject:record.subject});
  if(!record.access_token || !record.refresh_token || !record.scopes?.includes('chatgpt.tokens.use.direct') || !record.scopes?.includes('resource.invoke'))throw new Error('ChatGPT plan permission or credentials missing');
  const vmHost=await hostId(dir);
  await atomicWrite(join(dir,'credentials.json'),{...record,ext_agent_host_id:vmHost});
  return {imported:true,vm_host_preserved:true,inference_verified:false};
}

export async function officialJSON(url,options={},fetcher=fetch) {
  const response = await fetcher(url,{...options,redirect:'error',signal:AbortSignal.timeout(15000)});
  if (!response.ok) throw new Error(`OpenAI endpoint returned HTTP ${response.status}`);
  return response.json();
}

export async function startLogin({dir,port=1455,fetcher=fetch,attemptTimeoutMs=15*60*1000}) {
  if(!Number.isFinite(attemptTimeoutMs) || attemptTimeoutMs<=0 || attemptTimeoutMs>15*60*1000)throw new Error('Invalid OAuth attempt lifetime');
  const host = await hostId(dir);
  let saved;
  try {saved = await readProtected(join(dir,'credentials.json'));} catch (error) {if (error.code !== 'ENOENT') throw error;}
  const pending = attempt(host,saved);
  let used = false, busy = false, resolveDone, rejectDone;
  const done = new Promise((resolve,reject)=>{resolveDone=resolve;rejectDone=reject;});
  const html = (body) => `<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>MWSecondBrain</title><body><h1>MWSecondBrain</h1>${body}</body></html>`;
  const sockets=new Set();
  const server = createServer(async(request,response)=>{
    response.setHeader('Cache-Control','no-store');
    response.setHeader('Referrer-Policy','no-referrer');
    response.setHeader('Content-Security-Policy',"default-src 'none'; frame-ancestors 'none'; base-uri 'none'");
    response.setHeader('Content-Type','text/html; charset=utf-8');
    const reply = (status,text) => {response.writeHead(status);response.end(html(text));};
    const listenerPort=server.address().port;
    const requiredUrl=`http://127.0.0.1:${listenerPort}`;
    const allowedHosts=new Set([`127.0.0.1:${listenerPort}`,`localhost:${listenerPort}`]);
    if (!allowedHosts.has(request.headers.host)) return reply(400,`<p>Endereço local incorreto. Abra <a href="${requiredUrl}">${requiredUrl}</a> no Chrome ou Edge externo, com o túnel SSH conectado. A porta deve ser ${listenerPort}; não use o endereço reescrito do preview.</p>`);
    if(request.method !== 'GET')return reply(405,'Use o endereço local no navegador.');
    const url = new URL(request.url,'http://127.0.0.1');
    if (url.pathname === '/') {
      if (used || busy) return reply(409,'Tentativa encerrada ou em andamento.');
      return reply(200,`<p>Conecte sua conta ChatGPT e revise a permissão de usar seu plano. Nenhuma inferência é executada nesta etapa.</p><p><a href="${pending.url.replaceAll('&','&amp;')}" rel="noreferrer">Continue with ChatGPT</a></p><p>Não envie credenciais pelo chat.</p>`);
    }
    if (url.pathname !== '/auth/callback') return reply(404,'Página inexistente.');
    if (used || busy) return reply(409,'Tentativa já utilizada.');
    let callback;
    try {callback=acceptCallback(url,pending);} catch (error) {
      reply(400,'Callback inválido ou autorização recusada. Nenhuma credencial alterada.');
      if (url.searchParams.get('state') === pending.state && url.searchParams.has('error')) {used=true;rejectDone(new Error('OAuth denied or failed'));}
      return;
    }
    used=true;busy=true;
    try {
      const body = new URLSearchParams({grant_type:'authorization_code',client_id:callback.clientId,code:callback.code,code_verifier:pending.verifier,redirect_uri:REDIRECT,resource:RESOURCE});
      const tokens = await officialJSON(`${ISSUER}/api/accounts/oauth/token`,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body},fetcher);
      const jwks = await officialJSON(`${ISSUER}/.well-known/jwks.json`,{},fetcher);
      const claims=verifyIdToken(tokens.id_token,jwks,{clientId:callback.clientId,nonce:pending.nonce,subject:saved?.subject});
      const record=credentialsFrom(tokens,claims,callback.clientId,host);
      await atomicWrite(join(dir,'credentials.json'),record);
      reply(200,'Conta e permissão do plano validadas. Credenciais salvas com proteção local. Feche esta janela. Inferência ainda não foi testada.');
      resolveDone({oauth_validated:true,plan_scope_granted:true,inference_verified:false});
    } catch (error) {reply(400,'Validação falhou. Nenhuma credencial alterada. Reinicie o login.');rejectDone(error);} finally {busy=false;}
  });
  server.on('connection',socket=>{sockets.add(socket);socket.once('close',()=>sockets.delete(socket));});
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(port,'127.0.0.1',resolve);});
  const close=(force=false)=>{
    clearTimeout(timer);
    server.close();
    const disconnect=()=>{for(const socket of sockets)socket.destroy();};
    if(force)disconnect();else setTimeout(disconnect,100).unref();
  };
  const timer=setTimeout(()=>{
    rejectDone(new Error('OAuth attempt expired; start a fresh login'));
    close(true);
  },attemptTimeoutMs);
  done.then(()=>close(),()=>close());
  return {localUrl:`http://127.0.0.1:${server.address().port}`,pending,done,close};
}

export function childEnvironment(runtime,token) {
  const environment={CODEX_HOME:runtime,ACCESS_TOKEN:token};
  for (const key of ['PATH','LANG','LC_ALL','NODE_EXTRA_CA_CERTS','SSL_CERT_FILE','SSL_CERT_DIR','HTTPS_PROXY','HTTP_PROXY','ALL_PROXY','NO_PROXY']) {
    if (process.env[key]) environment[key]=process.env[key];
  }
  return environment;
}

export async function infer({dir,token,model,codex='codex',smoke=false,timeout=90000}) {
  const runtime=join(dir,'runtime');
  const workspace=join(runtime,'workspace');
  await mkdir(workspace,{recursive:true,mode:0o700});
  const overrides = [
    'model_provider="openai_chatgpt_plan"',
    'model_providers.openai_chatgpt_plan.name="ChatGPT plan"',
    'model_providers.openai_chatgpt_plan.base_url="https://api.openai.com/v1"',
    'model_providers.openai_chatgpt_plan.env_key="ACCESS_TOKEN"',
    'model_providers.openai_chatgpt_plan.wire_api="responses"',
    'model_providers.openai_chatgpt_plan.requires_openai_auth=false',
    'model_providers.openai_chatgpt_plan.supports_websockets=false',
    'analytics.enabled=false',
    'features.shell_tool=false',
    'features.unified_exec=false',
    'features.view_image=false',
    'features.apps=false',
    'features.plugins=false',
    'web_search="disabled"',
  ];
  const child=spawn(codex,['app-server','--listen','stdio://',...overrides.flatMap(value=>['-c',value])],{cwd:workspace,env:childEnvironment(runtime,token),stdio:['pipe','pipe','pipe']});
  child.stderr.resume(); // Never expose app-server logs or token-containing network errors.
  const lines=createInterface({input:child.stdout});
  let sequence=0, completed, threadId, turnId;
  const requests=new Map();
  const completions=new Map();
  const completion=new Promise((resolve,reject)=>{completed={resolve,reject};});
  completion.catch(()=>{});
  const send=message=>child.stdin.write(JSON.stringify(message)+'\n');
  const call=(method,params)=>new Promise((resolve,reject)=>{const id=++sequence;requests.set(id,{resolve,reject,method});send({id,method,params});});
  const fail=error=>{for(const request of requests.values())request.reject(error);requests.clear();completed.reject(error);};
  child.on('error',()=>fail(new Error('Cannot start Codex app-server')));
  child.on('exit',()=>fail(new Error('Codex app-server exited before completion')));
  child.stdin.on('error',()=>fail(new Error('Codex app-server input closed')));
  lines.on('line',line=>{
    let message;
    try {message=JSON.parse(line);} catch {return fail(new Error('Invalid app-server protocol response'));}
    if (message.id !== undefined && message.method) {
      send({id:message.id,error:{code:-32601,message:'Auth validation does not execute tools'}});
      return;
    }
    if (message.id !== undefined && requests.has(message.id)) {
      const request=requests.get(message.id);requests.delete(message.id);
      if(message.error) {
        const compatibility=message.error.message==='Invalid request: readOnly.access is no longer supported; use permissionProfile for restricted reads' ? '; readOnly.access unsupported; use permissionProfile' : '';
        request.reject(new Error(`App-server RPC failed: ${request.method} (${Number.isInteger(message.error.code)?message.error.code:'unknown'})${compatibility}`));
      } else request.resolve(message.result);
    }
    if(message.method==='turn/completed' && message.params?.threadId===threadId){
      const turn=message.params.turn;
      completions.set(turn.id,turn);
      if(turn.id===turnId)completed.resolve(turn);
    }
  });
  const timer=setTimeout(()=>fail(new Error('Codex verification timed out')),timeout);
  try {
    await call('initialize',{clientInfo:{name:'MWSecondBrain',title:'MWSecondBrain',version:'0.1.0'}});
    send({method:'initialized',params:{}});
    const thread=await call('thread/start',{model,cwd:workspace,approvalPolicy:'never',sandbox:'read-only',baseInstructions:'This is a single authentication verification. Reply exactly MWSECOND BRAIN_OK. Do not use tools, read files, or execute commands.'});
    if(smoke) return {protocol_initialized:true,thread_started:true,inference_verified:false};
    threadId=thread.thread.id;
    // Codex 0.160.1 rejects legacy readOnly.access; named permission profiles
    // are required for restricted reads. This gate uses supported readOnly
    // without claiming filesystem read confinement.
    const result=await call('turn/start',{threadId,input:[{type:'text',text:'Reply exactly MWSECOND BRAIN_OK. Do not use any tools.'}],approvalPolicy:'never',sandboxPolicy:{type:'readOnly',networkAccess:false}});
    turnId=result.turn.id;
    if(completions.has(turnId))completed.resolve(completions.get(turnId));
    const turn=await completion;
    if(turn.status!=='completed')throw new Error(`Inference not completed (${turn.status})`);
    return {model,thread_id:threadId,turn_id:turn.id,turn_status:turn.status,inference_verified:true,verified_at:new Date().toISOString()};
  } finally {clearTimeout(timer);lines.close();child.kill();}
}
