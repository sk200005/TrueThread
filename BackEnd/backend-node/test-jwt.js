const jwt = require('jsonwebtoken');
const http = require('http');

const token = jwt.sign({ userId: '6cf58554-2459-42d1-b791-2c9c91ce504c' }, 'research_jwt_secret_change_in_production', { expiresIn: '1d' });

http.get('http://localhost:3000/api/reports', {
  headers: { 'Authorization': 'Bearer ' + token }
}, (res) => {
  let data = '';
  res.on('data', (chunk) => { data += chunk; });
  res.on('end', () => {
    console.log("REPORTS:", data);
  });
});
