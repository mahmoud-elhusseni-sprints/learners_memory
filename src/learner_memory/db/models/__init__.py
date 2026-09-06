"""Import every model so Alembic autogenerate sees the full metadata."""
from learner_memory.db.base import Base
from learner_memory.db.models.learner import (
    CareerGoal, JourneyStep, Learner, LearnerPersonalData, LearnerProfile,
    LearnerTechnicalSkill, LearningJourney, SkillAssessment, SkillCatalog,
)
from learner_memory.db.models.raw import (
    CardContribution, DeadLetter, JobRun, MemoryCardRecord, RawDocument,
)

__all__ = [
    "Base", "Learner", "LearnerPersonalData", "SkillCatalog", "SkillAssessment",
    "LearnerTechnicalSkill", "CareerGoal", "LearningJourney", "JourneyStep",
    "LearnerProfile", "RawDocument", "MemoryCardRecord", "CardContribution",
    "JobRun", "DeadLetter",
]
