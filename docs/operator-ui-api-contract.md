# Operator UI API Contract

## Purpose

This document defines the current API contract for the future React/Next.js
operator interface and highlights the missing endpoints required for operator
UI v1.

The backend is already tenant-scoped. Every business-facing operator request
must be made in the context of a single `business_id`.

## Authentication Model

Base path: `/api/v1/`

Auth flow:

1. `POST /api/v1/auth/token/`
2. `POST /api/v1/auth/token/refresh/`
3. `GET /api/v1/auth/me/`
4. `GET /api/v1/memberships/`
5. frontend chooses the active business from memberships
6. all business-scoped requests use `/api/v1/businesses/<business_id>/...`

JWT is the only contract the future operator UI should rely on. Django admin
is temporary operational tooling and is not part of the product contract.

## Roles

Membership roles:

- `owner`
- `admin`
- `staff`

Current API permissions:

- `auth/me/` and `memberships/` require authentication
- business-scoped booking and outbound endpoints currently allow
  `staff` and above

## Current Endpoints

### `POST /api/v1/auth/token/`

Request:

```json
{
  "username": "owner",
  "password": "StrongPass123!"
}
```

Response:

```json
{
  "refresh": "jwt-refresh-token",
  "access": "jwt-access-token"
}
```

### `POST /api/v1/auth/token/refresh/`

Request:

```json
{
  "refresh": "jwt-refresh-token"
}
```

Response:

```json
{
  "access": "jwt-access-token"
}
```

### `GET /api/v1/auth/me/`

Response:

```json
{
  "id": 1,
  "username": "owner",
  "email": "owner@example.com",
  "is_staff": false
}
```

### `GET /api/v1/memberships/`

Response:

```json
[
  {
    "id": 10,
    "role": "owner",
    "is_active": true,
    "business": {
      "id": 1,
      "name": "Barber House",
      "brand_name": "Urban Flow",
      "city": "Алматы",
      "address": "Розыбакиева 247а",
      "working_hours": "10:00-20:00",
      "timezone_name": "Asia/Almaty",
      "is_active": true
    }
  }
]
```

Frontend use:

- business switcher
- role-aware navigation
- initial tenant bootstrap

### `GET /api/v1/businesses/<business_id>/bookings/`

Response:

```json
{
  "count": 1,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 101,
      "business_id": 1,
      "business_name": "Barber House",
      "client_id": 5,
      "client_name": "Adil",
      "master_id": 2,
      "master_name": "Ivan Petrov",
      "service_id": 7,
      "service_name": "Haircut",
      "start_time": "2026-04-25T11:00:00+05:00",
      "end_time": "2026-04-25T12:15:00+05:00",
      "status": "confirmed",
      "notes": "",
      "client_data": {
        "name": "Adil"
      },
      "created_at": "2026-04-24T14:22:00+05:00",
      "updated_at": "2026-04-24T14:22:00+05:00"
    }
  ]
}
```

Notes:

- list is tenant-scoped by URL and permissions
- response is already UI-friendly and does not expose raw nested objects

### `POST /api/v1/businesses/<business_id>/bookings/`

Request:

```json
{
  "client": 5,
  "master": 2,
  "service": 7,
  "start_time": "2026-04-25T11:00:00+05:00",
  "status": "pending",
  "client_data": {
    "name": "Adil"
  },
  "notes": "First visit"
}
```

Response:

- returns the same shape as booking read serializer

### `GET /api/v1/businesses/<business_id>/bookings/<pk>/`

Response:

- same shape as booking list item

Tenant behavior:

- wrong `business_id` or foreign `pk` returns `404`

### `PATCH /api/v1/businesses/<business_id>/bookings/<pk>/reschedule/`

Request:

```json
{
  "master": 2,
  "start_time": "2026-04-25T13:00:00+05:00"
}
```

Response:

- same shape as booking read serializer

### `PATCH /api/v1/businesses/<business_id>/bookings/<pk>/status/`

Request:

```json
{
  "status": "confirmed"
}
```

Allowed status values:

- `pending`
- `confirmed`
- `cancelled`
- `no_show`
- `needs_attention`

Response:

- same shape as booking read serializer

### `GET /api/v1/businesses/<business_id>/outbound-messages/`

Response:

```json
{
  "count": 1,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 9001,
      "business": 1,
      "business_name": "Barber House",
      "channel": "whatsapp",
      "recipient": "+77071234567",
      "message_type": "reminder",
      "status": "failed",
      "provider_message_id": "",
      "error_code": "timeout",
      "attempts": 2,
      "submitted_at": null,
      "delivered_at": null,
      "dead_lettered_at": null,
      "created_at": "2026-04-25T09:30:00+05:00"
    }
  ]
}
```

Outbound status values:

- `queued`
- `submitted`
- `delivered`
- `failed`
- `cancelled`
- `dead_letter`

Frontend use:

- delivery monitor
- failed/dead-letter lists

## Current Strengths

Already good enough for frontend integration:

- JWT auth exists
- memberships expose business switcher data
- booking flow is split into explicit use cases:
  - create
  - retrieve
  - reschedule
  - status update
- outbound delivery list already exists
- tenant isolation is enforced by:
  - URL scope
  - permission layer
  - scoped queryset
  - serializer validation
  - service layer checks

## Missing Endpoints For Operator UI V1

These are the endpoints that should be added before the React/Next operator UI
can replace Django admin for salon owners.

### P0: Business Context

#### `GET /api/v1/businesses/<business_id>/`

Needed for:

- business header
- timezone
- working hours
- branding

This endpoint should not expose internal AI configuration. `ai_rules` belongs
to the future business settings endpoint.

### P0: Clients

#### `GET /api/v1/businesses/<business_id>/clients/`

Needed for:

- client search
- repeat visitors
- booking creation flow

Minimum response fields:

- `id`
- `name`
- `phone`
- `telegram_id`
- `whatsapp_id`
- `is_active`
- `allow_follow_up`
- `created_at`

Recommended query params:

- `search`
- `phone`
- `page`

#### `GET /api/v1/businesses/<business_id>/clients/<pk>/`

Needed for:

- client profile panel
- booking history
- message troubleshooting

### P0: Services And Masters

#### `GET /api/v1/businesses/<business_id>/services/`
#### `GET /api/v1/businesses/<business_id>/masters/`

Needed for:

- booking form options
- filters
- schedules

### P0: Availability

#### `GET /api/v1/businesses/<business_id>/availability/`

Suggested query params:

- `date`
- `service_id`
- `master_id` optional

Needed for:

- operator-side manual booking flow
- parity with AI slot search

### P0: Outbound Recovery

#### `POST /api/v1/businesses/<business_id>/outbound-messages/<pk>/retry/`
#### `POST /api/v1/businesses/<business_id>/outbound-messages/<pk>/resend/`

Needed for:

- replacing temporary Django admin actions
- failed/dead-letter recovery in UI

These should mirror the semantics already used in admin:

- `retry` only for `failed`
- `resend` for `failed`, `dead_letter`, `cancelled`

### P1: Dashboard

#### `GET /api/v1/businesses/<business_id>/dashboard/summary/`

Suggested metrics:

- bookings today
- confirmed today
- pending follow-ups
- `needs_attention` count
- outbound failed count
- outbound dead-letter count

### P1: Handoff Queue

#### `GET /api/v1/businesses/<business_id>/handoffs/`

Could initially be backed by `Booking(status="needs_attention")` plus latest
message context.

Needed for:

- operator queue
- AI escalation review

### P1: Conversation Timeline

#### `GET /api/v1/businesses/<business_id>/clients/<pk>/conversation/`

Needed for:

- message history
- AI troubleshooting
- handoff context

### P1: Settings

#### `GET /api/v1/businesses/<business_id>/settings/`
#### `PATCH /api/v1/businesses/<business_id>/settings/`

Needed for:

- AI rules
- follow-up policy
- working hours
- messaging preferences

## Recommended Frontend Screens

Given the current API, these screens can already start:

1. Login
2. Business switcher
3. Booking list
4. Booking detail
5. Booking reschedule flow
6. Booking status update flow
7. Outbound delivery list

These screens still need backend support:

1. Client list and client detail
2. Manual booking form with master/service catalogs
3. Handoff queue
4. Dashboard summary
5. Delivery retry/resend screen
6. Settings

## Backend Task List Derived From This Contract

Recommended backend order:

1. `GET /businesses/<id>/`
2. `GET /businesses/<id>/clients/` and detail
3. `GET /businesses/<id>/masters/`
4. `GET /businesses/<id>/services/`
5. `GET /businesses/<id>/availability/`
6. `POST /businesses/<id>/outbound-messages/<id>/retry/`
7. `POST /businesses/<id>/outbound-messages/<id>/resend/`
8. `GET /businesses/<id>/dashboard/summary/`
9. `GET /businesses/<id>/handoffs/`
10. `GET /businesses/<id>/clients/<id>/conversation/`

## Non-Goals For V1

Not required before the first operator UI iteration:

- replacing Django admin completely
- real-time websockets
- multi-business requests in one API call
- deeply nested serializers
- public API version `v2`
