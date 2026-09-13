# Text to Audio

Text to Audio creates narration from plain text or timestamped scripts. Choose a voice and language, split long passages into manageable requests, and assemble continuous or timed speech into an MP3 file.

## Features

- Read plain text, SRT-style timing and inline timestamps.
- Choose voices and languages from a local interface.
- Split long input into service-sized chunks.
- Build timed MP3 narration or synthesize continuous speech, using PCM audio internally for assembly.

## How it works

Text normalization and timing parsers feed the speech synthesis routines. Audio assembly produces downloadable output, while provider credentials remain server-side.

**Stack:** Python · FastAPI · Google Cloud TTS · FFmpeg

## Getting started

Use Python 3.12 and a separate virtual environment. Run the following commands from this repository's root in Windows PowerShell.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-local.txt
```

The local requirements include Google Cloud Text-to-Speech. Configure `GOOGLE_APPLICATION_CREDENTIALS` for your own account with Text-to-Speech enabled, keeping the service-account file outside Git. FFmpeg handles audio conversion.

If you need provider credentials, copy `.env.example` to `.env` and configure only the services you use. Keep `.env` local.

### Start the application

Open http://127.0.0.1:8767. Configure Google Application Credentials for an account with Text-to-Speech enabled. FFmpeg is used for audio conversion.

```powershell
python app.py
```

## Example workflow

Compare plain-text narration with a two-part timed script containing a pause.

## Testing and limitations

Timestamp parsing, chunking and real MP3 assembly were checked with synthetic tones. This does not verify live voice generation.

See [Verification](VERIFICATION.md) for the recorded checks and [Limitations](LIMITATIONS.md) for integration requirements.

## Configuration and security

Keep web services bound to `127.0.0.1`. Hosting this application for multiple users requires authentication and separate storage and resource limits. Configure your own provider credentials when a feature requires them; credentials and personal data are not included. See [Security](SECURITY.md) for local configuration and reporting guidance.
