const { login, submitQuery } = require('./FrontEnd/src/api/client.js');
async function test() {
  try {
    const data = await login('email@gmail.com', 'somepassword');
    // We mock apiFetch for Node since it uses fetch and import.meta
  } catch (err) {
    console.error(err);
  }
}
