// Proves that the bytes deployed on a Studio network equal the committed contract source.
//   node scripts/verify_deployed_source.mjs <contract-address> [source-path] [rpc]
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { retryTransient } from './lib/common.mjs';

const [contractAddress, sourcePath = 'contracts/Litmus.py', rpc = 'https://studio.genlayer.com/api'] = process.argv.slice(2);
if (!contractAddress) throw new Error('usage: node scripts/verify_deployed_source.mjs <contract-address> [source-path] [rpc]');

const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');
const source = readFileSync(sourcePath);
if (source.includes(0x0d)) throw new Error(`${sourcePath} contains CR bytes; canonical contract source must use LF line endings.`);
if (source.subarray(0, 3).equals(Buffer.from([0xef, 0xbb, 0xbf]))) throw new Error(`${sourcePath} contains a UTF-8 BOM.`);

// Same transient-error policy as the live script: the public Studio RPC rate-limits and sometimes answers with HTML.
const payload = await retryTransient(async () => {
  const response = await fetch(rpc, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'gen_getContractCode', params: [contractAddress] }),
  });
  const text = await response.text();
  let body;
  try { body = JSON.parse(text); } catch (error) { throw new Error(`HTTP request failed (status ${response.status}): response is not valid JSON: ${text.slice(0, 80)}`); }
  if (!response.ok || body.error || typeof body.result !== 'string') throw new Error(`contract source retrieval failed: ${JSON.stringify(body)}`);
  return body;
}, { attempts: 5 });
const deployed = Buffer.from(payload.result, 'base64');
const result = {
  source: { path: sourcePath, bytes: source.length, sha256: digest(source) },
  deployed: { address: contractAddress, rpc, bytes: deployed.length, sha256: digest(deployed) },
  byteEqual: Buffer.compare(source, deployed) === 0,
};
if (!result.byteEqual) throw new Error(`source parity failed: ${JSON.stringify(result)}`);
console.log(JSON.stringify(result, null, 2));
