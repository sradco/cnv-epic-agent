"""Tests for schemas.stories data contracts."""

from schemas.stories import (
    StoryPayload,
    STORY_JSON_SCHEMA,
    SP_ESTIMATION_JSON_SCHEMA,
)


class TestStoryPayload:
    def test_dataclass_fields(self):
        story = StoryPayload(
            category="metrics",
            summary="Add metrics",
            description="Description",
        )
        assert story.category == "metrics"
        assert story.summary == "Add metrics"
        assert story.story_points is None

    def test_story_points_field(self):
        story = StoryPayload(
            category="docs",
            summary="Update docs",
            description="Desc",
            story_points=3,
        )
        assert story.story_points == 3
        assert story.category == "docs"


class TestJSONSchema:
    def test_schema_has_required_fields(self):
        assert "properties" in STORY_JSON_SCHEMA
        assert "stories" in STORY_JSON_SCHEMA["properties"]
        items = STORY_JSON_SCHEMA["properties"]["stories"]["items"]
        assert "category" in items["properties"]
        assert "summary" in items["properties"]
        assert "description" in items["properties"]
        assert "story_points" in items["properties"]

    def test_schema_category_includes_docs_qe(self):
        items = STORY_JSON_SCHEMA["properties"]["stories"]["items"]
        cat_enum = items["properties"]["category"]["enum"]
        assert "docs" in cat_enum
        assert "qe" in cat_enum
        assert "metrics" in cat_enum

    def test_schema_story_points_is_integer(self):
        items = STORY_JSON_SCHEMA["properties"]["stories"]["items"]
        sp = items["properties"]["story_points"]
        assert sp["type"] == "integer"


class TestSPEstimationSchema:
    def test_schema_has_estimates(self):
        assert "properties" in SP_ESTIMATION_JSON_SCHEMA
        assert "estimates" in SP_ESTIMATION_JSON_SCHEMA["properties"]

    def test_estimate_items_have_required_fields(self):
        items = SP_ESTIMATION_JSON_SCHEMA["properties"]["estimates"]["items"]
        assert "issue_key" in items["properties"]
        assert "story_points" in items["properties"]
        assert "rationale" in items["properties"]
        assert items["required"] == [
            "issue_key", "story_points", "rationale",
        ]
