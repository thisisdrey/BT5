### Title
RecurringAirdrop claim leaves are not domain-bound, allowing the same merkle proof to be replayed across contract instances or chains - (File: contracts/utils/RecurringAirdrop.sol)

### Summary
`RecurringAirdrop.claim` builds each leaf as `keccak256(abi.encodePacked(msg.sender, amount_))` — nothing binds the leaf to the airdrop contract address, the chain, the token, or a specific distribution round. This is the direct analog of the `rails_multisite` bug: a credential (signed cookie / merkle leaf) minted under one "site" is valid under every site that shares the same verification material. Any second `RecurringAirdrop` instance that (a) distributes the same `token`, (b) is funded, and (c) uses a tree containing the same `(account, amount)` leaf will honor the exact same `proof_` and `amount_`, letting an unprivileged user claim the same allocation multiple times. `claimed[msg.sender]` is per-contract and resets to 0 on a fresh instance, so nothing blocks the replay.

### Finding Description
The claim path (`contracts/utils/RecurringAirdrop.sol:52-66`) verifies the leaf only against the current `merkleRoot`:

```solidity
bytes32 _leaf = keccak256(abi.encodePacked(msg.sender, amount_));
if (!MerkleProof.verify(proof_, merkleRoot, _leaf)) revert InvalidProof();
uint256 _claimable = amount_ - claimed[msg.sender];
```

- No `address(this)`, `block.chainid`, `token`, or round/epoch identifier is mixed into the leaf or the verification.
- `claimed` is a per-contract cumulative counter; on another instance or another deployment it starts at zero.
- The contract is explicitly generic ("Generic Recurring Airdrop contract", line 16), `token` is an immutable constructor argument, and `updateMerkleRoot` lets the governor reuse or rotate arbitrary trees — including the same tree a sibling deployment uses on another chain or for a new campaign.
- No modifier stops this: `nonReentrant` only guards a single call, and the function is fully public to any EOA (it uses raw `msg.sender`, so it also works through no intermediary).

Replay scenarios reachable by an unprivileged attacker:
1. Same token, second instance (same chain): the team deploys a new `RecurringAirdrop` for the same token (new campaign, migrated contract, or parallel programs). If the new tree still credits the attacker `(attacker, cumulativeAmount)` — which is expected since the leaf format is cumulative across rounds — the attacker submits the same `(amount_, proof_)` and withdraws the full cumulative amount again.
2. Cross-chain: Metronome deploys on mainnet, optimism, hemi, swell, plasma. If the same token (e.g., a bridged synthetic/MET) is airdropped on several chains with the same recipient set, each chain's contract validates the identical leaf and proof. The attacker claims the allocation on every chain.

### Impact Explanation
Each funded airdrop instance pays out the attacker's allocation once, but the attacker collects it N times (once per instance/chain sharing the tree). This drains airdrop funds that were budgeted for other recipients — theft of unclaimed yield belonging to other users — and can leave later legitimate claimants with an empty contract (permanent freezing of their unclaimed rewards). The invariant broken is one-allocation-one-claim per distribution context.

### Likelihood Explanation
Requires the protocol to operate more than one funded `RecurringAirdrop` instance for the same token whose trees overlap — a plausible operational pattern for a generic, reusable contract deployed across the many chains Metronome targets. No privileged or malicious infrastructure is needed: the attacker simply resubmits their own valid `(amount_, proof_)` on each instance. The main uncertainty is deployment configuration (whether trees/tokens actually overlap), since `merkleRoot` is governor-set; the contract code provides no defense if they do.

### Recommendation
Domain-separate the leaf, e.g. `keccak256(abi.encodePacked(block.chainid, address(this), msg.sender, amount_))` or include a per-distribution `roundId`/`token` in the leaf. Alternatively, key `claimed` by `(merkleRoot, account)` if per-round independence is intended, and never reuse identical trees across funded instances.

### Proof of Concept
```solidity
// Foundry — two instances sharing token and tree
RecurringAirdrop a = new RecurringAirdrop(token);
RecurringAirdrop b = new RecurringAirdrop(token); // e.g., new campaign / other chain fork

bytes32 leaf = keccak256(abi.encodePacked(attacker, AMOUNT));
bytes32 root = leaf; // single-leaf tree for simplicity
vm.prank(governor); a.updateMerkleRoot(root, bytes32("x"));
vm.prank(governor); b.updateMerkleRoot(root, bytes32("x"));
token.transfer(address(a), AMOUNT);
token.transfer(address(b), AMOUNT);

bytes32[] memory proof = new bytes32[](0);
vm.prank(attacker); a.claim(AMOUNT, proof); // pays AMOUNT
vm.prank(attacker); b.claim(AMOUNT, proof); // pays AMOUNT again — replay
assertEq(token.balanceOf(attacker), 2 * AMOUNT);
```

The same test applies cross-chain by forking a second network where an identically-configured instance is deployed, since the leaf contains no `chainid`.