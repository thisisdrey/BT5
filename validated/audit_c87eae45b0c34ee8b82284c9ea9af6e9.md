### Title
RecurringAirdrop Merkle leaf lacks chain ID and contract address, enabling cross-chain/cross-deployment proof replay - (File: contracts/utils/RecurringAirdrop.sol)

### Summary
`RecurringAirdrop.claim()` builds its Merkle leaf as `keccak256(abi.encodePacked(msg.sender, amount_))`, binding the proof only to the claimant address and amount. It does not include `block.chainid` or `address(this)` in the leaf. This is the same bug class as the Opera-Bridge `_verifySignatures()` issue: an authorization artifact (here a Merkle proof instead of a validator signature) that is not domain-separated can be replayed on any other deployment of the same contract that uses the same Merkle root, causing the same allocation to be paid out multiple times.

### Finding Description
In `contracts/utils/RecurringAirdrop.sol:55`, the leaf is computed as:

```solidity
bytes32 _leaf = keccak256(abi.encodePacked(msg.sender, amount_));
if (!MerkleProof.verify(proof_, merkleRoot, _leaf)) revert InvalidProof();
```

`claimed` is tracked per contract instance (`mapping(address => uint256) public claimed`, line 34), so it provides no protection across deployments. Metronome is deployed on many chains (deployments exist for mainnet, optimism, base, hemi, swell, plasma), and `RecurringAirdrop` is a generic distributor intended to be reused. If the same or an overlapping airdrop tree is published to `RecurringAirdrop` instances on two chains (or the contract is redeployed with the same root on the same chain), any user can take their valid `(account, amount, proof)` from one chain and submit it verbatim on the other, claiming the same allocation twice. In a chain-fork scenario (e.g., a fork of a supported chain where the airdrop contract and its funded token balance are duplicated), every allocation is replayable in full.

Note also that `abi.encodePacked(msg.sender, amount_)` (20 bytes + 32 bytes) has no dynamic types, so no packing collision exists — the replay is purely a missing-domain-separator issue, exactly mirroring the Opera-Bridge recommendation to add `block.chainid` and `address(this)` to the hashed data.

### Impact Explanation
Direct theft of protocol/user funds. Each replayed claim calls `token.safeTransfer(to_, _claimable)` and drains the airdrop token balance on the second deployment. If N deployments share a root, each recipient can extract N× their intended allocation; early replayers drain the contract, leaving later legitimate claimants with `NothingToClaim`/`InvalidProof`-adjacent failures or an empty token balance (insolvency of the distribution).

### Likelihood Explanation
Likelihood is conditional but realistic: it requires the same Merkle root (or a tree containing the same `(account, amount)` leaves) to be set on more than one `RecurringAirdrop` deployment holding token balances. Multi-chain airdrop campaigns routinely reuse snapshot data across chains, and the "recurring"/"generic" design of this contract makes reuse across chains an expected operating mode rather than an edge case. An attacker needs no privileges — `claim` is permissionless and `msg.sender` is the leaf's account, so any eligible claimant can self-serve the replay. No governor, oracle, or malicious relayer involvement is required; `nonReentrant` does not help since the replay is a cross-chain/cross-contract action, not a reentrancy.

### Recommendation
Include a domain separator in the leaf encoding, e.g.:

```solidity
bytes32 _leaf = keccak256(abi.encode(block.chainid, address(this), msg.sender, amount_));
```

This requires the off-chain tree builder to emit per-deployment leaves, which is the standard practice for multi-chain airdrops. Alternatively, if a single global tree is intended, incorporate a per-chain/per-contract salt field into the leaf so proofs remain verifiable but non-replayable.

### Proof of Concept
Hardhat outline:

```solidity
// Fork A (e.g., mainnet) and Fork B (e.g., optimism), deploy identical RecurringAirdrop(token) on both.
// Governor calls updateMerkleRoot(ROOT, fileHash) on both instances with the same ROOT
// containing leaf keccak256(abi.encodePacked(alice, 1000e18)).

// Fork A:
airdropA.claim(1000e18, aliceProof);   // alice receives 1000e18

// Fork B — identical proof bytes:
airdropB.claim(1000e18, aliceProof);   // succeeds again, alice receives another 1000e18
```

The second claim succeeds because `_leaf` depends only on `msg.sender` and `amount_`, and `claimed[alice]` on instance B is zero. Adding `block.chainid`/`address(this)` to the leaf makes the B-side verification fail.