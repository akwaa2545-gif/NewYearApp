# Run on APP-HOST in the existing PowerShell window.
$ErrorActionPreference = 'Stop'
$appContainer = 'qrcheckin-eventdesk-eventdesk-1'
$tunnelContainer = 'qrcheckin-eventdesk-tunnel'

$appStatus = docker inspect --format '{{.State.Status}}' $appContainer
if ($LASTEXITCODE -ne 0 -or $appStatus -ne 'running') {
    throw "EventDesk container is not running: $appContainer"
}
$tunnelStatus = docker inspect --format '{{.State.Status}}' $tunnelContainer
if ($LASTEXITCODE -ne 0 -or $tunnelStatus -ne 'running') {
    throw "EventDesk tunnel is not running: $tunnelContainer"
}

$secret = Read-Host 'New EventDesk admin password' -AsSecureString
try {
    $password = (New-Object System.Net.NetworkCredential('', $secret)).Password
    if (-not $password) { throw 'The password cannot be empty.' }
    $python = @'
import json
import os
import secrets
import shutil
import sys
import tempfile
from datetime import datetime, timezone
import app

password = sys.stdin.read().rstrip('\r\n')
if not password:
    raise SystemExit('The password cannot be empty.')
path = app.CONFIG_PATH
config = app.load_config()
backup = path.with_name('config-before-admin-password-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '.json')
shutil.copy2(path, backup)
os.chmod(backup, 0o600)
salt = secrets.token_hex(16)
config['admin_password'] = {'salt': salt, 'hash': app.password_hash(password, salt)}
fd, temporary = tempfile.mkstemp(prefix='.config-admin-', suffix='.tmp', dir=path.parent)
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as output:
        json.dump(config, output, ensure_ascii=False)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
if not app.check_password(password, app.load_config()['admin_password']):
    raise SystemExit('Password verification failed.')
print('Admin password updated; backup:', backup)
'@
    $password | docker exec -i $appContainer python -c $python
    if ($LASTEXITCODE -ne 0) { throw 'Admin password update failed. The containers were not restarted.' }
} finally {
    Remove-Variable password, secret -ErrorAction SilentlyContinue
}

docker stop $tunnelContainer | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'The password changed, but the tunnel did not stop.' }
docker restart $appContainer | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'The password changed, but EventDesk did not restart.' }
docker start $tunnelContainer | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'The password changed, but the tunnel did not start.' }

docker exec $appContainer python -c "import app; app.load_config(); print('EventDesk config loads successfully')"
if ($LASTEXITCODE -ne 0) { throw 'EventDesk config validation failed after restart.' }
docker ps -a --filter "name=qrcheckin-eventdesk" --format 'table {{.Names}}\t{{.Status}}'
Write-Host 'EventDesk admin password update completed.'
