# Indian Mutual Fund Analytics API

FastAPI service, web UI and resumable loader for Indian mutual-fund scheme metadata and daily NAV history. It stores each scheme's complete history returned by the configured source, then exposes point returns, rolling returns and risk metrics. PostgreSQL is the recommended database for a full import; SQLite is useful for local development and small samples.

## Data source and coverage

The default source is [MFapi.in](https://www.mfapi.in/), a public JSON API that mirrors Indian mutual-fund NAV data. Its documented response shape is a scheme list at `/mf` and a scheme object containing `meta` and `data` at `/mf/{schemeCode}`. This is a third-party mirror, not an AMFI-operated API; source availability, scheme coverage and corrections are outside this service's control. The importer validates response shapes and does not mark malformed responses as loaded. It retries failed schemes on later runs.

[AMFI's NAV download page](https://www.amfiindia.com/net-asset-value/nav-download) is the primary industry source and offers official NAV files/history. Its web history form limits a download to 90 days at a time. The current importer uses MFapi.in's all-history endpoint rather than scraping that form. Verify data freshness and coverage with `/health`, `/schemes`, `/schemes/{code}/data-quality`, and source comparisons before operational use.

The importer fetches the source's scheme list and each returned scheme history. A successful import means the response was fetched and parsed; it does not claim that a third-party source has no omissions. Closed or merged schemes may be absent from the current scheme list, and this can create survivorship bias in category analytics.

## Run locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt        # runtime deps + pytest
python -m app.ingest load --limit 25 --with-metrics   # small live sample
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/` for the web UI and `http://127.0.0.1:8000/docs` for interactive API documentation. The default database is `sqlite:///mf.db`.

## Web UI

The API process also serves a small single-page UI from `app/static/` (plain HTML/CSS/JS, no build step). It is same-origin with the API, so it needs no CORS setup and ships inside the same container.

- **Header search**: press `/` anywhere, type a name, pick with the arrow keys and Enter.
- **Theme**: dark by default, with a light theme from the header button (remembered per browser).

- **Screener**: filter by name, category, plan, option, status and metric thresholds; sort by any column; tick schemes to compare.
- **Scheme page**: KPIs, NAV chart (1Y–max), point returns, risk by period with an adjustable risk-free rate, rolling-return chart and summary, SIP calculator with XIRR, and data-quality details.
- **Compare**: up to 10 schemes, growth-of-₹100 chart from a common start, side-by-side returns/risk with the best value in bold, category/IDCW warnings and a shareable link.
- **Categories**: category averages, then an index chart, rolling returns and the spread across schemes.

The compare basket is kept in the browser's local storage. `/`, `/static/*` and `/favicon.ico` are exempt from the API rate limit; static files are served with `Cache-Control: no-cache` plus an ETag, so a new deployment is picked up on the next page load.

### Design system

- **Direction** (from the UI/UX Pro Max design-system search for a fintech analytics dashboard): dark slate/navy base, green for gains and the primary action, red for losses, Fira Sans for text and Fira Code for figures, dense spacing, 6–8px corners. Text contrast is at least 4.5:1 in both themes, and touch targets are 44px on phones.
- **Motion** (taste-skill rules): the page heading and KPI row enter once per view, buttons step up on hover and press on click, links draw their underline, and charts draw in on first render. Nothing animates on scroll, and everything rests under `prefers-reduced-motion`. No violet is used anywhere.
- **Components ported from React Bits to plain JS**: CountUp (KPI values count up to their final figure, which screen readers get directly), SpotlightCard (pointer light and border glow on KPI tiles) and a sliding segmented control.
- **Lottie**: `app/static/vendor/lottie_light.min.js` is lottie-web 5.13.0's SVG-only light build. It has no expressions engine, so no `eval`. MIT licence in `app/static/vendor/LICENSE-lottie-web.txt`. The two animations in `app/static/anim/` (the busy indicator and the empty-state chart) are small hand-written JSON files.
- **Fonts** load from Google Fonts with `display=swap` and system fallbacks, which is the UI's only third-party request. To avoid it, self-host the two families and drop the `fonts.googleapis.com` links in `index.html`.
- **Licences**: React Bits is MIT plus the Commons Clause. Using adapted components inside this product is fine, but the components themselves must not be resold.

### Full import with PostgreSQL

```bash
docker compose up -d db api
docker compose run --rm loader            # loads history, then refreshes metrics
```

The database persists in the `pgdata` volume. Import is resumable: schemes loaded in the last 20 hours are skipped, while failed schemes are eligible on the next run. Tune `--concurrency`, `--rps`, and `--max-age-hours` for the source and environment. `--limit` limits the number of schemes fetched in that run; it is intended for smoke runs, not complete ingestion. `--with-metrics` refreshes screening metrics after the load, and `--strict` makes the command exit with status 1 if any scheme failed (useful in cron/CI). Failed scheme codes are logged at the end of each run.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///mf.db` | SQLAlchemy URL, e.g. `postgresql+psycopg://user:pass@host/db` |
| `RATE_LIMIT_PER_MIN` | `300` | Per-client API requests per minute; `0` disables |
| `TRUST_FORWARDED_FOR` | off | Use `X-Forwarded-For` as the client IP. Enable only behind a proxy you control |
| `CORS_ORIGINS` | empty | Comma-separated browser origins allowed to call the API (GET only) |
| `MFAPI_BASE_URL` | `https://api.mfapi.in` | Source base URL (for a mirror or a local stub) |

## API

All list endpoints are paginated (`page`, default 1; `page_size`, default 50, maximum 500). Interactive schemas and query constraints are available at `/docs`.

| Endpoint | Purpose |
|---|---|
| `GET /schemes` | Filter and sort schemes, including cached metrics |
| `GET /schemes/{code}` | Scheme metadata and cached metrics |
| `GET /schemes/{code}/nav` | Raw NAV history, date filters and pagination |
| `GET /schemes/{code}/returns` | 1w, 1m, 3m, 6m, 1y, 3y, 5y, 10y, YTD and since inception |
| `GET /schemes/{code}/rolling-returns` | Rolling-return summary and paginated date series |
| `GET /schemes/{code}/risk` | Volatility, maximum drawdown and Sharpe ratio |
| `GET /schemes/{code}/analytics` | Point returns and risk/rolling summaries together |
| `GET /schemes/{code}/sip` | Monthly SIP simulation: invested, current value, absolute return and XIRR |
| `GET /compare?codes=a,b,...` | Side-by-side returns and 3y risk for 2–10 schemes on a common as-of date |
| `GET /schemes/{code}/data-quality` | Coverage estimate, large date gaps and rejected input rows |
| `GET /categories` | Category counts and equal-weighted averages of cached scheme metrics |
| `GET /categories/analytics` | Metrics for an equal-weighted category NAV index and constituent dispersion |
| `GET /categories/rolling-returns` | Category index rolling-return series |
| `GET /schemes/{code}/chart` | Downsampled NAV and rolling-return series for charts (`range`, `window`, `max_points`) |
| `GET /categories/chart` | Downsampled category-index and rolling-return series for charts |
| `GET /health` | Database reachability (503 when down), latest NAV date and metrics freshness |

`/schemes` supports `q`, `category`, `fund_house`, `scheme_type`, `plan` (`direct`/`regular`), `option` (`growth`/`idcw`/`bonus`), `active`, `min_ret_1y`, `min_ret_3y`, `max_vol_3y`, `min_sharpe_3y`, `sort_by`, and `order`. Sort fields are allow-listed. Category names are case-insensitive exact matches except for `/categories?name=` substring search. Scheme responses include `isin`, `plan`, `option` and `metrics_as_of`. Plan and option are derived from the scheme name ("Direct" → direct, otherwise regular; "IDCW"/"Dividend" → idcw, "Growth" → growth, "Bonus" → bonus), so treat them as best-effort labels. Date ranges where `from_date` is after `to_date` are rejected with 422, and `rf` is limited to 0–50 on every endpoint.

## Metric conventions

- Returns, volatility and drawdown are percentages. Periods longer than one year are annualised CAGR; shorter periods are absolute returns. A value may be `null` when history is too short or the start date is too far from the requested date.
- The start NAV is the last published NAV on or before the target date, accepted only when it is within seven calendar days. No value is interpolated or extrapolated.
- Annualised volatility is sample standard deviation of simple NAV returns multiplied by `sqrt(252)`. Returns spanning more than seven calendar days are excluded.
- Sharpe is annualised from daily excess returns, using a configurable annual risk-free rate (`rf`, default 6.5%) and 252 trading days. `rf` is expressed in percent. Zero-variance or insufficient-observation cases produce `null`/unavailable.
- Maximum drawdown is the largest observed peak-to-trough NAV decline in the selected period; the response includes peak, trough and recovery dates when available.
- Rolling returns use each NAV date as the end date and the last valid NAV on or before the window start. Windows longer than one year are annualised.
- Category analytics build an equal-weighted, daily-rebalanced index from currently active schemes, using up to `max_schemes` (default 100, maximum 500), chosen by longest stored history. It is not AUM-weighted and has survivorship bias. `/categories` averages scheme metrics directly and should not be confused with index returns.
- Published NAV is used as-is. IDCW plans therefore show NAV price returns and do not include distributions/reinvestment. For performance comparisons, use Growth plans and compare like-for-like plan types.

- SIP simulation invests a fixed amount each month on `day` (1–28), at the first NAV on or after that date within seven days; months without such a NAV are skipped and counted. Holdings are valued at the last NAV on or before `end`. XIRR is the annualised money-weighted return on those dated cash flows. Stamp duty, exit loads and taxes are ignored.
- `/compare` evaluates every scheme as of the same date (default: the earliest of their latest NAV dates) and warns when schemes are from different categories or mix growth and IDCW options.

These are transparent implementation conventions, not a claim that there is one universally mandated return/risk calculation. Compare methodology when matching figures from another provider.

## Storage and ingestion notes

- `navs` uses `(scheme_code, date)` as its primary key, supporting ordered per-scheme range scans and idempotent upserts. PostgreSQL is configured for bulk-friendly chunks of 5,000 rows; writers are serialized for SQLite safety.
- `scheme_metrics` stores screening metrics so filtering, sorting and pagination do not scan the full NAV database. Recompute them after loading with `python -m app.ingest metrics`.
- Ingestion validates positive finite NAV values, dates and response shapes; duplicate dates are counted as rejected and the last source value wins. Isolated extreme spikes are filtered only when adjacent values strongly agree. Rejected row counts are exposed in data-quality output.
- The loader spaces requests globally within one process, retries network errors, malformed JSON, HTTP 429 and 5xx with exponential backoff and jitter, honours numeric and HTTP-date `Retry-After`, and isolates failures by scheme. `--rps` must be positive; `--concurrency` must be at least one.
- NAV rows are not forward-filled. Gaps remain visible and affect metric availability. The coverage percentage is a weekday estimate and does not account for Indian market holidays.
- API rate limiting is per process and per client IP (`RATE_LIMIT_PER_MIN`, default 300; set to `0` to disable). Idle clients are forgotten after a minute so memory stays bounded. Multiple API workers need a shared gateway or Redis limiter. `X-Forwarded-For` is ignored unless `TRUST_FORWARDED_FOR` is set.
- The metrics job skips and logs a scheme whose series cannot be computed instead of aborting the batch.
- The Docker image runs as a non-root user and has a `/health` container health check.

## Deployment

The image is self-contained: API + UI on one port, running as a non-root user with a `/health` container health check.

1. **Database.** Use PostgreSQL in production (`DATABASE_URL=postgresql+psycopg://...`). Tables are created on startup. SQLite works for a single small instance but not for several replicas.
2. **Run the API.** `docker build -t mf-api .` then run with `DATABASE_URL` set. The container listens on `$PORT` (default 8000), so platforms that inject `PORT` (Render, Railway, Fly.io, Cloud Run) work as-is. Point the platform's health check at `/health`; it returns 503 when the database is unreachable.
3. **Load data.** Run the same image as a one-off/scheduled job: `python -m app.ingest load --with-metrics --strict`. Schedule it daily after AMFI publishes NAVs (late evening IST). Re-runs are incremental and retry earlier failures.
4. **Behind a proxy/load balancer.** Set `TRUST_FORWARDED_FOR=true` so rate limiting uses the real client IP. Leave it off when the app is directly exposed.
5. **Scaling.** The in-process rate limiter and category cache are per process. With several replicas, enforce rate limits at the gateway (or set `RATE_LIMIT_PER_MIN=0` and rely on the gateway).

A full first load is about 38,000 schemes and tens of millions of NAV rows; at the default `--rps 5` expect 2–3 hours. Run it once before opening the service to users. After that, `python -m app.ingest refresh --with-metrics` keeps the data current cheaply: new schemes get their full history, active schemes fetch only NAVs since their last date (minus a 7-day overlap for corrections), and closed schemes are skipped. Schemes whose source rows are all invalid (some dead funds are published with NAV 0) are stored without history instead of being retried forever.

### Free hosting: Koyeb + GitHub Actions + a private Hugging Face dataset

Everything runs on free tiers:

| Piece | Free tier | Role |
|---|---|---|
| Koyeb free instance | 512 MB RAM, 0.1 vCPU, 2 GB disk, sleeps after 1 h idle | Runs the Docker image (API + UI) |
| Hugging Face dataset (private) | free storage | Holds the database snapshot `mf.db.gz` |
| GitHub Actions (public repo) | free minutes | Daily refresh from MFapi.in, then publishes a new snapshot |

**What is hosted.** The full history (about 38,000 schemes, roughly 55 million NAV rows, about 1.8 GB) does not fit a 2 GB free instance. `python -m app.ingest export --years 5` builds the hosted database:
- **All schemes:** every scheme's metadata (name, house, category, ISIN, inception date) and cached metrics.
- **Active schemes:** NAV rows for schemes with a NAV in the last 30 days, from 5 years (+15 days) before the latest NAV. That covers every 1W–5Y return, 1Y/3Y/5Y risk, rolling returns and SIP up to 5 years.
- **Trimmed history:** where history is trimmed, responses carry `history_from` and `since_inception` is `null` rather than guessed.
- **Closed schemes:** they stay searchable but have no NAV history (a 404 that says so).

**How it stays current.** `.github/workflows/refresh-data.yml` runs daily at 07:00 IST and does four things:
1. Restores the snapshot.
2. Runs `refresh --with-metrics`: new schemes in full, recent NAVs for active ones.
3. Trims with `export`.
4. Uploads a new snapshot, squashing dataset history so storage stays flat.

The Koyeb container runs `app.serve`, which restores the snapshot on boot and polls the dataset revision (`SNAPSHOT_CHECK_HOURS`). When a newer snapshot appears, it exits so Koyeb restarts it onto the new data. A sleeping instance also loads the newest snapshot when it wakes.

**Setup**
1. Hugging Face: create the private dataset (`kushal11111/mf-analytics-data`), then a fine-grained token with read/write access to that dataset only.
2. GitHub (repo Settings → Secrets and variables → Actions): add the secret `HF_TOKEN` and the variable `SNAPSHOT_REPO=kushal11111/mf-analytics-data`. Run the workflow once with **Run workflow**.
3. Koyeb: create a Web Service from this GitHub repo, with these settings:
   - **Builder:** Dockerfile. **Instance:** Free. **Region:** Frankfurt or Washington. **Port:** 8000 (HTTP).
   - **Environment:** `SNAPSHOT_REPO=kushal11111/mf-analytics-data`, `SNAPSHOT_CHECK_HOURS=3`, `TRUST_FORWARDED_FOR=true`, `RATE_LIMIT_PER_MIN=300`, plus a Koyeb secret `HF_TOKEN`. A read-only token is enough here.
   - **Health check:** HTTP `/health` with a 300 s grace period, so the first boot can restore the snapshot.

After the instance has been asleep, the first visit takes about 1–3 minutes while the snapshot downloads and decompresses on 0.1 vCPU.

## Tests and offline demo

```bash
python -m pytest
python -m app.ingest demo --n 30
python -m app.ingest metrics
```

Install dev dependencies with `pip install -r requirements-dev.txt`. Tests use generated series and `httpx.MockTransport`; they do not depend on the live data source. Demo data is synthetic and must not be used as real market data.

## Known limitations

- This is a NAV analytics service, not an investment recommendation or a total-return database.
- A third-party source may omit, revise or delay records. Compare source coverage and freshness for your use case.
- Category returns depend on current scheme classifications and constituents. Closed schemes are not guaranteed to appear in today's scheme list.
- Metric screening values are batch-refreshed and can be stale until the metrics job completes.
- Multi-worker API rate limiting is not coordinated. Large Postgres installations may benefit from partitioning the NAV table after measuring their query and ingest workload.
