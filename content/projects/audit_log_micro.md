---
title: Audit Log Microservices
description: A CRUD application where operations don't modify the original row, but create a new version on top of it.
---
## The Idea

A complete **CRUD** application where any defined resource (eg: products, articles, courses) where no operation **CREATE**, **UPDATE** or **DELETE** modifies the original registry. Instead, all changes are created as a new version, like a simplified **Event Sourcing Pattern**.

An **endpoint** `GET /resource/{id}` returns the latest active version. You could consult all consecutive changes made from the inception of the resource. You could even **rollback** to a previous version, and **audit** who changed what.

This would be something of the likes of a **simplified git-like system**.

## Project Stack and Scope

This project will be made using Python and the FastAPI framework. We will use **PostgreSQL** as database and pure SQL to write the tables (I'm working on [something to fix that](https://github.com/emiliano-gandini-outeda/DBWarden)).

**In scope**:
- **Immutable** event log for any resource type (generic, not tied to a specific domain)
- Every write operation **appends** a new event, no row is ever **mutated**
- Current state is always derived from the **latest active event** for a given resource
- Soft-delete via `DELETE` event (mark as `deleted`, no real **row deletion**)
- **PostgreSQL** with two tables: `resources` (for **metadata**) and `resource_events` (**append-only** log of mutations)
- **Rollback** and versioned writes are **atomic**
- Version number is incremental per resource, not global
- **JSON** diffing using JSON Patch (RFC 6902): **UPDATE** events store only the fields that changed, not the full snapshot. Current state is reconstructed by applying patches sequentially from the **CREATE event forward**

After scope is completed: **Compaction**.

**Out of scope:**
- No **frontend** or UI of any kind
- No per-resource-type **schema validation** (data is generic JSONB)
- No **authentication/authorization** implementation: `created_by` is accepted as input, not verified
- No real-time subscriptions or **webhooks** on events
- No multi-tenancy
- No support for **non-PostgreSQL** backends
- No migration tooling or admin **CLI**
## Implementation Details
### Database
#### Table: `resources`

```sql

CREATE TABLE resources (

id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

resource_type VARCHAR(50) NOT NULL, -- example: Course, product, etc

created_at TIMESTAMP NOT NULL DEFAULT now()

);

```

Stores only the identity and type of a resource. No mutable rows here.
#### Table: `resource_events`

```sql

CREATE TABLE resource_events (
    event_id     BIGSERIAL PRIMARY KEY,
    resource_id  UUID NOT NULL REFERENCES resources(id),
    version      INT NOT NULL,
    event_type   VARCHAR(20) NOT NULL,  
    -- 'CREATE' | 'UPDATE' | 'DELETE' | 'ROLLBACK'
    data         JSONB NOT NULL,        
    -- full snapshot on CREATE/ROLLBACK, JSON Patch array on UPDATE
    created_by   UUID NOT NULL,
    created_at   TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE (resource_id, version)
);

CREATE INDEX idx_resource_events_resource_version 
    ON resource_events (resource_id, version);
    
```

**Data column shapes**

Every `event_type` stores a JSON Patch array in `data`, no exceptions:

- `CREATE`: patch from empty object to initial state — `[{ "op": "add", "path": "/name", "value": "..." }, ...]`
- `UPDATE`: patch from previous state to new state
- `DELETE`: `[]` — empty patch, the event itself signals deletion
- `ROLLBACK`: patch from current state back to the target version's reconstructed state

#### Relationships
```
resources 1 ──── N resource_events
		(resource_id FK)
```

One resource has many events. The current state of a resource is never stored explicitly, it's always derived.

### Endpoints

`POST /{resource_type}`

- Inserts a row in `resources`
- Inserts a `CREATE` event in `resource_events` with `version=1`, full snapshot in `data`
- Both writes in a **single transaction**
- Returns the resource `id` and the initial state

---

`GET /{resource_type}/{id}`

- Queries the latest non-DELETE event for that `resource_id`
- If latest event is `DELETE`, returns 404 (or a tombstone response)
- Returns the reconstructed **current state**

State reconstruction logic: Every state is rebuilt by **replaying all patches** from `version=1` forward.
- Start with `{}`
- Apply each event's patch in version order
- Stop at the latest version (or at `?at=` timestamp)

---

`PUT /{resource_type}/{id}`

- Fetches current state to validate the resource exists and is **not deleted**
- Computes the **JSON Patch diff** between current state and the incoming payload
- Appends an `UPDATE` event with `version = max(version) + 1` and the diff as `data`
- **Atomic**: version increment and insert in one transaction (use `SELECT ... FOR UPDATE` or a CTE to avoid races)

---

`DELETE /{resource_type}/{id}`

- Appends a `DELETE` event with `version = max(version) + 1` and `data = {}`
- No rows are removed from either table
- Subsequent `GET` returns 404

---

`GET /{resource_type}/{id}/history`

- Returns all events for the resource in version order
- Each row includes: `event_id`, `version`, `event_type`, `data`, `created_by`, `created_at`
- Optional query params: `?from_version=`, `?to_version=`, `?event_type=`

---

`POST /{resource_type}/{id}/rollback/{version}`

- Reconstructs current state by replaying all patches from `version=1` to latest
- Reconstructs target state by replaying all patches from `version=1` to the specified version
- Computes the **JSON Patch diff** between current state and target state
- Appends a `ROLLBACK` event with that diff and `version = max(version) + 1`
- The resource is now back to that state, but history is preserved: the rollback itself is just another patch event

---

`GET /{resource_type}/{id}?at=<timestamp>`

- Fetches all events where `created_at <= timestamp`, in version order
- Replays all patches from `version=1` up to that cutoff
- Returns the reconstructed state at that point in time
- If no events exist before the timestamp, returns 404
- Useful for auditing the exact state of a resource at any past moment

### Compaction

Without **snapshots**, compaction becomes critical for **read performance**. Without it, a resource with **10,000 events** requires applying **10,000 patches** on every `GET`.

Compaction strategy:

- Every N events, reconstruct the full state by **replaying all patches** up to event N
- Replace events `1..N` with a single `CREATE` event whose patch builds the full state from `{}`
- All events after N remain as-is
- The compacted event is distinguishable (e.g. add a `compacted: true` flag or a separate `event_type = 'SNAPSHOT'`)

## Docker and Docker Compose

This project will also work as the introduction of **Docker** and **Docker Compose**, one of the most used tools **in the world** by devs and enterprises.

The service runs as a single FastAPI **container**, using `uv` for dependency management and a slim **Python base image**. Docker Compose ties together the FastAPI container and a **PostgreSQL service**, connecting them over a shared internal network with the database URL passed as an environment variable. This is the standard, minimal setup you'll see in almost every real backend project.

### Dockerfile
#### What is a Dockerfile?

A `Dockerfile` is a plain text recipe that tells **Docker** how to build an **image** for your app. It defines the base OS, how to install **dependencies**, copy your source code, and what command to run when the container starts. You build it once, and it produces a 
**reproducible, portable image** that runs the same **everywhere**.

```Dockerfile
FROM python:3.13-slim

# Copy the uv binary from the official image instead of installing it via pip
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

# Copy source code
COPY . .

# Install all dependencies from the lockfile into the system Python
RUN uv sync --frozen --no-dev

# Add the virtualenv to PATH so uvicorn is available directly
ENV PATH="/app/.venv/bin:$PATH"

# Start the FastAPI app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Docker Compose

#### What is a docker-compose.yml?

A `docker-compose.yml` is the file that orchestrates **multiple containers together**. Instead of running each container manually with long `docker run` commands, you declare all your **services** (API, database, etc.), their **configuration**, **environment variables**, shared **networks**, and **volumes** in one file, then bring everything up with a single `docker compose up`.

```yml
services:
  api:
    build: .    # build the image from the Dockerfile in this directory
    ports:
      - "8000:8000"  # expose the API to your machine at localhost:8000
    environment:
      DATABASE_URL: postgresql+asyncpg://postgres:postgres@db:5432/auditlog
    depends_on:
      db:
        condition: service_healthy  
        # don't start the API until postgres is ready
    networks:
      - backend

  db:
    image: postgres:16-alpine  
    # use the official postgres image, no Dockerfile needed
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: auditlog
    volumes:
      - pgdata:/var/lib/postgresql/data   
        # persist database data between restarts
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]   
      # how Docker knows postgres is ready
      interval: 5s
      timeout: 5s
      retries: 5
    networks:
      - backend

volumes:
  pgdata: # named volume so data survives container restarts and rebuilds

networks:
  backend:    
  # shared private network so api and db can talk to each other by service name
```

Two services: `api` (your FastAPI app, built from the **Dockerfile**) and `db` (Postgres, pulled directly from Docker Hub). They communicate over a **private network**, that's why the `DATABASE_URL` uses `db` as the **hostname** instead of `localhost`. The **named volume** `pgdata` makes sure your database data isn't lost when you **restart** or **rebuild** the containers.

## Applied Concepts
**Event Sourcing (simplified)** 
State is never stored directly. Every change is an event, and the current state is always derived by replaying the event log. This is the core architectural pattern of the entire project.

**Immutability** 
No row is ever updated or deleted at the database level. The log is append-only by design, which makes the system naturally auditable.

**JSON Patch (RFC 6902)** 
Instead of storing full snapshots, only the diff between states is stored. This requires understanding patch operations (`add`, `remove`, `replace`, `move`, `copy`, `test`) and how to apply and compute them programmatically.

**State Reconstruction** 
Reading current state is not a simple `SELECT`. It requires replaying an ordered sequence of patches from `{}` forward.

**Optimistic Concurrency / Version Control** 
Version numbers are incremental per resource. A `UNIQUE (resource_id, version)` constraint plus `SELECT FOR UPDATE` prevents two concurrent writes from producing the same version, acting as an optimistic lock.

**Soft Delete** 
Deletion is a logical concept expressed as an event, not a physical operation. The data is never lost and can be restored via rollback.

**Temporal Queries** 
The `?at=timestamp` query requires reconstructing state at an arbitrary point in the past, a direct consequence of having a full event history with timestamps.

**Compaction** 
A space-time tradeoff: replaying thousands of patches on every read is expensive, so periodically the history is collapsed into a single synthetic event. This is a classic optimization in event-sourced systems.

**Atomic Writes with CTEs** 
Version increment and event insert happen in a single SQL statement using a `WITH ... FOR UPDATE` CTE, avoiding race conditions without application-level locking.

**Docker and Docker Compose**
Docker **packages** your application and everything it needs to run (Python, dependencies, config) into an **isolated container** that behaves the **same** on **any machine**. Docker Compose takes it a step further by letting you define and run **multiple containers together**, in this project, the FastAPI service and PostgreSQL, wiring them up with a **shared network**, environment variables, and persistent **volumes**, all from a single `docker-compose.yml` file. This is the standard local **development and deployment** setup you'll encounter in virtually every real backend project.