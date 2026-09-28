### Title
Unprivileged dust transfers permanently fill a victim's `depositTokensOfAccount` to `MAX_TOKENS_PER_USER`, blocking new collateral/debt positions and exits - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
Analogous to the TLS KeyUpdate bug — where a peer can push repeated unsolicited post-handshake messages that permanently retain connection state and starve the connection — an unprivileged attacker can push unsolicited `DepositToken` transfers to a victim, permanently occupying slots in the victim's per-account token sets. `Pool.onlyIfAdditionWillNotReachMaxTokens` reverts once `debtTokensOfAccount + depositTokensOfAccount` reaches `MAX_TOKENS_PER_USER` (30) [1](#0-0) , and `DepositToken._transfer` unconditionally calls `pool.addToDepositTokensOfAccount(recipient_)` for any first-time recipient [2](#0-1) . There is no opt-in: any holder of an msdToken can force entries into any address's set.

### Finding Description
1. For every registered `DepositToken` in the pool, the attacker deposits a dust amount of the underlying via `DepositToken.deposit(1 wei, attacker)` [3](#0-2) , then calls `DepositToken.transfer(victim, 1)` [4](#0-3) .
2. `_transfer` sees `_recipientBalanceBefore == 0` and calls `pool.addToDepositTokensOfAccount(victim)`, inserting that token into `depositTokensOfAccount[victim]` [2](#0-1) .
3. Repeating across all deposit tokens (and, if the attacker issues dust synthetic debt via `DebtToken.issue` followed by `SyntheticToken` mechanics that add debt tokens — note `issue` mints to the caller, so the attacker would need `seize`/transfer-capable paths; the deposit-token side alone suffices) the victim's combined set reaches 30 entries.
4. From then on, every path that would add a *new* token to the victim's account reverts with `UserReachedMaxTokens`: `deposit(amount, victim)` for a not-yet-held collateral, receiving msdToken transfers, `seize` to a victim in liquidation, and `DebtToken.issue` for a new synthetic (which calls `addToDebtTokensOfAccount`).

### Impact Explanation
- The victim cannot deposit a new collateral type, cannot receive msdTokens, and cannot open debt positions in a synthetic they do not already hold — persistent protocol-level DoS of the position-management surface, mirroring the "persistent connection retention" of the TLS bug.
- Cleanup is attacker-favorable asymmetric: `removeFromDepositTokensOfAccount` only fires when the sender's balance reaches zero [5](#0-4) , so the victim must transfer each dust token out, and each outgoing `transfer` is gated by `_revertIfLocked`/`unlockedBalanceOf` [6](#0-5) . If the victim has debt with `_issuableInUsd == 0`, all balances are locked and they cannot evict a single entry — the freeze is effectively unrecoverable while underwater, and they cannot deposit fresh collateral of a new type to restore health. This is a temporary freezing of funds and liquidation-griefing enabler.

### Likelihood Explanation
- Fully unprivileged: only requires dust amounts of each listed underlying and gas. `transfer` has no whitelist or consent check.
- `deposit(amount_, onBehalfOf_)` lets the attacker mint dust msdTokens *directly to the victim*, so even tokens the attacker doesn't hold can be pushed into the victim's set in one call each.
- Bounded at 30 slots and requires the pool to have many listed deposit tokens to fully saturate; partial filling still degrades the victim's available slots. Impact is availability-only (temporary freezing), not theft.

### Recommendation
- Make set membership opt-in or recipient-controlled: e.g., only add on `deposit`/`issue` (where the beneficiary chose the action), and for `transfer`/`seize` either require recipient consent or do not add unsolicited dust (e.g., a minimum-amount threshold or an `acceptToken` toggle).
- Alternatively, let `unlockedBalanceOf`-locked dust still be evictable, or decouple "has balance" tracking from the capped set so a victim can always withdraw collateral even when the cap is reached.

### Proof of Concept
Hardhat/Foundry fork sketch:

```solidity
// Pool has N registered DepositTokens dT_1..dT_N
for (uint i; i < N; ++i) {
    IERC20 underlying = dT[i].underlying();
    deal(address(underlying), attacker, 1);
    underlying.approve(address(dT[i]), 1);
    // mints dust msdToken straight into victim's set
    dT[i].deposit(1, victim);
}
// If pool deposit-token count < 30, also:
// attacker deposits own collateral, issues dust of each synthetic,
// and (for any transferable debt-token path) or simply relies on
// deposits reaching the combined cap.
assertEq(
    pool.getDepositTokensOfAccount(victim).length +
    pool.getDebtTokensOfAccount(victim).length,
    pool.MAX_TOKENS_PER_USER()
);
// New collateral deposit to victim now reverts
vm.expectRevert(UserReachedMaxTokens.selector);
dT_new.deposit(amount, victim);
// New synthetic issuance to victim reverts
vm.expectRevert(UserReachedMaxTokens.selector);
debtTokenNew.issue(amount, victim);
// If victim has existing debt with issuableInUsd == 0,
// victim.transfer(dT_i, dust) reverts NotEnoughFreeBalance ->
// entries cannot be evicted; freeze persists while unhealthy.
```

Note: `addToDebtTokensOfAccount` reaches the cap only via tokens the account actually mints/receives debt in, so saturating all 30 slots relies primarily on the deposit-token side; this depends on the deployed pool listing enough deposit tokens, which should be confirmed against `deployments/` configuration on the target chain.

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

**File:** contracts/DepositToken.sol (L459-462)
```text
        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (_amount > 0 && _balanceAfter == 0) {
            pool.removeFromDepositTokensOfAccount(_account);
        }
```

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```
