from learner_memory.core.taxonomy import LEVEL_LABELS, Taxonomy
from learner_memory.schemas.memory_card import Contribution


def test_thirty_four_subskills_across_five_categories():
    skills = Taxonomy.all()
    assert len(skills) == 34
    assert {s.category for s in skills.values()} == {
        "Cognitive Skills", "Social Skills", "Execution Skills",
        "Growth Skills", "Digital & AI Skills",
    }


def test_every_skill_has_all_six_levels():
    for skill in Taxonomy.all().values():
        assert set(skill.level_descriptors) == set(LEVEL_LABELS)
        assert all(skill.level_descriptors[i].strip() for i in range(1, 7))


def test_unknown_general_skill_is_dropped_not_invented():
    kept, dropped = Taxonomy.filter_contributions([
        Contribution(target="general_skill", key="critical_thinking", level_signal=4),
        Contribution(target="general_skill", key="vibes"),
        Contribution(target="technical_skill", key="rust"),   # unconstrained by design
    ])
    assert [c.key for c in kept] == ["critical_thinking", "rust"]
    assert [c.key for c in dropped] == ["vibes"]
