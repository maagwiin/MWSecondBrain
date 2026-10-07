import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, writeFile, chmod, readdir} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {connect} from 'node:net';
import {request} from 'node:http';
import {generateKeyPairSync,sign} from 'node:crypto';
import {startLogin,infer,childEnvironment,importRegistration} from './onboarding.mjs';
import {readProtected,atomicWrite,hostId} from './auth.mjs';

function requestWithHost(url,host) {
  return new Promise((resolve,reject)=>{
    const outgoing=request(url,{headers:{Host:host}},response=>{
      let body='';
      response.setEncoding('utf8');
      response.on('data',chunk=>body+=chunk);
      response.on('end',()=>resolve({status:response.statusCode,body}));
    });
    outgoing.on('error',reject);
    outgoing.end();
  });
}

test('localhost alias works only on actual listener port; mismatched Host explains exact URL', async () => {
  const dir=await mkdtemp(join(tmpdir(),'mwsb-host-header-'));
  const session=await startLogin({dir,port:0});
  const port=new URL(session.localUrl).port;
  try {
    const valid=await requestWithHost(session.localUrl,`localhost:${port}`);
    assert.equal(valid.status,200);
    assert.match(valid.body,/Continue with ChatGPT/);
    for(const host of ['localhost:64455','evil.example:'+port,'127.0.0.1:64455']) {
      if(host===`localhost:${port}` || host===`127.0.0.1:${port}`)continue;
      const invalid=await requestWithHost(session.localUrl,host);
      assert.equal(invalid.status,400);
      assert.ok(invalid.body.includes(session.localUrl));
    }
  } finally {session.close();}
});

test('expired attempt closes incomplete connections and releases listener', async () => {
  const dir=await mkdtemp(join(tmpdir(),'mwsb-expiry-'));
  const session=await startLogin({dir,port:0,attemptTimeoutMs:100});
  const port=Number(new URL(session.localUrl).port);
  const rejected=assert.rejects(session.done,/expired/);
  const socket=connect(port,'127.0.0.1');
  socket.on('error',()=>{});
  await new Promise(resolve=>socket.once('connect',resolve));
  socket.write('GET / HTTP/1.1\r\nHost: 127.0.0.1');
  const closed=new Promise(resolve=>socket.once('close',resolve));
  try {
    await Promise.race([Promise.all([rejected,closed]),new Promise((_,reject)=>setTimeout(()=>reject(new Error('Expiry left socket open')),400).unref())]);
    await assert.rejects(()=>fetch(session.localUrl));
  } finally {socket.destroy();session.close();}
});

test('HTTP onboarding completes only one validated callback and hides credentials', async () => {
  const dir = await mkdtemp(join(tmpdir(),'mwsb-http-'));
  const {privateKey,publicKey} = generateKeyPairSync('rsa',{modulusLength:2048});
  let server, exchanges = 0;
  const exchange = async (endpoint,options) => {
    if (endpoint.endsWith('/oauth/token')) {
      exchanges++;
      const form = new URLSearchParams(options.body);
      assert.equal(form.get('client_id'),'oaiapp_new');
      assert.equal(form.get('redirect_uri'),'http://127.0.0.1:1455/auth/callback');
      assert.equal(form.get('code_verifier'),server.pending.verifier);
      const parts = [{alg:'RS256',kid:'http-fixture'},{iss:'https://auth.openai.com',aud:'oaiapp_new',sub:'test-account',nonce:server.pending.nonce,exp:Date.now()/1000+3600}].map(x=>Buffer.from(JSON.stringify(x)).toString('base64url'));
      const input=parts.join('.');
      return Response.json({access_token:'never-print-access',refresh_token:'never-print-refresh',id_token:`${input}.${sign('RSA-SHA256',Buffer.from(input),privateKey).toString('base64url')}`,token_type:'Bearer',expires_in:3600,scope:'openid resource.invoke chatgpt.tokens.use.direct offline_access'});
    }
    assert.equal(endpoint,'https://auth.openai.com/.well-known/jwks.json');
    return Response.json({keys:[{...publicKey.export({format:'jwk'}),kid:'http-fixture'}]});
  };
  server = await startLogin({dir,port:0,fetcher:exchange});
  try {
    const page=await fetch(server.localUrl);
    assert.match(await page.text(),/Continue with ChatGPT/);
    const wrong=await fetch(`${server.localUrl}/auth/callback?state=wrong&code=x&client_id=oaiapp_new`);
    assert.equal(wrong.status,400);
    assert.equal(exchanges,0);
    const granted=await fetch(`${server.localUrl}/auth/callback?state=${server.pending.state}&code=one-use&client_id=oaiapp_new`);
    const text=await granted.text();
    assert.equal(granted.status,200);
    assert.doesNotMatch(text,/never-print|id_token|refresh_token|access_token/);
    await server.done;
    const record=await readProtected(join(dir,'credentials.json'));
    assert.equal(record.access_token,'never-print-access');
    assert.equal(exchanges,1);
  } finally {server.close();}
});

async function fakeCodex(dir,status) {
  const file=join(dir,'fake-codex');
  await writeFile(file,`#!/usr/bin/env node\nconst readline=require('node:readline');\nconst send=x=>console.log(JSON.stringify(x));\nreadline.createInterface({input:process.stdin}).on('line',line=>{const x=JSON.parse(line);if(x.method==='initialize')send({id:x.id,result:{userAgent:'fake'}});if(x.method==='thread/start')send({id:x.id,result:{thread:{id:'thread-1'}}});if(x.method==='turn/start'){if(x.params.sandboxPolicy?.access || '${status}'==='rpc-error'){send({id:x.id,error:{code:-32600,message:'Invalid request: secret-fixture-must-never-print'}});return;}send({id:x.id,result:{turn:{id:'turn-1',status:'inProgress'}}});send({method:'turn/completed',params:{threadId:'thread-1',turn:{id:'turn-1',status:'${status}'}}});}});\n`);
  await chmod(file,0o700);
  return file;
}
test('inference reports success only after completed turn, with isolated environment', async () => {
  const dir=await mkdtemp(join(tmpdir(),'mwsb-infer-'));
  const result=await infer({dir,token:'fresh-oauth',model:'test-model',codex:await fakeCodex(dir,'completed')});
  assert.equal(result.turn_status,'completed');
  assert.equal(result.model,'test-model');
  assert.ok((await readdir(dir)).includes('runtime'));
});
test('failed turn never becomes entitlement proof', async () => {
  const dir=await mkdtemp(join(tmpdir(),'mwsb-infer-fail-'));
  const codex = await fakeCodex(dir,'failed');
  await assert.rejects(()=>infer({dir,token:'fresh-oauth',model:'test-model',codex}),/not completed/);
});
test('RPC rejection identifies method without exposing server message contents', async () => {
  const dir=await mkdtemp(join(tmpdir(),'mwsb-rpc-error-'));
  const codex=await fakeCodex(dir,'rpc-error');
  await assert.rejects(()=>infer({dir,token:'fresh-oauth',model:'test-model',codex}),error=>{
    assert.match(error.message,/turn\/start.*-32600/);
    assert.doesNotMatch(error.message,/secret-fixture/);
    return true;
  });
});
test('paid API keys and global provider settings never reach child', () => {
  const previous=process.env.OPENAI_API_KEY;
  process.env.OPENAI_API_KEY='fixture-paid-key';
  try {
    const environment=childEnvironment('/tmp/fresh-runtime','fixture-oauth');
    assert.equal(environment.CODEX_HOME,'/tmp/fresh-runtime');
    assert.equal(environment.ACCESS_TOKEN,'fixture-oauth');
    assert.equal(environment.OPENAI_API_KEY,undefined);
    assert.equal(environment.HOME,undefined);
  } finally {
    if(previous===undefined)delete process.env.OPENAI_API_KEY;else process.env.OPENAI_API_KEY=previous;
  }
});
test('local credential import preserves VM host rather than copied laptop ID', async () => {
  const dir=await mkdtemp(join(tmpdir(),'mwsb-import-'));
  const vm=join(dir,'vm');
  const vmId=await hostId(vm);
  const {privateKey,publicKey}=generateKeyPairSync('rsa',{modulusLength:2048});
  const encode=x=>Buffer.from(JSON.stringify(x)).toString('base64url');
  const input=`${encode({alg:'RS256',kid:'import-key'})}.${encode({iss:'https://auth.openai.com',aud:'oaiapp_laptop',sub:'saved-account',nonce:'import-nonce',exp:Date.now()/1000+300})}`;
  const source=join(dir,'local','credentials.json');
  await atomicWrite(source,{client_id:'oaiapp_laptop',subject:'saved-account',oidc_nonce:'import-nonce',ext_agent_host_id:'urn:uuid:laptop',id_token:`${input}.${sign('RSA-SHA256',Buffer.from(input),privateKey).toString('base64url')}`,access_token:'protected-access',refresh_token:'protected-refresh',scopes:['resource.invoke','chatgpt.tokens.use.direct']});
  await importRegistration({dir:vm,source,fetcher:async()=>Response.json({keys:[{...publicKey.export({format:'jwk'}),kid:'import-key'}]})});
  const imported=await readProtected(join(vm,'credentials.json'));
  assert.equal(imported.ext_agent_host_id,vmId);
  assert.equal(imported.client_id,'oaiapp_laptop');
  assert.equal(imported.access_token,'protected-access');
});
