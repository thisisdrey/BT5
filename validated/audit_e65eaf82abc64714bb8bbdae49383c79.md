### Title
Airdrop recipient can frontrun `updateMerkleRoot()` to claim an allocation that governance is reducing or revoking - ([File: contracts/utils/RecurringAirdrop.sol])

### Summary
`RecurringAirdrop.updateMerkleRoot()` replaces the merkle root in a single transaction while `claimed[account]` is never reset or reconciled against the new entitlement. A recipient who sees a pending root update that reduces (or removes) their cumulative allocation can frontrun it with `claim()` under the old root, withdrawing tokens they are no longer entitled to — the same "claim before the privileged action lands" class as `CouncilMember.removeFromOffice()` being frontrun.

### Finding Description
`claim()` verifies a proof against the *current* `merkleRoot` and pays out `amount_ - claimed[msg.sender]`, where `amount_` is the cumulative entitlement encoded in the leaf:

```solidity
// contracts/utils/RecurringAirdrop.sol
uint256 _claimable = amount_ - claimed[msg.sender];
claimed[msg.sender] += _claimable;
_transferReward(msg.sender, _claimable);
```

`updateMerkleRoot()` is permissioned (`onlyGovernor`) and atomically swaps `merkleRoot`, but `claimed[]` persists across updates and `updateMerkleRoot` has no mechanism to claw back or net already-claimed amounts:

```solidity
merkleRoot = merkleRoot_;
updatedAt = block.timestamp;
```

Two frontrunning paths exist for an unprivileged attacker (any recipient EOA/contract):

1. **Allocation reduction/removal**: if round N+1 lowers a user's cumulative `amount_` (recalculation, revocation, slashing of an allocation), the user frontruns `updateMerkleRoot` and claims the round-N amount. After the update, `claimed[user] > newEntitlement`, so the excess is permanently kept — there is no negative-claim path.
2. **Lock-period semantics in `MetAirdrop`**: `_transferReward` locks MET into esMET for `updatedAt + lockPeriod - block.timestamp`, and `updatedAt` is reset by `updateMerkleRoot`. A user who prefers liquid MET (or a shorter lock) can frontrun the root update to claim under the old `updatedAt`, receiving unlocked/short-locked MET, while claiming after the update locks for up to `lockPeriod` longer — extracting better terms than intended for the same entitlement.

This is structurally identical to the reference bug: a privileged, mempool-visible state transition (council removal ↔ merkle root update) that changes what an account is owed, while the account can atomically claim under the pre-change accounting first. `nonReentrant` does not help — this is cross-transaction ordering, not reentrancy.

### Impact Explanation
Protocol-funded MET (or reward token) is transferred to accounts beyond their current entitlement. Once the new root is live, the over-claimed amount is unrecoverable (`claimed[]` only accumulates, `claim` reverts rather than netting negative deltas). Direct theft of protocol-distributed tokens; magnitude bounded by the recipient's prior-round allocation, but repeatable for every recipient included in a root update that decreases entitlements.

### Likelihood Explanation
Requires a root update that reduces some entitlement (or a desire to dodge the lock reset in `MetAirdrop`). Root updates are governor transactions visible in the mempool on mainnet, so any affected recipient with a valid old proof can bundle the claim ahead of it. The claimed-token advantage in `MetAirdrop` (avoiding the freshly-reset `lockPeriod`) is available on *every* root update, not just reductions.

### Recommendation
Record per-round claims rather than a global cumulative counter (e.g., include a round id / `updatedAt` epoch in the leaf and track `claimed[round][account]`), or expire the previous root's claims at update time. Alternatively, process the root update and a forced "checkpoint" of outstanding old-root claims in the same transaction, or require a timelock between announcing and activating a new root so reductions can't be griefed — mirroring the claim-queue fix suggested in the reference report.

### Proof of Concept
Hardhat sketch (pattern mirrors `test/MetAirdrop.test.ts`):

```ts
// round0: alice entitled to 10 MET; round1 (pending tx): alice reduced to 4 MET
// alice sees updateMerkleRoot(root1) in mempool and frontruns:
const leaf0 = generateLeaf(alice.address, parseEther('10').toString())
const proof0 = merkleTree0.getHexProof(leaf0)
await airdrop.connect(alice).claim(parseEther('10'), proof0) // pays 10 MET

// governor tx lands
await airdrop.updateMerkleRoot(merkleRoot1, proofsHash)

// claimed[alice] = 10 > new entitlement 4 -> excess 6 MET permanently kept
// claim(4, proof1) now reverts NothingToClaim; there is no clawback path
expect(await airdrop.claimed(alice.address)).to.eq(parseEther('10'))
```

For the lock-dodging variant, frontrun a root update with a claim while `updatedAt_old + lockPeriod < block.timestamp` so `_transferReward` sends liquid MET instead of locking into esMET.