/**
 * screenerRenderer.js
 * Interface compilation engine for UI rendering and layout building.
 */
import { COHORT_EXCEPTIONS } from './marketDataStore.js';

/**
 * Compiles a structured, enterprise-ready data row for interactive table viewport insertion.
 * @param {Object} counter - Individual sanitized counter tracking entity
 * @returns {string} Clean HTML string literal block
 */
export function renderScreenerRow(counter) {
  const name = counter.counter.trim();
  const isNew = COHORT_EXCEPTIONS.NEW_LISTINGS.includes(name);
  
  // 1. Fallback Strategy for Active Suspended Counters vs Missing Values
  let closeDisplay = counter.close != null ? `$${counter.close.toFixed(4)}` : '—';
  if (counter.suspended) {
    closeDisplay = '<span class="status-suspended" style="color: #ff4d4d; font-size: 10px; font-weight: bold;">SUSP</span>';
  }

  // 2. Performance Tracking Validation for Post-Baseline Inceptions
  const ytdDisplay = isNew ? 'New Listing' : `${(counter.ytd * 100).toFixed(2)}%`;
  const trendClass = isNew ? 'text-neutral' : (counter.ytd >= 0 ? 'text-gain' : 'text-loss');
  
  // 3. Dynamic badge parsing for freshly registered instruments
  const badgeHtml = isNew ? '<span class="sect sect-v" style="margin-left:6px; font-size:8px; padding: 2px 4px;">NEW</span>' : '';

  return `
    <tr class="audit-row">
      <td class="font-mono font-medium" style="font-weight:500;">${counter.counter} ${badgeHtml}</td>
      <td class="market-tag" style="color:var(--ts); font-size:12px;">${counter.market}</td>
      <td class="text-muted" style="color:var(--tm); font-size:13px;">${counter.sector ?? 'Unclassified'}</td>
      <td class="text-right font-mono" style="font-weight: 500;">${closeDisplay}</td>
      <td class="text-right font-mono ${trendClass}">${ytdDisplay}</td>
    </tr>
  `;
}
