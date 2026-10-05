# Chat interaction ingest contract

Send a learner's timestamped interactions to:

```http
POST /v1/ingest/chat
```

`chat` is a single-learner source, so `learner_id` is required. `payload` may be
the message list directly (shown below) or `{ "messages": [...] }`.

```json
{
  "learner_id": "11111111-1111-1111-1111-111111111111",
  "occurred_at": "2026-10-05T10:00:00Z",
  "external_id": "conversation-9-export-2026-10-05",
  "payload": [
    {
      "text": "Can you send me a progress update?",
      "attachments": [],
      "sender": {"id": "manager-1", "role": "manager"},
      "timestamp": "2026-10-01T09:00:00Z"
    },
    {
      "text": "Yes, I finished the API and will send the link.",
      "attachments": [{"name": "status.pdf", "type": "application/pdf"}],
      "sender": {"id": "learner-7", "role": "learner"},
      "timestamp": "2026-10-01T09:12:00Z",
      "metadata": {"thread_id": "thread-4"}
    }
  ],
  "metadata": {
    "conversation_id": "conversation-9",
    "channel": "slack",
    "learner_sender_id": "learner-7"
  }
}
```

Message fields:

- `text` (or `content`, `message`, `body`) may be empty only for an attachment-only message.
- `attachments` is optional and accepts a list of producer-owned attachment metadata.
- `sender` is either a string such as `"learner"` or an object. Structured senders
  should include `id` and `role`; use the role `learner` for the learner's messages.
- `timestamp` (or `sent_at`, `time`, `created_at`) is required. Include a timezone.
- `metadata` is optional per-message metadata and is passed into extraction.

If upstream roles do not label the learner, set envelope
`metadata.learner_sender_id` to the matching `sender.id`.

Messages are ordered by timestamp. The extractor retains exact timestamps and
deterministically annotates response delays, elapsed time between messages, and
messages with no learner reply in the supplied transcript. For unanswered messages,
`occurred_at` acts as the transcript snapshot time when it is later than the last
message. Absence of a reply is not treated as proof of deliberate ignoring.
