### Title
`RecurringAirdrop::claim()` permanently locks a user's rewards if a new merkle root assigns a cumulative amount lower than `claimed[account]` - ([File: contracts/utils/RecurringAirdrop.sol](contracts/utils/RecurringAirdrop.sol))

### Summary
`RecurringAirdrop` distributes tokens via governor-updated merkle roots where each leaf encodes `(account, cumulativeAmount)`. `claim()` computes the payout as `amount_ - claimed[msg.sender]` and assumes leaf amounts are strictly monotonically increasing across root updates. If a new root ever assigns a user a cumulative amount that is less than or equal to their `claimed[]` balance, the subtraction underflows (or yields 0 and reverts with `NothingToClaim`), permanently freezing that user's remaining allocation in the contract — the same cumulative-amount design flaw as `TrufMigrator::migrate()`.

### Finding Description
In `contracts/utils/RecurringAirdrop.sol:52-66`:

```solidity
function claim(uint256 amount_, bytes32[] calldata proof_) external nonReentrant {
    if (merkleRoot == bytes32(0)) revert NothingToClaim();

    bytes32 _leaf = keccak256(abi.encodePacked(msg.sender, amount_));
    if (!MerkleProof.verify(proof_, merkleRoot, _leaf)) revert InvalidProof();

    uint256 _claimable = amount_ - claimed[msg.sender];   // underflows if amount_ < claimed
    if (_claimable == 0) revert NothingToClaim();

    claimed[msg.sender] += _claimable;
    _transferReward(msg.sender, _claimable);
    emit RewardClaimed(msg.sender, _claimable);
}
``` [1](#0-0) 

`claimed` is a per-account cumulative counter that is never reset or namespaced when `updateMerkleRoot` rotates the tree [2](#0-1) . The design implicitly assumes every successive root contains cumulative amounts ≥ all previously claimed amounts for every account. Nothing enforces this on-chain:

- If a new root gives a user `amount_ < claimed[user]` (allocation correction, a root computed per-epoch rather than cumulative, or an off-chain accounting error), the `amount_ - claimed[msg.sender]` subtraction reverts with an arithmetic underflow — the user's valid, provable allocation can never be claimed.
- There is no escape hatch: `claim` is the only way to pull funds, and only the governor-controlled `sweep`/`updateMerkleRoot` can remediate.

This is deployed as `MetAirdrop` on mainnet (locking into esMET via `_transferReward` override), so it protects real user reward balances [3](#0-2) .

### Impact Explanation
Permanent freezing of unclaimed user rewards. Any user whose cumulative entitlement in the active root is below their `claimed[]` total loses access to their entire remaining allocation; aggregated across users this can lock a large token balance in the contract, recoverable only through governance intervention (new root or `sweep`). This matches the "permanent freezing of funds / freezing of unclaimed yield" acceptance criterion.

### Likelihood Explanation
The trigger is a root update where at least one account's cumulative leaf value is lower than its already-claimed total — e.g., an airdrop re-computation that corrects over-allocations, or epoch-scoped (non-cumulative) trees. Because `updateMerkleRoot` is governor-only, this isn't attacker-triggerable, but it is a real design fragility identical in class to the reported `TrufMigrator` issue: the contract encodes an unstated monotonicity invariant that, once violated (even accidentally, once, in an off-chain generated tree), irreversibly locks funds with no in-contract recovery path.

### Recommendation
Make `amount_` a per-epoch claimable amount keyed to the root, or track claims per-root instead of globally:

```diff
+ mapping(bytes32 => mapping(address => bool)) public claimedForRoot;

function claim(uint256 amount_, bytes32[] calldata proof_) external nonReentrant {
    if (merkleRoot == bytes32(0)) revert NothingToClaim();
    bytes32 _leaf = keccak256(abi.encodePacked(msg.sender, amount_));
    if (!MerkleProof.verify(proof_, merkleRoot, _leaf)) revert InvalidProof();
-   uint256 _claimable = amount_ - claimed[msg.sender];
-   if (_claimable == 0) revert NothingToClaim();
-   claimed[msg.sender] += _claimable;
+   if (claimedForRoot[merkleRoot][msg.sender]) revert NothingToClaim();
+   claimedForRoot[merkleRoot][msg.sender] = true;
+   uint256 _claimable = amount_;
    _transferReward(msg.sender, _claimable);
    emit RewardClaimed(msg.sender, _claimable);
}
```

Alternatively, keep the cumulative scheme but clamp safely and let users at least claim `max(0, amount_ - claimed)` semantics by allowing `claimed` bookkeeping per root, or add an explicit `claimed` reset path when a root rotation intentionally lowers allocations.

### Proof of Concept
Hardhat test against the existing `MetAirdrop.test.ts` fixture pattern [4](#0-3) :

```ts
it('locks funds when new root amount < claimed', async () => {
  // round 1: alice has 100 MET entitlement
  const amount0 = parseEther('100')
  const tree0 = buildTree({ [alice.address]: amount0 })
  await airdrop.updateMerkleRoot(tree0.root, HASH)
  await airdrop.connect(alice).claim(amount0, tree0.proof(alice.address, amount0))
  expect(await airdrop.claimed(alice.address)).eq(amount0)

  // round 2: corrected tree assigns alice only 50 cumulative
  const amount1 = parseEther('50')
  const tree1 = buildTree({ [alice.address]: amount1 })
  await airdrop.updateMerkleRoot(tree1.root, HASH)

  // alice's valid leaf now reverts with arithmetic underflow — permanently
  await expect(
    airdrop.connect(alice).claim(amount1, tree1.proof(alice.address, amount1))
  ).to.be.revertedWithPanic(0x11) // arithmetic underflow
})
```

The revert leaves `amount1`'s allocation (and any future roots' delta) unreachable because `claimed[alice]` stays at 100 while no leaf with `amount_ > 100` exists.

### Citations

**File:** contracts/utils/RecurringAirdrop.sol (L52-66)
```text
    function claim(uint256 amount_, bytes32[] calldata proof_) external nonReentrant {
        if (merkleRoot == bytes32(0)) revert NothingToClaim();

        bytes32 _leaf = keccak256(abi.encodePacked(msg.sender, amount_));
        if (!MerkleProof.verify(proof_, merkleRoot, _leaf)) revert InvalidProof();

        uint256 _claimable = amount_ - claimed[msg.sender];
        if (_claimable == 0) revert NothingToClaim();

        claimed[msg.sender] += _claimable;

        _transferReward(msg.sender, _claimable);

        emit RewardClaimed(msg.sender, _claimable);
    }
```

**File:** contracts/utils/RecurringAirdrop.sol (L81-90)
```text
    function updateMerkleRoot(bytes32 merkleRoot_, bytes32 proofsFileHash_) external onlyGovernor {
        if (merkleRoot_ == merkleRoot) revert NewMerkleRootSameAsCurrent();
        if (proofsFileHash_ == bytes32(0)) revert ProofsFileIsNull();

        merkleRoot = merkleRoot_;
        updatedAt = block.timestamp;
        proofsFileHash = proofsFileHash_;

        emit MerkleRootUpdated(merkleRoot_, block.timestamp);
    }
```

**File:** deployments/mainnet/MetAirdrop.json (L418-450)
```json
      "claim(uint256,bytes32[])": {
        "details": "Every tree leaf is a `[account, amount]` tuple, we assume that the `msg.sender` is the account",
        "params": {
          "amount_": "The amount to claim",
          "proof_": "The merkle tree proof for the given leaf"
        }
      },
      "sweep(address,address,uint256)": {
        "params": {
          "amount_": "The amount to send",
          "to_": "The recipient of the transfer",
          "token_": "The token to transfer"
        }
      },
      "transferGovernorship(address)": {
        "details": "Can only be called by the current owner.",
        "params": {
          "proposedGovernor_": "The new proposed governor"
        }
      },
      "updateLockPeriod(uint256)": {
        "params": {
          "lockPeriod_": "The new value"
        }
      },
      "updateMerkleRoot(bytes32,bytes32)": {
        "params": {
          "merkleRoot_": "The merkle root"
        }
      }
    },
    "title": "MET Airdrop contract",
    "version": 1
```

**File:** test/MetAirdrop.test.ts (L159-178)
```typescript
    it('should receive correct rewards when claiming both rounds', async function () {
      // given
      const amount0 = rewards0[alice.address]
      const leaf0 = generateLeaf(alice.address, amount0)
      const proof0 = merkleTree0.getHexProof(leaf0)
      const tx0 = airdrop.connect(alice).claim(amount0, proof0)
      await expect(tx0).changeTokenBalance(met, esMET, amount0)
      expect(await airdrop.claimed(alice.address)).eq(amount0)

      // when
      await airdrop.updateMerkleRoot(merkleRoot1, `0x${randomBytes(32).toString('hex')}`)
      const amount1 = rewards1[alice.address]
      const leaf1 = generateLeaf(alice.address, amount1)
      const proof1 = merkleTree1.getHexProof(leaf1)
      const tx1 = airdrop.connect(alice).claim(amount1, proof1)

      // then
      await expect(tx1).changeTokenBalance(met, esMET, BigNumber.from(amount1).sub(amount0))
      expect(await airdrop.claimed(alice.address)).eq(amount1)
    })
```
