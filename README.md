# Smartschool API

A Flask-based API wrapper around the unofficial `smartschool` Python library. It exposes endpoints to read the Smartschool planner, fetch element details, and perform actions such as resolving, unresolving, trashing, and creating to-dos.

## Base URL

```
https://smartschoolbridge.onrender.com
```

## Authentication

All endpoints require Smartschool credentials, passed either as query parameters (GET) or as a JSON body (POST). The following fields are required:

| Field | Type | Description |
|---|---|---|
| `username` | string | Smartschool username |
| `password` | string | Smartschool password |
| `main_url` | string | School domain without protocol (e.g. `stjozefasoessen.smartschool.be`) |
| `mfa` | string | Date of birth in `YYYY-MM-DD` format |

## Endpoints

### GET /ping

Health check endpoint. Returns HTTP 200 with a static response. Intended for uptime monitors.

**Response**

```json
{
  "status": "ok"
}
```

---

### GET /planner

Returns all planner elements with full details, including description, attachments, and weblinks.

**Query parameters**

| Parameter | Required | Description |
|---|---|---|
| `username` | yes | Smartschool username |
| `password` | yes | Smartschool password |
| `main_url` | yes | School domain |
| `mfa` | yes | Date of birth |

**Response**

```json
{
  "status": "success",
  "count": 12,
  "data": [
    {
      "id": "d4090265-5714-4c12-9a4c-049b33bb7194",
      "platform_id": 455,
      "name": "Example task",
      "courses": ["Physics"],
      "type": "planned-to-dos",
      "status": "unresolved",
      "color": "tangerine-200",
      "icon": "icon_fill_flag",
      "pinned": false,
      "unconfirmed": false,
      "sort": "20260930000000_9__Example task",
      "period": {
        "date_time_from": "2026-09-30T00:00:00+02:00",
        "date_time_to": "2026-09-30T23:59:59+02:00",
        "whole_day": true,
        "deadline": false
      },
      "public_info_html": "<p>Description</p>",
      "public_info_text": "Description",
      "attachments": [],
      "weblinks": []
    }
  ]
}
```

---

### GET /planner/summary

Returns all planner elements without details. Faster than `/planner` because it does not fetch the detail endpoint for each element.

**Query parameters**

Same as `/planner`.

**Response**

Same structure as `/planner`, but without `public_info_html`, `public_info_text`, `attachments`, and `weblinks`.

---

### GET /element

Returns the details of a single planner element.

**Query parameters**

| Parameter | Required | Description |
|---|---|---|
| `username` | yes | Smartschool username |
| `password` | yes | Smartschool password |
| `main_url` | yes | School domain |
| `mfa` | yes | Date of birth |
| `id` | yes | Element UUID |
| `platform_id` | yes | Platform ID (e.g. `455`) |
| `type` | no | `planned-to-dos` or `planned-assignments` (default: `planned-to-dos`) |

**Response**

```json
{
  "status": "success",
  "data": {
    "id": "d4090265-5714-4c12-9a4c-049b33bb7194",
    "platformId": 455,
    "name": "Example task",
    "publicInfo": "<p>Description</p>",
    "public_info_text": "Description",
    "period": {
      "dateTimeFrom": "2026-09-30T00:00:00+02:00",
      "dateTimeTo": "2026-09-30T23:59:59+02:00",
      "wholeDay": true,
      "deadline": false
    },
    "attachments": [],
    "weblinks": [],
    "resolvedStatus": "unresolved",
    "plannedElementType": "planned-to-dos"
  }
}
```

---

### POST /element/resolve

Marks an element as resolved.

**Body parameters**

| Parameter | Required | Description |
|---|---|---|
| `username` | yes | Smartschool username |
| `password` | yes | Smartschool password |
| `main_url` | yes | School domain |
| `mfa` | yes | Date of birth |
| `id` | yes | Element UUID |
| `platform_id` | yes | Platform ID |
| `type` | no | `planned-to-dos` or `planned-assignments` (default: `planned-to-dos`) |

**Response**

```json
{
  "status": "success",
  "action": "resolve",
  "http_status": 200,
  "result": null
}
```

---

### POST /element/unresolve

Marks a resolved element as unresolved.

**Body parameters**

Same as `/element/resolve`.

**Response**

Same structure as `/element/resolve` with `"action": "unresolve"`.

---

### POST /element/trash

Moves an element to the trash.

**Body parameters**

Same as `/element/resolve`.

**Response**

Same structure as `/element/resolve` with `"action": "trash"`.

---

### POST /element/create

Creates a new to-do in the planner.

**Body parameters**

| Parameter | Required | Description |
|---|---|---|
| `username` | yes | Smartschool username |
| `password` | yes | Smartschool password |
| `main_url` | yes | School domain |
| `mfa` | yes | Date of birth |
| `name` | yes | Title of the to-do |
| `description` | no | Plain-text description |
| `color` | no | Color name (default: `tangerine-200`) |
| `icon` | no | Icon name (default: `icon_fill_flag`) |
| `date_from` | no | Start date in ISO format (default: today at 00:00) |
| `date_to` | no | End date in ISO format (default: start date at 23:59:59) |
| `whole_day` | no | Boolean (default: `true`) |

**Response**

```json
{
  "status": "success",
  "action": "create",
  "http_status": 201,
  "result": {
    "id": "...",
    "name": "New task",
    "publicInfo": "<p>Description</p>",
    "period": {
      "dateTimeFrom": "...",
      "dateTimeTo": "...",
      "wholeDay": true
    }
  }
}
```

---

## Error Responses

All errors return a JSON object with a `status` field set to `"error"` and a `message` field describing the problem.

| HTTP Status | Meaning |
|---|---|
| 400 | Missing or invalid parameters |
| 401 | Authentication failed |
| 404 | Endpoint or element not found |
| 405 | HTTP method not allowed |
| 500 | Internal server error |

**Example**

```json
{
  "status": "error",
  "message": "Missing required parameters"
}
```

---

## Deployment

### Files required

```
/
├── app.py
├── requirements.txt
├── Procfile
```

### requirements.txt

```
Flask==3.0.0
flask-cors
smartschool
gunicorn
```

### Procfile

```
web: gunicorn app:app
```

### Environment

No environment variables are required. All credentials are passed per request.

### Build and start commands (Render)

| Setting | Value |
|---|---|
| Build Command | `pip install -r requirements.txt` |
| Start Command | `gunicorn app:app` |

---

## Notes

- Credentials are transmitted per request and are not stored on the server.
- The API uses the unofficial `smartschool` Python library, which relies on Smartschool's internal web endpoints. These endpoints may change without notice.
- The `mfa` field expects the user's date of birth in `YYYY-MM-DD` format.
- The `/ping` endpoint is intended for uptime monitors to keep the service awake.
