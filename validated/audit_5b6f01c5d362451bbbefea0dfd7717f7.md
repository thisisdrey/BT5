### Title
First-time depositors are credited rewards for the entire accrual period via the `INITIAL_INDEX` fallback, draining previously accrued yield - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
The reference bug is a direction mismatch: the mailbox buffer is mapped `DMA_TO_DEVICE`, so data the NPU writes back is never observed by the CPU — the caller reads a stale snapshot instead of the freshly-written value. The analogous defect in Metronome lives in `RewardsDistributor._calculateTokenDelta()`: when an account has no recorded `accountIndexOf` (value `0`, i.e., the account's checkpoint was never "written back"), the code substitutes the stale `INITIAL_INDEX` instead of the current global index. The account is then credited `balance * (currentIndex - INITIAL_INDEX)` — rewards that accrued before the account held any tokens — mirroring the "stale value read instead of the fresh one" semantics of the kernel bug.

### Finding Description
In `_calculateTokenDelta` (contracts/RewardsDistributor.sol:217-231):

```solidity
uint256 _accountIndex = accountIndexOf[token_][account_];

if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}

uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

`accountIndexOf` is a `mapping` that defaults to `0`. The intended semantics (standard Compound-style accounting) is that a new account should start accruing from the *current* index. Instead, the fallback sets the account's baseline to `INITIAL_INDEX`, so the delta covers the whole period since reward tracking began.

The same stale-baseline read happens on every entry point that updates accrual:

- `updateBeforeMintOrBurn(token_, account_)` (line 175) — called by `DepositToken`/`DebtToken` on mint/burn, i.e., on deposit, withdraw, issue, repay.
- `updateBeforeTransfer` (line 186) — called on token transfers; a dust `transferFrom`/transfer to a fresh address triggers it too.
- `claimRewards` (line 150) — callable publicly for any account list, then `_transferRewardIfEnoughTokens` (line 248) pays out `tokensAccruedOf[account_]` if the distributor holds enough `rewardToken`.

So an attacker can deposit a large collateral amount (even flash-borrowed, since no hold duration is required), have `updateBeforeMintOrBurn` fire during `DepositToken.deposit`, get credited `balance * (currentIndex - INITIAL_INDEX)`, withdraw in the same or next transaction, and call `claimRewards` to drain the reward token balance.

### Impact Explanation
Direct theft of unclaimed yield. All rewards that legitimately accrued to existing holders since the index started are claimable by a brand-new account proportional to its instantaneous balance. With a sufficiently large deposit (flash loans or attacker-owned tokens are explicitly in scope), the attacker captures nearly the entire distributor reward balance, leaving honest holders' `tokensAccruedOf` underfunded (the transfer silently under-pays via `amount_ <= _balance` check). This is "theft of unclaimed yield," an accepted impact class.

### Likelihood Explanation
Fully reachable by an unprivileged EOA through public entry points (`DepositToken.deposit`, `DepositToken.transfer`, `RewardsDistributor.claimRewards`). No privileged role, oracle failure, or governance action is required — only that `tokenStates[token].index > INITIAL_INDEX`, i.e., reward distribution has been running. The `nonReentrant` guard does not help because the exploit uses a normal call sequence, not reentrancy. The only limiting factor is the reward token balance held by the distributor at claim time, but the attacker can time the claim or sandwich other users' claims.

### Recommendation
When `accountIndexOf[token_][account_] == 0`, initialize the account baseline to the *current* `_tokenIndex` (not `INITIAL_INDEX`), so a first-time accrual yields `_deltaIndex == 0`. Equivalently, skip the `INITIAL_INDEX` fallback in `_calculateTokenDelta` and set `accountIndexOf` to the fresh index inside `_updateTokensAccruedOf`/`updateBeforeTransfer` before computing the delta — the write-back must happen before the read, paralleling the `DMA_BIDIRECTIONAL` fix in the kernel patch.

### Proof of Concept
Hardhat-style reproduction (repo's own test stack; `deposit`/`rewardsDistributor` fixtures exist in `test/`):

```ts
// Setup: rewards for msdVaDAI have been accruing; index >> INITIAL_INDEX.
// Existing holders (bob) have accrued rewards. `rewardToken` funds RewardsDistributor.

// 1. Attacker (alice) has never held msdVaDAI: accountIndexOf == 0.
expect(await rewardsDistributor.accountIndexOf(msdVaDAI.address, alice.address)).eq(0)

// 2. Alice deposits a large amount (balance flash-able; no hold time needed).
await vaDAI.connect(alice).approve(msdVaDAI.address, MaxUint256)
await msdVaDAI.connect(alice).deposit(largeAmount, alice.address)
//    -> DepositToken calls rewardsDistributor.updateBeforeMintOrBurn
//    -> _calculateTokenDelta uses INITIAL_INDEX fallback
//    -> tokensAccruedOf[alice] = largeAmount * (currentIndex - INITIAL_INDEX)

// 3. Alice withdraws immediately — she keeps the accrued credit.
await msdVaDAI.connect(alice).withdraw(largeAmount, alice.address)

// 4. Alice claims the stolen rewards.
const balBefore = await rewardToken.balanceOf(alice.address)
await rewardsDistributor.claimRewards(alice.address, [msdVaDAI.address])
expect(await rewardToken.balanceOf(alice.address)).gt(balBefore)

// 5. Honest holders' accrued rewards can no longer be fully paid:
//    distributor balance is drained; their claimRewards under-pays or pays 0.
```

Note on verification limits: I confirmed the vulnerable fallback logic and its callers (`updateBeforeMintOrBurn`, `updateBeforeTransfer`, `claimRewards`) and that `DepositToken`/`DebtToken` invoke `updateBeforeMintOrBurn` on mint/burn. I did not exhaustively confirm every configured deployment has a non-zero `tokenSpeeds`/reward token balance, which is a precondition for real fund loss, but the code path is unconditionally exploitable once rewards are active.