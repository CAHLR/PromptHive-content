import test from 'node:test';
import { File } from 'node:buffer';
import assert from 'node:assert/strict';
import { build } from '../../../node_modules/esbuild/lib/main.js';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const output = await build({ entryPoints: [new URL('../../services/courseMaterialsService.js', import.meta.url).pathname], bundle: true, write: false, format: 'esm', define: {'import.meta.env': JSON.stringify({ VITE_COURSE_MATERIALS_API_BASE_URL: 'http://TEST-ONLY.invalid' })} });
const service = await import('data:text/javascript;base64,' + Buffer.from(output.outputFiles[0].text).toString('base64'));
const response = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: {'Content-Type': 'application/json'} });
test('browser upload calls completion once; server owns automatic queueing', async t => {
  const original = globalThis.fetch; t.after(() => {globalThis.fetch = original;}); const calls = [];
  globalThis.fetch = async (url, options) => { calls.push(url); if (url.endsWith('/materials')) return response({material:{id:'test'},upload:{method:'POST',url:'https://TEST-ONLY.invalid/upload',fields:{key:'test'}}}); if (url.endsWith('/upload')) return new Response(null,{status:204}); return response({material:{id:'test',status:'queued'},ingestion:{queued:true},capabilities:{processingEnabled:true}}); };
  const file = new File(['%PDF-1.4\n%%EOF'], 'test.pdf', {type:'application/pdf'});
  assert.equal((await service.uploadMaterial(file, {})).material.status, 'queued'); assert.equal(calls.length, 3); assert.ok(calls[2].endsWith('/complete')); assert.ok(!calls.some(url => url.endsWith('/process')));
});
test('queue failure preserves verified upload result for Process action', async t => {
  const original = globalThis.fetch; t.after(() => {globalThis.fetch = original;});
  globalThis.fetch = async url => url.endsWith('/materials') ? response({material:{id:'test'},upload:{method:'POST',url:'https://TEST-ONLY.invalid/upload',fields:{}}}) : url.endsWith('/upload') ? new Response(null,{status:204}) : response({material:{id:'test',status:'uploaded'},uploadVerified:true,ingestion:{queued:false,error:'Queue offline'}});
  const result = await service.uploadMaterial(new File(['test'], 'test.pdf'), {}); assert.equal(result.uploadVerified,true); assert.equal(result.ingestion.error, 'Queue offline');
});
test('authentication errors retain status for instructor sign-in; publication binds review', async t => {
  const original = globalThis.fetch; t.after(() => {globalThis.fetch = original;});
  globalThis.fetch = async () => response({error:'Sign in'},401); await assert.rejects(service.getCatalog(), error => error.status === 401);
  globalThis.fetch = async (url, options) => { assert.ok(url.endsWith('/publish')); assert.deepEqual(JSON.parse(options.body),{ reviewed:true,compiledHash:'hash',approvalFingerprint:'fingerprint',baseManifestHash:'manifest',baseCoursePlansHash:'plans' }); return response({material:{status:'publish_queued'}}); };
  await service.publishMaterial('test',{compiledHash:'hash',approvalFingerprint:'fingerprint',baseManifestHash:'manifest',baseCoursePlansHash:'plans'});
});
test('readable review renders notes, questions, explanations, provenance without raw giant JSON', async () => {
  const built = await build({entryPoints:[new URL('./MaterialReview.jsx',import.meta.url).pathname],bundle:true,write:false,format:'cjs',platform:'node',packages:'external',define:{'import.meta.env':'{}'}});
  const module={exports:{}}; new Function('require','module','exports',built.outputFiles[0].text)(require,module,module.exports);
  const React=require('react');const {renderToStaticMarkup}=require('react-dom/server');
  const html=renderToStaticMarkup(React.createElement(module.exports.ReadableContent,{page:0,setPage:()=>{},items:[{id:1,path:'notes.text',text:'Readable reference'},{id:2,path:'problems.prompt',text:'How many?'},{id:3,path:'problems.solution.text',text:'Six'},{id:4,path:'notes.source.pages',text:'pages 1, 2'},{id:5,path:'problems.solution.analysis_plan',text:'Multiply three by two.'}]}));
  assert.match(html,/Readable reference/);assert.match(html,/How many/);assert.match(html,/Six/);assert.match(html,/pages 1, 2/);assert.match(html,/Multiply three by two/);
});

test('cancel and recoverable delete use protected API requests', async t => {
  const original=globalThis.fetch; t.after(()=>{globalThis.fetch=original;}); const calls=[];
  globalThis.fetch=async(url,options)=>{calls.push({url,...options});return response({material:{id:'test'},deleted:true});};
  await service.cancelMaterial('test');await service.deleteMaterial('test');
  assert.deepEqual(calls.map(c=>c.method),['POST','DELETE']);
  assert.ok(calls[0].url.endsWith('/cancel')&&calls[1].url.endsWith('/materials/test'));
  assert.ok(calls.every(c=>c.credentials==='include'&&c.headers['X-Course-Materials-Request']==='1'));
});
test('upload abort signal reaches the transfer and prevents completion', async t=>{
  const original=globalThis.fetch;t.after(()=>{globalThis.fetch=original;});const controller=new AbortController();let completed=false;
  globalThis.fetch=async(url,options)=>{
    if(url.endsWith('/materials'))return response({material:{id:'test'},upload:{method:'POST',url:'https://TEST-ONLY.invalid/upload',fields:{}}});
    if(url.endsWith('/upload')){assert.equal(options.signal,controller.signal);controller.abort();throw new DOMException('Aborted','AbortError');}
    completed=true;return response({});
  };
  await assert.rejects(service.uploadMaterial(new File(['test'],'test.pdf'),{},()=>{},{signal:controller.signal}),e=>e.name==='AbortError'&&e.materialId==='test');assert.equal(completed,false);
});
