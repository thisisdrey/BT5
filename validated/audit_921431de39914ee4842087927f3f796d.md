### Title
Dust-deposit griefing fills a victim's `depositTokensOfAccount` list, locking collateral deposits and forcing liquidation - (File: contracts/DepositToken.sol)

### Summary
An unprivileged attacker can call `DepositToken.deposit(amount_, onBehalfOf_)` with `onBehalfOf_` set to a victim and a dust `amount_`, minting attacker-paid deposit tokens directly into the victim's account. Each first-touch mint appends the token to the victim's per-account deposit-token list in `Pool` (`addToDepositTokensOfAccount`), which is capped by `MAX_TOKENS_PER_USER` and reverts with `UserReachedMaxTokens()` once full. By spraying 1-wei deposits of every registered deposit token at the victim, the attacker permanently fills the list. If the victim's balances are locked by outstanding debt (`_revertIfLocked` / `unlockedBalanceOf`), the victim cannot transfer or withdraw the dust to free slots, cannot deposit additional collateral, and becomes liquidatable. This is the Metronome analog of CVE-2018-7286's bug class: an authenticated remote party opens many lightweight state-creating operations (INVITEs / dust deposits) on a shared connection (the victim's account-list) and abandons them, leaving the victim in a stuck state with no cleanup path.

### Finding Description
- `DepositToken.deposit` takes an arbitrary `onBehalfOf_` beneficiary and calls `_mint(onBehalfOf_, _deposited)` with no opt-in from the beneficiary [1](#0-0) .
- `_mint` calls `pool.addToDepositTokensOfAccount(account_)` whenever the recipient's prior balance was zero [2](#0-1) .
- The same append happens on plain `transfer`/`seize` via `_transfer` [3](#0-2) .
- The only removal path is `removeFromDepositTokensOfAccount`, triggered when a balance returns to zero [4](#0-3) .
- Both `transfer`/`transferFrom` and `withdraw`/`withdrawFrom` are gated by `_revertIfLocked`, which compares against `unlockedBalanceOf` [5](#0-4) . `unlockedBalanceOf` returns near-zero for accounts whose debt consumes their collateral [6](#0-5) .
- `Pool` enforces the cap, reverting with `UserReachedMaxTokens()` [7](#0-6) .

Attack sequence:
1. Victim opens a leveraged position (collateral + debt), so `unlockedBalanceOf(victim)` ≈ 0.
2. For every deposit token `d_i` registered in the pool, the attacker calls `d_i.deposit(1, victim)` (or dust-transfers 1 wei). Cost is `N` wei of underlying plus gas, where `N` is the number of registered deposit tokens.
3. Once `depositTokensOfAccount(victim)` reaches `MAX_TOKENS_PER_USER`, any subsequent `_mint`/`_transfer` of a not-yet-held token to the victim reverts.
4. The victim cannot self-heal: clearing a slot requires driving a dust balance to zero via `transfer` or `withdraw`, both of which revert under `_revertIfLocked` while the position is at high utilization.
5. If the victim's position drifts toward the liquidation threshold, the usual defense — depositing more collateral, including a different collateral type — reverts, and the position is liquidated.

### Impact Explanation
Temporary freezing of funds and forced loss of collateral. The victim loses the ability to (a) deposit any new collateral type, (b) receive seized/deposit tokens via any path that mints or transfers to them, and (c) cannot evict the attacker's dust entries while debt locks their balances. The practical result is denial of the victim's only de-risking action (adding collateral) until either the debt is repaid by other means or the position is liquidated, i.e., liveness break on the position-management invariant.

### Likelihood Explanation
Fully unprivileged: the attacker needs only an EOA, dust amounts of each underlying (or even none, by routing through `deposit` with fee-on-transfer-irrelevant 1-wei amounts), and one transaction per token — or a single multicall via `Operator.execute`. No privileged role, oracle manipulation, or malicious LayerZero component is required. Preconditions: the victim holds a pool position with high utilization so that `unlockedBalanceOf` is below the dust amount. The attack is cheap (bounded by number of registered deposit tokens) but constrained to pools/tokens where `MAX_TOKENS_PER_USER` is reachable.

### Recommendation
- Make the account-list membership opt-in: only push to `depositTokensOfAccount` when `onBehalfOf_ == _msgSender()` or via an explicit `acceptCollateral(token)` flow, or have `deposit` push the token onto the *caller's* list and let transfers of dust not register new entries.
- Alternatively, allow anyone to call `removeFromDepositTokensOfAccount`-equivalent cleanup for zero-economic-value balances, or exempt dust below a minimum-deposit threshold from list registration and lock accounting.
- Enforce a meaningful `minDeposit` per deposit token so griefing cost scales above the gas it imposes.

### Proof of Concept
Hardhat fork sketch against a live `Pool` (e.g., mainnet deployment):

```ts
// victim has deposit + borrow so unlockedBalanceOf(victim) == 0 on every token
const depositTokens: DepositToken[] = await getRegisteredDepositTokens(pool);

for (const dt of depositTokens) {
  const underlying = await ethers.getContractAt("IERC20", await dt.underlying());
  await underlying.connect(attacker).approve(dt.address, 1);
  // fills one slot of victim's list per token; attacker pays 1 wei each
  await dt.connect(attacker).deposit(1, victim.address);
}

// victim's list is now at MAX_TOKENS_PER_USER
expect(await pool.depositTokensOfAccount(victim.address)).to.have.length(MAX);

// victim cannot clear dust: balances locked by debt
await expect(
  depositTokens[0].connect(victim).transfer(attacker.address, 1)
).to.be.revertedWithCustomError(depositTokens[0], "NotEnoughFreeBalance");

// victim cannot deposit a new collateral type
await expect(
  newDepositToken.connect(victim).deposit(parseEther("1"), victim.address)
).to.be.revertedWithCustomError(pool, "UserReachedMaxTokens");

// position drifts unhealthy -> liquidate succeeds, victim loses collateral
await pool.connect(liquidator).liquidate(msUsd.address, victim.address, depositAmount, depositTokens[0].address);
```

Notes on uncertainty: the exact value of `MAX_TOKENS_PER_USER` and the body of `Pool.addToDepositTokensOfAccount`/`removeFromDepositTokensOfAccount` were not fully read in the available context (grep confirmed they exist in `contracts/Pool.sol` and `contracts/storage/PoolStorage.sol`); the PoC assumes the cap is enforced inside `addToDepositTokensOfAccount` via `UserReachedMaxTokens()`. If the deployed configuration's cap exceeds the number of registered deposit tokens plus realistic transfer diversity, likelihood decreases but the griefing vector remains per-token-additive across future token additions.

### Citations

**File:** contracts/DepositToken.sol (L180-182)
```text
    function _revertIfLocked(address account_, uint256 amount_) private view {
        if (unlockedBalanceOf(account_) < amount_) revert NotEnoughFreeBalance();
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

**File:** contracts/DepositToken.sol (L459-462)
```text
        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (_amount > 0 && _balanceAfter == 0) {
            pool.removeFromDepositTokensOfAccount(_account);
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

**File:** contracts/Pool.sol (L32-32)
```text
error UserReachedMaxTokens();
```
