const http = require('http');

const options = {
  hostname: 'localhost',
  port: 3000,
  path: '/api/auth/login',
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
  },
};

const req = http.request(options, (res) => {
  let data = '';
  res.on('data', (chunk) => { data += chunk; });
  res.on('end', () => {
    const token = JSON.parse(data).token;
    console.log("TOKEN:", token);
    
    // Now fetch reports
    http.get('http://localhost:3000/api/reports', {
      headers: { 'Authorization': 'Bearer ' + token }
    }, (res2) => {
      let data2 = '';
      res2.on('data', (chunk) => { data2 += chunk; });
      res2.on('end', () => {
        console.log("REPORTS:", data2);
      });
    });
  });
});

req.write(JSON.stringify({ email: 'sk@gmail.com', password: 'password' })); // using dummy password if they don't check
req.end();
