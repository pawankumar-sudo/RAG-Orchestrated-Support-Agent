# AI Agent Guidance for Slack-JIRA-Confluence Integration

## Project Overview
Slack application that integrates with JIRA and Confluence. Provides slash commands (`/it help`, `/it jira`) to search Confluence articles and create JIRA tickets directly from Slack.

**Architecture**: AWS Lambda handler with local Express server for testing. Dual deployment:
- **Production**: AWS Lambda function (entry point: `exports.handler` in `index.js`)
- **Local Development**: Express server on port 3000 (`local.js`)

---

## Critical Architecture Patterns

### 1. Event Flow: Slack → Verification → Processing → Response
```
Slack POST → verify signature → parse command → call Atlassian APIs → post reply to Slack
```

**Key Pattern**: All responses are asynchronous messages posted *back to Slack* via `postMessageToSlack()`, not returned directly. Lambda returns 200 OK immediately after confirming the request.

### 2. Slack Signature Verification (Security-Critical)
- **File**: `index.js` (lines 24-48)
- **Purpose**: Prevents unauthorized requests from reaching API integrations
- **Window**: 5-minute timestamp tolerance to prevent replay attacks
- **Pattern**: HMAC-SHA256 over `v0:{timestamp}:{body}` format
- **Environment Var**: `SLACK_SIGNING_SECRET` (required for security)

### 3. Three-System Integration
| System | Purpose | Auth Method | Key Functions |
|--------|---------|-------------|----------------|
| **Slack** | Command dispatcher | Bot token | `postMessageToSlack()` |
| **JIRA** | Ticket creation | Basic Auth (email+API token) | `createJiraTicket()` |
| **Confluence** | Knowledge base search | Basic Auth (same credentials) | `searchConfluence()` |

---

## Environment Configuration
All credentials use environment variables (never hardcoded):
```javascript
SLACK_SIGNING_SECRET          // Slack request validation
SLACK_BOT_TOKEN               // Slack API access
JIRA_BASE_URL                 // e.g., https://svavacapital.atlassian.net
CONFLUENCE_BASE_URL           // Same as JIRA_BASE_URL
JIRA_EMAIL                    // Atlassian account email
ATLASSIAN_API_TOKEN           // Atlassian API token (env var: ATLASSIAN_API_TOKEN)
```

---

## Developer Workflows

### Local Testing
```bash
# Install dependencies
npm install

# Run local Express server
node local.js
# Server listens on http://localhost:3000/slack

# Test with curl (example):
curl -X POST http://localhost:3000/slack \
  -d "channel_id=C123&text=help workflow+database"
```

**Note**: Local server bypasses signature verification (no X-Slack-Signature needed). Add proper headers for prod-like testing.

### Adding New Commands
1. **Add handler** in `processCommand()` function (line ~155)
2. **Parse command** with existing `cmd` logic: `text.split(' ')`
3. **Call appropriate API** (`searchConfluence()` or `createJiraTicket()`)
4. **Post response** via `postMessageToSlack(channel, message)`
5. **Always catch errors** and post user-friendly feedback

### Testing Confluence Queries
- **CQL Format**: `spaceKey="IS" AND text~"<query>"`
- **Space**: Currently hardcoded to "IS" (Internal Systems)
- **Limitation**: Returns first result only (`.limit=1`)
- **Error Handling**: HTML responses caught and rejected (line ~193)

---

## Project-Specific Conventions

### HTTP Promise Pattern
All Slack/Atlassian API calls use identical structure:
```javascript
return new Promise((resolve, reject) => {
  const req = https.request(options, res => {
    let body = '';
    res.on('data', d => (body += d));
    res.on('end', () => {
      try {
        const json = JSON.parse(body);
        if (res.statusCode >= 300) reject(json);
        else resolve(json);
      } catch { reject(...); }
    });
  });
  req.on('error', reject);
  req.write(payload);
  req.end();
});
```
**Pattern**: Accumulate response data, parse JSON, reject on non-2xx status. Use this for new API integrations.

### Error Responses
- **User-visible**: Posted to Slack channel as `❌ Message`
- **Logs**: Console errors for debugging (AWS CloudWatch in prod)
- **No stack traces** to users—generic errors only

### Header Case-Handling
Slack headers may be lowercase or titlecase (`x-slack-signature` vs `X-Slack-Signature`). Code checks both (line 27-28). Maintain this pattern for new header reads.

---

## Key Files
- **`index.js`**: Main handler, all API logic, command processing
- **`local.js`**: Express wrapper for local testing (rawBody handling for signature verification)
- **`package.json`**: Express + body-parser only; minimal dependencies
- **`.github/copilot-instructions.md`**: This file

---

## Common Issues & Solutions

| Issue | Cause | Solution |
|-------|-------|----------|
| Slack: "Invalid timestamp" | Request older than 5 min | Check server time sync |
| JIRA 401 Unauthorized | Missing/invalid API token | Verify `ATLASSIAN_API_TOKEN` env var |
| Confluence returns HTML | Query has syntax errors or permissions | Log response first 150 chars (see line 193) |
| Lambda timeout | Async operations slow | Ensure promises resolve quickly |
| Local server: "rawBody undefined" | body-parser misconfiguration | Verify `local.js` verify callback is attached |

---

## Deployment Notes
- **AWS Lambda**: Environment variables set via Lambda configuration console
- **Triggers**: AWS API Gateway → Lambda (Slack → POST webhook)
- **Response**: Must return 200 OK within timeout window; messages posted asynchronously
- **Scaling**: Stateless design; no database persistence required

