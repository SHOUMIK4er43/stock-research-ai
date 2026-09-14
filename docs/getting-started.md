# Getting Started — Stock Research AI (macOS Pilot)

This guide is for the **pilot** program. It assumes no prior knowledge of Python, Git,
or the command line beyond copy-pasting the commands shown here.

Stock Research AI has two parts that work together:

- a small **local backend service** that runs on your own Mac and does the research
- a **Chrome extension side panel** you use to trigger and read research

Nothing about your research ever leaves your Mac except the one request your backend
sends to Anthropic when you click "Research Company" (see
[`privacy-disclosure.md`](privacy-disclosure.md) for the exact details).

## 1. Obtain the release bundle

You'll receive a folder (or a ZIP of one) that looks like this:

```
stock-research-ai-release/
    backend/
        stock_research_ai-0.1.0-py3-none-any.whl
        stock_research_ai-0.1.0.tar.gz
    extension/
        stock-research-ai-extension-0.1.0.zip
    install-macos.sh
    uninstall-macos.sh
    RELEASE-MANIFEST.json
    SHA256SUMS
    getting-started.md
    privacy-disclosure.md
```

If it arrived as a ZIP, double-click it in Finder to extract the folder, then open a
Terminal window and `cd` into that folder.

## 2. (Optional) Verify the download

In Terminal, from inside the extracted folder:

```sh
shasum -a 256 -c SHA256SUMS
```

Every line should say `OK`. If anything says `FAILED`, do not proceed — ask whoever
gave you the release for a fresh copy.

## 3. Install

Stock Research AI is installed with [`uv`](https://astral.sh/uv), the same tool the
Python community increasingly uses to install command-line Python applications. If you
don't already have `uv`, install it first by following the instructions at
https://astral.sh/uv (the installer will tell you clearly if `uv` is missing).

Then, from inside the release folder:

```sh
./install-macos.sh
```

This will:

- install the `stock-research-ai` command for your user only (no admin password, no
  system-wide changes)
- create your local data folder at `~/.stock-research-ai/`
- set up a background service that starts automatically whenever you log in, so you
  never need to keep a Terminal window open
- wait briefly and confirm the service is running

You'll see a confirmation message once it's healthy. **Running this same command again
later upgrades you in place** — your existing research is never touched (see
[Upgrading](#12-upgrading)).

## 4. Configure your Anthropic credential

Stock Research AI uses Anthropic's API to run the analytical part of each research
run. Store your API key securely in your Mac's own Keychain (never in a plain text
file):

```sh
stock-research-ai configure-analyst
```

You'll be prompted to paste your key — it will not be displayed on screen as you type.

## 5. Confirm everything is set up

```sh
stock-research-ai diagnostics
```

This prints your version, whether the database is reachable, whether a credential is
configured (and where it came from — never the key itself), and the paths Stock
Research AI uses on your machine. Nothing sensitive is ever printed by this command.

## 6. Check the service is healthy

```sh
curl http://127.0.0.1:8000/health
```

You should see `{"status":"ok"}`. This never leaves your machine.

## 7. Install the Chrome extension

The pilot extension isn't on the Chrome Web Store yet (see
[`chrome-web-store-readiness.md`](chrome-web-store-readiness.md)), so it's loaded
manually from the extracted folder:

1. Unzip `extension/stock-research-ai-extension-0.1.0.zip` into its own folder (Chrome
   requires an extracted directory, not a ZIP file directly).
2. Open Chrome and go to `chrome://extensions`.
3. Turn on **Developer mode** (top-right toggle).
4. Click **Load unpacked** and select the folder you just extracted.
5. You should see "Stock Research AI" appear in your extensions list with its icon.

## 8. Generate a pairing code

The extension and your local backend need to be paired once, so only your own
extension can talk to your own backend:

```sh
stock-research-ai pair-code
```

This prints a one-time code, valid for a few minutes.

## 9. Pair the extension

1. Click the Stock Research AI icon in Chrome's toolbar to open the side panel.
2. Paste the code from step 8 and click **Pair**.

## 10. Open a supported company page

Navigate to a company page on Screener, for example:

```
https://www.screener.in/company/INFY/
```

The side panel will show a "Research Company" button once it recognizes the page.

## 11. Research Company

Click **Research Company**. You'll see real progress stages (preparing, evidence,
analysis, challenge, report) — never a fake percentage. This step is the one point
where your backend sends data to Anthropic's API to run the analysis.

## 12. View the report

Once complete, click **View Research** to open the full institutional report inside
the side panel — sections, evidence, the independent challenger's findings, and more.
You can also download the Institutional Report, Investor Brief, or Research JSON from
inside the report view.

## 13. Back up your research

```sh
stock-research-ai backup --output ~/Desktop/stock-research-ai-backup
```

This safely copies your research database and evidence (never your Anthropic key).

## 14. Upgrading

To upgrade later, just download the new release bundle and run `./install-macos.sh`
again from inside it. Your existing research, Keychain credential, and pairing are all
preserved. A safety backup of your database is taken automatically before the upgrade
if one already exists.

## 15. Uninstalling

```sh
./uninstall-macos.sh
```

This removes the application and the background service registration. **Your research
data is kept by default** — the script tells you exactly where it lives. If you want
to permanently delete your local research data too, see the printed instructions for
the explicit `--remove-data` option (this requires a typed confirmation and cannot be
undone).

## Troubleshooting

- **Service not healthy after install** — check `~/.stock-research-ai/logs/`, then run
  `stock-research-ai diagnostics`.
- **Extension shows "not paired"** — run `stock-research-ai pair-code` again and pair
  once more.
- **Port already in use** — set `STOCK_RESEARCH_AI_HTTP_PORT` to a different port
  before installing, or stop whatever else is using port 8000.
- **"uv was not found"** — install `uv` from https://astral.sh/uv first.

## Restart the service manually (rarely needed)

The background service starts automatically at login. If you ever need to restart it
by hand:

```sh
launchctl kickstart -k gui/$(id -u)/ai.stockresearch.local
```
