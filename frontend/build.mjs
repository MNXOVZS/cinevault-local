import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

const directory = fileURLToPath(new URL('.', import.meta.url));
await build({
  absWorkingDir: directory,
  entryPoints: ['src/main.ts'],
  outfile: resolve(directory, '../web/experience.js'),
  bundle: true,
  format: 'iife',
  platform: 'browser',
  target: ['es2020'],
  charset: 'utf8',
  legalComments: 'eof',
  sourcemap: false,
  minify: false,
  logLevel: 'info',
  banner: { js: '// CineVault typed playback/preview experience. Source: frontend/src; rebuild: npm ci && npm run build.' }
});
