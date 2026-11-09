export default {
  fetch(request, env) {
    if (typeof env?.EVENTDESK_PHOTO_PILOT?.fetch !== 'function') return new Response('Unavailable\n', { status: 503, headers: { 'Cache-Control': 'no-store' } });
    return env.EVENTDESK_PHOTO_PILOT.fetch(request);
  },
};
