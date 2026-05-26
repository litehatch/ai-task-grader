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
from datetime import datetime

try:
    import anthropic
except ImportError:
    st.error("Run: pip install anthropic")
    st.stop()

st.set_page_config(page_title="AI Data Cleanup Evaluation", layout="wide")

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

st.title("AI Data Cleanup Evaluation")
st.caption("Measure AI accuracy on real property records — task by task, backed by numbers.")

def load_env_key():
    # Check Streamlit secrets first (Cloud), then OS env var (local dev)
    try:
        if "ANTHROPIC_API_KEY" in st.secrets:
            return st.secrets["ANTHROPIC_API_KEY"]
    except (FileNotFoundError, Exception):
        pass
    return os.environ.get("ANTHROPIC_API_KEY", "")

_env_key = load_env_key()

# ── Sidebar ──
with st.sidebar:
    st.header("Configuration")
    if _env_key:
        api_key = _env_key
        st.caption("✓ Demo mode — no API key required to try the live evaluation.")
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
                st.session_state["data"] = payload["data"]
                st.session_state["results"] = payload["results"]
                st.session_state["data_source"] = payload.get("city", "uploaded")
                st.success(f"Loaded {len(payload['data'])} records from {payload.get('city', 'saved run')} (generated {payload.get('generated_at', 'unknown date')[:10]}).")
            except Exception as e:
                st.error(f"Couldn't load that file: {e}")
    st.divider()

    st.header("Data Source")
    source = st.radio("Choose source", ["Toronto (Open Data)", "NYC (PLUTO API)", "Vancouver (Open Data)", "Upload CSV"])

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

# ── Data fetching ──
def fetch_nyc_data(borough, zip_filter, street_filter, limit):
    conditions = ["address IS NOT NULL"]
    if borough != "Any":
        conditions.append(f"borough='{boro_map[borough]}'")
    if zip_filter:
        conditions.append(f"zipcode='{zip_filter}'")
    if street_filter:
        conditions.append(f"address like '%25{street_filter.upper()}%25'")
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
    if street:
        conditions.append(f"street_name LIKE '*{street.upper()}*'")
    if postal:
        conditions.append(f"property_postal_code LIKE '{postal.upper()}*'")
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

def parse_csv_upload(uploaded_file):
    content = uploaded_file.read().decode("utf-8")
    return list(csv.DictReader(io.StringIO(content)))

# ── LLM calls ──
def call_claude(client, prompt, model_name, max_tokens=4096):
    resp = client.messages.create(
        model=model_name, max_tokens=max_tokens,
        system="You are a real estate data processing assistant. Return ONLY valid JSON, no explanation or markdown.",
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

def parse_json_response(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if text.startswith("["):
            last_brace = text.rfind("}")
            if last_brace > 0:
                return json.loads(text[:last_brace+1] + "]")
        elif text.startswith("{"):
            last_brace = text.rfind("}")
            if last_brace > 0:
                return json.loads(text[:last_brace+1])
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

def run_address_task(client, records, model_name):
    city = detect_city(records)

    if city == "toronto":
        batch = [{"id": i+1, "raw_address": r.get("address", ""), "fsa": r.get("zipcode", ""), "ward": r.get("ward", "")}
                 for i, r in enumerate(records)]
        prompt = f"""Parse and standardize each Toronto address into structured components.

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

Return JSON array:
[{{"id": 1, "unit": "", "street_number": "12", "street_name": "The Donway E", "city": "Toronto", "province": "ON", "fsa": "M3C", "standardized": "12 The Donway E, Toronto, ON M3C", "changes": ["Title-cased street name", "Collapsed double spaces"]}}]"""
    elif city == "vancouver":
        batch = [{"id": i+1, "raw_address": r.get("address", ""), "postal_code": r.get("zipcode", "")}
                 for i, r in enumerate(records)]
        prompt = f"""Parse and standardize each Vancouver address into structured components.

Rules:
- Convert ALL CAPS to proper Title Case
- Abbreviate street types: Street→St, Avenue→Ave, Boulevard→Blvd, Place→Pl, Lane→Ln, Court→Ct, Road→Rd, Drive→Dr, Crescent→Cres, Mews→Mews
- Abbreviate directionals: West→W, East→E, North→N, South→S
- City is always Vancouver, province is always BC
- If a unit/townhouse prefix exists (e.g. "TH116"), split it into unit and street_number
- Note every change you made from the original

Input records:
{json.dumps(batch, indent=2)}

Return JSON array:
[{{"id": 1, "unit": "", "street_number": "605", "street_name": "Hamilton St", "city": "Vancouver", "province": "BC", "postal_code": "V6B 5W4", "standardized": "605 Hamilton St, Vancouver, BC V6B 5W4", "changes": ["Title-cased street name", "Abbreviated Street→St"]}}]"""
    else:
        batch = [{"id": i+1, "raw_address": r.get("address", ""), "borough": r.get("borough", ""),
                  "zipcode": r.get("zipcode", "")} for i, r in enumerate(records)]
        prompt = f"""Parse and standardize each NYC address into structured components.

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

Return JSON array:
[{{"id": 1, "unit": "", "street_number": "325", "street_name": "Greenwich St", "city": "New York", "state": "NY", "zipcode": "10013", "standardized": "325 Greenwich St, New York, NY 10013", "changes": ["Title-cased street name", "Abbreviated Street→St"]}}]"""

    raw = call_claude(client, prompt, model_name, max_tokens=8192)
    parsed = parse_json_response(raw)
    return {r["id"]: r for r in parsed}

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

    raw = call_claude(client, prompt, model_name)
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
    class_task_name = {"vancouver": "Zoning Classification", "toronto": "Building Type Classification"}.get(city, "Building Classification")
    prompt = f"""You just evaluated {num_records} real {city_label} property records with AI across three tasks. Here are the results:

{json.dumps(summary, indent=2)}

Based on these actual results, produce a verdict as a JSON object with this structure:
{{
  "tasks": [
    {{
      "name": "Address Standardization",
      "recommendation": "short recommendation (3-6 words)",
      "rationale": "one sentence based on what you actually observed"
    }},
    {{
      "name": "{class_task_name}",
      "recommendation": "short recommendation (3-6 words)",
      "rationale": "one sentence based on what you actually observed"
    }},
    {{
      "name": "Data Quality Audit",
      "recommendation": "short recommendation (3-6 words)",
      "rationale": "one sentence based on what you actually observed"
    }}
  ],
  "bottom_line": "2-3 sentence overall assessment specific to this dataset — what worked, what didn't, and what a production pipeline should do differently. Reference specific findings from the data."
}}"""

    raw = call_claude(client, prompt, model_name)
    return parse_json_response(raw)

# ── Verdict citation guardrail ──
# The verdict step is synthesis prose: the model summarizes the three task
# results and cites specific row numbers as evidence. Two real failure modes
# were observed in production runs:
#   1. unaudited_row — the verdict cites a row that no quality finding mentions
#      (pure fabrication).
#   2. cross_finding_mashup — the verdict groups several rows in one sentence
#      ("rows 3, 5, 23, 26, and 49 are missing FSA") when those rows actually
#      belong to different findings (49 was an FSA *mismatch*, not missing).
# This guardrail parses every row citation from the verdict prose and
# cross-checks it against the audit's affected_rows map.

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

def validate_verdict_citations(verdict, results):
    if not isinstance(verdict, dict):
        return []

    findings_by_row = {}
    for q in (results.get("quality") or []):
        title = q.get("title") or "(untitled finding)"
        for rid in (q.get("affected_rows") or []):
            try:
                findings_by_row.setdefault(int(rid), set()).add(title)
            except (TypeError, ValueError):
                continue
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

    raw = call_claude(client, prompt, model_name)
    return parse_json_response(raw)

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

    # Verdict block
    verdict_html = ""
    verdict = results.get("verdict")
    if verdict and isinstance(verdict, dict):
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

  <!-- Verdict -->
  <section class="mb-8">
    <h2 class="text-xl font-bold text-gray-900 mb-3">Verdict</h2>
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
        <div class="hero-num text-4xl font-bold mt-2">$300–1K</div>
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
      <strong class="text-white">AI is 10×–500× cheaper than manual</strong> on the tasks the verdict above scores safe to automate.
    </div>
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
            except Exception as e:
                st.error(f"Failed to fetch: {e}")
    if "data" in st.session_state:
        data = st.session_state["data"]

else:
    if uploaded:
        data = parse_csv_upload(uploaded)
        st.session_state["data"] = data
        st.session_state["data_source"] = "csv"
        st.session_state["results"] = None
    elif "data" in st.session_state:
        data = st.session_state["data"]

if data:
    src_label = st.session_state.get("data_source", "unknown").upper()
    st.success(f"**{len(data)} records loaded** from {src_label}")

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

        tab1, tab2, tab3, tab4 = st.tabs(["Addresses", "Classifications", "Data Quality", "Verdict"])

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
            st.subheader("Verdict")
            city_label = {"vancouver": "Vancouver", "toronto": "Toronto"}.get(city, "NYC")
            verdict = results.get("verdict")
            if verdict and isinstance(verdict, dict):
                citation_warnings = validate_verdict_citations(verdict, results)
                if citation_warnings:
                    with st.expander(
                        f"⚠️ Citation guardrail flagged {len(citation_warnings)} claim(s) — review before sharing",
                        expanded=True,
                    ):
                        st.caption(
                            "Cross-checks every row number cited in the verdict against the underlying audit "
                            "findings. Catches two real failure modes of LLM synthesis: hallucinated row IDs "
                            "and row groupings that mix evidence from different findings."
                        )
                        for w in citation_warnings:
                            st.markdown(f"- {w['message']}")
                            r2f = w.get("row_to_findings")
                            if r2f:
                                for r, titles in r2f.items():
                                    label = ", ".join(titles) if titles else "(no finding flags this row)"
                                    st.markdown(f"    - Row {r}: {label}")

                tasks = verdict.get("tasks", [])
                if tasks:
                    st.markdown(f"**{city_label} evaluation results ({len(data)} records):**")
                    table_md = "| Task | Recommendation | Rationale |\n|------|---------------|----------|\n"
                    for t in tasks:
                        table_md += f"| **{t.get('name', '')}** | {t.get('recommendation', '')} | {t.get('rationale', '')} |\n"
                    st.markdown(table_md)
                bottom_line = verdict.get("bottom_line", "")
                if bottom_line:
                    st.info(f"**Bottom line:** {bottom_line}")

                st.markdown(
                    """
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
                          <div style="font-size: 38px; font-weight: 800; margin-top: 6px; font-variant-numeric: tabular-nums; letter-spacing: -0.02em; line-height: 1.1;">$300–1K</div>
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
                        <strong style="color: #ffffff;">AI is 10×–500× cheaper than manual</strong> for the same volume —
                        but only worth deploying on tasks the verdict above scores as <em style="color: #cbd5e1;">automate</em> or <em style="color: #cbd5e1;">human-in-loop</em>.
                        That's the decision this harness exists to make.
                      </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            else:
                st.warning("Verdict not available — run the evaluation to generate one.")

elif source in ("NYC (PLUTO API)", "Vancouver (Open Data)", "Toronto (Open Data)"):
    st.info("Click **Fetch Data** in the sidebar to load property records.")
