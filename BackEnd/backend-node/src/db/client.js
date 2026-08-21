'use strict';

require('dotenv').config();
const { Pool } = require('pg');

/**
 * Shared pg connection pool.
 * Uses individual PG* env vars for reliability (DATABASE_URL can break
 * if the password contains special characters like # that confuse URL parsers).
 */
const db = new Pool({
  host: process.env.PGHOST || 'localhost',
  port: parseInt(process.env.PGPORT || '5433'),
  database: process.env.PGDATABASE || 'postgres',
  user: process.env.PGUSER || 'swayam',
  password: process.env.PGPASSWORD,
});

// Test connection on startup
db.connect((err, client, release) => {
  if (err) {
    console.error('[DB] Failed to connect to PostgreSQL:', err.message);
  } else {
    console.log('[DB] Connected to PostgreSQL successfully.');
    release();
  }
});

module.exports = db;
