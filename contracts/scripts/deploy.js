// Deployment script for TradeAuditTrail.sol
// Run with: npx hardhat run scripts/deploy.js --network xltestnet

const hre = require("hardhat");

async function main() {
  console.log("Deploying TradeAuditTrail contract...");

  const [deployer] = await hre.ethers.getSigners();
  console.log("Deploying with account:", deployer.address);
  console.log("Account balance:", (await deployer.getBalance()).toString());

  const TradeAuditTrail = await hre.ethers.getContractFactory("TradeAuditTrail");
  const contract = await TradeAuditTrail.deploy();
  await contract.waitForDeployment();

  const address = await contract.getAddress();
  console.log("TradeAuditTrail deployed to:", address);
  console.log("Block explorer: https://www.okx.com/explorer/xlayerTestnet/address/" + address);

  // Verify on Etherscan equivalent (OKX Explorer)
  if (process.env.VERIFY_CONTRACT === "true") {
    console.log("Waiting for block confirmation...");
    await new Promise(resolve => setTimeout(resolve, 15000));
    await hre.run("verify:verify", {
      address: address,
      constructorArguments: [],
    });
    console.log("Contract verified on explorer");
  }
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
