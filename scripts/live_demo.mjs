// Live end-to-end run on Studionet against Kraken's real status feed.
// Opens two short pools on the same venue with different scopes, buys cover,
// waits for the windows to close, then resolves both with real validators.
//   node scripts/live_demo.mjs <contractAddress>
import { createClient, createAccount, generatePrivateKey } from "genlayer-js";
import { studionet } from "genlayer-js/chains";
import { TransactionStatus } from "genlayer-js/types";
import fs from "node:fs";

const address = process.argv[2];
const FEED = "https://status.kraken.com/api/v2/incidents.json";
const GEN = 10n ** 18n;

function loadKey(name) {
  const env = fs.existsSync(".env") ? fs.readFileSync(".env", "utf8") : "";
  const m = env.match(new RegExp(`^${name}=(0x[0-9a-f]+)`, "m"));
  if (m) return m[1];
  const pk = generatePrivateKey();
  fs.appendFileSync(".env", `${name}=${pk}\n`);
  return pk;
}

async function fund(addr) {
  await fetch(studionet.rpcUrls.default.http[0], {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "sim_fundAccount", params: [addr, Number(1000n * GEN)] }),
  });
}

const underwriter = createAccount(loadKey("DEMO_UNDERWRITER_PK"));
const buyer = createAccount(loadKey("DEMO_BUYER_PK"));
const client = createClient({ chain: studionet });
await fund(underwriter.address);
await fund(buyer.address);

async function write(account, functionName, args, value = 0n) {
  const hash = await client.writeContract({ account, address, functionName, args, value });
  const receipt = await client.waitForTransactionReceipt({ hash, status: TransactionStatus.ACCEPTED, retries: 120, interval: 5000 });
  const res = receipt?.consensus_data?.leader_receipt?.[0]?.result ?? receipt?.result;
  console.log(`${functionName}(${JSON.stringify(args)}) -> tx ${hash}`);
  return { hash, receipt, res };
}
const read = (functionName, args) => client.readContract({ address, functionName, args });

const now = Math.floor(Date.now() / 1000);
const start = now + 150;
const end = start + 180;

const scopes = [
  "EGLD (MultiversX) spot trading on Kraken",
  "BTC spot order placement or matching on Kraken",
];
const ids = [];
for (const scope of scopes) {
  const before = Number(await read("get_pool_count", []));
  await write(underwriter, "open_pool", ["Kraken", FEED, scope, start, end, 2, 5], 10n * GEN);
  ids.push(before);
  await write(buyer, "buy_cover", [before], 1n * GEN);
}

const wait = (end + 20) * 1000 - Date.now();
console.log(`waiting ${Math.round(wait / 1000)}s for the cover windows to close...`);
await new Promise((r) => setTimeout(r, Math.max(wait, 0)));

const out = { contract: address, network: "studionet", pools: [] };
for (const id of ids) {
  const { hash } = await write(buyer, "resolve", [id]);
  const pool = await read("get_pool", [id]);
  const plain = JSON.parse(JSON.stringify(pool, (_, v) => (typeof v === "bigint" ? v.toString() : v instanceof Map ? Object.fromEntries(v) : v)));
  console.log(JSON.stringify(plain, null, 2));
  out.pools.push({ id, resolve_tx: hash, pool: plain });
}
fs.writeFileSync("demo_result.json", JSON.stringify(out, null, 2));
