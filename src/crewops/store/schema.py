"""The single DynamoDB table: attribute names and key layout.

Everything lives in one table. Each item has a partition key (``pk``) and sort key (``sk``),
and up to two overloaded secondary indexes for the access patterns a key lookup cannot
serve. The infrastructure stack and the data-access code both import these names, so the
table definition and the code that reads it cannot drift apart.

Key layout (``PREFIX#value``):

==============================  =========================  ===============================
Item                            pk / sk                    gsi1pk / gsi1sk
==============================  =========================  ===============================
Crew profile                    CREW#id / PROFILE          BASE#station / RANK#rank#id
Certification                   CREW#id / CERT#type
Duty clock (with history)       CREW#id / CLOCK
Reserve duty                    CREW#id / RESERVE#base
Risk signal                     CREW#id / RISK
Roster exception                CREW#id / EXCEPTION#date#rule
Flight                          FLIGHT#id / META           DATE#yyyy-mm-dd / DEP#utc#id
Pairing (days and crew seats)   PAIRING#id / META          AIRCRAFT#tail / DATE#first-day
Gate slot                       GATE#gate / SLOT#start#id  FLIGHT#id / GATE
Rule, cost table                CONFIG / RULE#id, COSTS
Disruption                      DISRUPTION#id / META       STATUS#status / AT#reported#id
Plan                            DISRUPTION#id / PLAN#id    PLAN#status / AT#submitted
Audit event                     DISRUPTION#id / AUDIT#ts#action
Rejected report                 REPORT#id / REJECTED
User profile                    USER#username / PROFILE
Roster version                  CREW#id / ROSTER_VERSION
==============================  =========================  ===============================

The operational records above are written by ``crewops.store.items``; disruptions, audit
events and rejected reports by ``crewops.store.reports``; plans and their decisions by
``crewops.store.plans``; user profiles by ``crewops.store.users``. A disruption is stored
exactly once by a conditional write on its id, so no separate idempotency marker is needed.
``gsi2`` is reserved for a second overloaded view (for example a controller's desk), so it
can be added without recreating the table.
"""

from __future__ import annotations

PK = "pk"
SK = "sk"
GSI1 = "gsi1"
GSI1_PK = "gsi1pk"
GSI1_SK = "gsi1sk"
GSI2 = "gsi2"
GSI2_PK = "gsi2pk"
GSI2_SK = "gsi2sk"
TTL = "expires_at"
"""Epoch seconds. Items with this attribute are deleted by DynamoDB after that time."""

SEPARATOR = "#"
