/**
 * marketDataStore.js
 * Centralized data repository and structural sanitization layer for ZSE & VFEX indices.
 */

// Master Registry of Historical Cohort Exclusions
export const COHORT_EXCEPTIONS = {
  // Instruments listed after the January 6, 2026 baseline benchmark.
  // Active for real-time transaction session volume but omitted from Point-to-Point models.
  NEW_LISTINGS: ['BAT', 'Starafrica', 'Pfuma', 'Pfuma REIT'],
  
  // Corrupted tracking duplicates generated at the footer of raw spreadsheet files.
  // Checked defensively via loop validations to insulate core chart metrics.
  CORRUPTED_PARSER_ROWS: ['Mash Holdings', 'Dairibord', 'CBZ', 'This Price Sheet is based']
};

export class MarketDataStore {
  /**
   * @param {Object} rawJsonData - The unparsed JSON object payload imported from server root.
   */
  constructor(rawJsonData) {
    this.generatedAt = rawJsonData.generated || new Date().toISOString();
    this.priceDate = rawJsonData.priceDate || 'Unknown Price Date';
    this.baseDate = rawJsonData.baseDate || 'Unknown Baseline Epoch';
    
    // Ingest and scrub raw counters array directly upon initial load execution
    this.counters = (rawJsonData.counters || []).filter(c => this.isValidRecord(c));
  }

  /**
   * Defensive Pipeline Verification Check
   * Rejects malformed metadata strings and broken shifted spreadsheet cells.
   */
  isValidRecord(c) {
    if (!c.counter) return false;
    
    const name = c.counter.trim();
    
    // Drop known broken tail-end rows where parser pushed layout header text into numeric data fields
    if (COHORT_EXCEPTIONS.CORRUPTED_PARSER_ROWS.includes(name) && c.shares === 0) {
      return false;
    }
    
    return true;
  }

  /**
   * Filters out post-baseline corporate listings and extracts pure January cohort members.
   * Directly feeds simulation data tracks to prevent mathematical data skewing.
   */
  getBaselineCohort() {
    return this.counters.filter(c => 
      c.inv100 !== null && 
      !COHORT_EXCEPTIONS.NEW_LISTINGS.includes(c.counter.trim())
    );
  }

  /**
   * Returns all active valid instruments trading on ZSE or VFEX boards.
   * Feeds global market screener layout tables, trading activity totals, and capitalization grids.
   */
  getAllActiveInstruments() {
    return this.counters;
  }
}
