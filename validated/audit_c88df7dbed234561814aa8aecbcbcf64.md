### Title
Merkle airdrop proofs are replayable across chains and contract deployments — leaf encoding lacks `chainId` and contract address - (File: contracts/utils/RecurringAirdrop.sol)

### Summary
`RecurringAirdrop.claim` verifies a Merkle leaf computed as `keccak256(abi.encodePacked(msg.sender, amount_))`. The leaf contains only the claimant address and the cumulative amount — it is not bound to `block.chainid` or `address(this)`. Any proof valid under a given `merkleRoot` is therefore valid on every `RecurringAirdrop`/`MetAirdrop` instance on every chain that registers the same root, letting an unprivileged user claim the same allocation multiple times and drain tokens reserved for other recipients.

### Finding Description
Metronome distributes MET via `MetAirdrop`, a subclass of `RecurringAirdrop` (`contracts/MetAirdrop.sol:14`), which locks claimed MET into `esMET`. The claim path is:

```solidity
// contracts/utils/RecurringAirdrop.sol:52-66
function claim(uint256 amount_, bytes32[] calldata proof_) external nonReentrant {
    if (merkleRoot == bytes32(0)) revert NothingToClaim();

    bytes32 _leaf = keccak256(abi.encodePacked(msg.sender, amount_));
    if (!MerkleProof.verify(proof_, merkleRoot, _leaf)) revert InvalidProof();

    uint256 _claimable = amount_ - claimed[msg.sender];
    if (_claimable == 0) revert NothingToClaim();
    claimed[msg.sender] += _claimable;
    _transferReward(msg.sender, _claimable);
    ...
}
```

The `_leaf` commits only to `(msg.sender, amount_)`. There is no EIP-712-style domain separator, no `block.chainid`, and no `address(this)` in the digest. The `claimed` mapping is per-contract storage, so it provides no cross-instance protection: each contract tracks claims independently.

Because Metronome is a multi-chain protocol (ProxyOFT/LayerZero bridges, `docs/cross-chain.md`), running the same airdrop snapshot on several chains or redeploying the airdrop contract with the same root on one chain are realistic operational scenarios. In both cases, a single leaf/proof authorizes `amount_` withdrawals on *each* instance — the proof is replayable exactly as in a signature-replay bug, with Merkle proofs standing in for signatures.

### Impact Explanation
Direct theft of funds. If the same root exists on two instances, a whitelisted user with entitlement `amount_` extracts `2 * amount_` (or N× across N chains/instances). The extra tokens are drawn from the contract's balance, which is funded to cover the distribution — the attacker steals allocations belonging to other claimants, leaving the contract insolvent for later legitimate claims (which revert or pay out nothing). No privileged role is needed; `claim` is a public function and the attacker only needs their own valid leaf and proof, which is public data (`proofsFileHash` is published on IPFS).

### Likelihood Explanation
- The protocol operates across multiple chains and the airdrop distribution data (the Merkle tree snapshot) is chain-agnostic by construction — the leaf deliberately excludes any chain/contract binding, so reusing the same tree across deployments is the natural way to run a multi-chain drop.
- Even on a single chain, `updateMerkleRoot` lets governance point a fresh contract (or a new campaign) at a root a user already proved against; `claimed` starts at zero on the new instance.
- The only mitigations are `nonReentrant` (irrelevant — replay is across transactions/contracts, not within a call) and the `claimed` accounting (per-instance only). Nothing in the claim path prevents replay.

### Recommendation
Bind each leaf to a single deployment. Include the chain id and contract address in the leaf encoding, e.g.:

```solidity
bytes32 _leaf = keccak256(abi.encodePacked(block.chainid, address(this), msg.sender, amount_));
```

and generate the off-chain Merkle tree with the same per-deployment tuple (`chainId`, `contract`, `account`, `cumulativeAmount`). This makes each root/proof set valid for exactly one contract on one chain, eliminating cross-chain and cross-contract replay while preserving the cumulative-amount accounting model.

### Proof of Concept
Hardhat test against `RecurringAirdrop` (simplified, local chain; the same-chain two-instance case demonstrates the missing `address(this)` binding — the missing `chainId` binding follows identically on a fork of a second chain):

```typescript
import { ethers } from "hardhat";
import { MerkleTree } from "merkletreejs";
import keccak256 from "keccak256";

it("replays the same proof on a second airdrop contract", async () => {
  const [user] = await ethers.getSigners();

  // Deploy reward token and fund two airdrop instances
  const Token = await ethers.getContractFactory("ERC20Mock");
  const token = await Token.deploy("MET", "MET", 18);
  const Airdrop = await ethers.getContractFactory("RecurringAirdrop");
  const dropA = await Airdrop.deploy(token.address);
  const dropB = await Airdrop.deploy(token.address); // e.g. re-deploy or second campaign

  const amount = ethers.utils.parseEther("100");
  await token.mint(dropA.address, amount);
  await token.mint(dropB.address, amount);

  // Tree leaf = (account, amount) — no chainId, no contract address
  const leaf = keccak256(
    ethers.utils.solidityPack(["address", "uint256"], [user.address, amount])
  );
  const tree = new MerkleTree([leaf], keccak256, { sortPairs: true });
  const root = tree.getHexRoot();
  const proof = tree.getHexProof(leaf);

  // Governor registers the same root on both contracts
  await dropA.updateMerkleRoot(root, ethers.utils.id("proofs"));
  await dropB.updateMerkleRoot(root, ethers.utils.id("proofs"));

  // Same proof claims successfully on BOTH contracts
  await dropA.connect(user).claim(amount, proof);
  expect(await token.balanceOf(user.address)).to.equal(amount);

  await dropB.connect(user).claim(amount, proof); // replay succeeds
  expect(await token.balanceOf(user.address)).to.equal(amount.mul(2)); // 2x entitlement
});
```

The second `claim` passes `MerkleProof.verify` because the leaf digest is identical on `dropB` — nothing in the leaf distinguishes the two contracts (or chains). On a multi-chain deployment using the same snapshot, the same proof yields one full claim per chain.