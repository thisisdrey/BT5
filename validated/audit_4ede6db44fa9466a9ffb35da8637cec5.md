### Title
Unprivileged dust-deposit/transfer griefing fills a victim's `MAX_TOKENS_PER_USER` account list and blocks them from opening any new collateral or debt position - (File: contracts/Pool.sol)

### Summary
The `Pool` tracks, per account, the set of `DepositToken`s and `DebtToken`s it holds in two `MappedEnumerableSet.AddressSet` lists, capped at `MAX_TOKENS_PER_USER = 30` combined. Any addition that would reach the cap reverts with `UserReachedMaxTokens`. Both `DepositToken._mint`/`_transfer` and `DebtToken._mint` push tokens into these lists whenever the recipient's prior balance is zero, and both `deposit(amount_, onBehalfOf_)` and plain `transfer`/`transferFrom` of deposit tokens are permissionless with an arbitrary beneficiary. An attacker can therefore stuff a victim's list to the cap for dust cost, after which every action that would add a new token to the victim's account reverts.

### Finding Description
- `Pool.onlyIfAdditionWillNotReachMaxTokens` reverts when `debtTokensOfAccount + depositTokensOfAccount >= 30` [1](#0-0) [2](#0-1) .
- `DepositToken.deposit(uint256 amount_, address onBehalfOf_)` is callable by any EOA for any beneficiary; it mints to `onBehalfOf_` and, on first receipt, calls `pool.addToDepositTokensOfAccount(account_)` [3](#0-2) [4](#0-3) .
- `DepositToken._transfer` adds the token to the *recipient's* list whenever the recipient balance was zero, with no opt-out [5](#0-4) .
- The same push-on-first-balance logic exists for debt tokens in `DebtToken._mint` [6](#0-5) .

Attack trace (unprivileged, no privileged role, no oracle manipulation):

1. Attacker picks victim `V`.
2. For each registered deposit token `msdX_i`, attacker calls `DepositToken_i.deposit(1 wei, V)` (or first self-deposits then `transfer(V, 1)`). Each call appends `msdX_i` to `V`'s `depositTokensOfAccount` list.
3. Once `V`'s combined list hits 30 entries, every subsequent operation that would add a new token to `V` reverts with `UserReachedMaxTokens`: `deposit` into a collateral `V` doesn't hold, `DebtToken.issue` for a synth `V` hasn't borrowed, `SmartFarmingManager.leverage`/`flashRepay` flows that mint deposit or debt tokens for `V`, and `Pool.liquidate` flows where `V` would receive a seized token it doesn't already hold.

### Impact Explanation
Availability-only, matching the CVE class (hang/DoS). The victim is denied use of any protocol position involving a token not already in their account list: they cannot open new collateral positions, cannot take new debt types, and cannot be the beneficiary of leverage or seized-collateral transfers for new tokens. On pools with many registered deposit/debt tokens (the cap is shared across both lists), filling the list is cheap — the attacker only needs 1 unit of underlying per token, subject to deposit fees. The DoS persists until the victim spends gas to clear entries: transferring a dust token out to zero balance triggers `removeFromDepositTokensOfAccount`, so the freeze is temporary rather than permanent, and the attacker can re-fill cleared slots to keep the victim blocked. The invariant broken is liveness of position management for a targeted user via forced state on their account they never consented to.

### Likelihood Explanation
Fully unprivileged and reproducible: `deposit` has no beneficiary allowlist and `_transfer` forcibly registers tokens on recipients. Requirements that limit severity: the pool must have enough registered tokens (or the attacker combines dust transfers across deposit tokens plus any debt tokens already on the victim) to reach 30; each griefing deposit costs underlying plus deposit fee; and the victim can unilaterally clear slots by sending dust balances away, so impact is a temporary denial of new-position liveness rather than loss or permanent freezing of existing funds. This mirrors the medium-severity, availability-only rating of the referenced CVE.

### Recommendation
- Do not add a token to an account's list for unsolicited receipts: only register in `deposit`/`issue` when `onBehalfOf_`/sender initiates the action, or make list membership lazy/derived rather than a hard cap.
- Alternatively, allow receipts past the cap but only enforce `onlyIfAdditionWillNotReachMaxTokens` on state-changing actions the account initiates (deposit/issue), not on `_transfer`/`seize` receipts.
- A minimal fix is to skip `addToDepositTokensOfAccount` in `_transfer` for pure transfers and only track tokens the account deposited itself; health computation (`debtPositionOf`) iterates these lists, so it must be verified it accounts for transferred-in balances.

### Proof of Concept
Hardhat fork sketch (per repo test style in `test/Pool.test.ts`):

```ts
// pool has registered deposit tokens msdA..msdN
for (const dt of depositTokens) {
  // attacker deposits 1 wei underlying credited to victim
  await underlying.approve(dt.address, 1);
  await dt.deposit(1, victim.address); // adds dt to victim's list
}
// once length(depositTokensOfAccount[victim]) + length(debtTokensOfAccount[victim]) == 30:
await expect(newDepositToken.deposit(1, victim.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
// likewise victim.issue(...) for a new synth reverts inside DebtToken._mint
// victim can recover only by transferring each dust balance out to clear entries
```

Note: I verified the list-add paths and the cap modifier, but did not enumerate the deployed per-chain token counts; the attack's cost scales with how many deposit/debt tokens are registered on the target pool.

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

**File:** contracts/DebtToken.sol (L597-600)
```text
        //  Add this token to the debt tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDebtTokensOfAccount(account_);
        }
```
