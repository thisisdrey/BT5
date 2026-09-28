### Title
User can frontrun `RecurringAirdrop.updateMerkleRoot` to claim under the old, higher allocation before the new root takes effect - (File: contracts/utils/RecurringAirdrop.sol)

### Summary
`RecurringAirdrop.claim` validates proofs against the *current* `merkleRoot` and credits `claimed[msg.sender]` with the leaf amount. When the governor calls `updateMerkleRoot` to rotate the tree (e.g., to *reduce* a user's allocation in a new epoch or to correct an over-allocation), the user can frontrun the update transaction and claim under the old root, locking in the previous (higher or revoked) amount. This is the same class as the RFPSimpleStrategy issue: a value controlled by the claimant is consumed by a privileged action, and the claimant races to change/exercise it before the privileged call lands.

### Finding Description
In `contracts/utils/RecurringAirdrop.sol`:

```solidity
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
``` [1](#0-0) 

`claimed` is a single cumulative counter that persists across root updates; nothing ties `claimed` to a specific epoch/root, and `updateMerkleRoot` performs no synchronization with pending claims: [2](#0-1) 

Attack flow:
1. Epoch N root grants user `A` tokens. Governor decides to reduce the user's entitlement to `B < A` (or remove it) and broadcasts `updateMerkleRoot(newRoot, hash)`.
2. User sees the pending tx and frontruns with `claim(A, oldProof)`. Since `merkleRoot` is still the old root, the proof verifies and the user receives `A`.
3. `updateMerkleRoot` lands. `claimed[user] == A >= B`, so the reduced entitlement can never be enforced — the user extracted `A - B` tokens the governor intended to claw back.

Equivalently, a user removed entirely from the new tree still collects under the old one.

### Impact Explanation
Direct theft of unclaimed yield: the attacker receives distribution tokens in excess of what the protocol's governor ultimately allocated to them, draining the airdrop contract's token balance at the expense of the pool of funds meant for other recipients or future epochs.

### Likelihood Explanation
High when applicable: `claim` is a public, unprivileged, `nonReentrant`-protected entry point callable by any account with a valid proof, and root updates are public transactions observable in the mempool. The only precondition is that the governor reduces or removes an existing allocation, which is a normal operational event for a "recurring" airdrop (e.g., correcting the distribution). Note this requires the attacker to hold a valid leaf under the *old* root — it cannot be exploited to claim arbitrary amounts.

### Recommendation
Make claims epoch-aware: derive the leaf as `keccak256(abi.encodePacked(msg.sender, amount_, merkleRoot))` (or an incrementing epoch id) and store `claimed` per root/epoch (`mapping(bytes32 root => mapping(address => uint256))`), so that frontrunning a root update cannot carry over a balance against the new allocation. Alternatively, process queued claim reductions atomically within `updateMerkleRoot`, or require the governor to publish the new root via a commit-reveal / timelock so users cannot race it.

### Proof of Concept
Hardhat sketch (fork not strictly required since no external dependencies):

```ts
// setup: airdrop funded with tokens; root1 = tree(alice => 100)
// 1. governor sends updateMerkleRoot(root2) where root2 grants alice => 0 (or 40)
// 2. alice frontruns:
await airdrop.connect(alice).claim(100, proofForAlice100); // succeeds under root1
// 3. update lands
await airdrop.connect(governor).updateMerkleRoot(root2, hash);
// alice keeps 100 tokens; claimed[alice] = 100, so no clawback path exists
expect(await token.balanceOf(alice.address)).to.eq(parseEther('100'));
```

I did not exhaustively verify whether a deployed `RecurringAirdrop` instance is in active use with governor-planned allocation reductions, but the contract-level race is directly reproducible from the code above.

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
