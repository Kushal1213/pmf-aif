# Indian Mutual Fund Analytics API

FastAPI service and resumable loader for Indian mutual-fund scheme metadata and daily NAV history. It stores each scheme's complete history returned by the configured source, then exposes point returns, rolling returns and risk metrics. PostgreSQL is the recommended database for a full import; SQLite is useful for local development and small samples.

## Data source and coverage

The default source is [MFapi.in](https://www.mfapi.in/), a public JSON API that mirrors Indian mutual-fund NAV data. Its documented response shape is a scheme list at `/mf` and a scheme object containing `meta` and `data` at `/mf/{schemeCode}`. This is a third-party mirror, not an AMFI-operated API; source availability, scheme coverage and corrections are outside this service's control. The importer validates response shapes and does not mark malformed responses as loaded. It retries failed schemes on later runs.

[AMFI's NAV download page](https://www.amfiindia.com/net-asset-value/nav-download) is the primary industry source and offers official NAV files/history. Its web history form limits a download to 90 days at a time. The current importer uses MFapi.in's all-history endpoint rather than scraping that form. Verify data freshness and coverage with `/health`, `/schemes`, `/schemes/{code}/data-quality`, and source comparisons before operational use.

The importer fetches the source's scheme list and each returned scheme history. A successful import means the response was fetched and parsed; it does not claim that a third-party source has no omissions. Closed or merged schemes may be absent from the current scheme list, and this can create survivorship bias in category analytics.

## Run locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m app.ingest load --limit 25       # small live sample
python -m app.ingest metrics
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for interactive API documentation. The default database is `sqlite:///mf.db`.

### Full import with PostgreSQL

```bash
docker compose up -d db api
docker compose run --rm loader
docker compose run --rm loader python -m app.ingest metrics
```

The database persists in the `pgdata` volume. Import is resumable: schemes loaded in the last 20 hours are skipped, while failed schemes are eligible on the next run. Tune `--concurrency`, `--rps`, and `--max-age-hours` for the source and environment. `--limit` limits the number of schemes fetched in that run; it is intended for smoke runs, not complete ingestion.

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
| `GET /schemes/{code}/data-quality` | Coverage estimate, large date gaps and rejected input rows |
| `GET /categories` | Category counts and equal-weighted averages of cached scheme metrics |
| `GET /categories/analytics` | Metrics for an equal-weighted category NAV index and constituent dispersion |
| `GET /categories/rolling-returns` | Category index rolling-return series |
| `GET /health` | Basic API/database health response |

`/schemes` supports `q`, `category`, `fund_house`, `scheme_type`, `active`, `min_ret_1y`, `min_ret_3y`, `max_vol_3y`, `min_sharpe_3y`, `sort_by`, and `order`. Sort fields are allow-listed. Category names are case-insensitive exact matches except for `/categories?name=` substring search.

## Metric conventions

- Returns, volatility and drawdown are percentages. Periods longer than one year are annualised CAGR; shorter periods are absolute returns. A value may be `null` when history is too short or the start date is too far from the requested date.
- The start NAV is the last published NAV on or before the target date, accepted only when it is within seven calendar days. No value is interpolated or extrapolated.
- Annualised volatility is sample standard deviation of simple NAV returns multiplied by `sqrt(252)`. Returns spanning more than seven calendar days are excluded.
- Sharpe is annualised from daily excess returns, using a configurable annual risk-free rate (`rf`, default 6.5%) and 252 trading days. `rf` is expressed in percent. Zero-variance or insufficient-observation cases produce `null`/unavailable.
- Maximum drawdown is the largest observed peak-to-trough NAV decline in the selected period; the response includes peak, trough and recovery dates when available.
- Rolling returns use each NAV date as the end date and the last valid NAV on or before the window start. Windows longer than one year are annualised.
- Category analytics build an equal-weighted, daily-rebalanced index from currently active schemes, using up to `max_schemes` (default 100, maximum 500), chosen by longest stored history. It is not AUM-weighted and has survivorship bias. `/categories` averages scheme metrics directly and should not be confused with index returns.
- Published NAV is used as-is. IDCW plans therefore show NAV price returns and do not include distributions/reinvestment. For performance comparisons, use Growth plans and compare like-for-like plan types.

These are transparent implementation conventions, not a claim that there is one universally mandated return/risk calculation. Compare methodology when matching figures from another provider.

## Storage and ingestion notes

- `navs` uses `(scheme_code, date)` as its primary key, supporting ordered per-scheme range scans and idempotent upserts. PostgreSQL is configured for bulk-friendly chunks of 5,000 rows; writers are serialized for SQLite safety.
- `scheme_metrics` stores screening metrics so filtering, sorting and pagination do not scan the full NAV database. Recompute them after loading with `python -m app.ingest metrics`.
- Ingestion validates positive finite NAV values, dates and response shapes; duplicate dates are counted as rejected and the last source value wins. Isolated extreme spikes are filtered only when adjacent values strongly agree. Rejected row counts are exposed in data-quality output.
- The loader spaces requests globally within one process, retries network errors, malformed JSON, HTTP 429 and 5xx with exponential backoff and jitter, honours numeric and HTTP-date `Retry-After`, and isolates failures by scheme. `--rps` must be positive; `--concurrency` must be at least one.
- NAV rows are not forward-filled. Gaps remain visible and affect metric availability. The coverage percentage is a weekday estimate and does not account for Indian market holidays.
- API rate limiting is per process and per client IP (`RATE_LIMIT_PER_MIN`, default 300; set to `0` to disable). Multiple API workers need a shared gateway or Redis limiter. Forwarded IP headers should only be trusted behind a correctly configured proxy.

## Tests and offline demo

```bash
python -m pytest
python -m app.ingest demo --n 30
python -m app.ingest metrics
```

Tests use generated series and `httpx.MockTransport`; they do not depend on the live data source. Demo data is synthetic and must not be used as real market data.

## Known limitations

- This is a NAV analytics service, not an investment recommendation or a total-return database.
- A third-party source may omit, revise or delay records. Compare source coverage and freshness for your use case.
- Category returns depend on current scheme classifications and constituents. Closed schemes are not guaranteed to appear in today's scheme list.
- Metric screening values are batch-refreshed and can be stale until the metrics job completes.
- Multi-worker API rate limiting is not coordinated. Large Postgres installations may benefit from partitioning the NAV table after measuring their query and ingest workload.
