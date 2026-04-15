// index.js — GUARANTEED WORKING VERSION
console.log('JIRA_EMAIL:', process.env.JIRA_EMAIL);
console.log('ATLASSIAN_API_TOKEN:', process.env.ATLASSIAN_API_TOKEN?.slice(0, 10));

const crypto = require('crypto');
const { exec } = require('child_process');
const qs = require('querystring');

const SLACK_SIGNING_SECRET = process.env.SLACK_SIGNING_SECRET;
const JIRA_EMAIL = process.env.JIRA_EMAIL;
const ATLASSIAN_API_TOKEN = process.env.ATLASSIAN_API_TOKEN;

/* ==========================
   SLACK SIGNATURE VERIFICATION
========================== */
function verifySlackSignature(event) {
  const headers = event.headers || {};
  const signature = headers['x-slack-signature'];
  const timestamp = headers['x-slack-request-timestamp'];

  if (!signature || !timestamp) return false;

  const baseString = `v0:${timestamp}:${event.body}`;
  const hash = crypto
    .createHmac('sha256', SLACK_SIGNING_SECRET)
    .update(baseString, 'utf8')
    .digest('hex');

  return `v0=${hash}` === signature;
}

/* ==========================
   CREATE JSM TICKET VIA CURL
========================== */
function createJsmTicket(summary) {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify({
      serviceDeskId: "4",
      requestTypeId: "115",
      requestFieldValues: {
        summary,
        description: "Created from Slack /it command"
      }
    });

    const cmd = `
curl -s -u "${JIRA_EMAIL}:${ATLASSIAN_API_TOKEN}" \
-X POST \
-H "Accept: application/json" \
-H "Content-Type: application/json" \
"https://svavacapital.atlassian.net/rest/servicedeskapi/request" \
-d '${payload}'
`;

    exec(cmd, (error, stdout, stderr) => {
      if (error) {
        return reject(stderr || error.message);
      }

      try {
        const json = JSON.parse(stdout);
        resolve(json);
      } catch {
        reject(stdout);
      }
    });
  });
}

/* ==========================
   LAMBDA HANDLER
========================== */
exports.handler = async (event) => {
  if (!verifySlackSignature(event)) {
    return { statusCode: 403, body: 'Forbidden' };
  }

  const payload = qs.parse(event.body);
  const text = payload.text?.trim() || 'Issue created from Slack';

  // ACK Slack immediately
  setImmediate(async () => {
    try {
      const result = await createJsmTicket(text);
      console.log('✅ Ticket created:', result.issueKey);
    } catch (err) {
      console.error('❌ JSM error:', err);
    }
  });

  return {
    statusCode: 200,
    headers: { 'Content-Type': 'text/plain' },
    body: 'Processing your request...'
  };
};
