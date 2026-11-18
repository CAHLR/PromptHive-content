# PromptHive — Deployment Runbook

Step-by-step guide to deploy the difficulty-filter build (Flask backend + Vite/React
frontend). Fill in every `<PLACEHOLDER>` once your lab admin gives you the server info.

> Security: never commit `.env`, `service_account_file.json`, `token.json`, or
> `credentials.json`. They are gitignored. Rotate any key that has ever been pasted
> into chat/email/logs.

---

## 0. What you need before starting

| Placeholder            | Meaning                                              | Who provides |
|------------------------|------------------------------------------------------|--------------|
| `<SERVER_HOST>`        | SSH host / public IP of the backend box              | **Lab admin** |
| `<SSH_USER>`           | SSH username                                          | **Lab admin** |
| `<DOMAIN>`             | Public domain pointing at the server (for SSL)       | **Lab admin** |
| `<CERT_PATH>` / `<KEY_PATH>` | TLS cert + key (e.g. Let's Encrypt paths)      | **Lab admin** |
| `OPENAI_API_KEY`       | OpenAI key                                           | You |
| `service_account_file.json` | Google service-account key (rotated)           | You |

---

## 1. Backend — server setup (Linux / AWS EC2)

SSH in:

```bash
ssh <SSH_USER>@<SERVER_HOST>
```

### 1a. System packages (Ubuntu/Debian example)

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git r-base
```

### 1b. Install R + the `mirt` package (required for Rasch/IRT calibration)

The difficulty calibration shells out to `Rscript` running `run_rasch.R`, which needs
the `mirt` package.

```bash
sudo Rscript -e 'install.packages("mirt", repos="https://cloud.r-project.org")'
# verify:
Rscript -e 'library(mirt); cat("mirt OK\n")'
which Rscript   # must be on PATH for the Flask process
```

### 1c. Get the code

```bash
git clone https://github.com/CAHLR/PromptHive.git
cd PromptHive
git checkout vedant_difficulty_filter
```

### 1d. Python environment + dependencies

Using a venv + pip (simplest for a server):

```bash
cd abscribe_backend
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip

# Option A (recommended): install via poetry
pip install poetry
poetry install

# Option B: if not using poetry, install the runtime deps directly
# pip install flask==2.2.5 werkzeug==2.2.2 flask-mongoengine flask-cors \
#   flask-security-too==5.1.2 python-dotenv openai pytz google-api-python-client \
#   google-auth-httplib2 google-auth-oauthlib pytexit pydantic pandas sympy numpy tqdm resend

# Production WSGI server (NOT in pyproject — install explicitly):
pip install gunicorn gevent
```

### 1e. Place credentials

```bash
# Service account JSON (from Google Cloud, project oatutor-askoski) — default path:
#   PromptHive/abscribe_backend/abscribe_backend/services/service_account_file.json
# Copy it up from your laptop, e.g.:
scp ./service_account_file.json <SSH_USER>@<SERVER_HOST>:~/PromptHive/abscribe_backend/abscribe_backend/services/service_account_file.json
```

Make sure the Google sheets you want to read are **shared with the service-account
email** (`client_email` in the JSON) as at least Viewer.

### 1f. Create the `.env`

```bash
cd ~/PromptHive/abscribe_backend
cp .env.example .env
nano .env    # set OPENAI_API_KEY, GOOGLE_SERVICE_ACCOUNT_FILE (absolute path), etc.
```

### 1g. Smoke test

```bash
cd ~/PromptHive/abscribe_backend
source .venv/bin/activate
# Dev run (bind to localhost to test):
python -m abscribe_backend.app     # or: poetry run flask run
```

### 1h. Run in production with gunicorn + SSL

From `abscribe_backend/abscribe_backend` (so `abscribe_backend.app:app` resolves).
Use `tmux`/`screen`/systemd for persistence.

```bash
cd ~/PromptHive/abscribe_backend/abscribe_backend
gunicorn -k gevent -w 9 -b 0.0.0.0:8081 \
  --certfile=<CERT_PATH>/fullchain.pem \
  --keyfile=<KEY_PATH>/privkey.pem \
  abscribe_backend.app:app
```

(Without a domain/cert yet, you can temporarily drop the `--certfile/--keyfile`
lines and serve plain HTTP on a port for internal testing only.)

Open the firewall / AWS security group for the chosen port (8081) and 443/80 as needed.

---

## 2. Frontend — build & host (Netlify)

The frontend reads the backend URL from `VITE_REACT_APP_API_BASE_URL`, falling back
to the value in `frontend/src/config.jsx` (currently the `BACKEND_URL_HERE` placeholder).

```bash
cd frontend
npm install

# Point at the deployed backend, either:
#   (a) set an env var at build time (preferred), or
#   (b) edit frontend/src/config.jsx and replace BACKEND_URL_HERE
export VITE_REACT_APP_API_BASE_URL="https://<DOMAIN>:8081"

npm run build
```

- Build output goes to **`frontend/build/`** (this project overrides Vite's default
  `dist` via `outDir: 'build'` in `vite.config.js`).
- On Netlify: set **Base directory** = `frontend`, **Build command** = `npm run build`,
  **Publish directory** = `frontend/build`, and add **environment variable**
  `VITE_REACT_APP_API_BASE_URL = https://<DOMAIN>:8081`.

> CORS: the Flask backend uses `flask-cors`. If the browser blocks requests from the
> Netlify origin, restrict/allow it in the backend `CORS(...)` config.

---

## 3. The ONE thing still blocked

Everything above is prepped **except** the live server. Before you can actually
deploy the backend you need from your **lab admin**:

- **SSH access + host details** for the deployment box: `<SERVER_HOST>`, `<SSH_USER>`,
  the **domain** `<DOMAIN>` that points to it, and the **SSL cert/key paths**
  (`<CERT_PATH>` / `<KEY_PATH>`), plus confirmation the firewall/security group
  allows the backend port (8081) and 80/443.

Once you have those, fill the placeholders and run sections 1 and 2.

## Course Materials authentication deployment

Course Materials uses a scoped same-origin API proxy so authenticated cookies work
with the documented Netlify frontend/separate Flask host. With Netlify base
`frontend`, `frontend/netlify/edge-functions/course-materials.js` handles
`/api/course-materials/*`. Set `COURSE_MATERIALS_BACKEND_ORIGIN` in Netlify's
Functions/runtime environment to the actual **HTTPS origin only**, and leave
`VITE_COURSE_MATERIALS_API_BASE_URL` unset. Existing authoring API variables are
unchanged. Use Node 22 (`frontend/.nvmrc`) for build and the durable worker.

The Flask host needs the strong Course Materials session secret, provisioned
course-scoped accounts, exact frontend allowed Origin, host-only Secure/HttpOnly/
SameSite=Lax cookies, and the shared durable API/worker storage described in
`COURSE_MATERIALS.md`. Do not disable cookie security or CSRF protection to make a
cross-site direct URL work. Backend hostname/certificate/host details above remain
required; public-hosting verification cannot be completed until that environment
is supplied and deployment is approved. No deployment was performed by this change.
