import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {generateKeyPairSync,sign} from 'node:crypto';
import {atomicWrite,readProtected} from './auth.mjs';
import {refreshCredentials} from './refresh.mjs';

const original={issuer:'https://auth.openai.com',subject:'account-fixture',client_id:'oaiapp_fixture',access_token:'old-access',refresh_token:'old-refresh',id_token:'old-id',token_type:'Bearer',expires_in:3600,scopes:['openid','offline_access','resource.invoke','chatgpt.tokens.use.direct'],oidc_nonce:'login-nonce',ext_agent_host_id:'urn:uuid:fixture',saved_at:'2000-01-01T00:00:00Z'};

test('refresh preserves registration and atomically rotates credentials with verified identity',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'mwsb-refresh-'));
  await atomicWrite(join(dir,'credentials.json'),original);
  const {privateKey,publicKey}=generateKeyPairSync('rsa',{modulusLength:2048});
  const encode=x=>Buffer.from(JSON.stringify(x)).toString('base64url');
  const input=`${encode({alg:'RS256',kid:'rotation'})}.${encode({iss:'https://auth.openai.com',aud:'oaiapp_fixture',sub:'account-fixture',exp:Date.now()/1000+300})}`;
  const id=`${input}.${sign('RSA-SHA256',Buffer.from(input),privateKey).toString('base64url')}`;
  const fetcher=async(url,options)=>{
    if(url.endsWith('/oauth/token')){
      const form=new URLSearchParams(options.body);
      assert.equal(form.get('client_id'),'oaiapp_fixture');
      assert.equal(form.get('refresh_token'),'old-refresh');
      assert.equal(form.get('grant_type'),'refresh_token');
      assert.equal(form.has('scope'),false);
      return Response.json({access_token:'replacement-access',refresh_token:'replacement-refresh',id_token:id,token_type:'Bearer',expires_in:3600});
    }
    assert.equal(url,'https://auth.openai.com/.well-known/jwks.json');
    return Response.json({keys:[{...publicKey.export({format:'jwk'}),kid:'rotation'}]});
  };
  await refreshCredentials(dir,{fetcher});
  const record=await readProtected(join(dir,'credentials.json'));
  assert.equal(record.client_id,'oaiapp_fixture');
  assert.equal(record.subject,'account-fixture');
  assert.equal(record.ext_agent_host_id,'urn:uuid:fixture');
  assert.equal(record.access_token,'replacement-access');
  assert.equal(record.refresh_token,'replacement-refresh');
});

test('revoked refresh produces reconnect code without leaking response or replacing credentials',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'mwsb-refresh-revoked-'));
  await atomicWrite(join(dir,'credentials.json'),original);
  await assert.rejects(()=>refreshCredentials(dir,{fetcher:async()=>Response.json({error:'invalid_grant',detail:'SECRET-never-output'},{status:400})}),error=>{
    assert.equal(error.code,'auth_required');
    assert.doesNotMatch(error.message,/SECRET/);
    return true;
  });
  assert.equal((await readProtected(join(dir,'credentials.json'))).refresh_token,'old-refresh');
});

test('reduced scope does not replace a valid registration',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'mwsb-refresh-scope-'));
  await atomicWrite(join(dir,'credentials.json'),original);
  await assert.rejects(()=>refreshCredentials(dir,{fetcher:async()=>Response.json({access_token:'replacement',token_type:'Bearer',expires_in:3600,scope:'openid'})}));
  assert.equal((await readProtected(join(dir,'credentials.json'))).access_token,'old-access');
});
