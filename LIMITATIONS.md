# Limitations and integration requirements

Timestamp parsing, chunking and real MP3 assembly were checked with synthetic tones. This does not verify live voice generation.

The local requirements include Google Cloud Text-to-Speech. Configure `GOOGLE_APPLICATION_CREDENTIALS` for your own account with Text-to-Speech enabled, keeping the service-account file outside Git. FFmpeg handles audio conversion.

Keep web services bound to `127.0.0.1`. Hosting this application for multiple users requires authentication and separate storage and resource limits.
