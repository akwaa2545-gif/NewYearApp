const PUBLIC_ORIGIN = 'https://tokinparty27.pages.dev';
const BACKEND_ORIGIN = 'https://eventdesk.pages.dev';

export default {
  fetch(request, env) {
    const url = new URL(request.url);
    if (url.origin !== PUBLIC_ORIGIN) return new Response('Use the public event address.', {status:421});
    if (url.pathname === '/__deployment' || url.pathname.startsWith('/__deployment/')) {
      return new Response('Not found.', {status:404});
    }
    const origin = request.headers.get('Origin');
    const site = request.headers.get('Sec-Fetch-Site');
    const read = request.method === 'GET' || request.method === 'HEAD';
    if ((origin !== null && origin !== PUBLIC_ORIGIN)
        || (!read && site !== null && site !== 'same-origin' && site !== 'none')) {
      return Response.json({error:'This request came from another website.'}, {status:403});
    }

    // Only a verified public origin is translated across this private service binding.
    url.hostname = new URL(BACKEND_ORIGIN).hostname;
    const headers = new Headers(request.headers);
    headers.delete('Host');
    if (origin !== null) headers.set('Origin', BACKEND_ORIGIN);
    const forwarded = new Request(new Request(url, request), {headers, redirect:'manual'});
    return env.EVENTDESK.fetch(forwarded);
  },
};
