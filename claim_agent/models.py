from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Confidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


class Locale(Model):
    country: str = Field(min_length=2, max_length=80)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class Sweep(Locale):
    id: str
    captured_at: str = Field(default_factory=utcnow)
    device: str
    duration_s: Nonnegative | None = None
    ended_at: str | None = None
    packet_ready_at: str | None = None
    synthetic: bool = False
    language: str = "English"
    capture_mode: Literal["camera", "lidar"] = "lidar"
    comparison_requested: bool = False


class Price(Model):
    amount: Money | None = None
    source: str = ""
    url: str = ""
    retrieved_at: str = ""
    converted: bool = False
    condition_assumed: str = ""
    currency: str = ""
    country: str = ""
    source_ref: str = ""
    verified: bool = False
    match: Literal["exact", "equivalent", "unknown"] = "unknown"
    match_basis: str = ""
    product_name: str = ""
    exclusion_reason: str = "No supported offer retrieved"
    original_amount: Money | None = None
    original_currency: str = ""
    fx_rate: Money | None = None
    fx_source_ref: str = ""
    source_type: str = ""


class PriceRange(Model):
    match: Literal["exact", "equivalent", "unknown"] = "unknown"
    match_basis: str = ""
    product_name: str = ""
    low: Money | None = None
    high: Money | None = None
    source: str = ""
    url: str = ""
    retrieved_at: str = ""
    currency: str = ""
    source_ref: str = ""
    verified: bool = False
    condition_assumed: str = "new equivalent"
    exclusion_reason: str = "No supported replacement offer retrieved"
    country: str = ""
    converted: bool = False
    original_amount: Money | None = None
    original_currency: str = ""
    fx_rate: Money | None = None
    fx_source_ref: str = ""
    source_type: str = ""


PricingStatus = Literal["idle", "queued", "searching", "completed", "unavailable", "interrupted"]


class Book(Model):
    pricing_status: PricingStatus = "idle"
    pricing_reason: str = ""
    id: str
    shelf: str = ""
    position: int = 0
    frame_ref: str
    status: Literal["identified", "unidentified", "needs_appraisal"] = "unidentified"
    title: str = ""
    author: str = ""
    publisher: str = ""
    edition: str = ""
    isbn: str = ""
    spine_height_cm: Nonnegative | None = None
    spine_thickness_cm: Nonnegative | None = None
    id_confidence: Confidence = 0
    replacement_cost: Price = Field(default_factory=Price)
    used_value: Price = Field(default_factory=Price)
    visible_text: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    polygon: list[list[float]] = Field(default_factory=list)
    world_corners: list[list[float]] = Field(default_factory=list)
    measurement: dict = Field(default_factory=dict)
    catalogue_candidates: list[dict] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)
    user_claims: list[str] = Field(default_factory=list)
    revision: int = 0
    excluded: bool = False
    rare_signals: list[str] = Field(default_factory=list)


class Dimensions(Model):
    w: Nonnegative | None = None
    h: Nonnegative | None = None
    d: Nonnegative | None = None


class Item(Model):
    pricing_status: PricingStatus = "idle"
    pricing_reason: str = ""
    id: str
    category: str
    description: str = ""
    material: str = ""
    brand_model: str = ""
    frame_ref: str
    dimensions_cm: Dimensions = Field(default_factory=Dimensions)
    status: Literal["priced", "range", "needs_appraisal", "unpriced"] = "unpriced"
    replacement_cost: PriceRange = Field(default_factory=PriceRange)
    confidence: Confidence = 0
    evidence_refs: list[str] = Field(default_factory=list)
    world_corners: list[list[float]] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)
    user_claims: list[str] = Field(default_factory=list)
    revision: int = 0
    excluded: bool = False
    is_print: bool = False


class FurnitureMeasurement(Model):
    id: str
    category: str
    dimensions_cm: Dimensions
    confidence: str = ""
    geometry_ref: str = ""


class Room(Model):
    length_m: Nonnegative | None = None
    width_m: Nonnegative | None = None
    height_m: Nonnegative | None = None
    floor_area_m2: Nonnegative | None = None
    wall_area_m2: Nonnegative | None = None
    shelved_wall_area_m2: Nonnegative | None = None
    floor_area_ft2: Nonnegative | None = None
    wall_area_ft2: Nonnegative | None = None
    openings_area_m2: Nonnegative | None = None
    net_wall_area_m2: Nonnegative | None = None
    scale_method: str = ""
    confidence: Confidence = 0
    shape_method: str = ""
    geometry_ref: str = ""
    floor_polygon_xz: list[list[float]] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)
    shelving_associations: list[dict] = Field(default_factory=list)
    furniture: list[FurnitureMeasurement] = Field(default_factory=list)


class Review(Model):
    ref_id: str
    reason: str


class Totals(Model):
    book_count: int = 0
    books_identified: int = 0
    books_unidentified: int = 0
    books_needs_appraisal: int = 0
    shelf_run_m: Nonnegative | None = None
    books_replacement_cost: Money = Decimal("0")
    books_used_value: Money = Decimal("0")
    items_replacement_cost_low: Money = Decimal("0")
    items_replacement_cost_high: Money = Decimal("0")
    excluded_from_totals: int = 0
    ownership_excluded: int = 0
    replacement_prices_missing: int = 0
    used_prices_missing: int = 0
    currency: str = ""


class ClaimPacket(Model):
    sweep: Sweep
    room: Room = Field(default_factory=Room)
    books: list[Book] = Field(default_factory=list)
    items: list[Item] = Field(default_factory=list)
    totals: Totals = Field(default_factory=Totals)
    review_queue: list[Review] = Field(default_factory=list)
    revision: int = 0
    completion: str = "capturing"
    metrics: dict = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=lambda: [
        "Unknown numeric observations are null; monetary amounts are decimal strings.",
        "Confidence is an uncalibrated model/geometry score, not a probability.",
        "Wall area is gross; openings and shelving coverage are reported separately.",
        "Excluded-from-totals counts unique eligible lines lacking every applicable valuation.",
    ])


class Evidence(Model):
    sha256: str
    size: int
    saved_at: str = Field(default_factory=utcnow)


class LocalProposal(Model):
    label: str = Field(min_length=1,max_length=80)
    confidence: Confidence
    polygon: list[list[Confidence]] = Field(min_length=4,max_length=4)

    @field_validator("polygon")
    @classmethod
    def points_have_two_coordinates(cls, value):
        if any(len(point)!=2 for point in value):
            raise ValueError("Expected normalized 2D coordinates")
        return value


class LocalRecognition(Model):
    model: str = Field(max_length=100)
    detections: list[LocalProposal] = Field(max_length=300)
    sharpness: Nonnegative
    dark_fraction: Confidence
    bright_fraction: Confidence
    fingerprint: str = Field(pattern=r"^[0-9a-f]{16}$")
    inference_ms: Nonnegative


class LocalOutline(Model):
    polygon: list[list[Confidence]] = Field(min_length=4, max_length=4)
    confidence: Confidence

    @field_validator("polygon")
    @classmethod
    def valid_outline(cls, value):
        from shapely.geometry import Polygon
        if any(len(point) != 2 for point in value):
            raise ValueError("Expected normalized 2D coordinates")
        polygon = Polygon(value)
        if not polygon.is_valid or polygon.area <= 0 or not polygon.equals(polygon.convex_hull):
            raise ValueError("Expected a convex ordered object quadrilateral")
        return value


class LocalBookClue(LocalOutline):
    visible_text: str = Field(max_length=4000)
    title: str | None = Field(default=None, max_length=500)
    author: str | None = Field(default=None, max_length=300)
    isbn: str | None = Field(default=None, max_length=40)


class LocalObjectClue(LocalOutline):
    category: str = Field(min_length=1, max_length=80)


class LocalAnalysis(Model):
    source: Literal["apple_foundation_models", "cloud_requested"]
    status: Literal["complete", "partial", "unavailable", "failed", "skipped"]
    books: list[LocalBookClue] = Field(max_length=100)
    objects: list[LocalObjectClue] = Field(max_length=250)
    needs_cloud: StrictBool
    reasons: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(max_length=30)


class FrameMetadata(Model):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    timestamp_s: Nonnegative
    image_width: int = Field(gt=0, le=8192)
    image_height: int = Field(gt=0, le=8192)
    # Raw ARKit camera buffer orientation; no EXIF or portrait rotation applied.
    orientation: Literal["arkit_landscape_right"] = "arkit_landscape_right"
    intrinsics: list[float] = Field(min_length=9, max_length=9)
    camera_transform: list[float] = Field(min_length=16, max_length=16)
    tracking: Literal["normal", "limited", "unavailable"] = "unavailable"
    depth_width: int = Field(default=0, ge=0, le=1024)
    depth_height: int = Field(default=0, ge=0, le=1024)
    ar_session_id: str = Field(min_length=1, max_length=80)
    ocr: list[dict] = Field(default_factory=list, max_length=1000)
    # Optional for old captures. Proposals are untrusted local hints, never inventory facts.
    local_recognition: LocalRecognition | None = None
    local_analysis: LocalAnalysis | None = None
    vision_rotation: Literal[0, 90, 180, 270] = 0

    @field_validator("intrinsics", "camera_transform")
    @classmethod
    def finite_matrices(cls, value):
        import math
        if not all(math.isfinite(x) for x in value):
            raise ValueError("Calibration must be finite")
        return value


class SweepState(Model):
    pricing_jobs: dict[str, dict] = Field(default_factory=dict)
    sweep: Sweep
    room: Room = Field(default_factory=Room)
    books: list[Book] = Field(default_factory=list)
    items: list[Item] = Field(default_factory=list)
    evidence: dict[str, Evidence] = Field(default_factory=dict)
    frames: dict[str, FrameMetadata] = Field(default_factory=dict)
    geometry: dict = Field(default_factory=dict)
    revision: int = 0
    completion: Literal["capturing", "processing", "ready", "partial"] = "capturing"
    review_reasons: list[str] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)
    events: list[dict] = Field(default_factory=list)
    excluded_shelves: list[str] = Field(default_factory=list)
    ar_session_id: str = ""
    shelf_run_m: Nonnegative | None = None
    spatial_tracks: dict = Field(default_factory=dict)
    shelf_geometry: dict = Field(default_factory=dict)
    frame_status: dict[str, str] = Field(default_factory=dict)
    image_fingerprints: dict[str, dict] = Field(default_factory=dict)
    image_receipts: dict[str, dict] = Field(default_factory=dict)
