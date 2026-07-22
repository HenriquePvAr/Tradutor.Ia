type AuditResult = {
  mode: "audit";
  remoteMigrationExecuted: false;
  preservesUserIds: true;
  printsSecrets: false;
};

export function buildAuditPlan(): AuditResult {
  return {
    mode: "audit",
    remoteMigrationExecuted: false,
    preservesUserIds: true,
    printsSecrets: false,
  };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  console.log(JSON.stringify(buildAuditPlan(), null, 2));
}

