# 📚 Study Planner AI

Upload your class/work schedule, tell the app when you are free, and get:

- a **study timetable** that fits only inside your free time (no clashes with your commitments)
- **improvement tips** for your study planner and habits
- a **downloadable** timetable (CSV) and full plan (JSON)

Built with [Streamlit](https://streamlit.io) and Google's Gemini Flash model.

## Supported schedule files

| Type | Notes |
|------|-------|
| Image (`png`, `jpg`, `jpeg`, `webp`) | Photo or screenshot of a timetable (read by Gemini) |
| `pdf` | Text or scanned timetable (read by Gemini) |
| `csv`, `xlsx` | Spreadsheet timetables |
| `txt`, `md` | Plain text, e.g. "Mon 9-11 Maths lecture" |

Max file size: 10 MB.

## Project files

```
app.py                       Streamlit user interface
planner.py                   Logic: file reading, prompt, Gemini call, validation
test_planner.py              Unit tests (no API key needed)
requirements.txt             Python dependencies
.gitignore                   Keeps secrets out of GitHub
.streamlit/secrets.toml.example   Template for your API key
```

## 1. Get a Gemini API key

1. Go to <https://aistudio.google.com/apikey> and sign in with a Google account.
2. Click **Create API key** and copy it. Keep it private.

## 2. Run locally

```bash
# (optional) create a virtual environment
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install -r requirements.txt

# add your key
mkdir -p .streamlit
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# open .streamlit/secrets.toml and paste your key

streamlit run app.py
```

The app opens at <http://localhost:8501>. You can also skip the secrets file and paste the
key into the sidebar each time.

Run the tests (no API key needed):

```bash
python -m unittest -v
```

## 3. Push to GitHub

1. Create an account at <https://github.com> and click **New repository**.
   Name it e.g. `study-planner-ai`. Do **not** tick "Add a README" (you already have one). Click **Create repository**.
2. Install Git from <https://git-scm.com> if you don't have it.
3. In your project folder run:

```bash
git init
git add .
git status                      # check that .streamlit/secrets.toml is NOT listed
git commit -m "Initial commit: Study Planner AI"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/study-planner-ai.git
git push -u origin main
```

When Git asks for a password, use a **Personal Access Token** (GitHub → Settings → Developer settings →
Personal access tokens), not your account password.

> ⚠️ Never commit your real API key. `.gitignore` already excludes `.streamlit/secrets.toml`.
> If you ever leak a key by accident, delete it in Google AI Studio and create a new one.

## 4. Deploy on Streamlit Community Cloud (free)

1. Go to <https://share.streamlit.io> and sign in with GitHub.
2. Click **Create app** → **Deploy a public app from GitHub**.
3. Choose your repository, branch `main`, and main file path `app.py`.
4. Open **Advanced settings → Secrets** and paste:

   ```toml
   GEMINI_API_KEY = "your-real-key-here"
   ```

5. Click **Deploy**. After a minute or two you get a public link like `https://your-app.streamlit.app`.

To update the app later, just `git add . && git commit -m "message" && git push`. Streamlit redeploys automatically.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "Check that your API key is correct" | Re-copy the key; make sure there are no spaces or quotes inside it |
| "Check the model name" | Model names change. Type a current Flash model name in the sidebar (default `gemini-flash-latest` always points to the newest) |
| `503 UNAVAILABLE` / "high demand" | Google's servers are temporarily overloaded. The app retries automatically (waits 2s, 4s, 8s) and then switches to a backup model. If it still fails, wait a minute and click the button again |
| Rate limit (429) | The free tier has limits. Wait a minute and try again |
| Timetable has clashes | The app shows warnings when the AI's plan breaks your free-time window or overlaps your commitments. Re-run or fix your file |
| Photo not read correctly | Use a clearer, straight-on photo, or upload a CSV/text version |

## Privacy

Your uploaded file and the text you type are sent to Google's Gemini API to create the plan.
Do not upload anything you would not want processed by Google.
