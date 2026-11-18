import html
import re
from dataclasses import dataclass
from typing import Any

from app.filters.decision import FilterDecision, passed, rejected, uncertain

DEFAULT_REQUIRED_HEADINGS = (
    "Requirements", "Basic Qualifications", "Required Qualifications",
    "Minimum Qualifications", "What You Bring", "You're Our Person If",
    "Our Ideal Candidate",
)
DEFAULT_PREFERRED_HEADINGS = (
    "Preferred Qualifications", "Nice to Have", "Bonus", "Even Better", "Even Better If",
)
SECTION_END = re.compile(
    r"\b(?:responsibilities|what (?:you|we)(?:'ll| will) do|what we offer|benefits|"
    r"about (?:us|the company)|company overview|who we are|our culture|nice to have|"
    r"preferred qualifications?|preferred skills|equal opportunity|compensation|salary)\b\s*:?",
    re.I,
)
RANGE = re.compile(
    r"(?P<minimum>\b\d{1,2})\s*(?:-|–|—|to)\s*\d{1,2}\s*\+?\s*(?:years?|yrs?)\b",
    re.I,
)
MINIMUM = re.compile(
    r"\b(?:minimum(?:\s+of)?|min\.?|at least)\s+(?P<minimum>\d{1,2})\s*(?:years?|yrs?)\b",
    re.I,
)
PLUS = re.compile(r"(?P<minimum>\b\d{1,2})\s*\+\s*(?:years?|yrs?)\b", re.I)
PLAIN_EXPERIENCE = re.compile(
    r"(?P<minimum>\b\d{1,2})\s*(?:years?|yrs?)(?:['’])?\s+(?:of\s+)?"
    r"(?:(?:relevant|professional|related|work|industry)\s+)?experience\b",
    re.I,
)
PLAIN_YEARS = re.compile(r"(?P<minimum>\b\d{1,2})\s*(?:years?|yrs?)\b", re.I)
FRESHER = re.compile(r"\b(?:fresher|freshers|fresh graduate|recent graduate)\b", re.I)
REQUIREMENT_CONTEXT = re.compile(
    r"\b(?:require[sd]?|must have|need(?:s)?|minimum|min\.?|at least|"
    r"experience\s*[:\-]|looking for|seeking)\b",
    re.I,
)
REQUIRED_SUFFIX = re.compile(r"\b(?:required|required experience|minimum)\b", re.I)
BOILERPLATE_PREFIX = re.compile(r"(?:over|more than|nearly|almost|around|for|founded|established)\s*$", re.I)
SENTENCE_END = re.compile(r"[.!?;\n]")
ROLE_COUNT_CLAUSE = re.compile(
    r"\b(?:applying\s+for\s+)?(?:a\s+)?maximum\s+of\s+\d+\s+roles?\b[^.!?\n]*[.!?]?",
    re.I,
)
ROLE_COUNT = re.compile(r"\b\d+\s+roles?\b", re.I)


@dataclass(frozen=True)
class _ExperienceEvidence:
    minimum_years: int | None
    conflicting_values: tuple[int, ...] = ()
    alternatives: bool = False


def _heading_pattern(headings: tuple[str, ...] | list[str]) -> re.Pattern[str] | None:
    terms = [
        re.escape(str(heading).strip()).replace(r"\ ", r"\s+")
        for heading in headings if str(heading).strip()
    ]
    if not terms:
        return None
    alternatives = "|".join(sorted(terms, key=len, reverse=True))
    return re.compile(r"(?<!\w)(?:" + alternatives + r")(?!\w)\s*[:.…]*", re.I)


def _requirement_text(
    text: str,
    required_headings: tuple[str, ...] | list[str] = DEFAULT_REQUIRED_HEADINGS,
    preferred_headings: tuple[str, ...] | list[str] = DEFAULT_PREFERRED_HEADINGS,
) -> tuple[str, bool]:
    normalized = text or ""
    for _ in range(8):
        decoded = html.unescape(normalized)
        if decoded == normalized:
            break
        normalized = decoded
    normalized = normalized.replace("’", "'")
    normalized = ROLE_COUNT_CLAUSE.sub(" ", normalized)
    normalized = ROLE_COUNT.sub(" ", normalized)
    requirement_pattern = _heading_pattern(required_headings)
    preferred_pattern = _heading_pattern(preferred_headings)
    headings = list(requirement_pattern.finditer(normalized)) if requirement_pattern else []
    if not headings:
        if preferred_pattern:
            preferred = preferred_pattern.search(normalized)
            if preferred:
                return normalized[:preferred.start()], False
        return normalized, False

    blocks: list[str] = []
    for index, heading in enumerate(headings):
        start = heading.end()
        next_heading = headings[index + 1].start() if index + 1 < len(headings) else len(normalized)
        boundaries = [next_heading]
        next_section = SECTION_END.search(normalized, start, next_heading)
        if next_section:
            boundaries.append(next_section.start())
        if preferred_pattern:
            preferred = preferred_pattern.search(normalized, start, next_heading)
            if preferred:
                boundaries.append(preferred.start())
        end = min(boundaries)
        blocks.append(normalized[start:end])
    return " ".join(blocks), True


def _sentence_prefix(text: str, position: int) -> str:
    previous = max((match.end() for match in SENTENCE_END.finditer(text, 0, position)), default=0)
    return text[previous:position]


OPTION_BLOCK = re.compile(
    r"\bOption\s+\d+\s*:\s*(.*?)(?=\bOption\s+\d+\s*:|\Z)",
    re.I | re.S,
)


def _experience_evidence(
    text: str,
    *,
    allow_alternatives: bool = True,
    required_headings: tuple[str, ...] | list[str] = DEFAULT_REQUIRED_HEADINGS,
    preferred_headings: tuple[str, ...] | list[str] = DEFAULT_PREFERRED_HEADINGS,
) -> _ExperienceEvidence:
    source, has_requirement_section = _requirement_text(
        text, required_headings, preferred_headings
    )
    options = list(OPTION_BLOCK.finditer(source)) if allow_alternatives else []
    if len(options) >= 2:
        option_evidence = [
            _experience_evidence(
                f"Requirements: {option.group(1)}", allow_alternatives=False,
                required_headings=required_headings,
                preferred_headings=preferred_headings,
            )
            for option in options
        ]
        if any(item.conflicting_values for item in option_evidence):
            conflicts = tuple(sorted({
                value for item in option_evidence for value in item.conflicting_values
            }))
            return _ExperienceEvidence(minimum_years=None, conflicting_values=conflicts)
        minima = [item.minimum_years for item in option_evidence if item.minimum_years is not None]
        if minima:
            return _ExperienceEvidence(minimum_years=min(minima), alternatives=True)

    values: set[int] = set()
    spans: list[tuple[int, int]] = []

    if FRESHER.search(source):
        values.add(0)

    for pattern in (RANGE, MINIMUM, PLUS):
        for match in pattern.finditer(source):
            if any(start <= match.start() < end for start, end in spans):
                continue
            prefix = _sentence_prefix(source, match.start())
            if BOILERPLATE_PREFIX.search(prefix.strip()):
                continue
            values.add(int(match.group("minimum")))
            spans.append(match.span())

    for match in PLAIN_EXPERIENCE.finditer(source):
        # A range such as 2-4 years also contains a plain "4 years of
        # experience" match. The range minimum is the relevant value.
        if any(start <= match.start() < end for start, end in spans):
            continue
        prefix = _sentence_prefix(source, match.start())
        if BOILERPLATE_PREFIX.search(prefix.strip()):
            continue
        if has_requirement_section or REQUIREMENT_CONTEXT.search(prefix):
            values.add(int(match.group("minimum")))
            spans.append(match.span())

    for match in PLAIN_YEARS.finditer(source):
        if any(start <= match.start() < end for start, end in spans):
            continue
        prefix = _sentence_prefix(source, match.start())
        if BOILERPLATE_PREFIX.search(prefix.strip()):
            continue
        suffix = source[match.end():match.end() + 50]
        if REQUIREMENT_CONTEXT.search(prefix) or REQUIRED_SUFFIX.search(suffix):
            values.add(int(match.group("minimum")))
            spans.append(match.span())

    ordered = tuple(sorted(values))
    if len(ordered) > 1:
        return _ExperienceEvidence(minimum_years=None, conflicting_values=ordered)
    if not ordered:
        return _ExperienceEvidence(minimum_years=None)
    return _ExperienceEvidence(minimum_years=ordered[0])


def parse_experience_requirement(
    text: str,
    *,
    required_headings: tuple[str, ...] | list[str] = DEFAULT_REQUIRED_HEADINGS,
    preferred_headings: tuple[str, ...] | list[str] = DEFAULT_PREFERRED_HEADINGS,
) -> int | None:
    """Return an unambiguous minimum requirement; None means unknown or conflicting."""
    evidence = _experience_evidence(
        text,
        required_headings=required_headings,
        preferred_headings=preferred_headings,
    )
    return evidence.minimum_years


def extract_experience_years(text: str) -> int | None:
    """Backward-compatible alias for the parsed minimum experience requirement."""
    return parse_experience_requirement(text)


def matches_experience(
    job: dict[str, Any],
    max_min_years: int = 2,
    *,
    required_headings: tuple[str, ...] | list[str] = DEFAULT_REQUIRED_HEADINGS,
    preferred_headings: tuple[str, ...] | list[str] = DEFAULT_PREFERRED_HEADINGS,
) -> FilterDecision:
    years = job.get("experience_requirement")
    evidence = _experience_evidence(
        f"{job.get('title', '')} {job.get('description', '')}",
        required_headings=required_headings,
        preferred_headings=preferred_headings,
    )
    if years is None:
        years = evidence.minimum_years
    if job.get("experience_requirement") is None and evidence.conflicting_values:
        found = ", ".join(str(value) for value in evidence.conflicting_values)
        return uncertain(f"Conflicting minimum experience requirements were found: {found} years.")
    alternatives_note = " (alternatives, lowest used)" if evidence.alternatives else ""
    if years is None:
        return uncertain("Minimum experience requirement is not stated unambiguously.")
    if years > max_min_years:
        return rejected(f"Minimum experience requirement is {years} years; maximum allowed is {max_min_years}.{alternatives_note}")
    return passed(f"Minimum experience requirement is {years} years, within the {max_min_years}-year limit.{alternatives_note}")
