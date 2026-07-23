I was unable to read any of the source files in this repository — all `read_file` calls returned `Error: Missing required params`. I only have the directory structure. Without access to the actual contract code in:

- `smart-contracts-poc/contracts/AnchoredPriceProvider.sol`
- `smart-contracts-poc/contracts/AnchoredProviderFactory.sol`
- `smart-contracts-poc/contracts/PriceProvider.sol`
- `smart-contracts-poc/contracts/ProtectedPriceProvider.sol`
- `metric-core/contracts/MetricOmmPool.sol`
- `metric-core/contracts/MetricOmmPoolFactory.sol`

...I cannot trace the bid/ask quote path, verify whether `getBidAndAskPrice` properly constrains carry/state values on reset conditions, or confirm whether any guard is missing. Fabricating a finding without reading the code would be irresponsible.

Due to index size limits, the file contents of this repository are not available to me. To perform this analysis properly, please start a Devin session with full filesystem access so the actual Solidity source can be read and the analog to the underconstrained carry-value pattern can be properly traced through the price provider and pool swap path.