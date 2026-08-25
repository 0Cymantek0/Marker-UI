/** Render a long server token (state/digest/revision id) in a compact form
 *  for inline display. The full value stays available via `title` tooltips
 *  and the copy button, so truncation never destroys information. */
export function shortToken(value: string, keep = 12): string {
  return value.length > keep ? `${value.slice(0, keep)}…` : value
}
