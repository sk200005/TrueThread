const { translate } = require('@vitalets/google-translate-api');

async function test() {
  console.log("Starting translation...");
  try {
    const res = await translate("Namaste", { to: 'en' });
    console.log("Result:", res.text);
  } catch (err) {
    console.error("Error:", err.message);
  }
}

test();
