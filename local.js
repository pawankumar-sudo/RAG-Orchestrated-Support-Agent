// local.js — GUARANTEED WORKING VERSION

const express = require('express');
const bodyParser = require('body-parser');
const { handler } = require('./index');

const app = express();
const PORT = 3000;

// Capture RAW body (CRITICAL for Slack signature)
app.use(
  bodyParser.urlencoded({
    extended: false,
    verify: (req, res, buf) => {
      req.rawBody = buf.toString();
    }
  })
);

app.post('/slack', async (req, res) => {
  try {
    const event = {
      body: req.rawBody,
      headers: req.headers
    };

    const response = await handler(event);
    res.status(response.statusCode).send(response.body);
  } catch (err) {
    console.error('Local server error:', err);
    res.status(500).send('Internal Server Error');
  }
});

app.listen(PORT, () => {
  console.log(`⚡ Server running on http://localhost:${PORT}`);
});
