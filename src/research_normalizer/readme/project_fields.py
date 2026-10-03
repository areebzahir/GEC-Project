"""Map README key/values to canonical project metadata.

Keys vary across repositories ("Title of dataset", "Dataset title", "Title"), so we match a key to
a canonical field by normalised comparison against a synonym table aligned with DataCite (Ref [17])
and the Cornell README template (Ref [15]), falling back to fuzzy matching (DESIGN.md section 11).
People are parsed from repeated Name/ORCID/Institution/Email groups; dates and funding are parsed
from their values. Anything unmapped is preserved in ``additional_fields`` so nothing is lost.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from ..schema.document import DateRange, KeyValue, MethodologyNote, Person
from ..schema.provenance import Source
from ..matching.normalize import normalize_name
from .segmenter import Block, BlockType, Section

# Canonical field -> list of synonym keys (normalised on load). Order matters only for display.
_FIELD_SYNONYMS: dict[str, list[str]] = {
    "title": ["title of dataset", "dataset title", "title"],
    "description": ["description of the dataset", "dataset description", "description", "abstract", "summary"],
    "identifier": ["dataset doi", "doi", "identifier", "persistent identifier"],
    "collection_period": ["date of data collection", "collection date", "dates of collection"],
    "geographic_location": ["geographic location of data collection", "geographic location", "location"],
    "funding": ["funding information", "information about funding sources that supported the collection of the data",
                "funding sources", "funding", "grant information"],
    "license": ["licenses/restrictions placed on the data", "licenses/restrictions placed on the dataset",
                "license", "licence", "licenses", "licences", "rights"],
    "citation": ["recommended citation for this dataset", "recommended citation", "dataset citation", "citation"],
    "readme_generated_on": ["this readme file was generated on"],
}

# Person-record field keys.
_PERSON_KEYS = {"name", "orcid", "institution", "affiliation", "email", "address"}
# Keys that start a new person's role.
_ROLE_KEYS = {
    "author/principal investigator information": "principal_investigator",
    "principal investigator": "principal_investigator",
    "author/associate or co-investigator information": "co_investigator",
    "co-investigator": "co_investigator",
    "associate or co-investigator": "co_investigator",
    "long-term contact": "contact",
    "contact person": "contact",
    "authors": "author",
    "author": "author",
}
# Keys whose values are URL/relationship lists.
_PUBLICATION_KEYS = {"links to publications that cite or use the data",
                     "links to publications that cite or use the dataset", "related publications"}
_RELATED_DATA_KEYS = {"links/relationships to ancillary data sets", "links/relationships to related datasets",
                      "links to other publicly accessible locations of the data", "related datasets"}

_FIELD_MATCH_THRESHOLD = 85.0  # fuzzy key match acceptance (0-100)
_DATE_RANGE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s*(?:to|–|-|through|until)\s*(\d{4}-\d{2}-\d{2})")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_GENERATED_RE = re.compile(r"generated on\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE)


@dataclass
class ProjectFields:
    """Collected canonical metadata, ready to populate the Project model."""

    values: dict[str, object] = field(default_factory=dict)
    sources: dict[str, Source] = field(default_factory=dict)
    creators: list[Person] = field(default_factory=list)
    contacts: list[Person] = field(default_factory=list)
    related_publications: list[str] = field(default_factory=list)
    related_datasets: list[str] = field(default_factory=list)
    funding: list[str] = field(default_factory=list)
    methodology: list[MethodologyNote] = field(default_factory=list)
    additional: list[KeyValue] = field(default_factory=list)


# Pre-normalise the synonym table once.
_NORMALISED_SYNONYMS: dict[str, list[str]] = {
    field_name: [normalize_name(s) for s in syns] for field_name, syns in _FIELD_SYNONYMS.items()
}


def _match_field(key: str) -> str | None:
    """Return the canonical field a README key maps to, or None (DESIGN.md section 11)."""
    norm = normalize_name(key)
    if not norm:
        return None
    for field_name, syns in _NORMALISED_SYNONYMS.items():
        if norm in syns:
            return field_name
    # Fuzzy fallback for near-miss keys.
    best_field, best_score = None, 0.0
    for field_name, syns in _NORMALISED_SYNONYMS.items():
        for syn in syns:
            score = fuzz.ratio(norm, syn)
            if score > best_score:
                best_field, best_score = field_name, score
    return best_field if best_score >= _FIELD_MATCH_THRESHOLD else None


def _split_list_value(value: str) -> list[str]:
    """Split a value that holds multiple items (newlines were already joined; split on ; or , URLs)."""
    parts = re.split(r"[;\n]+|,\s*(?=https?://)", value)
    return [p.strip() for p in parts if p.strip() and p.strip().lower() not in {"none", "n/a", "na", "tbd"}]


def _parse_funding(value: str) -> list[str]:
    parts = re.split(r"[;\n]+", value)
    return [p.strip() for p in parts if p.strip()]


def extract_project_fields(sections: list[Section], file_name: str, full_text: str) -> ProjectFields:
    """Walk the sections and pull out canonical project metadata with provenance."""
    out = ProjectFields()

    # README generation date (and author) from the first line, e.g. "generated on 2026-06-24 by X".
    gen = _GENERATED_RE.search(full_text)
    if gen:
        out.values["readme_generated_on"] = gen.group(1)
        out.sources["readme_generated_on"] = Source(file=file_name, method="generated_on")

    current_role = "author"
    pending_person: dict[str, str] = {}
    # Section-title cues that mean "Name:" here is a variable, not a person. Person records only
    # appear under general-info/author/contact sections, so we switch person parsing off elsewhere.
    non_person_cues = ("variable", "data-specific", "data specific", "method", "overview",
                       "file list", "column", "dictionary", "codebook")

    def flush_person() -> None:
        if "name" in pending_person:
            person = Person(
                name=pending_person.get("name", ""),
                role=current_role,
                orcid=pending_person.get("orcid") or None,
                affiliation=pending_person.get("institution") or pending_person.get("affiliation") or None,
                email=pending_person.get("email") or None,
            )
            if current_role == "contact":
                out.contacts.append(person)
            else:
                out.creators.append(person)
        pending_person.clear()

    for section in sections:
        section_title_low = (section.title or "").lower()
        person_zone = not any(cue in section_title_low for cue in non_person_cues)
        if not person_zone:
            flush_person()  # leaving the people area; commit whoever is pending
        # Section titles that denote a person role (DataCite creator grouping).
        role_hit = _role_for(section.title)
        if role_hit:
            flush_person()
            current_role = role_hit

        # Methodology sections: keep their text as notes.
        if section.title and _is_methodology(section.title):
            text = " ".join(b.text for b in section.blocks if b.type in (BlockType.TEXT, BlockType.KEY_VALUE))
            if text.strip():
                out.methodology.append(MethodologyNote(heading=section.title, text=text.strip()))

        for block in section.blocks:
            if block.type is not BlockType.KEY_VALUE or block.key is None:
                continue
            key_norm = normalize_name(block.key)
            value = (block.value or "").strip()

            # Person record fields (only inside a people-bearing section).
            if person_zone and key_norm in {normalize_name(k) for k in _PERSON_KEYS}:
                plain = block.key.strip().lower()
                if plain == "name":
                    flush_person()  # a new Name starts a new person
                pending_person[_person_key(plain)] = value
                continue

            # Role-marker key/values (some templates put the role as a key).
            role = _role_for(block.key)
            if role:
                flush_person()
                current_role = role
                continue

            # Publication / related-dataset link lists.
            if key_norm in {normalize_name(k) for k in _PUBLICATION_KEYS}:
                out.related_publications.extend(_split_list_value(value))
                continue
            if key_norm in {normalize_name(k) for k in _RELATED_DATA_KEYS}:
                out.related_datasets.extend(_split_list_value(value))
                continue

            # Canonical scalar/compound fields.
            field_name = _match_field(block.key)
            if field_name == "collection_period":
                out.values["collection_period"] = _parse_date_range(value)
                out.sources["collection_period"] = _src(file_name, section, block)
            elif field_name == "funding":
                out.funding.extend(_parse_funding(value))
                out.sources["funding"] = _src(file_name, section, block)
            elif field_name and field_name not in out.values and value:
                out.values[field_name] = value
                out.sources[field_name] = _src(file_name, section, block)
            elif value:
                # Unmapped but non-empty: keep it so nothing is dropped.
                out.additional.append(KeyValue(label=block.key.strip(), value=value,
                                                source=_src(file_name, section, block)))

    flush_person()
    return out


def _person_key(plain: str) -> str:
    return "institution" if plain == "institution" else plain


def _role_for(title: str | None) -> str | None:
    if not title:
        return None
    norm = title.strip().lower().rstrip(":")
    return _ROLE_KEYS.get(norm)


def _is_methodology(title: str) -> bool:
    low = title.lower()
    return any(k in low for k in ("method", "protocol", "quality", "instrument", "calibration", "procedure"))


def _parse_date_range(value: str) -> DateRange:
    m = _DATE_RANGE_RE.search(value)
    if m:
        return DateRange(text=value, start=m.group(1), end=m.group(2))
    dates = _ISO_DATE_RE.findall(value)
    if len(dates) == 1:
        return DateRange(text=value, start=dates[0], end=dates[0])
    return DateRange(text=value)


def _src(file_name: str, section: Section, block: Block) -> Source:
    return Source(file=file_name, section=section.title, lines=(block.line_start, block.line_end),
                  method="key_value")
