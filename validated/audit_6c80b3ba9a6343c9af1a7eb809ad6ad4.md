### Title
Unprivileged attacker can permanently DoS liquidations by filling the fee collector's token list to `MAX_TOKENS_PER_USER` - (File: contracts/Pool.sol)

### Summary
The MySQL CVE is a remotely-triggerable denial of service. The reachable Metronome analog is a liveness break in `Pool.liquidate`: every liquidation with a nonzero protocol fee ends with `depositToken_.seize(account_, feeCollector, _fee)`, which routes through `DepositToken._transfer` and calls `Pool.addToDepositTokensOfAccount(feeCollector)` whenever the fee collector's balance of that deposit token was previously zero. That call reverts with `UserReachedMaxTokens` once the fee collector's combined debt+deposit token list reaches `MAX_TOKENS_PER_USER = 30`. An unprivileged attacker can dust-transfer deposit tokens to the fee collector to occupy all 30 slots, after which any liquidation whose fee is denominated in a deposit token the fee collector does not yet hold always reverts — permanently freezing that collateral type's liquidation path and letting bad debt accrue.

### Finding Description
- `Pool.liquidate` seizes collateral twice: `_toLiquidator` to the caller and `_fee` to `poolRegistry.feeCollector()` [1](#0-0) .
- `DepositToken.seize` calls `_transfer`, which calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance was zero [2](#0-1) .
- `addToDepositTokensOfAccount` is gated by `onlyIfAdditionWillNotReachMaxTokens`, which reverts when `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` [3](#0-2) [4](#0-3) .
- Deposit tokens are freely transferable by any unlocked holder via `transfer`/`transferFrom` [5](#0-4) .
- The check is applied to the *recipient* (fee collector or liquidator), not the victim, so the attacker needs no privilege and the victim cannot prevent it.

Attack sequence:
1. Attacker deposits a tiny amount of each of 30 distinct deposit tokens (or acquires them via `transfer`) and dust-transfers each to `feeCollector`. Each transfer adds an entry to `depositTokensOfAccount[feeCollector]`, filling all 30 slots.
2. Later, any `liquidate` call where `_fee > 0` and the seized deposit token is one the fee collector does not already hold reverts inside `seize` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens`.
3. The revert happens at the end of `liquidate`, after `syntheticToken_.burn` and `debtToken.burn` — but the whole tx reverts, so nothing is repaid and no collateral is seized. The liquidation is bricked regardless of the liquidator.

The same griefing applies to `_toLiquidator` only if the liquidator is already at 30 tokens (self-inflicted), and to any user receiving a dust-filled list: the victim can still withdraw existing collateral but is blocked from depositing new collateral types and from minting new synth types (`DebtToken.issue` → `addToDebtTokensOfAccount` shares the same combined 30-slot counter).

### Impact Explanation
Positions backed by a deposit token the fee collector doesn't yet hold become unliquidatable whenever the protocol liquidation fee is nonzero. Since the attacker controls which slots are occupied and can refresh the set, underwater positions in the remaining collateral type(s) cannot be liquidated, converting a price move into unrecoverable bad debt / protocol insolvency. This matches the CVE bug class: an unprivileged network attacker causes a repeatable, persistent denial of a core protocol function (liveness invariant: "unhealthy positions can always be liquidated").

### Likelihood Explanation
Requires a pool with more than 30 registered deposit token types for the fee-collector variant to permanently brick a specific collateral's liquidation; with ≤30 types the attack only bricks liquidations until the fee collector holds that token (transient). The per-user variant (dust-filling a victim to 30 tokens to block new deposits/mints) works unconditionally. No privileged role, oracle manipulation, or governance action is needed — only dust transfers and public `deposit`/`transfer` entry points. Gas/unbounded-loop DoS is not the mechanism; the revert is a hard state check.

### Recommendation
- In `DepositToken._transfer`, do not revert when the recipient's token list is full for protocol-internal recipients (fee collector, liquidator) — or let `seize` skip the per-account list registration entirely.
- Alternatively, only enforce `onlyIfAdditionWillNotReachMaxTokens` for user-initiated additions (`deposit`, `issue`) and exempt additions driven by `Pool.liquidate`/`seize`.
- Consider tracking fee-collector receipts separately (e.g., pull fees to `Treasury` instead of minting msdTOKEN balances) so an attacker cannot influence liquidation liveness via the collector's token list.

### Proof of Concept
Hardhat sketch (fork or local deployment with ≥31 deposit tokens and `protocolLiquidationFee > 0`):

```ts
// setup: pool has deposit tokens dt0..dt30, protocolFee > 0
const feeCollector = await poolRegistry.feeCollector()

// 1) attacker dust-fills feeCollector's token list to MAX_TOKENS_PER_USER
for (let i = 0; i < 30; i++) {
  const dt = depositTokens[i]
  await dt.underlying().approve(dt.address, 1)
  await dt.deposit(1, attacker.address)          // mint dust msdTOKEN
  await dt.transfer(feeCollector.address, 1)     // adds entry to feeCollector's list
}
expect(await pool.getDepositTokensOfAccount(feeCollector)).to.have.length(30)

// 2) victim's position backed by dt30 (feeCollector holds 0 of dt30) goes unhealthy
await masterOracle.updatePrice(underlying30.address, crashPrice)

// 3) every liquidation attempt reverts
await expect(
  pool.connect(liquidator).liquidate(
    msEth.address, victim.address, amountToRepay, dt30.address)
).revertedWithCustomError(pool, 'UserReachedMaxTokens')
```

The revert propagates from `DepositToken._transfer` → `Pool.addToDepositTokensOfAccount` during `depositToken_.seize(account_, feeCollector, _fee)`, so the liquidation can never execute while the fee is charged in a token the collector doesn't already hold.

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

**File:** contracts/Pool.sol (L587-593)
```text
        syntheticToken_.burn(_msgSender, amountToRepay_);
        _debtToken.burn(account_, amountToRepay_);
        depositToken_.seize(account_, _msgSender, _toLiquidator);

        if (_fee > 0) {
            depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee);
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
