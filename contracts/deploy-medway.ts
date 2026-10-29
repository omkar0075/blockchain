import { network } from "hardhat";

async function main() {
  console.log("Connecting to network: localhost...");
  const { viem } = await network.connect("localhost");

  console.log("Deploying MEDwayReservation contract...");
  const reservation = await viem.deployContract("MEDwayReservation");

  console.log(`MEDwayReservation deployed to: ${reservation.address}`);
}

main().catch((error) => {
  console.error("Deployment failed:", error);
  process.exitCode = 1;
});
