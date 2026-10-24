// Сборка attr.min.js: node build.js. Версия terser зафиксирована точно, чтобы сборка не плыла.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');

const TERSER = 'terser@5.36.0';
const source = path.join(__dirname, 'attr.js');
const target = path.join(__dirname, 'attr.min.js');

const hash = crypto.createHash('sha256').update(fs.readFileSync(source)).digest('hex');

const result = spawnSync('npx', [
  '--yes', TERSER, source,
  '--ecma', '2015', '--compress', '--mangle', '--comments', 'false'
], { encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 });

if (result.error || result.status !== 0 || !result.stdout) {
  process.stderr.write('terser не отработал: ' + (result.error ? result.error.message : result.stderr) + '\n');
  process.exit(1);
}

fs.writeFileSync(target, '/* sha256 ' + hash + ' */\n' + result.stdout.trim() + '\n');
console.log('attr.min.js: sha256 ' + hash + ', ' + fs.statSync(target).size + ' байт');
