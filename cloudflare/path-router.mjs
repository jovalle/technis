import TECHNIS_ICON_SVG from "../assets/impossible-t.svg";
import TECHNIS_LOGO_SVG from "../assets/logo.svg";
import TECHNIS_WORDMARK_SVG from "../assets/technis.svg";

const SECONDARY_DOMAIN = "techn.is";

const place = (svg, x, y, w, h) =>
  svg.replace(
    / width="\d+" height="\d+"/,
    ` x="${x}" y="${y}" width="${w}" height="${h}"`
  );
// Icon over wordmark, same proportions as the Tracearr login header (88px icon, 20px gap, 240x41 wordmark).
const TECHNIS_STACKED_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="240" height="149" viewBox="0 0 240 149" role="img" aria-label="Technis">
${place(TECHNIS_ICON_SVG, 76, 0, 88, 88)}
${place(TECHNIS_WORDMARK_SVG, 0, 108, 240, 41)}
</svg>`;

// Served by this Worker on every routed host, including techn.is/logo* and techn.is/favicon*.
const BRAND_ASSETS = {
  "/favicon.svg": TECHNIS_ICON_SVG,
  "/logo.svg": TECHNIS_LOGO_SVG,
  "/logo_full.svg": TECHNIS_LOGO_SVG,
  "/logo_stacked.svg": TECHNIS_STACKED_SVG,
  "/logo_wordmark.svg": TECHNIS_WORDMARK_SVG,
};

function brandingInjection(title) {
  // Tracearr login: the header block holding the 80px shield, the "Tracearr" h1, and the subtitle.
  const login = "div.min-h-screen:has(> div.mb-8.items-center > svg.h-20.w-20)";
  const loginHeader = `${login} > div.mb-8.items-center`;

  return `
  <link data-managed-brand rel="icon" type="image/svg+xml" href="/favicon.svg">
  <style id="technis-ui-customizations">
    /* 1. Login: replace the Tracearr shield with the Impossible T and the title with the wordmark */
    ${loginHeader} > svg,
    ${loginHeader} > h1,
    ${loginHeader} > p {
      display: none !important;
    }
    ${loginHeader} {
      gap: 20px !important;
      margin-bottom: 24px !important;
    }
    ${loginHeader}::before,
    ${loginHeader}::after {
      content: "" !important;
      display: block !important;
      background-repeat: no-repeat !important;
      background-position: center !important;
      background-size: contain !important;
    }
    ${loginHeader}::before {
      width: 88px !important;
      height: 88px !important;
      background-image: url("/favicon.svg") !important;
    }
    ${loginHeader}::after {
      width: 240px !important;
      height: 41px !important;
      background-image: url("/logo_wordmark.svg") !important;
    }

    /* 2. Login: remove everything below the Plex button and the Tracearr footer line */
    ${login} div.relative:has(span.bg-card),
    ${login} p.text-muted-foreground.text-center.text-sm,
    ${login} > p.text-muted-foreground.text-xs {
      display: none !important;
    }

    /* 3. Header / Sidebar logo: show the full Technis logo */
    div.flex.items-center:has(> svg[viewBox="0 0 64 64"]) > svg,
    div.flex.items-center:has(> svg[viewBox="0 0 64 64"]) > span.font-bold {
      display: none !important;
    }
    div.flex.items-center:has(> svg[viewBox="0 0 64 64"])::before {
      content: "" !important;
      display: inline-block !important;
      width: 150px !important;
      height: 38px !important;
      background-image: url("/logo.svg") !important;
      background-repeat: no-repeat !important;
      background-position: left center !important;
      background-size: contain !important;
    }
  </style>
  <script>
    (() => {
      const APP_TITLE = ${JSON.stringify(title)};
      try {
        let currentTitle = document.title || APP_TITLE;
        Object.defineProperty(document, "title", {
          get() {
            return currentTitle;
          },
          set(val) {
            if (typeof val === "string") {
              currentTitle = val.replace(/Tracearr|Seerr/g, APP_TITLE);
            } else {
              currentTitle = val;
            }
            const t = document.querySelector("title");
            if (t) t.textContent = currentTitle;
          },
          configurable: true,
        });
        document.title = APP_TITLE;
      } catch {
        if (document.title !== APP_TITLE) document.title = APP_TITLE;
      }
    })();
  <\/script>
`;
}

function brandApplication(response, title) {
  const contentType = response.headers.get("content-type") || "";
  if (response.status >= 400 || !contentType.includes("text/html"))
    return response;

  return new HTMLRewriter()
    .on("title", {
      element(element) {
        element.setInnerContent(title);
      },
    })
    .on('link[rel*="icon"]', {
      element(element) {
        element.remove();
      },
    })
    .on("head", {
      element(element) {
        element.append(brandingInjection(title), { html: true });
      },
    })
    .transform(response);
}

function handleCors(response, request, url) {
  const reqOrigin = request.headers.get("Origin") || `https://${url.hostname}`;
  const res = new Response(response.body, response);
  res.headers.set("Access-Control-Allow-Origin", reqOrigin);
  res.headers.set("Access-Control-Allow-Credentials", "true");
  res.headers.set(
    "Access-Control-Allow-Methods",
    "GET, HEAD, POST, PUT, DELETE, PATCH, OPTIONS"
  );
  const reqAllowHeaders = request.headers.get("Access-Control-Request-Headers");
  if (reqAllowHeaders) {
    res.headers.set("Access-Control-Allow-Headers", reqAllowHeaders);
  } else if (!res.headers.has("Access-Control-Allow-Headers")) {
    res.headers.set(
      "Access-Control-Allow-Headers",
      "Content-Type, Authorization, X-Requested-With, sentry-trace, baggage"
    );
  }
  const currentVary = res.headers.get("Vary");
  if (!currentVary) {
    res.headers.set("Vary", "Origin");
  } else if (!currentVary.includes("Origin")) {
    res.headers.set("Vary", `${currentVary}, Origin`);
  }
  return res;
}

function handleOptions(request, url) {
  const reqOrigin = request.headers.get("Origin") || `https://${url.hostname}`;
  return new Response(null, {
    status: 204,
    headers: {
      "Access-Control-Allow-Origin": reqOrigin,
      "Access-Control-Allow-Credentials": "true",
      "Access-Control-Allow-Methods":
        "GET, HEAD, POST, PUT, DELETE, PATCH, OPTIONS",
      "Access-Control-Allow-Headers":
        request.headers.get("Access-Control-Request-Headers") ||
        "Content-Type, Authorization, X-Requested-With, sentry-trace, baggage",
      "Access-Control-Max-Age": "86400",
      Vary: "Origin, Access-Control-Request-Headers",
    },
  });
}

async function formatPlexInitiateResponse(response, request, url) {
  let text = await response.text();
  text = text.replaceAll(
    "context%5Bdevice%5D%5Bproduct%5D=Tracearr",
    "context%5Bdevice%5D%5Bproduct%5D=Technis"
  );
  text = text.replaceAll(
    "context[device][product]=Tracearr",
    "context[device][product]=Technis"
  );
  const transformed = new Response(text, response);
  return handleCors(transformed, request, url);
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Immich moved from photos.ovall.es; 308 keeps API methods and bodies intact.
    if (url.hostname === "photos.ovall.es") {
      url.hostname = `photos.${SECONDARY_DOMAIN}`;
      return Response.redirect(url.toString(), 308);
    }

    // Public Immich allows OAuth only; LAN password login goes to immich.techn.is, which skips Cloudflare.
    if (
      url.hostname === `photos.${SECONDARY_DOMAIN}` &&
      url.pathname === "/api/auth/login"
    ) {
      if (request.method !== "POST") return fetch(request);
      return Response.json(
        { message: "Password login is available only on the local network" },
        { status: 403 }
      );
    }

    // 0. Intercept Logo & Favicon asset requests across all routed domains
    const assetName = url.pathname.slice(url.pathname.lastIndexOf("/"));
    const iconAlias = [
      "/favicon.ico",
      "/favicon-96x96.png",
      "/apple-touch-icon.png",
    ].includes(assetName);
    const brandAsset = iconAlias ? TECHNIS_ICON_SVG : BRAND_ASSETS[assetName];
    if (brandAsset) {
      return new Response(brandAsset, {
        headers: {
          "Content-Type": "image/svg+xml",
          "Cache-Control": "public, max-age=3600",
        },
      });
    }

    // 1. Host alias redirects for Seerr subdomains
    if (
      url.hostname === `req.${SECONDARY_DOMAIN}` ||
      url.hostname === `requests.${SECONDARY_DOMAIN}`
    ) {
      url.hostname = `request.${SECONDARY_DOMAIN}`;
      return Response.redirect(url.toString(), 308);
    }

    // 2. Dash subdomain: dash.techn.is
    if (url.hostname === `dash.${SECONDARY_DOMAIN}`) {
      if (url.pathname === "" || url.pathname === "/") {
        url.pathname = "/dashboard/";
        return Response.redirect(url.toString(), 302);
      }
      if (url.pathname === "/dashboard") {
        url.pathname = "/dashboard/";
        return Response.redirect(url.toString(), 308);
      }

      if (request.method === "OPTIONS") {
        return handleOptions(request, url);
      }

      const response = await fetch(request);

      let corsResponse;
      if (url.pathname.includes("/auth/plex/initiate")) {
        corsResponse = await formatPlexInitiateResponse(response, request, url);
      } else {
        corsResponse = handleCors(response, request, url);
      }

      return brandApplication(corsResponse, "Technis");
    }

    // 3. Request subdomain: request.techn.is
    if (url.hostname === `request.${SECONDARY_DOMAIN}`) {
      if (request.method === "OPTIONS") {
        return handleOptions(request, url);
      }
      const response = await fetch(request);
      const corsResponse = handleCors(response, request, url);
      return brandApplication(corsResponse, "Technis");
    }

    // 4. Apex domain: techn.is
    if (url.hostname === SECONDARY_DOMAIN) {
      // Path-based redirects to Seerr (like skulltrail.com)
      if (
        url.pathname === "/req" ||
        url.pathname.startsWith("/req/") ||
        url.pathname === "/requests" ||
        url.pathname.startsWith("/requests/") ||
        url.pathname === "/request" ||
        url.pathname.startsWith("/request/")
      ) {
        let subpath = "";
        if (url.pathname.startsWith("/requests")) {
          subpath = url.pathname.slice("/requests".length);
        } else if (url.pathname.startsWith("/request")) {
          subpath = url.pathname.slice("/request".length);
        } else if (url.pathname.startsWith("/req")) {
          subpath = url.pathname.slice("/req".length);
        }
        const target = new URL(
          `https://request.${SECONDARY_DOMAIN}${subpath}${url.search}`
        );
        return Response.redirect(target.toString(), 308);
      }

      // /dashboard trailing slash redirect
      if (url.pathname === "/dashboard") {
        url.pathname = "/dashboard/";
        return Response.redirect(url.toString(), 308);
      }

      // /dashboard/* routing to Tracearr (fetches from dash.techn.is origin)
      if (url.pathname.startsWith("/dashboard/")) {
        if (request.method === "OPTIONS") {
          return handleOptions(request, url);
        }

        const targetUrl = new URL(request.url);
        targetUrl.hostname = `dash.${SECONDARY_DOMAIN}`;

        const reqHeaders = new Headers(request.headers);
        reqHeaders.set("Host", `dash.${SECONDARY_DOMAIN}`);
        reqHeaders.set("X-Forwarded-Host", url.hostname);
        reqHeaders.set("X-Forwarded-Proto", "https");

        const response = await fetch(targetUrl.toString(), {
          method: request.method,
          headers: reqHeaders,
          body: ["GET", "HEAD"].includes(request.method)
            ? undefined
            : request.body,
          redirect: "manual",
        });

        let routedResponse;
        if (url.pathname.includes("/auth/plex/initiate")) {
          routedResponse = await formatPlexInitiateResponse(
            response,
            request,
            url
          );
        } else {
          routedResponse = handleCors(response, request, url);
        }

        // Rewrite redirects that point to dash.techn.is back to techn.is
        const location = routedResponse.headers.get("location");
        if (location) {
          try {
            const locUrl = new URL(
              location,
              `https://dash.${SECONDARY_DOMAIN}`
            );
            if (locUrl.hostname === `dash.${SECONDARY_DOMAIN}`) {
              locUrl.hostname = SECONDARY_DOMAIN;
              routedResponse.headers.set("location", locUrl.toString());
            }
          } catch {
            // Keep original location if unparseable
          }
        }

        return brandApplication(routedResponse, "Technis");
      }
    }

    // Default passthrough
    return fetch(request);
  },
};
