import re
from typing import Any

from app.filters.decision import FilterDecision, FilterStatus, passed, rejected, uncertain

REMOTE = re.compile(r"\b(?:remote|work from home|work-from-home|anywhere|worldwide|global)\b", re.I)
UNRESTRICTED = re.compile(r"\b(?:anywhere|worldwide|global|location[- ]flexible|no location restriction)\b", re.I)
UNKNOWN_LOCATION = re.compile(r"^(?:unknown|unspecified|not specified|n/?a|tbd)$", re.I)
INDIA = re.compile(
    r"\b(?:india|indian|bengaluru|bangalore|hyderabad|navi mumbai|mumbai|pune|chennai|"
    r"new delhi|greater noida|delhi|noida|gurgaon|gurugram|kolkata|ahmedabad|kochi|jaipur|indore|"
    r"agra|ajmer|amritsar|bareilly|bhopal|bhubaneswar|chandigarh|coimbatore|dehradun|"
    r"dhanbad|faridabad|ghaziabad|gwalior|guwahati|jabalpur|jodhpur|kanpur|kozhikode|"
    r"lucknow|ludhiana|madurai|mangaluru|mangalore|meerut|mysuru|nagpur|nashik|patna|"
    r"prayagraj|raipur|rajkot|ranchi|srinagar|surat|thiruvananthapuram|tiruchirappalli|"
    r"udaipur|vadodara|varanasi|vijayawada|visakhapatnam|warangal)\b",
    re.I,
)
INDIA_COMPATIBLE_REGION = re.compile(r"\b(?:apac|asia(?:[- ]pacific)?)\b", re.I)
OUTSIDE_INDIA = re.compile(
    r"\b(?:united states|u\.?s\.?a?\.?|america|canada|united kingdom|u\.?k\.?|"
    r"england|scotland|wales|ireland|europe|emea|australia|new zealand|singapore|"
    r"european union|\.?eu\.?|latam|latin america|japan|china|hong kong|taiwan|israel|"
    r"brazil|mexico|south africa|new york|san francisco|seattle|los angeles|austin|"
    r"chicago|boston|denver|miami|atlanta|london|toronto|vancouver|montreal|sydney|"
    r"melbourne|berlin|paris)\b",
    re.I,
)
CITY_ALIAS_GROUPS = (
    frozenset({"gurgaon", "gurugram"}),
    frozenset({"bengaluru", "bangalore"}),
)
WORKDAY_LOCATION_PREFIX = re.compile(r"^(?P<country>[A-Z]{2})\s+(?P<state>[A-Z]{2})\b", re.I)
MULTIPLE_LOCATIONS = re.compile(r"^\d+\s+locations?$", re.I)
WORKDAY_MULTIPLE_LOCATIONS = re.compile(r"^(?:\d+\s+locations?|multiple\s+locations)$", re.I)


def matches_location(job: dict[str, Any], unknown_location_policy: str = "strict") -> FilterDecision:
    location = str(job.get("location") or "").strip()
    if not location or UNKNOWN_LOCATION.fullmatch(location):
        return uncertain("Location is not stated.")

    if unknown_location_policy == "strict" and re.fullmatch(
        r"(?:remote|anywhere|multiple locations)", location, re.I
    ):
        return uncertain("Location does not state a region or specific eligible location.")

    is_remote = bool(REMOTE.search(location))
    india_compatible = bool(INDIA.search(location) or INDIA_COMPATIBLE_REGION.search(location))
    restricted_elsewhere = bool(OUTSIDE_INDIA.search(location))

    if UNRESTRICTED.search(location):
        return passed("Role is explicitly location-flexible or worldwide.")
    if is_remote and india_compatible:
        return passed("Remote role is explicitly India-compatible.")
    if is_remote and restricted_elsewhere:
        return rejected("Remote role is restricted to a region outside India.")
    if is_remote:
        return passed("Remote role has no stated region restriction.")
    if india_compatible:
        return passed("Role is located in India.")
    if restricted_elsewhere:
        return rejected("Role is located outside India and is not remote-compatible.")
    if unknown_location_policy == "strict":
        return rejected("Location is unrecognized under the strict unknown-location policy.")
    return uncertain("Location does not establish India eligibility.")


def matches_workday_location(
    job: dict[str, Any], unknown_location_policy: str, assume_country: str | None
) -> FilterDecision:
    location = str(job.get("location") or "").strip()
    assumed = str(assume_country or "").strip()
    detail_country = str(job.get("_workday_location_country") or "").strip().upper()
    if detail_country:
        if detail_country == "IN":
            return passed("Workday requisition location country is India.")
        return rejected(f"Workday requisition location country is {detail_country}, outside India.")
    if assumed and WORKDAY_MULTIPLE_LOCATIONS.fullmatch(location):
        return uncertain("Location lists multiple unspecified locations.")
    prefix = WORKDAY_LOCATION_PREFIX.match(location)
    if prefix:
        country = prefix.group("country").casefold()
        assumed_code = assumed.casefold()
        if country == "in" or (
            assumed and (
                country == assumed_code
                or (assumed_code in {"india", "ind"} and country == "in")
            )
        ):
            return passed("Workday location prefix identifies an eligible country and state.")
    decision = matches_location(job, unknown_location_policy)
    if (
        assumed
        and decision.status is FilterStatus.REJECT
        and decision.reason == "Location is unrecognized under the strict unknown-location policy."
    ):
        return uncertain("Workday location is unrecognized; tenant country is assumed.")
    return decision


def is_india_location(job: dict[str, Any], additional_cities: list[str] | None = None) -> bool:
    """Identify India country mentions and Indian city names for diagnostics."""
    location = str(job.get("location") or "").strip()
    if INDIA.search(location):
        return True
    for city in additional_cities or []:
        normalized_city = re.sub(r"\s+", " ", city).strip().casefold()
        aliases = next((group for group in CITY_ALIAS_GROUPS if normalized_city in group), {normalized_city})
        if any(re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", location, re.I) for alias in aliases):
            return True
    return False


def preferred_city_rank(job: dict[str, Any], preferred_cities: list[str]) -> int:
    """Sort preferred city matches first without rejecting other eligible jobs."""
    location = str(job.get("location") or "").casefold()
    for rank, city in enumerate(preferred_cities):
        normalized_city = re.sub(r"\s+", " ", city).strip().casefold()
        aliases = next((group for group in CITY_ALIAS_GROUPS if normalized_city in group), {normalized_city})
        if any(re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", location) for alias in aliases):
            return rank
    return len(preferred_cities)
