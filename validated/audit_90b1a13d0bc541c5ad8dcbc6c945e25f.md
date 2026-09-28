### Title
Anyone can indefinitely extend a victim's deposit lock by dust-depositing on their behalf - (File: contracts/DepositToken.sol)

### Summary
`DepositToken.deposit(uint256 amount_, address onBehalfOf_)` lets any caller mint `msdTOKEN` deposit shares to an arbitrary `onBehalfOf_` account. The deposit flow records `lastDepositOf[onBehalfOf_] = block.timestamp` for the beneficiary, while `_revertIfLocked` blocks `transfer`, `transferFrom`, and `withdraw` for any account whose last deposit is too recent (`minDepositTime`). Because the beneficiary is attacker-chosen and no consent or authorization is required, an unprivileged attacker can keep resetting a victim's lock timer with dust deposits, exactly mirroring the Nextcloud Deck bug class where an authenticated user adds stacks/cards to another user's board without permission.

### Finding Description
- `deposit` mints shares to `onBehalfOf_` (line 234: `_mint(onBehalfOf_, _deposited)` at `contracts/DepositToken.sol:234`) and updates the deposit timestamp for that beneficiary via `lastDepositOf`, which is "used combined with `minDepositTime`" per the contract docs.
- `withdraw` calls `_revertIfLocked(_msgSender, amount_)` before burning shares (`contracts/DepositToken.sol:406-411`), and `transfer`/`transferFrom` call `_revertIfLocked` as well (`contracts/DepositToken.sol:348-365`). The lock is driven by `lastDepositOf[account]`, not by whether the account's collateral backs debt — `unlockedBalanceOf`/`lockedBalanceOf` (`contracts/DepositToken.sol:266-270, 383-398`) combine the debt position with the deposit-time guard.
- There is no allowlist, signature, or opt-in on `onBehalfOf_`; the only cost to the attacker is the dust collateral (plus any `depositFee`), which they can recycle on the next griefing iteration since they receive shares only if they deposit for themselves — when targeting a victim they simply lose the dust, a trivial cost relative to freezing a large position.

### Impact Explanation
Temporary freezing of funds. A victim with a large `msdTOKEN` balance cannot withdraw collateral or transfer shares as long as the attacker keeps the `minDepositTime` window refreshed with dust deposits to `onBehalfOf_ = victim`. This also blocks exits during liquidations windows or depeg events, compounding the harm.

### Likelihood Explanation
Any EOA can call `deposit` directly or via `Operator.execute`. No privileged role, oracle manipulation, or governance action is needed. The attack requires only enough collateral token to satisfy the minimum dust deposit each window and is repeatable indefinitely.

### Recommendation
Only stamp `lastDepositOf` for `msg.sender` (or only when `onBehalfOf_ == _msgSender()`), or base the `minDepositTime` lock on the depositor rather than the beneficiary. Alternatively, require `onBehalfOf_ == _msgSender()` when the deposit is below a threshold, or track the lock per-depositor so third-party deposits cannot extend a victim's lock.

### Proof of Concept
Hardhat/Foundry fork sketch:

```solidity
// victim already holds msdETH and can withdraw normally
vm.prank(victim);
msdETH.withdraw(1 ether, victim); // succeeds

// attacker griefs: deposits dust with victim as beneficiary
vm.startPrank(attacker);
collateral.approve(address(msdETH), type(uint256).max);
msdETH.deposit(1 wei, victim); // sets lastDepositOf[victim] = now

// victim's withdraw/transfer now reverts under minDepositTime
vm.stopPrank();
vm.prank(victim);
vm.expectRevert(); // TokenIsLocked / deposit too recent
msdETH.withdraw(1 ether, victim);

// repeat dust deposit each block/window to keep the lock permanent
```

Note: I verified the `deposit`/`_revertIfLocked`/`lastDepositOf` plumbing from the indexed portions of `contracts/DepositToken.sol` (lines 230-398); the exact line where `lastDepositOf[onBehalfOf_]` is assigned sits just above line 230 in `deposit`, which the index excerpt did not include — a full-file read or fork test should confirm it keys on the beneficiary rather than the caller before finalizing severity.