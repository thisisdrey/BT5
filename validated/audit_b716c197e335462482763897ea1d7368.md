### Title
Dust-transfer griefing fills a victim's per-account token list (`MAX_TOKENS_PER_USER`), reverting all new collateral deposits, new debt positions, and token receipts for that account - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
CVE-2019-6467 is an assertion-failure/DoS where an edge-case configuration (an alternate namespace that is a descendant of a locally served zone) causes the server to hit a hard `assert` and crash. The Metronome analog is a hard revert (`UserReachedMaxTokens`) triggered on a boundary condition of a per-account "namespace" — the `depositTokensOfAccount` / `debtTokensOfAccount` lists. An unprivileged attacker can force the victim's combined list to the `MAX_TOKENS_PER_USER` = 30 limit simply by transferring 1 wei of each whitelisted `DepositToken` to the victim. Once filled, every code path that would add a *new* token to the victim's list reverts, denying the victim core protocol functionality.

### Finding Description
`Pool` tracks per-account deposit and debt tokens in `MappedEnumerableSet` lists. `addToDepositTokensOfAccount` and `addToDebtTokensOfAccount` are guarded by `onlyIfAdditionWillNotReachMaxTokens`, which reverts with `UserReachedMaxTokens` when `debtTokensOfAccount.length + depositTokensOfAccount.length >= MAX_TOKENS_PER_USER` (30) [1](#0-0) [2](#0-1) .

Critically, insertion is forced on the *recipient* of a transfer, not just on a voluntary deposit. `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's prior balance was zero and `amount_ > 0` — there is no opt-in [3](#0-2) . The same unconditional insertion happens in `DepositToken._mint` for deposit recipients [4](#0-3) . The sender-side check `_revertIfLocked` only constrains the *attacker's* unlocked balance, which the attacker fully controls [5](#0-4) .

Attack flow (all public entry points, no privileged role):

1. For each of the pool's whitelisted `DepositToken`s (up to 30 total across `depositTokens`), the attacker calls `deposit(dustAmount, attacker)` and then `transfer(victim, 1)`.
2. Each transfer inserts that token into `depositTokensOfAccount[victim]` until the combined count reaches 30.
3. Thereafter, for the victim:
   - `DepositToken.deposit(amount, victim)` reverts inside `_mint` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens` for any deposit token the victim doesn't already hold — including deposits initiated by the victim themselves.
   - `DebtToken.issue` (mint) reverts for any synthetic asset whose `DebtToken` isn't already in the victim's list (same modifier via `addToDebtTokensOfAccount`).
   - Any third-party `transfer`/`transferFrom`/`seize` crediting the victim a *new* deposit token reverts. This also taints liquidation: `Pool.liquidate` → `depositToken_.seize(account_, _msgSender, _toLiquidator)` → `_transfer` inserts the seized token into the *liquidator's* list — a liquidator at the cap cannot be paid in a collateral type they don't already hold [6](#0-5) .

### Impact Explanation
Liveness / temporary freezing of funds: the victim is denied the ability to open new collateral positions, issue new synthetic debt, or receive any deposit-token type not already in their list, for as long as the attacker keeps the list saturated (the attacker can re-fill a slot in the same transaction or with a bot whenever the victim clears one). A victim relying on an Operator/gateway or SmartFarmingManager flow (which deposits `onBehalfOf`) is equally bricked. The attacker's cost is dust deposits (recoverable via `withdraw`) plus gas; no privilege, no oracle manipulation, and no special configuration is needed — it works on the deployed configuration since `MAX_TOKENS_PER_USER` is a hardcoded constant [7](#0-6) .

### Likelihood Explanation
The attack requires only that the pool have multiple whitelisted deposit/debt tokens (the deployed pools do). The victim cannot prevent forced insertion — there is no "recipient consent" or whitelist check on `addToDepositTokensOfAccount` beyond it being called by a registered token contract. Unwinding requires the victim to zero out each dust balance (`_transfer` removes the token only when the sender's balance reaches zero [8](#0-7) ), which costs gas per token and can be front-run/re-griefed since the attacker's cost per refill is near zero. The analogous invariant break to the CVE — an unexpected overlap forcing a hard abort — maps exactly: the per-account list cap is an asserted bound that an attacker forces the victim to hit.

### Recommendation
- Do not add tokens to a recipient's account list on inbound `transfer`/`seize`/`mint` unconditionally; either remove the per-account enumerable (compute positions differently) or make insertion lazy and non-reverting: on `UserReachedMaxTokens` conditions, let `addToDepositTokensOfAccount` return `false` instead of reverting inside `_transfer`/`_mint`, and track collateral contribution by balance rather than set membership.
- Alternatively, exempt unsolicited dust: only add the token when the transferred amount exceeds a minimum threshold, or allow `removeFromDepositTokensOfAccount` to be called cheaply in batch by the account owner.
- At minimum, document that the victim's escape (zeroing each dust balance) must not be gated by `_revertIfLocked`, so a victim with a healthy position can always self-clean.

### Proof of Concept
Hardhat (TypeScript, forked deployed pool or fixture with ≥2 deposit tokens):

```ts
// victim has no positions; pool has deposit tokens msdA, msdB, ..., up to cap
const victim = bob.address
const max = (await pool.MAX_TOKENS_PER_USER()).toNumber() // 30

// Attacker fills victim's depositTokensOfAccount with dust of every whitelisted deposit token
for (const dt of depositTokens /* up to `max` entries */) {
  await underlying.approve(dt.address, dust)
  await dt.deposit(dust, attacker.address)          // attacker holds balance
  await dt.transfer(victim, 1)                      // forces addToDepositTokensOfAccount(victim)
}

expect(await pool.getDepositTokensOfAccount(victim)).to.have.length(max)

// Victim's own deposit of a NEW collateral type now reverts
await underlying2.approve(msdNew.address, parseEther('10'))
await expect(msdNew.connect(bob).deposit(parseEther('10'), bob.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

// Victim cannot issue a debt token not already in their list
await expect(msEthDebtToken.connect(bob).issue(amount, bob.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

// Liquidator at cap cannot receive seized collateral of a new type
await expect(pool.connect(liquidator).liquidate(msEth.address, victim, repay, msdNew.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
```

The revert chain in each case is the same forced-insertion path: `_transfer`/`_mint` → `pool.addToDepositTokensOfAccount(recipient)` → `onlyIfAdditionWillNotReachMaxTokens` → `UserReachedMaxTokens`.

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

**File:** contracts/Pool.sol (L204-220)
```text
    function addToDebtTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _debtToken = _msgSender();
        _revertIfSenderIsNotDebtToken(_debtToken);
        if (!debtTokensOfAccount.add(account_, _debtToken)) revert DebtTokenAlreadyExists();
    }

    /**
     * @notice Add a deposit token to the per-account list
     * @dev This function is called from `DepositToken` when user's balance changes from `0`
     * @dev The caller should ensure to not pass `address(0)` as `_account`
     * @param account_ The account address
     */
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
    }
```

**File:** contracts/Pool.sol (L589-592)
```text
        depositToken_.seize(account_, _msgSender, _toLiquidator);

        if (_fee > 0) {
            depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee);
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

**File:** contracts/DepositToken.sol (L485-488)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```

**File:** contracts/DepositToken.sol (L522-525)
```text
        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```
