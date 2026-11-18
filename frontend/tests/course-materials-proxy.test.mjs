import test from 'node:test';
import assert from 'node:assert/strict';
import {proxyMaterials} from '../netlify/edge-functions/course-materials.js';
test('same-origin proxy preserves cookie security, credentials and CSRF origin', async () => {
  const response = await proxyMaterials(new Request('https://instructors.example.test/api/course-materials/session',{method:'POST',headers:{Origin:'https://instructors.example.test',Cookie:'session=TEST','Content-Type':'application/json','X-Course-Materials-Request':'1'},body:'{}'}),'https://api.example.test',async (url,options) => {
    assert.equal(url.origin,'https://api.example.test'); assert.equal(options.headers.get('Cookie'),'session=TEST'); assert.equal(options.headers.get('Origin'),'https://instructors.example.test'); assert.equal(options.headers.get('X-Course-Materials-Request'),'1'); assert.equal(options.redirect,'manual');
    return new Response('{}',{headers:{'Set-Cookie':'session=TEST; Secure; HttpOnly; SameSite=Lax'}});
  });
  assert.match(response.headers.get('Set-Cookie'),/Secure; HttpOnly; SameSite=Lax/); assert.equal(response.headers.get('Cache-Control'),'private, no-store');
});
test('proxy fails closed for missing/http/credentialed targets and redirects', async () => {
  for (const target of [undefined,'http://api.example.test','https://name:password@api.example.test','https://api.example.test/path']) assert.equal((await proxyMaterials(new Request('https://ui.example.test/api/course-materials/catalog'),target)).status,503);
  assert.equal((await proxyMaterials(new Request('https://ui.example.test/api/course-materials/catalog'),'https://api.example.test',async()=>new Response(null,{status:302,headers:{Location:'https://other.example.test'}}))).status,502);
});
test('proxy forwards authenticated image and streaming download responses', async () => {
  const response=await proxyMaterials(new Request('https://ui.example.test/api/course-materials/materials/test/assets/assets/figure.png',{headers:{Cookie:'session=TEST'}}),'https://api.example.test',async(url,options)=>{assert.equal(options.headers.get('cookie'),'session=TEST');return new Response('TEST image',{headers:{'Content-Type':'image/png'}});});
  assert.equal(response.headers.get('content-type'),'image/png'); assert.equal(await response.text(),'TEST image');
});
test('proxy removes stale encoding/length headers from fetch-decoded bodies',async()=>{
  const response=await proxyMaterials(new Request('https://ui.example.test/api/course-materials/catalog'),'https://api.example.test',async()=>new Response('{"decoded":true}',{headers:{'Content-Encoding':'gzip','Content-Length':'999','Content-Type':'application/json'}}));
  assert.equal(response.headers.get('content-encoding'),null);assert.equal(response.headers.get('content-length'),null);assert.deepEqual(await response.json(),{decoded:true});
});
