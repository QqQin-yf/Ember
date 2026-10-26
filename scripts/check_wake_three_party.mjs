// Run with App's tsx loader. All databases, tokens and service ports are temporary.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createServer} from 'node:http';
import {createInterface} from 'node:readline';
import {randomBytes} from 'node:crypto';
import {mkdtempSync,mkdirSync,writeFileSync,rmSync,existsSync,readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,resolve,dirname} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';

const appRoot=process.env.WAKE_APP_ROOT, heartRoot=process.env.WAKE_HEART_ROOT;
assert(appRoot&&heartRoot,'Set WAKE_APP_ROOT and WAKE_HEART_ROOT explicitly');
const repo=resolve(dirname(fileURLToPath(import.meta.url)),'..'),serverDir=join(appRoot,'server');
const python=process.env.WAKE_PYTHON||'python';
const runtime=mkdtempSync(join(tmpdir(),'ember-three-party-'));
const tokens=Object.fromEntries(['main','source','wake','heart','ember'].map(k=>[k,randomBytes(32).toString('hex')]));
const cleanEnv=Object.fromEntries(Object.entries(process.env).filter(([k])=>!/^(DELIZ_|EMBER_|HEART_|LLM_|TAVILY_|WEB_PROXY|DB_PATH|HOST|PORT|WAKE_)/.test(k)));
process.env.DB_PATH=join(runtime,'app.db');
const {db,ensureConversation,updateConversation,insertMessage,setLeaf}=await import(pathToFileURL(join(serverDir,'src/db.ts')).href);
db.exec('PRAGMA busy_timeout=5000');
const {windowOf}=await import(pathToFileURL(join(serverDir,'src/revision.ts')).href);
const home=ensureConversation('isolated-three-party');updateConversation(home.id,{ember:true,instance:'default'});
const user=insertMessage({conversationId:home.id,parentId:null,role:'user',content:[{type:'text',text:'Synthetic integration history.'}]});setLeaf(home.id,user.id);
let app,ember,heart,log='',mode='silent',sourceDown=false,modelCalls=0,sourceFailures=0,checks=0;
const children=[],results=[],modelBodies=[];
const managed=(exe,args,env=cleanEnv)=>{const p=spawn(exe,args,{cwd:runtime,env,stdio:['pipe','pipe','pipe']});children.push(p);p.stderr.on('data',b=>log+=b);p.on('error',e=>log+=String(e));return p};
const until=async(fn,label='condition')=>{for(let i=0;i<1800;i++){for(const p of children)if(p.exitCode!==null&&!p.killed)throw Error('Isolated service exited: '+p.exitCode);if(await fn())return;await delay(50)}throw Error('Timeout: '+label)};
const check=(value,label)=>{assert.ok(value,label);checks++;results.push(label);console.log('PASS '+label)};
const port=async()=>{const s=createServer();await new Promise(r=>s.listen(0,'127.0.0.1',r));const p=s.address().port;await new Promise(r=>s.close(r));return p};
const stop=async(p)=>{if(!p||p.exitCode!==null||p.signalCode!==null)return;const ended=new Promise(r=>p.once('exit',r));p.kill();await ended};
const appPort=await port(),emberPort=await port();
const appUrl=`http://127.0.0.1:${appPort}`,emberUrl=`http://127.0.0.1:${emberPort}`;
const http=async(base,path,token,body,method=body===undefined?'GET':'POST')=>{const r=await fetch(base+path,{method,headers:{authorization:'Bearer '+token,'X-Heart-Instance':'default',...(body!==undefined?{'content-type':'application/json'}:{})},...(body!==undefined?{body:JSON.stringify(body)}:{}),signal:AbortSignal.timeout(5000)});return {http:r.status,body:await r.json()}};
const gateway=createServer(async(req,res)=>{
 try {
  let raw='';for await(const chunk of req)raw+=chunk;
  if(req.url==='/v1/messages'){
   modelCalls++;modelBodies.push(JSON.parse(raw));res.setHeader('content-type','text/event-stream');res.writeHead(200);
   res.write('event: message_start\ndata: '+JSON.stringify({type:'message_start',message:{id:'msg-test',type:'message',role:'assistant',model:'test-model',content:[],stop_reason:null,stop_sequence:null,usage:{input_tokens:10,output_tokens:0}}})+'\n\n');
   if(mode==='held')return;
   const request=JSON.parse(raw),toolRound=mode==='tool'&&!request.messages.some(m=>Array.isArray(m.content)&&m.content.some(c=>c.type==='tool_result'));
   if(toolRound){
    for(const ev of [{type:'content_block_start',index:0,content_block:{type:'tool_use',id:'isolated-read-1',name:'read_memory_source',input:{}}},{type:'content_block_delta',index:0,delta:{type:'input_json_delta',partial_json:JSON.stringify({event_id:'missing-isolated-event'})}},{type:'content_block_stop',index:0},{type:'message_delta',delta:{stop_reason:'tool_use',stop_sequence:null},usage:{output_tokens:10}},{type:'message_stop'}])res.write(`event: ${ev.type}\ndata: ${JSON.stringify(ev)}\n\n`);
    res.end();return;
   }
   const text=mode==='visible'?'An isolated waking reply.\n<<<HEART>>> null':'<<<HEART>>> null';
   for(const ev of [{type:'content_block_start',index:0,content_block:{type:'text',text:''}},{type:'content_block_delta',index:0,delta:{type:'text_delta',text}},{type:'content_block_stop',index:0},{type:'message_delta',delta:{stop_reason:'end_turn',stop_sequence:null},usage:{output_tokens:10}},{type:'message_stop'}])res.write(`event: ${ev.type}\ndata: ${JSON.stringify(ev)}\n\n`);
   res.end();return;
  }
  if(sourceDown){sourceFailures++;res.writeHead(503,{'content-type':'application/json'});res.end(JSON.stringify({code:'isolated_source_outage'}));return}
  const r=await fetch(appUrl+req.url,{method:req.method,headers:{authorization:req.headers.authorization,'X-Heart-Instance':req.headers['x-heart-instance'],'content-type':'application/json'},...(raw?{body:raw}:{}),signal:AbortSignal.timeout(5000)});
  res.writeHead(r.status,{'content-type':'application/json'});res.end(await r.text());
 }catch(e){res.writeHead(502,{'content-type':'application/json'});res.end(JSON.stringify({code:'fixture_transport_error'}))}
});
await new Promise(r=>gateway.listen(0,'127.0.0.1',r));const gatewayUrl=`http://127.0.0.1:${gateway.address().port}`;
let heartUrl,heartPending=[];
const control=body=>new Promise((resolve,reject)=>{heartPending.push({resolve,reject});heart.stdin.write(JSON.stringify(body)+'\n')});
const startApp=()=>{app=managed(process.execPath,['--import',pathToFileURL(join(serverDir,'node_modules/tsx/dist/esm/index.mjs')).href,join(serverDir,'src/index.ts')],{...cleanEnv,DB_PATH:process.env.DB_PATH,HOST:'127.0.0.1',PORT:String(appPort),DELIZ_TOKEN:tokens.main,DELIZ_SOURCE_TOKEN:tokens.source,DELIZ_WAKE_TOKEN:tokens.wake,HEART_BASE_URL:heartUrl,HEART_TOKEN:tokens.heart,HEART_PHASE2:'1',EMBER_BASE_URL:emberUrl,EMBER_TOKEN:tokens.ember,EMBER_RECALL_REFS:'1',EMBER_INSTANCES:'default',WAKE_MIN_GAP_MIN:'0',WAKE_MAX_PER_DAY:'1000',LLM_BASE_URL:gatewayUrl,LLM_API_KEY:'isolated-model',LLM_MODEL:'test-model',LLM_THINKING_BUDGET:'0'});app.stdout.on('data',b=>log+=b)};
const readyApp=()=>until(async()=>{try{return(await fetch(appUrl+'/health')).ok}catch{return false}},'App readiness');
const readyEmber=()=>until(async()=>{try{return(await http(emberUrl,'/ember/runs/absent',tokens.ember)).http===404}catch{return false}},'Ember readiness');
const startEmber=()=>{ember=managed(python,['-B','-X','utf8',join(repo,'scripts/run_phase2_integration.py'),'--runtime-dir',join(runtime,'ember'),'--port',String(emberPort),'--instances','default','--source-env-file',join(runtime,'source.env')]);ember.stdout.on('data',b=>log+=b)};
const count=()=>Number(db.prepare('SELECT count(*) n FROM messages').get().n);
let fixture=0;
async function candidate(){
 const c=db.prepare('SELECT * FROM conversations WHERE id=?').get(home.id),w=windowOf(c);
 const r=await control({op:'candidate',window:{event_id:'fixture-window-'+(++fixture),session_id:c.id,activity_revision:c.activity_revision,window_revision:c.window_revision,leaf_id:c.leaf_id,context_start_id:w.contextStartId,messages:w.messages}});
 assert(!r.error,JSON.stringify(r));return r.candidate;
}
const submit=p=>http(appUrl,'/api/wake',tokens.wake,p);
async function done(p){let value;await until(async()=>{value=await http(appUrl,'/api/wake/'+p.wake_id,tokens.wake);return ['done','cancelled','error'].includes(value.body.status)},'App terminal');return value.body}
const runStatus=id=>http(emberUrl,'/ember/runs/'+id,tokens.ember);
async function settled(id){await until(async()=>db.prepare('SELECT state FROM wake_ember_outbox WHERE run_id=?').get(id)?.state==='sent','Ember finish delivered');await until(async()=>!db.prepare("SELECT 1 FROM heart_outbox WHERE state!='sent' AND event_id=?").get('wake-end-'+id),'Heart finish delivered')}
try{
 mkdirSync(join(runtime,'heart'));mkdirSync(join(runtime,'ember'));
 writeFileSync(join(runtime,'heart.json'),JSON.stringify({heart_root:heartRoot,runtime:join(runtime,'heart'),token:tokens.heart}));
 writeFileSync(join(runtime,'ember','token.txt'),tokens.ember);
 writeFileSync(join(runtime,'source.env'),`EMBER_SOURCE_URL=${gatewayUrl}\nEMBER_SOURCE_TOKEN=${tokens.source}\nEMBER_SOURCE_TIMEOUT=3\n`);
 heart=managed(python,['-B','-u','-X','utf8',join(repo,'scripts/wake_heart_fixture.py'),join(runtime,'heart.json')]);
 createInterface({input:heart.stdout}).on('line',line=>{try{const data=JSON.parse(line);if(data.port){heartUrl=`http://127.0.0.1:${data.port}`}else{const p=heartPending.shift();p?.resolve(data)}}catch{log+=line}});
 await until(async()=>!!heartUrl,'Heart readiness');startEmber();await readyEmber();startApp();await readyApp();
 const before=count(),quiet=await candidate(),q=await submit(quiet);check(q.http===202,'real Heart candidate accepted by real App');const qdone=await done(quiet);await settled(qdone.run_id);
 check(qdone.status==='done'&&!qdone.spoke&&count()===before,'quiet completion creates no chat message');
 check((await runStatus(qdone.run_id)).body.status==='done','real Ember quiet terminal persisted');
 const replay=await submit(quiet);check(replay.body.duplicate&&replay.body.run_id===qdone.run_id,'candidate retry preserves run identity');
 check(modelBodies[0].tools.some(t=>t.name==='read_memory_source')&&!modelBodies[0].tools.some(t=>t.name==='search_memory'),'real MCP whitelist reaches model');
 mode='visible';const visible=await candidate();await submit(visible);const v=await done(visible);await settled(v.run_id);
 check(v.spoke&&count()===before+1,'visible completion commits exactly one assistant');
 check((await runStatus(v.run_id)).body.status==='done','real source API validates visible Ember finish');
 const ev=await control({op:'evidence'});check(ev.events.some(e=>e.kind==='wake_end'&&e.run_id===v.run_id&&e.effect==='applied_live'),'real Heart applies wake end live');
 sourceDown=true;const outage=await candidate();await submit(outage);const o=await done(outage);await until(async()=>(await runStatus(o.run_id)).body.status==='finishing','source outage finishing');
 const fixed=db.prepare('SELECT body FROM wake_ember_outbox WHERE run_id=?').get(o.run_id).body,calls=modelCalls;
 check(sourceFailures>0&&db.prepare('SELECT state FROM wake_ember_outbox WHERE run_id=?').get(o.run_id).state==='pending','503 retains fixed finish in pending outbox');
 await stop(app);await stop(ember);startEmber();await readyEmber();startApp();await readyApp();
 check((await runStatus(o.run_id)).body.status==='finishing','Ember restart retains frozen finish intent');
 sourceDown=false;
 // Wait for App's actual production retry timer; do not edit its outbox.
 await settled(o.run_id);
 check(db.prepare('SELECT body FROM wake_ember_outbox WHERE run_id=?').get(o.run_id).body===fixed&&modelCalls===calls,'restart retries identical finish without rerunning model');
 check((await runStatus(o.run_id)).body.status==='done','source restoration completes original run');
 mode='tool';const tool=await candidate();await submit(tool);const t=await done(tool);await settled(t.run_id);const toolLedger=(await runStatus(t.run_id)).body;
 check(t.status==='done'&&toolLedger.tool_calls.some(c=>c.tool_call_id==='isolated-read-1'&&c.tool_name==='read_memory_source'),'actual App tool call is bound to real Ember run');
 check(db.prepare('SELECT count(*) n FROM wake_tools WHERE run_id=?').get(t.run_id).n===1,'tool outcome is durably recorded once in App');
 mode='held';const interrupted=await candidate(),beforeCalls=modelCalls;await submit(interrupted);await until(async()=>modelCalls>beforeCalls,'held model starts');await stop(app);startApp();await readyApp();const recovered=await done(interrupted);await settled(recovered.run_id);
 check(recovered.status==='error'&&modelCalls===beforeCalls+1,'App crash reconciles real ledgers without rerunning model');
 check((await runStatus(recovered.run_id)).body.status==='error','interrupted execution closes real Ember run');
 const cancel=await candidate(),beforeCancel=count(),cancelCalls=modelCalls;await submit(cancel);await until(async()=>modelCalls>cancelCalls,'cancellable model starts');
 const activity=await http(appUrl,`/api/conversations/${home.id}/context-start`,tokens.main,{messageId:null},'PUT');check(activity.http===200,'real window activity accepted during wake');
 const cancelled=await done(cancel);await settled(cancelled.run_id);
 check(cancelled.status==='cancelled'&&count()===beforeCancel,'new window activity cancels wake without publishing partial text');
 check((await runStatus(cancelled.run_id)).body.status==='cancelled','cancellation reaches real Ember terminal ledger');
 const eventsFile=join(runtime,'ember','data','ember','events.jsonl');
 check(!existsSync(eventsFile)||!readFileSync(eventsFile,'utf8').trim(),'wake scenarios create no ordinary memory Event');
 console.log(JSON.stringify({checks,results,environment:'isolated-local',model:'streaming-fixture',daily_delivery_enabled:false},null,2));
 if(process.env.WAKE_REPORT)writeFileSync(process.env.WAKE_REPORT,JSON.stringify({checks,results,environment:'isolated-local',model:'streaming-fixture',daily_delivery_enabled:false},null,2));
}catch(error){console.error(log.slice(-10000));throw error}
finally{for(const p of children.reverse())await stop(p);gateway.closeAllConnections();await new Promise(r=>gateway.close(r));db.close();rmSync(runtime,{recursive:true,force:true})}
