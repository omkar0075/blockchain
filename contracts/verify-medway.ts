import { network } from "hardhat";

async function main() {
  console.log("Connecting to network: localhost...");
  const { viem } = await network.connect("localhost");

  // Local deployed address (can be overridden via CONTRACT_ADDRESS env variable)
  const contractAddress = (process.env.CONTRACT_ADDRESS ||
    "0x5FbDB2315678afecb367f032d93F642f64180aa3") as `0x${string}`;

  console.log(`Loading MEDwayReservation at ${contractAddress}...`);
  const reservation = await viem.getContractAt("MEDwayReservation", contractAddress);

  console.log("Reading contract state...");
  const owner = await reservation.read.owner();

  console.log("\n========================================================");
  console.log(" MEDwayReservation Contract Verification Successful!");
  console.log("========================================================");
  console.log(` Contract Address: ${contractAddress}`);
  console.log(` Contract Owner:   ${owner}`);
  console.log("========================================================\n");
}

main().catch((error) => {
  console.error("Verification failed:", error);
  process.exitCode = 1;
});
