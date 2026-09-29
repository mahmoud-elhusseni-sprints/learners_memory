"""LMS learner context -> our PersonalInfoUpdate. The one place that knows which
LMS field feeds which profile field."""
from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from learner_memory.core.logging import get_logger
from learner_memory.integrations.lms.schemas import LmsProfileContext
from learner_memory.services.learner_profile_sync import PersonalInfoUpdate

log = get_logger(__name__)


def to_personal_info(context: LmsProfileContext) -> PersonalInfoUpdate:
    """Read the curated sections (`basic_info`, `location`, `links`) first; the raw
    `fields` row only for values nothing else carries."""
    profile = context.profile
    basic = profile.basic_info
    return PersonalInfoUpdate(
        full_name=_text(basic.full_name),
        email=_text(basic.email),
        phone=_text(basic.contact.mobile_number),
        phone_country_code=_text(basic.contact.country_code),
        city=_text(profile.location.city),
        country=_text(profile.location.country),
        bio=_text(basic.bio),
        avatar_url=_text(basic.media.avatar.cdn_url),
        timezone=_iana_timezone(basic.timezone, context.user_id),
        preferred_language=_text(basic.language),
        job_preference=_text(profile.fields.job_preference),
        github_url=_text(profile.links.github),
        linkedin_url=_text(profile.links.linkedin),
        cv_url=_text(profile.links.cv),
        github_id=_text(profile.fields.github_id),
        linkedin_id=_text(profile.fields.linkedin_id),
        source_updated_at=context.last_updated_at,
    )


def _text(value: str | int | None) -> str | None:
    """Trimmed text; blank means no value."""
    if value is None:
        return None
    return str(value).strip() or None


def _iana_timezone(value: str | None, external_id: int) -> str | None:
    """Keep only names the tz database knows. An unknown one is dropped rather
    than failing the whole profile, since every other field is still good."""
    name = _text(value)
    if name is None:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("lms.timezone_unknown", external_id=external_id, timezone=name)
        return None
    return name
