"""LMS learner context -> our domain updates. The one place that knows which
LMS field feeds which profile, journey or step field."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from learner_memory.core.logging import get_logger
from learner_memory.integrations.lms.schemas import (
    LmsEnrollment,
    LmsJourneyContext,
    LmsJourneyProgress,
    LmsProfileContext,
    LmsProgramCounters,
)
from learner_memory.services.learner_journey_sync import (
    EnrollmentUpdate,
    JourneyUpdate,
    ProgramCounters,
    ProgramProgress,
    ProgressUpdate,
)
from learner_memory.services.learner_profile_sync import PersonalInfoUpdate

log = get_logger(__name__)

_PERCENT = Decimal(100)
# learning_journey.progress is NUMERIC(5,4); round half up to its 4 places.
_PROGRESS_PLACES = Decimal("0.0001")


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


def to_journey_update(context: LmsJourneyContext, journey_id: int) -> JourneyUpdate:
    """The requested journey's enrollment and progress. Entries for any other
    journey are ignored; either part is None when the LMS did not send it."""
    enrollment = next((e for e in context.enrollments if e.journey_id == journey_id), None)
    progress = next((p for p in context.progress if p.journey_id == journey_id), None)
    return JourneyUpdate(
        external_id=journey_id,
        enrollment=_enrollment(enrollment) if enrollment is not None else None,
        progress=_progress(progress) if progress is not None else None,
    )


def _enrollment(enrollment: LmsEnrollment) -> EnrollmentUpdate:
    return EnrollmentUpdate(
        slug=_text(enrollment.journey_slug),
        public_url=_text(enrollment.public_url),
        status=enrollment.status.strip(),
        blocked=enrollment.blocked,
        manual_added=enrollment.manual_added,
        started_at=enrollment.start_date,
        graduated_at=enrollment.graduation_date,
        source_updated_at=enrollment.last_updated_at,
    )


def _progress(progress: LmsJourneyProgress) -> ProgressUpdate:
    fraction = (progress.progress_percent / _PERCENT).quantize(_PROGRESS_PLACES,
                                                               rounding=ROUND_HALF_UP)
    return ProgressUpdate(
        progress=fraction,
        programs=tuple(ProgramProgress(external_id=program.program_id,
                                       counters=_counters(program.counters))
                       for program in progress.programs),
        source_updated_at=progress.last_updated_at,
    )


def _counters(counters: LmsProgramCounters) -> ProgramCounters:
    return ProgramCounters(
        files_completed=counters.files_counter,
        videos_completed=counters.videos_counter,
        text_lessons_completed=counters.text_lessons_counter,
        live_sessions_completed=counters.sessions_live_counter,
        recorded_sessions_completed=counters.sessions_record_counter,
        codelabs_completed=counters.codelabs_counter,
        quizzes_completed=counters.quizzes_counter,
        tasks_completed=counters.tasks_counter,
        regular_projects_completed=counters.regular_projects_counter,
        final_projects_completed=counters.final_projects_counter,
        peer_reviews_completed=counters.peer_reviews_counter,
        ai_interviews_completed=counters.ai_interviews_counter,
    )
