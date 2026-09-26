# PulseCheck — Vercel fixed deployment

This package keeps the existing PulseCheck Flask app and routes. The fix is specifically for the Vercel error:

`jinja2.exceptions.TemplateNotFound: index.html`

The deployment explicitly bundles the Flask `templates/` and `static/` folders with the `app.py` Vercel Function and uses an absolute template/static path.

## Upload/deploy exactly this folder

The project root must directly contain:

- `app.py`
- `vercel.json`
- `requirements.txt`
- `templates/index.html`
- `templates/login.html`
- `templates/dashboard.html`
- `static/style.css`

Do **not** upload the parent folder containing this folder as the Vercel project root, and do not place `templates` beside some other nested project directory.

## Vercel settings

- Framework Preset: Flask (or leave Auto Detect)
- Build Command: empty/default
- Output Directory: empty/default
- Install Command: default

The `vercel.json` `includeFiles` rule is the important part for this deployment: it packages `templates/**` and `static/**` with `app.py`.

## Environment variables

Add the existing values you use, especially:

- `SECRET_KEY`
- `PULSECHECK_AI_API_KEY` or `OPENAI_API_KEY` if the external AI provider is enabled
- `PULSECHECK_AI_MODEL` if your current app expects it

## SQLite

The app already uses `/tmp/pulsecheck.db` when `VERCEL` is set. This allows the demo to write during a serverless instance, but `/tmp` is ephemeral and is not permanent multi-user storage. Move to PostgreSQL later for persistent production data.

## Local run

```powershell
python app.py
```
