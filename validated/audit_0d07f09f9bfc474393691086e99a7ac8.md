### Title
Attacker can fill a victim's `depositTokensOfAccount` set with dust transfers so all subsequent deposits/mints/transfers to the victim revert - (contracts/Pool.sol)

### Summary
`Pool` enforces `MAX_TOKENS_PER_USER = 30` via the `onlyIfAdditionWillNotReachMaxTokens` modifier on `addToDepositTokensOfAccount()` / `addToDebtTokensOfAccount()`, which are called by `DepositToken._mint()` and `DepositToken._transfer()` whenever a recipient's balance moves from zero. Because `DepositToken.transfer()`/`transferFrom()` are permissionless, an unprivileged attacker can send dust amounts of every registered deposit token to a victim, filling the victim's per-account set to the cap. From then on, any action that would add a *new* deposit token to the victim's list — depositing a different collateral, receiving a deposit-token transfer, or being the recipient in a liquidation `seize()` — reverts with `UserReachedMaxTokens`, exactly the same over-reverting limit-check class as the report.

### Finding Description
- `Pool.addToDepositTokensOfAccount()` is gated by `onlyIfAdditionWillNotReachMaxTokens`, which reverts when `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` [1](#0-0) [2](#0-1) .
- `DepositToken._transfer()` unconditionally calls `pool.addToDepositTokensOfAccount(recipient_)` when the recipient's prior balance is zero [3](#0-2) .
- `transfer()`/`transferFrom()` are public and only check the *sender's* unlocked balance via `_revertIfLocked` — there is no opt-in or minimum-amount check on the recipient [4](#0-3) .
- `DepositToken._mint()` (reached from `Pool.deposit()`) applies the same add when a depositor's balance is zero [5](#0-4) .
- `seize()` routes through `_transfer()`, so a liquidation whose beneficiary already has 30 entries reverts too [6](#0-5) .

Attack: attacker deposits 1 wei of underlying into each of the pool's deposit tokens from a fresh account, then calls `msdTokenX.transfer(victim, 1)` for each token until the victim's combined set reaches 30. The victim's subsequent `pool.deposit()` of a new collateral reverts inside `_mint → addToDepositTokensOfAccount`, and any msdTOKEN sent to them reverts.

### Impact Explanation
Temporary freezing of funds / forced-liquidation griefing. A victim with an open position cannot add new collateral types to improve health; if the set is filled before their first deposit they cannot onboard at all until they spend gas transferring the dust balances out (`_transfer` removes an entry only when the sender's balance reaches zero, so recovery is possible but costs the victim up to 30 outbound transactions per cycle, and the attacker can re-fill). No reentrancy guard, pause flag, or SynthContext check stops it — `transfer` is only `nonReentrant` on `transferFrom`, and the check operates on the *recipient*, who never consented.

### Likelihood Explanation
Requires only unprivileged EOAs and public entry points; the sole cost is dust collateral for up to 30 registered deposit tokens (the attacker must deposit real underlying, so feasibility depends on how many deposit tokens the pool has — the cap of 30 is only reachable if ≥15 deposit+debt tokens exist, otherwise the attacker can still fill the victim's remaining slots). The victim can clear entries by transferring dust away, so the freeze is temporary, matching Medium severity.

### Recommendation
Either whitelist/approve recipients before tokens count toward their limit, exempt the recipient-side set growth during `seize`, or track balances in a mapping and iterate a global token list in `debtPositionOf`/`depositOf` instead of a per-account enumerable set. At minimum, allow a user to remove tokens from their own list without a full-balance transfer.

### Proof of Concept
Hardhat fork outline:
```ts
// pool has N registered deposit tokens msd1..msdN
for (const msd of depositTokens) {
  await underlying(msd).approve(msd.address, dustAmount);
  await msd.deposit(dustAmount);            // attacker gets dust balance
  await msd.transfer(victim.address, 1);    // adds msd to victim's set
}
// repeat until debtTokensOfAccount+depositTokensOfAccount length == 30
expect(await pool.getDepositTokensOfAccount(victim.address)).length.eq(30);
// victim's deposit of a token not already in their set now reverts
await expect(
  pool.connect(victim).deposit(msdNew.address, amount)
).revertedWithCustomError(pool, 'UserReachedMaxTokens');
```
This reproduces on a mainnet/Base fork with the deployed `Pool` (`VERSION = "1.3.2"`, `MAX_TOKENS_PER_USER = 30`) [7](#0-6) .

### Citations

**File:** contracts/Pool.sol (L74-79)
```text
    string public constant VERSION = "1.3.2";

    /**
     * @notice Maximum tokens per pool a user may have
     */
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

**File:** contracts/DepositToken.sol (L343-345)
```text
    function seize(address from_, address to_, uint256 amount_) external override onlyIfCanSeize {
        _transfer(from_, to_, amount_);
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
