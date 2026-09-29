/**
 * What the dashboard's Vercel project is configured with — names and environments, never a value.
 *
 * The dashboard can only see the variables Vercel gives the environment it runs in, and a variable
 * or store connection that exists for Preview but not Production looks, from the dashboard, exactly
 * like one that does not exist. This reads the project's configuration through Vercel's API with the
 * repository's VERCEL_TOKEN and prints, for each variable, its name, the environments it reaches and
 * its type; for each Blob store connected to the project, the environments of that connection and
 * the prefix its variables are named with; and the Production branch.
 *
 * The output goes to a public log, so it is built field by field from names, targets and types:
 * no value, token, store id or URL is printed, including when the API returns one.
 *
 *     node scripts/cloud/vercel_config.mjs      (VERCEL_TOKEN, VERCEL_ORG_ID, VERCEL_PROJECT_ID)
 */
const { VERCEL_TOKEN, VERCEL_ORG_ID, VERCEL_PROJECT_ID } = process.env;
if (!VERCEL_TOKEN || !VERCEL_PROJECT_ID) {
  console.log("VERCEL_TOKEN or VERCEL_PROJECT_ID is not set; nothing to read");
  process.exit(0);
}
const team = VERCEL_ORG_ID?.startsWith("team_") ? `teamId=${VERCEL_ORG_ID}` : "";
const api = async (path) => {
  const sep = path.includes("?") ? "&" : "?";
  const res = await fetch(`https://api.vercel.com${path}${team ? sep + team : ""}`, {
    headers: { authorization: `Bearer ${VERCEL_TOKEN}` },
  });
  if (!res.ok) return { refused: res.status };
  return res.json();
};
const targets = (t) => (Array.isArray(t) ? t : t ? [t] : []).sort().join(",") || "-";

const project = await api(`/v9/projects/${VERCEL_PROJECT_ID}`);
if (project.refused) console.log(`project: refused (HTTP ${project.refused})`);
else console.log(`production branch: ${project.link?.productionBranch ?? "(not linked to git)"}`);

const env = await api(`/v10/projects/${VERCEL_PROJECT_ID}/env`);
if (env.refused) {
  console.log(`environment variables: refused (HTTP ${env.refused})`);
} else {
  console.log("\nenvironment variables (name  environments  type  [branch]):");
  for (const e of [...(env.envs ?? [])].sort((a, b) => a.key.localeCompare(b.key))) {
    const branch = e.gitBranch ? `  branch-only` : "";
    const managed = e.configurationId ? "  from-integration" : "";
    console.log(`  ${String(e.key).padEnd(40)} ${targets(e.target).padEnd(32)} ${e.type}${branch}${managed}`);
  }
}

const stores = await api(`/v1/storage/stores`);
if (stores.refused) {
  console.log(`\nstorage stores: refused (HTTP ${stores.refused})`);
} else {
  const list = stores.stores ?? stores ?? [];
  console.log("\nstores connected to this project (type  environments  variable prefix):");
  let n = 0;
  for (const s of Array.isArray(list) ? list : []) {
    for (const p of s.projectsMetadata ?? []) {
      if (p.projectId !== VERCEL_PROJECT_ID) continue;
      n += 1;
      console.log(`  ${String(s.type ?? "?").padEnd(8)} ${targets(p.environments).padEnd(32)} ${p.envVarPrefix ?? "(default)"}`);
    }
  }
  if (!n) console.log("  none found for this project");
}
