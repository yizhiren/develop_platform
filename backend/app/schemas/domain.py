from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_serializer


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_serializer("*", when_used="json", check_fields=False)
    def serialize_utc_datetimes(self, value: Any) -> Any:
        if not isinstance(value, datetime):
            return value
        aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return aware.isoformat().replace("+00:00", "Z")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"


class UserView(ORMModel):
    id: str
    email: EmailStr
    display_name: str
    system_role: str


class UserCreate(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=12, max_length=256)
    system_role: Literal["admin", "member"] = "member"


class ProjectMemberCreate(BaseModel):
    user_id: str
    role: Literal["owner", "developer", "viewer"]


class ProjectMemberView(BaseModel):
    user_id: str
    email: EmailStr
    display_name: str
    role: str


class ProjectCreate(BaseModel):
    key: str = Field(pattern=r"^[A-Z][A-Z0-9_-]{1,39}$")
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=4000)


class ProjectView(ORMModel):
    id: str
    key: str
    name: str
    description: str
    owner_id: str
    created_at: datetime
    updated_at: datetime


class RepositoryCreate(BaseModel):
    provider: Literal["github", "gitlab"]
    external_id: str
    full_name: str
    clone_url: str
    web_url: str
    default_branch: str = "main"


class RepositoryUpdate(RepositoryCreate):
    pass


class RepositoryView(ORMModel):
    id: str
    project_id: str
    provider: str
    external_id: str
    full_name: str
    clone_url: str
    web_url: str
    default_branch: str
    webhook_status: str


class ProviderCredentialUpdate(BaseModel):
    token: SecretStr = Field(min_length=20, max_length=4096)


class ProviderCredentialStatus(BaseModel):
    provider: Literal["github", "gitlab"]
    configured: bool
    source: Literal["managed", "environment", "none"]


class RequirementRepositoryInput(BaseModel):
    repository_id: str
    target_branch: str
    merge_order: int = Field(default=0, ge=0)


class RequirementImageInput(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    data_base64: str = Field(min_length=4, max_length=7_000_000)


class RequirementCreate(BaseModel):
    title: str = Field(min_length=3, max_length=240)
    description: str = Field(min_length=10, max_length=50_000)
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    repositories: list[RequirementRepositoryInput] = Field(min_length=1)
    attachments: list[RequirementImageInput] = Field(default_factory=list, max_length=5)


class RequirementView(ORMModel):
    id: str
    project_id: str
    number: int
    title: str
    description: str
    status: str
    priority: str
    owner_id: str
    version: int
    review_failures: int
    acceptance_failures: int
    created_at: datetime
    updated_at: datetime


class RequirementAttachmentView(ORMModel):
    id: str
    requirement_id: str
    filename: str
    media_type: str
    sha256: str
    size_bytes: int
    created_at: datetime


class RequirementRepositoryView(ORMModel):
    id: str
    repository_id: str
    target_branch: str
    work_branch: str | None
    pull_request_number: int | None
    pull_request_url: str | None
    head_sha: str | None
    merge_order: int
    status: str


class RequirementRepositoryDeliveryUpdate(BaseModel):
    work_branch: str = Field(min_length=1, max_length=255)
    pull_request_number: int = Field(ge=1)
    pull_request_url: str | None = Field(default=None, max_length=4000)
    head_sha: str = Field(pattern=r"^[0-9a-fA-F]{7,64}$")


class MessageCreate(BaseModel):
    body: str = Field(min_length=1, max_length=20_000)


class MessageView(ORMModel):
    id: str
    requirement_id: str
    author_type: str
    author_id: str | None
    stage: str
    body: str
    created_at: datetime


class TransitionRequest(BaseModel):
    event: str
    expected_version: int = Field(ge=1)
    reason: str = Field(default="", max_length=4000)


class TransitionView(BaseModel):
    requirement: RequirementView
    scheduled_task_id: str | None = None


class AcceptanceCriterion(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=4000)
    verification_method: str = Field(min_length=1, max_length=4000)
    priority: Literal["must", "should", "could"] = "must"


class ClarificationSpec(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    summary: str
    users_and_scenarios: list[str]
    functional_requirements: list[str]
    non_functional_requirements: list[str]
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1, max_length=100)
    edge_cases: list[str]
    out_of_scope: list[str]
    dependencies: list[str]
    risks: list[str]
    open_questions: list[str]
    repository_ids: list[str]


class RepositoryPlan(BaseModel):
    repository_id: str
    purpose: str
    changes: list[str]
    test_commands: list[str]
    depends_on: list[str] = []
    merge_order: int = Field(ge=0)


class ArchitecturePlan(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    confidence: int = Field(
        ge=0,
        le=100,
        description="方案基于需求与仓库证据可直接实施的校准置信度（0-100）",
    )
    current_state: str
    target_architecture: str
    data_flow: list[str]
    public_interface_changes: list[str]
    database_changes: list[str]
    repositories: list[RepositoryPlan]
    security_considerations: list[str]
    migration_and_rollback: list[str]
    test_strategy: list[str]
    risks: list[str]


class DevelopmentReport(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    summary: str
    repositories_changed: list[str]
    commits: dict[str, list[str]]
    tests: list[dict[str, Any]]
    unresolved_risks: list[str]
    files_changed: list[str] = Field(default_factory=list)


class ReviewFinding(BaseModel):
    severity: Literal["blocker", "high", "medium", "low"]
    repository_id: str
    path: str | None = None
    line: int | None = None
    title: str
    rationale: str
    required_change: str


class CodeReviewReport(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    approved: bool
    summary: str
    findings: list[ReviewFinding]
    plan_compliance: list[str]
    test_assessment: list[str]


class CriterionResult(BaseModel):
    criterion_id: str
    status: Literal["passed", "failed", "blocked"]
    summary: str
    evidence_paths: list[str]


class AcceptanceReport(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    approved: bool
    summary: str
    criteria: list[CriterionResult]
    regression_results: list[dict[str, Any]]
    environment: dict[str, str]


ArtifactPayload = ClarificationSpec | ArchitecturePlan | DevelopmentReport | CodeReviewReport | AcceptanceReport
