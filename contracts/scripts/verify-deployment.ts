import { network } from "hardhat";

async function main() {
  // Defaults to "coral", or respects HARDHAT_NETWORK env if specified
  const networkName = process.env.HARDHAT_NETWORK || "coral";
  console.log(`Connecting to network: ${networkName}...`);

  const { viem } = await network.connect(networkName);
  const publicClient = await viem.getPublicClient();

  // Target deployed contract address on Coral Testnet (Upgraded EIP-712 build)
  const CONTRACT_ADDRESS = "0xf8a978c5d5dfc258b5aefb4c3ea99b73615b7995";
  const contractAddress = CONTRACT_ADDRESS as `0x${string}`;

  console.log(`Verifying deployment at address: ${contractAddress}`);

  // 1. Verify that bytecode exists at the given address
  const bytecode = await publicClient.getBytecode({ address: contractAddress });
  if (!bytecode || bytecode === "0x") {
    throw new Error(
      `No contract bytecode found at ${contractAddress} on network '${networkName}'. Verify the address and ensure the deployment transaction was confirmed.`
    );
  }

  console.log(`Bytecode verified on-chain (${bytecode.length / 2 - 1} bytes).`);

  // 2. Load the MEDwayReservation contract interface
  console.log("Loading MEDwayReservation contract interface...");
  const reservation = await viem.getContractAt("MEDwayReservation", contractAddress);

  // 3. Read on-chain contract state
  console.log("Reading state from blockchain...");
  const owner = await reservation.read.owner();
  const disputeWindow = await reservation.read.DISPUTE_WINDOW();
  const blockNumber = await publicClient.getBlockNumber();

  console.log("\n========================================================");
  console.log(" MEDwayReservation On-Chain Deployment Verified!");
  console.log("========================================================");
  console.log(` Network:          ${networkName}`);
  console.log(` Contract Address: ${contractAddress}`);
  console.log(` Contract Owner:   ${owner}`);
  console.log(` Dispute Window:   ${disputeWindow} seconds (${Number(disputeWindow) / 86400} days)`);
  console.log(` Verified at Block:#${blockNumber}`);
  console.log(" Status:           DEPLOYED & ACTIVE");
  console.log("========================================================\n");
}

main().catch((error) => {
  console.error("\nDeployment verification failed:", error?.message || error);
  process.exitCode = 1;
});

