import test from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPairSync, sign, createHash } from 'node:crypto';
import { mkdtemp, readFile, stat, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { attempt, acceptCallback, verifyIdToken, credentialsFrom, atomicWrite, hostId } from './auth.mjs';

const {privateKey, publicKey} = generateKeyPairSync('rsa', {modulusLength: 2048});
const jwk = {...publicKey.export({format:'jwk'}), kid:'fixture', alg:'RS256', use:'sig'};
const claims = {iss:'https://auth.openai.com',aud:'oaiapp_fixture',sub:'account-1',nonce:'fresh-nonce',exp:2000000100,email:'fixture@example.test'};
function jwt(patch = {}, header = {}) {
  const encode = x => Buffer.from(JSON.stringify(x)).toString('base64url');
  const input = `${encode({alg:'RS256',kid:'fixture',...header})}.${encode({...claims,...patch})}`;
  return `${input}.${sign('RSA-SHA256', Buffer.from(input), privateKey).toString('base64url')}`;
}
const validation = {clientId:'oaiapp_fixture',nonce:'fresh-nonce',now:2000000000};

test('OAuth challenge protects a fresh verifier and identity across attempts', () => {
  const first = attempt('urn:uuid:host');
  const second = attempt('urn:uuid:host');
  const url = new URL(first.url);
  assert.equal(url.searchParams.get('redirect_uri'), 'http://127.0.0.1:1455/auth/callback');
  assert.equal(url.searchParams.get('client_id'), 'dynamic_agent_client');
  assert.equal(url.searchParams.get('agent_name_hint'), 'MWSecondBrain');
  assert.equal(url.searchParams.get('resource'), 'https://api.openai.com/v1');
  assert.equal(url.searchParams.get('code_challenge'), createHash('sha256').update(first.verifier).digest('base64url'));
  assert.ok(first.verifier.length >= 43);
  assert.notEqual(first.state, second.state);
  assert.notEqual(first.nonce, second.nonce);
});
test('valid callback returns issued ID and code', () => {
  assert.deepEqual(acceptCallback(new URL('http://127.0.0.1/auth/callback?state=expected&code=short-lived&client_id=oaiapp_new'),{state:'expected'}),{code:'short-lived',clientId:'oaiapp_new'});
});
test('reauthorization keeps client binding and omits registration metadata', () => {
  const pending = attempt('urn:uuid:host',{client_id:'oaiapp_saved',id_token:'retained-id',email:'saved@example.test'});
  const url = new URL(pending.url);
  assert.equal(url.searchParams.get('client_id'),'oaiapp_saved');
  assert.equal(url.searchParams.has('agent_name_hint'),false);
  assert.equal(url.searchParams.get('id_token_hint'),'retained-id');
  assert.deepEqual(acceptCallback(new URL(`http://127.0.0.1/auth/callback?state=${pending.state}&code=renew`),pending),{code:'renew',clientId:'oaiapp_saved'});
  assert.throws(() => acceptCallback(new URL(`http://127.0.0.1/auth/callback?state=${pending.state}&code=renew&client_id=oaiapp_wrong`),pending));
});
test('callback rejects CSRF, duplicate values, errors and placeholder client IDs', () => {
  for (const query of ['state=wrong&code=x&client_id=oaiapp_new','state=expected&state=expected&code=x&client_id=oaiapp_new','state=expected&error=access_denied','state=expected&code=x&client_id=dynamic_agent_client','state=expected&code=x']) {
    assert.throws(() => acceptCallback(new URL(`http://127.0.0.1/auth/callback?${query}`),{state:'expected'}));
  }
});
test('valid RS256 signature and bound identity accepted', () => {
  assert.equal(verifyIdToken(jwt(),{keys:[jwk]},validation).sub,'account-1');
});
test('JWT rejects modified signature, issuer, audience, nonce, expiry and algorithm', () => {
  const token = jwt();
  const signature = token.split('.')[2];
  const corrupted = `${token.slice(0, token.lastIndexOf('.')+1)}${signature[0]==='A'?'B':'A'}${signature.slice(1)}`;
  for (const bad of [corrupted,jwt({iss:'https://attacker.test'}),jwt({aud:'other-client'}),jwt({nonce:'old'}),jwt({exp:2000000000}),jwt({nbf:2000001000}),jwt({}, {alg:'none'}),jwt({}, {kid:'unknown'})]) {
    assert.throws(() => verifyIdToken(bad,{keys:[jwk]},validation));
  }
});
test('credentials require independently granted plan-use scope', () => {
  const tokens = {id_token:'opaque-id',access_token:'opaque-access',refresh_token:'opaque-refresh',token_type:'Bearer',expires_in:3600,scope:'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'};
  const record = credentialsFrom(tokens,claims,'oaiapp_fixture','urn:uuid:vm');
  assert.equal(record.client_id,'oaiapp_fixture');
  assert.equal(record.ext_agent_host_id,'urn:uuid:vm');
  assert.ok(record.scopes.includes('chatgpt.tokens.use.direct'));
  assert.throws(() => credentialsFrom({...tokens,scope:'openid profile email'},claims,'oaiapp_fixture','urn:uuid:vm'));
  assert.throws(() => credentialsFrom({...tokens,token_type:'Basic'},claims,'oaiapp_fixture','urn:uuid:vm'));
});
test('credential directory and replacement file stay owner-only', async () => {
  const root = await mkdtemp(join(tmpdir(),'mwsb-security-'));
  const file = join(root,'private','credentials.json');
  await atomicWrite(file,{secret:'first'});
  await atomicWrite(file,{secret:'replacement'});
  assert.equal((await stat(file)).mode & 0o777,0o600);
  assert.equal((await stat(join(root,'private'))).mode & 0o777,0o700);
  assert.equal(JSON.parse(await readFile(file,'utf8')).secret,'replacement');
});
test('host ID persists through restart and refuses symlink directories', async () => {
  const root = await mkdtemp(join(tmpdir(),'mwsb-host-'));
  const dir = join(root,'private');
  const first = await hostId(dir);
  assert.match(first,/^urn:uuid:/);
  assert.equal(await hostId(dir),first);
  const link = join(root,'unsafe');
  await symlink(dir,link);
  await assert.rejects(() => hostId(link));
});
