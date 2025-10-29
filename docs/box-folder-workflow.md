# Box Folder Workflow Integration Guide

This guide describes how to split the current `/post/create_box_folder` workflow into three lighter-weight endpoints that can be called directly from a HubSpot workflow. Each action receives only the properties it needs, avoiding redundant HubSpot API calls and giving you tighter control over automation.

## High-Level Flow

1. **Create the client folder** (clone the Box template, derive the folder name).
2. **Apply metadata** using the HubSpot fields collected in the workflow.
3. **Share the Client Sharing subfolder** with the desired email addresses.

All three steps can be triggered sequentially within a HubSpot workflow. The same payload shape is used in each step, with fields trimmed to only what is required.

## Required HubSpot Properties

Capture these fields in the workflow action so they are available in the `event` payload that HubSpot sends to each endpoint.

| Purpose | Properties |
|---------|------------|
| Folder naming & sharing | `hs_deal_record_id`, `hs_contact_id`, `hs_contact_firstname`, `hs_contact_lastname`, `hs_contact_email` |
| Spouse record & sharing | `hs_spouse_id`, `hs_spouse_firstname`, `hs_spouse_lastname`, `hs_spouse_email` |
| Metadata tagging | `deal_salutation`, `household_type` |

Spouse details are mandatory for every deal so the metadata and sharing always capture both contacts.

## Endpoint Overview

### 1. `POST /box/folder/create`

- **Purpose:** Clone the Box template and create the client folder under `Team Advice/Pivot Clients/1. Active Clients`.
- **Required properties:** `hs_deal_record_id`, `hs_contact_firstname`, `hs_contact_lastname`, `hs_spouse_firstname`, `hs_spouse_lastname`.
- **Response:** Folder id, name, and Box URL you will pass to subsequent steps.

```python
import requests

event = {
    "deal_id": "42970036094",
    "fields": {
        "hs_deal_record_id": "42970036094",
        "hs_contact_firstname": "Alex",
        "hs_contact_lastname": "Test",
        "hs_spouse_firstname": "Jordan",
        "hs_spouse_lastname": "Test"
    }
}

resp = requests.post(
    "https://<your-app-domain>/box/folder/create",
    json=event,
    timeout=10,
)
resp.raise_for_status()
folder_info = resp.json()["folder"]
```

### 2. `POST /box/folder/tag`

- **Purpose:** Apply metadata from HubSpot to the newly created folder.
- **Required properties:** `hs_deal_record_id`, `deal_salutation`, `household_type`, `hs_contact_id`, `hs_spouse_id`.
- **Inputs:** `folder_id` returned from step 1.

```python
event = {
    "deal_id": "42970036094",
    "folder_id": folder_info["id"],
    "fields": {
        "deal_salutation": "Alex & Jordan Test",
        "household_type": "Couple",
        "hs_contact_id": "131609018283",
        "hs_spouse_id": "131609018299"
    }
}

resp = requests.post(
    "https://<your-app-domain>/box/folder/tag",
    json=event,
    timeout=10,
)
resp.raise_for_status()
```

### 3. `POST /box/folder/share`

- **Purpose:** Share the `Client Sharing` subfolder with the primary contact (and spouse when provided).
- **Required properties:** `folder_id` from step 1, plus the email addresses to invite.
- **Note:** The current UI exposes this step in a locked state; HubSpot can call the endpoint directly when you're ready to re-enable automatic invites.

```python
event = {
    "folder_id": folder_info["id"],
    "emails": [
        "client.primary@example.com",
        "client.spouse@example.com"
    ]
}

resp = requests.post(
    "https://<your-app-domain>/box/folder/share",
    json=event,
    timeout=10,
)
resp.raise_for_status()
```

> **Note:** Sharing is optional. If you keep the feature disabled, you can skip this endpoint entirely.

## HubSpot Workflow Custom Code

Create a custom-code action in your HubSpot workflow with the following guidelines:

1. **Properties to send**
   - `hs_deal_record_id` (string, required)
   - `hs_contact_id`, `hs_contact_email`, `hs_contact_firstname`, `hs_contact_lastname`
   - `hs_spouse_id`, `hs_spouse_email`, `hs_spouse_firstname`, `hs_spouse_lastname`
   - Metadata: `deal_salutation`, `household_type`
   - Workflow input for steps 2 & 3: `folder_id` (map the output from Action 1 into later actions)
2. **Custom code snippets**

   Configure three back-to-back custom-code actions—one for each endpoint. Reuse the field lists shown below and map the `folder_id` output from Action 1 into Actions 2 and 3.

   #### Action 1 – `POST /box/folder/create`

   Required properties: `hs_deal_record_id`, `hs_contact_firstname`, `hs_contact_lastname`, `hs_spouse_firstname`, `hs_spouse_lastname`, `deal_salutation`

   ```python
   import requests

   BOX_BASE = "https://<your-app-domain>"


   def main(event):
       fields = {**(event.get("fields") or {}), **(event.get("inputFields") or {})}
       deal_id = event.get("deal_id") or fields["hs_deal_record_id"]

       payload = {
           "deal_id": str(deal_id),
           "fields": {
               "hs_deal_record_id": str(deal_id),
               **{key: str(fields[key]) for key in (
                   "hs_contact_firstname", "hs_contact_lastname",
                   "hs_spouse_firstname", "hs_spouse_lastname",
                   "deal_salutation",
               )},
           },
       }

       resp = requests.post(
           f"{BOX_BASE}/box/folder/create",
           json=payload,
           timeout=20,
       )
       resp.raise_for_status()
       folder = resp.json().get("folder", {})

       return {
           "outputFields": {
               "folder_id": folder.get("id"),
               "folder_url": folder.get("url"),
               "folder_name": folder.get("name"),
           }
       }
   ```

   #### Action 2 – `POST /box/folder/tag`

   Required properties: `hs_deal_record_id`, `folder_id`, `hs_contact_id`, `hs_contact_firstname`, `hs_contact_lastname`, `hs_contact_email`, `hs_spouse_id`, `hs_spouse_firstname`, `hs_spouse_lastname`, `hs_spouse_email`, `deal_salutation`, `household_type`

   ```python
   import requests

   BOX_BASE = "https://<your-app-domain>"


   def main(event):
       fields = {**(event.get("fields") or {}), **(event.get("inputFields") or {})}
       deal_id = event.get("deal_id") or fields["hs_deal_record_id"]
       folder_id = event.get("folder_id") or fields["folder_id"]

       payload = {
           "deal_id": str(deal_id),
           "folder_id": str(folder_id),
           "fields": {
               "hs_deal_record_id": str(deal_id),
               **{key: str(fields[key]) for key in (
                   "hs_contact_id", "hs_contact_email",
                   "hs_contact_firstname", "hs_contact_lastname",
                   "hs_spouse_id", "hs_spouse_email",
                   "hs_spouse_firstname", "hs_spouse_lastname",
                   "deal_salutation", "household_type",
               )},
           },
       }

       resp = requests.post(
           f"{BOX_BASE}/box/folder/tag",
           json=payload,
           timeout=20,
       )
       resp.raise_for_status()

       return {
           "outputFields": {
               "folder_id": str(folder_id),
               "metadata_status": "applied",
           }
       }
   ```

   #### Action 3 – `POST /box/folder/share`

   Required properties: `folder_id`, `hs_contact_email`, `hs_spouse_email`

   ```python
   import requests

   BOX_BASE = "https://<your-app-domain>"


   def main(event):
       fields = {**(event.get("fields") or {}), **(event.get("inputFields") or {})}
       folder_id = event.get("folder_id") or fields["folder_id"]
       emails = [
           fields[name]
           for name in ("hs_contact_email", "hs_spouse_email")
           if fields.get(name)
       ]

       resp = requests.post(
           f"{BOX_BASE}/box/folder/share",
           json={"folder_id": str(folder_id), "emails": emails},
           timeout=20,
       )
       resp.raise_for_status()

       return {
           "outputFields": {
               "folder_id": str(folder_id),
               "shared_emails": emails,
           }
       }
   ```
3. **Return values**
   - Capture `folder_id` and `folder_url` in the action output so you can reference them in downstream workflow steps or confirmation emails.

## Workflow Diagram

```mermaid
flowchart TD
    subgraph HubSpot Workflow
        A1[Deal triggers<br/>HubSpot workflow]
        A2[Send Box Folder<br/>Create request]
        A3[Send metadata<br/>tagging request]
        A4[Optional: send<br/>sharing request]
        A1 --> A2 --> A3 --> A4
    end

    subgraph Box Service
        B1[POST /box/folder/create\n• Clone template\n• Build folder name\n• Return folder_id & URL]
        B2[POST /box/folder/tag\n• Apply metadata\n• Return status]
        B3[POST /box/folder/share\n• Invite emails to Client Sharing\n• Return recipients]
    end

    HubSpot Workflow -->|JSON event (deal_id, contact details)| B1
    B1 -->|folder_id, folder_url| HubSpot Workflow
    HubSpot Workflow -->|folder_id + metadata| B2
    B2 -->|tagging status| HubSpot Workflow
    HubSpot Workflow -->|folder_id + emails| B3
    B3 -->|collaboration results| HubSpot Workflow

    B3 --> E[Box sends email notifications<br/>to invited collaborators]
```

## Implementation Notes

- Keep using `hs_deal_record_id` as the canonical reference across all steps.
- When migration is complete, retire the legacy `/post/create_box_folder` combined endpoint to avoid accidental double-processing.
- All endpoints should log the payload and step status so you can trace HubSpot executions end-to-end.
- The environment variables `BOX_CLIENT_SHARE_SUBFOLDER` and `BOX_CLIENT_SHARE_ROLE` continue to control which subfolder (default `Client Sharing`) and permission level (default `viewer`) are used for sharing.

With these endpoints and payloads in place, each HubSpot workflow action can be focused and idempotent, giving you fine-grained control over folder automation inside Box.
