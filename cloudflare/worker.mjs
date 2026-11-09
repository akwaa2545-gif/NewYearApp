// Bind EVENTDESK to the VPC Service for the EventDesk host and port.
export default {
  async fetch(request, env) {
    const incoming = new URL(request.url);
    const target = new URL('http://127.0.0.1:18080');
    target.pathname = incoming.pathname;
    target.search = incoming.search;
    return env.EVENTDESK.fetch(new Request(target, request));
  },
};
