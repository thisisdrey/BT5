### Title
Dust deposit/transfer fills victim's per-account token list (`MAX_TOKENS_PER_USER`), DoS-ing new deposits, borrows and liquidations - (File: contracts/Pool.sol)

### Summary
`Pool` tracks, per account, the deposit tokens and debt tokens with non-zero balance in `depositTokensOfAccount` / `debtTokensOfAccount`. Any addition that would bring the combined count to `MAX_TOKENS_PER_USER = 30` reverts with `UserReachedMaxTokens` [1](#0-0) . Entries are added not only by the account's own actions but whenever a `DepositToken` balance goes from `0` to `>0`, including via `deposit(amount_, onBehalfOf_)` and plain `transfer`/`transferFrom`/`seize` [2](#0-1) . An unprivileged attacker can therefore grief any victim by pushing 1-wei balances of every pool deposit token onto them, bricking any victim action that would add a *new* token to their list.

### Finding Description
`DepositToken._mint` and `DepositToken._transfer` call `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's prior balance was zero [3](#0-2) . `Pool.addToDepositTokensOfAccount` is permissionless in effect: it only requires the caller to be a registered deposit token, which is satisfied automatically because the call originates inside the token contract itself [4](#0-3) . The `onlyIfAdditionWillNotReachMaxTokens` modifier enforces `debtTokensOfAccount.length + depositTokensOfAccount.length < 30` [5](#0-4) .

Attack path:

1. Attacker deposits a tiny amount of each underlying via `DepositToken.deposit(dust, victim)` (no victim consent needed; `onBehalfOf_` is arbitrary) or simply buys/withdraws each msdTOKEN and calls `transfer(victim, 1)`.
2. Each such deposit/transfer reverts the *whole* transaction if the victim is already at 30 entries — so the attacker must front-run, but once 30 distinct tokens are on the victim's list, every subsequent victim action that adds a new token reverts:
   - `deposit` of any collateral type the victim does not already hold (`addToDepositTokensOfAccount` reverts inside `_mint`).
   - `DebtToken.issue`/mint of any synthetic the victim does not already owe (`addToDebtTokensOfAccount` reverts).
   - Receiving seized collateral as a liquidator (`seize` → `_transfer` → `addToDepositTokensOfAccount` reverts), blocking that liquidator from liquidating if their own list was filled.
3. Cleanup is not guaranteed: the victim can only remove a dust entry by transferring it out, and `transfer` enforces `_revertIfLocked`, i.e. `unlockedBalanceOf(account) >= amount` [6](#0-5) . `unlockedBalanceOf` returns `0` whenever the victim's position has `_issuableInUsd == 0` (fully utilized collateral) [7](#0-6) , so a levered victim cannot evict the dust at all and stays bricked until they deleverage — which itself may require a swap/deposit path that is now blocked.

Analogous to CVE-2020-2901 (optimizer-induced repeatable crash/DoS), this is a liveness/availability break: a deterministic revert forced onto a victim by an unprivileged attacker through public entry points.

### Impact Explanation
Temporary freezing of funds / forced liveness failure. The victim cannot open or expand positions in new collateral or debt types, cannot receive new deposit tokens (anyone transferring to them reverts), and liquidation bots whose lists are dust-filled cannot seize. For a fully-leveraged victim, dust entries are locked (`unlockedBalanceOf == 0`), extending the DoS indefinitely until market prices move or the victim injects fresh capital of an already-held type. Direct theft is not possible, but position management (the core protocol function) is bricked — matching the "temporary freezing of funds" impact class.

### Likelihood Explanation
Requires a pool where the number of registered deposit + debt tokens can reach 30 (across the sum), or a victim already holding several tokens so fewer dust pushes suffice. Cost is gas plus negligible dust value; all calls use public functions (`deposit`, `transfer`) with attacker-chosen `onBehalfOf_`/recipient. No privileged role, oracle manipulation, or malicious infrastructure is needed. Mitigation in the victim's favor: they can clear unlocked dust entries by transferring them away, so impact is bounded unless the position is fully leveraged.

### Recommendation
- Do not gate `addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` on the recipient's total when the addition is triggered by an incoming transfer, or enforce `MAX_TOKENS_PER_USER` only for *voluntary* position-opening calls (`deposit`, `issue`) initiated by the account itself.
- Alternatively, exempt forced receipts (`seize`, inbound `transfer`) and let the cap apply only at `deposit`/`issue` time.
- Consider letting any account always remove entries regardless of lock status (e.g., a `transfer`/`withdraw` of dust that only reduces balances), so griefed users can self-heal.

### Proof of Concept
Hardhat (fork) sketch:

```ts
// setup: pool with >= 2 deposit tokens (msdA, msdB, ... up to MAX_TOKENS_PER_USER)
// victim holds a normal position; attacker fills victim's list with dust.

const depositTokens: DepositToken[] = [msdA, msdB, /* ... all registered deposit tokens */];

for (const dt of depositTokens) {
  // attacker deposits 1 wei-equivalent of underlying on behalf of victim
  const underlying = await ethers.getContractAt('IERC20', await dt.underlying());
  await underlying.connect(attacker).approve(dt.address, 10);
  await dt.connect(attacker).deposit(10, victim.address); // adds dt to victim's list
}

// fill remaining slots with debt tokens is not attacker-controllable,
// so ensure sum(deposit + debt tokens of victim) == MAX_TOKENS_PER_USER (30).
expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.length(30);

// 1) Victim cannot deposit a collateral type they don't already hold
await expect(msdNew.connect(victim).deposit(1e6, victim.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 2) Victim cannot mint a new synthetic (addToDebtTokensOfAccount reverts)
await expect(pool.connect(victim).mint(newSynthetic, amount, victim.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 3) Inbound transfer to victim reverts
await expect(msdNew.connect(attacker).transfer(victim.address, 1))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 4) If victim is fully levered, dust cannot be evicted
//    unlockedBalanceOf(victim) == 0 -> victim.transfer(...) reverts NotEnoughFreeBalance
```

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

**File:** contracts/DepositToken.sol (L348-353)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
```

**File:** contracts/DepositToken.sol (L383-398)
```text
    function unlockedBalanceOf(address account_) public view override returns (uint256 _unlockedBalance) {
        IPool _pool = pool;

        (, , uint256 _debtInUsd, , uint256 _issuableInUsd) = _pool.debtPositionOf(account_);

        if (_debtInUsd == 0) {
            return balanceOf[account_];
        }

        if (_issuableInUsd > 0) {
            _unlockedBalance = Math.min(
                balanceOf[account_],
                _pool.masterOracle().quoteUsdToToken(address(underlying), _issuableInUsd.wadDiv(collateralFactor))
            );
        }
    }
```

**File:** contracts/DepositToken.sol (L486-488)
```text
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L517-525)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```
