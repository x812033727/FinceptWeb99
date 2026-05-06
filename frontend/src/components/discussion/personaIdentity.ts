// Per-persona visual identity. Hashes a persona id (e.g. "buffett",
// "market_analyst") to a stable HSL hue + matching accent classes,
// plus a 1-2 char initial for the avatar circle. Same id → same
// look across the entire app, even after refresh, so an 8-persona
// transcript stays scannable.

export interface PersonaIdentity {
  /** 0-359 HSL hue. Stable per id. */
  hue: number;
  /** Inline-style colour for accent bars / avatar fill. */
  accentColor: string;
  /** Slightly darker variant for ring borders. */
  ringColor: string;
  /** Soft tint for the bubble's left-edge accent. */
  softColor: string;
  /** 1-2 char glyph for the avatar circle. */
  initial: string;
}

// Pre-defined hue palette. Avoids the warning-colour bands
// already in use elsewhere:
//   - red 0-20 (dissent stance, error pills)
//   - amber 25-50 (user_input stance, post-mortem chip, win-skip)
//   - purple 270-320 (post-mortem conclusion card)
// 19 entries cover every persona — hash assignment is stable so
// the same id always lands on the same hue across reloads.
const HUE_BAND = [
  60, 70, 80, 90, // yellow-greens to greens
  110, 130, 150, 170, // greens to teals
  185, 200, 215, 230, 245, 260, // teals to blues to indigos
  335, 345, 355, // pinks (just under red)
  100, 195, // extras for collisions
];

function hashId(id: string): number {
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h;
}

/** First grapheme of the localised persona name, uppercased.
 *  Single CJK char passes through unchanged; ASCII names get the
 *  first letter (e.g. "Buffett" → "B"). */
function deriveInitial(localizedName: string, fallbackId: string): string {
  const source = (localizedName || fallbackId).trim();
  if (!source) return "?";
  // CJK / non-ASCII first character: take it as-is.
  const first = [...source][0];
  if (!first) return "?";
  if (/[A-Za-z]/.test(first)) return first.toUpperCase();
  return first;
}

export function getPersonaIdentity(
  personaId: string,
  localizedName: string,
  /** Optional explicit short label from i18n (`personas.agents.<id>.short`). */
  explicitInitial?: string,
): PersonaIdentity {
  const hash = hashId(personaId);
  const hue = HUE_BAND[hash % HUE_BAND.length];
  const initial = explicitInitial?.trim() || deriveInitial(localizedName, personaId);
  return {
    hue,
    accentColor: `hsl(${hue} 70% 55%)`,
    ringColor: `hsl(${hue} 65% 45%)`,
    softColor: `hsl(${hue} 60% 50% / 0.18)`,
    initial,
  };
}
