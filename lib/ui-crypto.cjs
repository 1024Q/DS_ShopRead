// Shared by Next route handlers and the native WebSocket server.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

function createCodec(directory = process.env.ECMP_UI_DATA_DIR || path.join(process.cwd(), '.data')) {
  let cached;
  function key() {
    if (cached) return cached;
    fs.mkdirSync(directory, { recursive: true });
    const file = path.join(directory, 'ui.key');
    try {
      fs.writeFileSync(file, crypto.randomBytes(32), { flag: 'wx', mode: 0o600 });
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
    }
    const value = fs.readFileSync(file);
    if (value.length !== 32) throw new Error('Invalid UI encryption key');
    cached = value;
    return value;
  }
  return {
    encrypt(value) {
      const iv = crypto.randomBytes(12);
      const cipher = crypto.createCipheriv('aes-256-gcm', key(), iv);
      return Buffer.concat([iv, cipher.update(JSON.stringify(value)), cipher.final(), cipher.getAuthTag()]).toString(
        'base64url',
      );
    },
    decrypt(value) {
      if (typeof value !== 'string' || value.length > 32768) throw new Error('Invalid cookie');
      const data = Buffer.from(value, 'base64url');
      if (data.length < 29) throw new Error('Invalid cookie');
      const cipher = crypto.createDecipheriv('aes-256-gcm', key(), data.subarray(0, 12));
      cipher.setAuthTag(data.subarray(-16));
      return JSON.parse(Buffer.concat([cipher.update(data.subarray(12, -16)), cipher.final()]).toString());
    },
  };
}
module.exports = { ...createCodec(), createCodec };
