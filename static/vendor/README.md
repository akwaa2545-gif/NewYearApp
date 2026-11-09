# Vendored browser dependency

`html5-qrcode.min.js` is version 2.3.8 of `html5-qrcode`, bundled locally for reliable camera scanning when the event network has no internet access.

- Upstream: <https://github.com/mebjas/html5-qrcode>
- Package: <https://www.npmjs.com/package/html5-qrcode/v/2.3.8>
- License: Apache-2.0; see `html5-qrcode.LICENSE`

When updating the file, update the version above, retain the matching license, run `node --check static/app.js`, and rehearse camera scanning over the production HTTPS origin on supported mobile browsers.

`jsQR.js` is the unmodified browser distribution of `jsqr` version 1.4.0, downloaded from the npm registry with its SHA-512 package integrity verified. Its Apache-2.0 license is retained as `jsQR.LICENSE`. The camera detail worker uses this QR-only decoder on bounded native-resolution crops; frames are processed locally without uploads. It is loaded only when the live camera's detail worker starts.

- Upstream: <https://github.com/cozmo/jsQR>
- Package: <https://www.npmjs.com/package/jsqr/v/1.4.0>
- Distribution SHA-256: `bc40c8a15196236b2314db0856f72ca0b49980cd5413b8c852a7349f5fee0859`

Run `node --test tests/test_detail_worker.js tests/test_detail_scanner.js` to
verify real QR decoding, bounded low-light contrast enhancement, and scanner
lifecycle behavior. The detail worker imports only the local jsQR asset.
