// WhatsApp training watcher. Runs on GitHub (whatsapp.yml), no PC needed.
//
// It is logged in to WhatsApp as a linked device (like WhatsApp Web). The login is kept
// encrypted in the `whatsapp-state` branch (auth.enc), next to the schedule (state.json).
//
//   MODE=login   pair this GitHub job with the phone: WhatsApp > Linked devices > Link a device
//                > "Link with phone number instead". The 8-letter code is pushed to the phone
//                through Claude News (never printed: the job's log is public).
//   MODE=watch   stay connected for up to MAX_HOURS, read what the group says, and send:
//                  - CONFIG.reminderText, reminderMinutesBefore the training
//                  - the equipment poll, pollMinutesAfter the training starts
//                A newer run that GitHub starts waits; this run sees it and hands over.
//
// A training is announced by a group message that contains the word "מחר" (tomorrow) and a
// time (17:00, 17.00, בשעה 17, ב-17). "מחרתיים" means the day after tomorrow.
//
// Nothing the group writes is printed to the log: it is public.

import * as baileysModule from '@whiskeysockets/baileys';
import pino from 'pino';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync, spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const baileys = baileysModule.default?.default ? baileysModule.default : baileysModule;
const makeWASocket = baileys.default ?? baileys.makeWASocket;
const { useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion, Browsers,
        makeCacheableSignalKeyStore, jidNormalizedUser } = baileys;

// ----------------------------------------------------------------------------- settings
const CONFIG = {
  group: 'כושר קרבי ח ט רעננה🏆👑💥',           // the group to watch (its exact name)
  triggerWord: 'מחר',                             // a message with this word and a time = a training tomorrow
  reminderText: 'תזכורת להביא ציוד!',
  reminderMinutesBefore: 30,
  pollMinutesAfter: 70,                           // an hour and ten minutes after the training starts
  pollTitle: (date) => `ציוד בתאריך ${date}`,     // date as 24.11.2026
  pollOptions: ['כרית', 'שתי כריות', 'כרית ותיק'],
  sendTo: 'me',                                   // 'group': into the group. 'me': to your own chat (trial period, from 2026-11-24)
  tellClaudeNews: true,                           // also tell the phone (Claude News) about every training noted and every send
  timeZone: 'Asia/Jerusalem',
  pollLateHours: 3,                               // a poll more than this late is not sent
  messageMaxAgeHours: 36,                         // older group messages are ignored (history, not news)
};

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const STATE_DIR = path.resolve(process.env.STATE_DIR || path.join(ROOT, 'state'));
const AUTH_FILE = path.join(STATE_DIR, 'auth.enc');
const STATE_FILE = path.join(STATE_DIR, 'state.json');
const LOGIN_NEEDED = path.join(STATE_DIR, 'LOGIN_NEEDED');
const AUTH_DIR = path.join(process.env.RUNNER_TEMP || HERE, 'wa-auth');
const MODE = process.env.MODE || 'watch';
const MAX_HOURS = Number(process.env.MAX_HOURS || 5.5);
const DRY = process.env.DRY === '1';
const MIN = 60_000;

const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const pad = (n) => String(n).padStart(2, '0');

// ----------------------------------------------------------------------------- Israel time
function zoned(ms) {
  const f = new Intl.DateTimeFormat('en-GB', { timeZone: CONFIG.timeZone, year: 'numeric', month: '2-digit',
    day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  const p = Object.fromEntries(f.formatToParts(new Date(ms)).map(x => [x.type, x.value]));
  return { y: +p.year, m: +p.month, d: +p.day, hh: +p.hour % 24, mm: +p.minute };
}

function instant(y, m, d, hh, mm) {           // Israel wall-clock time -> ms since epoch
  const want = Date.UTC(y, m - 1, d, hh, mm);
  let guess = want;
  for (let i = 0; i < 3; i++) {
    const z = zoned(guess);
    guess += want - Date.UTC(z.y, z.m - 1, z.d, z.hh, z.mm);
  }
  return guess;
}

// ----------------------------------------------------------------------------- reading a message
const HEB_DAYS = ['ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת'];
const CANCEL_WORDS = /(מבוטל|בוטל|מתבטל|ביטול|אין אימון|לא יהיה אימון|לא יתקיים|נדחה לשבוע הבא)/;
const heb = (word) => new RegExp(`(^|[^א-ת])${word}([^א-ת]|$)`);   // a whole Hebrew word (\b does not know Hebrew)

// Reads one group message. Returns null when it says nothing about a training day, otherwise
//   { date, display, time, at, cancel, noTime }
// The day: an explicit date (26.11, 26/11, 26.11.2026), "יום שלישי" (the coming one), "מחרתיים",
// "מחר", or "היום"/"הערב". The time: 17:00, 17.00, בשעה 17, ב-17. Questions (with "?") are ignored:
// "מחר ב-17:00?" is somebody asking, not the trainer announcing.
// cancel: the message cancels that day's training. noTime: a day and the word "אימון" but no time.
export function parseTraining(text, sentMs) {
  const t = String(text || '').trim();
  if (!t || t.includes('?')) return null;
  const z = zoned(sentMs);
  const todayUTC = Date.UTC(z.y, z.m - 1, z.d);
  const weekdayToday = new Date(todayUTC).getUTCDay();          // 0 = Sunday = ראשון

  // the time, first with a colon
  let hh, mm;
  let m = t.match(/(?<!\d)(\d{1,2}):(\d{2})(?!\d)/);
  if (m) { hh = +m[1]; mm = +m[2]; }

  // the day
  let dayUTC = null;
  for (const d of t.matchAll(/(?<![\d.\/:])(\d{1,2})([.\/])(\d{1,2})(?:[.\/](\d{4}))?(?![\d:])/g)) {
    const a = +d[1], b = +d[3], sep = d[2];
    const before = t.slice(Math.max(0, d.index - 8), d.index);
    // "17.30" with a dot is a time unless a colon time exists elsewhere, it says בתאריך, or it cannot be a time
    const isDate = sep === '/' || hh !== undefined || /בתאריך/.test(before) || a > 23 || d[3].length === 1 || d[4];
    if (isDate && a >= 1 && a <= 31 && b >= 1 && b <= 12) {
      let y = d[4] ? +d[4] : z.y;
      let u = Date.UTC(y, b - 1, a);
      if (!d[4] && u < todayUTC - 2 * 86400_000) u = Date.UTC(y + 1, b - 1, a);   // a date that passed means next year
      dayUTC = u;
      break;
    }
  }
  if (dayUTC === null) {
    m = t.match(/(?:ביום|יום)\s*(ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת)/);
    if (m) dayUTC = todayUTC + ((HEB_DAYS.indexOf(m[1]) - weekdayToday + 7) % 7) * 86400_000;
    else if (/מחרתיים/.test(t)) dayUTC = todayUTC + 2 * 86400_000;
    else if (heb(CONFIG.triggerWord).test(t)) dayUTC = todayUTC + 86400_000;
    else if (heb('היום').test(t) || heb('הערב').test(t)) dayUTC = todayUTC;
  }
  if (dayUTC === null) return null;

  // the time without a colon
  if (hh === undefined) {
    m = t.match(/(?<![\d.])(\d{1,2})\.(\d{2})(?![\d.])/);          // 17.30
    if (m && +m[1] <= 23) { hh = +m[1]; mm = +m[2]; }
    else {
      m = t.match(/(?:בשעה|ב-|ב)\s*(\d{1,2})(?![\d:.])/);         // בשעה 17, ב-17, ב17
      if (m) { hh = +m[1]; mm = 0; }
    }
  }
  const cancel = CANCEL_WORDS.test(t);
  const hasTime = hh !== undefined && hh <= 23 && mm <= 59;
  if (!hasTime && !cancel && !heb('אימון').test(t)) return null;

  const day = new Date(dayUTC);
  const y = day.getUTCFullYear(), mo = day.getUTCMonth() + 1, d = day.getUTCDate();
  return {
    date: `${y}-${pad(mo)}-${pad(d)}`,
    display: `${d}.${mo}.${y}`,
    time: hasTime ? `${pad(hh)}:${pad(mm)}` : null,
    at: hasTime ? instant(y, mo, d, hh, mm) : null,
    cancel,
    noTime: !hasTime,
  };
}

function unwrap(message) {
  let m = message;
  for (let i = 0; i < 4 && m; i++) {
    const inner = m.ephemeralMessage?.message || m.viewOnceMessage?.message || m.viewOnceMessageV2?.message
      || m.documentWithCaptionMessage?.message || m.editedMessage?.message?.protocolMessage?.editedMessage;
    if (!inner) break;
    m = inner;
  }
  return m || {};
}

function textOf(msg) {
  const m = unwrap(msg.message);
  return m.conversation || m.extendedTextMessage?.text || m.imageMessage?.caption || m.videoMessage?.caption || '';
}

function timestampMs(msg) {
  const t = msg.messageTimestamp;
  const s = typeof t === 'object' && t !== null ? (t.toNumber ? t.toNumber() : Number(t.low)) : Number(t);
  return (s || Math.floor(Date.now() / 1000)) * 1000;
}

// ----------------------------------------------------------------------------- encrypted login
function key() {
  const secret = process.env.WA_AUTH_KEY;
  if (!secret) throw new Error('WA_AUTH_KEY is not set');
  return crypto.scryptSync(secret, 'claude-news-whatsapp', 32);
}

function encryptDir(dir) {
  const files = {};
  for (const f of fs.readdirSync(dir)) files[f] = fs.readFileSync(path.join(dir, f), 'utf8');
  const iv = crypto.randomBytes(12);
  const c = crypto.createCipheriv('aes-256-gcm', key(), iv);
  const enc = Buffer.concat([c.update(Buffer.from(JSON.stringify(files))), c.final()]);
  return Buffer.concat([iv, c.getAuthTag(), enc]).toString('base64') + '\n';
}

function decryptToDir(b64, dir) {
  const buf = Buffer.from(b64.trim(), 'base64');
  const d = crypto.createDecipheriv('aes-256-gcm', key(), buf.subarray(0, 12));
  d.setAuthTag(buf.subarray(12, 28));
  const files = JSON.parse(Buffer.concat([d.update(buf.subarray(28)), d.final()]).toString());
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
  for (const [f, c] of Object.entries(files)) fs.writeFileSync(path.join(dir, f), c);
  return Object.keys(files).length;
}

// ----------------------------------------------------------------------------- state branch
function git(...args) {
  return execFileSync('git', args, { cwd: STATE_DIR, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
}

let committing = Promise.resolve();
function commitState(message) {
  committing = committing.then(async () => {
    try {
      git('add', '-A');
      if (git('status', '--porcelain') === '') return;
      git('-c', 'user.name=whatsapp-watcher', '-c', 'user.email=whatsapp-watcher@users.noreply.github.com',
          'commit', '-q', '-m', message);
    } catch (e) { log('could not commit the state:', e.message.slice(-200)); return; }
    for (let i = 0; i < 3; i++) {
      try { git('pull', '--rebase', '-q', 'origin', 'whatsapp-state'); git('push', '-q', 'origin', 'HEAD:whatsapp-state'); log('saved:', message); return; }
      catch (e) { try { git('rebase', '--abort'); } catch {} await sleep(5000); }
    }
    log('could not push the state; the next save tries again');
  });
  return committing;
}

function loadState() {
  try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); }
  catch { return { groupId: null, trainings: [], seen: [] }; }
}

function saveState(state, message) {
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2) + '\n');
  return commitState(message);
}

let authTimer = null;
function saveAuth(reason, now = false) {
  const write = () => {
    authTimer = null;
    try { fs.writeFileSync(AUTH_FILE, encryptDir(AUTH_DIR)); } catch (e) { log('could not encrypt the login:', e.message); return; }
    return commitState(`login: ${reason}`);
  };
  if (now) { if (authTimer) clearTimeout(authTimer); return write(); }
  if (!authTimer) authTimer = setTimeout(write, 20_000);          // WhatsApp rotates keys often: save in batches
}

// ----------------------------------------------------------------------------- the phone
function tellPhone(title, body) {
  return new Promise((resolve) => {
    const p = spawn('python', [path.join(HERE, 'notify.py'), title, body], { stdio: 'inherit' });
    p.on('close', (code) => resolve(code === 0));
    p.on('error', () => resolve(false));
  });
}

// ----------------------------------------------------------------------------- GitHub
async function newerRunWaiting() {
  const { GITHUB_REPOSITORY: repo, GITHUB_RUN_NUMBER: mine, GITHUB_TOKEN: token, GITHUB_WORKFLOW_REF: ref = '' } = process.env;
  if (!repo || !mine) return false;
  const workflow = ref.includes('/workflows/') ? ref.split('@')[0].split('/').pop() : 'whatsapp.yml';
  try {
    const r = await fetch(`https://api.github.com/repos/${repo}/actions/workflows/${workflow}/runs?per_page=10`, {
      headers: { Accept: 'application/vnd.github+json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      signal: AbortSignal.timeout(20_000),
    });
    if (!r.ok) return false;
    const runs = (await r.json()).workflow_runs || [];
    return runs.some(run => run.run_number > Number(mine) && run.status !== 'completed');
  } catch (e) { log('could not ask GitHub about newer runs:', e.name); return false; }
}

// ----------------------------------------------------------------------------- WhatsApp
async function connect(onUpdate, onMessages) {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  // While pairing, the library's own log helps to see where linking fails. The job's log is
  // public, so every long run of digits (phone numbers, ids) is masked before it is written.
  const masked = { write: (s) => process.stdout.write(String(s).replace(/\d{7,}/g, '***')) };
  const logger = MODE.startsWith('login') ? pino({ level: 'info', base: null }, masked) : pino({ level: 'error' });
  const { version } = await fetchLatestBaileysVersion();
  const sock = makeWASocket({
    version,
    auth: { creds: state.creds, keys: makeCacheableSignalKeyStore ? makeCacheableSignalKeyStore(state.keys, logger) : state.keys },
    logger,
    browser: Browsers.ubuntu('Chrome'),
    markOnlineOnConnect: false,          // otherwise the phone stops showing WhatsApp notifications
    // pairing code: keep the code valid for a while instead of dropping the link after 2.5 minutes.
    // QR: a fresh one every 45 seconds, like WhatsApp Web (the server's refs do not live long)
    qrTimeout: MODE === 'login-qr' ? 45_000 : 10 * MIN,
    syncFullHistory: false,
    generateHighQualityLinkPreview: false,
  });
  let wasRegistered = !!state.creds.registered;
  sock.ev.on('creds.update', async () => {
    await saveCreds();
    if (state.creds.registered && !wasRegistered) { wasRegistered = true; log('paired with the phone; waiting for WhatsApp to confirm'); }
    if (state.creds.registered) saveAuth('keys updated');
  });
  sock.ev.on('connection.update', (u) => onUpdate(sock, u));
  if (onMessages) sock.ev.on('messages.upsert', (u) => onMessages(sock, u));
  return sock;
}

function closeReason(update) {
  return update.lastDisconnect?.error?.output?.statusCode;
}

async function findGroup(sock) {
  const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const groups = await sock.groupFetchAllParticipating();
  const list = Object.values(groups);
  const hit = list.find(g => norm(g.subject) === norm(CONFIG.group))
    || list.find(g => norm(g.subject).startsWith(norm(CONFIG.group).replace(/[^֐-׿\w ]+$/, '').trim()));
  log(`groups on this account: ${list.length}; the watched group was ${hit ? '' : 'NOT '}found`);
  return hit?.id || null;
}

// ----------------------------------------------------------------------------- login
function appChannel() {                       // the app's ntfy server and topic, from config.js
  const text = fs.readFileSync(path.join(ROOT, 'config.js'), 'utf8');
  return JSON.parse(text.slice(text.indexOf('{'), text.lastIndexOf('}') + 1));
}

async function publishQR(qr) {
  const cfg = appChannel();
  const r = await fetch(`${cfg.ntfy.replace(/\/$/, '')}/${cfg.topic}-qr`, {
    method: 'POST', body: qr, headers: { Title: 'WhatsApp login QR', Priority: 'low' }, signal: AbortSignal.timeout(20_000),
  });
  return r.ok;
}

async function login() {
  const QR = MODE === 'login-qr';
  const phone = (process.env.WA_PHONE || '').replace(/\D/g, '');
  if (!QR && !phone) { console.error('WA_PHONE (the WhatsApp number, digits only, with country code) is not set'); process.exit(1); }
  for (const f of [AUTH_FILE, LOGIN_NEEDED]) if (fs.existsSync(f)) fs.rmSync(f);   // a new login replaces the old one
  await commitState('login: replacing the login');
  const fresh = () => { fs.rmSync(AUTH_DIR, { recursive: true, force: true }); fs.mkdirSync(AUTH_DIR, { recursive: true }); };
  fresh();
  let asked = false, done = false, codes = 0, paired = false;
  const MAX_CODES = 2;
  const deadline = Date.now() + 22 * MIN;

  let qrs = 0;
  const run = () => connect(async (sock, u) => {
    if (u.qr && QR) {                                           // every new or refreshed QR goes to the app's channel
      qrs++;
      try { log((await publishQR(u.qr)) ? `QR ${qrs} published to the channel; scan it with the phone` : `could not publish QR ${qrs}`); }
      catch (e) { log('could not publish the QR:', e.message); }
    } else if (u.qr && !asked) {
      asked = true;
      if (++codes > MAX_CODES) { log('three codes were sent and none was typed in; giving up'); process.exit(1); }
      try {
        const code = await sock.requestPairingCode(phone);
        const pretty = code.length === 8 ? `${code.slice(0, 4)}-${code.slice(4)}` : code;
        const ok = await tellPhone(`WhatsApp code: ${pretty}`,
          `Type it within 10 minutes in WhatsApp > Linked devices > Link a device > Link with phone number. Older codes no longer work.${codes > 1 ? ` (code ${codes} of ${MAX_CODES})` : ''}`);
        log(ok ? `pairing code ${codes} sent to the phone; waiting for it to be typed in` : 'could not send the pairing code to the phone');
      } catch (e) { log('could not get a pairing code:', e.message); process.exit(1); }
    }
    if (u.isNewLogin || sock.authState?.creds?.registered) paired = true;
    if (u.connection === 'open') {
      log('logged in');
      await sleep(8000);                                        // let the first key exchange settle
      if (fs.existsSync(LOGIN_NEEDED)) fs.rmSync(LOGIN_NEEDED);
      await saveAuth('paired', true);
      done = true;
      await tellPhone('WhatsApp connected', 'The training watcher is logged in. It starts watching the group on its next run.');
      process.exit(0);
    }
    if (u.connection === 'close' && !done) {
      const code = closeReason(u);
      if (code === DisconnectReason.restartRequired || paired) { log('reconnecting after pairing (code', code, ')'); await sleep(2000); run(); return; }
      if (Date.now() > deadline) { log('login timed out'); process.exit(1); }
      // the pairing was dropped (nobody typed the code or scanned in time): start over
      log('connection closed while pairing (code', code, QR ? '); new QRs follow' : '); asking for a new code');
      if (!QR || code === DisconnectReason.loggedOut) fresh();   // a QR session keeps its keys; the server just hands out new refs
      asked = false;
      await sleep(3000); run();
    }
  });
  await run();
  setTimeout(() => { if (!done) { log('nobody typed a code in time'); process.exit(1); } }, 22 * MIN);
}

// ----------------------------------------------------------------------------- watch
async function watch() {
  if (fs.existsSync(LOGIN_NEEDED) || !fs.existsSync(AUTH_FILE)) {
    log('not logged in to WhatsApp: run this workflow by hand with mode = login');
    return process.exit(0);
  }
  decryptToDir(fs.readFileSync(AUTH_FILE, 'utf8'), AUTH_DIR);
  const state = loadState();
  const started = Date.now();
  let sock = null, open = false, busy = false, closing = false, reconnects = 0;

  const targetJid = () => CONFIG.sendTo === 'me' ? jidNormalizedUser(sock.user.id) : state.groupId;

  const send = async (content, what) => {
    if (DRY) { log('DRY: would send', what); return true; }
    if (!open || !targetJid()) { log('cannot send', what, '(not connected or no target)'); return false; }
    await sock.sendMessage(targetJid(), content);
    log('sent', what);
    if (CONFIG.tellClaudeNews) {
      tellPhone('WhatsApp watcher', `Sent ${what} to ${CONFIG.sendTo === 'me' ? 'your own chat' : 'the group'}.`).catch(() => {});
    }
    return true;
  };

  const nextDue = () => {
    let soonest = Infinity;
    for (const t of state.trainings) {
      if (t.reminder === 'pending') soonest = Math.min(soonest, t.at - CONFIG.reminderMinutesBefore * MIN);
      if (t.poll === 'pending') soonest = Math.min(soonest, t.at + CONFIG.pollMinutesAfter * MIN);
    }
    return soonest;
  };

  const tick = async () => {
    if (busy || closing) return;
    busy = true;
    try {
      const now = Date.now();
      let changed = false;
      for (const t of state.trainings) {
        const remAt = t.at - CONFIG.reminderMinutesBefore * MIN;
        const pollAt = t.at + CONFIG.pollMinutesAfter * MIN;
        if (t.reminder === 'pending') {
          if (now >= remAt && now <= t.at) {
            if (await send({ text: CONFIG.reminderText }, `the reminder for ${t.date} ${t.time}`)) { t.reminder = 'sent'; changed = true; }
          } else if (now > t.at) { t.reminder = 'skipped'; changed = true; log('reminder for', t.date, 'is too late, skipped'); }
        }
        if (t.poll === 'pending') {
          if (now >= pollAt && now <= pollAt + CONFIG.pollLateHours * 3600_000) {
            const poll = { name: CONFIG.pollTitle(t.display), values: CONFIG.pollOptions, selectableCount: 1 };
            if (await send({ poll }, `the poll for ${t.date}`)) { t.poll = 'sent'; changed = true; }
          } else if (now > pollAt + CONFIG.pollLateHours * 3600_000) { t.poll = 'skipped'; changed = true; log('poll for', t.date, 'is too late, skipped'); }
        }
      }
      const before = state.trainings.length;
      state.trainings = state.trainings.filter(t => t.at > now - 2 * 24 * 3600_000);
      if (state.trainings.length !== before) changed = true;
      if (changed) await saveState(state, 'schedule: sent / cleaned');
    } catch (e) { log('tick failed:', e.message); }
    finally { busy = false; }
  };

  const onMessages = async (_sock, { messages, type }) => {
    if (type !== 'notify' || !state.groupId) return;
    let changed = false;
    for (const msg of messages) {
      if (msg.key?.remoteJid !== state.groupId || !msg.key?.id) continue;
      if (state.seen.includes(msg.key.id)) continue;
      state.seen.push(msg.key.id);
      if (state.seen.length > 300) state.seen = state.seen.slice(-300);
      changed = true;
      const sentMs = timestampMs(msg);
      if (Date.now() - sentMs > CONFIG.messageMaxAgeHours * 3600_000) continue;
      const found = parseTraining(textOf(msg), sentMs);
      if (!found) continue;
      const same = state.trainings.find(t => t.date === found.date);
      let note = null;
      if (found.cancel) {                                        // the trainer cancelled that day
        if (same && same.poll !== 'sent') {
          same.reminder = same.reminder === 'sent' ? 'sent' : 'cancelled';
          same.poll = 'cancelled';
          log('training on', found.date, 'cancelled');
          note = `The training on ${found.display} was cancelled: nothing more will be sent for it.`;
        } else if (!same) {
          note = `The group says the training on ${found.display} is cancelled (nothing was scheduled for that day).`;
        }
      } else if (found.noTime) {                                 // a day and "אימון" but no time: just tell the phone
        note = same ? null : `The group mentions a training on ${found.display} without a time. Nothing scheduled yet.`;
      } else if (same && same.time === found.time) {
        if (same.poll === 'cancelled' && same.at > Date.now()) { same.reminder = 'pending'; same.poll = 'pending'; note = `The training on ${found.display} at ${found.time} is on again.`; }
      } else if (same && same.poll !== 'sent') {                  // moved: a later time arms the reminder again
        const movedLater = found.at > same.at;
        Object.assign(same, found, { msgId: msg.key.id, poll: 'pending',
          reminder: same.reminder === 'sent' && !movedLater ? 'sent' : 'pending' });
        log('training on', found.date, 'moved to', found.time);
        note = `The training on ${found.display} moved to ${found.time}.`;
      } else if (!same) {
        state.trainings.push({ ...found, msgId: msg.key.id, reminder: 'pending', poll: 'pending' });
        log('training announced for', found.date, 'at', found.time);
        note = `Training noted: ${found.display} at ${found.time}. Reminder ${CONFIG.reminderMinutesBefore} min before, poll ${CONFIG.pollMinutesAfter} min after.`;
      }
      if (note && CONFIG.tellClaudeNews) tellPhone('WhatsApp watcher', note).catch(() => {});
    }
    if (changed) await saveState(state, 'schedule: group read');
  };

  const finish = async (why, code = 0) => {
    if (closing) return;
    closing = true;
    log('ending:', why);
    clearInterval(tickTimer); clearInterval(handTimer);
    try { await saveAuth('run ended', true); } catch {}
    await committing;
    try { sock?.end?.(); } catch {}
    process.exit(code);
  };

  const onUpdate = async (_sock, u) => {
    if (u.connection === 'open') {
      open = true; reconnects = 0;
      log('connected to WhatsApp');
      if (!state.groupId) {
        state.groupId = await findGroup(sock);
        if (state.groupId) await saveState(state, 'schedule: group found');
        else log('the group was not found: check CONFIG.group against the exact group name');
      }
    }
    if (u.connection === 'close') {
      open = false;
      const code = closeReason(u);
      if (code === DisconnectReason.loggedOut) {
        log('WhatsApp logged this device out: a new login is needed');
        fs.writeFileSync(LOGIN_NEEDED, new Date().toISOString() + '\n');
        try { fs.rmSync(AUTH_FILE); } catch {}
        await commitState('login: logged out by WhatsApp');
        await tellPhone('WhatsApp watcher logged out', 'WhatsApp disconnected the training watcher. Run the WhatsApp workflow with mode = login to connect it again.');
        return finish('logged out', 2);
      }
      if (closing) return;
      if (++reconnects > 8) return finish('could not stay connected', 1);
      log('connection closed (code', code, '); reconnecting in a moment');
      await sleep(Math.min(60_000, 3000 * reconnects));
      if (!closing) sock = await connect(onUpdate, onMessages);
    }
  };

  sock = await connect(onUpdate, onMessages);
  const tickTimer = setInterval(tick, 20_000);
  const handTimer = setInterval(async () => {
    const soon = nextDue() - Date.now() < 6 * MIN;              // never hand over right before a send
    if (soon) return;
    if (Date.now() - started > MAX_HOURS * 3600_000) return finish('time is up; a newer run takes over');
    if (await newerRunWaiting()) return finish('a newer run is waiting: handing over to it');
  }, 2 * MIN);
  process.on('SIGTERM', () => finish('stopped by GitHub'));
}

// ----------------------------------------------------------------------------- go
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv[2] === 'parse') {                            // node bot.mjs parse "מחר ב-17:00"  (a test)
    console.log(parseTraining(process.argv[3], Date.now()));
  } else if (MODE === 'login' || MODE === 'login-qr') login().catch(e => { console.error(e); process.exit(1); });
  else watch().catch(e => { console.error(e); process.exit(1); });
}
