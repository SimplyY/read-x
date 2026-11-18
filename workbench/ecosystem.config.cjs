const path = require('node:path');

module.exports = {
  apps: [{
    name: 'ywdev-read-x-workbench',
    cwd: __dirname,
    script: path.join(__dirname, 'server.mjs'),
    interpreter: process.execPath,
    instances: 1,
    exec_mode: 'fork',
    autorestart: true,
    watch: false,
    env: { READX_WORKBENCH_PORT: '4185' },
  }, {
    name: 'ywdev-read-x-intake-sync',
    env: { PYTHONDONTWRITEBYTECODE: '1' },
    cwd: path.join(__dirname, '..'),
    script: path.join(__dirname, '..', 'scripts', 'reading_sync_worker.py'),
    interpreter: 'python3',
    instances: 1,
    exec_mode: 'fork',
    autorestart: true,
    watch: false,
  }],
};
