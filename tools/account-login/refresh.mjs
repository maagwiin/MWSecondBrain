import {join,resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {readProtected,atomicWrite,verifyIdToken,ISSUER,RESOURCE} from './auth.mjs';
import {officialJSON} from './onboarding.mjs';

function failure(code) {
  const error=new Error(code==='auth_required'?'OAuth refresh requires reconnect':'OAuth refresh failed');
  error.code=code;
  return error;
}

// The Python caller holds an exclusive OS file lock for this registration.
// Do not invoke this command concurrently outside that caller.
export async function refreshCredentials(dir,{fetcher=fetch}={}) {
  const file=join(dir,'credentials.json');
  const previous=await readProtected(file);
  if(!/^oaiapp_[A-Za-z0-9_-]+$/.test(previous.client_id) || !previous.refresh_token || !previous.subject)throw failure('auth_required');
  let response;
  try {
    response=await fetcher(`${ISSUER}/api/accounts/oauth/token`,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({grant_type:'refresh_token',client_id:previous.client_id,refresh_token:previous.refresh_token,resource:RESOURCE}),redirect:'error',signal:AbortSignal.timeout(15000)});
  } catch {throw failure('runtime_failure');}
  if(!response.ok)throw failure([400,401,403].includes(response.status)?'auth_required':response.status===429?'quota_exceeded':'runtime_failure');
  const tokens=await response.json();
  const scopes=typeof tokens.scope==='string'?tokens.scope.split(/\s+/).filter(Boolean):previous.scopes;
  if(!scopes?.includes('resource.invoke') || !scopes.includes('chatgpt.tokens.use.direct'))throw failure('auth_required');
  if(typeof tokens.access_token!=='string' || !tokens.access_token || tokens.token_type?.toLowerCase()!=='bearer' || !Number.isFinite(tokens.expires_in) || tokens.expires_in<=0)throw failure('auth_required');
  if(tokens.id_token) {
    const jwks=await officialJSON(`${ISSUER}/.well-known/jwks.json`,{},fetcher);
    const unverified=JSON.parse(Buffer.from(tokens.id_token.split('.')[1]??'','base64url'));
    try {verifyIdToken(tokens.id_token,jwks,{clientId:previous.client_id,subject:previous.subject,nonce:unverified.nonce===undefined?undefined:previous.oidc_nonce});} catch {throw failure('auth_required');}
  }
  const record={...previous,access_token:tokens.access_token,refresh_token:tokens.refresh_token??previous.refresh_token,id_token:tokens.id_token??previous.id_token,token_type:tokens.token_type,expires_in:tokens.expires_in,scopes,saved_at:new Date().toISOString()};
  if(typeof record.refresh_token!=='string' || !record.refresh_token)throw failure('auth_required');
  await atomicWrite(file,record);
  return {ok:true};
}

if(process.argv[1] && import.meta.url===pathToFileURL(resolve(process.argv[1])).href) {
  refreshCredentials(process.env.MWSB_AUTH_DIR??'').then(result=>console.log(JSON.stringify(result))).catch(error=>{
    console.log(JSON.stringify({ok:false,error:['auth_required','quota_exceeded'].includes(error.code)?error.code:'runtime_failure'}));
    process.exitCode=1;
  });
}
