"""LMS learner context -> PersonalInfoUpdate."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from learner_memory.integrations.lms.mapping import to_personal_info
from learner_memory.integrations.lms.schemas import LmsProfileContext, LmsProfileEnvelope
from learner_memory.services.learner_profile_sync import PersonalInfoUpdate

FIXTURE = Path(__file__).parent.parent / "fixtures" / "lms_learner_profile_context.json"


def context(**basic_info_overrides) -> LmsProfileContext:
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))
    body["data"]["profile"]["basic_info"].update(basic_info_overrides)
    return LmsProfileEnvelope.model_validate(body).data


def test_captured_user_updated_payload_maps_to_personal_info():
    update = to_personal_info(context())

    assert update == PersonalInfoUpdate(
        full_name="Ardith Conn",
        email="mreynolds@example.com",
        phone=None,
        phone_country_code=None,
        city="Cairo",
        country=None,
        bio=None,
        avatar_url=None,
        timezone=None,
        preferred_language="en",
        job_preference="Onsite",
        github_url=None,
        linkedin_url=None,
        cv_url=None,
        github_id=None,
        linkedin_id=None,
        source_updated_at=datetime(2026, 9, 16, 2, 18, 5, tzinfo=UTC),
    )


def test_filled_in_profile_maps_every_field():
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))
    profile = body["data"]["profile"]
    profile["basic_info"].update(
        bio="Backend developer",
        timezone="Africa/Cairo",
        contact={"country_code": "+20", "mobile_number": "1001234567"},
        media={"avatar": {"cdn_url": "https://cdn.test/a.png"}},
    )
    profile["location"]["country"] = "Egypt"
    profile["links"] = {"github": "https://github.com/ardith",
                        "linkedin": "https://linkedin.com/in/ardith",
                        "cv": "https://cdn.test/cv.pdf"}
    profile["fields"].update(github_id=4412, linkedin_id="li-77")

    update = to_personal_info(LmsProfileEnvelope.model_validate(body).data)

    assert (update.bio, update.timezone) == ("Backend developer", "Africa/Cairo")
    assert (update.phone_country_code, update.phone) == ("+20", "1001234567")
    assert update.avatar_url == "https://cdn.test/a.png"
    assert (update.city, update.country) == ("Cairo", "Egypt")
    assert update.github_url == "https://github.com/ardith"
    assert update.linkedin_url == "https://linkedin.com/in/ardith"
    assert update.cv_url == "https://cdn.test/cv.pdf"
    assert (update.github_id, update.linkedin_id) == ("4412", "li-77")


def test_blank_text_is_no_value():
    update = to_personal_info(context(bio="   ", full_name=" Ardith Conn "))

    assert update.bio is None
    assert update.full_name == "Ardith Conn"


def test_unknown_timezone_is_dropped():
    update = to_personal_info(context(timezone="Mars/Olympus_Mons"))

    assert update.timezone is None
    assert update.email == "mreynolds@example.com"
