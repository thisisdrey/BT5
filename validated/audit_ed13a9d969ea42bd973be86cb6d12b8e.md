### Title
`RecurringAirdrop.claim` binds the merkle leaf and the payout to `msg.sender`, permanently locking rewards for leaf accounts that are contracts unable to interact with `esMET` — (File: `contracts/utils/RecurringAirdrop.sol`)

### Summary
`RecurringAirdrop.claim` encodes each merkle leaf as `keccak256(abi.encodePacked(msg.sender, amount_))` and always pays out to `msg.sender`. In the deployed `MetAirdrop` implementation, `_transferReward` does not send liquid tokens during the lock window — it calls `ESMET.lockFor(to_, amount_, _remainLockPeriod)`, which creates an esMET lock position owned by the claiming address. If an address registered in the merkle tree is a smart contract that cannot later call into esMET (e.g., a multisig/module-less contract, a gateway-style contract without an esMET integration, or any account unable to originate the required external calls), the claimed MET is locked into esMET under a position that account can never act on. There is no `to_`/recipient parameter to redirect the reward, and `claimed[msg.sender]` is incremented regardless, so retrying with a different recipient is impossible.

Relevant code:
- Leaf and payout pinned to `msg.sender`: `contracts/utils/RecurringAirdrop.sol` — `claim` builds `_leaf` from `msg.sender`, updates `claimed[msg.sender]`, and calls `_transferReward(msg.sender, _claimable)` (lines 52–66).
- Reward escrowed into an esMET position owned by the caller: `contracts/MetAirdrop.sol` — `_transferReward` calls `ESMET.lockFor(to_, amount_, _remainLockPeriod)` (lines 31–48).
- esMET interface confirming positions/lock semantics: `contracts/interfaces/external/IESMET.sol` — `lockFor`, `positions`, `MINIMUM_LOCK_PERIOD`/`MAXIMUM_LOCK_PERIOD` (lines 8–24).

### Finding Description
Same bug class as the Footium report: a merkle claim is identity-bound to `msg.sender` and the payout is force-pushed to that same identity, with no escape hatch. In Footium it was a raw ETH transfer that reverted for non-payable contracts; here the payout itself usually succeeds (`lockFor` will accept any `to_`), but the funds become functionally frozen because the resulting esMET position belongs to a contract that cannot manage it (unlock/withdraw after `unlockTime`, or transfer the position if esMET is NFT-based — this is an external contract at `0xA28D...b8`, so the exact withdrawal mechanics cannot be fully confirmed from this repo). In the post-lock-window branch (`_end < block.timestamp`), `MetAirdrop` falls back to a plain `MET.safeTransfer`, so ERC20-capable contracts are unaffected — the exposure is specifically contract leaves claiming while `updatedAt + lockPeriod` is still in the future.

### Impact Explanation
Permanent freezing of unclaimed yield: claimed MET ends up inside an esMET position owned by an address that can never extract it. The `claimed[]` mapping prevents re-claiming, and `updateMerkleRoot` rounds are cumulative (`amount_ - claimed`), so the stuck amount compounds across rounds. Funds are only recoverable via a governor `sweep`/root change, which is a privileged fallback, not a user path.

### Likelihood Explanation
Low-to-medium. It requires a merkle-tree leaf whose address is a contract without esMET interaction capability, claiming during the lock window. Smart-account and multisig recipients in airdrop trees are realistic. No privileged actor is needed to trigger it — the victim calls `claim` itself (or is induced to).

### Recommendation
Add a recipient parameter decoupled from the claim identity, mirroring the Footium fix:

```solidity
function claim(address to_, uint256 amount_, bytes32[] calldata proof_) external nonReentrant {
    if (merkleRoot == bytes32(0)) revert NothingToClaim();
    bytes32 _leaf = keccak256(abi.encodePacked(msg.sender, amount_));
    if (!MerkleProof.verify(proof_, merkleRoot, _leaf)) revert InvalidProof();
    uint256 _claimable = amount_ - claimed[msg.sender];
    if (_claimable == 0) revert NothingToClaim();
    claimed[msg.sender] += _claimable;
    _transferReward(to_, _claimable); // recipient chosen by caller
    emit RewardClaimed(msg.sender, to_, _claimable);
}
```

Optionally also let the claimant choose between locking into `esMET` and receiving liquid `MET` (if economically acceptable), since `lockFor` to an arbitrary address is the freezing step.

### Proof of Concept
Hardhat sketch against the deployed `MetAirdrop` (`deployments/mainnet/MetAirdrop.json`, contract `0x265714B10B9309a8A7A505DBFA6Cb6c39b842309`):

```ts
// victim: a minimal contract that cannot call esMET (no external-call capability)
const Victim = await ethers.getContractFactory('ContractWithoutCallbacks') // e.g. TokenHolder-like contract
const victim = await Victim.deploy()

// build tree with leaf = keccak256(victim.address ++ amount)
const leaf = generateLeaf(victim.address, amount)
const proof = merkleTree.getHexProof(leaf)

// governor updates root containing the contract leaf
await airdrop.updateMerkleRoot(root, proofsHash)

// within lockPeriod window:
// victim's owner triggers victim.claim via its only available generic path, or
// if victim is a contract that itself cannot initiate calls, the reward is
// unreachable entirely; if it can call claim(), then:
await victim.callClaim(airdrop.address, amount, proof)
// -> MetAirdrop._transferReward -> ESMET.lockFor(victim, amount, remaining)
// -> esMET position now owned by `victim`, which has no code path to unlock it

expect(await esMET.balanceOf(victim.address)).to.be.gt(0) // or positions(id).lockedAmount == amount
expect(await met.balanceOf(victim.address)).to.eq(0)
// any subsequent claim reverts with NothingToClaim once claimed[] covers amount_
```

Caveat: the PoC's "permanent" limb depends on the external `esMET` contract's unlock flow being callable only by the position owner; if esMET permissionlessly unlocks to the owner after `unlockTime`, the impact downgrades to a time-lock only. That behavior is in the external contract at `0xA28D70795a61Dc925D4c220762A4344803876bb8` and is not verifiable from this repository — a fork test should confirm it.