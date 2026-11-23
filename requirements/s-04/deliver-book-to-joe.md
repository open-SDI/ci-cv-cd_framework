---
schema_version: sdi.mobility-requirements-specification/v1
document_version: 1
requirement_ids:
  - secure-book
  - accept-spoken-request
  - resolve-rendezvous
  - navigate-to-rendezvous
  - verify-recipient-face
  - transfer-book
  - acknowledge-receipt
  - return-to-origin
---

# Deliver a book to Joe — Mobility Requirements Specification

## 1. Introduction

### 1.1 Purpose

This specification states what the S-04 delivery mission requires of a
mobility target, so that CI can assess which services cover each
requirement and CV can plan how each requirement is verified. It plans
verification; it does not claim that verification occurred.

### 1.2 Scope

A mobility target carries one book from its origin to the recipient named
in a spoken request, at the rendezvous pose that a supplied mapping gives
for that recipient. After the target verifies the recipient's face, the
recipient takes the book by hand and confirms receipt with a predefined
spoken phrase. The target then returns to its origin.

Every requirement in section 4 is a mission obligation: a proposal or a
model refinement may not delete or weaken one. Which services, bindings
and hosts provide them is decided per target profile.

Out of scope: speaker authentication, independent proof that the physical
transfer happened, loading or securing the book by the robot, and any
runtime, readiness or performance claim.

### 1.3 Definitions

- **Mobility target**: the robot and any edge host that run the mission,
  as described by the target profile.
- **Origin**: the identified pose where the mission starts and where the
  target returns.
- **Recipient**: the person named in the spoken request. In this mission
  the intended recipient is Joe.
- **Rendezvous mapping**: a supplied table from recipient to a meeting
  pose in the committed navigation environment. For Joe the meeting pose
  is `joe-dropoff`. The mapping is an input, not something the target
  infers.
- **Enrolled recipient**: a recipient whose reference face is registered,
  with their consent, before the mission.
- **Receipt acknowledgment**: one of a predefined set of spoken phrases
  confirming that the book was received.
- **Orchestrator**: the delivery-specific component that sequences the
  steps below, relays between services and supplies navigation goals and
  the initial pose. It is carried alongside the composed services, not
  chosen among them.

## 2. Overall description

### 2.1 System context

The mobility target carries the book and provides the drive base,
sensors, microphone and compute that the target profile describes. The
orchestrator runs the mission sequence. A human secures the book before
departure and the recipient takes it at the rendezvous.

### 2.2 Stakeholders

- **Delivery operator**: secures the book, starts the mission and
  recovers the target.
- **Joe (recipient)**: receives the book in person.

### 2.3 Assumptions

- The rendezvous mapping is supplied and covers the requested recipient.
- The recipient is enrolled before the mission.
- The origin and every rendezvous pose lie in the committed navigation
  environment.

### 2.4 Exclusions

- The spoken request and the receipt acknowledgment are not attributed to
  a verified speaker; no speaker authentication is implied.
- A receipt acknowledgment is a spoken statement, not independent proof
  that the book changed hands.

### 2.5 Open prerequisites

These are needed to execute and verify the mission, not to choose
services. They stay open rather than defaulted.

| Prerequisite | Status |
|---|---|
| Arrival radius around a rendezvous or origin pose | 0.25 m placeholder; not accepted |
| Rendezvous mapping content, map and frames | To be supplied |
| Origin pose | To be supplied |
| Book carrying and transfer provisions | To be supplied |
| Face verification criteria | To be supplied |
| Receipt acknowledgment phrases | To be supplied |
| Step timeouts and retry limits | To be supplied |
| Cancellation and safe failure | To be supplied |
| Return policy after a failed delivery | To be supplied |

## 3. Stakeholder needs

- **N1 receive-book**: Joe receives the book in person. *(Joe)*
- **N2 recover-mobility-target**: the target is back at its origin when
  the mission ends. *(Delivery operator)*

## 4. Requirements

All requirements are functional, have priority **must** and are mission
obligations. Delivery requirements trace to N1 and the return
requirement to N2, so delivery and return are separately traceable: a
successful return does not make a failed delivery succeed, and a failed
return does not erase an acknowledged delivery. A step does not proceed
while a requirement it depends on is unmet.

"Allocated to" names who provides each requirement. A requirement
allocated only to the orchestrator or a human has no composed service
that could provide it.

| Requirement | Allocated to | Traces to | Depends on |
|---|---|---|---|
| secure-book | delivery operator | N1 | — |
| accept-spoken-request | services (speech recognition), orchestrator | N1 | secure-book |
| resolve-rendezvous | orchestrator | N1 | accept-spoken-request |
| navigate-to-rendezvous | services | N1 | resolve-rendezvous |
| verify-recipient-face | services, delivery operator (enrollment) | N1 | navigate-to-rendezvous |
| transfer-book | orchestrator, recipient | N1 | verify-recipient-face |
| acknowledge-receipt | services (speech recognition), orchestrator | N1 | transfer-book |
| return-to-origin | services | N2 | acknowledge-receipt |

### 4.1 Delivery

#### secure-book

**Shall:** A human shall secure the book on the mobility target before it
departs the origin; the mobility target does not load or secure the book.

**Rationale:** Securing the book is a human step in the approved mission.

**Verification (demonstration):** The book is secured before departure.

#### accept-spoken-request

**Shall:** The mobility target shall take the recipient only from a
spoken request that names them. When the request names no recipient, or
one it cannot recognise, the mobility target shall not substitute Joe or
any other recipient.

**Rationale:** The approved mission names the recipient by voice; a
silently substituted recipient defeats the request.

**Verification (test):** A spoken request naming Joe yields Joe as the
recipient; an unrecognised request yields no recipient.

#### resolve-rendezvous

**Shall:** The mobility target shall take the rendezvous pose for the
named recipient from the supplied rendezvous mapping, and shall not
depart when the recipient is absent from the mapping.

**Rationale:** The meeting place comes from a supplied mapping, not from
a guess or a default.

**Verification (test):** Joe yields `joe-dropoff`; an unmapped recipient
yields no navigation goal.

#### navigate-to-rendezvous

**Shall:** The mobility target shall navigate from the origin to within
the arrival radius of the rendezvous pose.

**Rationale:** The hand-over happens at the rendezvous.

**Verification (test):** The final pose is within the arrival radius of
the rendezvous pose.

#### verify-recipient-face

**Shall:** At the rendezvous, the mobility target shall verify by face
recognition that the person present is the enrolled recipient named in
the request, using only reference faces enrolled with consent.

**Rationale:** The book is meant for one person, and face data is
personal data.

**Verification (test):** A matching enrolled face yields a verified
recipient; a non-matching or unenrolled face does not.

#### transfer-book

**Shall:** After the recipient's face is verified, the mobility target
shall stay at the rendezvous while the recipient takes the book by hand.

**Rationale:** The transfer is a human step in the approved mission.

**Verification (demonstration):** The target does not leave the
rendezvous between verification and the receipt acknowledgment.

#### acknowledge-receipt

**Shall:** After the hand-over, the mobility target shall recognise a
predefined spoken receipt acknowledgment and record that receipt was
acknowledged.

**Rationale:** The acknowledgment closes the delivery.

**Verification (test):** A predefined phrase is recorded as acknowledged;
an unrelated utterance is not.

### 4.2 Return

#### return-to-origin

**Shall:** After receipt is acknowledged, the mobility target shall
navigate back to within the arrival radius of the origin.

**Rationale:** The approved mission ends with the target back at its
origin.

**Verification (test):** The final pose is within the arrival radius of
the origin.
