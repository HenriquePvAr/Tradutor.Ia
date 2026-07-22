export function verifyDryRunOnly() {
  return {
    remoteMigrationExecuted: false,
    verificationScope: "dry-run-only",
  };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  console.log(JSON.stringify(verifyDryRunOnly(), null, 2));
}

