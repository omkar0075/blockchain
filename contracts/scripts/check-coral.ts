import { network } from "hardhat";
import { formatEther } from "viem";

async function main() {
  console.log("Connecting to network: coral...");
  console.log("RPC Endpoint: https://coralrpc.mstblockchain.com\n");

  const { viem } = await network.connect("coral");
  const publicClient = await viem.getPublicClient();

  console.log("Checking RPC responsiveness and querying chain parameters...");
  const chainId = await publicClient.getChainId();
  const blockNumber = await publicClient.getBlockNumber();
  const latestBlock = await publicClient.getBlock({ blockNumber });

  console.log("\n========================================================");
  console.log(" Coral Testnet Connection Verified!");
  console.log("========================================================");
  console.log(` Network Name:      Coral Testnet`);
  console.log(` Chain ID:          ${chainId} ${chainId === 54635 ? "(matches expected 54635)" : ""}`);
  console.log(` Latest Block #:    ${blockNumber}`);
  console.log(` Block Timestamp:   ${new Date(Number(latestBlock.timestamp) * 1000).toISOString()}`);

  // Query account information if configured
  try {
    const walletClients = await viem.getWalletClients();
    if (walletClients && walletClients.length > 0) {
      const deployer = walletClients[0];
      const balanceWei = await publicClient.getBalance({
        address: deployer.account.address,
      });
      const balanceMst = formatEther(balanceWei);

      console.log(` Deployer Address:  ${deployer.account.address}`);
      console.log(` Account Balance:   ${balanceMst} MST (${balanceWei} wei)`);
    } else {
      console.log(" Deployer Account:  None loaded. (Set CORAL_PRIVATE_KEY via keystore or env)");
    }
  } catch (accountError: any) {
    console.log(` Deployer Notice:   ${accountError?.message || "Could not load wallet account"}`);
    console.log(" Tip: Set your private key with: npx hardhat keystore set CORAL_PRIVATE_KEY");
  }

  console.log(" Connection Status: ONLINE & RESPONSIVE");
  console.log("========================================================\n");
}

main().catch((error) => {
  console.error("\n[FAILED] Could not connect to Coral Testnet:", error?.message || error);
  console.error("Troubleshooting steps:");
  console.error("1. Check internet connectivity to https://coralrpc.mstblockchain.com");
  console.error("2. Ensure CORAL_PRIVATE_KEY is set: npx hardhat keystore set CORAL_PRIVATE_KEY");
  console.error("3. Verify the Coral RPC endpoint status.\n");
  process.exitCode = 1;
});

