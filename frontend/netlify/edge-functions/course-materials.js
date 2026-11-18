// Only this feature is proxied. Existing authoring API routing stays unchanged.
export async function proxyMaterials(request, upstreamOrigin, fetcher = fetch) {
  const url = new URL(request.url);
  if (!url.pathname.startsWith('/api/course-materials/')) return new Response('Not found', { status: 404 });
  let upstream;
  try {
    upstream = new URL(upstreamOrigin);
    if (upstream.protocol !== 'https:' || upstream.username || upstream.password || upstream.pathname !== '/' || upstream.search || upstream.hash || upstream.origin === url.origin) throw new Error();
  } catch { return Response.json({ error: 'Course Materials HTTPS proxy target is not configured.' }, { status: 503 }); }
  if (!['GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'OPTIONS'].includes(request.method)) return new Response('Method not allowed', {status:405});
  const headers = new Headers();
  for (const name of ['content-type', 'cookie', 'origin', 'x-course-materials-request', 'if-none-match']) {
    if (request.headers.has(name)) headers.set(name, request.headers.get(name));
  }
  // No redirect following: credentials must never escape the configured origin.
  try {
    const response = await fetcher(new URL(url.pathname + url.search, upstream.origin), {
      method: request.method, headers, redirect: 'manual',
      body: ['GET', 'HEAD'].includes(request.method) ? undefined : request.body,
      ...(request.body ? { duplex: 'half' } : {}),
    });
    if (response.status >= 300 && response.status < 400 && response.status !== 304) return Response.json({error:'Unexpected API redirect.'},{status:502});
    const result = new Response(response.body, response);
    // Flask's host-only cookie is intentionally preserved. The browser stores it
    // on the frontend origin; Secure/HttpOnly/SameSite remain intact.
    result.headers.set('Cache-Control', 'private, no-store');
    result.headers.delete('content-encoding');
    result.headers.delete('content-length');
    result.headers.delete('access-control-allow-origin');
    result.headers.delete('access-control-allow-credentials');
    return result;
  } catch { return Response.json({error:'Course Materials upstream is disconnected.'},{status:502}); }
}
export default request => proxyMaterials(request, Netlify.env.get('COURSE_MATERIALS_BACKEND_ORIGIN'));
export const config = { path: '/api/course-materials/*' };
