import streamlit as st
import json
import re
import urllib.request
import urllib.parse
import time
import os
import csv
import io
import html
import random
from datetime import datetime

try:
    import anthropic
except ImportError:
    st.error("Run: pip install anthropic")
    st.stop()

st.set_page_config(page_title="Should AI Clean Your Property Data?", layout="wide")

st.markdown("""
<style>
    .block-container { max-width: 1100px; }
    .stTabs [data-baseweb="tab-list"] { gap: 24px; }
    div[data-testid="stMetric"] { background: white; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; }
    /* Hide Streamlit Cloud branding for a cleaner app look */
    [data-testid="stToolbar"] { display: none !important; }
    [data-testid="stDecoration"] { display: none !important; }
    [data-testid="stStatusWidget"] { display: none !important; }
    .stDeployButton { display: none !important; }
    #MainMenu { visibility: hidden !important; }
    footer { visibility: hidden !important; }
    header { background: transparent !important; }
</style>
""", unsafe_allow_html=True)

st.title("Should AI Clean Your Property Data?")
st.caption("Run real property records through three AI tasks. See which findings to auto-apply, which to verify, which to hold — and what it costs at scale.")

def load_env_key():
    # Check Streamlit secrets first (Cloud), then OS env var (local dev). The
    # secrets accessor raises a StreamlitSecretNotFoundError subclass when no
    # secrets.toml exists; broaden to Exception so any unexpected internal error
    # falls back to the env var rather than crashing the sidebar.
    try:
        if "ANTHROPIC_API_KEY" in st.secrets:
            return st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        pass
    return os.environ.get("ANTHROPIC_API_KEY", "")

_env_key = load_env_key()

# ── Sidebar ──
with st.sidebar:
    st.header("Configuration")
    if _env_key:
        api_key = _env_key
        st.caption("✓ Ready — no API key required to run an evaluation.")
    else:
        api_key = st.text_input(
            "Anthropic API Key",
            type="password",
            help="Bring your own key — used only for this session, never saved server-side.",
        )
    model = st.selectbox("Model", ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"], index=0)
    st.divider()

    with st.expander("📂 Load saved run", expanded=False):
        st.caption("Drop in a previously downloaded `aitaskgrader-*.json` to view its results without re-running.")
        uploaded_run = st.file_uploader("Run JSON", type=["json"], key="saved_run", label_visibility="collapsed")
        if uploaded_run is not None:
            try:
                payload = json.loads(uploaded_run.read().decode("utf-8"))
                if not isinstance(payload, dict) or "data" not in payload or "results" not in payload:
                    raise ValueError("File missing required 'data' and 'results' fields.")
                loaded_results = payload["results"]
                # Address results are keyed by integer record id at write time, but JSON
                # round-trips object keys as strings. Cast back so downstream int lookups work.
                addrs = loaded_results.get("addresses")
                if isinstance(addrs, dict):
                    loaded_results["addresses"] = {int(k): v for k, v in addrs.items() if str(k).isdigit()}
                st.session_state["data"] = payload["data"]
                st.session_state["results"] = loaded_results
                st.session_state["data_source"] = payload.get("city", "uploaded")
                st.success(f"Loaded {len(payload['data'])} records from {payload.get('city', 'saved run')} (generated {payload.get('generated_at', 'unknown date')[:10]}).")
            except Exception as e:
                st.error(f"Couldn't load that file: {e}")
    st.divider()

    st.header("Data Source")
    source = st.radio("Choose source", ["NYC (PLUTO API)", "Toronto (Open Data)", "Vancouver (Open Data)", "Upload CSV"])

    if source == "NYC (PLUTO API)":
        st.subheader("NYC Filters")
        borough = st.selectbox("Borough", ["Any", "Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"])
        boro_map = {"Manhattan": "MN", "Brooklyn": "BK", "Queens": "QN", "Bronx": "BX", "Staten Island": "SI"}
        zip_filter = st.text_input("ZIP Code (optional)", placeholder="e.g. 10013")
        street_filter = st.text_input("Street name (optional)", placeholder="e.g. BROADWAY")
        num_records = st.slider("Records to fetch", 10, 200, 50)

    elif source == "Vancouver (Open Data)":
        st.subheader("Vancouver Filters")
        van_neighbourhoods = {
            "Any": None,
            "Downtown": "029",
            "West End": "030",
            "Kitsilano": "004",
            "Mount Pleasant": "010",
            "Grandview-Woodland": "009",
            "Hastings-Sunrise": "008",
            "Strathcona": "028",
            "Fairview": "011",
            "Kerrisdale": "005",
            "Dunbar-Southlands": "003",
            "Shaughnessy": "006",
            "South Cambie": "012",
            "Riley Park": "013",
            "Kensington-Cedar Cottage": "014",
            "Renfrew-Collingwood": "015",
            "Marpole": "016",
            "Oakridge": "017",
            "Sunset": "018",
            "Victoria-Fraserview": "019",
            "Killarney": "020",
            "West Point Grey": "002",
            "Arbutus Ridge": "001",
        }
        van_neighbourhood = st.selectbox("Neighbourhood", list(van_neighbourhoods.keys()))
        van_street = st.text_input("Street name (optional)", placeholder="e.g. ROBSON", key="van_street")
        van_postal = st.text_input("Postal code prefix (optional)", placeholder="e.g. V6B", key="van_postal")
        num_records = st.slider("Records to fetch", 10, 200, 50, key="van_num")

    elif source == "Toronto (Open Data)":
        st.subheader("Toronto Filters")
        tor_wards = {
            "Any": None,
            "1 — Etobicoke North": "01", "2 — Etobicoke Centre": "02", "3 — Etobicoke-Lakeshore": "03",
            "4 — Parkdale-High Park": "04", "5 — York South-Weston": "05", "6 — York Centre": "06",
            "7 — Humber River-Black Creek": "07", "8 — Eglinton-Lawrence": "08", "9 — Davenport": "09",
            "10 — Spadina-Fort York": "10", "11 — University-Rosedale": "11", "12 — Toronto-St. Paul's": "12",
            "13 — Toronto Centre": "13", "14 — Toronto-Danforth": "14", "15 — Don Valley West": "15",
            "16 — Don Valley East": "16", "17 — Don Valley North": "17", "18 — Willowdale": "18",
            "19 — Beaches-East York": "19", "20 — Scarborough Southwest": "20", "21 — Scarborough Centre": "21",
            "22 — Scarborough-Agincourt": "22", "23 — Scarborough North": "23",
            "24 — Scarborough-Guildwood": "24", "25 — Scarborough-Rouge Park": "25",
        }
        tor_ward = st.selectbox("Ward", list(tor_wards.keys()), key="tor_ward_sel")
        tor_street = st.text_input("Street name (optional)", placeholder="e.g. DUNDAS", key="tor_street")
        tor_fsa = st.text_input("Postal FSA prefix (optional)", placeholder="e.g. M5V", key="tor_fsa")
        num_records = st.slider("Records to fetch", 10, 200, 50, key="tor_num")

    else:
        uploaded = st.file_uploader("Upload CSV", type=["csv"])
        st.caption("CSV must have columns: address, and optionally: zipcode, ownername, bldgclass, yearbuilt")
        csv_city = st.selectbox(
            "Treat records as",
            ["Auto-detect", "NYC", "Vancouver", "Toronto"],
            help="Auto-detect uses postal-code prefixes (V→Vancouver, M→Toronto). "
                 "Pick explicitly if the data is from somewhere else — otherwise prompts default to NYC.",
        )

# ── Data fetching ──
def _safe_soql_str(s):
    """Whitelist filter inputs going into SoQL string literals: alphanumeric, space,
    dash. Socrata has no parameterized binding for SoQL string literals, so the
    only safe path is to refuse anything else."""
    return re.sub(r"[^A-Za-z0-9 \-]", "", s or "").strip()

def fetch_nyc_data(borough, zip_filter, street_filter, limit):
    conditions = ["address IS NOT NULL"]
    if borough != "Any":
        conditions.append(f"borough='{boro_map[borough]}'")
    zip_clean = _safe_soql_str(zip_filter)
    if zip_clean:
        conditions.append(f"zipcode='{zip_clean}'")
    street_clean = _safe_soql_str(street_filter).upper()
    if street_clean:
        conditions.append(f"address like '%25{street_clean}%25'")
    where = " AND ".join(conditions)
    params = urllib.parse.urlencode({"$limit": limit, "$where": where, "$order": "bbl", "$offset": 0})
    url = f"https://data.cityofnewyork.us/resource/64uk-42ks.json?{params}"
    with urllib.request.urlopen(urllib.request.Request(url), timeout=30) as resp:
        return json.loads(resp.read())

def fetch_vancouver_data(neighbourhood, street, postal, limit):
    conditions = ["from_civic_number IS NOT NULL"]
    code = van_neighbourhoods.get(neighbourhood)
    if code:
        conditions.append(f"neighbourhood_code='{code}'")
    street_clean = _safe_soql_str(street).upper()
    if street_clean:
        conditions.append(f"street_name LIKE '*{street_clean}*'")
    postal_clean = _safe_soql_str(postal).upper()
    if postal_clean:
        conditions.append(f"property_postal_code LIKE '{postal_clean}*'")
    where = " AND ".join(conditions)
    params = urllib.parse.urlencode({"limit": limit, "where": where})
    url = f"https://opendata.vancouver.ca/api/explore/v2.1/catalog/datasets/property-tax-report/records?{params}"
    with urllib.request.urlopen(urllib.request.Request(url), timeout=30) as resp:
        raw = json.loads(resp.read())

    records = []
    for r in raw.get("results", []):
        civic = r.get("from_civic_number", "")
        street_name = r.get("street_name", "")
        address = f"{civic} {street_name}".strip() if civic else street_name
        records.append({
            "address": address,
            "zipcode": r.get("property_postal_code", ""),
            "ownername": "",
            "bldgclass": r.get("zoning_classification", ""),
            "yearbuilt": r.get("year_built", ""),
            "legal_type": r.get("legal_type", ""),
            "zoning_district": r.get("zoning_district", ""),
            "current_land_value": r.get("current_land_value", ""),
            "current_improvement_value": r.get("current_improvement_value", ""),
            "tax_levy": r.get("tax_levy", ""),
            "neighbourhood_code": r.get("neighbourhood_code", ""),
            "narrative_legal": " ".join(filter(None, [
                r.get("narrative_legal_line1", ""),
                r.get("narrative_legal_line2", ""),
                r.get("narrative_legal_line3", ""),
            ])),
            "_source": "vancouver",
        })
    return records

TORONTO_RESOURCE_ID = "3ad76a8c-0518-4df2-b94e-8c747d62f8c1"

def fetch_toronto_data(ward_label, street, fsa, limit):
    filters = {}
    ward_code = tor_wards.get(ward_label)
    if ward_code:
        filters["WARD"] = ward_code
    fetch_limit = 4000 if (street or fsa) else limit
    params = {
        "resource_id": TORONTO_RESOURCE_ID,
        "limit": fetch_limit,
    }
    if filters:
        params["filters"] = json.dumps(filters)
    url = "https://ckan0.cf.opendata.inter.prod-toronto.ca/api/3/action/datastore_search?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(url), timeout=30) as resp:
        raw = json.loads(resp.read())

    records = []
    street_upper = street.upper() if street else None
    fsa_upper = fsa.upper() if fsa else None
    for r in raw.get("result", {}).get("records", []):
        if len(records) >= limit:
            break
        pcode = (r.get("PCODE") or "").strip()
        if fsa_upper and not pcode.upper().startswith(fsa_upper):
            continue
        address = " ".join((r.get("SITE_ADDRESS") or "").split())
        if street_upper and street_upper not in address.upper():
            continue
        records.append({
            "address": address,
            "zipcode": pcode,
            "ownername": r.get("PROP_MANAGEMENT_COMPANY_NAME", ""),
            "bldgclass": r.get("PROPERTY_TYPE", ""),
            "yearbuilt": r.get("YEAR_BUILT", ""),
            "year_registered": r.get("YEAR_REGISTERED", ""),
            "confirmed_units": r.get("CONFIRMED_UNITS", ""),
            "no_of_units": r.get("NO_OF_UNITS", ""),
            "confirmed_storeys": r.get("CONFIRMED_STOREYS", ""),
            "no_of_storeys": r.get("NO_OF_STOREYS", ""),
            "ward": r.get("WARD", ""),
            "heating_type": r.get("HEATING_TYPE", ""),
            "parking_type": r.get("PARKING_TYPE", ""),
            "amenities": r.get("AMENITIES_AVAILABLE", ""),
            "_source": "toronto",
        })
    return records

def parse_csv_upload(uploaded_file, source_tag=None):
    raw_bytes = uploaded_file.read()
    try:
        content = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # Fall back to latin-1 so government exports with stray non-utf-8 bytes
        # don't blow up — surface a warning to the user separately.
        content = raw_bytes.decode("latin-1", errors="replace")
    rows = list(csv.DictReader(io.StringIO(content)))
    if source_tag:
        for r in rows:
            r["_source"] = source_tag
    return rows

# ── LLM calls ──
# Public list prices, USD per 1M tokens (input, output). Update when Anthropic re-prices.
MODEL_PRICING = {
    "claude-sonnet-4-6":            (3.00, 15.00),
    "claude-haiku-4-5-20251001":    (1.00,  5.00),
}
BATCH_DISCOUNT = 0.5  # Anthropic Batches API: 50% off both sides.

_RETRY_STATUS = {429, 500, 502, 503, 504, 529}
_MAX_RETRIES = 4

def call_claude(client, prompt, model_name, max_tokens=4096, label=None):
    """Call Anthropic with exponential-backoff retries on transient errors.
    Accumulates token usage and surfaces max_tokens truncation as a session warning."""
    last_exc = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = client.messages.create(
                model=model_name, max_tokens=max_tokens,
                system="You are a real estate data processing assistant. Return ONLY valid JSON, no explanation or markdown.",
                messages=[{"role": "user", "content": prompt}],
            )
            break
        except anthropic.APIStatusError as e:
            if getattr(e, "status_code", None) in _RETRY_STATUS and attempt < _MAX_RETRIES:
                last_exc = e
                time.sleep(min(2 ** attempt, 16))
                continue
            raise
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            if attempt < _MAX_RETRIES:
                last_exc = e
                time.sleep(min(2 ** attempt, 16))
                continue
            raise
    else:
        if last_exc is not None:
            raise last_exc

    usage = st.session_state.setdefault("_usage", {"input_tokens": 0, "output_tokens": 0, "calls": 0})
    usage["input_tokens"] += getattr(resp.usage, "input_tokens", 0) or 0
    usage["output_tokens"] += getattr(resp.usage, "output_tokens", 0) or 0
    usage["calls"] += 1

    if getattr(resp, "stop_reason", None) == "max_tokens":
        warnings = st.session_state.setdefault("_warnings", [])
        warnings.append(
            f"{label or 'A model call'} hit the {max_tokens}-token output cap — "
            f"output was truncated and downstream results may be incomplete."
        )

    return resp.content[0].text

def compute_cost(usage, model_name, num_records):
    """Return (actual_cost, projected_1m_batched) in USD given accumulated usage."""
    if not usage:
        return None, None
    in_price, out_price = MODEL_PRICING.get(model_name, MODEL_PRICING["claude-sonnet-4-6"])
    actual = (usage.get("input_tokens", 0) * in_price + usage.get("output_tokens", 0) * out_price) / 1_000_000
    if not num_records or actual <= 0:
        return actual, None
    per_record = actual / num_records
    projected_batched = per_record * 1_000_000 * BATCH_DISCOUNT
    return actual, projected_batched

def unit_economics_strings(results, model_name, num_records):
    """Single source of truth for the (ai_headline, cost_footnote) pair shown in
    both the in-app cost card and the downloadable HTML report."""
    usage = (results.get("_usage") or {}) if isinstance(results, dict) else {}
    actual_cost, projected = compute_cost(usage, model_name, num_records)
    if actual_cost is None or projected is None:
        return "—", "No usage recorded for this run — re-run the evaluation to see real cost numbers."
    if projected < 1000:
        headline = f"${projected:,.0f}"
    elif projected < 10000:
        headline = f"${projected/1000:.1f}K"
    else:
        headline = f"${projected/1000:,.0f}K"
    footnote = (
        f"This run: {usage.get('calls', 0)} API call(s), "
        f"{usage.get('input_tokens', 0):,} in / {usage.get('output_tokens', 0):,} out tokens, "
        f"${actual_cost:.4f} actual on {num_records} records. "
        f"Projection assumes Anthropic Batches API ({int(BATCH_DISCOUNT*100)}% discount) at {model_name} list prices."
    )
    return headline, footnote

def parse_json_response(text):
    """Parse Claude's JSON output, salvaging a trailing-truncated array if needed.
    Object-mode truncation is NOT silently recovered (we'd just produce malformed
    JSON) — it raises so the caller can surface a real error."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Model returned an empty response — the output may have been truncated or the request was too large.")
    if text.startswith("```"):
        parts = text.split("\n", 1)
        text = parts[1] if len(parts) > 1 else ""
        text = text.rsplit("```", 1)[0].strip()
    if not text:
        raise ValueError("Model returned only an empty code fence — the output may have been truncated.")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # If the failure is "Extra data" (valid JSON followed by trailing prose
        # or a second value), consume just the first JSON value and ignore the
        # rest. This is a common failure mode on complex structured tasks.
        try:
            parsed, _end = json.JSONDecoder().raw_decode(text)
            return parsed
        except json.JSONDecodeError:
            pass
        if text.startswith("["):
            # Trim back to the last complete top-level object, then close the array.
            depth = 0
            in_str = False
            esc = False
            last_complete = -1
            for i, ch in enumerate(text):
                if esc:
                    esc = False
                    continue
                if ch == "\\" and in_str:
                    esc = True
                    continue
                if ch == '"':
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        last_complete = i
            if last_complete > 0:
                return json.loads(text[:last_complete + 1] + "]")
        raise

# ── Task prompts (adaptive to city) ──
def detect_city(records):
    for r in records:
        src = r.get("_source")
        if src == "vancouver":
            return "vancouver"
        if src == "toronto":
            return "toronto"
        zc = (r.get("zipcode") or "").strip().upper()
        if zc.startswith("V"):
            return "vancouver"
        if zc.startswith("M"):
            return "toronto"
    return "nyc"

ADDRESS_CHUNK_SIZE = 40

def _build_address_prompt(batch, city):
    if city == "toronto":
        return f"""Parse and standardize each Toronto address into structured components.

Rules:
- Convert ALL CAPS to proper Title Case
- Collapse double/triple spaces in the raw address
- Abbreviate street types: Street→St, Avenue→Ave, Boulevard→Blvd, Place→Pl, Lane→Ln, Court→Ct, Road→Rd, Drive→Dr, Crescent→Cres, Terrace→Ter, Parkway→Pkwy
- Abbreviate directionals: West→W, East→E, North→N, South→S (these appear AFTER the street name in Toronto, e.g. "The Donway E")
- City is always Toronto, province is always ON
- The "fsa" input is the first half of the postal code (Forward Sortation Area, 3 chars). Keep it in the output; do not invent a full postal code
- If a unit number is embedded (e.g. "12-100 King St"), split it into unit and street_number
- Note every change you made from the original

Input records:
{json.dumps(batch, indent=2)}

Return JSON array (one object per input id, preserving every input id):
[{{"id": 1, "unit": "", "street_number": "12", "street_name": "The Donway E", "city": "Toronto", "province": "ON", "fsa": "M3C", "standardized": "12 The Donway E, Toronto, ON M3C", "changes": ["Title-cased street name", "Collapsed double spaces"]}}]"""
    if city == "vancouver":
        return f"""Parse and standardize each Vancouver address into structured components.

Rules:
- Convert ALL CAPS to proper Title Case
- Abbreviate street types: Street→St, Avenue→Ave, Boulevard→Blvd, Place→Pl, Lane→Ln, Court→Ct, Road→Rd, Drive→Dr, Crescent→Cres, Mews→Mews
- Abbreviate directionals: West→W, East→E, North→N, South→S
- City is always Vancouver, province is always BC
- If a unit/townhouse prefix exists (e.g. "TH116"), split it into unit and street_number
- Note every change you made from the original

Input records:
{json.dumps(batch, indent=2)}

Return JSON array (one object per input id, preserving every input id):
[{{"id": 1, "unit": "", "street_number": "605", "street_name": "Hamilton St", "city": "Vancouver", "province": "BC", "postal_code": "V6B 5W4", "standardized": "605 Hamilton St, Vancouver, BC V6B 5W4", "changes": ["Title-cased street name", "Abbreviated Street→St"]}}]"""
    return f"""Parse and standardize each NYC address into structured components.

Rules:
- Convert ALL CAPS to proper Title Case
- Abbreviate street types: Street→St, Avenue→Ave, Boulevard→Blvd, Place→Pl, Lane→Ln, Court→Ct, Road→Rd
- Abbreviate directionals: West→W, East→E, North→N, South→S
- Borough codes: MN=New York, BK=Brooklyn, QN=Queens, BX=Bronx, SI=Staten Island
- State is always NY
- If a unit/apt number is embedded, split it out
- Note every change you made from the original

Input records:
{json.dumps(batch, indent=2)}

Return JSON array (one object per input id, preserving every input id):
[{{"id": 1, "unit": "", "street_number": "325", "street_name": "Greenwich St", "city": "New York", "state": "NY", "zipcode": "10013", "standardized": "325 Greenwich St, New York, NY 10013", "changes": ["Title-cased street name", "Abbreviated Street→St"]}}]"""

def _address_batch_for(record, city, rid):
    if city == "toronto":
        return {"id": rid, "raw_address": record.get("address", ""), "fsa": record.get("zipcode", ""), "ward": record.get("ward", "")}
    if city == "vancouver":
        return {"id": rid, "raw_address": record.get("address", ""), "postal_code": record.get("zipcode", "")}
    return {"id": rid, "raw_address": record.get("address", ""), "borough": record.get("borough", ""), "zipcode": record.get("zipcode", "")}

def run_address_task(client, records, model_name):
    city = detect_city(records)
    all_results = {}
    n = len(records)
    for start in range(0, n, ADDRESS_CHUNK_SIZE):
        chunk = records[start:start + ADDRESS_CHUNK_SIZE]
        batch = [_address_batch_for(r, city, start + i + 1) for i, r in enumerate(chunk)]
        prompt = _build_address_prompt(batch, city)
        label = f"Address chunk {start // ADDRESS_CHUNK_SIZE + 1} of {(n + ADDRESS_CHUNK_SIZE - 1) // ADDRESS_CHUNK_SIZE}"
        raw = call_claude(client, prompt, model_name, max_tokens=8192, label=label)
        parsed = parse_json_response(raw)
        for r in parsed:
            try:
                all_results[int(r["id"])] = r
            except (KeyError, TypeError, ValueError):
                continue
    return all_results

def run_classification_task(client, records, model_name):
    city = detect_city(records)

    if city == "toronto":
        property_types = sorted(set(r.get("bldgclass", "") for r in records if r.get("bldgclass")))
        heating_types = sorted(set(r.get("heating_type", "") for r in records if r.get("heating_type")))
        parking_types = sorted(set(r.get("parking_type", "") for r in records if r.get("parking_type")))
        prompt = f"""Decode these Toronto apartment-building classifications into human-readable descriptions.

These come from the City of Toronto's RentSafeTO Apartment Building Registration dataset.

Property types: {json.dumps(property_types)}
Heating types: {json.dumps(heating_types)}
Parking types: {json.dumps(parking_types)}

Return a JSON object with three keys:
{{
  "property_types": {{"PRIVATE": "description...", "TCHC": "Toronto Community Housing Corporation, ..."}},
  "heating_types": {{"HOT WATER": "description...", "FORCED AIR": "description..."}},
  "parking_types": {{"UNDERGROUND": "description...", "SURFACE": "description..."}}
}}"""
    elif city == "vancouver":
        codes = list(set(r.get("bldgclass", "") for r in records if r.get("bldgclass")))
        zoning_districts = list(set(r.get("zoning_district", "") for r in records if r.get("zoning_district")))
        legal_types = list(set(r.get("legal_type", "") for r in records if r.get("legal_type")))
        prompt = f"""Decode these Vancouver property classifications into human-readable descriptions.

Zoning classifications: {json.dumps(codes)}
Zoning district codes: {json.dumps(zoning_districts)}
Legal types: {json.dumps(legal_types)}

Return a JSON object with three keys:
{{
  "zoning_classifications": {{"Comprehensive Development": "description..."}},
  "zoning_districts": {{"DD": "description...", "CD-1 (266)": "description..."}},
  "legal_types": {{"STRATA": "description...", "LAND": "description..."}}
}}"""
    else:
        codes = list(set(r.get("bldgclass", "") for r in records if r.get("bldgclass")))
        prompt = f"""Decode these NYC Department of Finance building class codes into human-readable descriptions.

Codes to decode: {json.dumps(codes)}

Return JSON object mapping each code to its description:
{{"D2": "Elevator Apartment, ...", "A7": "One Family, ..."}}"""

    raw = call_claude(client, prompt, model_name, max_tokens=4096, label="Classification task")
    return parse_json_response(raw)

def run_verdict_task(client, results, city, num_records, model_name):
    summary = {
        "city": city,
        "records_evaluated": num_records,
        "address_count": len(results.get("addresses", {})),
        "classification_result": results.get("classifications", {}),
        "quality_issues": [
            {"severity": q.get("severity"), "title": q.get("title"), "affected_rows": q.get("affected_rows", [])}
            for q in results.get("quality", [])
        ],
        "quality_issue_count": len(results.get("quality", [])),
        "high_severity_count": sum(1 for q in results.get("quality", []) if q.get("severity") == "high"),
        "medium_severity_count": sum(1 for q in results.get("quality", []) if q.get("severity") == "medium"),
        "low_severity_count": sum(1 for q in results.get("quality", []) if q.get("severity") == "low"),
    }

    city_label = {"vancouver": "Vancouver", "toronto": "Toronto"}.get(city, "NYC")
    available_findings = [q.get("title", "") for q in results.get("quality", []) if q.get("title")]
    prompt = f"""You just evaluated {num_records} real {city_label} property records with AI across three tasks.

Your job now is to convert these results into a TRIAGE QUEUE for a data analyst.
Every cleanup opportunity in this dataset — whether from address standardization,
classification, or the quality audit — should become one item in the queue with
a clear disposition:

- "auto_apply": AI's fix is mechanical, reversible, and safe. Apply without review.
  (e.g. title-casing addresses, expanding standard abbreviations, normalizing
  obviously-equivalent name variants like "AKELIUS " → "AKELIUS CANADA LTD")
- "verify": AI made a judgment call that's probably right but worth a human check.
  (e.g. inferring a missing FSA from neighboring records, flagging a suspicious
  registration year)
- "hold": Issue is real but no safe automated fix exists; needs investigation.
  (e.g. near-empty placeholder records, contradictions that could go either way)
- "ignore": Not actionable; cosmetic or out of scope for this pipeline.

Bundle bulk uniform work into a single item where appropriate
(e.g. "Apply 50 address standardizations" → auto_apply with affected_rows = all
records and finding = null). Do not produce one item per row for uniform work.

CITATION REQUIREMENT: every row in an item's affected_rows must be supported.
If the item is tied to a specific quality finding, set "finding" to the EXACT
title from the list below and every affected row must actually appear in that
finding's audit. If the item is bulk work not tied to a quality issue (e.g. the
sweep of address standardizations), set "finding" to null.

Available finding titles (use one verbatim, or null):
{json.dumps(available_findings, indent=2)}

Audit results:

{json.dumps(summary, indent=2)}

Format:
{{
  "items": [
    {{
      "title": "Short imperative — what would be done (5-10 words)",
      "disposition": "auto_apply" | "verify" | "hold" | "ignore",
      "proposed_fix": "What AI would do to resolve this, in one sentence",
      "why_disposition": "Why this disposition and not another (1-2 sentences)",
      "affected_rows": [3, 5, 23, 26],
      "finding": "exact finding title from the list above, or null"
    }}
  ],
  "summary": "2-3 sentence overall narrative — what's safe to automate, what needs human review, what's blocked on investigation."
}}"""

    raw = call_claude(client, prompt, model_name, max_tokens=8192, label="Verdict / triage queue")
    return parse_json_response(raw)

# ── Verdict citation guardrail ──
# The verdict step is synthesis prose: the model summarizes the three task
# results and cites specific row numbers as evidence. Two real failure modes
# were observed in production runs:
#   1. unknown_finding — the verdict cites a finding title that doesn't exist
#      in the audit (pure fabrication).
#   2. row_not_in_finding — the verdict attributes a row to a finding whose
#      affected_rows doesn't include it (e.g. citing row 49 as "missing FSA"
#      when row 49 was actually flagged for an FSA *mismatch*, not missing).
#
# Primary validation uses STRUCTURED citations the prompt now requires — each
# {row, finding} pair is checked deterministically against affected_rows. A
# regex fallback runs on the rationale prose for older saved runs that don't
# include the structured fields.

_ROW_CITATION_RE = re.compile(
    r'\b(?:row|rows|ID|IDs|id|ids)\s+([0-9][0-9,\s]*(?:and\s+[0-9]+)?)',
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r'\d+')

def _extract_row_citation_groups(text):
    groups = []
    for m in _ROW_CITATION_RE.finditer(text or ""):
        nums = [int(n) for n in _NUMBER_RE.findall(m.group(1))]
        if nums:
            groups.append(nums)
    return groups

def _match_finding_title(claim, finding_rows):
    """Match a model-provided finding string to a real audit title.
    Tries exact, case-insensitive, then bidirectional substring."""
    if not claim:
        return None
    if claim in finding_rows:
        return claim
    lower = claim.lower().strip()
    for title in finding_rows:
        if title.lower().strip() == lower:
            return title
    for title in finding_rows:
        tl = title.lower()
        if lower and (lower in tl or tl in lower):
            return title
    return None

def _validate_structured_citations(verdict, finding_rows):
    """Validate explicit {row, finding} citations from the model."""
    warnings = []

    def check(scope, citations):
        for c in citations or []:
            if not isinstance(c, dict):
                continue
            try:
                row = int(c.get("row"))
            except (TypeError, ValueError):
                continue
            claim = (c.get("finding") or "").strip()
            matched = _match_finding_title(claim, finding_rows)
            if matched is None:
                warnings.append({
                    "scope": scope,
                    "type": "unknown_finding",
                    "row": row,
                    "claim": claim,
                    "message": (f"Cites row {row} under finding '{claim}' in **{scope}**, "
                                f"but no such finding exists in the audit."),
                })
            elif row not in finding_rows[matched]:
                actual = sorted(finding_rows[matched])
                warnings.append({
                    "scope": scope,
                    "type": "row_not_in_finding",
                    "row": row,
                    "claim": matched,
                    "actual_rows": actual,
                    "message": (f"Cites row {row} under **{matched}** in {scope}, "
                                f"but that finding actually flags rows {actual}."),
                })

    for t in (verdict.get("tasks") or []):
        check(t.get("name", "task"), t.get("cited_rows"))
    check("bottom line", verdict.get("bottom_line_citations"))
    return warnings

def _validate_prose_citations(verdict, findings_by_row):
    """Regex fallback: scan rationale/bottom_line prose for row references."""
    audited_rows = set(findings_by_row.keys())
    sections = [(t.get("name", "task"), t.get("rationale", "") or "")
                for t in (verdict.get("tasks") or [])]
    sections.append(("bottom line", verdict.get("bottom_line", "") or ""))

    warnings = []
    for label, text in sections:
        for group in _extract_row_citation_groups(text):
            unaudited = [r for r in group if r not in audited_rows]
            if unaudited:
                warnings.append({
                    "scope": label,
                    "type": "unaudited_row",
                    "rows": unaudited,
                    "message": f"Cites row(s) {unaudited} in **{label}**, but no quality finding flags these rows.",
                })

            audited_in_group = [r for r in group if r in audited_rows]
            if len(group) > 1 and audited_in_group:
                shared = set(findings_by_row[audited_in_group[0]])
                for r in audited_in_group[1:]:
                    shared &= findings_by_row[r]
                if not shared:
                    row_to_findings = {r: sorted(findings_by_row.get(r, set())) for r in group}
                    warnings.append({
                        "scope": label,
                        "type": "cross_finding_mashup",
                        "rows": group,
                        "row_to_findings": row_to_findings,
                        "message": f"Groups rows {group} in **{label}**, but these rows belong to different findings.",
                    })
    return warnings

def _validate_triage_items(verdict, finding_rows, num_records):
    """Validate the triage-queue schema: each item carries affected_rows and a
    finding title (or null). For items tied to a finding, every affected row
    must appear in that finding's audit. For items with finding=null (e.g. bulk
    address standardization), we still check that rows are real record IDs."""
    warnings = []
    for item in (verdict.get("items") or []):
        if not isinstance(item, dict):
            continue
        title = item.get("title", "(untitled item)")
        affected = []
        for rid in (item.get("affected_rows") or []):
            try:
                affected.append(int(rid))
            except (TypeError, ValueError):
                continue
        if not affected:
            continue

        finding_claim = item.get("finding")
        if finding_claim is None or finding_claim == "":
            out_of_range = [r for r in affected if r < 1 or r > num_records]
            if out_of_range:
                warnings.append({
                    "scope": title,
                    "type": "row_out_of_range",
                    "rows": out_of_range,
                    "message": (f"Item **{title}** lists rows {out_of_range} that are "
                                f"outside the {num_records}-record dataset."),
                })
            continue

        matched = _match_finding_title(finding_claim, finding_rows)
        if matched is None:
            warnings.append({
                "scope": title,
                "type": "unknown_finding",
                "claim": finding_claim,
                "message": (f"Item **{title}** references finding '{finding_claim}', "
                            f"but no such finding exists in the audit."),
            })
            continue

        bad_rows = [r for r in affected if r not in finding_rows[matched]]
        if bad_rows:
            actual = sorted(finding_rows[matched])
            warnings.append({
                "scope": title,
                "type": "row_not_in_finding",
                "rows": bad_rows,
                "claim": matched,
                "actual_rows": actual,
                "message": (f"Item **{title}** includes rows {bad_rows} under "
                            f"**{matched}**, but that finding actually flags rows {actual}."),
            })
    return warnings

def validate_verdict_citations(verdict, results, num_records=None):
    if not isinstance(verdict, dict):
        return []

    finding_rows = {}      # title -> set of affected row IDs
    findings_by_row = {}   # row ID -> set of finding titles
    for q in (results.get("quality") or []):
        title = q.get("title") or "(untitled finding)"
        rows = set()
        for rid in (q.get("affected_rows") or []):
            try:
                rows.add(int(rid))
            except (TypeError, ValueError):
                continue
        finding_rows[title] = rows
        for r in rows:
            findings_by_row.setdefault(r, set()).add(title)

    # Schema dispatch: new triage queue uses items[]; older runs used tasks[].
    if verdict.get("items"):
        n = num_records if num_records is not None else 10**9
        return _validate_triage_items(verdict, finding_rows, n)

    has_structured = any(t.get("cited_rows") for t in (verdict.get("tasks") or [])) \
                     or bool(verdict.get("bottom_line_citations"))
    if has_structured:
        return _validate_structured_citations(verdict, finding_rows)
    return _validate_prose_citations(verdict, findings_by_row)

def run_quality_task(client, records, model_name):
    city = detect_city(records)

    if city == "toronto":
        batch = [{"id": i+1, "address": r.get("address", ""), "fsa": r.get("zipcode", ""),
                  "ward": r.get("ward", ""), "property_type": r.get("bldgclass", ""),
                  "yearbuilt": r.get("yearbuilt", ""), "year_registered": r.get("year_registered", ""),
                  "confirmed_units": r.get("confirmed_units", ""), "no_of_units": r.get("no_of_units", ""),
                  "confirmed_storeys": r.get("confirmed_storeys", ""), "no_of_storeys": r.get("no_of_storeys", ""),
                  "heating_type": r.get("heating_type", ""), "management_company": r.get("ownername", "")}
                 for i, r in enumerate(records)]
    elif city == "vancouver":
        batch = [{"id": i+1, "address": r.get("address", ""), "postal_code": r.get("zipcode", ""),
                  "zoning": r.get("bldgclass", ""), "zoning_district": r.get("zoning_district", ""),
                  "legal_type": r.get("legal_type", ""), "yearbuilt": r.get("yearbuilt", ""),
                  "land_value": r.get("current_land_value", ""),
                  "improvement_value": r.get("current_improvement_value", ""),
                  "tax_levy": r.get("tax_levy", ""),
                  "legal_description": r.get("narrative_legal", "")}
                 for i, r in enumerate(records)]
    else:
        batch = [{"id": i+1, "address": r.get("address", ""), "zipcode": r.get("zipcode", ""),
                  "ownername": r.get("ownername", ""), "bldgclass": r.get("bldgclass", ""),
                  "yearbuilt": r.get("yearbuilt", ""), "unitsres": r.get("unitsres", ""),
                  "unitstotal": r.get("unitstotal", ""), "assesstot": r.get("assesstot", "")}
                 for i, r in enumerate(records)]

    city_label = {"vancouver": "Vancouver", "toronto": "Toronto"}.get(city, "NYC")
    prompt = f"""Audit these {city_label} property records for ACTIONABLE data quality issues only.

Focus on issues that would cause real problems in a production data pipeline:
- Missing or null values in critical fields (address, assessment, year built)
- Probable data entry errors (e.g. yearbuilt=0 or yearbuilt in the future)
- Contradictions between fields (e.g. residential zoning with commercial use, assessed value inconsistent with property type)
- Statistical outliers that suggest bad data (e.g. a value 10x higher/lower than similar properties in the same area)
- {"Postal code vs address location mismatches" if city in ("vancouver", "toronto") else "ZIP code vs borough mismatches"}
- {"Confirmed vs declared unit/storey count divergence (CONFIRMED_UNITS vs NO_OF_UNITS, CONFIRMED_STOREYS vs NO_OF_STOREYS)" if city == "toronto" else ""}
- {"Assessment value anomalies relative to property type and neighbourhood" if city == "vancouver" else ""}
- Duplicate address detection: when multiple records share the same street address, analyze whether they are:
  - **Legitimate**: separate strata/condo units in the same building (different lot numbers, different assessed values, same year built) — report as low severity with an explanation
  - **Suspicious**: truly duplicate records with identical or near-identical data across all fields — report as high severity
  - **Contradictory**: same address but conflicting info that can't be explained by separate units (e.g. different year built, different zoning) — report as high severity

DO NOT flag:
- Truncated legal descriptions or text formatting issues — these are normal in government data exports
- ALL CAPS text — this is standard formatting, not an error
- Word-break artifacts or spacing in text fields
- Minor formatting inconsistencies that don't affect data usability
- Issues affecting the majority of records — that's a dataset characteristic, not a quality issue

Only report issues where a specific record has data that is likely WRONG, not just messy.

Records:
{json.dumps(batch, indent=2)}

Return JSON array of issues found:
[{{"severity": "high|medium|low", "title": "Short description", "description": "Detailed explanation", "affected_rows": [1,2,3]}}]"""

    raw = call_claude(client, prompt, model_name, max_tokens=8192, label="Quality audit")
    return parse_json_response(raw)

# ── Pipeline Merge / Reconciliation ──
# Demonstrates the integration-day problem: merging a newly-acquired data feed
# (Source B) with an existing pipeline (Source A) when the same physical
# properties appear with field-level inconsistencies. This is the Constellation-
# ID problem in miniature — entity resolution under realistic upstream drift.

_ABBREV_SWAPS = {
    "ST": "STREET", "STREET": "ST",
    "AVE": "AVENUE", "AVENUE": "AVE",
    "BLVD": "BOULEVARD", "BOULEVARD": "BLVD",
    "RD": "ROAD", "ROAD": "RD",
    "DR": "DRIVE", "DRIVE": "DR",
    "PL": "PLACE", "PLACE": "PL",
    "CT": "COURT", "COURT": "CT",
    "LN": "LANE", "LANE": "LN",
    "CRES": "CRESCENT", "CRESCENT": "CRES",
}

def _perturb_address(addr, rng):
    if not addr:
        return addr
    parts = str(addr).upper().split()
    for i, p in enumerate(parts):
        if p in _ABBREV_SWAPS and rng.random() < 0.5:
            parts[i] = _ABBREV_SWAPS[p]
            break
    out = " ".join(parts)
    r = rng.random()
    if r < 0.35:
        out = out.title()
    # 4% chance of single-letter transposition typo
    if rng.random() < 0.04:
        letters = [i for i, c in enumerate(out) if c.isalpha() and i + 1 < len(out) and out[i+1].isalpha()]
        if letters:
            i = rng.choice(letters)
            out = out[:i] + out[i+1] + out[i] + out[i+2:]
    return out

def _perturb_year(y, rng):
    try:
        n = int(str(y).strip())
    except (TypeError, ValueError):
        return y
    if n < 1700 or n > 2030:
        return y
    r = rng.random()
    if r < 0.75:
        return str(n)
    if r < 0.92:
        return str(n + rng.choice([-1, 1]))
    return str(n + rng.choice([-10, -5, 5, 10]))

def _perturb_name(name, rng):
    if not name:
        return name
    out = str(name)
    r = rng.random()
    if r < 0.25:
        out = out.title()
    elif r < 0.45:
        out = out.lower()
    if "LLC" in out.upper() and rng.random() < 0.25:
        out = re.sub(r"\bLLC\b", "llc", out, flags=re.IGNORECASE)
    return out

def synthesize_source_b(records, seed=42):
    """Deterministically generate a synthetic 'Source B' from Source A records,
    simulating an acquired-feed scenario with realistic field-level drift.

    Returns (source_b_records, dropped_a_indices, novel_b_ids). The hidden
    `_b_source_a_id` on each B record is ground truth for scoring; it is NOT
    sent to the LLM at reconciliation time."""
    rng = random.Random(seed)
    n = len(records)
    if n == 0:
        return [], [], []

    indices = list(range(n))
    rng.shuffle(indices)
    drop_count = max(1, int(round(n * 0.15)))
    dropped = set(indices[:drop_count])

    source_b = []
    next_id = 1

    for i, r in enumerate(records):
        if i in dropped:
            continue
        new_rec = dict(r)
        if "address" in new_rec:
            new_rec["address"] = _perturb_address(new_rec.get("address", ""), rng)
        if "ownername" in new_rec:
            new_rec["ownername"] = _perturb_name(new_rec.get("ownername", ""), rng)
        if "yearbuilt" in new_rec:
            new_rec["yearbuilt"] = _perturb_year(new_rec.get("yearbuilt", ""), rng)
        if "zipcode" in new_rec and rng.random() < 0.08:
            new_rec["zipcode"] = ""
        new_rec["_b_source_a_id"] = i + 1  # ground truth, kept hidden from LLM
        new_rec["_b_id"] = next_id
        next_id += 1
        source_b.append(new_rec)

    # ~15% novel B-orphans: plausible-looking records that don't exist in A
    add_count = max(1, int(round(n * 0.15)))
    novel_ids = []
    for _ in range(add_count):
        template = dict(rng.choice(records))
        addr = template.get("address", "")
        if addr:
            parts = str(addr).split()
            if parts and parts[0].isdigit():
                try:
                    bump = rng.choice([-300, -200, -100, 100, 200, 300, 500])
                    new_num = max(1, int(parts[0]) + bump)
                    parts[0] = str(new_num)
                    template["address"] = " ".join(parts)
                except (ValueError, TypeError):
                    pass
        # Also perturb the owner so it doesn't look like a known entity
        if "ownername" in template:
            template["ownername"] = _perturb_name(template.get("ownername", ""), rng)
        template["_b_source_a_id"] = None
        template["_b_id"] = next_id
        novel_ids.append(next_id)
        next_id += 1
        source_b.append(template)

    rng.shuffle(source_b)
    return source_b, sorted(dropped), novel_ids

def _trim_for_reconciliation(records, is_b=False):
    """Project to essential reconciliation fields. Strip ground-truth markers."""
    essential = ["address", "zipcode", "ownername", "bldgclass", "yearbuilt", "ward"]
    out = []
    for i, r in enumerate(records):
        rid = r.get("_b_id") if is_b else i + 1
        item = {"id": rid}
        for k in essential:
            v = r.get(k)
            if v not in (None, ""):
                item[k] = v
        out.append(item)
    return out

def run_reconciliation_task(client, records_a, records_b, model_name):
    """LLM-driven entity resolution between two record sets. Uses a compact
    output schema (tuple-style conflicts) to keep output well under max_tokens
    even at 50+ matched pairs. Retries once on empty response."""
    a_trim = _trim_for_reconciliation(records_a, is_b=False)
    b_trim = _trim_for_reconciliation(records_b, is_b=True)

    prompt = f"""You are reconciling two property record sets from independent upstream sources during a data-pipeline merge.
Source A is the canonical pipeline. Source B is a newly-acquired feed that describes many of the same physical properties but may use different formatting, contain typos, or drop/add records.

For each Source B record, decide:
1. Does it match a Source A record (same underlying physical property)? If yes, which A id, and what fields conflict?
2. If no match — it's a B-orphan (new property not in A).
For each A record not matched by any B record: it's an A-orphan.

Tolerate realistic upstream drift:
- Address abbreviations (St↔Street, Ave↔Avenue, Rd↔Road) and casing
- Owner-name format variations and single-character typos
- yearbuilt ±1 (data-entry variation) — same property
- Missing zipcode on one side

Conflict severity:
- "trivial": cosmetic only (case, abbreviations) → auto-merge
- "minor": small disagreement, probably same value → verify
- "major": factually contradictory → hold

Source A ({len(a_trim)} records):
{json.dumps(a_trim)}

Source B ({len(b_trim)} records):
{json.dumps(b_trim)}

Return JSON. Use this COMPACT schema where each conflict is a 4-tuple [field, a_value, b_value, severity]:
{{
  "matched_pairs": [
    {{"a_id": 1, "b_id": 17, "confidence": "high", "conflicts": [["yearbuilt", "1915", "1914", "minor"]]}}
  ],
  "a_orphans": [3, 8],
  "b_orphans": [22, 31],
  "summary": "2-3 sentences — match rate, conflict distribution, what needs human attention."
}}"""

    # Generous output budget: ~16K tokens accommodates 50+ matched pairs with
    # conflict tuples plus orphans plus prose summary.
    raw = call_claude(client, prompt, model_name, max_tokens=16384, label="Pipeline merge / reconciliation")
    text = (raw or "").strip()
    if not text:
        # Empty response — retry once. Models occasionally emit nothing on
        # complex structured-output tasks; a second attempt usually succeeds.
        raw = call_claude(client, prompt, model_name, max_tokens=16384, label="Pipeline merge / reconciliation (retry)")
        text = (raw or "").strip()
        if not text:
            raise ValueError("Reconciliation returned empty output twice in a row — try fewer records or switch to Haiku.")

    parsed = parse_json_response(text)

    # Normalize compact tuple-conflicts back to dict form for downstream consumers
    for pair in (parsed.get("matched_pairs") or []):
        normalized = []
        for c in (pair.get("conflicts") or []):
            if isinstance(c, list) and len(c) >= 4:
                normalized.append({
                    "field": c[0], "a_value": c[1], "b_value": c[2], "severity": c[3]
                })
            elif isinstance(c, dict):
                normalized.append(c)
        pair["conflicts"] = normalized
    return parsed

def score_reconciliation(reconciliation, source_b):
    """Score the LLM's reconciliation against the hidden ground truth in source_b.
    Returns dict with correct_matches, false_matches, missed_matches, orphan_recall."""
    if not isinstance(reconciliation, dict):
        return None
    b_to_truth = {r.get("_b_id"): r.get("_b_source_a_id") for r in source_b}
    true_orphan_b_ids = {bid for bid, src in b_to_truth.items() if src is None}

    correct = 0
    wrong = 0
    pairs = reconciliation.get("matched_pairs") or []
    matched_b_in_pred = set()
    for p in pairs:
        if not isinstance(p, dict):
            continue
        try:
            a_id = int(p.get("a_id"))
            b_id = int(p.get("b_id"))
        except (TypeError, ValueError):
            continue
        matched_b_in_pred.add(b_id)
        truth = b_to_truth.get(b_id)
        if truth == a_id:
            correct += 1
        else:
            wrong += 1

    # Predicted orphans
    pred_b_orphans = set()
    for x in (reconciliation.get("b_orphans") or []):
        try:
            pred_b_orphans.add(int(x))
        except (TypeError, ValueError):
            continue
    true_b_orphans_caught = len(pred_b_orphans & true_orphan_b_ids)

    total_real_matches = sum(1 for src in b_to_truth.values() if src is not None)
    return {
        "correct_matches": correct,
        "wrong_matches": wrong,
        "total_real_matches": total_real_matches,
        "match_recall_pct": (correct / total_real_matches * 100) if total_real_matches else 0,
        "b_orphans_caught": true_b_orphans_caught,
        "total_real_b_orphans": len(true_orphan_b_ids),
        "b_orphan_recall_pct": (true_b_orphans_caught / len(true_orphan_b_ids) * 100) if true_orphan_b_ids else 0,
    }

# ── Share/export helpers ──
def export_json(data, results, city, model_name):
    payload = {
        "version": "1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "city": city,
        "model": model_name,
        "data": data,
        "results": results,
    }
    return json.dumps(payload, indent=2, default=str)

def _esc(v):
    return html.escape(str(v)) if v is not None else ""

def generate_html_report(data, results, city, model_name):
    city_label = {"vancouver": "Vancouver", "toronto": "Toronto"}.get(city, "NYC")
    timestamp = datetime.now().strftime("%B %-d, %Y at %-I:%M %p")
    record_count = len(data)

    # Cost numbers driven by real token usage from the run.
    ai_headline, cost_footnote = unit_economics_strings(results, model_name, record_count)

    # Verdict block — triage queue (items[]) or backwards-compat tasks[]
    verdict_html = ""
    verdict = results.get("verdict")
    if verdict and isinstance(verdict, dict):
        items = verdict.get("items", [])
        if items:
            disposition_styles = {
                "auto_apply": ("🟢 Auto-apply", "border-emerald-300 bg-emerald-50", "bg-emerald-100 text-emerald-800"),
                "verify":     ("🟡 Verify",     "border-amber-300 bg-amber-50",     "bg-amber-100 text-amber-800"),
                "hold":       ("🔴 Hold",       "border-red-300 bg-red-50",         "bg-red-100 text-red-800"),
                "ignore":     ("⚪ Ignore",     "border-gray-300 bg-gray-50",       "bg-gray-100 text-gray-700"),
            }
            counts = {"auto_apply": 0, "verify": 0, "hold": 0, "ignore": 0}
            for it in items:
                d = it.get("disposition")
                if d in counts:
                    counts[d] += 1

            cards_html = ""
            for disp in ("auto_apply", "verify", "hold", "ignore"):
                matching = [it for it in items if it.get("disposition") == disp]
                if not matching:
                    continue
                label, border_class, badge_class = disposition_styles[disp]
                section_html = f'<h3 class="text-sm font-bold uppercase tracking-wider text-gray-600 mt-5 mb-2">{_esc(label)} <span class="text-gray-400 font-normal">({len(matching)})</span></h3>'
                for it in matching:
                    rows = it.get("affected_rows") or []
                    finding_name = it.get("finding")
                    finding_line = f' • finding: {_esc(finding_name)}' if finding_name else ' • bulk task'
                    section_html += f"""
                    <div class="border {border_class} rounded-lg p-4 mb-3">
                      <div class="flex items-start justify-between mb-2">
                        <p class="font-semibold text-sm text-gray-900">{_esc(it.get('title', ''))}</p>
                        <span class="text-xs px-2 py-0.5 rounded {badge_class} ml-3 whitespace-nowrap">{_esc(label)}</span>
                      </div>
                      <p class="text-sm text-gray-700 mb-1"><em>Proposed fix:</em> {_esc(it.get('proposed_fix', ''))}</p>
                      <p class="text-xs text-gray-500 mb-2">{_esc(it.get('why_disposition', ''))}</p>
                      <p class="text-xs text-gray-400">{len(rows)} affected row(s){finding_line}</p>
                    </div>"""
                cards_html += section_html

            metrics_html = f"""
            <div class="grid grid-cols-4 gap-3 mb-4">
              <div class="border border-emerald-200 bg-emerald-50 rounded-lg p-3 text-center"><div class="text-2xl font-bold text-emerald-700">{counts['auto_apply']}</div><div class="text-xs text-emerald-700 uppercase tracking-wider">Auto-apply</div></div>
              <div class="border border-amber-200 bg-amber-50 rounded-lg p-3 text-center"><div class="text-2xl font-bold text-amber-700">{counts['verify']}</div><div class="text-xs text-amber-700 uppercase tracking-wider">Verify</div></div>
              <div class="border border-red-200 bg-red-50 rounded-lg p-3 text-center"><div class="text-2xl font-bold text-red-700">{counts['hold']}</div><div class="text-xs text-red-700 uppercase tracking-wider">Hold</div></div>
              <div class="border border-gray-200 bg-gray-50 rounded-lg p-3 text-center"><div class="text-2xl font-bold text-gray-700">{counts['ignore']}</div><div class="text-xs text-gray-700 uppercase tracking-wider">Ignore</div></div>
            </div>"""

            summary_text = _esc(verdict.get("summary", "") or verdict.get("bottom_line", ""))
            summary_html = f"""
            <div class="bg-blue-50 border border-blue-200 rounded-lg p-5 mt-4">
              <p class="text-sm text-blue-900 leading-relaxed"><strong>Summary:</strong> {summary_text}</p>
            </div>""" if summary_text else ""

            verdict_html = metrics_html + cards_html + summary_html
        else:
            tasks_rows = ""
            for t in verdict.get("tasks", []):
                tasks_rows += f"""
                <tr>
                  <td class="px-4 py-3 font-semibold text-sm text-gray-900 align-top">{_esc(t.get('name', ''))}</td>
                  <td class="px-4 py-3 text-sm text-gray-700 align-top">{_esc(t.get('recommendation', ''))}</td>
                  <td class="px-4 py-3 text-sm text-gray-600 align-top leading-relaxed">{_esc(t.get('rationale', ''))}</td>
                </tr>"""
            bottom_line = _esc(verdict.get("bottom_line", ""))
            verdict_html = f"""
            <div class="bg-white rounded-lg border overflow-hidden mb-4">
              <table class="w-full">
                <thead class="bg-gray-50 text-left text-xs text-gray-500 uppercase tracking-wider">
                  <tr><th class="px-4 py-3">Task</th><th class="px-4 py-3">Recommendation</th><th class="px-4 py-3">Rationale</th></tr>
                </thead>
                <tbody class="divide-y divide-gray-100">{tasks_rows}</tbody>
              </table>
            </div>
            <div class="bg-blue-50 border border-blue-200 rounded-lg p-5 mb-6">
              <p class="text-sm text-blue-900 leading-relaxed"><strong>Bottom line:</strong> {bottom_line}</p>
            </div>"""

    # Quality issues
    quality_html = ""
    if results.get("quality"):
        for issue in results["quality"]:
            severity = issue.get("severity", "low")
            color = {"high": "border-red-300 bg-red-50", "medium": "border-amber-300 bg-amber-50"}.get(severity, "border-blue-200 bg-blue-50")
            badge = {"high": "bg-red-100 text-red-700", "medium": "bg-amber-100 text-amber-700"}.get(severity, "bg-blue-100 text-blue-700")
            quality_html += f"""
            <div class="border rounded-lg p-4 mb-3 {color}">
              <div class="flex justify-between items-start gap-3">
                <h4 class="font-semibold text-sm text-gray-900">{_esc(issue.get('title', 'Issue'))}</h4>
                <span class="text-xs font-medium px-2 py-0.5 rounded-full {badge} whitespace-nowrap">{_esc(severity)}</span>
              </div>
              <p class="text-sm text-gray-700 mt-2 leading-relaxed">{_esc(issue.get('description', ''))}</p>
            </div>"""

    # Classifications
    classifications_html = ""
    class_data = results.get("classifications", {})
    if class_data:
        nested = any(isinstance(v, dict) for v in class_data.values())
        if nested:
            for category, mappings in class_data.items():
                if not isinstance(mappings, dict):
                    continue
                rows = "".join(
                    f"""<tr><td class="px-4 py-2 font-semibold text-gray-700 text-sm align-top w-1/4">{_esc(k)}</td><td class="px-4 py-2 text-sm text-gray-600 align-top leading-relaxed">{_esc(v)}</td></tr>"""
                    for k, v in mappings.items()
                )
                classifications_html += f"""
                <div class="mb-5">
                  <h4 class="font-semibold text-gray-700 mb-2 text-xs uppercase tracking-wider">{_esc(category.replace('_', ' ').title())}</h4>
                  <div class="bg-white rounded-lg border overflow-hidden">
                    <table class="w-full"><tbody class="divide-y divide-gray-100">{rows}</tbody></table>
                  </div>
                </div>"""
        else:
            rows = "".join(
                f"""<tr><td class="px-4 py-2 font-semibold text-gray-700 text-sm align-top w-1/6">{_esc(k)}</td><td class="px-4 py-2 text-sm text-gray-600 align-top leading-relaxed">{_esc(v)}</td></tr>"""
                for k, v in class_data.items()
            )
            classifications_html = f"""
            <div class="bg-white rounded-lg border overflow-hidden">
              <table class="w-full"><tbody class="divide-y divide-gray-100">{rows}</tbody></table>
            </div>"""

    # Addresses
    addr_rows_html = ""
    addr_data = results.get("addresses", {})
    if addr_data:
        for i, r in enumerate(data):
            entry = addr_data.get(i + 1, {})
            if isinstance(entry, str):
                entry = {"standardized": entry, "changes": []}
            raw = r.get("address", "")
            standardized = entry.get("standardized", "—")
            changes = "; ".join(entry.get("changes", [])) or "—"
            addr_rows_html += f"""
            <tr>
              <td class="px-3 py-2 text-xs text-gray-400 font-mono">{i+1}</td>
              <td class="px-3 py-2 text-xs font-mono text-gray-700">{_esc(raw)}</td>
              <td class="px-3 py-2 text-xs font-mono text-gray-900">{_esc(standardized)}</td>
              <td class="px-3 py-2 text-xs text-gray-500">{_esc(changes)}</td>
            </tr>"""

    addresses_html = f"""
    <div class="bg-white rounded-lg border overflow-x-auto">
      <table class="w-full">
        <thead class="bg-gray-50 text-left text-xs text-gray-500 uppercase tracking-wider">
          <tr>
            <th class="px-3 py-3 w-8">#</th>
            <th class="px-3 py-3">Raw</th>
            <th class="px-3 py-3">Standardized</th>
            <th class="px-3 py-3">Changes</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-100">{addr_rows_html}</tbody>
      </table>
    </div>""" if addr_rows_html else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Task Grader — {_esc(city_label)} Report</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');
  body {{ font-family: 'Inter', sans-serif; }}
  .hero-num {{ font-variant-numeric: tabular-nums; letter-spacing: -0.02em; }}
  .cost-card {{ background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); }}
</style>
</head>
<body class="bg-gray-50 text-gray-800">
<div class="max-w-5xl mx-auto px-6 py-10">

  <!-- Header -->
  <div class="mb-8">
    <div class="flex items-center gap-3 mb-2">
      <h1 class="text-3xl font-bold text-gray-900 tracking-tight">AI Task Grader Report</h1>
      <span class="text-xs font-semibold bg-blue-100 text-blue-700 px-2 py-0.5 rounded-full">{_esc(city_label.upper())}</span>
    </div>
    <p class="text-gray-600 text-base">Evaluation of {record_count} real {_esc(city_label)} property records — task by task, backed by numbers.</p>
    <div class="flex flex-wrap gap-x-6 gap-y-2 mt-4 text-sm text-gray-500">
      <span><strong class="text-gray-700">Generated:</strong> {_esc(timestamp)}</span>
      <span><strong class="text-gray-700">Model:</strong> {_esc(model_name)}</span>
      <span><strong class="text-gray-700">Records:</strong> {record_count}</span>
    </div>
  </div>

  <!-- Triage Queue -->
  <section class="mb-8">
    <h2 class="text-xl font-bold text-gray-900 mb-3">Triage Queue</h2>
    {verdict_html or '<p class="text-gray-500 text-sm">No verdict generated.</p>'}
  </section>

  <!-- Cost block -->
  <section class="cost-card text-white rounded-xl p-8 mb-10 shadow-lg">
    <div class="flex items-baseline justify-between mb-1">
      <div class="text-xs font-bold uppercase tracking-wider text-blue-300">Unit economics at scale</div>
      <div class="text-xs text-gray-400">Projected to 1,000,000 records</div>
    </div>
    <p class="text-gray-300 text-sm mt-2 mb-6 max-w-2xl">
      Accuracy means nothing without unit economics. Here's what cleanup costs at production volume — AI vs. the labor alternative.
    </p>
    <div class="grid grid-cols-3 gap-6">
      <div>
        <div class="text-xs uppercase tracking-wider text-blue-300 font-semibold">AI pipeline</div>
        <div class="hero-num text-4xl font-bold mt-2">{_esc(ai_headline)}</div>
        <div class="text-sm text-gray-400 mt-1">API spend, batched</div>
        <div class="text-xs text-gray-500 mt-2">8–12 hours wall time</div>
      </div>
      <div>
        <div class="text-xs uppercase tracking-wider text-amber-300 font-semibold">Offshore BPO</div>
        <div class="hero-num text-4xl font-bold mt-2">~$10K</div>
        <div class="text-sm text-gray-400 mt-1">Per-record outsourced</div>
        <div class="text-xs text-gray-500 mt-2">Weeks of turnaround</div>
      </div>
      <div>
        <div class="text-xs uppercase tracking-wider text-red-300 font-semibold">Domestic analyst</div>
        <div class="hero-num text-4xl font-bold mt-2">~$500K</div>
        <div class="text-sm text-gray-400 mt-1">In-house review</div>
        <div class="text-xs text-gray-500 mt-2">Months at FTE rates</div>
      </div>
    </div>
    <div class="mt-6 pt-5 border-t border-gray-700 text-sm text-gray-300">
      <strong class="text-white">AI is dramatically cheaper than manual</strong> at this volume — but only worth running on items the queue tags as <em>auto-apply</em> or <em>verify</em>.
    </div>
    <div class="mt-3 text-xs text-gray-500 leading-relaxed">{_esc(cost_footnote)}</div>
  </section>

  <!-- Data Quality -->
  <section class="mb-10">
    <h2 class="text-xl font-bold text-gray-900 mb-3">Data Quality Issues</h2>
    {quality_html or '<p class="text-gray-500 text-sm">No quality issues to display.</p>'}
  </section>

  <!-- Classifications -->
  <section class="mb-10">
    <h2 class="text-xl font-bold text-gray-900 mb-3">Classification Decoding</h2>
    {classifications_html or '<p class="text-gray-500 text-sm">No classifications to display.</p>'}
  </section>

  <!-- Addresses -->
  <section class="mb-10">
    <h2 class="text-xl font-bold text-gray-900 mb-3">Address Standardization</h2>
    {addresses_html or '<p class="text-gray-500 text-sm">No address results to display.</p>'}
  </section>

  <footer class="text-center text-xs text-gray-400 mt-12 pt-6 border-t">
    Generated by <strong class="text-gray-600">AI Task Grader</strong> · {_esc(timestamp)}
  </footer>
</div>
</body>
</html>"""

# ── Main ──
if not api_key:
    st.info("Enter your Anthropic API key in the sidebar to get started.")
    st.stop()

data = None

if source == "NYC (PLUTO API)":
    if st.sidebar.button("Fetch Data", type="primary", use_container_width=True):
        with st.spinner(f"Fetching {num_records} records from NYC PLUTO..."):
            try:
                data = fetch_nyc_data(borough, zip_filter, street_filter, num_records)
                st.session_state["data"] = data
                st.session_state["data_source"] = "nyc"
                st.session_state["results"] = None
                st.session_state["_requested_count"] = num_records
            except Exception as e:
                st.error(f"Failed to fetch: {e}")
    if "data" in st.session_state:
        data = st.session_state["data"]

elif source == "Vancouver (Open Data)":
    if st.sidebar.button("Fetch Data", type="primary", use_container_width=True, key="van_fetch"):
        with st.spinner(f"Fetching {num_records} records from Vancouver Open Data..."):
            try:
                data = fetch_vancouver_data(van_neighbourhood, van_street, van_postal, num_records)
                st.session_state["data"] = data
                st.session_state["data_source"] = "vancouver"
                st.session_state["results"] = None
                st.session_state["_requested_count"] = num_records
            except Exception as e:
                st.error(f"Failed to fetch: {e}")
    if "data" in st.session_state:
        data = st.session_state["data"]

elif source == "Toronto (Open Data)":
    if st.sidebar.button("Fetch Data", type="primary", use_container_width=True, key="tor_fetch"):
        with st.spinner(f"Fetching {num_records} records from Toronto Open Data..."):
            try:
                data = fetch_toronto_data(tor_ward, tor_street, tor_fsa, num_records)
                st.session_state["data"] = data
                st.session_state["data_source"] = "toronto"
                st.session_state["results"] = None
                st.session_state["_requested_count"] = num_records
            except Exception as e:
                st.error(f"Failed to fetch: {e}")
    if "data" in st.session_state:
        data = st.session_state["data"]

else:
    if uploaded:
        _csv_source_map = {"NYC": "nyc", "Vancouver": "vancouver", "Toronto": "toronto"}
        source_tag = _csv_source_map.get(csv_city)  # None for "Auto-detect"
        data = parse_csv_upload(uploaded, source_tag=source_tag)
        st.session_state["data"] = data
        st.session_state["data_source"] = source_tag or "csv"
        st.session_state["results"] = None
    elif "data" in st.session_state:
        data = st.session_state["data"]

if data:
    src_label = st.session_state.get("data_source", "unknown").upper()
    st.success(f"**{len(data)} records loaded** from {src_label}")
    _requested = st.session_state.get("_requested_count")
    if _requested and len(data) < _requested:
        st.warning(
            f"You requested {_requested} records but the source returned only {len(data)} "
            f"after filtering. The dataset may not have enough rows matching your filters."
        )

    with st.expander("Preview raw data", expanded=False):
        city = detect_city(data)
        if city == "vancouver":
            preview_cols = ["address", "zipcode", "legal_type", "bldgclass", "yearbuilt", "current_land_value"]
        elif city == "toronto":
            preview_cols = ["address", "zipcode", "ward", "bldgclass", "yearbuilt", "confirmed_units", "no_of_units"]
        else:
            preview_cols = ["address", "zipcode", "ownername", "bldgclass", "yearbuilt"]
        rows = []
        for r in data[:20]:
            rows.append({c: r.get(c, "") for c in preview_cols})
        st.table(rows)

    if st.button("Run AI Evaluation", type="primary", use_container_width=True):
        client = anthropic.Anthropic(api_key=api_key)
        # Reset per-run token accounting and truncation warnings so cost & status
        # reflect this run only.
        st.session_state["_usage"] = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
        st.session_state["_warnings"] = []
        results = {}
        col1, col2, col3 = st.columns(3)

        with col1:
            with st.status("Task 1: Address Standardization...", expanded=True) as s:
                t0 = time.time()
                try:
                    addr_results = run_address_task(client, data, model)
                    elapsed1 = time.time() - t0
                    results["addresses"] = addr_results
                    s.update(label=f"Addresses done ({elapsed1:.1f}s)", state="complete", expanded=True)
                    st.metric("Records processed", len(addr_results))
                except Exception as e:
                    s.update(label="Address task failed", state="error")
                    st.error(str(e))

        with col2:
            with st.status("Task 2: Classification...", expanded=True) as s:
                t0 = time.time()
                try:
                    class_results = run_classification_task(client, data, model)
                    elapsed2 = time.time() - t0
                    results["classifications"] = class_results
                    s.update(label=f"Classification done ({elapsed2:.1f}s)", state="complete", expanded=True)
                    st.metric("Codes decoded", len(data))
                except Exception as e:
                    s.update(label="Classification failed", state="error")
                    st.error(str(e))

        with col3:
            with st.status("Task 3: Data Quality Audit...", expanded=True) as s:
                t0 = time.time()
                try:
                    quality_results = run_quality_task(client, data, model)
                    elapsed3 = time.time() - t0
                    results["quality"] = quality_results
                    s.update(label=f"Quality audit done ({elapsed3:.1f}s)", state="complete", expanded=True)
                    st.metric("Issues found", len(quality_results))
                except Exception as e:
                    s.update(label="Quality audit failed", state="error")
                    st.error(str(e))

        if results.get("addresses") or results.get("classifications") or results.get("quality"):
            with st.status("Generating verdict...", expanded=True) as s:
                t0 = time.time()
                try:
                    verdict = run_verdict_task(client, results, detect_city(data), len(data), model)
                    elapsed_v = time.time() - t0
                    results["verdict"] = verdict
                    s.update(label=f"Verdict ready ({elapsed_v:.1f}s)", state="complete")
                except Exception as e:
                    s.update(label="Verdict failed", state="error")
                    st.error(str(e))

        # Persist usage and any truncation warnings into results so they round-trip
        # through saved-run JSON and are available to the HTML report generator.
        results["_usage"] = dict(st.session_state.get("_usage", {}))
        results["_warnings"] = list(st.session_state.get("_warnings", []))
        st.session_state["results"] = results

    if st.session_state.get("results"):
        results = st.session_state["results"]
        st.divider()
        city = detect_city(data)

        # Share / export this run
        ts_slug = datetime.now().strftime("%Y%m%d-%H%M")
        share_col1, share_col2, _ = st.columns([1, 1, 2])
        with share_col1:
            st.download_button(
                "⬇ Download JSON",
                data=export_json(data, results, city, model),
                file_name=f"aitaskgrader-{city}-{ts_slug}.json",
                mime="application/json",
                help="Reload later with the 'Load saved run' uploader in the sidebar, or share the file.",
                use_container_width=True,
            )
        with share_col2:
            st.download_button(
                "⬇ Download HTML report",
                data=generate_html_report(data, results, city, model),
                file_name=f"aitaskgrader-{city}-{ts_slug}.html",
                mime="text/html",
                help="A standalone shareable report — opens in any browser, no app required.",
                use_container_width=True,
            )

        run_warnings = results.get("_warnings") or []
        if run_warnings:
            with st.expander(f"⚠️ {len(run_warnings)} truncation warning(s) — results may be incomplete", expanded=True):
                for w in run_warnings:
                    st.markdown(f"- {w}")
                st.caption("Reduce the record count, or chunked tasks will retry next run with smaller batches.")

        # ── Pipeline Merge demo button — simulates merging an acquired data feed ──
        if "reconciliation" not in results:
            with st.expander("🔀 **Pipeline Merge** — simulate acquiring a second data feed", expanded=False):
                st.markdown(
                    "Generates a synthetic *Source B* by perturbing the current records "
                    "(realistic abbreviation swaps, casing variation, single-char typos, ±1 year_built, "
                    "occasional missing zipcodes, plus ~15% dropped records and ~15% novel records). "
                    "Then asks the model to reconcile A vs B — entity matching with conflict triage. "
                    "This is the integration-day problem that an acquired pipeline creates."
                )
                if st.button("Run Pipeline Merge Demo", key="run_merge", use_container_width=True):
                    try:
                        client = anthropic.Anthropic(api_key=api_key)
                        with st.spinner("Synthesizing Source B and reconciling…"):
                            source_b, dropped_a, novel_b = synthesize_source_b(data, seed=42)
                            reconciliation = run_reconciliation_task(client, data, source_b, model)
                            score = score_reconciliation(reconciliation, source_b)
                            results["reconciliation"] = {
                                "source_b": source_b,
                                "result": reconciliation,
                                "score": score,
                                "dropped_a_indices": dropped_a,
                                "novel_b_ids": novel_b,
                            }
                            results["_usage"] = dict(st.session_state.get("_usage", {}))
                            st.session_state["results"] = results
                        st.rerun()
                    except Exception as e:
                        st.error(f"Pipeline merge failed: {type(e).__name__}: {e}")
                        st.caption("If this is the first run after a deploy, try once more — the model occasionally returns empty output on complex structured tasks and the retry path may help.")

        tab1, tab2, tab3, tab4, tab5 = st.tabs(["Addresses", "Classifications", "Data Quality", "Verdict", "Pipeline Merge"])

        with tab1:
            if "addresses" in results:
                st.subheader("Address Standardization Results")
                addr_data = results["addresses"]
                rows = []
                for i, r in enumerate(data):
                    entry = addr_data.get(i+1, {})
                    if isinstance(entry, str):
                        entry = {"standardized": entry, "changes": []}
                    row = {
                        "#": i+1,
                        "Raw": r.get("address", ""),
                        "Standardized": entry.get("standardized", "—"),
                        "Street #": entry.get("street_number", ""),
                        "Street": entry.get("street_name", ""),
                        "Unit": entry.get("unit", ""),
                        "City": entry.get("city", ""),
                    }
                    if city == "vancouver":
                        row["Prov"] = entry.get("province", "")
                        row["Postal"] = entry.get("postal_code", r.get("zipcode", ""))
                    elif city == "toronto":
                        row["Prov"] = entry.get("province", "")
                        row["FSA"] = entry.get("fsa", r.get("zipcode", ""))
                    else:
                        row["State"] = entry.get("state", "")
                        row["ZIP"] = entry.get("zipcode", r.get("zipcode", ""))
                    changes = entry.get("changes", [])
                    row["Changes"] = "; ".join(changes) if changes else "None"
                    rows.append(row)
                st.dataframe(rows, use_container_width=True, height=500)

        with tab2:
            if "classifications" in results:
                class_data = results["classifications"]
                if city in ("vancouver", "toronto"):
                    st.subheader("Property Type / Building Decoding" if city == "toronto" else "Zoning & Property Type Decoding")
                    for category, mappings in class_data.items():
                        if isinstance(mappings, dict):
                            st.markdown(f"**{category.replace('_', ' ').title()}**")
                            rows = [{"Code": k, "Description": v} for k, v in mappings.items()]
                            st.dataframe(rows, use_container_width=True)
                else:
                    st.subheader("Building Class Decoding")
                    rows = []
                    seen = set()
                    for r in data:
                        code = r.get("bldgclass", "")
                        if code and code not in seen:
                            seen.add(code)
                            rows.append({
                                "Code": code,
                                "AI Description": class_data.get(code, "Unknown"),
                                "Example Address": r.get("address", "")
                            })
                    st.dataframe(rows, use_container_width=True)

        with tab3:
            if "quality" in results:
                st.subheader("Data Quality Issues")
                for issue in results["quality"]:
                    severity = issue.get("severity", "low")
                    icon = "🔴" if severity == "high" else "🟡" if severity == "medium" else "🔵"
                    with st.expander(f"{icon} {issue.get('title', 'Issue')}", expanded=severity == "high"):
                        st.write(issue.get("description", ""))
                        affected = issue.get("affected_rows", [])
                        if affected and data:
                            if city == "vancouver":
                                show_cols = ["address", "zipcode", "bldgclass", "zoning_district", "legal_type",
                                             "yearbuilt", "current_land_value", "current_improvement_value",
                                             "tax_levy", "narrative_legal"]
                            elif city == "toronto":
                                show_cols = ["address", "zipcode", "ward", "bldgclass", "yearbuilt",
                                             "year_registered", "confirmed_units", "no_of_units",
                                             "confirmed_storeys", "no_of_storeys", "heating_type", "ownername"]
                            else:
                                show_cols = ["address", "zipcode", "ownername", "bldgclass",
                                             "yearbuilt", "unitsres", "unitstotal", "assesstot"]
                            affected_rows = []
                            for row_id in affected:
                                idx = row_id - 1
                                if 0 <= idx < len(data):
                                    r = data[idx]
                                    row = {"#": row_id}
                                    for c in show_cols:
                                        val = r.get(c, "")
                                        if val != "":
                                            row[c] = val
                                    affected_rows.append(row)
                            if affected_rows:
                                st.dataframe(affected_rows, use_container_width=True, hide_index=True)

        with tab4:
            st.subheader("Triage Queue")
            city_label = {"vancouver": "Vancouver", "toronto": "Toronto"}.get(city, "NYC")
            verdict = results.get("verdict")
            if verdict and isinstance(verdict, dict):
                citation_warnings = validate_verdict_citations(verdict, results, num_records=len(data))
                if citation_warnings:
                    with st.expander(
                        f"⚠️ Citation guardrail flagged {len(citation_warnings)} claim(s) — review before sharing",
                        expanded=True,
                    ):
                        st.caption(
                            "Cross-checks every row number cited in the verdict against the underlying audit "
                            "findings. Catches two real failure modes of LLM synthesis: row IDs attributed to "
                            "the wrong finding, and references to findings that don't exist."
                        )
                        for w in citation_warnings:
                            st.markdown(f"- {w['message']}")
                            r2f = w.get("row_to_findings")
                            if r2f:
                                for r, titles in r2f.items():
                                    label = ", ".join(titles) if titles else "(no finding flags this row)"
                                    st.markdown(f"    - Row {r}: {label}")

                items = verdict.get("items", [])
                if items:
                    st.caption(f"AI scanned {len(data)} {city_label} records and proposed {len(items)} cleanup action(s). "
                               f"Each item is tagged with how much human review it needs before applying.")

                    counts = {"auto_apply": 0, "verify": 0, "hold": 0, "ignore": 0}
                    for it in items:
                        d = it.get("disposition")
                        if d in counts:
                            counts[d] += 1
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("🟢 Auto-apply", counts["auto_apply"])
                    m2.metric("🟡 Verify", counts["verify"])
                    m3.metric("🔴 Hold", counts["hold"])
                    m4.metric("⚪ Ignore", counts["ignore"])

                    disposition_meta = [
                        ("auto_apply", "🟢 Auto-apply", "AI can make these fixes without human review."),
                        ("verify",     "🟡 Verify",     "AI made a judgment call — a human should confirm before applying."),
                        ("hold",       "🔴 Hold",       "Real issue, but no safe automated fix — needs investigation."),
                        ("ignore",     "⚪ Ignore",     "Not actionable in this pipeline."),
                    ]

                    finding_lookup = {(q.get("title") or ""): q for q in (results.get("quality") or [])}

                    for disp, label, blurb in disposition_meta:
                        matching = [it for it in items if it.get("disposition") == disp]
                        if not matching:
                            continue
                        st.markdown(f"### {label}")
                        st.caption(blurb)
                        for it in matching:
                            st.markdown(f"**{it.get('title', '')}**")
                            fix = it.get("proposed_fix", "")
                            if fix:
                                st.markdown(f"*Proposed fix:* {fix}")
                            why = it.get("why_disposition", "")
                            if why:
                                st.caption(why)
                            affected = it.get("affected_rows") or []
                            finding_name = it.get("finding")
                            with st.expander(
                                f"Evidence — {len(affected)} affected row(s)"
                                + (f" • finding: {finding_name}" if finding_name else " • bulk task"),
                                expanded=False,
                            ):
                                if affected:
                                    rows_to_show = []
                                    for idx, rec in enumerate(data):
                                        if (idx + 1) in affected:
                                            row = {"#": idx + 1}
                                            for c in ("address", "zipcode", "ward", "bldgclass", "yearbuilt",
                                                      "year_registered", "confirmed_units", "confirmed_storeys",
                                                      "heating_type", "ownername"):
                                                v = rec.get(c)
                                                if v not in (None, ""):
                                                    row[c] = v
                                            rows_to_show.append(row)
                                    if rows_to_show:
                                        st.dataframe(rows_to_show, use_container_width=True, hide_index=True)
                                if finding_name and finding_name in finding_lookup:
                                    src = finding_lookup[finding_name]
                                    desc = src.get("description", "")
                                    if desc:
                                        st.caption(f"Source finding ({src.get('severity', '?')}): {desc}")
                            st.divider()

                    summary_text = verdict.get("summary", "") or verdict.get("bottom_line", "")
                    if summary_text:
                        st.info(f"**Summary:** {summary_text}")

                else:
                    # Backwards compat: render older saved runs that used the tasks[] schema.
                    tasks = verdict.get("tasks", [])
                    if tasks:
                        st.caption("Loaded a saved run from an earlier schema. Re-run the evaluation to see the triage queue view.")
                        st.markdown(f"**{city_label} evaluation results ({len(data)} records):**")
                        table_md = "| Task | Recommendation | Rationale |\n|------|---------------|----------|\n"
                        for t in tasks:
                            table_md += f"| **{t.get('name', '')}** | {t.get('recommendation', '')} | {t.get('rationale', '')} |\n"
                        st.markdown(table_md)
                    bottom_line = verdict.get("bottom_line", "")
                    if bottom_line:
                        st.info(f"**Bottom line:** {bottom_line}")

                # ── Unit economics card — driven by real token usage from this run ──
                ai_headline, cost_footnote = unit_economics_strings(results, model, len(data))
                st.markdown(
                    f"""
                    <div style="
                        background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
                        border-radius: 14px;
                        padding: 32px;
                        margin-top: 24px;
                        box-shadow: 0 10px 30px -10px rgba(15,23,42,0.4);
                        color: #f8fafc;
                        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
                    ">
                      <div style="display:flex; align-items:baseline; justify-content:space-between; margin-bottom: 6px;">
                        <div style="font-size: 11px; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; color: #93c5fd;">
                          Unit economics at scale
                        </div>
                        <div style="font-size: 12px; color: #94a3b8;">Projected to 1,000,000 records</div>
                      </div>
                      <div style="font-size: 14px; color: #cbd5e1; margin-bottom: 24px; max-width: 640px; line-height: 1.5;">
                        Accuracy means nothing without unit economics. Here's what cleanup costs at production volume — AI vs. the labor alternative.
                      </div>
                      <div style="display:grid; grid-template-columns: repeat(3, 1fr); gap: 24px;">
                        <div>
                          <div style="font-size: 11px; font-weight: 700; letter-spacing: 0.10em; text-transform: uppercase; color: #93c5fd;">AI pipeline</div>
                          <div style="font-size: 38px; font-weight: 800; margin-top: 6px; font-variant-numeric: tabular-nums; letter-spacing: -0.02em; line-height: 1.1;">{ai_headline}</div>
                          <div style="font-size: 13px; color: #94a3b8; margin-top: 4px;">API spend, batched</div>
                          <div style="font-size: 12px; color: #64748b; margin-top: 6px;">8–12 hours wall time</div>
                        </div>
                        <div>
                          <div style="font-size: 11px; font-weight: 700; letter-spacing: 0.10em; text-transform: uppercase; color: #fcd34d;">Offshore BPO</div>
                          <div style="font-size: 38px; font-weight: 800; margin-top: 6px; font-variant-numeric: tabular-nums; letter-spacing: -0.02em; line-height: 1.1;">~$10K</div>
                          <div style="font-size: 13px; color: #94a3b8; margin-top: 4px;">Per-record outsourced</div>
                          <div style="font-size: 12px; color: #64748b; margin-top: 6px;">Weeks of turnaround</div>
                        </div>
                        <div>
                          <div style="font-size: 11px; font-weight: 700; letter-spacing: 0.10em; text-transform: uppercase; color: #fca5a5;">Domestic analyst</div>
                          <div style="font-size: 38px; font-weight: 800; margin-top: 6px; font-variant-numeric: tabular-nums; letter-spacing: -0.02em; line-height: 1.1;">~$500K</div>
                          <div style="font-size: 13px; color: #94a3b8; margin-top: 4px;">In-house review</div>
                          <div style="font-size: 12px; color: #64748b; margin-top: 6px;">Months at FTE rates</div>
                        </div>
                      </div>
                      <div style="margin-top: 24px; padding-top: 18px; border-top: 1px solid #334155; font-size: 14px; color: #cbd5e1; line-height: 1.55;">
                        <strong style="color: #ffffff;">AI is dramatically cheaper than manual</strong> at the same volume —
                        but only worth running on items the queue above tags as <em style="color: #cbd5e1;">auto-apply</em> or <em style="color: #cbd5e1;">verify</em>.
                        That's the decision this harness exists to make.
                      </div>
                      <div style="margin-top: 14px; font-size: 11px; color: #64748b; line-height: 1.5;">
                        {html.escape(cost_footnote)}
                      </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            else:
                st.warning("Verdict not available — run the evaluation to generate one.")

        with tab5:
            st.subheader("Pipeline Merge — Source A vs Source B Reconciliation")
            recon_payload = results.get("reconciliation")
            if not recon_payload:
                st.caption(
                    "Click the **Pipeline Merge** button above the tabs to simulate "
                    "acquiring a second data feed for these properties and reconcile it "
                    "against the current pipeline."
                )
            else:
                recon = recon_payload.get("result") or {}
                source_b = recon_payload.get("source_b") or []
                score = recon_payload.get("score") or {}

                st.caption(
                    f"Source A: {len(data)} records (current pipeline). "
                    f"Source B: {len(source_b)} records (synthesized acquired feed). "
                    f"Seed: 42 — deterministic, reproducible."
                )

                pairs = recon.get("matched_pairs") or []
                a_orphans = recon.get("a_orphans") or []
                b_orphans = recon.get("b_orphans") or []

                # Conflict severity breakdown across all matched pairs
                sev_counts = {"trivial": 0, "minor": 0, "major": 0, "no_conflict": 0}
                for p in pairs:
                    confs = p.get("conflicts") or []
                    if not confs:
                        sev_counts["no_conflict"] += 1
                        continue
                    worst = "trivial"
                    for c in confs:
                        s = (c.get("severity") or "trivial").lower()
                        if s == "major":
                            worst = "major"
                            break
                        if s == "minor" and worst != "major":
                            worst = "minor"
                    sev_counts[worst] += 1

                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Matched pairs", len(pairs))
                m2.metric("A-orphans (in A, not in B)", len(a_orphans))
                m3.metric("B-orphans (in B, not in A)", len(b_orphans))
                if score:
                    m4.metric(
                        "Match accuracy",
                        f"{score.get('correct_matches', 0)}/{score.get('total_real_matches', 0)}",
                        f"{score.get('match_recall_pct', 0):.0f}% recall",
                    )

                # Triage-style disposition mapping
                d1, d2, d3, d4 = st.columns(4)
                d1.metric("🟢 Auto-merge (clean)", sev_counts["no_conflict"])
                d2.metric("🟢 Auto-merge (trivial)", sev_counts["trivial"])
                d3.metric("🟡 Verify (minor)", sev_counts["minor"])
                d4.metric("🔴 Hold (major)", sev_counts["major"])

                summary_text = recon.get("summary", "")
                if summary_text:
                    st.info(f"**Summary:** {summary_text}")

                if score:
                    st.caption(
                        f"Ground-truth scoring: {score.get('correct_matches', 0)} correct of "
                        f"{score.get('total_real_matches', 0)} real matches "
                        f"({score.get('match_recall_pct', 0):.0f}% recall, "
                        f"{score.get('wrong_matches', 0)} wrong matches). "
                        f"B-orphan detection: {score.get('b_orphans_caught', 0)} of "
                        f"{score.get('total_real_b_orphans', 0)} "
                        f"({score.get('b_orphan_recall_pct', 0):.0f}% recall)."
                    )

                # Build A and B lookup tables
                a_by_id = {i + 1: r for i, r in enumerate(data)}
                b_by_id = {r.get("_b_id"): r for r in source_b}

                with st.expander(f"🟢 Auto-merge candidates ({sev_counts['no_conflict'] + sev_counts['trivial']})", expanded=False):
                    rows = []
                    for p in pairs:
                        confs = p.get("conflicts") or []
                        worst = "no_conflict" if not confs else min((c.get("severity", "trivial") for c in confs), key=lambda s: 0 if s == "trivial" else (1 if s == "minor" else 2))
                        if confs and any((c.get("severity", "trivial") in ("minor", "major")) for c in confs):
                            continue
                        a_id, b_id = p.get("a_id"), p.get("b_id")
                        a_rec = a_by_id.get(a_id, {})
                        b_rec = b_by_id.get(b_id, {})
                        rows.append({
                            "A id": a_id, "B id": b_id,
                            "A address": a_rec.get("address", ""),
                            "B address": b_rec.get("address", ""),
                            "conflicts": "; ".join(
                                f"{c.get('field')}: '{c.get('a_value')}'→'{c.get('b_value')}'"
                                for c in confs
                            ) or "—",
                        })
                    if rows:
                        st.dataframe(rows, use_container_width=True, hide_index=True)
                    else:
                        st.caption("No auto-merge candidates.")

                with st.expander(f"🟡 Verify (minor conflicts) ({sev_counts['minor']})", expanded=True):
                    rows = []
                    for p in pairs:
                        confs = p.get("conflicts") or []
                        if not confs:
                            continue
                        worst = "trivial"
                        for c in confs:
                            s = (c.get("severity") or "trivial").lower()
                            if s == "major":
                                worst = "major"
                                break
                            if s == "minor" and worst != "major":
                                worst = "minor"
                        if worst != "minor":
                            continue
                        a_id, b_id = p.get("a_id"), p.get("b_id")
                        a_rec = a_by_id.get(a_id, {})
                        b_rec = b_by_id.get(b_id, {})
                        rows.append({
                            "A id": a_id, "B id": b_id,
                            "A address": a_rec.get("address", ""),
                            "B address": b_rec.get("address", ""),
                            "conflicts": "; ".join(
                                f"{c.get('field')}: '{c.get('a_value')}' vs '{c.get('b_value')}' ({c.get('severity')})"
                                for c in confs
                            ),
                        })
                    if rows:
                        st.dataframe(rows, use_container_width=True, hide_index=True)
                    else:
                        st.caption("No verify-tier conflicts.")

                with st.expander(f"🔴 Hold (major conflicts) ({sev_counts['major']})", expanded=True):
                    rows = []
                    for p in pairs:
                        confs = p.get("conflicts") or []
                        if not any((c.get("severity") or "").lower() == "major" for c in confs):
                            continue
                        a_id, b_id = p.get("a_id"), p.get("b_id")
                        a_rec = a_by_id.get(a_id, {})
                        b_rec = b_by_id.get(b_id, {})
                        rows.append({
                            "A id": a_id, "B id": b_id,
                            "A address": a_rec.get("address", ""),
                            "B address": b_rec.get("address", ""),
                            "conflicts": "; ".join(
                                f"{c.get('field')}: '{c.get('a_value')}' vs '{c.get('b_value')}' ({c.get('severity')})"
                                for c in confs
                            ),
                        })
                    if rows:
                        st.dataframe(rows, use_container_width=True, hide_index=True)
                    else:
                        st.caption("No major conflicts.")

                col_a_orph, col_b_orph = st.columns(2)
                with col_a_orph:
                    with st.expander(f"⚪ A-orphans ({len(a_orphans)}) — in main pipeline, missing from acquired feed", expanded=False):
                        rows = []
                        for a_id in a_orphans:
                            r = a_by_id.get(a_id, {})
                            rows.append({
                                "A id": a_id,
                                "address": r.get("address", ""),
                                "zipcode": r.get("zipcode", ""),
                                "bldgclass": r.get("bldgclass", ""),
                            })
                        if rows:
                            st.dataframe(rows, use_container_width=True, hide_index=True)
                        else:
                            st.caption("No A-orphans.")
                with col_b_orph:
                    with st.expander(f"⚪ B-orphans ({len(b_orphans)}) — new properties from acquired feed", expanded=False):
                        rows = []
                        for b_id in b_orphans:
                            r = b_by_id.get(b_id, {})
                            rows.append({
                                "B id": b_id,
                                "address": r.get("address", ""),
                                "zipcode": r.get("zipcode", ""),
                                "bldgclass": r.get("bldgclass", ""),
                            })
                        if rows:
                            st.dataframe(rows, use_container_width=True, hide_index=True)
                        else:
                            st.caption("No B-orphans.")

elif source in ("NYC (PLUTO API)", "Vancouver (Open Data)", "Toronto (Open Data)"):
    st.info("Click **Fetch Data** in the sidebar to load property records.")
