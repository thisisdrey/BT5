### Title
Attacker can fill a victim's `depositTokensOfAccount` list with dust transfers, blocking new deposits - ([File: contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
`Pool` enforces a hard cap `MAX_TOKENS_PER_USER = 30` on the combined `debtTokensOfAccount + depositTokensOfAccount` lists [1](#0-0) , reverting with `UserReachedMaxTokens` in `onlyIfAdditionWillNotReachMaxTokens` once reached [2](#0-1) . `DepositToken._transfer` unconditionally calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance goes from 0 to nonzero — no opt-in or minimum amount [3](#0-2) . The same happens on mint via `deposit` (line 486-488). An unprivileged attacker who holds dust of each listed DepositToken can spam 1-wei transfers to a victim and permanently occupy victim slots, analogous to OpenQ's `nftDeposits` never shrinking.

### Finding Description
- Attacker deposits a tiny amount of underlying into every DepositToken registered in the pool (or acquires dust on secondary markets).
- For each token, attacker calls `DepositToken.transfer(victim, 1)` → `_transfer` → `pool.addToDepositTokensOfAccount(victim)` [4](#0-3) .
- Only the sender's `_revertIfLocked` is checked; the recipient cannot refuse, and `addToDepositTokensOfAccount` only validates that the caller is a real deposit token and the cap isn't yet hit [5](#0-4) .
- Once `debtTokensOfAccount.length(victim) + depositTokensOfAccount.length(victim) >= 30`, every subsequent `deposit` of a *new* collateral type, and every `transfer`/`seize` of a token the victim doesn't already hold, reverts with `UserReachedMaxTokens`.

### Impact Explanation
The victim is unable to open positions in new collateral types (including via `SmartFarmingManager` leverage flows that end in mint) until they manually clear entries — a temporary freezing of deposit functionality. Worse, a victim near the liquidation threshold who is mid-transaction adding a *new* collateral can be front-run with dust fills so the deposit reverts, leaving them liquidatable. Clearing slots is only possible by transferring the full dust balance of each spam token back out (sender-side removal at zero balance, lines 522-525), which costs gas per token and can be re-griefed cheaply, since the attacker's cost per slot is ~1 wei of a deposit token.

### Likelihood Explanation
Requires only public `transfer` calls and dust balances of registered deposit tokens — no privileged role. Cost scales with the number of distinct deposit tokens in the pool (bounded by the pool's own `ReachedMaxDepositTokens` cap). The limitation: the victim can self-recover by emptying the dust balances, so the DoS is temporary and requires the victim to hold no more than 29 legitimate entries and to notice the attack.

### Recommendation
Skip `addToDepositTokensOfAccount` (and/or revert) when the received amount is below a dust threshold; alternatively, allow the account itself to call `removeFromDepositTokensOfAccount` for tokens it wants to delist, or only add tokens on `deposit`/mint rather than on unsolicited `transfer`/`seize`.

### Proof of Concept
Hardhat outline (mirroring `test/Pool.test.ts:1386-1416`):

```ts
// attacker holds dust of N deposit tokens registered in pool
for (const dt of depositTokens) {
  await dt.connect(attacker).transfer(victim.address, 1); // fills victim's list
}
// victim tries to deposit a collateral type they don't hold yet
await expect(
  newDepositToken.connect(victim).deposit(amount, victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
// front-run variant: execute fills in the same block before victim's
// collateral top-up when victim is near the liquidation threshold
```

Note: I did not fully verify whether any frontend/operational mitigation exists on deployed pools (e.g., few registered deposit tokens making 30 slots unreachable); validity depends on the pool having enough distinct DepositTokens for an attacker to approach the cap.

### Citations

**File:** contracts/Pool.sol (L79-79)
```text
    uint256 public constant MAX_TOKENS_PER_USER = 30;
```

**File:** contracts/Pool.sol (L143-148)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
    }
```

**File:** contracts/Pool.sol (L216-220)
```text
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
    }
```

**File:** contracts/DepositToken.sol (L348-354)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
    }
```

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```
