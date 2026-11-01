# Isolated hosting-network comparison

This branch prepares a separate copy of the existing application on Render. It
does not establish that Render can recover Google's errors. No Render service
has been created and production's domain stays on Vercel.

The Blueprint selects a Free Python service in Virginia, deploys this branch,
and disables subsequent automatic deployments. It uses the same pinned scraper,
city identifiers, filters, shared-session behavior, 1.5-second pacing and normal
stop rules. The existing UI is available on the test service, so its full date
grid, diagnostics and copy controls can be exercised without changing production.
The Chromium diagnostic function is not exposed by this Python server.

## Create the isolated service

[Deploy this test branch to Render](https://render.com/deploy?repo=https%3A%2F%2Fgithub.com%2Fvictorramirezd%2Fflight-search-web%2Ftree%2Fcodex%2Frender-network-test)

In an existing Render account, create a new Blueprint from this repository,
select branch `codex/render-network-test`, and review `render.yaml`. The service
must show the **Free** plan. Render will create a separate `onrender.com` URL.
Do not attach the production domain or change the site's frontend endpoint.

An account owner needs to connect the repository and create the service; no
Render account or API credential is available in the current workspace. If a
new account is needed, the owner must complete account creation themselves.

Render's Free service sleeps after 15 idle minutes and can take about a minute
to wake. Wake it with a normal page visit before timing flight searches. Its
limited compute and usage allowances make this a bounded diagnostic service,
not a production scaling design. See [Render's Free documentation](https://render.com/docs/free).

## Verification protocol

Verify the configured Virginia region in the Render dashboard and the revision
against this branch's commit. `render-virginia` in API diagnostics is a configured
label; `RENDER_GIT_COMMIT` is supplied by the hosting platform.

Run MIL → Istanbul (All airports), round trip, target 12 December 2026 ±4 days,
stays 3–5 days, one adult, economy, EUR, 0–2 layovers, 20 offers, transfers included.
This contains 27 pairs. Leave `fetch_path=root` and `http_transport=primp`.
Continue only unattempted combinations after ordinary generic-error stops.
Stop on blocking, consent, parsing or transport failures. Preserve pending cells
and record all per-pair events, without cookies, credentials or raw HTML.

Compare priced/attempted against Vercel's same grid. Repeat a successful complete
grid, then test transfers excluded. A single good pass is insufficient: London
returned 26/27 once, then stopped at 6/11 on its next pass.

Before any production routing change, review repeated outcomes and deployment
limits. Keep this service isolated if reliability does not improve.

Configuration reference: [Render Blueprint specification](https://render.com/docs/blueprint-spec).
