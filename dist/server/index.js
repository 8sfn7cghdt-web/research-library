const DEFAULT_HEADERS = {
  "x-content-type-options": "nosniff",
  "referrer-policy": "strict-origin-when-cross-origin",
};

function withHeaders(response) {
  const headers = new Headers(response.headers);
  for (const [key, value] of Object.entries(DEFAULT_HEADERS)) {
    if (!headers.has(key)) headers.set(key, value);
  }
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function assetRequest(requestUrl, pathname) {
  const url = new URL(requestUrl);
  url.pathname = pathname;
  return new Request(url.toString());
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    let pathname = url.pathname;

    if (pathname.endsWith("/")) pathname += "index.html";
    if (pathname === "") pathname = "/index.html";

    let response = await env.ASSETS.fetch(assetRequest(request.url, pathname));

    if (response.status === 404 && !pathname.includes(".")) {
      const clean = pathname.replace(/\/+$/, "");
      response = await env.ASSETS.fetch(assetRequest(request.url, `${clean}.html`));
    }

    if (response.status === 404) {
      response = await env.ASSETS.fetch(assetRequest(request.url, "/404.html"));
    }

    if (response.status === 404) {
      response = await env.ASSETS.fetch(assetRequest(request.url, "/index.html"));
    }

    return withHeaders(response);
  },
};
