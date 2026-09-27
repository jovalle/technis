# Technis Cloudflare Edge Path Router

Edge router Worker for `techn.is` deployed via Wrangler to account `Technis`.

## Functionality

1. **Subdomain Routing & Aliases**:
   - `dash.techn.is` -> routes to `tracearr` (`http://tracearr:3000`) via Nexus Tunnel (`origin.techn.is`). Auto-redirects `/` to `/dashboard/`.
   - `request.techn.is` -> routes to `seerr` (`http://seerr:5055`) via Nexus Tunnel.
   - `req.techn.is` & `requests.techn.is` -> 308 redirect to `request.techn.is`.

2. **Apex Domain Path Routing (`techn.is`)**:
   - `techn.is/dashboard*` -> routes to `tracearr` via Nexus Tunnel.
   - `techn.is/req*`, `techn.is/request*`, `techn.is/requests*` -> 308 redirect to `https://request.techn.is`.
   - `techn.is/` (apex fallback) -> 301 to `https://www.techn.is`, served by Pages project `technis-web-www` (`www-techn-is.pages.dev`) in the Technis account.

3. **Deployment**:

   ```bash
   just cf deploy
   ```
