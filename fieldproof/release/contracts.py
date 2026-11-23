from typing import Annotated, Literal

from pydantic import Field, StrictInt, StringConstraints

from fieldproof.design.contracts import Input, Key, ShortText

Desk = Literal["field", "methods"]
Outcome = Literal["advance", "hold"]
Cut = Annotated[StrictInt, Field(ge=0, le=2**63 - 1)]
EvidenceId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class FreezeEvidence(Input):
    expected_cut: Cut


class Review(Input):
    desk: Desk
    assessment: Literal["endorse", "object"]
    reviewer: Key
    note: ShortText


class IssueBulletin(Input):
    evidence_id: EvidenceId
    expected_edition: Annotated[StrictInt, Field(ge=0, le=2**63 - 1)]
    outcome: Outcome
    note: ShortText
