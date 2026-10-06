# Chrome Web Store Readiness Checklist

**Current status: NOT SUBMITTED.** The pilot distribution model is "Load unpacked"
from an extracted release ZIP (see [`getting-started.md`](getting-started.md), step
7). This document is a checklist for later, once the pilot has validated the product —
it is not itself a submission and nothing here has been sent to Google.

## Already satisfied (Part 6C-B)

- [x] Manifest V3
- [x] Icons at 16/32/48/128px (`extension/icons/`) — original, restrained mark, no
      third-party branding
- [x] Minimum permissions (`storage`, `sidePanel` only — no `tabs`, `scripting`,
      `downloads`, `content_scripts`, `<all_urls>`)
- [x] Narrow `host_permissions` (`https://www.screener.in/*`,
      `http://127.0.0.1/*` only)
- [x] No remote code (no external `<script>` sources, no `eval`)
- [x] Deterministic, reproducible release packaging (`scripts/package-extension.sh`)
- [x] Privacy disclosure document (`docs/privacy-disclosure.md`)

## Still needed before submission

- [ ] **Public privacy policy URL** — Chrome Web Store requires a hosted, publicly
      reachable URL (not just a repo file) for any extension requesting host
      permissions. `docs/privacy-disclosure.md`'s content should be published there
      first.
- [ ] **Store listing text** — a short and long description written for an
      unfamiliar reviewer/user, distinct from this engineering documentation.
- [ ] **Screenshots** — at least one, ideally 2-3, showing the side panel report view
      in context on a real (or clearly-marked sample) Screener page.
- [ ] **Promotional images** (optional but recommended by Chrome Web Store) — a small
      and/or marquee promo tile.
- [ ] **Permission justification text** — Chrome Web Store's developer console asks
      for a plain-language justification per requested permission
      (`storage`, `sidePanel`, and the two `host_permissions`). Straightforward given
      how narrow they already are.
- [ ] **Data-use disclosure** — the Chrome Web Store developer console's own
      structured privacy questionnaire (what data is collected/used) must be filled
      in consistently with `docs/privacy-disclosure.md`.
- [ ] **Support contact** — a real, monitored contact (email or form) for the listing.
- [ ] **Test/review account or instructions**, if the reviewer needs a way to exercise
      "Research Company" without their own Anthropic key — likely a short recorded
      demo or reviewer notes, since the backend requires a local install the Web
      Store's automated review cannot perform.
- [ ] **Version/update strategy for the Web Store** — once published, Chrome
      auto-updates the extension for users; this only needs a monotonically increasing
      `manifest.json` version per release (already Chrome-enforced — see
      `docs/part6c-b-proof.md`'s versioning policy notes).
- [ ] **Developer account** (one-time Chrome Web Store developer registration and fee,
      if not already done) — an account/business decision, not an engineering task.

## Explicitly deferred, not evaluated here

- Signed/notarized native macOS installer — a separate, larger distribution decision
  for after the pilot (see `docs/part6c-b-proof.md`, "Signing/notarization status").
- Public marketing site.

## Do not submit until

1. The pilot cohort has used the extension successfully end-to-end at least once with
   a real Anthropic key and real Screener research (Part 6C-C).
2. The privacy policy is hosted at a public URL.
3. Screenshots and listing text exist and have been reviewed by someone outside the
   engineering team.
