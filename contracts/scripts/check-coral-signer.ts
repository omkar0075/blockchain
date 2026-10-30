import { network } from "hardhat";
import { formatEther } from "viem";

async function main() {
  console.log("Connecting to network: coral...");
  const { viem } = await network.connect("coral");

  const publicClient = await viem.getPublicClient();
  const walletClients = await viem.getWalletClients();

  const contractAddress = (process.env.CONTRACT_ADDRESS ||
    "0x8e8f0fa3541f6599affcbdeab71ce4fd5b2bf06d") as `0x${string}`;

  console.log(`Loading MEDwayReservation at: ${contractAddress}`);
  const reservation = await viem.getContractAt("MEDwayReservation", contractAddress);

  // 1. Read on-chain contract owner
  console.log("Reading contract owner from Coral Testnet...");
  const contractOwner = await reservation.read.owner();

  // 2. Read configured signer
  if (!walletClients || walletClients.length === 0) {
    throw new Error(
      "No signer/wallet client found for 'coral'. Check that CORAL_PRIVATE_KEY is set in your environment or keystore."
    );
  }

  const signer = walletClients[0];
  const signerAddress = signer.account.address;

  // 3. Check signer balance
  const balanceWei = await publicClient.getBalance({ address: signerAddress });
  const balanceMst = formatEther(balanceWei);

  // 4. Compare addresses
  const isMatch = signerAddress.toLowerCase() === contractOwner.toLowerCase();

  console.log("\n========================================================");
  console.log(" Coral Signer & Owner Verification");
  console.log("========================================================");
  console.log(` Network:             Coral Testnet (Chain ID: 54635)`);
  console.log(` Contract Address:    ${contractAddress}`);
  console.log(` On-Chain Owner:      ${contractOwner}`);
  console.log(` Configured Signer:   ${signerAddress}`);
  console.log(` Signer Balance:      ${balanceMst} MST (${balanceWei} wei)`);
  console.log("--------------------------------------------------------");

  if (isMatch) {
    console.log(" Signer Status:       VERIFIED MATCH (Signer IS the contract owner)");
    console.log(" Permissions:         Full Admin / Arbitrator authority enabled");
  } else {
    console.log(" Signer Status:       MISMATCH (Signer is NOT the contract owner)");
    console.log(" Notice:              Transactions will be submitted as an institutional participant,");
    console.log("                      not as the contract administrator.");
  }
  console.log("========================================================\n");
}

main().catch((error) => {
  console.error("\n[FAILED] Signer check failed:", error?.message || error);
  process.exitCode = 1;
});

