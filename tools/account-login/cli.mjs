import {fileURLToPath} from 'node:url';
import {dirname,join,resolve} from 'node:path';
import {startLogin,infer,officialJSON,importRegistration} from './onboarding.mjs';
import {atomicWrite,readProtected,RESOURCE} from './auth.mjs';

const dir=resolve(process.env.MWSB_AUTH_DIR ?? join(dirname(fileURLToPath(import.meta.url)),'private'));
const command=process.argv[2];

async function main() {
  if(command==='login') {
    const session=await startLogin({dir});
    console.log(`Abra ${session.localUrl} no computador do navegador. Se usar VPS, estabeleça o túnel SSH primeiro.`);
    console.log('Revise Use your ChatGPT plan. Não envie códigos, tokens nem arquivo de credenciais pelo chat.');
    process.once('SIGINT',()=>{session.close();process.exit(130);});
    console.log(JSON.stringify(await session.done));
    return;
  }
  if(command==='smoke') {
    console.log(JSON.stringify(await infer({dir,token:'unused-no-oauth',smoke:true,timeout:15000})));
    return;
  }
  if(command==='import') {
    if(!process.argv[3])throw new Error('Indique somente o caminho do arquivo protegido recebido via SSH.');
    console.log(JSON.stringify(await importRegistration({dir,source:resolve(process.argv[3])})));
    return;
  }
  if(command==='verify') {
    const record=await readProtected(join(dir,'credentials.json'));
    if(!record.scopes?.includes('chatgpt.tokens.use.direct') || !record.scopes?.includes('resource.invoke'))throw new Error('Permissão de usar plano ausente.');
    const expires=Date.parse(record.saved_at)+record.expires_in*1000;
    if(!Number.isFinite(expires) || expires<=Date.now()+30000)throw new Error('OAuth expirado. Execute login novamente antes da verificação.');
    const catalog=await officialJSON(`${RESOURCE}/models`,{headers:{Authorization:`Bearer ${record.access_token}`}});
    const models=catalog.models?.filter(model=>model.visibility==='list') ?? [];
    const model=process.argv[3] ?? models[0]?.slug;
    if(!model || !models.some(item=>item.slug===model))throw new Error('Modelo solicitado ausente do catálogo da conta.');
    console.log(`Verificando uma inferência curta com ${model} usando a permissão do plano ChatGPT.`);
    const proof=await infer({dir,token:record.access_token,model});
    await atomicWrite(join(dir,'inference-proof.json'),{...proof,oauth_validated:true,plan_scope_granted:true,plan_tier_verified:false});
    console.log(JSON.stringify({...proof,plan_scope_granted:true,plan_tier_verified:false}));
    return;
  }
  console.log('Uso: node cli.mjs login | import ARQUIVO | smoke | verify [MODELO]');
  console.log('Node >=22. Dados privados em private/; MWSB_AUTH_DIR define um diretório próprio.');
}

main().catch(error=>{
  // Do not expose HTTP bodies, credentials, token hints, or app-server stderr.
  const known=/^(OpenAI endpoint returned HTTP|App-server RPC failed|Inference not completed|Codex|Cannot start Codex|OAuth|ID token|ChatGPT plan|Permissão|Modelo|Indique|Incomplete|Invalid|Unsafe|Unsupported|Tentativa)/;
  console.error(error.code==='ENOENT'?'Credenciais próprias ausentes. Execute login antes de verify.':known.test(error.message)?error.message:'Etapa falhou. Nenhuma prova de acesso foi emitida.');
  process.exitCode=1;
});
