// The Pages address is independent of the account-wide workers.dev name.
export default {
  fetch(request, env) {
    return env.EVENTDESK.fetch(request);
  },
};
