// Applies four small fixes to the installed @whiskeysockets/baileys 7.0.0-rc14, so that linking
// a device with a pairing code works again. Since July 2026 WhatsApp sends the
// `link_code_companion_reg` notification twice (first an empty one), and the released library
// crashes on the empty one with "Invalid buffer", so pairing never completes.
//
// The changes are those of WhiskeySockets/Baileys pull request 2872 (not merged yet), copied
// here line by line so no code from outside the official release is installed. Run after
// `npm install`:  node whatsapp/patch-baileys.mjs
// The script refuses to continue if any expected line is missing (a new library version).

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const LIB = path.join(HERE, 'node_modules', '@whiskeysockets', 'baileys', 'lib');
const MARK = '/* claude-news: pairing fix applied */';

const edits = [
  {
    file: 'Socket/messages-recv.js',
    // 1. only the second notification (stage "primary_hello") carries the keys; skip the first one
    from: "const linkCodeCompanionReg = getBinaryNodeChild(node, 'link_code_companion_reg');",
    to: "const linkCodeCompanionReg = getBinaryNodeChild(node, 'link_code_companion_reg');\n"
      + "                if (linkCodeCompanionReg?.attrs?.stage !== 'primary_hello') break;",
  },
  {
    file: 'Socket/socket.js',
    // 2. a device that asked for a pairing code but is not registered yet must still register, not log in
    from: 'if (!creds.me) {',
    to: 'if (!creds.me || (!!creds.pairingCode && !creds.registered)) {',
  },
  {
    file: 'Socket/socket.js',
    // 3. the nonce is one zero byte, not the character "0"
    from: "content: '0'",
    to: 'content: Buffer.alloc(1)',
  },
  {
    file: 'Utils/validate-connection.js',
    // 4. a successful pairing marks the device as registered
    from: 'platform: platformNode?.attrs.name',
    to: 'platform: platformNode?.attrs.name,\n        registered: true',
  },
];

const touched = new Map();
for (const e of edits) {
  const file = path.join(LIB, e.file);
  let text = touched.get(file) ?? fs.readFileSync(file, 'utf8');
  if (text.includes(MARK)) { console.log('already patched:', e.file); continue; }
  const count = text.split(e.from).length - 1;
  if (count !== 1) {
    console.error(`patch-baileys: expected exactly one match in ${e.file} for: ${e.from}  (found ${count})`);
    process.exit(1);
  }
  text = text.replace(e.from, e.to);
  touched.set(file, text);
}
for (const [file, text] of touched) {
  fs.writeFileSync(file, text + `\n${MARK}\n`);
  console.log('patched', path.relative(LIB, file));
}
