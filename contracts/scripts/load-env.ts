import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { isHex } from "viem";
import { privateKeyToAccount } from "viem/accounts";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

export function initEnv() {
  const candidatePaths = [
    path.resolve(process.cwd(), ".env"),
    path.resolve(__dirname, "../.env"),
    path.resolve(__dirname, "../../backend/.env"),
    path.resolve(process.cwd(), "contracts/.env"),
    path.resolve(process.cwd(), "backend/.env"),
  ];

  for (const envPath of candidatePaths) {
    if (fs.existsSync(envPath) && typeof process.loadEnvFile === "function") {
      try {
        process.loadEnvFile(envPath);
      } catch {
        // Ignore read/syntax errors from secondary candidates
      }
    }
  }
}

export function getInstitutionKeys() {
  initEnv();

  const hospitalAKey = process.env.HOSPITAL_A_PRIVATE_KEY;
  const labCKey = process.env.LAB_C_PRIVATE_KEY;

  if (!hospitalAKey || !isHex(hospitalAKey) || hospitalAKey.length !== 66) {
    throw new Error(
      "Missing or invalid HOSPITAL_A_PRIVATE_KEY. Must be a 32-byte hex string (0x...). " +
      "Configure it in contracts/.env or backend/.env."
    );
  }

  if (!labCKey || !isHex(labCKey) || labCKey.length !== 66) {
    throw new Error(
      "Missing or invalid LAB_C_PRIVATE_KEY. Must be a 32-byte hex string (0x...). " +
      "Configure it in contracts/.env or backend/.env."
    );
  }

  return {
    hospitalAKey: hospitalAKey as `0x${string}`,
    hospitalA: privateKeyToAccount(hospitalAKey as `0x${string}`),
    labCKey: labCKey as `0x${string}`,
    labC: privateKeyToAccount(labCKey as `0x${string}`),
  };
}

