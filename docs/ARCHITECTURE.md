# Architecture and request flow

## Live deployment

```mermaid
flowchart LR
    Client[Reviewer / Postman / burst client] -->|HTTPS| Edge[Cloudflare proxy<br/>seats.algocrafter.in]
    Edge -->|HTTPS| Caddy[Existing Caddy<br/>new hostname route]
    Caddy --> API[FastAPI<br/>seat-reservation-api]
    API -->|private database network| PG[(PostgreSQL 17<br/>dedicated persistent volume)]
    API --> Logs[JSON stdout + bounded admin log view]
    PG --> Metrics[Committed-state metrics]
    Metrics --> API
    Caddy -->|other hostname routes| Existing[Other application<br/>currently stopped by owner]
```

Only the reverse proxy network is shared. The reservation database has no published
host port. A loopback API port is available for operator checks. Dedicated container
CPU and memory limits contain resource use; the VM and Caddy remain shared infrastructure.
There is one API process in this deployment. PostgreSQL coordinates correctness if
additional API instances are introduced.

## Reservation transaction

```mermaid
sequenceDiagram
    participant C as Client
    participant A as API
    participant D as PostgreSQL
    C->>A: seats + idempotency key + user JWT
    A->>A: Queue admission, validate identity and body
    A->>D: BEGIN; claim unique user/operation/key
    alt Key already committed
        A->>D: Compare canonical body and read saved response
        D-->>A: Original result or body conflict
        A->>D: Record replay/conflict outcome; COMMIT
        A-->>C: 200 replay or saved decline / 409 conflict
    else New key
        A->>D: Lock user/show quota row
        A->>D: Lock requested seat rows in sorted order
        alt Limit and all seats available
            A->>D: Insert reservation; update seats and quota
            A->>D: Save original response; COMMIT
            A-->>C: 201 confirmed
        else Domain rule fails
            A->>D: Save decline; COMMIT without inventory change
            A-->>C: 409 conflict or 404 unknown resource
        end
    end
    A->>A: Correlated structured completion log
```

The unique idempotency row serializes the same key; the quota row serializes one
user's total; seat row locks serialize ownership decisions. All are in one transaction.
Different unrelated users and seats can progress concurrently. The Python admission
semaphore limits memory consumption; it does not decide seat ownership.

## Owner cancellation

```mermaid
flowchart TD
    R[Authenticated cancel request] --> O{Token user owns reservation?}
    O -->|No| F[403 / no mutation]
    O -->|Yes| Q[Lock quota, then reservation]
    Q --> S{Already cancelled?}
    S -->|Yes| I[Return existing cancelled result]
    S -->|No| L[Lock seat rows in deterministic order]
    L --> U[Release only seats still owned by this reservation]
    U --> T[Restore quota and mark cancelled in same transaction]
    T --> C[Commit and return 200]
```

PostgreSQL is the authority during failures. Database loss fails readiness and blocks
new mutations. API restart restores no local ownership state because ownership and
idempotency were committed durably. A response lost after commit is resolved by a retry.
