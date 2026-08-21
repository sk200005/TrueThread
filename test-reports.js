const { login, listReports } = require('./FrontEnd/src/api/client.js');
async function test() {
  try {
    const data = await login('sk@gmail.com', 'somepassword');
    console.log(data);
  } catch (err) {
    console.error(err);
  }
}
// wait, client.js uses fetch, localStorage, import.meta.env which won't work in pure Node.
