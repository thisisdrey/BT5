### Title
Attacker can fill a victim's token list to `MAX_TOKENS_PER_USER`, blocking them from depositing new collateral to improve their health - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
The Notional bug is a validity check (`checkValidMaturity`) that is applied even when a user is performing a health-improving action on an already-open position; when the position's maturity becomes idiosyncratic, `enterVault` reverts and the user cannot add collateral to avoid liquidation. The Metronome analog lives in `Pool.addToDepositTokensOfAccount` / `addToDebtTokensOfAccount`: the shared `MAX_TOKENS_PER_USER` cap check is applied on every *new* token added to an account's list, including additions triggered by actions the account itself never initiated (`DepositToken.deposit(amount, onBehalfOf)` with an arbitrary beneficiary, and `DepositToken.transfer` to the victim). An unprivileged attacker can grief-fill a victim's list with dust amounts of the pool's other collateral tokens, after which the victim's own `deposit` of any *new* collateral type reverts with `UserReachedMaxTokens`, leaving them unable to improve their collateral ratio through a new collateral while their position approaches liquidation.

### Finding Description
In `Pool.sol`, both list-insertion hooks enforce a hard cap of 30 tokens per account, shared across deposit and debt tokens:

```solidity
modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
    if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
        revert UserReachedMaxTokens();
    }
    _;
}
``` [1](#0-0) [2](#0-1) 

`DepositToken._mint` and `DepositToken._transfer` call `pool.addToDepositTokensOfAccount(account_)` whenever the recipient's balance moves from zero — including when the recipient is `onBehalfOf_` in `deposit` or `to_` in `transfer`, neither of which is restricted to the caller: [3](#0-2) [4](#0-3) [5](#0-4) 

So an attacker can execute, for each deposit token the victim does not yet hold:

```solidity
depositToken.deposit(1, victim);          // or depositToken.transfer(victim, 1)
```

pushing the victim's combined list length to `MAX_TOKENS_PER_USER`. From then on, any `deposit` into a collateral type the victim does not already hold reverts in `addToDepositTokensOfAccount`, because `_mint` runs `addToDepositTokensOfAccount` for first-time recipients before the deposit completes. The revert happens *after* the underlying was already pulled into `Treasury`, but the whole transaction rolls back — the deposit simply cannot succeed.

The cleanup path is unreliable when it matters most. Removing a dust entry requires burning the victim's dust balance (via `withdraw`), and `withdraw` is gated by `_revertIfLocked` → `unlockedBalanceOf`, which returns 0 whenever `_issuableInUsd == 0` — i.e., precisely when the victim's position is at/below healthy and they most need to top up: [6](#0-5) [7](#0-6) 

Additionally, the same dust entries block `addToDebtTokensOfAccount`, so the victim also cannot `issue` a new synthetic type, and any inbound `transfer`/`transferFrom` of a deposit token they do not yet hold reverts — the attacker can even keep re-filling slots after the victim clears them, since the griefing only costs dust collateral.

### Impact Explanation
A borrower whose collateral factor is deteriorating (e.g., a vault-share collateral losing value) may need to deposit a *different* collateral to restore health. With the token list saturated, every such `deposit` reverts with `UserReachedMaxTokens`. If the victim's existing deposit tokens are exhausted (`issuableInUsd == 0`), they cannot withdraw the dust entries to free slots, so the block persists until the position is liquidated. Invariant broken: liveness of the health-improving `deposit` path, mirroring the Notional issue where users "will be unable to improve their collateral ratio... and thus are extremely susceptible to liquidations." The residual loss is realized through `Pool.liquidate`, which remains fully operational (`whenNotShutdown` only).

### Likelihood Explanation
The attack requires a pool that lists close to `MAX_TOKENS_PER_USER` (30) deposit tokens — the pool itself is capped at 30 deposit tokens (`ReachedMaxDepositTokens` in `addDepositToken`), so on smaller pools the attacker fills proportionally fewer slots. The attacker must spend dust of each underlying (or dust msdTokens via `transfer`) and may need to re-grief if the victim clears entries while still healthy, so the grief is a race. Victims holding positions in several existing collateral types can partially mitigate by depositing an already-held type, and `repay`/`repayAll` remain available to anyone holding the synthetic token. No privileged role, oracle failure, or configuration change is needed — `deposit`'s `onBehalfOf_` parameter and permissionless `transfer` make the list-filling fully attacker-controllable.

### Recommendation
Apply the cap check only when it is the account itself expanding its own risk surface, not when it is passively receiving tokens:

- In `DepositToken._transfer` / `addToDepositTokensOfAccount`, skip adding the token to the recipient's list (or allow silent overflow of the view list) when `recipient_ != _msgSender()`, similar to skipping `checkValidMaturity` for existing positions. Alternatively, only enforce `UserReachedMaxTokens` when `_msgSender() == account_` in `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount`.
- Optionally allow `withdraw` of a dust balance to bypass `unlockedBalanceOf` when it would zero out the token balance (withdrawing the last wei cannot worsen health since it also removes the collateral from the list — note this changes accounting, so it must be weighed against the issuable-limit check).
- Note `debtPositionOf`/`depositOf` iterate `depositTokensOfAccount`, so enlarging the list also raises gas costs of every health check — bounding additions to self-initiated actions solves both issues.

### Proof of Concept
Hardhat test sketch (place under `test/`, assuming the existing fixture exposes `pool`, `msdX` deposit tokens and `msUSDDebt`):

```ts
it('attacker fills victim token list, blocking new collateral deposit', async () => {
  // setup: pool lists >= 29 deposit tokens D0..D28, victim deposits D0 and issues msUSD
  await d0.connect(victim).deposit(parseEther('10'), victim.address);
  await msUSDDebt.connect(victim).issue(parseEther('1'), victim.address);

  // attacker deposits dust of each other collateral on behalf of victim
  for (const dt of [d1, d2, /* ... */ d28]) {
    await underlyingOf(dt).connect(attacker).approve(dt.address, 1);
    await dt.connect(attacker).deposit(1, victim.address);   // +1 list entry each
  }
  expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.length(29);
  // combined with victim's 1 debt token => length == MAX_TOKENS_PER_USER (30)

  // victim tries to deposit a collateral type they don't hold yet to restore health
  await underlyingOf(d29).connect(victim).approve(d29.address, parseEther('100'));
  await expect(d29.connect(victim).deposit(parseEther('100'), victim.address))
    .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

  // if victim is now unhealthy (issuableInUsd == 0), they cannot withdraw dust to free slots:
  await expect(d5.connect(victim).withdraw(1, victim.address))
    .to.be.revertedWithCustomError(d5, 'NotEnoughFreeBalance');
});
```

Reproduction path: `DepositToken.deposit(1, victim)` → `_mint(victim, 1)` → `Pool.addToDepositTokensOfAccount(victim)` → `onlyIfAdditionWillNotReachMaxTokens` passes until the list reaches 30; afterwards every first-time `_mint`/`_transfer` to the victim reverts, blocking the victim's own `deposit` into new collateral and any inbound `transfer`, while `Pool.liquidate` continues to function against the frozen position.

### Citations

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

**File:** contracts/DepositToken.sol (L211-216)
```text
    function deposit(
        uint256 amount_,
        address onBehalfOf_
    ) external override whenNotPaused nonReentrant onlyIfDepositTokenExists returns (uint256 _deposited, uint256 _fee) {
        if (amount_ == 0) revert AmountIsZero();
        if (onBehalfOf_ == address(0)) revert BeneficiaryIsNull();
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

**File:** contracts/DepositToken.sol (L406-412)
```text
    function withdraw(uint256 amount_, address to_) external override returns (uint256 _withdrawn, uint256 _fee) {
        if (to_ == address(0)) revert RecipientIsNull();
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        return _withdraw({account_: _msgSender, amount_: amount_, to_: to_});
    }
```

**File:** contracts/DepositToken.sol (L486-489)
```text
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
    }
```

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```
