/**
 * Identifiers in the same shape as azmonitor/appstate/__init__.py `new_id`: a prefix, ten
 * characters of millisecond time and sixteen of randomness, in lower-case Crockford base32. Time
 * first so they sort in creation order; randomness so two created in the same millisecond differ.
 */
const ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz";

export const JOB_ID_PATTERN = /^job_[0-9a-hjkmnp-tv-z]{26}$/;

export function newId(prefix: string, now: number = Date.now()): string {
  let ms = now;
  let head = "";
  for (let i = 0; i < 10; i += 1) {
    head = ALPHABET[ms % 32] + head;
    ms = Math.floor(ms / 32);
  }
  const random = crypto.getRandomValues(new Uint8Array(16));
  let tail = "";
  for (const byte of random) tail += ALPHABET[byte % 32];
  return `${prefix}_${head}${tail}`;
}
