const jwt = require('jsonwebtoken');
const token = jwt.sign({ userId: '6cf58554-2459-42d1-b791-2c9c91ce504c' }, 'research_jwt_secret_change_in_production');
fetch('http://localhost:3000/api/reports', {
  headers: { 'Authorization': `Bearer ${token}` }
}).then(r => r.json()).then(console.log).catch(console.error);
