# PulseCheck — Vercel deployment

This package keeps the existing Flask application and frontend structure.

## Structure

- `app.py` — existing Flask backend
- `templates/index.html` — landing page
- `templates/login.html` — login/register
- `templates/dashboard.html` — team + mentor dashboard
- `static/style.css` — shared landing-page styling
- `requirements.txt` — Python dependencies

## Local run

```powershell
python app.py
```

## Vercel

1. Put this folder in a GitHub repository.
2. Import the repository into Vercel.
3. Vercel detects Flask/Python automatically.
4. No custom build command is required.
5. Add environment variables:
   - `SECRET_KEY` = a long random value
   - `PULSECHECK_AI_API_KEY` = your AI provider key, if using the external AI copilot
   - `PULSECHECK_AI_MODEL` = your configured model name, if required
6. Deploy.

## Important SQLite note

The app keeps SQLite for compatibility with the existing project. On Vercel, the database is placed in `/tmp` so the demo can run without trying to write into the deployed source filesystem.

`/tmp` is ephemeral. Data created on one serverless instance is not guaranteed to survive a new deployment/cold instance and is not suitable for reliable multi-user production persistence.

For a permanent public deployment, the next upgrade should move the database to PostgreSQL (Neon/Supabase/etc.) while leaving the UI and API workflow unchanged.
