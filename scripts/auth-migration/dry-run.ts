import { buildAuditPlan } from "./audit.js";

export function dryRun() {
  return {
    ...buildAuditPlan(),
    usersFound: 0,
    accountsFound: 0,
    conflicts: [],
    ownershipAffected: [],
    wouldInvalidateSessions: true,
  };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  console.log(JSON.stringify(dryRun(), null, 2));
}

