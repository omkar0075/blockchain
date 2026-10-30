import { network } from "hardhat";

async function main() {
  console.log("Connecting to network: coral...");
  const { viem } = await network.connect("coral");

  console.log("Deploying MEDwayReservation contract to Coral Testnet...");
  const reservation = await viem.deployContract("MEDwayReservation");

  console.log("\n========================================================");
  console.log(" MEDwayReservation deployed to Coral Testnet!");
  console.log("========================================================");
  console.log(` Contract Address: ${reservation.address}`);
  console.log("========================================================\n");
}

main().catch((error) => {
  console.error("Deployment failed:", error);
  process.exitCode = 1;
});
