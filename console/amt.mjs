import tls from 'node:tls';
import { constants, createHash, randomBytes, X509Certificate } from 'node:crypto';

export function tlsOptions(config) {
  const pin = new X509Certificate(config.certificate);
  return {
    host: config.host, port: config.port ?? 16995,
    ca: config.certificate, rejectUnauthorized: true, minVersion: 'TLSv1.2',
    secureOptions: config.legacy ? constants.SSL_OP_LEGACY_SERVER_CONNECT : 0,
    checkServerIdentity: (_host, cert) => cert.raw.equals(pin.raw)
      ? undefined : new Error('AMT certificate does not match the enrolled pin'),
  };
}

function authPacket(data) {
  const header = Buffer.from([0x13, 0, 0, 0, 4, 0, 0, 0, 0]);
  header.writeUInt32LE(data.length, 5);
  return Buffer.concat([header, data]);
}

function fields(values) {
  return Buffer.concat(values.map(value => {
    const bytes = Buffer.from(value, 'latin1');
    if (bytes.length > 255) throw new Error('AMT authentication field too long');
    return Buffer.concat([Buffer.from([bytes.length]), bytes]);
  }));
}

const md5 = value => createHash('md5').update(value, 'latin1').digest('hex');

// Intel redirection authentication precedes the otherwise standard RFB stream.
export async function* authenticate(socket, { username, password }) {
  const iterator = socket[Symbol.asyncIterator]();
  let pending = Buffer.alloc(0);
  async function take(size) {
    while (pending.length < size) {
      const { value, done } = await iterator.next();
      if (done) throw Object.assign(new Error('AMT closed the console connection'), { code: 'AMT_CLOSED' });
      pending = Buffer.concat([pending, value]);
    }
    const result = pending.subarray(0, size);
    pending = pending.subarray(size);
    return result;
  }
  async function reply() {
    const header = await take(9);
    if (header[0] !== 0x14 || header[4] !== 4) throw new Error('Unexpected AMT authentication reply');
    const size = header.readUInt32LE(5);
    if (size > 4096) throw new Error('AMT authentication reply too large');
    return { status: header[1], data: await take(size) };
  }

  socket.write(Buffer.from([0x10, 1, 0, 0, 0x4b, 0x56, 0x4d, 0x52]));
  const start = await take(4);
  if (start[0] !== 0x11 || start[1] !== 0) {
    const reason = { 2: 'Console is busy; close the other KVM client', 3: 'KVM is not enabled or supported' }[start[1]];
    throw Object.assign(new Error(reason ?? 'AMT refused the KVM session'), start[1] === 2 ? { code: 'AMT_BUSY' } : {});
  }
  const details = await take(9);
  await take(details[8]);
  socket.write(Buffer.from([0x13, 0, 0, 0, 0, 0, 0, 0, 0]));
  const methods = await take(9);
  const size = methods.readUInt32LE(5);
  if (methods[0] !== 0x14 || methods[4] !== 0 || size > 256) throw new Error('Invalid AMT authentication methods');
  if (!(await take(size)).includes(4)) throw new Error('AMT does not offer Digest authentication');
  const uri = '/RedirectionService';
  socket.write(authPacket(fields([username, '', '', uri, '', '', '', ''])));
  const challenge = await reply();
  if (challenge.status !== 1) throw new Error('AMT did not send a Digest challenge');
  let offset = 0;
  function field() {
    if (offset >= challenge.data.length) throw new Error('Truncated AMT Digest challenge');
    const length = challenge.data[offset++];
    if (offset + length > challenge.data.length) throw new Error('Truncated AMT Digest challenge');
    const value = challenge.data.toString('latin1', offset, offset + length);
    offset += length;
    return value;
  }
  const realm = field(), nonce = field(), qop = field();
  if (qop !== 'auth' || !nonce) throw new Error('Unsupported AMT Digest challenge');
  const cnonce = randomBytes(16).toString('hex'), nc = '00000002';
  const digest = md5(`${md5(`${username}:${realm}:${password}`)}:${nonce}:${nc}:${cnonce}:${qop}:${md5(`POST:${uri}`)}`);
  socket.write(authPacket(fields([username, realm, nonce, uri, cnonce, nc, digest, qop])));
  if ((await reply()).status !== 0) throw new Error('AMT rejected the credentials');
  socket.write(Buffer.from([0x40, 0, 0, 0, 0, 0, 0, 0]));
  const ready = await take(8);
  if (ready[0] !== 0x41) throw new Error('AMT could not start KVM');
  socket.setTimeout(0);
  if (pending.length) yield pending;
  for (;;) {
    const { value, done } = await iterator.next();
    if (done) return;
    yield value;
  }
}

export function connect(config) {
  const socket = tls.connect(tlsOptions(config));
  socket.setTimeout(15000, () => socket.destroy(Object.assign(new Error('AMT connection timed out'), { code: 'ETIMEDOUT' })));
  socket.setNoDelay(true);
  return socket;
}
