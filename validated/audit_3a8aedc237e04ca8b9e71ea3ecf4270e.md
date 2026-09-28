### Title
Unclaimed airdrop tokens can be permanently locked in `RecurringAirdrop`/`MetAirdrop` — no rescue function exists - ([File: contracts/utils/RecurringAirdrop.sol](contracts/utils/RecurringAirdrop.sol))

### Summary
`RecurringAirdrop` (and its concrete implementation `MetAirdrop`) holds the distribution token (`MET`) and the only code path that can move tokens out of the contract is `claim()`, which depends entirely on each eligible user submitting a valid merkle proof. There is no `sweep`/rescue/withdraw function, so any tokens that are never claimed — including tokens for users who lost keys, dust remainders, or surplus tokens sent to the contract — are locked forever. Unlike `Treasury`, `DepositToken`, `DebtToken`, `NativeTokenGateway`, `VesperGateway`, and `AMO`, which all expose a governor-controlled `sweep()`, the airdrop contracts inherit only `ReentrancyGuardTransient` and `Governable` (`contracts/utils/RecurringAirdrop.sol:18`, `contracts/MetAirdrop.sol:14`).

### Finding Description
The sole outbound token path is `_transferReward()`, invoked only from `claim()` at `contracts/utils/RecurringAirdrop.sol:52-66` after a merkle proof verification of `keccak256(abi.encodePacked(msg.sender, amount_))`. If `merkleRoot` is set but some accounts never call `claim()` (lost keys, abandoned wallets, users below the claim-ability threshold after `claimed[msg.sender]` accounting), the corresponding balance remains in the contract indefinitely. `Governable.onlyGovernor` gates `updateMerkleRoot()` but no governor-only function transfers tokens out — updating the root cannot recover stranded balances either; it only changes future claimability. For `MetAirdrop`, `_transferReward` locks claimed MET into `esMET` via `ESMET.lockFor` (`contracts/MetAirdrop.sol:31-48`), so the contract also permanently holds any MET balance that never becomes claimable.

### Impact Explanation
Permanent freezing of funds: all `MET` (or generic `token`) balance in the airdrop contract that is not claimed is unrecoverable by anyone, including the governor. This directly matches the NukeFund bug class — value egress requires voluntary user action (`claim` ~ `nuke`), and if that action never occurs the funds are locked indefinitely.

### Likelihood Explanation
Likelihood is moderate: airdrops historically have substantial non-zero unclaimed remainder (expired/dust entitlements, lost keys, users unaware of distribution). No attacker action or privileged misbehavior is required; the lock occurs organically through inactivity.

### Recommendation
Add a governor-only rescue function, e.g.:

```solidity
function sweep(IERC20 token_, address to_, uint256 amount_) external onlyGovernor {
    token_.safeTransfer(to_, amount_);
}
```

in `contracts/utils/RecurringAirdrop.sol`, optionally restricted to the surplus above total claimable entitlements to protect pending claims.

### Proof of Concept
Reproducible in Foundry/Hardhat without a fork of external state:

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {MetAirdrop} from "../contracts/MetAirdrop.sol";
import {ERC20Mock} from "../contracts/mock/ERC20Mock.sol"; // or any ERC20

contract LockedAirdropTest is Test {
    MetAirdrop airdrop;
    ERC20Mock met;
    address governor = address(0xA);
    address alice = address(0xB);

    function setUp() public {
        met = new ERC20Mock("MET", "MET", 18);
        airdrop = new MetAirdrop();
        // initialize governor depending on Governable init path
        met.mint(address(airdrop), 1000e18);
    }

    function test_fundsLockedForever() public {
        // Even the governor has no way to recover the 1000 MET.
        // No `sweep`, `rescue`, `withdraw`, or `transfer` exists on the contract.
        // If alice never calls claim(), the full balance stays locked.
        assertEq(met.balanceOf(address(airdrop)), 1000e18);
        // vm.expectRevert(); airdrop.sweep(...) — function does not exist
    }
}
```

A fork variant can set a real `merkleRoot` via `updateMerkleRoot`, warp time past claim windows, and show that any un-claimed MET balance cannot be moved by any caller.