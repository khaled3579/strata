import assert from 'node:assert/strict';
import { statSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { currentUserSid } from '../../server/private-file.mjs';

/** The effective protection of a file: mode 0600 on POSIX, the exact DACL (one protected entry for this user) on NTFS. */
export function assertPrivate(path) {
  if (process.platform !== 'win32') { assert.equal(statSync(path).mode & 0o777, 0o600); return; }
  const inspect = `$acl = Get-Acl -LiteralPath '${path.replace(/'/g, "''")}'; ` +
    '$rules = $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]); ' +
    '[pscustomobject]@{ protected = $acl.AreAccessRulesProtected; rules = @($rules | ForEach-Object { ' +
    '[pscustomobject]@{ sid = $_.IdentityReference.Value; allow = $_.AccessControlType -eq "Allow"; ' +
    'full = $_.FileSystemRights -eq "FullControl"; inherited = $_.IsInherited } }) } | ConvertTo-Json -Compress';
  const result = execFileSync(join(process.env.SystemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
    ['-NoProfile', '-NonInteractive', '-Command', inspect], { encoding: 'utf8', timeout: 30000, windowsHide: true });
  assert.deepEqual(JSON.parse(result), {
    protected: true,
    rules: [{ sid: currentUserSid(), allow: true, full: true, inherited: false }],
  });
}
