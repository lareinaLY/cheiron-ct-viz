"""Aggregation results passed from `aggregate/` to `spec/`."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ctviz.schemas.citation import Citation
from ctviz.schemas.plan import Dimension, Relation
from ctviz.schemas.response import NetworkEdge, NetworkNode


class Bucket(BaseModel):
    """Trials sharing one value of a dimension.

    `key` is the grouping identity (e.g. a MeSH id); `label` is the display text
    (e.g. the MeSH term). They are equal for most dimensions.
    """

    key: str
    label: str
    count: int
    estimated_count: int = Field(default=0, description="Trials here whose date is ESTIMATED.")
    incomplete: bool = Field(default=False, description="Period still in progress at as_of.")
    is_unknown: bool = Field(default=False, description="Trials missing this value.")
    citations: list[Citation] = Field(default_factory=list)
    citation_count_total: int = Field(description="True number of citations before the cap.")


class GroupResult(BaseModel):
    """Output of `aggregate.group_by`.

    `buckets_overlap` is True when one trial can count in several buckets
    (e.g. a multi-country trial), so the bucket counts can add up to more than the
    number of trials. `assumptions` and `warnings` are copied into the response meta.
    """

    dimension: Dimension
    buckets: list[Bucket]
    buckets_overlap: bool
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Graph(BaseModel):
    """Output of `aggregate.build_network`.

    `assumptions` and `warnings` are copied into the response meta by the spec stage.
    """

    relation: Relation
    nodes: list[NetworkNode]
    edges: list[NetworkEdge]
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
