/* Token figures as the dashboard prints them. A count the source did not record is a dash, never 0. */

export function fmtNum(n) {
  if (n === undefined || n === null) return '—';
  return Number(n).toLocaleString();
}

// One-line token breakdown for a row's tooltip; cache buckets stay separate from input volume.
export function tokBreakdown(r) {
  return 'uncached in ' + fmtNum(r.uncached_input_tokens)
    + ' · cache read ' + fmtNum(r.cache_read_tokens)
    + ' · cache write ' + fmtNum(r.cache_write_tokens)
    + ' · out ' + fmtNum(r.completion_tokens)
    + ' · total in ' + fmtNum(r.total_input_tokens)
    + ' · effective ' + fmtNum(r.effective_input_tokens) + ' in / ' + fmtNum(r.effective_output_tokens) + ' out'
    + (r.tokens_per_credit == null ? '' : ' · ' + fmtNum(r.tokens_per_credit) + ' tok/credit');
}

// How many requests carry no token figures, so a total reads as "of the ones that did".
export function unknownTokensNote(r) {
  return r.unknown_token_requests ? fmtNum(r.unknown_token_requests) + ' without token data' : '';
}

export function cacheLine(r) {
  const bits = [];
  if (r.cache_read_tokens) bits.push(fmtTok(r.cache_read_tokens) + ' cache read');
  if (r.cache_write_tokens) bits.push(fmtTok(r.cache_write_tokens) + ' cache write');
  return bits.join(' · ');
}

export function fmtTok(n) {
  if (n === undefined || n === null) return '—';
  n = Number(n) || 0;
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1) + 'M';
  if (n >= 1_000) return (n / 1_000).toFixed(n >= 10_000 ? 0 : 1) + 'k';
  return String(n);
}
