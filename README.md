# SQL Faker
A deterministic fake-user data generator where **all randomness lives in
SQL stored procedures**. The Python layer is a thin FastAPI shell over
PostgreSQL: it renders a form, collects locale/seed inputs, and calls
`fn_generate_batch` to produce users.

Can be self-deployed or used in a web app (deployed at Render.com with DB in Neon.tech)

- **Live demo:** [LINK](https://sql-faker-1uu2.onrender.com/)
- **Benchmark:** ~2100 users/second, generation only

Supported locales: English (United States), German (Germany), with schema designed for easy addition of new locales.

---

## Table of Contents

1. [What it does](#what-it-does)
2. [Quick start](#quick-start)
3. [Architecture](#architecture)
4. [Stored procedure reference](#stored-procedure-reference)
5. [Algorithms](#algorithms)
6. [Benchmark](#benchmark)
7. [Design decisions](#design-decisions)
8. [Data sources and attribution](#data-sources-and-attribution)
9. [Limitations and future work](#limitations-and-future-work)

---

## What it does

Given a locale and a seed, SQL Faker generates reproducible batches of
fake user contact records. Each user has:

- Gender, full name (with optional title and middle name)
- Address formatted per locale (US: house-first; DE: street-first)
- Email (auto-matched to the displayed name, accent-stripped)
- Phone number in one of several locale-appropriate formats
- Height and weight (normally distributed, gender-specific)
- Eye color, hair color (weighted by locale demographics)
- Geolocation uniformly distributed over the sphere

The defining property: **`(locale, seed, batch, idx)` always produces
the exact same user, forever.** Same seed = same data. As all of these are query parameters, same link = same data in a web deployed app.

---

## Quick start

### Run locally

```bash
# Clone
git clone https://github.com/amelylina/sql-faker.git
cd sql-faker

# Start Postgres in Docker
docker compose up -d

# Python environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Initialize schema + functions, then load data
python init_db.py     # initializes db and runs scripts in sql/
python load_data.py.  # loads data into db

# Start the web app
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

---

## Architecture

### File layout

- **app**/ FastAPI web app 
	- **main.py** Routes 
	- **database.py** Postgres connection helpers 
	- **templates**/ Jinja2 templates 
- **scripts**/ Python orchestration and loaders 
	- **benchmark.py** Performance measurement 
	- **load_`*`**.py Per-table loaders 
- **sql**/ Schema and stored procedures 
	- **01_schema.sql** Tables + indexes 
	- **02_seed_locales.sql** Locale registry 
	- **03_seed_small_tables.sql** Titles, colors, phone formats, etc.
	- **04_seed_street_names.sql** Hand-curated street roots 
	- **05_picker_cache.sql** Picker cache table 
	- **10_functions_core.sql** PRNG primitives 
	- **11_functions_picker_cache.sql** Cache rebuild function   
	- **20_functions_pickers.sql** Weighted pickers per lookup table 
	- **30_functions_generators.sql** Composite per-attribute generators 
	- **40_functions_user.sql** User orchestrator + batch function 
- **data/** 
	- **seed/** Committed parsed reference data (names, cities)
-  **init_db.py** Runs SQL migrations in order 
- **load_data.py** Loads lookup data from seed CSVs 


### Schema logic

All lookup tables use a `locale` column rather than per-locale tables.
Adding a new locale requires inserting data, not creating tables.
`street_types.format` encodes whether the street type glues to the
root (`'Bahnhofstraße'`) or is space-separated (`'Main Street'`) -
locale-specific formatting lives in data, not code.

The `names` table uses `name_type ∈ {'first', 'middle', 'last'}` and a
`gender` column with `'u'` for unisex / unknown (last names, genuinely
unisex first names). This allows a single table to serve every name
lookup.

---

## Stored procedure reference

SQL Faker is organized in four tiers, each depending only on tiers
below it:
- User-facing: 
	- fn_generate_user, 
	- fn_generate_batch 
- Generators: 
	- fn_generate_full_name, 
	- fn_generate_address, 
	- fn_generate_phone, 
	- fn_generate_email, 
	- fn_generate_geolocation 
- Pickers: 
	- fn_pick_name, 
	- fn_pick_city, 
	- fn_pick_title, 
	- ... 
- Primitives: 
	- fn_hash_uniform, 
	- fn_hash_int, 
	- fn_hash_normal, 
	- fn_hash_sphere_point, 
	- fn_hash_weighted_pick_id

All the stored functions were built using PLpgSQL.

### Primitives

#### `fn_hash_uniform(p_seed, p_batch, p_idx, p_field) -> double precision`

Deterministic pseudo-random float in `[0, 1)`.

| Arg | Type | Meaning |
|---|---|---|
| `p_seed` | BIGINT | User-supplied seed |
| `p_batch` | INT | Batch index (0, 1, 2, ...) |
| `p_idx` | INT | Index within batch |
| `p_field` | TEXT | Label for this specific attribute draw |

**Algorithm.** Builds the string `seed:batch:idx:field`, hashes it with
MD5, takes the first 16 hex characters (64 bits), interprets as unsigned
integer, divides by 2^64. See [Hash-based PRNG](#hash-based-prng) for
rationale and collision analysis.

```sql
SELECT fn_hash_uniform(42, 0, 0, 'first_name');
-- 0.22831511437925428
```

Identical inputs always produce identical output. Distinct `p_field`
values produce independent streams.

#### `fn_hash_int(p_seed, p_batch, p_idx, p_field, p_lo, p_hi) -> bigint`

Uniform integer in `[p_lo, p_hi]` (inclusive on both ends).

Built on `fn_hash_uniform`. Scales, floors, shifts. Raises an exception
if `p_lo > p_hi`.

#### `fn_hash_normal(p_seed, p_batch, p_idx, p_field, p_mean, p_stddev) -> double precision`

Deterministic draw from a Normal distribution with mean `p_mean` and
standard deviation `p_stddev`. Uses the Box-Muller transform;
see [Box-Muller](#box-muller-normal-distribution).

Output is unbounded; callers clamp for physical attributes like height.

#### `fn_hash_sphere_point(p_seed, p_batch, p_idx) -> (lat, lon)`

Uniform-on-sphere geographic coordinate. Returns `(lat, lon)` in
degrees via OUT parameters. Uses Lambert's equal-area inverse
projection; see [Uniform on sphere](#uniform-on-sphere).

#### `fn_hash_weighted_pick_id(p_seed, p_batch, p_idx, p_field, p_ids, p_weights) -> bigint`

Given parallel arrays of ids and positive weights, returns one id
chosen with probability proportional to its weight. Linear scan over
cumulative sums.

### Pickers

Each picker wraps the primitive `fn_hash_weighted_pick_id` with a
specific lookup table. All pickers read from `picker_cache`
(precomputed id/weight arrays, see [picker_cache](#picker-cache)) so
the cost is O(1) cache fetch plus O(n) scan over candidates.

| Function                                                           | Returns                     | Notes                                                          |
| ------------------------------------------------------------------ | --------------------------- | -------------------------------------------------------------- |
| `fn_pick_name(seed, batch, idx, field, locale, name_type, gender)` | TEXT                        | Respects `'u'` unisex names when gender is `'m'` or `'f'`      |
| `fn_pick_title(seed, batch, idx, field, locale, gender)`           | TEXT                        | Title matching gender, plus unisex entries like Dr./Prof.      |
| `fn_pick_city(seed, batch, idx, field, locale)`                    | (city, region, postal_code) | Atomic - all three from the same picked row                    |
| `fn_pick_street_name(seed, batch, idx, field, locale)`             | TEXT                        |                                                                |
| `fn_pick_street_type(seed, batch, idx, field, locale)`             | (type_value, type_format)   | `format` controls space vs. no-space joining (locale-specific) |
| `fn_pick_email_domain(seed, batch, idx, field, locale)`            | TEXT                        | Includes locale-agnostic domains (gmail, yahoo)                |
| `fn_pick_eye_color(seed, batch, idx, field, locale)`               | TEXT                        |                                                                |
| `fn_pick_hair_color(seed, batch, idx, field, locale)`              | TEXT                        |                                                                |
| `fn_pick_phone_format(seed, batch, idx, field, locale)`            | TEXT                        | Returns a pattern like `'(###) ###-####'`                      |

### Composite generators

| Function | Purpose |
|---|---|
| `fn_generate_full_name(seed, batch, idx, locale, gender)` | First + last, optionally title (20%) and middle (40%) |
| `fn_generate_address(seed, batch, idx, locale)` | Locale-formatted address with house number, street, city, postal |
| `fn_generate_phone(seed, batch, idx, locale)` | Phone number with `#` placeholders substituted |
| `fn_generate_email(seed, batch, idx, locale, gender)` | Email matching the generated name (re-derives first/last via same field labels) |
| `fn_generate_geolocation(seed, batch, idx)` | Wraps `fn_hash_sphere_point` |

### Orchestrator

#### `fn_generate_user(p_seed, p_batch, p_idx, p_locale) -> user_row`

Returns one complete fake user as a `user_row` composite with 15
columns: identifying tuple (locale, seed, batch_idx, idx_in_batch),
gender, full_name, address, phone, email, height_cm, weight_kg,
eye_color, hair_color, lat, lon.

Physical attributes are gender-specific normal distributions clamped
to realistic ranges:
- Male: height ~N(178, 7²) cm, weight ~N(82, 12²) kg
- Female: height ~N(165, 6²) cm, weight ~N(68, 11²) kg
- Both clamped to [140, 210] cm / [40, 150] kg

#### `fn_generate_batch(p_seed, p_batch, p_locale, p_batch_size) -> SETOF user_row`

Returns a set of `p_batch_size` users. Index within batch runs from 0
to `p_batch_size - 1`.

```sql
SELECT * FROM fn_generate_batch(42, 0, 'en_US', 10);
```

---

## Algorithms

### Hash-based PRNG

The task requires that `(locale, seed, batch, idx)` always produces the exact same user. The `SETSEED(seed); SELECT random()` - works in a loop on paper, but breaks as soon as functions start calling other functions in different orders or in parallel. Whichever generator runs first consumes different random numbers and reproducibility falls apart.

So instead of a stateful RNG, I went with a stateless one: for every random draw, build a string that uniquely identifies that draw, hash it, and turn the hash into a number. Concretely, `fn_hash_uniform` builds the string `seed:batch:idx:field`, MD5s it, takes the first 16 hex characters (64 bits), interprets those as an unsigned integer, and divides by 2^64. Output is a float in `[0, 1)`.

Why MD5 and not something stronger? MD5 might be cryptographically broken, but what matters is that it mixes inputs uniformly across the output range, is built into Postgres, and is fast. SHA-256 would work identically at roughly 2x the cost. (First thing i checked)

The colon separators prevent collisions like `(batch=12, idx=3)` vs `(batch=1, idx=23)` producing the same string. The `field` argument is what lets different attributes of the same user: first name, last name, height, email pattern come from independent random streams. And that's exactly why the email matches the displayed name without any coordination. Email can call name generation function independently of name generation passing it, because it can form same string that we later hash.

The function is marked `IMMUTABLE`, which tells Postgres the output depends only on inputs, so it can cache and reorder calls freely.

### Box-Muller normal distribution

Heights and weights need a bell-curve distribution, not a uniform one and you can't get that by linearly rescaling a uniform.

In Box-Muller the idea is that a 2D Gaussian distribution has circular symmetry (points at equal distances from the origin are equally likely, regardless of direction), which makes it easier to generate in polar coordinates than in Cartesian. Pick an angle uniformly in `[0, 2π)`, pick a radius with a specific distribution (derived from the Gaussian), then convert to (x, y). Both coordinates turn out to be independent standard normals.

The formula: given two independent uniforms `u1, u2 ∈ (0, 1)`:

```
z = sqrt(-2 * ln(u1)) * cos(2π * u2)
```

This `z` is a draw from `N(0, 1)`. Scale by stddev and shift by mean to get any desired normal: `mean + stddev * z ~ N(mean, stddev²)`.

Box-Muller actually produces two independent normals in one shot - the paired formula uses `sin()` instead of `cos()` for the second draw. During my research I saw most of the libraries cache and store the second one for later calls, but I decided against that for ease of use and structure.

One edge case: if `u1` is exactly 0, `ln(0) = -∞` and the formula blows up. I put a guard against that in code.

For user heights, I use gender-specific means and stdevs, and then clamp the output.
### Uniform on sphere

Here's why naive wouldn't work for randomly picked unified coords. If you pick `lat` uniformly in `[-90, 90]` and `lon` uniformly in `[-180, 180]`, you get clustering at the poles.

The fix is Lambert's cylindrical equal-area projection. If you plot `sin(latitude)` instead of `latitude`, equal intervals of `sin(lat)` correspond to equal surface areas on the sphere. So you sample in `sin`-space and invert back to degrees.

Pick `u, v` uniformly in `[0, 1)`, then:

```
longitude = 360 * u - 180
latitude  = degrees(asin(2 * v - 1))
```

Longitude is "flat" in terms of area - rotating around the axis doesn't distort anything - so it maps linearly.

Consequence worth calling out: geolocation coordinates are independent of the user's street address. A generated user can live at "4723 Main Street, Springfield, IL" while their geolocation coordinate drops them in the middle of the Pacific. About 71% of generated points fall over oceans, since that's 71% of Earth's surface. So let's suppose our fake user is just having a vacation too many kilometers away from land, for the sake of task's requirements.

### Picker cache

The problem: for generating 10,000 users, `fn_pick_name` alone gets called 3-4 times per user (first, last, sometimes middle, sometimes title). That's ~30-40k calls to rebuild the exact same array. Hence the addition of picker cache table and functions.

The cache: a single table, `picker_cache`, that stores precomputed `(ids, weights)` arrays under string keys like `'names:en_US:first:m'` or `'cities:de_DE'` as PRIMARY KEY for fast lookups. Populated once by `fn_rebuild_picker_cache()` after any data load. 

---

## Benchmark

| Locale | Batch size | Server time | Round-trip time | Server u/s | Round-trip u/s |
|---|---|---|---|---|---|
| en_US | 10 | 0.008s | 0.009s | 1306 | 1175 |
| en_US | 100 | 0.072s | 0.071s | 1398 | 1409 |
| en_US | 1000 | 0.720s | 0.715s | 1388 | 1398 |
| en_US | 10000 | 7.274s | 7.223s | 1375 | 1384 |
| de_DE | 10 | 0.005s | 0.005s | 2051 | 1920 |
| de_DE | 100 | 0.048s | 0.049s | 2077 | 2047 |
| de_DE | 1000 | 0.479s | 0.481s | 2089 | 2077 |
| de_DE | 10000 | 4.803s | 4.808s | 2082 | 2080 |

Measured on Apple M5 (local Docker Postgres), median of 5 runs per
configuration.

**Methodology.** Server timing via `EXPLAIN ANALYZE`'s reported
execution time. Round-trip timing via `time.perf_counter()` wrapping
`cursor.execute()` + `cursor.fetchall()` in Python.

**Diagnostic note.** Naive benchmarking with
`SELECT COUNT(*) FROM (SELECT fn_generate_user(...))`
does *not* force evaluation of `STABLE`/`IMMUTABLE` functions whose
outputs aren't materialized. In order to test the self-deployed db - run the actual batch generations with **`fn_generate_batch`**.

**Optimization history.** 
The picker originally relied on a per-call `array_agg`  scans of lookup tables (for id and weight of parameter) and had output of ~1000 u/s. Current optimized state was reached through the addition of cache table and caching function, to avoid multiple aggregations of same table per one function call (like in composite function for generating name, where we need to draw first, last and possibly middle name for one person).
Remaining overhead is primarily PLpgSQL translation overhead related, as multiple tests on functions separately did not show any signs of them being a "bottleneck". Further optimization could be achieved through inlining logic into a single SQL CTE call, but that would remove one of the main characteristics of this project being a "library of small procedures".

---

## Design decisions
### Schema
- Single table per category with locale column
- Gender column semantics ('m', 'f', 'u')
- `email_domains.locale` nullable for globally-applicable entries
- Street formatting rules as data (`street_types.format`), not code

### Determinism
- Hash-based PRNG over seed-based stateful random
- MD5 over SHA-256 (speed, non-adversarial workload)
- Middle/email name consistency via shared field labels (no parameter passing)

### Performance
- Picker cache for O(1) candidate array retrieval
- `IMMUTABLE` / `STABLE` function markings for planner optimization

### Locale handling
- Some instances of hardcoded IF/ELSE structure in first-run small data insertion to the database (2 locales justifies this, database structure is designed to be expandable and no actual changes will need to be done to the database itself, just data loader scripts)
- Accent stripping: ß -> ss (traditional), umlauts -> single letter (ae to a)

### Math
- Box-Muller for normal distribution
- Lambert equal-area cylinder inverse projection for sphere points
- Addresses and geolocation are independent; ~71% of
  generated points will probably fall over oceans

---

## Data sources and attribution

- **US first names:** US Social Security Administration
  "yob2020.txt" from the national baby-names dataset.
- **US last names:** US Census Bureau 2010 surname file.
- **US cities:** SimpleMaps Basic US Cities Database (free tier).
- **German first names and surnames:** public wikipedia pages and tables, extracted
  into `data/seed/de_firstnames.csv` and `data/seed/de_surname.csv`.
- **German cities:** GeoNames DE postal code dataset, CC-BY 4.0.
- **Street names, email domains, titles, colors, phone formats:**
  hand-curated.

All lookup data is either downloaded from API in provided python scripts or in `data/seed/` and committed to the repository.

**Note**. Finding good quality, parsed German name data appeared to be one of the hardest tasks for this project, hence I decided to stick to a self parsed aggregated file.

---

## Limitations and future work

- Two locales (en_US, de_DE). Adding a third requires inserting data
  in every lookup table and adding an IF branch to `fn_generate_address`.
- Phone numbers are plausibility-grade, not numbering-plan-accurate
  (no real area-code rules).
- Physical attributes are biologically plausible but not
  demographically calibrated.
- Benchmark: ~2100 u/s is adequate for the task's 100k–1M scale
  requirement but could be improved with query inlining or a C
  extension for hashing.
- Geolocation is uniform on sphere per spec; ~71% of points are in
  ocean. This is intentional, not a bug.
