### Title
Fee rounding to zero for low-decimal collateral allows repeatable fee-free deposits/withdrawals - ([File: contracts/DepositToken.sol])

### Summary
All collateral fees in Metronome (`depositFee`, `withdrawFee`, `repayFee`, `issueFee`, `swapFees`) are computed with `WadRayMath.wadMul`, which returns `(amount * fee + 0.5e18) / 1e18`. For tokens with few decimals, small absolute amounts make the fee term round to zero, and because `deposit` and `withdraw` have no minimum-amount floor, an attacker can repeat dust-sized operations to move arbitrarily large collateral while paying zero `depositFee`/`withdrawFee` — the feeCollector receives nothing.

### Finding Description
`DepositToken.deposit` pulls `amount_` of underlying into the Treasury, then computes the fee via `quoteDepositOut(amount_)`, which does `_fee = amount_.wadMul(_depositFee)` [1](#0-0) . `wadMul` rounds half-up at the 1-unit granularity of the token [2](#0-1) , so `_fee == 0` whenever `amount_ * _depositFee < 0.5e18`. With a 1% fee (`1e16`), any deposit of fewer than 50 base units pays no fee; with a 0.5% fee, any deposit under 100 units pays no fee.

The same applies to withdrawals: `_withdraw` calls `quoteWithdrawOut`, which is `amount_.wadMul(_withdrawFee)` [3](#0-2) , and `_fee` is only transferred to `pool.feeCollector()` when nonzero [4](#0-3) . Neither `deposit` nor `_withdraw` enforces a minimum size [5](#0-4) , so the operation is repeatable in arbitrarily small chunks.

For a 6-decimal collateral (USDC) the fee-free chunk is ~$0.00005, so evasion is uneconomic. For a 2-decimal collateral (e.g., GUSD-listed Vesper or tokenized assets supported through `VesperGateway`), the fee-free chunk is ~$0.50 at 1% fee, making mass-chunking practical on a low-fee chain (Base/Optimism deployments exist). This is the same bug class as the Arrakis finding: fee math divided at token-unit granularity yields zero fee for low-decimal tokens.

### Impact Explanation
The protocol's `feeCollector` receives no `depositFee`/`withdrawFee` on chunked operations, while users fully avoid the intended fee on any total amount — direct loss of protocol revenue and asymmetric treatment of low-decimal collateral. It also affects liquidation-related fee paths that use the same `wadMul` pattern (liquidator incentive/protocol fee computed on seized collateral amounts).

### Likelihood Explanation
Requires a whitelisted low-decimal collateral and a nonzero fee. Evasion is bounded by gas per call; on L2s the fee saved per chunk (~$0.005 at 1% on $0.50) can exceed L2 gas, and profitability grows with the fee rate or lower-decimal tokens. No privileged role needed — `deposit` and `withdraw` are public, gated only by `whenNotPaused`/`nonReentrant`, which do not stop it.

### Recommendation
Enforce a minimum fee of 1 unit (round up: `(a*b + WAD - 1) / WAD`) on fee-bearing paths, or enforce a minimum deposit/withdraw amount scaled to the underlying token's decimals so `amount_ * fee` cannot round to zero.

### Proof of Concept
Fork test sketch (Hardhat, Base fork): whitelist a 2-decimal token as `underlying` for a `DepositToken`, set `depositFee = 1e16` via governor in test setup. Then:
```solidity
// deposit(49, attacker) repeated N times
for (uint i; i < N; ++i) {
    depositToken.deposit(49, attacker); // quoteDepositOut(49): 49*1e16/1e18 = 0.49 -> 0
}
// msd balance of attacker == 49*N, feeCollector balance == 0
// vs deposit(49*N) once -> fee = floor((49*N*1e16 + 0.5e18)/1e18) > 0
```
Assert `balanceOf(feeCollector) == 0` after chunked deposits while the single-shot equivalent charges a nonzero fee. Note: a deployed whitelisted 2-decimal collateral is a precondition I could not fully verify in the indexed deployment config; if none exists, the issue only materializes if/when such collateral is added.

### Citations

**File:** contracts/DepositToken.sol (L214-237)
```text
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

**File:** contracts/DepositToken.sol (L294-301)
```text
    function quoteDepositOut(uint256 amount_) public view override returns (uint256 _amountToDeposit, uint256 _fee) {
        uint256 _depositFee = pool.feeProvider().depositFee();
        if (_depositFee == 0) {
            return (amount_, _fee);
        }

        _fee = amount_.wadMul(_depositFee);
        _amountToDeposit = amount_ - _fee;
```

**File:** contracts/DepositToken.sol (L326-334)
```text
    function quoteWithdrawOut(uint256 amount_) public view override returns (uint256 _amountToWithdraw, uint256 _fee) {
        uint256 _withdrawFee = pool.feeProvider().withdrawFee();
        if (_withdrawFee == 0) {
            return (amount_, _fee);
        }

        _fee = amount_.wadMul(_withdrawFee);
        _amountToWithdraw = amount_ - _fee;
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

**File:** contracts/lib/WadRayMath.sol (L25-31)
```text
    function wadMul(uint256 a, uint256 b) internal pure returns (uint256) {
        if (a == 0 || b == 0) {
            return 0;
        }

        return (a * b + HALF_WAD) / WAD;
    }
```
