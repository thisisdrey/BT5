[1](#0-0) ### Title
Attacker can permanently DoS a victim's deposits (and `seize` receipt) of new collateral types by filling their `depositTokensOfAccount` list via dust `DepositToken.transfer` - (File: contracts/DepositToken.sol)

### Summary
`DepositToken` is a freely transferable ERC-20. Both `_mint` and `_transfer` call `Pool.addToDepositTokensOfAccount(recipient)` whenever the recipient's prior balance is zero. `Pool` caps this per-account list at `MAX_TOKENS_PER_USER` and reverts once it is full. An unprivileged attacker can deposit once into each of the pool's deposit tokens, then `transfer` dust (`amount_ > 0` is the only requirement) to a victim until the victim's list is saturated. After that, any `_mint` or `_transfer` that would add a *new* deposit token to the victim's list reverts: the victim can no longer `deposit` a collateral type they don't already hold, and `Pool.liquidate`'s `seize` reverts if the liquidator's list is full (self-inflicted, but it also means the victim's `withdraw`/`transfer` of their last unit of a token is fine while any *new* token they try to acquire reverts). This is the Solidity analog of the NASM bug class — attacker-crafted input causes an operation to hit a hard bound and unconditionally revert, i.e., input-driven denial of service rather than state corruption.

### Finding Description
- `DepositToken.deposit(amount_, onBehalfOf_)` → `_mint(onBehalfOf_, _deposited)` → if `balanceOf[account_] == 0`, `pool.addToDepositTokensOfAccount(account_)` (`contracts/DepositToken.sol` lines 485–488). [2](#0-1) 
- `DepositToken.transfer(to_, amount_)` only checks the *sender's* unlocked balance via `_revertIfLocked(_msgSender, amount_)`, then `_transfer` adds the recipient to the list when their balance was zero (`contracts/DepositToken.sol` lines 348–353, 517–520). [3](#0-2) [4](#0-3) 
- `Pool.addToDepositTokensOfAccount` enforces `MAX_TOKENS_PER_USER` and reverts when the account's `MappedEnumerableSet` is full (matches for `MAX_TOKENS_PER_USER`/`addToDepositTokensOfAccount` in `contracts/Pool.sol`). [5](#0-4) 
- No lock, health, pause, reentrancy, or SynthContext check stops it: `transfer` is unauthenticated beyond the sender's own unlocked balance, the attacker only needs dust quantities of each listed deposit token, and receiving tokens is permissionless — the victim cannot opt out. [6](#0-5) 

Attack trace (all public, unprivileged):
1. Attacker deposits a minimal amount into every `DepositToken` of the pool (N tokens).
2. For N distinct deposit tokens, call `transfer(victim, 1 wei)` — each call appends to `victim`'s `depositTokensOfAccount` until `MAX_TOKENS_PER_USER` is reached.
3. Victim calls `deposit(x, victim)` on any collateral they do not yet hold → `_mint` → `addToDepositTokensOfAccount` → revert. Same for anyone transferring/sending a new deposit token to them, and for `seize` → `_transfer` to a recipient whose list is full.

### Impact Explanation
Temporary freezing of funds / griefing: the victim is blocked from depositing into any collateral token not already in their list until they clear a slot (transferring a dust balance to zero via `transfer` or `withdraw`, which they can do themselves). Positions and withdrawals are not frozen. Cost to the attacker is N deposit minimums plus N transactions; the attack is repeatable against any account, including freshly created smart-contract wallets that may not be able to execute the recovery transactions, making the freeze effectively permanent for that class of victim.

### Likelihood Explanation
Medium-low. Requires no privileges, no oracle manipulation, and only standard ERC-20 transfers — but impact is bounded: the victim retains full access to existing positions and can self-recover by emptying a dust slot, so this is a temporary deposit-blocking grief rather than theft or insolvency. It is most damaging for contracts/multisigs holding deposits that cannot easily perform the recovery `transfer`.

### Recommendation
- Do not mutate `depositTokensOfAccount` on plain `transfer`/`transferFrom`, or make `addToDepositTokensOfAccount` failure non-fatal for transfers (skip list insertion; the list is only an enumeration aid for `debtPositionOf` gas bounding).
- Alternatively, add a `sweep`-style public function that lets anyone remove zero-balance entries, or gate list insertion behind `deposit` only.

### Proof of Concept
```solidity
// Foundry fork test (schematic)
// assume pool has >= MAX_TOKENS_PER_USER deposit tokens: dt[0..N-1]
address victim = makeAddr("victim");
for (uint i; i < N; ++i) {
    IERC20 underlying = dt[i].underlying();
    deal(address(underlying), attacker, 1e18);
    underlying.approve(address(dt[i]), 1e18);
    dt[i].deposit(1e18, attacker);      // attacker holds dt[i]
    dt[i].transfer(victim, 1);          // fills victim's list by 1
}
// victim's depositTokensOfAccount.length == MAX_TOKENS_PER_USER

// victim now tries to deposit a collateral they don't hold (dtNew)
deal(address(underlyingNew), victim, 1e18);
vm.prank(victim);
underlyingNew.approve(address(dtNew), 1e18);
vm.prank(victim);
vm.expectRevert(); // MaxTokensPerUser-style revert from Pool.addToDepositTokensOfAccount
dtNew.deposit(1e18, victim);
```

Uncertain points: the exact revert name and cap value in `Pool.addToDepositTokensOfAccount`/`PoolStorage` were confirmed to exist by grep but the function body was not read; the PoC above assumes the standard Metronome `MAX_TOKENS_PER_USER` bound — verify the constant and error selector in `contracts/Pool.sol`/`contracts/storage/PoolStorage.sol` before finalizing the test.

### Citations

**File:** contracts/DepositToken.sol (L348-353)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
```

**File:** contracts/DepositToken.sol (L485-488)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L498-525)
```text
    function _transfer(
        address sender_,
        address recipient_,
        uint256 amount_
    ) private updateRewardsBeforeTransfer(sender_, recipient_) {
        if (sender_ == address(0)) revert TransferFromTheZeroAddress();
        if (recipient_ == address(0)) revert TransferToTheZeroAddress();

        uint256 _senderBalanceBefore = balanceOf[sender_];
        if (_senderBalanceBefore < amount_) revert TransferAmountExceedsBalance();
        uint256 _recipientBalanceBefore = balanceOf[recipient_];

        unchecked {
            balanceOf[sender_] = _senderBalanceBefore - amount_;
            balanceOf[recipient_] += amount_;
        }

        emit Transfer(sender_, recipient_, amount_);

        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```

**File:** contracts/Pool.sol (L1-5)
```text
// SPDX-License-Identifier: MIT

pragma solidity 0.8.24;

import {Initializable} from "./dependencies/openzeppelin-upgradeable/proxy/utils/Initializable.sol";
```
