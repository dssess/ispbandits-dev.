# SSH metering integration contract

The collector accepts cumulative per-account network counters from a separate trusted producer at `/run/vpn-ssh-meter/counters.json` (override with `SSH_COUNTER_FILE`). The producer must attribute actual network bytes to an SSH or Dropbear account; `/proc/<pid>/io` is not valid network accounting.

The file must be owned and protected so only root can read it (no group/world permissions). Write atomically using a temporary file in the same directory followed by `os.replace`. The top-level `sampled_at` must be an ISO-8601 timestamp with timezone and no more than 180 seconds old by default. A `generation` value remains stable while counters are cumulative and must change when the producer resets its counters. Values are non-negative cumulative bytes.

Example format:

```json
{
  "version": 1,
  "sampled_at": "2026-11-12T14:30:00Z",
  "counters": [
    {
      "protocol": "openssh",
      "external_identity": "alice",
      "generation": "agent-start-uuid",
      "upload_bytes": 1024,
      "download_bytes": 2048
    }
  ]
}
```

Each `(protocol, external_identity)` must already map to an account in `protocol_accounts`. Direction must be measured from the client's perspective and verified with known-byte transfers. A missing or stale snapshot aborts the collector transaction, preventing Xray and SSH totals from getting out of sync silently. Install automatic quota enforcement only after validating this meter against known transfers and disconnect/restart edge cases.
