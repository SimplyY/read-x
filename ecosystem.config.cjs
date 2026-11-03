const path = require('node:path');

const repo = __dirname;
const stateDir = path.join(repo, 'state', 'long-read');

module.exports = {
  apps: [{
    name: 'ywdev-read-x',
    cwd: repo,
    script: '.agents/skills/long-read/scripts/long_read_workflow_cli.py',
    interpreter: 'python3',
    args: [
      '--state-dir', stateDir,
      'run-worker', '--serve', '--poll-seconds', '15', '--owner', 'read-x-worker',
    ],
    exec_mode: 'fork',
    instances: 1,
    autorestart: true,
    exp_backoff_restart_delay: 1000,
    min_uptime: '10s',
    max_restarts: 12,
    watch: false,
    time: true,
    log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
    out_file: path.join(stateDir, 'logs', 'worker-out.log'),
    error_file: path.join(stateDir, 'logs', 'worker-error.log'),
    merge_logs: true,
    kill_timeout: 10000,
  }],
};
