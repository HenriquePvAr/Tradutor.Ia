// UI contract guard for removal of the social profile editing experience.
// Keeps the former single-submit regression slot useful: no profile editor is mounted,
// and the normal client no longer exposes a profile mutation method.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const shell = fs.readFileSync(path.join(ROOT, 'ui', 'ui_shell.html'), 'utf8');
const community = fs.readFileSync(path.join(ROOT, 'static', 'social_community.js'), 'utf8');
const api = fs.readFileSync(path.join(ROOT, 'static', 'social_api.js'), 'utf8');

assert.doesNotMatch(shell, /data-tab="profile"|view-profile|profile-form|profile-save/i);
assert.doesNotMatch(community, /renderProfile|profileForm|updateMyProfile|getMyProfile/);
assert.doesNotMatch(api, /updateMyProfile|getMyProfile|getProfileByUsername/);
console.log('ok - social profile editor and client save path are absent');
