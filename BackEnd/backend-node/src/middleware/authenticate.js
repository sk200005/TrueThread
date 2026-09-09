'use strict';

const jwt = require('jsonwebtoken');

/**
 * authenticate — verifies the JWT in the Authorization header.
 * Attaches decoded payload as req.user = { userId }.
 */
function authenticate(req, res, next) {
  const authHeader = req.headers.authorization;

  if (!authHeader || !authHeader.startsWith('Bearer ')) {
    return res.status(401).json({ error: 'Missing or invalid Authorization header' });
  }

  const token = authHeader.slice(7);

  try {
    const decoded = jwt.verify(token, process.env.JWT_SECRET); //decoded stores userID from payload
    req.user = decoded; // { userId: '...' }
    next();
  } catch (err) {
    return res.status(401).json({ error: 'Token invalid or expired' });
  }
}

module.exports = { authenticate };





// GET /api/profile HTTP/1.1
// Host: example.com
// Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
//                Bearer <JWT_TOKEN>   // Bearer tells the server: "The value after this is an authentication token."