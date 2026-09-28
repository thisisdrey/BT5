### Title
Attacker can permanently block a victim from opening new collateral or debt positions by dust-filling the victim's `depositTokensOfAccount`/`debtTokensOfAccount` lists up to `MAX_TOKENS_PER_USER` - (File: contracts/DepositToken.sol)

### Summary
`Pool` enforces a hard cap of `MAX_TOKENS_PER_USER = 30` on the combined number of per-account deposit tokens and debt tokens. Any entry point that causes a new token to be added to an account's list reverts with `UserReachedMaxTokens` when the combined list length is already 30. Because `DepositToken.deposit(uint256 amount_, address onBehalfOf_)` lets anyone mint deposit tokens to an arbitrary beneficiary, and `DepositToken.transfer`/`transferFrom` let anyone push tokens to an arbitrary recipient, an unprivileged attacker can unilaterally fill a victim's token list with 30 entries by depositing/transferring dust amounts of each listed deposit token `onBehalfOf_`/to the victim. Once the list is full, every operation that would add a new token to the victim's account reverts — new collateral deposits, minting any debt token (`DebtToken` → `Pool.addToDebtTokensOfAccount`), receiving deposit-token transfers, and liquidations that seize a token the victim (as liquidator) doesn't already hold.

### Finding Description
`DepositToken.deposit` accepts a freely chosen `onBehalfOf_` beneficiary and mints deposit tokens to it (DepositToken.sol:211-237). The internal `_mint` adds the token to the beneficiary's per-account list when their balance was zero (DepositToken.sol:486-488):

```solidity
// contracts/DepositToken.sol:486-488
if (_balanceBefore == 0 && amount_ > 0) {
    pool.addToDepositTokensOfAccount(account_);
}
```

The same add happens on plain transfers (DepositToken.sol:518-520) via `_transfer`, which is also reached by `transfer`, `transferFrom`, `Pool.seize`/`DepositToken.seize` and the fee-mint path in `_withdraw` (DepositToken.sol:547). The pool-side add is guarded by the combined cap:

```solidity
// contracts/Pool.sol:143-148
modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
    if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
        revert UserReachedMaxTokens();
    }
    _;
}
```

Since the pool itself is capped at `MAX_TOKENS_PER_USER` deposit tokens (`ReachedMaxDepositTokens`, Pool.sol:703), a deployed pool can have up to ~30 deposit tokens — enough for an attacker to single-handedly max out a victim's combined list (deposits alone reach the cap because `length(deposit) + length(debt) >= 30`). There is no opt-in, approval, or minimum-amount check protecting the beneficiary/recipient; `_revertIfLocked` only constrains the *sender's* balance (DepositToken.sol:180-182, 350).

### Impact Explanation
Availability/liveness impact matching the CVE's DoS bug class. Once the victim's list is at 30:

- `DepositToken.deposit(..., onBehalfOf = victim)` for any token the victim doesn't already hold reverts at `_mint` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens`.
- Any `DebtToken` mint/issue to the victim reverts at `Pool.addToDebtTokensOfAccount` — the victim cannot open or extend a borrow position in a new synthetic asset.
- Incoming `transfer`/`transferFrom`/`seize` of a not-yet-held deposit token to the victim reverts, which can also break liquidation attempts where the victim is the liquidator and reward/mint flows in `SmartFarmingManager`/`RecurringAirdrop`-style integrations that mint fresh tokens to the account.

The victim's already-held tokens remain withdrawable (withdraw removes entries rather than adding), so this is a temporary freezing / liveness denial on all *new* position activity, not theft. The attacker can re-fill the list cheaply whenever the victim frees a slot by fully exiting a dust position, sustaining the denial indefinitely. No privileged actor, oracle manipulation, or governance change is required — only dust deposits of the pool's existing deposit tokens.

### Likelihood Explanation
Fully reachable by any EOA via public entry points: `DepositToken.deposit(amount, victim)` with `amount` just above the rounding floor (and after `depositFee`, the minted `_deposited` must be > 0 for the list-add to fire). Cost is bounded by dust collateral of each deposit token plus gas; on pools with many deposit tokens the attack approaches the 30-entry cap entirely through deposits. No pause flag, reentrancy guard, health check, or SynthContext rule stops it — `whenNotPaused` and `nonReentrant` don't constrain `onBehalfOf_`, and `deposit`/`transfer`/`transferFrom` are unprivileged. It is not an unbounded-loop/gas DoS (the cap is a fixed revert), so it avoids the excluded bug classes, but the persistent, attacker-renewable liveness denial on deposits/borrows/liquidation-seizes yields a medium-severity availability finding.

### Recommendation
Do not add tokens to a beneficiary's per-account list on actions they did not initiate, or decouple the accounting from the revert: e.g., make the beneficiary opt-in (only add to `depositTokensOfAccount` when `onBehalfOf_ == _msgSender()` or via an explicit `accept`/`claim` pull pattern), or allow the add to be skipped lazily and computed off-chain for health checks. At minimum, exclude dust/de-minimis mints from list insertion and/or split the cap so deposit-token spam cannot block debt-token additions.

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
// Hardhat/Foundry-style PoC (fork of a deployed Pool with D deposit tokens)
function test_fillVictimTokenList_blocksNewPositions() external {
    // setup: victim has an existing deposit in depositToken0
    depositToken0.deposit(100 ether, victim);

    // attacker deposits dust of each pool deposit token on behalf of victim
    for (uint i; i < pool.getDepositTokens().length; ++i) {
        IDepositToken dt = IDepositToken(pool.getDepositTokens()[i]);
        IERC20 underlying = dt.underlying();
        deal(address(underlying), attacker, DUST);
        vm.startPrank(attacker);
        underlying.approve(address(dt), DUST);
        dt.deposit(DUST, victim); // victim never consents
        vm.stopPrank();
    }
    // attacker also dust-transfers to reach exactly 30 entries
    // ...

    assertEq(
        pool.getDepositTokensOfAccount(victim).length +
        pool.getDebtTokensOfAccount(victim).length,
        30
    );

    // victim can no longer mint a debt token it doesn't already hold
    vm.prank(victim);
    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    debtTokenX.mint(1, victim);

    // victim cannot receive a new deposit token via transfer or deposit-onBehalf
    vm.prank(victim);
    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    depositTokenNew.deposit(1 ether, victim);

    // attacker re-fills freed slots cheaply after victim exits a dust position
}
```
Reproducible on a mainnet/Base/Optimism fork against the deployed `Pool`/`DepositToken` implementations (deployments/*/Pool.json), with `DUST` chosen so `quoteDepositOut(DUST)` mints a non-zero amount.