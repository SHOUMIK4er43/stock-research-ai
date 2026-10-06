# Offline Screener fixtures

Public anonymous captures are identified and hashed in manifest.json. Sanitization removes scripts,
styles, forms, inputs and event handlers by byte slicing; table values, date attributes and hrefs
are retained. The sanitized fixture bytes are the exact evidence ingested by replay tests.
No cookies, authentication or document bodies are captured. Tests never refresh these files.

expected_yesbank.json is a hand-specified selection independently inspected from the HTML table
cells and links. It must not be regenerated from normalizer output. Historical expectations do
not apply to live pages. peers_fragment.html is a synthetic structural fixture, not a live capture.
