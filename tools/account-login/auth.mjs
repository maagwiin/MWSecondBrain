import {randomBytes, randomUUID, createHash, createPublicKey, verify, timingSafeEqual} from 'node:crypto';
import {mkdir, lstat, chmod, open, rename, unlink} from 'node:fs/promises';
import {constants} from 'node:fs';
import {dirname, join} from 'node:path';

export const ISSUER = 'https://auth.openai.com';
export const RESOURCE = 'https://api.openai.com/v1';
export const REDIRECT = 'http://127.0.0.1:1455/auth/callback';
const SCOPES = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct';
const opaque = () => randomBytes(32).toString('base64url');
const equal = (a,b) => typeof a === 'string' && typeof b === 'string' && Buffer.byteLength(a) === Buffer.byteLength(b) && timingSafeEqual(Buffer.from(a),Buffer.from(b));

export function attempt(host, saved) {
  const state = opaque(), nonce = opaque(), verifier = opaque();
  const clientId = saved?.client_id ?? 'dynamic_agent_client';
  const url = new URL(`${ISSUER}/api/accounts/authorize`);
  url.search = new URLSearchParams({client_id:clientId,ext_agent_host_id:host,response_type:'code',redirect_uri:REDIRECT,scope:SCOPES,resource:RESOURCE,state,nonce,code_challenge_method:'S256',code_challenge:createHash('sha256').update(verifier).digest('base64url')}).toString();
  if (saved) {
    if (saved.id_token) url.searchParams.set('id_token_hint',saved.id_token);
    if (saved.email) url.searchParams.set('login_hint',saved.email);
  } else url.searchParams.set('agent_name_hint','MWSecondBrain');
  return {url:url.href,state,nonce,verifier,clientId};
}

export function acceptCallback(url, pending) {
  for (const key of ['state','error','code','client_id']) {
    if (url.searchParams.getAll(key).length > 1) throw new Error('Duplicate OAuth parameter');
  }
  if (!equal(url.searchParams.get('state'),pending.state)) throw new Error('OAuth state mismatch');
  if (url.searchParams.has('error')) throw new Error('OAuth denied or failed');
  const code = url.searchParams.get('code');
  const clientId = url.searchParams.get('client_id') ?? pending.clientId;
  if (!code || !clientId || clientId === 'dynamic_agent_client' || !/^oaiapp_[A-Za-z0-9_-]+$/.test(clientId)) throw new Error('OAuth registration incomplete');
  if (pending.clientId && pending.clientId !== 'dynamic_agent_client' && clientId !== pending.clientId) throw new Error('OAuth client mismatch');
  return {code,clientId};
}

export function verifyIdToken(token, jwks, {clientId,nonce,now=Date.now()/1000,subject}) {
  if (typeof token !== 'string' || token.length > 32768) throw new Error('Invalid ID token');
  const parts = token.split('.');
  if (parts.length !== 3 || parts.some(part => !/^[A-Za-z0-9_-]+$/.test(part))) throw new Error('Invalid JWT encoding');
  const header = JSON.parse(Buffer.from(parts[0],'base64url'));
  if (header.alg !== 'RS256' || !header.kid || header.crit) throw new Error('Unsupported JWT header');
  const keys = jwks.keys.filter(key => key.kid === header.kid && key.kty === 'RSA' && (!key.alg || key.alg === 'RS256') && (!key.use || key.use === 'sig'));
  if (keys.length !== 1 || !verify('RSA-SHA256',Buffer.from(`${parts[0]}.${parts[1]}`),createPublicKey({key:keys[0],format:'jwk'}),Buffer.from(parts[2],'base64url'))) throw new Error('ID token signature invalid');
  const claims = JSON.parse(Buffer.from(parts[1],'base64url'));
  const audiences = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (claims.iss !== ISSUER || !audiences.includes(clientId) || (audiences.length > 1 && claims.azp !== clientId) || (claims.azp && claims.azp !== clientId)) throw new Error('ID token issuer or audience invalid');
  if (!Number.isFinite(claims.exp) || claims.exp <= now || (claims.nbf !== undefined && (!Number.isFinite(claims.nbf) || claims.nbf > now))) throw new Error('ID token expired or premature');
  if ((nonce !== undefined && !equal(claims.nonce,nonce)) || typeof claims.sub !== 'string' || !claims.sub || (subject && subject !== claims.sub)) throw new Error('ID token identity or nonce mismatch');
  return claims;
}

export function credentialsFrom(tokens, claims, clientId, host) {
  const scopes = typeof tokens.scope === 'string' ? tokens.scope.split(/\s+/).filter(Boolean) : [];
  if (!scopes.includes('chatgpt.tokens.use.direct') || !scopes.includes('resource.invoke')) throw new Error('ChatGPT plan permission not granted');
  if (tokens.token_type?.toLowerCase() !== 'bearer' || !tokens.access_token || !tokens.refresh_token || !tokens.id_token || !Number.isFinite(tokens.expires_in) || tokens.expires_in <= 0) throw new Error('Incomplete OAuth credentials');
  return {email:claims.email,issuer:claims.iss,subject:claims.sub,client_id:clientId,ext_agent_host_id:host,oidc_nonce:claims.nonce,id_token:tokens.id_token,access_token:tokens.access_token,refresh_token:tokens.refresh_token,token_type:tokens.token_type,expires_in:tokens.expires_in,scopes,saved_at:new Date().toISOString()};
}

async function secureDir(dir) {
  await mkdir(dir,{recursive:true,mode:0o700});
  const info = await lstat(dir);
  if (!info.isDirectory() || info.isSymbolicLink() || info.uid !== process.getuid()) throw new Error('Unsafe credential directory');
  await chmod(dir,0o700);
}

export async function readProtected(file) {
  const handle = await open(file,constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.uid !== process.getuid() || (info.mode & 0o077)) throw new Error('Unsafe credential file');
    return JSON.parse(await handle.readFile('utf8'));
  } finally { await handle.close(); }
}

export async function atomicWrite(file, data) {
  await secureDir(dirname(file));
  const temp = `${file}.${randomUUID()}.tmp`;
  const handle = await open(temp,'wx',0o600);
  try {
    await handle.writeFile(JSON.stringify(data,null,2)+'\n');
    await handle.sync();
  } finally { await handle.close(); }
  try { await rename(temp,file); } catch (error) { await unlink(temp); throw error; }
}

export async function hostId(dir) {
  await secureDir(dir);
  const file = join(dir,'host.json');
  try {
    const saved = await readProtected(file);
    if (typeof saved.ext_agent_host_id !== 'string' || !/^urn:uuid:[0-9a-f-]{36}$/.test(saved.ext_agent_host_id)) throw new Error('Invalid host ID');
    return saved.ext_agent_host_id;
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  const id = `urn:uuid:${randomUUID()}`;
  await atomicWrite(file,{ext_agent_host_id:id});
  return id;
}
