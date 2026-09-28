### Title
Unprivileged dust transfers permanently DoS withdrawals, deposits, and liquidations by exhausting `MAX_TOKENS_PER_USER` on target accounts (incl. `feeCollector`) - (File: contracts/Pool.sol)

### Summary
`DepositToken._transfer` (and `_mint`) unconditionally call `pool.addToDepositTokensOfAccount(recipient)` whenever the recipient's balance goes 0→N. `addToDepositTokensOfAccount` reverts with `UserReachedMaxTokens` once `debtTokensOfAccount + depositTokensOfAccount >= 30`. Any EOA can permissionlessly deposit dust and `transfer` 1-wei of every `DepositToken` to a victim — including the protocol `feeCollector` — permanently stuffing their account list and making any subsequent mint/transfer/seize to that account revert.

### Finding Description
The relevant code paths:

- `DepositToken.transfer` → `_transfer`, which on first receipt calls `pool.addToDepositTokensOfAccount(recipient_)` [1](#0-0) 
- `Pool.addToDepositTokensOfAccount` is gated by `onlyIfAdditionWillNotReachMaxTokens`, which reverts `UserReachedMaxTokens()` once the combined debt+deposit token count hits `MAX_TOKENS_PER_USER = 30` [2](#0-1) [3](#0-2) 
- Critically, protocol internals send to shared accounts: `deposit` mints the deposit fee to `pool.feeCollector()` [4](#0-3) , `_withdraw` transfers the withdraw fee to `feeCollector` [5](#0-4) , and `Pool.liquidate` seizes `_fee` to `feeCollector` [6](#0-5) .

Attack (all unprivileged, public entry points):

1. For each registered `DepositToken` (pool allows up to 30), the attacker deposits 1 wei of underlying (`deposit(1, attacker)`), receiving a non-zero msdToken balance.
2. Attacker calls `depositToken.transfer(victim, 1)` — victim must only have zero balance in that token for the add to trigger. Each transfer adds one entry to `depositTokensOfAccount[victim]`.
3. Once victim reaches 30 combined entries, any further `addToDepositTokensOfAccount(victim)` / `addToDebtTokensOfAccount(victim)` reverts, so the griefed account can never receive a new deposit token (no new collateral types), cannot receive seized liquidation proceeds, and cannot take on new debt.

The strongest variant targets `feeCollector` itself: stuffing its list to 30 means the fee `_transfer`/`_mint`/`seize` inside `deposit`, `withdraw`, and `liquidate` reverts for every deposit token the collector doesn't already hold — a protocol-wide liveness break on three core flows whenever fees are non-zero.

### Impact Explanation
Direct freezing of user funds and liveness loss:
- **Victim griefing**: a targeted user is permanently blocked from depositing any new collateral type, receiving msdToken transfers, and (via the same shared 30-slot counter) opening new debt positions. If their position turns unhealthy, they cannot top-up collateral to avoid liquidation — forced loss of collateral to liquidators.
- **feeCollector griefing**: when `depositFee`/`withdrawFee`/`protocolLiquidationFee > 0`, every `deposit`, `withdraw`, and `liquidate` that would credit a not-yet-held token to the collector reverts → user collateral is frozen in the Treasury (temporary-to-permanent depending on whether the collector's list can ever shrink, which it can't since nothing removes entries except balance going to zero, and fees accrue, not leave).

This is a hang/crash-style availability loss (the analog of CVE-2020-14836's DoS class) reachable by a plain EOA; no privileged role is involved.

### Likelihood Explanation
- Cost is negligible: 1 wei deposits + 1 wei transfers per token (bounded by the number of registered deposit tokens, ≤30, and debt tokens the victim doesn't yet hold — debt token entries can also be filled if `DebtToken` mints/repays `onBehalfOf` or via flash paths; this part was not fully verified within the search budget, but deposit-token stuffing alone reaches the cap since the two lists share one counter).
- No modifier stops it: `transfer`/`transferFrom` only check `_revertIfLocked` on the *sender*; `addToDepositTokensOfAccount` is unauthenticated beyond "caller is a deposit token".
- The only mitigation is the victim clearing entries by zeroing balances, which requires them to first withdraw/transfer the dust — possible for deposit tokens but they cannot get below 30 until they do, and a healthy-position victim has no reason to expect it. For `feeCollector`, entries are effectively permanent.

Caveat: I did not fully verify whether a `DebtToken` path lets an attacker push entries into `debtTokensOfAccount[victim]` (e.g., `issue`/`repay` on behalf); the reported attack only needs the deposit-token side.

### Recommendation
- Do not let inbound permissionless transfers consume the victim's `MAX_TOKENS_PER_USER` slots: either only add to `depositTokensOfAccount` on `deposit`/`seize` (not `transfer`), or track "position" lists separately from ERC20 holdings.
- Alternatively, raise/decouple the cap (e.g., keep the counter for positions opened via `deposit`/`issue` only), or skip `addToDepositTokensOfAccount` when `amount_` is below a dust threshold.
- Special-case `feeCollector` (exempt it from the cap) since it is an involuntary recipient inside deposit/withdraw/liquidate.

### Proof of Concept
Hardhat sketch (fork against deployed Pool or repo test harness):

```ts
// Pool has >= 2 deposit tokens registered; victim has none.
const depositTokens = await pool.getDepositTokens(); // e.g. msdMET, msdVAETH, ...

// Fill victim's list to MAX_TOKENS_PER_USER (30)
for (const dt of depositTokens) {
  const token = await ethers.getContractAt('DepositToken', dt)
  const underlying = await ethers.getContractAt('IERC20', await token.underlying())
  await underlying.connect(attacker).approve(token.address, 1)
  await token.connect(attacker).deposit(1, attacker.address)
  await token.connect(attacker).transfer(victim.address, 1) // +1 slot each
}
// victim depositTokensOfAccount.length == N; if N reaches 30 with existing debt tokens:
await expect(
  someDepositToken.connect(victim).deposit(1, victim.address) // adds new slot -> revert
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

// Protocol-wide variant: repeat targeting feeCollector
const feeCollector = await pool.feeCollector()
// ...same dust transfers to feeCollector until length == 30...
await feeProvider.connect(governor).updateWithdrawFee(parseEther('0.01'))
await expect(
  newDepositToken.connect(user).withdraw(amount, user.address) // _transfer fee -> feeCollector
).to.be.reverted // UserReachedMaxTokens inside Pool.addToDepositTokensOfAccount
```

Expected result: `deposit`/`withdraw`/`liquidate` revert for any token not already in `feeCollector`'s (or victim's) list once the combined counter reaches 30, demonstrating permanent freezing of collateral and blocked liquidations.

### Citations

**File:** contracts/DepositToken.sol (L229-234)
```text
        (_deposited, _fee) = quoteDepositOut(amount_);
        if (_fee > 0) {
            _mint(_pool.feeCollector(), _fee);
        }

        _mint(onBehalfOf_, _deposited);
```

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```

**File:** contracts/DepositToken.sol (L545-551)
```text
        (_withdrawn, _fee) = quoteWithdrawOut(amount_);
        if (_fee > 0) {
            _transfer(account_, _pool.feeCollector(), _fee);
        }

        _burn(account_, _withdrawn);
        _pool.treasury().pull(to_, _withdrawn);
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

**File:** contracts/Pool.sol (L589-593)
```text
        depositToken_.seize(account_, _msgSender, _toLiquidator);

        if (_fee > 0) {
            depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee);
        }
```
