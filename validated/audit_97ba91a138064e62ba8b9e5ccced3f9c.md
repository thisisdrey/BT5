### Title
Users can be permanently blocked from claiming airdrop rewards by underflow when a new merkle root assigns a lower cumulative amount - (contracts/utils/RecurringAirdrop.sol:58)

### Summary
`RecurringAirdrop.claim()` computes the payout as `amount_ - claimed[msg.sender]` against a lifetime `claimed` mapping that is never reset across merkle root updates. If a later distribution assigns a user a cumulative amount lower than (or the root is corrected to a value below) what they have already claimed, the subtraction underflows and reverts, making the user's remaining/new rewards permanently unclaimable. The same applies when a leaf encodes a per-period amount rather than an ever-increasing cumulative amount. `MetAirdrop` inherits this logic.

### Finding Description
In `contracts/utils/RecurringAirdrop.sol:52-66`:

```solidity
uint256 _claimable = amount_ - claimed[msg.sender];
if (_claimable == 0) revert NothingToClaim();
claimed[msg.sender] += _claimable;
```

`claimed` is a monotonically increasing per-account accumulator (`contracts/utils/RecurringAirdrop.sol:34`) and `updateMerkleRoot` (`lines 81-90`) swaps the root without touching it. The design assumes every future leaf's `amount_` is a cumulative total strictly ≥ `claimed[msg.sender]`. If any future distribution encodes `amount_ < claimed[msg.sender]` (a smaller reward epoch, a corrected/ recomputed allocation, or a leaf representing a per-round rather than cumulative amount), `claim` reverts with an arithmetic underflow — exactly the Footium `FootiumPrizeDistributor` bug class. The test suite only exercises the increasing-cumulative path (`test/MetAirdrop.test.ts:159-178`, where round-2 leaf amounts are strictly greater than round-1). `MetAirdrop` (deployed on mainnet per `deployments/mainnet/MetAirdrop.json`) overrides `_transferReward` to lock into `esMET` but reuses the same `claim`/`claimed` logic.

### Impact Explanation
Permanent freezing of unclaimed yield: affected users can never claim rewards from any root whose leaf amount is below their historical `claimed` balance, and since `claimed` only grows, the condition is irreversible. Funds remain locked in the airdrop contract (recoverable only by governor sweep).

### Likelihood Explanation
The contract is explicitly designed for *recurring* distributions — `updateMerkleRoot` exists precisely so roots change over time. Any epoch that legitimately assigns a smaller cumulative figure to an account (recomputed allocations, slashing, error correction, or switching leaf semantics to per-epoch amounts) triggers the revert. No attacker privilege is needed to be a victim, though triggering requires a governor root update, which lowers likelihood vs. a purely attacker-driven bug.

### Recommendation
Reset or epoch-scope the claimed accounting on root updates, e.g. key claims by root (`mapping(bytes32 => mapping(address => bool))` or `claimed[merkleRoot][account]`), or make leaf semantics explicitly cumulative and clamp `_claimable` to 0 instead of underflowing (`if (amount_ <= claimed[msg.sender]) revert NothingToClaim();`). The clamp option prevents reverting but still prevents double-claiming.

### Proof of Concept
```solidity
// Foundry-style PoC (Hardhat equivalent: extend test/MetAirdrop.test.ts)
// 1. Governor sets root1 where leaf(alice) = 4 MET
// 2. alice.claim(4, proof1) -> succeeds; claimed[alice] = 4
// 3. Governor calls updateMerkleRoot(root2) where leaf(alice) = 3 MET
//    (smaller/corrected cumulative allocation)
// 4. alice.claim(3, proof2) -> proof verifies, then
//    _claimable = 3 - 4 reverts (panic 0x11 arithmetic underflow)
//    -> alice can never claim under root2 or any root with amount <= 4
```

Concrete test addition mirroring `test/MetAirdrop.test.ts:159`:

```ts
await airdrop.connect(alice).claim(amount0, proof0) // claimed = amount0
await airdrop.updateMerkleRoot(merkleRootSmaller, hash)
const tx = airdrop.connect(alice).claim(smallerAmount, proofSmaller)
await expect(tx).to.be.reverted // underflow in `amount_ - claimed[msg.sender]`
```