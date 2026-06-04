/**
 * investmentEngine.js
 * High-performance analytics matrix math engine for investment progression tracking.
 */

/**
 * Computes unified point-to-point progression indices across the baseline active cohort.
 * @param {Array} cohortCounters - Clean baseline cohort array from MarketDataStore
 * @param {number} principalAmount - The dynamic investment baseline amount (Default: $100)
 * @returns {Object} Analytical KPI metrics block
 */
export function calculateInvestmentMetrics(cohortCounters, principalAmount = 100) {
  let totalSimulatedValue = 0;
  let winnersCount = 0;
  let losersCount = 0;

  cohortCounters.forEach(c => {
    // 1. Fallback Rule: Gracefully replace missing performance indicators with flat par baseline index (100)
    const performanceFactor = (c.inv100 ?? 100) / 100;
    
    // 2. Extrapolate compound return indices relative to the runtime input principal
    const currentCounterValue = principalAmount * performanceFactor;
    totalSimulatedValue += currentCounterValue;
    
    // 3. Track distribution performance metrics split benchmarks
    if (c.inv100 > 100) winnersCount++;
    if (c.inv100 < 100) losersCount++;
  });

  // 4. Compute safe portfolio averages avoiding zero-division runtime errors
  const totalCounters = cohortCounters.length;
  const portfolioAverage = totalCounters > 0 ? (totalSimulatedValue / totalCounters) : principalAmount;

  return {
    totalValue: totalSimulatedValue,
    averageValue: portfolioAverage,
    winners: winnersCount,
    losers: losersCount,
    count: totalCounters
  };
}
